"""Multimodal file reading: images, audio, video → model-visible content.

read_file() routes through here for anything that is not plain text:

- image  → base64 data URL (attached only if the model can see, gated by
  the runtime via the vision probe sidecar)
- audio  → 16 kHz mono wav (ffmpeg) → whisper (voice.transcribe) → text
- video  → evenly sampled JPEG frames (ffmpeg) + embedded audio track
  transcribed the same way; the frames ride the vision channel

Every external dependency (ffmpeg, whisper binaries, STT models) is
detected at call time with an honest error string — never a crash.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import shutil
import tempfile
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".opus", ".mp4a"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv", ".flv"}

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # per image on the wire
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024  # refuse absurd media files outright


def classify(path: Path) -> str:
    """text | image | audio | video | binary — by extension, then sniff."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return "binary"
    if not head:
        return "text"
    if b"\x00" in head:
        return "binary"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def image_data_url(path: Path) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > MAX_IMAGE_BYTES:
        return None
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


async def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return -1, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    """Video/audio length in seconds (0.0 when ffprobe is missing/fails)."""
    fp = _ffprobe()
    if not fp:
        return 0.0
    rc, out, _ = await _run(
        [fp, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=15
    )
    if rc != 0:
        return 0.0
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


async def to_wav16k(path: Path, workdir: Path) -> tuple[Path | None, str | None]:
    """Any audio container → 16 kHz mono PCM wav for whisper.cpp."""
    ff = _ffmpeg()
    if not ff:
        return None, "ffmpeg is not installed — cannot decode this audio format"
    out = workdir / (path.stem + ".16k.wav")
    rc, _, err = await _run(
        [ff, "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out)],
        timeout=120,
    )
    if rc != 0 or not out.exists():
        return None, f"audio decode failed: {err.strip()[-200:] or 'ffmpeg error'}"
    return out, None


async def transcribe_file(path: Path) -> tuple[str | None, str | None]:
    """(text, error). Uses the same whisper/whisperx engines as hold-Ctrl+E."""
    from . import voice

    engine, why = voice.pick_engine()
    if not engine:
        return None, f"no speech-to-text engine available ({why}) — install whisper.cpp or a model via the Market"
    with tempfile.TemporaryDirectory(prefix="oml-stt-") as td:
        wav, err = await to_wav16k(path, Path(td))
        if wav is None:
            return None, err
        text, err2 = await voice.transcribe(wav)
        if err2:
            return None, err2
        return (text or "").strip(), None


async def video_frames(path: Path, max_frames: int = 8) -> tuple[list[Path], str | None]:
    """Evenly sampled JPEG frames (≤ max_frames, ≤1280 px wide) into a temp
    dir owned by the CALLER. Returns (frames, error)."""
    ff = _ffmpeg()
    if not ff:
        return [], "ffmpeg is not installed — cannot extract video frames"
    dur = await probe_duration(path)
    n = max(1, min(max_frames, int(dur) if dur else max_frames))
    td = Path(tempfile.mkdtemp(prefix="oml-frames-"))
    frames: list[Path] = []
    # frame at t≈0 always (the opening shot carries the most context)
    first = td / "frame_00.jpg"
    rc, _, _ = await _run(
        [ff, "-y", "-v", "error", "-ss", "0.1", "-i", str(path),
         "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(first)],
        timeout=60,
    )
    if rc == 0 and first.exists():
        frames.append(first)
    if n > 1:
        fps = (n - 1) / dur if dur and dur > 0 else 1.0 / 5
        pat = td / "frame_%02d.jpg"
        rc, _, err = await _run(
            [ff, "-y", "-v", "error", "-i", str(path),
             "-vf", f"fps={fps:.6f},scale='min(1280,iw)':-2", "-q:v", "3",
             "-frames:v", str(n - 1), str(pat)],
            timeout=180,
        )
        if rc == 0:
            frames.extend(sorted(td.glob("frame_*.jpg")))
    if not frames:
        return [], "no frames could be extracted (corrupt or unsupported video?)"
    return frames, None


async def extract_audio_stream(path: Path, workdir: Path) -> tuple[Path | None, str | None]:
    """True when the video has an audio track: decode it to wav16k directly."""
    ff = _ffmpeg()
    if not ff:
        return None, None
    out = workdir / "video_audio.16k.wav"
    rc, _, _ = await _run(
        [ff, "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out)],
        timeout=180,
    )
    if rc == 0 and out.exists() and out.stat().st_size > 44:  # > bare wav header
        return out, None
    return None, None


def human_media_summary(path: Path, kind: str, dur: float, nframes: int, has_audio: bool) -> str:
    bits = [f"{kind} {path.name}"]
    if dur:
        m, s = divmod(int(dur), 60)
        bits.append(f"{m}:{s:02d} long" if m else f"{s}s long")
    if kind == "video":
        bits.append(f"{nframes} frame(s) attached")
        bits.append("audio track present" if has_audio else "no audio track")
    size = path.stat().st_size
    bits.append(f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024 else f"{size // 1024} KB")
    return ", ".join(bits)
