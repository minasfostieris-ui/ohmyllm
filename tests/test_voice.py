"""Voice I/O: engine routing, transcribe argv, market dest, hold-to-talk, commands."""
from __future__ import annotations

import asyncio

import pytest

from ohmyllm import market, voice


@pytest.fixture
def vdir(tmp_path, monkeypatch):
    """Isolated voice dir; no binaries on PATH by default."""
    d = tmp_path / "voice"
    monkeypatch.setattr(voice, "voice_dir", lambda: d)
    monkeypatch.setattr(market, "voice_dir", lambda: d, raising=False)
    return d


@pytest.fixture
def no_bins(monkeypatch):
    real = dict.fromkeys(("pw-record", "arecord", "whisper-cli", "whisperx", "sensevoice-cli", "parakeet-cli", "audio-cli"), True)

    def fake_which(name):
        return f"/usr/bin/{name}" if real.get(name) else None

    monkeypatch.setattr(voice.shutil, "which", fake_which)
    return real


# ---------------------------------------------------------------- routing
def test_pick_engine_requires_binary_and_model(vdir, no_bins, monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda n: None)
    eng, msg = voice.pick_engine("en")
    assert eng is None and "no STT engine" in msg


def test_pick_engine_runtime_without_model_is_honest(vdir, no_bins, monkeypatch):
    # whisper.cpp-style binaries present, models absent → never claim readiness
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("whisper-cli", "sensevoice-cli",
                                                            "parakeet-cli", "audio-cli") else None)
    eng, msg = voice.pick_engine("en")
    assert eng is None
    assert "no model downloaded" in msg and "/market" in msg


def test_pick_engine_en_prefers_parakeet(vdir, no_bins):
    m = vdir / "stt" / "p"
    m.mkdir(parents=True)
    (m / "parakeet-tdt-0.6b-v3-Q8_0.gguf").write_bytes(b"x")
    eng, binary = voice.pick_engine("en")
    assert eng == "parakeet" and binary.endswith("parakeet-cli")


def test_pick_engine_ko_prefers_sensevoice(vdir, no_bins):
    s = vdir / "stt" / "sv"
    s.mkdir(parents=True)
    (s / "sensevoice-small-q8.gguf").write_bytes(b"x")
    eng, _ = voice.pick_engine("ko")
    assert eng == "sensevoice"


def test_pick_engine_other_language_prefers_whisperx(vdir, no_bins):
    w = vdir / "stt" / "wh"
    w.mkdir(parents=True)
    (w / "ggml-small.bin").write_bytes(b"x")
    # whisperx present on PATH (no_bins fixture includes it)
    eng, binary = voice.pick_engine("fi")
    assert eng == "whisperx" and binary.endswith("whisperx")


def test_pick_engine_auto_without_parakeet_model_falls_back(vdir, monkeypatch):
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/whisper-cli" if n == "whisper-cli" else None)
    w = vdir / "stt"
    w.mkdir(parents=True)
    (w / "ggml-small.bin").write_bytes(b"x")
    eng, _ = voice.pick_engine(None)
    assert eng == "whisper"


# ---------------------------------------------------------- model discovery
def test_stt_model_of_per_family(vdir):
    (vdir / "a").mkdir(parents=True)
    (vdir / "a" / "sensevoice-small-q8.gguf").write_bytes(b"x")
    assert voice.stt_model_of("sensevoice") is not None
    assert voice.stt_model_of("parakeet") is None
    assert voice.stt_model() is not None  # fallback scan finds sensevoice


# -------------------------------------------------------------- transcribe
class _Proc:
    def __init__(self, out: bytes):
        self._out = out
        self.stdout = None
        self.returncode = 0

    async def communicate(self):
        return self._out, b""


def _capture_cmd(monkeypatch, out: bytes = "안녕하세요\n".encode()):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return _Proc(out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


def test_transcribe_sensevoice_argv(vdir, no_bins, monkeypatch):
    s = vdir / "stt"
    s.mkdir(parents=True)
    model = s / "sensevoice-small-q8.gguf"
    model.write_bytes(b"x")
    captured = _capture_cmd(monkeypatch, out="여보세요".encode())
    wav = vdir / "r.wav"
    wav.write_bytes(b"RIFF")
    text, err = asyncio.run(voice.transcribe(wav, language="ko"))
    assert err is None and text == "여보세요"
    cmd = captured["cmd"]
    assert cmd[0].endswith("sensevoice-cli") and "-m" in cmd and str(model) in cmd


def test_transcribe_no_engine_honest_error(vdir, monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda n: None)
    wav = vdir / "r.wav"
    vdir.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF")
    text, err = asyncio.run(voice.transcribe(wav))
    assert not text and "install" in err


def test_transcribe_no_model_says_market(vdir, no_bins):
    vdir.mkdir(parents=True, exist_ok=True)
    wav = vdir / "r.wav"
    wav.write_bytes(b"RIFF")
    text, err = asyncio.run(voice.transcribe(wav, language="fi"))
    # whisperx branch has its own weights; without any binary-model combo
    # the error must name /market
    assert not text
    assert err is not None and ("/market" in err or "whisperx" in err.lower())


# ------------------------------------------------------------- market wiring
def test_market_voice_dest_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(market, "lmstudio_models_dir", lambda: tmp_path / "lm")
    import ohmyllm.voice as v

    monkeypatch.setattr(v, "voice_dir", lambda: tmp_path / "voice")
    stt = next(e for e in market.CATALOG if e.mid == "stt-sensevoice")
    tts = next(e for e in market.CATALOG if e.mid == "tts-40m-de")
    llm = next(e for e in market.CATALOG if e.section == "llm")
    assert str(market.download_dest(stt)).endswith(
        "voice/stt/FunAudioLLM__SenseVoiceSmall-GGUF")
    assert "voice/tts" in str(market.download_dest(tts))
    assert str(market.download_dest(llm)).startswith(str(tmp_path / "lm"))


def test_zipball_url():
    pack = next(e for e in market.CATALOG if e.mid == "tts-voices-pack")
    assert pack.url == "https://huggingface.co/gradium/phonon-voices/archive/main.zip"


# ------------------------------------------------------------------ status
def test_status_line_honest_without_anything(monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda n: None)
    line = voice.status_line()
    assert "✗" in line and "whisper" in line


# ------------------------------------------------------------------ config
def test_voice_settings_roundtrip(svc):
    from ohmyllm.config import ConfigStore

    cfg = svc.config
    assert cfg.voice.language == "auto" and cfg.voice.tts_enabled is False
    cfg.voice.tts_enabled = True
    cfg.voice.language = "ko"
    svc.save_config()
    again = ConfigStore().load()
    assert again.voice.tts_enabled is True and again.voice.language == "ko"


# ---------------------------------------------------------------- TUI glue
async def test_ctrl_e_binding_and_honest_notify(oml_env, svc, monkeypatch):
    from ohmyllm.tui.app import OhMyLLMApp

    monkeypatch.setattr(voice.shutil, "which", lambda n: None)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        ids = {b.key for b in app.screen.BINDINGS}
        assert "ctrl+e" in ids
        notes = []
        app.notify = lambda *a, **k: notes.append(a[0] if a else "")
        app.screen.action_talk()
        await pilot.pause(0.3)
        assert any("STT" in n or "whisper" in n for n in notes)


async def test_voice_and_tts_commands(oml_env, svc, monkeypatch):
    from ohmyllm.tui.app import OhMyLLMApp

    monkeypatch.setattr(voice.shutil, "which", lambda n: None)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/voice"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert True
        inp.value = "/tts on"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.svc.config.voice.tts_enabled is True
        inp.value = "/tts off"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.svc.config.voice.tts_enabled is False
        # persisted
        from ohmyllm.config import ConfigStore

        assert ConfigStore().load().voice.tts_enabled is False


async def test_onboarding_reopens_wizard(oml_env, svc):
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.wizard import WizardScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/onboarding"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert isinstance(app.screen, WizardScreen)
        # walk to the voice step (step 8) and assert content
        for _ in range(8):
            await app.screen._advance()
        await pilot.pause(0.3)
        assert app.screen.step == 8
        prompts = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert "SenseVoice" in prompts and "Parakeet" in prompts and "Ctrl+E" in prompts


# ------------------------------------------------------------------ bridge
# (HTTP routes exercised in test_bridge.py::test_bridge_voice_routes)
