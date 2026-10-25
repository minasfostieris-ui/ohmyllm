//! OhMyLLM native window.
//!
//! The window is a thin shell: it boots the Python bridge (`python -m
//! ohmyllm.bridge`) on a private loopback port, waits for the port/token
//! file, then points the webview at the served console. The bridge process
//! is killed when this app exits, so a closed window means no orphaned
//! server holding your workspace.

use std::fs;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url};

struct BridgeHandle(Mutex<Option<Child>>);

fn pick_python() -> PathBuf {
    if let Ok(p) = std::env::var("OMLLM_PYTHON") {
        return PathBuf::from(p);
    }
    // If the packaged binary sits next to a venv install (…/bin/ohmyllm),
    // prefer that interpreter so `ohmyllm gui` always finds the package.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for cand in [dir.join("python3"), dir.join("python")] {
                if cand.exists() {
                    return cand;
                }
            }
        }
    }
    for cand in ["python3", "python"] {
        if Command::new(cand).arg("-c").arg("import ohmyllm").stdout(Stdio::null()).stderr(Stdio::null()).status().ok().map(|s| s.success()).unwrap_or(false) {
            return PathBuf::from(cand);
        }
    }
    PathBuf::from("python3")
}

fn spawn_bridge() -> Result<(Child, u16, String), String> {
    let port_file: PathBuf = std::env::temp_dir().join(format!("ohmyllm-gui-{}.port", std::process::id()));
    let _ = fs::remove_file(&port_file);
    let python = pick_python();
    let mut child = Command::new(&python)
        .arg("-m")
        .arg("ohmyllm.bridge")
        .arg("--port-file")
        .arg(&port_file)
        .env("PYTHONUNBUFFERED", "1")
        .spawn()
        .map_err(|e| format!("failed to launch {} -m ohmyllm.bridge: {e}", python.display()))?;

    let deadline = Instant::now() + Duration::from_secs(15);
    let info = loop {
        if let Ok(s) = fs::read_to_string(&port_file) {
            if s.trim().split(' ').count() >= 2 {
                break s;
            }
        }
        if let Some(status) = child.try_wait().ok().flatten() {
            let _ = fs::remove_file(&port_file);
            return Err(format!("bridge exited early ({status}) — is ohmyllm installed? try: pipx install ohmyllm or run `ohmyllm doctor`"));
        }
        if Instant::now() > deadline {
            return Err("bridge did not come up within 15s".into());
        }
        thread::sleep(Duration::from_millis(80));
    };
    let _ = fs::remove_file(&port_file);
    let mut parts = info.trim().splitn(2, ' ');
    let port: u16 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    let token = parts.next().unwrap_or("").to_string();
    if port == 0 {
        return Err("bridge reported an invalid port".into());
    }
    Ok((child, port, token))
}

fn main() {
    let (child, port, token) = match spawn_bridge() {
        Ok(v) => v,
        Err(msg) => {
            eprintln!("ohmyllm-gui: {msg}");
            // Still open the window: its bundled page explains the fix.
            (silent_child(), 0, String::new())
        }
    };

    if port > 0 {
        // Print the loopback URL so the console can also be opened in a browser.
        println!("OhMyLLM console → http://127.0.0.1:{port}/?t={token}");
        println!("(loopback only · token required · open it in any browser if you prefer)");
    }
    let token_run = token.clone();

    tauri::Builder::default()
        .manage(BridgeHandle(Mutex::new(Some(child))))
        .setup(move |app| {
            if port > 0 {
                let url = Url::parse(&format!("http://127.0.0.1:{port}/?t={token}"))
                    .map_err(|e| format!("bad bridge url: {e}"))?;
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.navigate(url);
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building OhMyLLM window")
        .run(move |app_handle, event| {
            match event {
                RunEvent::Exit | RunEvent::ExitRequested { .. } => {
                    if port > 0 {
                        // ask the bridge to eject the model + stop cleanly
                        shutdown_bridge(port, &token_run);
                        std::thread::sleep(std::time::Duration::from_millis(2500));
                    }
                    if let Some(state) = app_handle.try_state::<BridgeHandle>() {
                        if let Ok(mut guard) = state.0.lock() {
                            if let Some(mut child) = guard.take() {
                                let _ = child.kill();
                                let _ = child.wait();
                            }
                        }
                    }
                }
                _ => {}
            }
        });
}

/// Raw one-shot HTTP POST so we add no new dependency; the bridge's SIGTERM
/// path may not run on Windows, so this is the graceful close channel.
fn shutdown_bridge(port: u16, token: &str) {
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;
    if let Ok(mut s) = TcpStream::connect(("127.0.0.1", port)) {
        let _ = s.set_write_timeout(Some(Duration::from_secs(2)));
        let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
        let req = format!(
            "POST /api/shutdown?t={token} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}"
        );
        let _ = s.write_all(req.as_bytes());
        let mut buf = [0u8; 16];
        let _ = s.read(&mut buf);
    }
}

/// A trivial short-lived process handle used when the bridge could not
/// start, so the shutdown path stays uniform.
fn silent_child() -> Child {
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("cmd");
        c.args(["/C", "exit"]);
        c
    };
    #[cfg(not(windows))]
    let mut cmd = {
        let mut c = Command::new("sleep");
        c.arg("0");
        c
    };
    cmd.stdout(Stdio::null()).stderr(Stdio::null()).spawn().expect("trivial child")
}
