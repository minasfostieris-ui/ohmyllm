"""File-system tools, workspace-scoped, backup-on-write, diff previews."""

from __future__ import annotations

import datetime as _dt
import difflib
import fnmatch
import os
from pathlib import Path

from .. import media, paths
from .spec import ToolContext, ToolResult

READ_CAP = 64_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".ruff_cache", ".cache", "target"}


def resolve(ctx: ToolContext, raw: str | Path) -> Path:
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = ctx.workspace / p
    return p.resolve(strict=False)


def rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.workspace))
    except ValueError:
        return str(p)


async def read_file(ctx: ToolContext, args: dict) -> ToolResult:
    p = resolve(ctx, args.get("path", ""))
    if not p.is_file():
        return ToolResult(False, "", f"not a file: {rel(ctx, p)}")
    kind = media.classify(p)
    if kind != "text":
        return await _read_media(ctx, p, kind)
    offset = int(args.get("offset") or 1)
    limit = int(args.get("max_lines") or 400)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(False, "", f"read failed: {exc}")
    lines = text.splitlines()
    chunk = lines[offset - 1 : offset - 1 + limit]
    head = f"{rel(ctx, p)} ({len(lines)} lines total, showing {offset}-{offset + len(chunk) - 1} of them):" if len(lines) > len(chunk) else f"{rel(ctx, p)} ({len(lines)} lines):"
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
    p = resolve(ctx, args.get("path") or args.get("directory") or ".")
    if not p.is_dir():
        return ToolResult(False, "", f"not a directory: {rel(ctx, p)}")
    max_entries = int(args.get("limit") or 300)
    entries: list[str] = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if len(entries) >= max_entries:
                entries.append("… [limit reached]")
                break
            kind = "d" if e.is_dir() else "f"
            size = e.stat().st_size if e.is_file() else 0
            entries.append(f"[{kind}] {e.name}" + (f"  ({_human(size)})" if kind == "f" else ""))
    except OSError as exc:
        return ToolResult(False, "", f"list failed: {exc}")
    return ToolResult(True, f"{rel(ctx, p)}/\n" + "\n".join(entries) if entries else f"{rel(ctx, p)}/ (empty)")


def search_files(ctx: ToolContext, args: dict) -> ToolResult:
    query = str(args.get("query") or args.get("pattern") or "")
    if not query:
        return ToolResult(False, "", "empty search query")
    root = resolve(ctx, args.get("path") or args.get("directory") or ".")
    case = bool(args.get("case_sensitive"))
    glob_filter = args.get("glob") or ""
    max_hits = int(args.get("max_results") or 80)
    rx = query if args.get("regex") else None
    import re

    matcher = None
    if rx:
        try:
            matcher = re.compile(query if case else query, 0 if case else re.IGNORECASE)
        except re.error as exc:
            return ToolResult(False, "", f"invalid regex: {exc}")
    hay_needle = query if case else query.lower()
    hits: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            if glob_filter and not fnmatch.fnmatch(fn.lower(), glob_filter.lower()):
                continue
            if scanned > 4000 or len(hits) >= max_hits:
                break
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                text = fp.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                if matcher:
                    ok = matcher.search(line) is not None
                else:
                    ok = hay_needle in (line if case else line.lower())
                if ok:
                    hits.append(f"{rel(ctx, fp)}:{i}: {line.strip()[:180]}")
                    if len(hits) >= max_hits:
                        break
        if len(hits) >= max_hits or scanned > 4000:
            break
    if not hits:
        return ToolResult(True, f"no matches for {query!r} (scanned {scanned} files)")
    return ToolResult(True, "\n".join(hits) + f"\n— {len(hits)} match(es)")


def write_file(ctx: ToolContext, args: dict) -> ToolResult:
    p = resolve(ctx, args.get("path") or args.get("file") or "")
    if not str(args.get("path") or args.get("file") or ""):
        return ToolResult(False, "", "missing path")
    content = args.get("content")
    if content is None:
        return ToolResult(False, "", "missing content")
    content = str(content)
    append = bool(args.get("append"))
    old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    backup = backup_file(p) if old else None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if append and old:
            p.write_text(old + content, encoding="utf-8")
        else:
            p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(False, "", f"write failed: {exc}")
    final = content if not append else old + content
    diff = make_diff(old, final, rel(ctx, p))
    note = f" [backup: {backup}]" if backup else ""
    words = len(content.split())
    meta = {"path": rel(ctx, p), "created": not bool(old), **line_stats(old, final)}
    return ToolResult(True, f"{'appended to' if append and old else 'wrote'} {rel(ctx, p)} ({len(content)} chars, ~{words} words){note}\n{diff}", meta=meta)


def patch_file(ctx: ToolContext, args: dict) -> ToolResult:
    p = resolve(ctx, args.get("path") or "")
    old_text = args.get("old_text")
    new_text = args.get("new_text")
    if not p.is_file():
        return ToolResult(False, "", f"not a file: {rel(ctx, p)}")
    if old_text is None or new_text is None:
        return ToolResult(False, "", "patch_file needs old_text and new_text")
    old_text, new_text = str(old_text), str(new_text)
    replace_all = bool(args.get("replace_all"))
    try:
        current = p.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(False, "", f"read failed: {exc}")
    count = current.count(old_text)
    if count == 0:
        return ToolResult(False, "", "old_text not found — read the file first and match exact text")
    if count > 1 and not replace_all:
        return ToolResult(False, "", f"old_text matches {count} times; give more context or set replace_all")
    updated = current.replace(old_text, new_text) if replace_all else current.replace(old_text, new_text, 1)
    backup = backup_file(p)
    try:
        p.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolResult(False, "", f"write failed: {exc}")
    diff = make_diff(current, updated, rel(ctx, p))
    meta = {"path": rel(ctx, p), "created": False, **line_stats(current, updated)}
    return ToolResult(True, f"patched {rel(ctx, p)} ({count if replace_all else 1} replacement(s), backup: {backup})\n{diff}", meta=meta)


# ------------------------------------------------------------------ helpers
def backup_file(p: Path) -> Path | None:
    if not p.is_file():
        return None
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    digest = abs(hash(str(p))) % 10**6
    dest_dir = paths.file_backups_dir() / f"{stamp}-{digest:06d}"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        dest.write_bytes(p.read_bytes())
        return dest
    except OSError:
        return None


def line_stats(old: str, new: str) -> dict:
    import difflib as _dl

    a, b = old.splitlines(), new.splitlines()
    sm = _dl.SequenceMatcher(a=a, b=b, autojunk=False)
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
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{name}", tofile=f"b/{name}", n=2,
        )
    )
    out = "".join(lines)
    if len(out) > 4000:
        out = out[:4000] + "\n… [diff truncated]"
    return out or "(no textual change)"


def preview_write(args: dict) -> str:
    path = args.get("path") or args.get("file") or "?"
    p = Path(str(path))
    content = str(args.get("content") or "")
    old = p.read_text(errors="replace") if p.is_file() else ""
    new = (old + content) if args.get("append") else content
    return f"write → {path}\n{make_diff(old, new, str(p))}"


def preview_patch(args: dict) -> str:
    path = str(args.get("path") or "?")
    p = Path(path)
    old_text, new_text = str(args.get("old_text") or ""), str(args.get("new_text") or "")
    if not p.is_file():
        return f"patch → {path} (file missing!)"
    current = p.read_text(errors="replace")
    updated = current.replace(old_text, new_text, -1 if args.get("replace_all") else 1)
    return f"patch → {path}\n{make_diff(current, updated, p.name)}"


def _human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n}B"
