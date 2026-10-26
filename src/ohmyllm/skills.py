"""Importable skill folders: OpenCode/Claude-style SKILL.md guides.

A skill is a directory containing ``SKILL.md`` (YAML frontmatter with
``name`` and ``description``, then markdown instructions). Import them with
``oml skills import <folder>``; the agent sees the index in its system
prompt and pulls full instructions with the ``skills_read`` tool.
"""

from __future__ import annotations

import builtins
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path

    @property
    def dir(self) -> Path:
        return self.path.parent


class SkillManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    # ------------------------------------------------------------- listing
    def list(self) -> builtins.list[Skill]:
        out: list[Skill] = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            md = d / "SKILL.md"
            if md.is_file():
                out.append(self._read_meta(md))
        return out

    @staticmethod
    def _read_meta(md: Path) -> Skill:
        name = desc = ""
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        body = text
        m = _FRONT_RE.match(text)
        if m:
            body = text[m.end():]
            for line in m.group(1).splitlines():
                k, _, v = line.partition(":")
                k = k.strip().lower()
                v = v.strip().strip("\"'")
                if k == "name" and v:
                    name = v
                elif k == "description" and v:
                    desc = v
        if not name:
            name = md.parent.name
        if not desc:
            for line in body.splitlines():
                s = line.strip().lstrip("#").strip()
                if len(s) > 20:
                    desc = s[:200]
                    break
        return Skill(name=name, description=desc, path=md)

    # --------------------------------------------------------------- read
    def read(self, name: str, max_chars: int = 20000) -> str | None:
        needle = name.strip().lower()
        for s in self.list():
            if s.name.lower() == needle or s.dir.name.lower() == needle:
                text = s.path.read_text(encoding="utf-8", errors="replace")
                return text[:max_chars] + ("\n… [truncated]" if len(text) > max_chars else "")
        return None

    # ------------------------------------------------------------- import
    def import_path(self, src: str | Path) -> builtins.list[str]:
        """Import one skill folder (contains SKILL.md) or a parent of many.

        Re-importing a skill by the same name refreshes it (old copy replaced).
        """
        src = Path(str(src)).expanduser().resolve(strict=False)
        if not src.is_dir():
            raise ValueError(f"{src} is not a directory")
        self.root.mkdir(parents=True, exist_ok=True)
        imported: list[str] = []
        if (src / "SKILL.md").is_file():
            imported.append(self._copy_skill(src))
        else:
            for d in sorted(src.iterdir()):
                if d.is_dir() and (d / "SKILL.md").is_file():
                    imported.append(self._copy_skill(d))
        if not imported:
            raise ValueError("no SKILL.md found in that folder (or its subfolders)")
        return imported

    def _copy_skill(self, src: Path) -> str:
        meta = self._read_meta(src / "SKILL.md")
        slug = _SLUG_RE.sub("-", meta.name.lower()).strip("-") or "skill"
        if slug in {".", ".."} or slug.startswith("."):
            raise ValueError(f"invalid skill name {meta.name!r}")
        dest = self.root / slug
        if dest.resolve().parent != self.root.resolve():
            raise ValueError(f"skill name escapes the skills folder: {meta.name!r}")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return meta.name
