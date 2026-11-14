"""Append-only JSONL audit log with rotation and secret redaction."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..secureio import UnsafePathError, append_private, canonical_json, private_mkdir
from .redact import redact_secrets, redact_value

_MAX_RECORD_BYTES = 32_768
_MAX_READ_BYTES = 1_000_000
_MAX_RECENT = 1000


class AuditLogger:
    def __init__(
        self,
        path: Path | None = None,
        *,
        enabled: bool = True,
        max_bytes: int = 2_000_000,
        keep: int = 3,
    ) -> None:
        self.path = Path(path) if path else paths.audit_log_path()
        self.enabled = enabled
        self.max_bytes = max(4096, int(max_bytes))
        self.keep = max(1, min(int(keep), 20))
        self._lock = threading.Lock()
        self.last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.last_error is None

    def health(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "healthy": self.healthy, "error": self.last_error}

    @contextmanager
    def _process_lock(self):
        """Serialize rotation across processes where advisory locking exists."""
        private_mkdir(self.path.parent)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError:
            # A planted symlink must never be followed (O_NOFOLLOW) nor brick
            # auditing: remove the link itself and recreate a private lock once.
            if not lock_path.is_symlink():
                raise
            lock_path.unlink()
            fd = os.open(lock_path, flags, 0o600)
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                os.fchmod(fd, 0o600)
            yield
        finally:
            if os.name == "posix":
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def log(
        self,
        *,
        mode: str,
        tool: str,
        args: dict[str, Any] | None,
        approval: str,
        success: bool,
        duration_ms: float,
        backend: str | None = None,
        model: str | None = None,
        error: str | None = None,
        event: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        safe_args = redact_value(args or {})
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": str(event or "tool_call")[:80],
            "permission_mode": str(mode)[:40],
            "tool": str(tool)[:100],
            "arguments": safe_args,
            "approval": str(approval)[:40],
            "success": bool(success),
            "duration_ms": round(float(duration_ms), 2),
            "backend": str(backend)[:80] if backend else None,
            "model": str(model)[:160] if model else None,
        }
        if error:
            entry["error"] = redact_secrets(str(error))[:1000]
        record = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        if len(record.encode("utf-8")) > _MAX_RECORD_BYTES:
            raw_args = canonical_json(safe_args).encode("utf-8")
            entry["arguments"] = {
                "omitted": True,
                "bytes": len(raw_args),
                "sha256": hashlib.sha256(raw_args).hexdigest(),
            }
            record = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        try:
            with self._lock, self._process_lock():
                self._rotate_if_needed(len(record.encode("utf-8")))
                append_private(self.path, record, max_bytes=_MAX_RECORD_BYTES)
            self.last_error = None
        except (OSError, UnsafePathError) as exc:
            # Agent execution continues, but health/status surfaces the loss of
            # auditability. R3 S5: a symlinked/hostile state dir raises
            # UnsafePathError (a ValueError) from private_mkdir — it must
            # degrade to last_error exactly like OSError, never escape log().
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]

    def _rotate_if_needed(self, incoming: int = 0) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self.max_bytes:
            return
        for i in range(self.keep - 1, 0, -1):
            src = self.path.with_suffix(self.path.suffix + f".{i}")
            dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
            if src.exists() and not src.is_symlink():
                src.replace(dst)
                if os.name == "posix":
                    dst.chmod(0o600)
        if not self.path.is_symlink():
            rotated = self.path.with_suffix(self.path.suffix + ".1")
            self.path.replace(rotated)
            if os.name == "posix":
                rotated.chmod(0o600)

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        count = max(0, min(int(n), _MAX_RECENT))
        if count == 0 or not self.path.is_file() or self.path.is_symlink():
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - _MAX_READ_BYTES)
                fh.seek(start)
                raw = fh.read(_MAX_READ_BYTES)
            lines = raw.decode("utf-8", errors="replace").splitlines()
            if start and lines:
                lines = lines[1:]
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-count:]:
            try:
                value = json.loads(line)
                out.append(value if isinstance(value, dict) else {"tool": "?", "error": "invalid audit record"})
            except json.JSONDecodeError:
                out.append({"tool": "?", "error": "unparseable audit line", "raw": redact_secrets(line[:200])})
        return out


def format_event(entry: dict[str, Any]) -> str:
    ts = str(entry.get("timestamp", ""))[:19].replace("T", " ")
    ok = "OK " if entry.get("success") else "ERR"
    dur = entry.get("duration_ms", 0)
    return (
        f"{ts}  {ok}  {entry.get('permission_mode', '?'):<11} {entry.get('tool', '?'):<16} "
        f"{dur:>7.1f}ms [{entry.get('approval', '-')}] "
        f"{str(entry.get('arguments', ''))[:80]}"
    )
