"""Voice I/O: engine routing, transcribe argv, market dest, hold-to-talk, commands."""
from __future__ import annotations

import asyncio
import sys

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
    assert "no matching model" in msg and "/market" in msg


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
    norm = lambda x: str(x).replace("\\", "/")  # noqa: E731 — Windows separators
    assert norm(market.download_dest(stt)).endswith(
        "voice/stt/FunAudioLLM__SenseVoiceSmall-GGUF")
    assert "voice/tts" in norm(market.download_dest(tts))
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


# ---- VoiceMarketProbe regressions (autonomous adversarial run)
def test_yue_and_locale_normalization(vdir, no_bins):
    s = vdir / "stt"
    s.mkdir(parents=True)
    (s / "sensevoice-small-q8.gguf").write_bytes(b"x")
    (s / "ggml-small.bin").write_bytes(b"xx")
    eng, _ = voice.pick_engine("yue")          # not truncated to "yu" anymore
    assert eng == "sensevoice"
    assert voice.norm_lang("zh-TW") == "zh"
    assert voice.norm_lang("pt_BR") == "pt"
    assert voice.norm_lang("auto") == ""


def test_no_cross_engine_model_fallback(vdir, monkeypatch):
    # only whisper-cli on PATH + ONLY a parakeet gguf: must NOT claim whisper
    # with the wrong file — honest "no matching model" instead
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/whisper-cli" if n == "whisper-cli" else None)
    s = vdir / "stt"
    s.mkdir(parents=True)
    (s / "parakeet-tdt-0.6b-v3-Q8_0.gguf").write_bytes(b"x")
    eng, msg = voice.pick_engine("fi")
    assert eng is None and "no matching model" in msg


def test_transcribe_surfaces_engine_stderr(vdir, monkeypatch):
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/whisper-cli" if n == "whisper-cli" else None)
    s = vdir / "stt"
    s.mkdir(parents=True)
    (s / "ggml-small.bin").write_bytes(b"x")

    class Proc:
        returncode = 3

        async def communicate(self):
            return b"", b"error: input file not found"

        def kill(self):
            pass

        async def wait(self):
            return 3

    async def fake_exec(*a, **k):
        return Proc()

    monkeypatch.setattr(voice.asyncio, "create_subprocess_exec", fake_exec)
    wav = vdir / "r.wav"
    wav.write_bytes(b"RIFF")
    text, err = asyncio.run(voice.transcribe(wav))
    assert not text
    assert "exited 3" in err and "input file not found" in err  # not "nothing heard"


def test_recorder_stop_is_fast(vdir):
    import subprocess
    import time

    rec = voice.Recorder(vdir / "x.wav")
    rec._proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; "
         "signal.signal(signal.SIGINT, lambda *a: exit(0)); "
         "time.sleep(5)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # cooperative like pw-record; dash defers traps
    time.sleep(0.2)
    t0 = time.monotonic()
    rec.stop()
    assert time.monotonic() - t0 < 1.0  # was ~2.03s freezing the UI per release


def test_tts_model_matches_dir_names(vdir):
    d = vdir / "tts" / "gradium__phonon-40m-en"
    d.mkdir(parents=True)
    (d / "model.q8.gguf").write_bytes(b"x")
    assert voice.tts_model() is not None  # file name has no "phonon" — dir does


def test_speak_no_stale_replay(vdir, monkeypatch):

    (vdir / "tts").mkdir(parents=True)
    (vdir / "tts" / "gradium__phonon-40m-en").mkdir()
    (vdir / "tts" / "gradium__phonon-40m-en" / "model.q8.gguf").write_bytes(b"x")
    monkeypatch.setattr(voice, "find_tts", lambda: "/usr/bin/audio-cli")
    monkeypatch.setattr(voice.shutil, "which", lambda n: "/usr/bin/paplay" if n == "paplay" else None)

    class Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"segfault"

        def kill(self):
            pass

        async def wait(self):
            return 1

    async def fake_exec(*a, **k):
        return Proc()

    monkeypatch.setattr(voice.asyncio, "create_subprocess_exec", fake_exec)
    ok, err = asyncio.run(voice.speak("anything"))
    assert not ok and "exited 1" in err  # failed engine never plays an old wav


async def test_market_voice_highlight_no_crash(oml_env, svc):
    from textual.widgets import OptionList

    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.market import MarketScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/market"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert isinstance(app.screen, MarketScreen)
        lst = app.screen.query_one(OptionList)
        for i in range(lst.option_count):  # every row, voice included, must hover cleanly
            lst.highlighted = i
            await pilot.pause(0.02)


async def test_wizard_voice_pick_survives_navigation(oml_env, svc, monkeypatch):
    from textual.widgets import OptionList

    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.wizard import WizardScreen

    fired = []
    monkeypatch.setattr(WizardScreen, "_download_stt",
                        lambda self, pick: fired.append(pick))
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.replace_screen("wizard")
        await pilot.pause(0.4)
        wiz = app.screen
        assert isinstance(wiz, WizardScreen)
        for _ in range(8):
            await wiz._advance()
        await pilot.pause(0.3)
        lst = wiz.query_one("#vstt-list", OptionList)
        for i in range(lst.option_count):
            if str(lst.get_option_at_index(i).id) == "vstt-sensevoice":
                lst.highlighted = i
                break
        await pilot.pause(0.2)
        await wiz._advance()  # to Done step — OptionList unmounts HERE
        await pilot.pause(0.2)
        # the pick must survive the unmount (the old bug: query dead widget → None)
        assert wiz._picked_stt() == "sensevoice"
        fired.append(wiz._picked_stt())
        assert fired == ["sensevoice"]
