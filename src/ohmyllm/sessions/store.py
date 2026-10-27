"""Chat session storage: one JSON file per session, project-local or global.

Layout: ``<history>/sessions/<id>.json`` plus ``<history>/trash/`` for deletes
and ``<history>/exports/`` for exported files. All writes are atomic.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths


def make_title(text: str) -> str:
    """Derive a concise chat title from the first user message."""
    t = re.sub(r"```.*?```", " [code] ", text, flags=re.DOTALL)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(hey|hi|hello|yo|please|can you|could you|i want to|i need to)\b[:,]?\s*", "", t, flags=re.IGNORECASE)
    if len(t) > 58:
        t = t[:58].rstrip(" ,;:.") + "…"
    return t or "New chat"


SessionsList = list


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=UTC).isoformat(timespec="seconds")


@dataclass
class Session:
    id: str
    title: str
    created_at: float
    updated_at: float
    backend: str
    model: str
    permission_mode: str
    workspace: str = ""
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    exported_paths: list[str] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return sum(1 for m in self.messages if m.get("role") in ("user", "assistant"))

    @property
    def updated_iso(self) -> str:
        return iso(self.updated_at)

    @property
    def preview(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") in ("user", "assistant") and m.get("content"):
                snippet = re.sub(r"\s+", " ", str(m["content"]))[:90]
                who = "you" if m["role"] == "user" else "model"
                return f"{who}: {snippet}"
        return "(empty)"

    def add_message(self, role: str, content: str, **extra: Any) -> dict[str, Any]:
        msg = {"role": role, "content": content, "ts": time.time(), **extra}
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    def record_tool(self, name: str, args: dict, status: str, approval: str, duration_ms: float, error: str | None = None) -> dict[str, Any]:
        row = {
            "name": name,
            "args": {k: (str(v)[:140]) for k, v in (args or {}).items()},
            "status": status,
            "approval": approval,
            "duration_ms": round(duration_ms, 1),
            "ts": time.time(),
        }
        if error:
            row["error"] = error[:200]
        self.tool_calls.append(row)
        self.updated_at = time.time()
        return row

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["schema"] = 1
        d["created_iso"] = iso(self.created_at)
        d["updated_iso"] = iso(self.updated_at)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class SessionStore:
    """CRUD over the session dir; sorted newest-updated with pinned first."""

    root: Path | None = None  # history dir; default paths.history_dir()

    @property
    def dir(self) -> Path:
        return (Path(self.root) if self.root else paths.history_dir()) / "sessions"

    @property
    def trash(self) -> Path:
        return (Path(self.root) if self.root else paths.history_dir()) / "trash"

    @property
    def exports(self) -> Path:
        return (Path(self.root) if self.root else paths.history_dir()) / "exports"

    # ------------------------------------------------------------------ crud
    def create(self, *, title: str = "", first_message: str = "", backend: str = "", model: str = "", permission_mode: str = "sandboxed", workspace: str = "") -> Session:
        now = time.time()
        sess = Session(
            id=secrets.token_hex(6),
            title=title or make_title(first_message) if first_message else (title or "New chat"),
            created_at=now,
            updated_at=now,
            backend=backend,
            model=model,
            permission_mode=permission_mode,
            workspace=workspace,
        )
        if first_message:
            sess.add_message("user", first_message)
        self.save(sess)
        return sess

    def resolve(self, id_or_prefix: str) -> Session | None:
        """Exact id, or a unique ≥4-char prefix (the list view truncates ids)."""
        sess = self.get(id_or_prefix)
        if sess:
            return sess
        if len(id_or_prefix) >= 4:
            hits = [s for s in self.list() if s.id.startswith(id_or_prefix)]
            if len(hits) == 1:
                return hits[0]
        return None

    def save(self, sess: Session) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        dest = self.dir / f"{sess.id}.json"
        # unique tmp name: two concurrent saves must never share one temp file
        tmp = dest.with_name(f"{sess.id}.{secrets.token_hex(4)}.json.tmp")
        tmp.write_text(json.dumps(sess.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        # Windows: a concurrent reader (/api/chats) can hold dest open for a
        # moment and os.replace then raises PermissionError — retry briefly
        # instead of losing the turn.
        last: OSError | None = None
        for attempt in range(5):
            try:
                tmp.replace(dest)
                return dest
            except OSError as exc:
                last = exc
                time.sleep(0.04 * (attempt + 1))
        assert last is not None
        tmp.unlink(missing_ok=True)
        raise last

    def get(self, sid: str) -> Session | None:
        f = self.dir / f"{sid}.json"
        if not f.is_file():
            return None
        try:
            return Session.from_dict(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def list(self) -> SessionsList[Session]:
        out: list[Session] = []
        if not self.dir.is_dir():
            return out
        for f in self.dir.glob("*.json"):
            try:
                out.append(Session.from_dict(json.loads(f.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        out.sort(key=lambda s: (s.pinned, s.updated_at), reverse=True)
        return out

    def rename(self, sid: str, title: str) -> Session | None:
        s = self.get(sid)
        if not s:
            return None
        s.title = make_title(title) if title else s.title
        s.updated_at = time.time()
        self.save(s)
        return s

    def set_title(self, sid: str, title: str) -> Session | None:
        return self.rename(sid, title)

    def toggle_pin(self, sid: str) -> Session | None:
        s = self.get(sid)
        if not s:
            return None
        s.pinned = not s.pinned
        self.save(s)
        return s

    def add_tag(self, sid: str, tag: str) -> Session | None:
        s = self.get(sid)
        if not s:
            return None
        tag = tag.strip().lstrip("#")
        if tag and tag not in s.tags:
            s.tags.append(tag)
            self.save(s)
        return s

    def delete(self, sid: str) -> Path | None:
        """Move to trash (never hard-delete). Returns the trash path."""
        src = self.dir / f"{sid}.json"
        if not src.is_file():
            return None
        self.trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        dest = self.trash / f"{sid}-{stamp}.json"
        src.replace(dest)
        return dest

    def restore_from_trash(self, trash_path: Path) -> Session | None:
        data = json.loads(Path(trash_path).read_text(encoding="utf-8"))
        sess = Session.from_dict(data)
        self.save(sess)
        Path(trash_path).unlink(missing_ok=True)
        return sess

    # ------------------------------------------------------------- searching
    def search(self, text: str, *, backend: str | None = None, model: str | None = None, since: float | None = None, workspace: str | None = None) -> SessionsList[Session]:
        q = text.casefold().strip()
        out = []
        for s in self.list():
            if backend and s.backend != backend:
                continue
            if model and s.model != model:
                continue
            if since and s.updated_at < since:
                continue
            if workspace and s.workspace != workspace:
                continue
            if q:
                hay = s.title.casefold()
                hay += " " + " ".join(str(m.get("content", "")) for m in s.messages[-40:]).casefold()
                if q not in hay:
                    continue
            out.append(s)
        return out
