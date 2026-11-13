//! OhMyLLM native window.
//!
//! The window is a thin shell: it boots the Python bridge (`python -m
//! ohmyllm.bridge`) on a private loopback port, waits for the port/token
//! file, then points the webview at the served console. The bridge process
//! is killed when this app exits, so a closed window means no orphaned
//! server holding your workspace.

use std::fs;
use std::io::{ErrorKind, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url};

struct BridgeHandle(Mutex<Option<Child>>);

struct LaunchArtifacts {
    dir: PathBuf,
    discovery: PathBuf,
}

impl LaunchArtifacts {
    fn new(dir: PathBuf) -> Self {
        let discovery = dir.join("bridge.json");
        Self { dir, discovery }
    }
}

impl Drop for LaunchArtifacts {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.discovery);
        let _ = fs::remove_dir(&self.dir);
    }
}

struct OwnedChild(Option<Child>);

impl OwnedChild {
    fn new(child: Child) -> Self {
        Self(Some(child))
    }

    fn child_mut(&mut self) -> &mut Child {
        self.0.as_mut().expect("owned child is present")
    }

    fn into_inner(mut self) -> Child {
        self.0.take().expect("owned child is present")
    }
}

impl Drop for OwnedChild {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            terminate_child(child);
        }
    }
}

#[cfg(unix)]
extern "C" {
    fn kill(pid: i32, signal: i32) -> i32;
}

fn terminate_child(child: &mut Child) {
    if child.try_wait().ok().flatten().is_some() {
        return;
    }
    #[cfg(unix)]
    unsafe {
        let _ = kill(child.id() as i32, 15);
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Err(_) => break,
            Ok(None) => thread::sleep(Duration::from_millis(25)),
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

const DISCOVERY_MAX_BYTES: u64 = 4096;

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
        if Command::new(cand)
            .arg("-c")
            .arg("import ohmyllm")
            .env_clear()
            .env("PATH", "/usr/local/bin:/usr/bin:/bin")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .ok()
            .map(|s| s.success())
            .unwrap_or(false)
        {
            return PathBuf::from(cand);
        }
    }
    PathBuf::from("python3")
}

fn parse_discovery(info: &str) -> Result<(u16, String, u32), String> {
    // The Python side emits this compact, versioned shape. Parsing the exact
    // grammar keeps the native shell dependency-free and rejects extra fields.
    let rest = info
        .trim()
        .strip_prefix("{\"version\":1,\"port\":")
        .ok_or("invalid bridge discovery version")?;
    let (port_raw, rest) = rest
        .split_once(",\"token\":\"")
        .ok_or("invalid bridge discovery port")?;
    let (token, pid_raw) = rest
        .split_once("\",\"pid\":")
        .ok_or("invalid bridge discovery token")?;
    let pid_raw = pid_raw
        .strip_suffix('}')
        .ok_or("invalid bridge discovery record")?;
    let port: u16 = port_raw
        .parse()
        .map_err(|_| "invalid bridge discovery port")?;
    let pid: u32 = pid_raw
        .parse()
        .map_err(|_| "invalid bridge discovery process")?;
    if port == 0
        || pid == 0
        || token.len() != 32
        || !token
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err("invalid bridge discovery values".into());
    }
    Ok((port, token.to_string(), pid))
}

fn read_private_discovery(path: &Path, launch_dir: &Path) -> Result<Option<String>, String> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("failed to inspect bridge discovery: {error}")),
    };
    if before.file_type().is_symlink() || !before.is_file() {
        return Err("bridge discovery is not a regular file".into());
    }
    if before.len() > DISCOVERY_MAX_BYTES {
        return Err("bridge discovery is too large".into());
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let parent = fs::symlink_metadata(launch_dir)
            .map_err(|error| format!("failed to inspect launch directory: {error}"))?;
        if parent.file_type().is_symlink()
            || !parent.is_dir()
            || parent.mode() & 0o077 != 0
            || before.uid() != parent.uid()
            || before.mode() & 0o777 != 0o600
            || before.nlink() != 1
        {
            return Err("bridge discovery permissions are not private".into());
        }
    }

    let file = fs::File::open(path)
        .map_err(|error| format!("failed to open bridge discovery: {error}"))?;
    let opened = file
        .metadata()
        .map_err(|error| format!("failed to inspect open bridge discovery: {error}"))?;
    let after = fs::symlink_metadata(path)
        .map_err(|error| format!("bridge discovery changed while opening: {error}"))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if before.dev() != opened.dev()
            || before.ino() != opened.ino()
            || after.dev() != opened.dev()
            || after.ino() != opened.ino()
        {
            return Err("bridge discovery changed while opening".into());
        }
    }

    if !opened.is_file() || opened.len() > DISCOVERY_MAX_BYTES {
        return Err("bridge discovery is not a bounded regular file".into());
    }
    let mut raw = String::new();
    file.take(DISCOVERY_MAX_BYTES + 1)
        .read_to_string(&mut raw)
        .map_err(|error| format!("failed to read bridge discovery: {error}"))?;
    if raw.len() as u64 > DISCOVERY_MAX_BYTES {
        return Err("bridge discovery is too large".into());
    }
    Ok(Some(raw))
}

fn launch_nonce() -> Result<String, String> {
    #[cfg(unix)]
    {
        let mut bytes = [0u8; 16];
        fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut bytes))
            .map_err(|e| format!("secure randomness unavailable: {e}"))?;
        return Ok(bytes.iter().map(|b| format!("{b:02x}")).collect());
    }
    #[cfg(not(unix))]
    {
        let value = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "system clock is unavailable")?
            .as_nanos();
        Ok(format!("{value:x}"))
    }
}

fn spawn_bridge() -> Result<(Child, u16, String), String> {
    let launch_dir = std::env::temp_dir().join(format!(
        "ohmyllm-gui-{}-{}",
        std::process::id(),
        launch_nonce()?
    ));
    fs::create_dir(&launch_dir)
        .map_err(|e| format!("failed to create private launch directory: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Err(e) = fs::set_permissions(&launch_dir, fs::Permissions::from_mode(0o700)) {
            let _ = fs::remove_dir(&launch_dir);
            return Err(format!("failed to protect launch directory: {e}"));
        }
    }
    let artifacts = LaunchArtifacts::new(launch_dir);
    let port_file = artifacts.discovery.clone();
    let python = pick_python();
    let mut command = Command::new(&python);
    command
        .arg("-m")
        .arg("ohmyllm.bridge")
        .arg("--port-file")
        .arg(&port_file);
    command
        .env_clear()
        .env("PYTHONUNBUFFERED", "1")
        .env("PATH", "/usr/local/bin:/usr/bin:/bin");
    for key in [
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "WINDIR",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "OMLLM_HOME",
        "OMLLM_WORKSPACE",
    ] {
        if let Ok(value) = std::env::var(key) {
            command.env(key, value);
        }
    }
    let child = match command.spawn() {
        Ok(child) => child,
        Err(e) => {
            return Err(format!(
                "failed to launch {} -m ohmyllm.bridge: {e}",
                python.display()
            ));
        }
    };
    let mut child = OwnedChild::new(child);

    let deadline = Instant::now() + Duration::from_secs(15);
    let result = loop {
        match read_private_discovery(&port_file, &artifacts.dir) {
            Ok(Some(raw)) => match parse_discovery(&raw) {
                Ok((port, token, pid)) if pid == child.child_mut().id() => break Ok((port, token)),
                Ok(_) => break Err("bridge discovery process does not match child".into()),
                Err(error) => break Err(error),
            },
            Ok(None) => {}
            Err(error) => break Err(error),
        }
        if let Some(status) = child.child_mut().try_wait().ok().flatten() {
            break Err(format!("bridge exited early ({status}) — is ohmyllm installed? try: pipx install ohmyllm or run `ohmyllm doctor`"));
        }
        if Instant::now() > deadline {
            break Err("bridge did not come up within 15s".into());
        }
        thread::sleep(Duration::from_millis(80));
    };
    let (port, token) = result?;
    Ok((child.into_inner(), port, token))
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
        println!("OhMyLLM console ready on loopback port {port}");
    }
    let token_run = token.clone();

    tauri::Builder::default()
        .manage(BridgeHandle(Mutex::new(Some(child))))
        .setup(move |app| {
            if port > 0 {
                let url = Url::parse(&format!("http://127.0.0.1:{port}/#token={token}"))
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
                                terminate_child(&mut child);
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
            "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-OMLLM-Token: {token}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}"
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
    cmd.env_clear()
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("trivial child")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn launch_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "ohmyllm-gui-test-{}-{}",
            std::process::id(),
            launch_nonce().expect("secure test nonce")
        ));
        fs::create_dir(&dir).expect("create isolated launch directory");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))
                .expect("protect isolated launch directory");
        }
        dir
    }

    #[test]
    fn discovery_parser_rejects_extra_fields_and_bad_tokens() {
        let valid = "{\"version\":1,\"port\":1234,\"token\":\"0123456789abcdef0123456789abcdef\",\"pid\":42}";
        assert_eq!(parse_discovery(valid).expect("valid record").2, 42);
        assert!(parse_discovery(&valid.replace("}", ",\"extra\":true}")).is_err());
        assert!(parse_discovery(&valid.replace("0123456789abcdef", "ABCDEF0123456789")).is_err());
    }

    #[test]
    fn discovery_reader_enforces_size_bound() {
        let dir = launch_dir();
        let path = dir.join("bridge.json");
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .expect("create oversized discovery");
        file.write_all(&vec![b'x'; DISCOVERY_MAX_BYTES as usize + 1])
            .expect("write oversized discovery");
        drop(file);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .expect("protect discovery");
        }
        assert!(read_private_discovery(&path, &dir).is_err());
        fs::remove_file(path).expect("remove test discovery");
        fs::remove_dir(dir).expect("remove test launch directory");
    }

    #[cfg(unix)]
    #[test]
    fn discovery_reader_rejects_symlinks() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let dir = launch_dir();
        let target = dir.join("target.json");
        let path = dir.join("bridge.json");
        fs::write(&target, "{}").expect("write symlink target");
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600))
            .expect("protect symlink target");
        symlink(&target, &path).expect("create discovery symlink");
        assert!(read_private_discovery(&path, &dir).is_err());
        fs::remove_file(path).expect("remove test symlink");
        fs::remove_file(target).expect("remove test target");
        fs::remove_dir(dir).expect("remove test launch directory");
    }
}
