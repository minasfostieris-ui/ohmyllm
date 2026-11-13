"""Chat session storage: one JSON file per session, project-local or global.

Layout: ``<history>/sessions/<id>.json`` plus ``<history>/trash/`` for deletes
and ``<history>/exports/`` for exported files. All writes are atomic.
"""

from __future__ import annotations

import builtins
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, overload

from .. import paths
from ..safety.redact import redact_secrets, redact_value
from ..secureio import atomic_write, has_symlink_component, is_within, private_mkdir

_SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_TRASH_NAME_RE = re.compile(r"^([a-f0-9]{12})-\d{8}T\d{6}\.json$")


def _valid_session_id(value: str) -> bool:
    return _SESSION_ID_RE.fullmatch(str(value)) is not None


def _require_session_id(value: str) -> str:
    sid = str(value)
    if not _valid_session_id(sid):
        raise ValueError("invalid session id")
    return sid


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
        # Persist bounded metadata, never raw credential-bearing tool payloads.
        safe_args = redact_value(args or {})
        row = {
            "name": str(name)[:80],
            "args": {str(k)[:80]: str(v)[:140] for k, v in safe_args.items()},
            "status": str(status)[:40],
            "approval": str(approval)[:40],
            "duration_ms": round(duration_ms, 1),
            "ts": time.time(),
        }
        if error:
            row["error"] = redact_secrets(str(error))[:200]
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
        if not isinstance(d, dict):
            raise TypeError("session record must be an object")
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in d.items() if k in known}
        values["id"] = _require_session_id(str(values.get("id", "")))
        return cls(**values)


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
        sid = _require_session_id(sess.id)
        private_mkdir(self.dir)
        dest = self.dir / f"{sid}.json"
        if dest.is_symlink():
            raise ValueError("session destination must not be a symlink")
        payload = json.dumps(redact_value(sess.to_dict()), ensure_ascii=False, indent=1)
        written: Path | None = None
        for attempt in range(5):
            try:
                written = atomic_write(dest, payload + "\n", mode=0o600)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.04 * (attempt + 1))
        if written is None:
            raise AssertionError("unreachable")
        self._index_session(sess)
        return written

    def get(self, sid: str) -> Session | None:
        if not _valid_session_id(sid):
            return None
        f = self.dir / f"{sid}.json"
        if not f.is_file() or f.is_symlink():
            return None
        try:
            sess = Session.from_dict(json.loads(f.read_text(encoding="utf-8")))
            return sess if sess.id == sid and f.stem == sess.id else None
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None

    def list(self) -> SessionsList[Session]:
        out: list[Session] = []
        if not self.dir.is_dir():
            return out
        for f in self.dir.glob("*.json"):
            if f.is_symlink() or not _valid_session_id(f.stem):
                continue
            try:
                sess = Session.from_dict(json.loads(f.read_text(encoding="utf-8")))
                if sess.id == f.stem:
                    out.append(sess)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
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
        if not _valid_session_id(sid):
            return None
        src = self.dir / f"{sid}.json"
        if not src.is_file() or src.is_symlink():
            return None
        private_mkdir(self.trash)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        dest = self.trash / f"{sid}-{stamp}.json"
        src.replace(dest)
        if os.name == "posix":
            dest.chmod(0o600)
        self._unindex_session(sid)
        return dest

    def restore_from_trash(self, trash_path: Path) -> Session | None:
        source = Path(trash_path)
        try:
            source = source.resolve(strict=True)
            trash_root = self.trash.resolve(strict=False)
        except OSError:
            return None
        match = _TRASH_NAME_RE.fullmatch(source.name)
        if not match or has_symlink_component(Path(trash_path)) or not is_within(source, trash_root):
            return None
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            sess = Session.from_dict(data)
        except (ValueError, KeyError, TypeError, OSError):
            # A corrupt or unreadable trash record is reported as "not
            # restorable" (None), never raised into the caller.
            return None
        if sess.id != match.group(1):
            return None
        self.save(sess)
        source.unlink(missing_ok=True)
        return sess

    # ------------------------------------------------------------- searching
    @overload
    def search(self, text: str, limit: int, *, backend: str | None = ..., model: str | None = ...,
               since: float | None = ..., workspace: str | None = ...) -> builtins.list[dict[str, Any]]: ...

    @overload
    def search(self, text: str, limit: None = None, *, backend: str | None = None, model: str | None = None,
               since: float | None = None, workspace: str | None = None) -> SessionsList[Session]: ...

    def search(self, text: str, limit: int | None = None, *, backend: str | None = None, model: str | None = None,
               since: float | None = None, workspace: str | None = None) -> SessionsList[Session] | builtins.list[dict[str, Any]]:
        """Two forms: ``limit=None`` keeps the legacy filter scan returning
        Session objects; an integer ``limit`` runs the ranked FTS5 index and
        returns ``{session_id,title,updated_iso,snippet,rank}`` hit dicts."""
        if limit is not None:
            return self._fts_search(text, limit)
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

    # ----------------------------------------------------- FTS5 session index
    @property
    def index_path(self) -> Path:
        return (Path(self.root) if self.root else paths.history_dir()) / "search-index.sqlite3"

    def _connect_index(self) -> sqlite3.Connection:
        path = self.index_path
        if path.is_symlink():
            raise ValueError("search index must not be a symlink")
        private_mkdir(path.parent)
        conn = sqlite3.connect(str(path))
        try:
            if os.name == "posix":
                os.chmod(path, 0o600)
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5("
                "session_id UNINDEXED, title, body, updated UNINDEXED, tokenize='unicode61')"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)")
            if conn.execute("SELECT value FROM index_meta WHERE key='built'").fetchone() is None:
                self._rebuild_index(conn)
                conn.execute("INSERT INTO index_meta(key, value) VALUES ('built', '1')")
            conn.commit()
        except BaseException:
            conn.close()
            raise
        return conn

    def _rebuild_index(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM session_fts")
        for sess in self.list():
            self._write_index_row(conn, sess)

    @staticmethod
    def _write_index_row(conn: sqlite3.Connection, sess: Session) -> None:
        parts = [str(m.get("content", "")) for m in sess.messages]
        for row in sess.tool_calls:
            parts.append(str(row.get("name", "")))
            parts.append(json.dumps(redact_value(row.get("args", {})), ensure_ascii=False))
            if row.get("error"):
                parts.append(str(row["error"]))
        # Redact at index time: even legacy records never leak into search.
        body = redact_secrets("\n".join(parts))
        conn.execute("DELETE FROM session_fts WHERE session_id = ?", (sess.id,))
        conn.execute(
            "INSERT INTO session_fts(session_id, title, body, updated) VALUES (?, ?, ?, ?)",
            (sess.id, redact_secrets(sess.title), body, sess.updated_iso),
        )

    def _index_session(self, sess: Session) -> None:
        try:
            conn = self._connect_index()
        except (sqlite3.Error, OSError, ValueError):
            return
        try:
            self._write_index_row(conn, sess)
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _unindex_session(self, sid: str) -> None:
        try:
            conn = self._connect_index()
        except (sqlite3.Error, OSError, ValueError):
            return
        try:
            conn.execute("DELETE FROM session_fts WHERE session_id = ?", (sid,))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _fts_search(self, query: str, limit: int) -> builtins.list[dict[str, Any]]:
        q = str(query).strip()
        cap = max(1, min(int(limit), 50))
        if not q:
            return []
        # Quote every token so user input can never inject FTS5 syntax.
        match = " ".join('"' + tok.replace('"', '""') + '"' for tok in q.split()[:16])
        conn = self._connect_index()
        try:
            rows = conn.execute(
                "SELECT session_id, title, updated, snippet(session_fts, 2, '[', ']', ' … ', 12), rank "
                "FROM session_fts WHERE session_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, cap),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"session_id": r[0], "title": r[1], "updated_iso": r[2], "snippet": r[3], "rank": float(r[4])}
            for r in rows
        ]
