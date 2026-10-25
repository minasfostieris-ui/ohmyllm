"""soul.md — persistent agent memory with sections, dedupe, backups, changelog."""

from __future__ import annotations

import contextlib
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..safety.redact import contains_secrets

SOUL_TEMPLATE = """# Soul

This file contains persistent memory for the OhMyLLM agent.
It is loaded as part of the agent's context.
Keep entries concise, factual, and useful.

## User

## Projects

## Preferences

## Pets

## Lessons

## Important Notes
"""

DEFAULT_SECTIONS = ["User", "Projects", "Preferences", "Pets", "Lessons", "Important Notes"]
_DATE_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2}\]\s*")


class SoulError(Exception):
    pass


class SoulSecretError(SoulError):
    pass


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", _DATE_PREFIX.sub("", text).lower())
    stop = {"the", "a", "an", "is", "are", "was", "were", "of", "and", "or", "to", "in", "on", "for", "with", "my", "their", "his", "her"}
    return {w for w in words if w not in stop and len(w) > 1}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb)
    containment = inter / min(len(ta), len(tb))
    return max(jaccard, 0.5 * containment)


@dataclass
class Entry:
    section: str
    index: int  # 1-based position among entries of that section
    line_no: int  # 1-based line number in the file
    text: str

    @property
    def body(self) -> str:
        return _DATE_PREFIX.sub("", self.text.lstrip("- ").strip())


class SoulManager:
    """File-backed store for the structured soul.md memory."""

    def __init__(
        self,
        path: Path,
        backups_dir: Path,
        *,
        keep_backups: int = 50,
        changelog: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.backups_dir = Path(backups_dir)
        self.keep_backups = keep_backups
        self.changelog = changelog or (self.path.parent / "soul.changelog.jsonl")

    # ------------------------------------------------------------- lifecycle
    def ensure(self) -> Path:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write(SOUL_TEMPLATE)
        return self.path

    def read(self) -> str:
        if not self.path.is_file():
            return SOUL_TEMPLATE
        return self.path.read_text(encoding="utf-8")

    def exists(self) -> bool:
        return self.path.is_file()

    def backup(self) -> Path | None:
        if not self.path.is_file():
            return None
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%f")
        dest = self.backups_dir / f"soul-{stamp}.md"
        shutil.copy2(self.path, dest)
        self._prune_backups()
        return dest

    def backups(self) -> list[Path]:
        if not self.backups_dir.is_dir():
            return []
        return sorted(self.backups_dir.glob("soul-*.md"), key=lambda p: p.name, reverse=True)

    def revert(self, backup: Path | str | None = None) -> Path:
        """Restore the newest (or given) backup. Returns restored path."""
        avail = self.backups()
        if backup is not None:
            src = Path(backup)
            if not src.is_file():
                raise SoulError(f"backup not found: {src}")
        else:
            if not avail:
                raise SoulError("no backups available to revert to")
            src = avail[0]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write(src.read_text(encoding="utf-8"))
        self._log("revert", "", f"restored from {src.name}")
        return self.path

    def clear(self) -> Path:
        self.backup()
        self._write(SOUL_TEMPLATE)
        self._log("clear", "", "soul.md cleared to template")
        return self.path

    # ------------------------------------------------------------ structure
    def entries(self, section: str | None = None) -> list[Entry]:
        out: list[Entry] = []
        cur_section: str | None = None
        idx = 0
        for line_no, line in enumerate(self.read().splitlines(), start=1):
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                cur_section = m.group(1)
                idx = 0
                continue
            if re.match(r"^#\s+", line):
                cur_section = None
                idx = 0
                continue
            if line.lstrip().startswith("- ") and cur_section:
                idx += 1
                if section is None or cur_section.lower() == section.lower():
                    out.append(Entry(cur_section, idx, line_no, line.strip()))
        return out

    def _section_bounds(self, section: str) -> tuple[int, int]:
        """Return (heading_line_index, end_index_exclusive) for a section."""
        lines = self.read().splitlines()
        start = -1
        for i, line in enumerate(lines):
            if re.match(rf"^##\s+{re.escape(section)}\s*$", line, re.IGNORECASE):
                start = i
                break
        if start < 0:
            return -1, -1
        for j in range(start + 1, len(lines)):
            if re.match(r"^#{1,2}\s+", lines[j]):
                return start, j
        return start, len(lines)

    def resolve_section(self, wanted: str) -> str:
        wl = wanted.lower().replace("_", " ").strip()
        for known in self._all_sections():
            kl = known.lower()
            if kl == wl or wl in kl or kl in wl:
                return known
        return wanted.strip().title()

    def _all_sections(self) -> list[str]:
        text = self.read()
        found = re.findall(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
        return found or list(DEFAULT_SECTIONS)

    # ---------------------------------------------------------------- edits
    def append(
        self,
        section: str,
        text: str,
        *,
        force: bool = False,
        dated: bool = True,
    ) -> dict:
        clean = self._sanitize(text)
        if not clean:
            raise SoulError("empty memory text")
        if contains_secrets(clean) and not force:
            raise SoulSecretError(
                "This looks like a secret/credential. OhMyLLM will not store secrets "
                "in soul.md unless the user explicitly asks (pass force=true)."
            )
        sec = self.resolve_section(section)
        self.ensure()
        existing = self.entries(sec)
        for e in existing:
            if e.body.strip().lower() == clean.lower():
                return {"action": "duplicate", "section": sec, "entry": e.text}
        prefix = f"[{datetime.now(UTC).strftime('%Y-%m-%d')}] " if dated else ""
        line = f"- {prefix}{clean}"
        self.backup()
        lines = self.read().splitlines()
        start, end = self._section_bounds(sec)
        if start < 0:
            lines.append("")
            lines.append(f"## {sec}")
            lines.append(line)
            insert_at = len(lines) - 1
        else:
            insert_at = end
            while insert_at - 1 > start and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, line)
        self._write("\n".join(lines) + "\n")
        self._log("append", sec, clean)
        return {"action": "appended", "section": sec, "entry": line, "line": insert_at + 1}

    def upsert(self, section: str, text: str, *, force: bool = False, threshold: float = 0.6) -> dict:
        clean = self._sanitize(text)
        if contains_secrets(clean) and not force:
            raise SoulSecretError("refusing to store a secret in soul.md (force=true only with user consent)")
        sec = self.resolve_section(section)
        best: tuple[float, Entry] | None = None
        for e in self.entries(sec):
            s = _similarity(e.body, clean)
            if best is None or s > best[0]:
                best = (s, e)
        if best and best[0] >= threshold:
            e = best[1]
            self.backup()
            lines = self.read().splitlines()
            stamp = datetime.now(UTC).strftime("%Y-%m-%d")
            lines[e.line_no - 1] = f"- [{stamp}] {clean}"
            self._write("\n".join(lines) + "\n")
            self._log("upsert", sec, f"{e.body} -> {clean}")
            return {"action": "updated", "section": sec, "previous": e.text, "entry": lines[e.line_no - 1], "score": round(best[0], 2)}
        return self.append(section, text, force=force)

    def search(self, query: str, section: str | None = None) -> list[Entry]:
        q = query.lower().strip()
        if not q:
            return []
        results: list[Entry] = []
        for e in self.entries(section):
            el = e.text.lower()
            if q in el or _similarity(e.body, query) >= 0.5 or any(t in el for t in _tokens(query)):
                results.append(e)
        results.sort(key=lambda e: -_similarity(e.body, query) - (5 if q in e.text.lower() else 0))
        return results

    def forget(self, query: str = "", *, section: str | None = None, index: int | None = None) -> dict:
        """Delete a memory by exact entry index (section+index) or best match of query."""
        target: Entry | None = None
        if section and index is not None:
            for e in self.entries(section):
                if e.index == index:
                    target = e
                    break
        else:
            matches = self.search(query, section)
            if len(matches) == 1:
                target = matches[0]
            elif matches:
                for e in matches:
                    if e.body.strip().lower() == query.strip().lower():
                        target = e
                        break
                target = target or matches[0]
        if target is None:
            return {"action": "not_found", "query": query}
        self.backup()
        lines = self.read().splitlines()
        removed = lines[target.line_no - 1]
        del lines[target.line_no - 1]
        self._write("\n".join(lines) + "\n")
        self._log("forget", target.section, target.body)
        return {"action": "forgotten", "section": target.section, "removed": removed}

    def recent(self, n: int = 10) -> list[dict]:
        if not self.changelog.is_file():
            return []
        import json

        out = []
        for line in self.changelog.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))

    def for_injection(self, max_chars: int = 6000) -> str:
        text = self.read().strip()
        if not text or text == SOUL_TEMPLATE.strip():
            return ""
        if len(text) > max_chars:
            keep = text[: max_chars - 40].rstrip()
            return keep + "\n… [truncated: soul.md is longer; use soul_read for all]"
        return text

    # -------------------------------------------------------------- helpers
    def _sanitize(self, text: str) -> str:
        t = re.sub(r"\s+", " ", str(text)).strip().strip("-").strip()
        if len(t) > 400:
            t = t[:400].rstrip() + "…"
        return t

    def _write(self, content: str) -> None:
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(self.path)

    def _log(self, action: str, section: str, text: str) -> None:
        import json

        try:
            self.changelog.parent.mkdir(parents=True, exist_ok=True)
            with open(self.changelog, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                            "action": action,
                            "section": section,
                            "text": text[:300],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _prune_backups(self) -> None:
        avail = self.backups()
        for old in avail[self.keep_backups :]:
            with contextlib.suppress(OSError):
                old.unlink()
