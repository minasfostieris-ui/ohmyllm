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
# what each engine ACTUALLY covers; None = any language
_ENGINE_LANGS: dict[str, set[str] | None] = {
    "sensevoice": SENSEVOICE_LANGS,
    "parakeet": {"en"},
    "whisperx": None,
    "whisper": None,
}


def norm_lang(lang: str | None) -> str:
    """zh-TW/pt_BR/yue/auto/None -> "" | "zh" | "pt" | "yue" ("" = unknown/auto)."""
    if not lang:
        return ""
    norm = lang.strip().lower()
    if norm in ("auto", ""):
        return ""
    return norm.split("-")[0].split("_")[0]


def pick_engine(lang: str | None = None) -> tuple[str, str] | tuple[None, str]:
    """(engine, binary) — an engine is eligible only when its runtime binary
    AND its own model family are present AND it covers the language. No
    cross-family fallback: feeding whisper-cli a Parakeet GGUF just dies.

    Priority: explicit en -> Parakeet first; explicit ko/zh/ja/yue ->
    SenseVoice first; other/unknown language -> WhisperX (VAD) then
    whisper; auto -> multilingual-safe engines first.
    """
    lg = norm_lang(lang)
    bins = {"sensevoice": find_sensevoice(), "parakeet": find_parakeet(),
            "whisperx": find_whisperx(), "whisper": find_whisper()}
    # whisperx rides the whisper ggml family (its own pattern list is empty)
    models = {"sensevoice": stt_model_of("sensevoice"), "parakeet": stt_model_of("parakeet"),
              "whisperx": stt_model_of("whisper"), "whisper": stt_model_of("whisper")}
    if lg == "en":
        order = ["parakeet", "sensevoice", "whisperx", "whisper"]
    elif lg in ("ko", "zh", "ja", "yue"):
        order = ["sensevoice", "whisperx", "whisper"]
    elif lg:
        order = ["whisperx", "whisper"]
    else:
        order = ["whisperx", "whisper", "sensevoice", "parakeet"]
    for e in order:
        covers = _ENGINE_LANGS[e]
        if covers is not None and lg and lg not in covers:
            continue
        model = models[e]
        if bins[e] and model is not None:
            return e, str(bins[e])
    if any(bins.values()):
        return None, ("STT runtime found but no matching model for this language — "
                      "/market → Speech-to-text (SenseVoice 254 MB / Parakeet 740 MB / "
                      "Whisper small 488 MB)")
    return None, "no STT engine on PATH (whisper-cli / whisperx / sensevoice-cli / parakeet-cli)"


def find_tts() -> str | None:
    for b in _TTS_BINS:
        p = shutil.which(b)
        if p:
            return p
    return None


def recorder_candidates() -> list[list[str]]:
    """Every 16 kHz mono WAV recorder available, best first: PipeWire > ALSA > ffmpeg."""
    out: list[list[str]] = []
    if shutil.which("pw-record"):
        out.append(["pw-record", "--rate", "16000", "--channels", "1"])
    if shutil.which("arecord"):
        out.append(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1"])
    if shutil.which("ffmpeg"):
        src = "pulse" if sys.platform != "darwin" else "avfoundation"
        out.append(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", src,
                    "-i", "default" if src == "pulse" else ":none:default",
                    "-ac", "1", "-ar", "16000"])
    return out


def find_recorder() -> list[str] | None:
    cands = recorder_candidates()
    return cands[0] if cands else None


_STT_PATTERNS = {
    "sensevoice": ("sensevoice*.gguf",),
    "parakeet": ("parakeet*.gguf",),
    "whisper": ("ggml-*.bin",),
}


def stt_model_of(engine: str) -> Path | None:
    d = voice_dir()
    pats = _STT_PATTERNS.get(engine, ())
    for pat in pats:
        hits = sorted(d.rglob(pat), key=lambda p: p.stat().st_size, reverse=True)
        if hits:
            return hits[0]  # biggest = the quality pick the user downloaded
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
    # market stores gradium/phonon-40m-en as voice/tts/gradium__phonon-40m-en/model.q8.gguf —
    # the family lives in the DIRECTORY name, not the file name
    cands = [p for p in d.rglob("*.gguf")
             if "phonon" in str(p.relative_to(d)).lower()
             and "mmproj" not in p.name.lower()]
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
        import time

        cmds = recorder_candidates()
        if not cmds:
            return "no recorder found — install pipewire (pw-record), alsa-utils (arecord) or ffmpeg"
        for cmd in cmds:
            self._cmd = cmd + [str(self.out)]
            try:
                # new session so Ctrl+C in the terminal never kills the recorder
                proc = subprocess.Popen(self._cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        start_new_session=True)
            except OSError:
                continue
            # a dead-on-arrival pw-record (no daemon) exits instantly — give it
            # a moment then fall through to the next candidate instead of
            # "successfully" recording nothing
            time.sleep(0.25)
            if proc.poll() is None:
                self._proc = proc
                return None
            with contextlib.suppress(Exception):
                proc.kill()
                proc.wait(timeout=1)
        return "recorder started but died immediately — no working microphone capture (checked pw-record, arecord, ffmpeg)"

    def stop(self) -> None:
        if self._proc is None:
            return
        # SIGINT FIRST — pw-record/arecord finalize the WAV on it; waiting on a
        # live recorder before the signal froze the UI for ~2s every release.
        # Windows cannot deliver SIGINT via send_signal — terminate() there.
        try:
            if sys.platform == "win32":
                self._proc.terminate()
            else:
                self._proc.send_signal(signal.SIGINT)
            self._proc.wait(timeout=1.5)
        except (OSError, subprocess.SubprocessError, ValueError):
            with contextlib.suppress(Exception):
                self._proc.kill()
                self._proc.wait(timeout=1.0)
        self._proc = None


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
    lg = norm_lang(language)
    if engine == "whisperx":
        # map the downloaded ggml size to whisperx's HF model id instead of
        # always fetching "small" — the turbo ggml means turbo
        wmodel = stt_model_of("whisper")
        name = wmodel.name if wmodel else ""
        wx_model = ("turbo" if "turbo" in name else
                    next((sz for sz in ("tiny", "base", "small", "medium", "large") if sz in name), "small"))
        cmd = [binary, str(wav), "--model", wx_model, "--language", lg or "auto",
               "--output_format", "txt", "--output_dir", str(wav.parent)]
    else:
        model = stt_model_of(engine)
        if model is None:  # never hand an engine another family's file
            return "", (f"no {engine} model downloaded — /market → Speech-to-text "
                        f"({'SenseVoice' if engine == 'sensevoice' else 'Parakeet' if engine == 'parakeet' else 'Whisper small'})")
        if engine == "sensevoice":
            cmd = [binary, "-m", str(model), "-f", str(wav)] + (["-l", lg] if lg else [])
        elif engine == "parakeet":
            cmd = [binary, "-m", str(model), "-f", str(wav)]
        else:
            cmd = [binary, "-m", str(model), "-f", str(wav), "-nt", "-np",
                   "-l", lg or "auto"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
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
    if rc:
        tail = " ".join(err.decode(errors="replace").split())[-200:]
        return "", f"{engine} exited {rc}: {tail or 'no error output'}"
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
    import time as _t

    out_wav = voice_dir() / f"say-{int(_t.time() * 1000)}.wav"
    out_wav.unlink(missing_ok=True)  # a stale wav must never masquerade as this reply
    cmd = [binary, "-m", str(model), "-f", str(out_wav), "--text", text[:2000]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
    except TimeoutError:
        await _kill(proc)
        out_wav.unlink(missing_ok=True)
        return False, "tts timed out"
    except OSError as exc:
        return False, f"tts failed: {exc}"
    if rc:
        tail = " ".join((err or b"").decode(errors="replace").split())[-200:]
        out_wav.unlink(missing_ok=True)
        return False, f"tts engine exited {rc}: {tail or 'no error output'}"
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
        out_wav.unlink(missing_ok=True)
        return False, "playback failed"
    out_wav.unlink(missing_ok=True)
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
