"""Filesystem tools with symlink-safe resolution, backups, and bounded output."""

from __future__ import annotations

import difflib
import fnmatch
import os
import secrets
from pathlib import Path

from .. import media, paths
from ..secureio import (
    UnsafePathError,
    atomic_write,
    file_identity,
    is_environment_file,
    is_within,
    private_mkdir,
    resolve_path,
)
from .spec import ToolCancelled, ToolContext, ToolResult

READ_CAP = 64_000
FILE_READ_MAX = 2_000_000
PREVIEW_READ_MAX = 256_000
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".ruff_cache", ".cache", "target", ".ohmyllm",
}


def resolve(ctx: ToolContext, raw: str | Path) -> Path:
    return resolve_path(raw, base=ctx.workspace, reject_symlinks=True)


def rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.workspace))
    except ValueError:
        return str(p)


def _explicitly_allowed(ctx: ToolContext, target: Path) -> bool:
    for value in ctx.config.safety.allow_sensitive_paths:
        if not value:
            continue
        try:
            boundary = resolve_path(value, base=ctx.workspace, reject_symlinks=False)
        except (OSError, ValueError):
            continue
        if is_within(target, boundary):
            return True
    return False


def _protected(ctx: ToolContext, target: Path) -> str | None:
    if is_within(target, paths.global_home()) or is_within(target, ctx.workspace / ".ohmyllm"):
        return "private OhMyLLM state is not accessible through filesystem tools"
    if is_environment_file(target) and not _explicitly_allowed(ctx, target):
        return "environment files require an explicit component-aware sensitive-path exception"
    from ..safety.policy import PolicyEngine

    if PolicyEngine(ctx.config, ctx.workspace).is_sensitive(target) and not _explicitly_allowed(ctx, target):
        return "sensitive paths require an explicit component-aware exception"
    return None


def _read_text_bounded(
    path: Path, max_bytes: int, *, errors: str = "replace"
) -> tuple[str, bool]:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    return raw[:max_bytes].decode("utf-8", errors=errors), truncated


def _approved_identity(args: dict, target: Path) -> dict:
    expected = args.get("__target_identity")
    if expected is None:
        return file_identity(target)
    if type(expected) is not dict:
        raise UnsafePathError("approved target identity is malformed")
    return expected


def _commit_check(ctx: ToolContext, raw: str | Path, target: Path, expected: dict) -> None:
    ctx.check_cancelled()
    if resolve(ctx, raw) != target or file_identity(target) != expected:
        raise UnsafePathError("target identity changed after approval")


def _target(ctx: ToolContext, raw: str | Path) -> tuple[Path | None, str | None]:
    try:
        target = resolve(ctx, raw)
    except (UnsafePathError, OSError, ValueError) as exc:
        return None, str(exc)
    return target, _protected(ctx, target)


async def read_file(ctx: ToolContext, args: dict) -> ToolResult:
    p, blocked = _target(ctx, args.get("path", ""))
    if p is None or blocked:
        return ToolResult(False, "", blocked or "unsafe path")
    if not p.is_file():
        return ToolResult(False, "", f"not a file: {rel(ctx, p)}")
    kind = media.classify(p)
    if kind != "text":
        return await _read_media(ctx, p, kind)
    offset = max(1, int(args.get("offset") or 1))
    limit = max(0, min(int(args.get("max_lines") or 400), 2000))
    try:
        text, truncated = _read_text_bounded(p, FILE_READ_MAX)
    except OSError as exc:
        return ToolResult(False, "", f"read failed: {exc}")
    lines = text.splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    if len(lines) > len(chunk) or truncated:
        total = f"at least {len(lines)}" if truncated else str(len(lines))
        head = f"{rel(ctx, p)} ({total} lines, showing {offset}-{offset + len(chunk) - 1} of them):"
    else:
        head = f"{rel(ctx, p)} ({len(lines)} lines):"
    body = "\n".join(f"{offset + i:>5}| {ln}" for i, ln in enumerate(chunk))
    out = head + "\n" + body
    if len(out) > READ_CAP:
        out = out[:READ_CAP] + "\n…[truncated]"
    return ToolResult(True, out)


async def _read_media(ctx: ToolContext, p: Path, kind: str) -> ToolResult:
    """Images/audio/video for the model. Frames & images land in
    ToolResult.meta["images"] — the runtime attaches them ONLY if the
    active model is vision-capable and converts them into a follow-up
    user message (tool messages cannot carry images on any backend)."""
    import shutil
    import tempfile

    if kind == "binary":
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        return ToolResult(False, "", f"{rel(ctx, p)} is a binary file ({size} bytes) — no text, image, audio or video decoder applies")
    try:
        size = p.stat().st_size
    except OSError as exc:
        return ToolResult(False, "", f"stat failed: {exc}")
    if size > media.MAX_MEDIA_BYTES:
        return ToolResult(False, "", f"{rel(ctx, p)} is {size / 2**30:.1f} GiB — too large to read")

    if kind == "image":
        if size > media.MAX_IMAGE_BYTES:
            return ToolResult(False, "", f"{rel(ctx, p)} is {size / 2**20:.0f} MB — over the {media.MAX_IMAGE_BYTES // 2**20} MB image cap")
        url = media.image_data_url(p)
        if not url:
            return ToolResult(False, "", f"could not encode {rel(ctx, p)} as an image")
        return ToolResult(
            True, f"image {rel(ctx, p)} ({size // 1024} KB) loaded",
            meta={"media": "image", "path": str(p), "images": [url]},
        )

    if kind == "audio":
        text, err = await media.transcribe_file(p)
        if err:
            return ToolResult(False, "", f"audio {rel(ctx, p)}: {err}")
        if not text:
            return ToolResult(True, f"audio {rel(ctx, p)}: transcription succeeded but nothing was heard")
        out = f"transcript of audio {rel(ctx, p)} (speech-to-text):\n{text}"
        if len(out) > READ_CAP:
            out = out[:READ_CAP] + "\n…[truncated]"
        return ToolResult(True, out, meta={"media": "audio", "path": str(p)})

    # video: sampled frames (+ audio-track transcript)
    workdir = Path(tempfile.mkdtemp(prefix="oml-video-"))
    try:
        frames, ferr = await media.video_frames(p)
        dur = await media.probe_duration(p)
        transcript = ""
        terr = None
        audio_wav, _ = await media.extract_audio_stream(p, workdir)
        has_audio = audio_wav is not None
        if audio_wav is not None:
            from .. import voice

            engine, _why = voice.pick_engine()
            if engine:
                text, terr = await voice.transcribe(audio_wav)
                transcript = (text or "").strip()
            else:
                terr = "no STT engine — video audio not transcribed"
        note = media.human_media_summary(p, "video", dur, len(frames), has_audio)
        if ferr:
            return ToolResult(False, "", f"video {rel(ctx, p)}: {ferr}")
        urls: list[str] = []
        for f in frames:
            try:
                if f.stat().st_size <= media.MAX_IMAGE_BYTES:
                    u = media.image_data_url(f)
            except OSError:
                u = None
            if u:
                urls.append(u)
        out = f"{note}."
        if transcript:
            out += f"\nvideo transcript (speech-to-text):\n{transcript}"
        elif terr:
            out += f"\n[audio: {terr}]"
        if not urls:
            out += "\n⚠ no frames could be attached"
        return ToolResult(True, out[:READ_CAP], meta={"media": "video", "path": str(p), "images": urls})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def list_dir(ctx: ToolContext, args: dict) -> ToolResult:
    p, blocked = _target(ctx, args.get("path") or args.get("directory") or ".")
    if p is None or blocked:
        return ToolResult(False, "", blocked or "unsafe path")
    if not p.is_dir():
        return ToolResult(False, "", f"not a directory: {rel(ctx, p)}")
    max_entries = max(0, min(int(args.get("limit") or 300), 1000))
    entries: list[str] = []
    try:
        for entry in sorted(p.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if len(entries) >= max_entries:
                entries.append("… [limit reached]")
                break
            if entry.is_symlink() or _protected(ctx, entry):
                continue
            kind = "d" if entry.is_dir() else "f"
            size = entry.stat().st_size if entry.is_file() else 0
            entries.append(f"[{kind}] {entry.name}" + (f"  ({_human(size)})" if kind == "f" else ""))
    except OSError as exc:
        return ToolResult(False, "", f"list failed: {exc}")
    return ToolResult(True, f"{rel(ctx, p)}/\n" + "\n".join(entries) if entries else f"{rel(ctx, p)}/ (empty)")


def search_files(ctx: ToolContext, args: dict) -> ToolResult:
    query = str(args.get("query") or args.get("pattern") or "")
    if not query:
        return ToolResult(False, "", "empty search query")
    root, blocked = _target(ctx, args.get("path") or args.get("directory") or ".")
    if root is None or blocked:
        return ToolResult(False, "", blocked or "unsafe path")
    if not root.is_dir():
        return ToolResult(False, "", f"not a directory: {rel(ctx, root)}")
    case = bool(args.get("case_sensitive"))
    glob_filter = str(args.get("glob") or "")
    max_hits = max(0, min(int(args.get("max_results") or 80), 500))
    import re

    matcher = None
    if args.get("regex"):
        try:
            matcher = re.compile(query, 0 if case else re.IGNORECASE)
        except re.error as exc:
            return ToolResult(False, "", f"invalid regex: {exc}")
    hay_needle = query if case else query.lower()
    hits: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        safe_dirs: list[str] = []
        for dirname in dirnames:
            candidate = Path(dirpath) / dirname
            if dirname in SKIP_DIRS or dirname.startswith(".") or candidate.is_symlink():
                continue
            if _protected(ctx, candidate):
                continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in filenames:
            fp = Path(dirpath) / filename
            if fp.is_symlink() or _protected(ctx, fp):
                continue
            if glob_filter and not fnmatch.fnmatch(filename.lower(), glob_filter.lower()):
                continue
            if scanned >= 4000 or len(hits) >= max_hits:
                break
            try:
                checked = resolve(ctx, fp)
                if checked != fp.resolve(strict=False) or fp.stat().st_size > 2_000_000:
                    continue
                text, truncated = _read_text_bounded(fp, FILE_READ_MAX, errors="strict")
                if truncated:
                    continue
            except (OSError, UnicodeDecodeError, UnsafePathError):
                continue
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                ok = matcher.search(line) is not None if matcher else hay_needle in (line if case else line.lower())
                if ok:
                    hits.append(f"{rel(ctx, fp)}:{i}: {line.strip()[:180]}")
                    if len(hits) >= max_hits:
                        break
        if len(hits) >= max_hits or scanned >= 4000:
            break
    if not hits:
        return ToolResult(True, f"no matches for {query!r} (scanned {scanned} files)")
    return ToolResult(True, "\n".join(hits) + f"\n— {len(hits)} match(es)")


def write_file(ctx: ToolContext, args: dict) -> ToolResult:
    raw = args.get("path") or args.get("file") or ""
    if not str(raw):
        return ToolResult(False, "", "missing path")
    p, blocked = _target(ctx, raw)
    if p is None or blocked:
        return ToolResult(False, "", blocked or "unsafe path")
    content = args.get("content")
    if content is None:
        return ToolResult(False, "", "missing content")
    content = str(content)
    append = bool(args.get("append"))
    existed = p.is_file()
    try:
        expected = _approved_identity(args, p)
        ctx.check_cancelled()
        if file_identity(p) != expected:
            raise UnsafePathError("target identity changed after approval")
        old, truncated = _read_text_bounded(p, FILE_READ_MAX) if existed else ("", False)
        if truncated:
            return ToolResult(False, "", "existing file is too large to modify safely")
        final = old + content if append and existed else content
        mode = (p.stat().st_mode & 0o777) if existed else 0o600
        backup = backup_file(p) if existed else None
        atomic_write(
            p, final, mode=mode,
            commit_check=lambda: _commit_check(ctx, raw, p, expected),
        )
    except (OSError, UnsafePathError, ToolCancelled, ValueError) as exc:
        return ToolResult(False, "", f"write aborted safely: {exc}")
    diff = make_diff(old, final, rel(ctx, p))
    note = f" [backup: {backup}]" if backup else ""
    meta = {"path": rel(ctx, p), "created": not existed, **line_stats(old, final)}
    verb = "appended to" if append and existed else "wrote"
    return ToolResult(True, f"{verb} {rel(ctx, p)} ({len(content)} chars, ~{len(content.split())} words){note}\n{diff}", meta=meta)


def patch_file(ctx: ToolContext, args: dict) -> ToolResult:
    raw = args.get("path") or ""
    p, blocked = _target(ctx, raw)
    if p is None or blocked:
        return ToolResult(False, "", blocked or "unsafe path")
    old_text = args.get("old_text")
    new_text = args.get("new_text")
    if not p.is_file():
        return ToolResult(False, "", f"not a file: {rel(ctx, p)}")
    if old_text is None or new_text is None:
        return ToolResult(False, "", "patch_file needs old_text and new_text")
    old_text, new_text = str(old_text), str(new_text)
    replace_all = bool(args.get("replace_all"))
    try:
        expected = _approved_identity(args, p)
        ctx.check_cancelled()
        if file_identity(p) != expected:
            raise UnsafePathError("target identity changed after approval")
        current, truncated = _read_text_bounded(p, FILE_READ_MAX)
        if truncated:
            return ToolResult(False, "", "file is too large to patch safely")
    except (OSError, UnsafePathError, ToolCancelled, ValueError) as exc:
        return ToolResult(False, "", f"read failed safely: {exc}")
    count = current.count(old_text)
    if count == 0:
        return ToolResult(False, "", "old_text not found — read the file first and match exact text")
    if count > 1 and not replace_all:
        return ToolResult(False, "", f"old_text matches {count} times; give more context or set replace_all")
    updated = current.replace(old_text, new_text) if replace_all else current.replace(old_text, new_text, 1)
    try:
        backup = backup_file(p)
        atomic_write(
            p, updated, mode=p.stat().st_mode & 0o777,
            commit_check=lambda: _commit_check(ctx, raw, p, expected),
        )
    except (OSError, UnsafePathError, ToolCancelled, ValueError) as exc:
        return ToolResult(False, "", f"patch aborted safely: {exc}")
    diff = make_diff(current, updated, rel(ctx, p))
    meta = {"path": rel(ctx, p), "created": False, **line_stats(current, updated)}
    return ToolResult(True, f"patched {rel(ctx, p)} ({count if replace_all else 1} replacement(s), backup: {backup})\n{diff}", meta=meta)


def backup_file(p: Path) -> Path | None:
    """Create a unique private backup; callers abort if this raises."""
    if not p.is_file():
        return None
    dest_dir = paths.file_backups_dir() / secrets.token_hex(12)
    private_mkdir(dest_dir)
    dest = dest_dir / p.name
    atomic_write(dest, p.read_bytes(), mode=0o600)
    return dest


def line_stats(old: str, new: str) -> dict:
    a, b = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    added = removed = changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            changed += n
            added += (j2 - j1) - n
            removed += (i2 - i1) - n
    return {"added": added, "removed": removed, "changed": changed}


def make_diff(old: str, new: str, name: str) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=2,
    ))
    out = "".join(lines)
    if len(out) > 4000:
        out = out[:4000] + "\n… [diff truncated]"
    return out or "(no textual change)"


def preview_write(args: dict, ctx: ToolContext | None = None) -> str:
    path = args.get("path") or args.get("file") or "?"
    if ctx is None:
        return f"write → {path}"
    p, blocked = _target(ctx, path)
    if p is None or blocked:
        return f"write blocked → {path}: {blocked or 'unsafe path'}"
    content = str(args.get("content") or "")
    try:
        old, truncated = _read_text_bounded(p, PREVIEW_READ_MAX) if p.is_file() else ("", False)
    except OSError as exc:
        return f"write preview unavailable → {p}: {exc}"
    if truncated:
        return f"write preview unavailable → {p}: existing file exceeds preview limit"
    new = old + content if args.get("append") else content
    return f"write → {p}\n{make_diff(old, new, str(p))}"


def preview_patch(args: dict, ctx: ToolContext | None = None) -> str:
    path = args.get("path") or "?"
    if ctx is None:
        return f"patch → {path}"
    p, blocked = _target(ctx, path)
    if p is None or blocked:
        return f"patch blocked → {path}: {blocked or 'unsafe path'}"
    old_text, new_text = str(args.get("old_text") or ""), str(args.get("new_text") or "")
    if not p.is_file():
        return f"patch → {p} (file missing!)"
    try:
        current, truncated = _read_text_bounded(p, PREVIEW_READ_MAX)
    except OSError as exc:
        return f"patch preview unavailable → {p}: {exc}"
    if truncated:
        return f"patch preview unavailable → {p}: file exceeds preview limit"
    updated = current.replace(old_text, new_text, -1 if args.get("replace_all") else 1)
    return f"patch → {p}\n{make_diff(current, updated, p.name)}"


def _human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n}B"
