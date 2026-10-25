"""Session export to markdown / JSON + session suggestion helper."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .store import Session, SessionStore


def to_markdown(sess: Session) -> str:
    created = datetime.fromtimestamp(sess.created_at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    updated = datetime.fromtimestamp(sess.updated_at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# {sess.title}",
        "",
        f"- Session ID: `{sess.id}`",
        f"- Backend: {sess.backend or '—'}",
        f"- Model: {sess.model or '—'}",
        f"- Permission mode: {sess.permission_mode}",
        f"- Workspace: {sess.workspace or '—'}",
        f"- Created: {created}",
        f"- Updated: {updated}",
        f"- Messages: {sess.message_count} · Tool calls: {len(sess.tool_calls)}",
    ]
    if sess.tags:
        lines.append(f"- Tags: {', '.join(sess.tags)}")
    lines.append("\n---\n")
    for m in sess.messages:
        ts = datetime.fromtimestamp(m.get("ts", sess.created_at), tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        role = m.get("role", "note")
        head = {"user": "### You", "assistant": "### OhMyLLM"}.get(role, f"### {role.title()}")
        lines.append(f"{head} · {ts}\n")
        content = str(m.get("content", "")).strip()
        if content:
            lines.append(content)
        for t in m.get("tools", []) or []:
            lines.append(
                f"> 🔧 `{t.get('name')}` ({t.get('approval', '?')}, {t.get('status', '?')}) — "
                f"```{json.dumps(t.get('args', {}), ensure_ascii=False, default=str)[:160]}```"
            )
        lines.append("")
    if sess.tool_calls:
        lines.append("## Tool activity\n")
        lines.append("| Tool | Args | Approval | Status | ms |")
        lines.append("|---|---|---|---|---|")
        for t in sess.tool_calls:
            args = re.sub(r"[|\n]", " ", json.dumps(t.get("args", {}), ensure_ascii=False, default=str))[:90]
            lines.append(f"| {t.get('name')} | {args} | {t.get('approval')} | {t.get('status')} | {t.get('duration_ms')} |")
    return "\n".join(lines) + "\n"


def to_json(sess: Session) -> str:
    return json.dumps(sess.to_dict(), ensure_ascii=False, indent=2)


def export(store: SessionStore, sess: Session, fmt: str = "markdown", dest: Path | None = None) -> Path:
    """Write an export file; records it on the session. Returns the path."""
    fmt = fmt.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", sess.title.lower()).strip("-")[:48] or sess.id
    if fmt in ("md", "markdown"):
        content, suffix = to_markdown(sess), ".md"
    elif fmt == "json":
        content, suffix = to_json(sess), ".json"
    else:
        raise ValueError(f"unknown export format: {fmt}")
    if dest is None:
        store.exports.mkdir(parents=True, exist_ok=True)
        dest = store.exports / f"{sess.id}-{slug}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    if str(dest) not in sess.exported_paths:
        sess.exported_paths.append(str(dest))
        store.save(sess)
    return dest
