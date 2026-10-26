"""Voice I/O: speech-to-text (whisper.cpp) and text-to-speech (audio.cpp/Phonon).

Everything degrades honestly: if the runtime binary is missing we say so
with the exact install command instead of pretending. Models live under
~/.ohmyllm/voice/ (marketplace downloads land there for STT/TTS entries).
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .paths import global_home


def voice_dir() -> Path:
    d = global_home() / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- discovery
_WHISPER_BINS = ("whisper-cli", "whisper-cpp", "whisper", "main")
_WHISPERX_BINS = ("whisperx",)
_SENSEVOICE_BINS = ("sensevoice-cli", "sensevoice", "sense-voice")
_PARAKEET_BINS = ("parakeet-cli", "parakeet", "parakeet-tdt")
_TTS_BINS = ("audio-cli", "audiocpp", "audio.cpp", "phonon-cli", "tts")


def _which_any(names: tuple[str, ...]) -> str | None:
    for b in names:
        p = shutil.which(b)
        if p:
            return p
    return None


def find_whisper() -> str | None:
    return _which_any(_WHISPER_BINS)


def find_whisperx() -> str | None:
    """WhisperX = Whisper + VAD segmentation — preferred for long/non-EN clips."""
    return _which_any(_WHISPERX_BINS)


def find_sensevoice() -> str | None:
    return _which_any(_SENSEVOICE_BINS)


def find_parakeet() -> str | None:
    return _which_any(_PARAKEET_BINS)


# languages each engine actually covers (from the models we ship)
SENSEVOICE_LANGS = {"en", "ko", "zh", "ja", "yue"}


def pick_engine(lang: str | None = None) -> tuple[str, str] | tuple[None, str]:
    """(engine, binary) for the current models + language.

    Routing: explicit Korean/Chinese/Japanese -> SenseVoice if present;
    English-only -> Parakeet when present; anything else (or unknown) ->
    WhisperX (Whisper+VAD) then plain whisper.
    """
    lg = (lang or "").lower()[:2]
    sv, pk, wx, wh = find_sensevoice(), find_parakeet(), find_whisperx(), find_whisper()
    if lg in SENSEVOICE_LANGS and sv and (stt_model_of("sensevoice") or lg in {"ko", "zh", "ja", "yue"}):
        return "sensevoice", sv
    if lg == "en" and pk and stt_model_of("parakeet"):
        return "parakeet", pk
    if wx and stt_model_of("whisper"):
        return "whisperx", wx
    if wh and stt_model():
        return "whisper", wh
    if sv and stt_model_of("sensevoice"):
        return "sensevoice", sv
    if pk and stt_model_of("parakeet"):
        return "parakeet", pk
    if any((sv, pk, wx, wh)):
        return None, ("STT runtime found but no model downloaded — /market → "
                      "Speech-to-text (SenseVoice 254 MB / Parakeet 740 MB / Whisper small 488 MB)")
    return None, "no STT engine on PATH (whisper-cli / whisperx / sensevoice-cli / parakeet-cli)"


def find_tts() -> str | None:
    for b in _TTS_BINS:
        p = shutil.which(b)
        if p:
            return p
    return None


def find_recorder() -> list[str] | None:
    """Best 16 kHz mono WAV recorder available: PipeWire > ALSA > ffmpeg."""
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", "16000", "--channels", "1"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1"]
    if shutil.which("ffmpeg"):
        src = "pulse" if sys.platform != "darwin" else "avfoundation"
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", src,
                "-i", "default" if src == "pulse" else ":none:default",
                "-ac", "1", "-ar", "16000"]
    return None


_STT_PATTERNS = {
    "sensevoice": ("sensevoice*.gguf",),
    "parakeet": ("parakeet*.gguf",),
    "whisper": ("ggml-*.bin",),
}


def stt_model_of(engine: str) -> Path | None:
    d = voice_dir()
    pats = _STT_PATTERNS.get(engine, ())
    for pat in pats:
        hits = sorted(d.rglob(pat), key=lambda p: p.stat().st_size)
        if hits:
            return hits[0]
    return None


def stt_model() -> Path | None:
    d = voice_dir()
    bins = sorted(d.rglob("ggml-*.bin"), key=lambda p: p.stat().st_size)
    if bins:
        return bins[0]
    for eng in ("sensevoice", "parakeet"):
        m = stt_model_of(eng)
        if m:
            return m
    return None


def tts_model() -> Path | None:
    d = voice_dir()
    cands = [p for p in d.rglob("*.gguf") if "phonon" in p.name.lower() and "mmproj" not in p.name.lower()]
    return sorted(cands, key=lambda p: p.stat().st_size)[0] if cands else None


# ---------------------------------------------------------------- record
class Recorder:
    """Subprocess recorder; stop() flushes and finalizes the WAV."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self._proc: subprocess.Popen | None = None
        self._cmd: list[str] | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> str | None:
        cmd = find_recorder()
        if cmd is None:
            return "no recorder found — install pipewire (pw-record), alsa-utils (arecord) or ffmpeg"
        self._cmd = cmd + [str(self.out)]
        try:
            # new session so Ctrl+C in the terminal never kills the recorder
            self._proc = subprocess.Popen(self._cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          start_new_session=True)
        except OSError as exc:
            return f"recorder failed to start: {exc}"
        return None

    def stop(self) -> None:
        if self._proc is None:
            return
        with_suppress_errors(self._proc)
        try:
            self._proc.send_signal(signal.SIGINT)
            self._proc.wait(timeout=4)
        except (OSError, subprocess.SubprocessError):
            with_suppress_errors(self._proc, kill=True)
        self._proc = None


def with_suppress_errors(proc: subprocess.Popen, kill: bool = False) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        if kill:
            proc.kill()
        proc.wait(timeout=2)


# ------------------------------------------------------------- transcribe
async def transcribe(wav: Path, *, language: str | None = None,
                     timeout: float = 120.0) -> tuple[str, str | None]:
    """(text, error). Routes to the right engine for the language + the
    models actually downloaded: SenseVoice (en/ko/zh/ja/yue), Parakeet
    (best English), WhisperX/whisper (~99 languages)."""
    engine, binary = pick_engine(language)
    if engine is None:
        return "", (binary + " — install: whisper.cpp (cmake --build build), "
                    "pip install whisperx, sensevoice.cpp, or parakeet.cpp")
    if engine == "whisperx":
        model = None
        cmd = [binary, str(wav), "--model", "small", "--language", language or "auto",
               "--output_format", "txt", "--output_dir", str(wav.parent)]
    else:
        model = stt_model_of(engine) or stt_model()
        if model is None:
            return "", (f"no {engine} model downloaded — /market → Speech-to-text "
                        f"({'SenseVoice' if engine == 'sensevoice' else 'Parakeet' if engine == 'parakeet' else 'Whisper small'})")
        if engine == "sensevoice":
            cmd = [binary, "-m", str(model), "-f", str(wav)] + (["-l", language] if language else [])
        elif engine == "parakeet":
            cmd = [binary, "-m", str(model), "-f", str(wav)]
        else:
            cmd = [binary, "-m", str(model), "-f", str(wav), "-nt", "-np",
                   "-l", language or "auto"]
    _ = model
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = " ".join(ln.strip() for ln in out.decode(errors="replace").splitlines() if ln.strip())
        if engine == "whisperx" and not text.strip():
            cand = wav.with_suffix(".txt")  # whisperx names it after the input stem
            if cand.exists():
                text = cand.read_text(errors="replace").strip()
    except TimeoutError:
        await _kill(proc)
        return "", "transcription timed out"
    except OSError as exc:
        return "", f"{engine} failed: {exc}"
    return text.strip() or "", None if text.strip() else "nothing heard"


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """wait_for cancels the reader, not the child — reap it or leak a process."""
    with contextlib.suppress(Exception):
        proc.kill()
        await proc.wait()


# -------------------------------------------------------------------- speak
async def speak(text: str, *, timeout: float = 180.0) -> tuple[bool, str | None]:
    binary = find_tts()
    if binary is None:
        return False, ("no TTS runtime — audio.cpp (Phonon GGUFs): "
                       "https://github.com/audiocpp — once installed, /tts on works")
    model = tts_model()
    if model is None:
        return False, "no Phonon voice downloaded — /market → Text-to-speech"
    play = shutil.which("paplay") or shutil.which("ffplay") or shutil.which("afplay")
    if play is None:
        return False, "no audio player (paplay/ffplay/afplay)"
    out_wav = voice_dir() / "last-say.wav"
    cmd = [binary, "-m", str(model), "-f", str(out_wav), "--text", text[:2000]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _kill(proc)
        return False, "tts timed out"
    except OSError as exc:
        return False, f"tts failed: {exc}"
    if not out_wav.exists() or out_wav.stat().st_size < 1000:
        return False, "tts produced no audio"
    play_cmd = [play, str(out_wav)] if "ffplay" not in play else [play, "-nodisp", "-autoexit", "-loglevel", "error", str(out_wav)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *play_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _kill(proc)
        return False, "playback timed out"
    except OSError:
        return False, "playback failed"
    return True, None


def status_line() -> str:
    parts = []
    eng = [e for e, ok in (("sensevoice", find_sensevoice()), ("parakeet", find_parakeet()),
                           ("whisperx", find_whisperx()), ("whisper", find_whisper())) if ok]
    parts.append("🎤 " + ("✓ " + "/".join(eng) if eng else "✗ (install whisper.cpp / whisperx / sensevoice.cpp / parakeet.cpp)"))
    parts.append("🔊 " + ("tts ✓" if find_tts() else "tts ✗"))
    parts.append("mic " + ("✓" if find_recorder() else "✗"))
    parts.append("stt-model " + ("✓" if stt_model() else "✗"))
    parts.append("voice " + ("✓" if tts_model() else "✗"))
    return " · ".join(parts)
