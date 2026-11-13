"""Importable skill folders: OpenCode/Claude-style SKILL.md guides.

A skill is a directory containing ``SKILL.md`` (YAML frontmatter with
``name`` and ``description``, then markdown instructions). Import them with
``oml skills import <folder>``; the agent sees the index in its system
prompt and pulls full instructions with the ``skills_read`` tool.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from .secureio import UnsafePathError, has_symlink_component, is_within, private_mkdir, resolve_path

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_RESERVED = {
    ".", "..", "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_META_MAX_BYTES = 128_000
_READ_MAX_CHARS = 100_000


def _read_bounded(path: Path, max_bytes: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    return raw[:max_bytes].decode("utf-8", errors="replace"), truncated


def _safe_slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", str(name).strip().lower()).strip("-")
    if (not slug or slug.casefold() in _RESERVED or slug.startswith(".")
            or Path(slug).name != slug or "/" in slug or "\\" in slug):
        raise ValueError(f"unsafe skill destination name: {name!r}")
    return slug


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
            if d.is_symlink() or not d.is_dir():
                continue
            md = d / "SKILL.md"
            if md.is_file() and not md.is_symlink():
                out.append(self._read_meta(md))
        return out

    @staticmethod
    def _read_meta(md: Path) -> Skill:
        name = desc = ""
        try:
            text, _ = _read_bounded(md, _META_MAX_BYTES)
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
    def read(self, name: str, max_chars: int = 20000, path: str | None = None) -> str | None:
        """Read a skill's SKILL.md, or (level-2) one bundled reference file.

        ``path`` must be strictly relative inside the skill directory: no
        absolute paths, no ``.``/``..`` components, no symlinks. The same
        per-file byte cap and truncation marker apply to every file.
        """
        needle = name.strip().lower()
        limit = max(0, min(int(max_chars), _READ_MAX_CHARS))
        for s in self.list():
            if s.name.lower() == needle or s.dir.name.lower() == needle:
                target = s.path if path is None else self._resolve_reference(s, path)
                text, byte_truncated = _read_bounded(target, max(1, limit * 4))
                char_truncated = len(text) > limit
                suffix = "\n… [truncated]" if byte_truncated or char_truncated else ""
                return text[:limit] + suffix
        return None

    @staticmethod
    def _resolve_reference(skill: Skill, raw: str) -> Path:
        rel = str(raw)
        if not rel.strip() or Path(rel).is_absolute() or "\\" in rel:
            raise ValueError("skill path must be a relative file inside the skill directory")
        if any(part in ("", ".", "..") for part in rel.split("/")):
            raise ValueError("skill path must not contain empty, '.' or '..' components")
        try:
            resolved = resolve_path(rel, base=skill.dir, root=skill.dir, reject_symlinks=True)
        except UnsafePathError as exc:
            raise ValueError(f"unsafe skill path: {exc}") from exc
        if has_symlink_component(skill.dir / rel) or resolved.is_symlink():
            raise ValueError("skill references may not traverse symlinks")
        if not is_within(resolved, skill.dir.resolve(strict=False)) or resolved == skill.dir:
            raise ValueError("skill path escapes the skill directory")
        if not resolved.is_file():
            raise ValueError(f"no such skill file: {rel}")
        return resolved

    # ------------------------------------------------------------- import
    def import_path(self, src: str | Path) -> builtins.list[str]:
        """Import one skill folder (contains SKILL.md) or a parent of many.

        Re-importing a skill by the same name refreshes it (old copy replaced).
        """
        requested = Path(str(src)).expanduser()
        if has_symlink_component(requested):
            raise ValueError("skill imports may not traverse symlinks")
        src = requested.resolve(strict=False)
        if not src.is_dir():
            raise ValueError(f"{src} is not a directory")
        if self.root.is_symlink():
            raise ValueError("skills root must not be a symlink")
        private_mkdir(self.root)
        imported: list[str] = []
        skill_md = src / "SKILL.md"
        if skill_md.is_symlink():
            raise ValueError(f"skill source contains a symlink: {skill_md}")
        if skill_md.is_file():
            imported.append(self._copy_skill(src))
        else:
            for d in sorted(src.iterdir()):
                if d.is_symlink():
                    raise ValueError(f"skill source contains a symlink: {d}")
                candidate = d / "SKILL.md"
                if candidate.is_symlink():
                    raise ValueError(f"skill source contains a symlink: {candidate}")
                if d.is_dir() and candidate.is_file():
                    imported.append(self._copy_skill(d))
        if not imported:
            raise ValueError("no SKILL.md found in that folder (or its subfolders)")
        return imported

    def _copy_skill(self, src: Path) -> str:
        if src.is_symlink() or has_symlink_component(src):
            raise ValueError(f"skill source contains a symlink: {src}")
        if any(path.is_symlink() for path in src.rglob("*")):
            raise ValueError(f"skill source contains a symlink: {src}")
        meta = self._read_meta(src / "SKILL.md")
        slug = _safe_slug(meta.name)
        root = self.root.resolve(strict=False)
        dest = root / slug
        if not is_within(dest, root) or dest == root or dest.is_symlink():
            raise ValueError(f"unsafe skill destination: {slug!r}")

        stage = root / f".import-{secrets.token_hex(12)}"
        previous = root / f".replaced-{secrets.token_hex(12)}"
        shutil.copytree(src, stage, symlinks=False)
        moved_previous = False
        try:
            if dest.exists():
                os.replace(dest, previous)
                moved_previous = True
            os.replace(stage, dest)
        except BaseException:
            if moved_previous and not dest.exists() and previous.exists():
                os.replace(previous, dest)
            raise
        finally:
            with contextlib.suppress(OSError):
                if stage.exists():
                    shutil.rmtree(stage)
            with contextlib.suppress(OSError):
                if previous.exists():
                    shutil.rmtree(previous)
        return meta.name
