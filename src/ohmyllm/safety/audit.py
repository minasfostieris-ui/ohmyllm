"""Append-only JSONL audit log with rotation and secret redaction."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from .redact import redact_secrets


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
        self.max_bytes = max_bytes
        self.keep = keep
        self._lock = threading.Lock()

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
        entry = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event or "tool_call",
            "permission_mode": mode,
            "tool": tool,
            "arguments": redact_secrets(json.dumps(args or {}, default=str, sort_keys=True)),
            "approval": approval,
            "success": success,
            "duration_ms": round(duration_ms, 2),
            "backend": backend,
            "model": model,
        }
        if error:
            entry["error"] = redact_secrets(str(error))[:400]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # auditing must never crash the agent; doctor surfaces it

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return
        for i in range(self.keep - 1, 0, -1):
            src = self.path.with_suffix(self.path.suffix + f".{i}")
            dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
            if src.exists():
                src.replace(dst)
        self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-max(n, 0):]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"tool": "?", "error": "unparseable audit line", "raw": line[:200]})
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
