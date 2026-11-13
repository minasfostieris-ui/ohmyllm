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

/// Whether this process owns the bridge child. An attached bridge belongs
/// to its launcher (the CLI) and must survive closing this window.
struct BridgeOwned(bool);

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
        let cand = PathBuf::from(p);
        if cand.is_file() {
            return cand;
        }
    }
    // The shipped binary lives inside the installing venv
    // (<venv>/lib/python3.X/site-packages/ohmyllm/gui_bin/): walk up to that
    // venv's interpreter so uv-tool/pipx installs never fall back to a
    // system python that cannot import ohmyllm (the old splash stall).
    if let Ok(exe) = std::env::current_exe() {
        if let Some(py) = venv_python_near(&exe) {
            return py;
        }
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

/// Find the interpreter of the venv that installed this executable, marked
/// by `pyvenv.cfg` so a bare system directory never masquerades as a venv.
fn venv_python_near(exe: &Path) -> Option<PathBuf> {
    for base in exe.ancestors().skip(1) {
        if base.join("pyvenv.cfg").is_file() {
            for cand in [base.join("bin/python3"), base.join("bin/python")] {
                if cand.is_file() {
                    return Some(cand);
                }
            }
            return None;
        }
    }
    None
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
            break Err(format!(
                "bridge exited early ({status}) using {} — is ohmyllm installed for that interpreter? try: pipx install ohmyllm or run `ohmyllm doctor`",
                python.display()
            ));
        }
        if Instant::now() > deadline {
            break Err("bridge did not come up within 15s".into());
        }
        thread::sleep(Duration::from_millis(80));
    };
    let (port, token) = result?;
    Ok((child.into_inner(), port, token))
}

/// Attach to a bridge another launcher (the CLI) already started, using the
/// same owner-private discovery contract the headless checker uses. Keeps
/// exactly one bridge per console instead of stacking a second one.
fn attach_bridge_from_env() -> Result<Option<(u16, String)>, String> {
    let raw = match std::env::var("OMLLM_BRIDGE_DISCOVERY") {
        Ok(value) if !value.is_empty() => value,
        _ => return Ok(None),
    };
    attach_from_path(&PathBuf::from(raw)).map(Some)
}

fn attach_from_path(path: &Path) -> Result<(u16, String), String> {
    let dir = path
        .parent()
        .ok_or("bridge discovery has no parent directory")?
        .to_path_buf();
    let raw = read_private_discovery(path, &dir)?.ok_or("bridge discovery file is missing")?;
    let (port, token, pid) = parse_discovery(&raw)?;
    if !pid_alive(pid) {
        return Err(format!("bridge process {pid} is not running"));
    }
    Ok((port, token))
}

fn pid_alive(pid: u32) -> bool {
    #[cfg(unix)]
    unsafe {
        // kill(-1, 0) probes *every* process and always succeeds: reject
        // pids that would wrap negative before asking the kernel.
        pid <= i32::MAX as u32 && kill(pid as i32, 0) == 0
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        true
    }
}

fn percent_encode(value: &str) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

fn main() {
    let mut splash_error = String::new();
    let (child, port, token, owned) = match attach_bridge_from_env() {
        Ok(Some((port, token))) => (None, port, token, false),
        Ok(None) => match spawn_bridge() {
            Ok((child, port, token)) => (Some(child), port, token, true),
            Err(msg) => {
                eprintln!("ohmyllm-gui: {msg}");
                splash_error = msg;
                (None, 0, String::new(), true)
            }
        },
        Err(msg) => {
            eprintln!("ohmyllm-gui: {msg}");
            splash_error = msg;
            (None, 0, String::new(), true)
        }
    };

    if port > 0 {
        println!("OhMyLLM console ready on loopback port {port}");
    }
    let token_run = token.clone();

    tauri::Builder::default()
        .manage(BridgeHandle(Mutex::new(child)))
        .manage(BridgeOwned(owned))
        .setup(move |app| {
            if let Some(win) = app.get_webview_window("main") {
                if port > 0 {
                    let url = Url::parse(&format!("http://127.0.0.1:{port}/#token={token}"))
                        .map_err(|e| format!("bad bridge url: {e}"))?;
                    let _ = win.navigate(url);
                } else if !splash_error.is_empty() {
                    // Surface the real failure on the splash instead of an
                    // endless "starting the local bridge…" spinner.
                    if let Ok(mut url) = win.url() {
                        url.set_query(Some(&format!(
                            "bridge_error={}",
                            percent_encode(&splash_error)
                        )));
                        let _ = win.navigate(url);
                    }
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building OhMyLLM window")
        .run(move |app_handle, event| {
            match event {
                RunEvent::Exit | RunEvent::ExitRequested { .. } => {
                    let owned = app_handle
                        .try_state::<BridgeOwned>()
                        .map(|s| s.0)
                        .unwrap_or(true);
                    if !owned {
                        return; // attached bridge belongs to its launcher
                    }
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

    #[test]
    fn percent_encode_escapes_query_payload() {
        assert_eq!(
            percent_encode("bridge exited early (1)"),
            "bridge%20exited%20early%20%281%29"
        );
    }

    #[test]
    fn attach_accepts_live_private_discovery_and_rejects_dead_pid() {
        let dir = launch_dir();
        let path = dir.join("bridge.json");
        let token = "0123456789abcdef0123456789abcdef";
        let live = format!(
            "{{\"version\":1,\"port\":1234,\"token\":\"{token}\",\"pid\":{}}}",
            std::process::id()
        );
        fs::write(&path, live).expect("write live discovery");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .expect("protect discovery");
        }
        let (port, got) = attach_from_path(&path).expect("attach to live bridge");
        assert_eq!(port, 1234);
        assert_eq!(got, token);
        let dead = format!(
            "{{\"version\":1,\"port\":1234,\"token\":\"{token}\",\"pid\":{}}}",
            u32::MAX
        );
        fs::write(&path, dead).expect("write dead-pid discovery");
        assert!(attach_from_path(&path).is_err());
        fs::remove_file(&path).expect("remove test discovery");
        fs::remove_dir(&dir).expect("remove test launch directory");
    }

    #[test]
    fn attach_rejects_missing_discovery() {
        let dir = launch_dir();
        assert!(attach_from_path(&dir.join("bridge.json")).is_err());
        fs::remove_dir(&dir).expect("remove test launch directory");
    }

    #[test]
    fn venv_python_near_finds_installing_interpreter() {
        let root = launch_dir();
        let venv = root.join("venv");
        let bin = venv.join("bin");
        fs::create_dir_all(&bin).expect("create venv bin");
        let py = bin.join("python3");
        fs::write(&py, "").expect("create venv python");
        fs::write(venv.join("pyvenv.cfg"), "").expect("mark venv root");
        let gui_bin = venv.join("lib/python3.13/site-packages/ohmyllm/gui_bin");
        fs::create_dir_all(&gui_bin).expect("create shipped binary dir");
        let exe = gui_bin.join("ohmyllm-gui");
        fs::write(&exe, "").expect("create shipped binary");
        assert_eq!(venv_python_near(&exe), Some(py));
        assert_eq!(venv_python_near(&root.join("bare")), None);
        fs::remove_dir_all(&root).expect("remove test tree");
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
