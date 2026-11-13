"""Small, dependency-free security primitives used at trust boundaries."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SECRET_ENV_WORDS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "ACCESS_KEY",
    "PRIVATE_KEY", "CREDENTIAL", "AUTH", "COOKIE", "SESSION",
)


class UnsafePathError(ValueError):
    """Raised when a path crosses a symlink or escapes an expected root."""


def private_mkdir(path: Path) -> Path:
    """Create owner-only state without traversing symlinked components."""
    path = Path(path).expanduser()
    if has_symlink_component(path):
        raise UnsafePathError(f"private state path contains a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    if has_symlink_component(path):
        raise UnsafePathError(f"private state path contains a symlink: {path}")
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise UnsafePathError(f"private state path is not a real directory: {path}")
    if os.name == "posix":
        if st.st_uid != os.getuid():
            raise UnsafePathError(f"private state directory has the wrong owner: {path}")
        path.chmod(_PRIVATE_DIR_MODE)
    return path


def atomic_write(
    path: Path,
    content: str | bytes,
    *,
    mode: int = _PRIVATE_FILE_MODE,
    encoding: str = "utf-8",
    commit_check: Callable[[], None] | None = None,
) -> Path:
    """Replace *path* atomically using a unique sibling file.

    The caller chooses the file mode. Parent directories are created without
    changing existing workspace permissions; private-state callers must use
    :func:`private_mkdir` for their parent first.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode(encoding) if isinstance(content, str) else content
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    fd_owned_by_file = False
    try:
        if os.name == "posix":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fd_owned_by_file = True
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if commit_check is not None:
            commit_check()
        os.replace(tmp, path)
        if os.name == "posix":
            path.chmod(mode)
        return path
    except BaseException:
        if not fd_owned_by_file:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp.unlink(missing_ok=True)
        raise


def append_private(path: Path, content: str, *, max_bytes: int | None = None) -> None:
    """Append one bounded record while preserving a private file mode."""
    path = Path(path)
    private_mkdir(path.parent)
    data = content.encode("utf-8")
    if max_bytes is not None and len(data) > max_bytes:
        data = data[:max_bytes]
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        if os.name == "posix":
            os.fchmod(fd, _PRIVATE_FILE_MODE)
    finally:
        os.close(fd)


def file_identity(path: Path, *, max_bytes: int = 16_000_000) -> dict[str, Any]:
    """Snapshot existence, object metadata, and a bounded regular-file digest."""
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if stat.S_ISLNK(before.st_mode):
        raise UnsafePathError(f"target is a symlink: {path}")
    identity: dict[str, Any] = {
        "exists": True,
        "regular": stat.S_ISREG(before.st_mode),
        "mode": stat.S_IFMT(before.st_mode),
        "dev": before.st_dev,
        "ino": before.st_ino,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
    }
    if not stat.S_ISREG(before.st_mode):
        return identity
    if before.st_size > max_bytes:
        raise ValueError(f"target is too large to bind safely ({before.st_size} bytes)")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise UnsafePathError(f"target changed while binding approval: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"target is too large to bind safely: {path}")
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
    ):
        raise UnsafePathError(f"target changed while binding approval: {path}")
    identity["sha256"] = digest.hexdigest()
    return identity


def read_tail(path: Path, *, max_bytes: int = 64_000) -> str:
    """Read at most the final max_bytes from a regular, non-symlink file."""
    path = Path(path)
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"tail source is not a regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        size = os.fstat(fd).st_size
        os.lseek(fd, max(0, size - max_bytes), os.SEEK_SET)
        raw = os.read(fd, max_bytes)
    finally:
        os.close(fd)
    return raw.decode("utf-8", errors="replace")


def rotate_private_log(path: Path, *, max_bytes: int = 2_000_000) -> None:
    """Rotate one oversized private log to a single bounded backup."""
    path = Path(path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"log path is not a regular file: {path}")
    if st.st_size <= max_bytes:
        return
    backup = path.with_name(path.name + ".1")
    if backup.exists() and (backup.is_symlink() or not backup.is_file()):
        raise UnsafePathError(f"log backup path is unsafe: {backup}")
    os.replace(path, backup)
    if os.name == "posix":
        backup.chmod(_PRIVATE_FILE_MODE)


def has_symlink_component(path: Path) -> bool:
    """Return true if any existing lexical component is a symlink."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def resolve_path(
    raw: str | Path,
    *,
    base: Path,
    root: Path | None = None,
    reject_symlinks: bool = True,
) -> Path:
    """Resolve a path once, optionally confining it and rejecting symlinks."""
    base = Path(base).expanduser().resolve(strict=False)
    lexical = Path(str(raw)).expanduser()
    if not lexical.is_absolute():
        lexical = base / lexical
    if reject_symlinks and has_symlink_component(lexical):
        raise UnsafePathError(f"symlink traversal is not allowed: {raw}")
    resolved = lexical.resolve(strict=False)
    if root is not None:
        boundary = Path(root).expanduser().resolve(strict=False)
        try:
            inside = resolved == boundary or resolved.is_relative_to(boundary)
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise UnsafePathError(f"path escapes protected root: {raw}")
    return resolved


def is_within(path: Path, root: Path) -> bool:
    """Component-aware containment check."""
    try:
        path = Path(path).resolve(strict=False)
        root = Path(root).resolve(strict=False)
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def is_environment_file(path: Path) -> bool:
    """Recognize dotenv files and common environment-file variants."""
    name = Path(path).name.casefold()
    return name == ".env" or name.startswith(".env.") or name.startswith(".env-") or name.endswith(".env")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def private_file_ok(path: Path, *, owner_uid: int | None = None) -> bool:
    """Validate a regular, owner-only, non-symlink state file on POSIX."""
    path = Path(path)
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    if os.name == "posix":
        expected_uid = os.getuid() if owner_uid is None else owner_uid
        if st.st_uid != expected_uid or stat.S_IMODE(st.st_mode) & 0o077:
            return False
    return True


def read_private_json(path: Path, *, max_bytes: int = 16_384) -> Any:
    """Read bounded JSON only from a private regular file without following links."""
    path = Path(path)
    if not private_file_ok(path):
        raise UnsafePathError(f"insecure private state file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError("private JSON file is too large")
    return json.loads(raw.decode("utf-8"))


def read_bridge_discovery(path: Path) -> tuple[int, str]:
    """Validate the complete private bridge-discovery contract."""
    info = read_private_json(path)
    if not isinstance(info, dict) or set(info) != {"version", "port", "token", "pid"}:
        raise ValueError("invalid bridge discovery record")
    port, token = info.get("port"), info.get("token")
    if info.get("version") != 1 or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError("invalid bridge discovery port")
    if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid bridge discovery token")
    if not isinstance(info.get("pid"), int) or info["pid"] <= 0:
        raise ValueError("invalid bridge discovery process")
    return port, token


def sanitized_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a minimal child environment with inherited credentials removed."""
    keep = {
        "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
        "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
        "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
    }
    env = {
        key: value for key, value in os.environ.items()
        if key in keep and not any(word in key.upper() for word in _SECRET_ENV_WORDS)
    }
    trusted_dirs = [str(Path(sys.executable).resolve().parent)]
    trusted_dirs.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    env["PATH"] = os.pathsep.join(dict.fromkeys(trusted_dirs))
    env["NO_COLOR"] = "1"
    if extra:
        for key, value in extra.items():
            if not any(word in key.upper() for word in _SECRET_ENV_WORDS):
                env[str(key)] = str(value)
    return env


def url_is_loopback(url: str) -> bool:
    """Accept only explicit localhost names or loopback IP literals."""
    try:
        parsed = urlsplit(str(url))
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def trusted_executable(value: str) -> str | None:
    """Resolve a configured executable without consulting an inherited PATH."""
    candidate = Path(str(value)).expanduser()
    if candidate.name != str(value) or candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=True)
            return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
        except OSError:
            return None
    roots = [Path(sys.executable).resolve().parent, Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")]
    for root in roots:
        path = root / str(value)
        if path.is_file() and not path.is_symlink() and os.access(path, os.X_OK):
            return str(path)
    return None


def editor_argv(path: Path, editor: str | None = None) -> list[str]:
    """Build a non-shell editor invocation from EDITOR-style arguments."""
    raw = editor if editor is not None else os.environ.get("EDITOR", "vi")
    argv = shlex.split(raw, posix=os.name != "nt")
    if not argv:
        argv = ["vi"]
    return [*argv, str(Path(path))]


def private_temp_dir(parent: Path, prefix: str) -> Path:
    private_mkdir(parent)
    made = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    if os.name == "posix":
        made.chmod(_PRIVATE_DIR_MODE)
    return made


def random_name(prefix: str = "state") -> str:
    return f"{prefix}-{secrets.token_hex(12)}"
