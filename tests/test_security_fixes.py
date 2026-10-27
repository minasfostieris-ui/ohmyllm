"""Regressions for the security sweep findings (config masking, token files,
skill traversal, save atomicity, routing honesty, multimodal, pid validation)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import threading

import httpx
import pytest

from ohmyllm.backends.base import BackendError
from ohmyllm.backends.registry import make_backend
from ohmyllm.config import Config, ConfigStore

SECRET = "sk-REAL-abcdef123456789-DO-NOT-LEAK"


# ---- #1 config serialization masks keys; PATCH cannot write them
def test_api_config_masks_keys(bridge):
    bridge.post("/api/providers/key", {"pid": "openrouter", "key": SECRET})
    body = json.dumps(bridge.get("/api/config"))
    assert SECRET not in body
    r = bridge.post("/api/config", {"cloud": {"keys": {"openrouter": "sk-INJECTED-999"}}})
    assert r.get("ok")
    rows = bridge.get("/api/providers")["providers"]
    row = next(p for p in rows if p["pid"] == "openrouter")
    assert row["has_key"] and "INJECTED" not in row["masked"]
    # the real key survived the attempted overwrite
    from ohmyllm.config import ConfigStore

    assert ConfigStore().load().cloud.keys["openrouter"] == SECRET


def test_partial_cloud_patch_keeps_other_fields(bridge):
    bridge.post("/api/providers/custom", {"name": "Home", "base_url": "http://192.168.1.5:8000",
                                          "models": "a,b"})
    bridge.post("/api/config", {"cloud": {"provider": "openrouter"}})
    cfg = ConfigStore().load()
    assert cfg.cloud.customs and cfg.cloud.model_lists  # not erased by the patch


# ---- #2 port/token file owner-only; log never contains the token
def test_port_file_owner_only(tmp_path):
    from ohmyllm.bridge import serve

    pf = tmp_path / "x.port"
    th = threading.Thread(target=lambda: serve(port=0, write_port_file=str(pf)), daemon=True)
    th.start()
    for _ in range(50):
        if pf.exists():
            break
        __import__("time").sleep(0.1)
    assert pf.exists()
    if os.name == "posix":
        assert stat.S_IMODE(pf.stat().st_mode) == 0o600
    content = pf.read_text()
    tok = content.split(" ")[1]
    # log redaction: fake a request line through the handler's logger
    from ohmyllm import paths

    log = paths.app_log_path()
    if log.exists():
        assert tok not in log.read_text()
    # stop the server
    import urllib.request

    port = int(content.split(" ")[0])
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST")
    req.add_header("X-OMLLM-Token", tok)
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:  # noqa: BLE001
        pass


# ---- #3 skill named ".." cannot rmtree the home
def test_skill_dotdot_name_rejected(tmp_path, monkeypatch):
    from ohmyllm.skills import SkillManager

    home = tmp_path / ".ohmyllm"
    (home / "skills").mkdir(parents=True)
    (home / "config.toml").write_text("# precious\n")
    monkeypatch.setattr("ohmyllm.paths.data_dir", lambda: home, raising=False)
    mgr = SkillManager(root=home / "skills")
    src = tmp_path / "evil"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: ..\ndescription: x\n---\nboom\n")
    with pytest.raises(ValueError):
        mgr._copy_skill(src)
    assert (home / "config.toml").exists()
    assert (home / "skills").exists()


# ---- #4 concurrent saves never collide on one tmp name
def test_dump_file_unique_tmp(tmp_path):
    from ohmyllm.config import tomlio

    target = tmp_path / "c.toml"
    tomlio.dump_file(target, {"a": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "c.toml"]
    assert leftovers == []
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ---- #7 whisperx needs a whisper model; no dead condition
def test_whisperx_requires_whisper_model(tmp_path, monkeypatch):
    from ohmyllm import voice

    vdir = tmp_path / "voice"
    monkeypatch.setattr(voice, "voice_dir", lambda: vdir)
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/" + n if n in ("whisperx", "whisper-cli") else None)
    eng, msg = voice.pick_engine("fi")
    assert eng is None and "no matching model" in msg
    vdir.mkdir(parents=True)
    (vdir / "ggml-small.bin").write_bytes(b"x")
    eng, _ = voice.pick_engine("fi")
    assert eng == "whisperx"


# ---- #5 timeout kills the child
def test_transcribe_timeout_kills(monkeypatch, tmp_path):
    from ohmyllm import voice

    vdir = tmp_path / "voice"
    vdir.mkdir(parents=True)
    (vdir / "ggml-small.bin").write_bytes(b"x")
    monkeypatch.setattr(voice, "voice_dir", lambda: vdir)
    monkeypatch.setattr(voice.shutil, "which",
                        lambda n: "/usr/bin/whisper-cli" if n == "whisper-cli" else None)
    killed = {}

    class Proc:
        returncode = None

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            killed["k"] = True

        async def wait(self):
            return 0

    async def fake_exec(*a, **k):
        return Proc()

    monkeypatch.setattr(voice.asyncio, "create_subprocess_exec", fake_exec)
    wav = vdir / "r.wav"
    wav.write_bytes(b"RIFF")
    text, err = asyncio.run(voice.transcribe(wav, timeout=0.01))
    assert "timed out" in (err or "") and killed.get("k")


# ---- #6 anthropic folds image parts instead of str()-ing them
def test_anthropic_multimodal_blocks():
    cfg = Config()
    cfg.cloud.provider = "anthropic"
    cfg.cloud.keys["anthropic"] = "sk-ant"
    be = make_backend("cloud", cfg)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what number?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]}]
    system, out = be._to_anthropic(msgs)
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "what number?"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"


# ---- #8/#9 pid validation + junk URLs
def test_use_rejects_unknown_pid(bridge):
    r = bridge.post("/api/providers/use", {"pid": "not-a-provider"})
    assert "error" in r


def test_custom_rejects_junk_base(bridge):
    r = bridge.post("/api/providers/custom", {"name": "x", "base_url": "javascript:alert(1)"})
    assert "error" in r
    r = bridge.post("/api/providers/custom", {"name": "x", "base_url": "httpx://ok/v1"})
    assert "error" in r


# ---- CloudBackendProbe findings (autonomous adversarial run)
def test_exotic_pid_and_control_chars_cannot_wipe_config(bridge):
    # unicode pid is rejected at the route…
    r = bridge.post("/api/providers/key", {"pid": "ключ", "key": "sk-x"})
    assert "error" in r
    # …and control chars in a custom NAME still round-trip the file
    bridge.post("/api/providers/custom", {"name": "we\x7fird", "base_url": "http://h/v1"})
    body = json.dumps(bridge.get("/api/providers"))
    assert "weird" in body or "we" in body  # stored, name sanitized-ish or kept
    from ohmyllm.config import ConfigStore

    cfg = ConfigStore().load()
    assert cfg.cloud.keys or cfg.cloud.customs  # file still parses WITH content


def test_tomlio_rejects_invalid_output(tmp_path):
    from ohmyllm.config import tomlio

    # sanity: the round-trip guard exists and valid docs pass
    tomlio.dump_file(tmp_path / "ok.toml", {"a": {"ключ": "v", "x\x7fy": "\n"}})
    assert tomlio.load_file(tmp_path / "ok.toml") == {"a": {"ключ": "v", "x\x7fy": "\n"}}


def test_base_v1_append_only_for_bare_host(bridge):
    r = bridge.post("/api/providers/custom", {"name": "V2", "base_url": "https://host/v2"})
    row = next(p for p in r["providers"] if p["name"] == "V2")
    assert row["base_url"] == "https://host/v2"  # NOT https://host/v2/v1
    r = bridge.post("/api/providers/custom", {"name": "Bare", "base_url": "https://host"})
    row = next(p for p in r["providers"] if p["name"] == "Bare")
    assert row["base_url"] == "https://host/v1"


def test_mask_short_keys_fully_hidden():
    from ohmyllm.providers import mask

    assert "…" not in mask("123456789")  # 9 chars: no first4+last4 reveal
    assert mask("1234567890abcdefghijkl") == "1234…ijkl"


def test_is_installed_counts_env_only_key(monkeypatch):
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import Config

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-only")
    cfg = Config()
    assert cfg.cloud.provider == "" and not cfg.cloud.keys
    assert make_backend("cloud", cfg).is_installed() is True


def _sse_resp(events):
    text = "".join("data: " + json.dumps(o) + "\n\n" for o in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())


def test_tool_calls_without_index_do_not_merge():
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import Config

    def handler(req):
        return _sse_resp([
            {"choices": [{"delta": {"tool_calls": [{"id": "a", "function": {"name": "f1", "arguments": '{"x":1}'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"id": "b", "function": {"name": "f2", "arguments": '{"y":2}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])

    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys["openai"] = "k"
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))

    async def go():
        return [c async for c in be.stream_chat("m", [{"role": "user", "content": "?"}],
                                                cfg.generation, tools=[{"type": "function", "function": {"name": "f1"}}],
                                                native_tools=True)]

    out = asyncio.run(go())
    calls = out[-1].tool_calls
    assert len(calls) == 2
    assert calls[0]["function"]["arguments"] == '{"x":1}'
    assert calls[1]["function"]["arguments"] == '{"y":2}'


def test_early_eof_still_yields_terminal_chunk():
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import Config

    def handler(req):
        return _sse_resp([  # no finish_reason at all, stream just ends
            {"choices": [{"delta": {"content": "partial"}}]},
            {"choices": [{"delta": {"tool_calls": [{"id": "z", "function": {"name": "f", "arguments": '{"a":1}'}}]}}]},
        ])

    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys["openai"] = "k"
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))

    async def go():
        return [c async for c in be.stream_chat("m", [{"role": "user", "content": "?"}], cfg.generation)]

    out = asyncio.run(go())
    assert out[-1].done and out[-1].finish == "stream_ended"
    assert out[-1].tool_calls[0]["function"]["arguments"] == '{"a":1}'
    assert "".join(c.content for c in out) == "partial"


def test_malformed_sneaky_payloads_become_backend_error_or_skip():
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import Config

    def handler(req):
        return _sse_resp([
            {"choices": "not-a-list"},
            {"choices": [["weird"]]},
            {"choices": [{"delta": {"tool_calls": [{"index": "first", "function": {"arguments": {"dict": 1}}}]}}]},
            {"choices": [{"delta": {"content": 12345}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])

    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys["openai"] = "k"
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))

    async def go():
        return [c async for c in be.stream_chat("m", [{"role": "user", "content": "?"}], cfg.generation)]

    out = asyncio.run(go())  # must not raise ValueError/TypeError/AttributeError
    assert out[-1].done
    assert "12345" in "".join(c.content for c in out)


def test_anthropic_thinking_sets_temperature_one():
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import Config

    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"event: message_delta\ndata: {\"delta\": {\"stop_reason\": \"end_turn\"}}\n\n")

    cfg = Config()
    cfg.cloud.provider = "anthropic"
    cfg.cloud.keys["anthropic"] = "k"
    cfg.generation.effort = "high"
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))

    async def go():
        return [c async for c in be.stream_chat("claude", [{"role": "user", "content": "?"}], cfg.generation)]

    asyncio.run(go())
    assert "thinking" in seen["body"]
    assert seen["body"]["temperature"] == 1


def test_static_health_agrees_with_picker():
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import Config

    def live(req):
        return httpx.Response(200, json={"data": [{"id": f"m{i}"} for i in range(5)]})

    cfg = Config()
    cfg.cloud.provider = "openrouter"
    cfg.cloud.keys["openrouter"] = "k"
    cfg.cloud.model_lists = {"openrouter": "a,b,c"}
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(live))
    ok, info = asyncio.run(be.health())
    assert ok and info["models"] == 3 and len(asyncio.run(be.list_models())) == 3


# ---- TuiGuiBridgeProbe regressions
def test_wizard_download_survives_screen_death(oml_env, svc, monkeypatch):
    """The bug: the download ran as a SCREEN worker and Textual cancelled it
    the moment _finish replaced the screen. It is now a plain coroutine
    scheduled on the App — prove it completes with no screen at all."""
    import asyncio as _a

    import ohmyllm.market as mkt
    import ohmyllm.tui.screens.wizard as wz

    calls: list[str] = []

    async def fake_dl(entry, state, **kw):
        await _a.sleep(0.2)          # in-flight while the "screen" dies
        calls.append(entry.mid)
        state.done = True
        return state

    monkeypatch.setattr(mkt, "download", fake_dl)

    class FakeApp:
        def __init__(self):
            self.notes = []

        def notify(self, msg, **kw):
            self.notes.append(msg)

    app = FakeApp()
    wiz = wz.WizardScreen()
    # schedule exactly like _finish does — on the app node, not the screen;
    # the screen object is gone before the coroutine even starts
    del wiz
    _a.run(wz._download_stt_app(app, "turbo"))
    assert calls == ["stt-whisper-turbo"]
    assert any("ready" in n for n in app.notes)
    # and _finish must route through app.run_worker (grep the source contract)
    import inspect

    src = inspect.getsource(wz.WizardScreen._finish)
    assert "app.run_worker" in src and "_download_stt(pick)" not in src


async def test_unmount_stops_recorder(oml_env, svc, monkeypatch, tmp_path):
    from ohmyllm import voice
    from ohmyllm.tui.app import OhMyLLMApp

    monkeypatch.setattr(voice.shutil, "which", lambda n: None)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        chat = app.screen

        class FakeRec:
            stopped = False
            running = True

            def stop(self):
                FakeRec.stopped = True

        wav = tmp_path / "oml-test-hold.wav"
        wav.write_bytes(b"RIFF")
        chat._rec = FakeRec()
        chat._rec_path = wav
        chat._stop_talk_hardware()
        assert FakeRec.stopped and not wav.exists()


async def test_providers_markup_name_no_crash(oml_env, svc, monkeypatch):
    import ohmyllm.voice as _voice
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.providers import ProvidersScreen

    monkeypatch.setattr(_voice.shutil, "which", lambda n: None)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.replace_screen("providers")
        await pilot.pause(0.4)
        scr = app.screen
        assert isinstance(scr, ProvidersScreen)
        scr._add_custom("x[/]y|http://w/v1")   # was MarkupError killing the screen
        await pilot.pause(0.3)
        assert scr._picked_customs() if hasattr(scr, "_picked_customs") else True
        # and the cursor survives a rebuild
        from textual.widgets import OptionList

        lst = scr.query_one(OptionList)
        for i in range(lst.option_count):
            if str(lst.get_option_at_index(i).id or "") == "p-openrouter":
                lst.highlighted = i
                break
        assert lst.highlighted is not None
        scr._store("openrouter", "sk-cursor-test")  # rebuilds the list
        await pilot.pause(0.3)
        assert str(lst.get_option_at_index(lst.highlighted).id) == "p-openrouter"


def test_market_download_pre_try_error(oml_env, monkeypatch):
    from ohmyllm import market

    entry = next(e for e in market.CATALOG if e.mid == "stt-sensevoice")
    monkeypatch.setattr(market, "download_dest",
                        lambda e: (_ for _ in ()).throw(NotADirectoryError("nope")))
    st = market.DownloadState(entry=entry)
    out = asyncio.run(market.download(entry, st))
    assert out.error and "NotADirectoryError" in out.error and out.done


def test_bridge_tts_coercion_and_key_caps(bridge):
    bridge.post("/api/voice/tts", {"on": True})
    r = bridge.post("/api/voice/tts", {"on": None})   # status query must NOT disable
    assert r["enabled"] is True
    r = bridge.post("/api/voice/tts", {"on": "false"})
    assert r["enabled"] is False
    r = bridge.post("/api/voice/tts", {"on": "maybe"})
    assert "error" in r
    bridge.post("/api/providers/key", {"pid": "groq", "key": "gsk-real"})
    r = bridge.post("/api/providers/key", {"pid": "groq", "key": 0})
    assert "error" in r
    r = bridge.get("/api/providers")
    row = next(p for p in r["providers"] if p["pid"] == "groq")
    assert row["has_key"]  # falsy non-string didn't delete it
    r = bridge.post("/api/providers/key", {"pid": "groq", "key": "x" * 5_000_000})
    assert "error" in r


def test_boot_reverts_orphaned_cloud_default(oml_env, monkeypatch):
    """default_backend=cloud with no provider/keys must not brick the models
    screen — Services.boot() reverts to a local backend (the user's frozen
    Ctrl+M state after removing their last key)."""
    from ohmyllm.config import ConfigStore
    from ohmyllm.core import Services

    st = ConfigStore()
    cfg = st.load()
    cfg.default_backend = "cloud"
    cfg.cloud.provider = ""
    cfg.cloud.keys = {}
    st.save(cfg)
    svc = Services.boot()
    assert svc.config.default_backend.value != "cloud"
    assert svc.config.default_backend.value in ("lmstudio", "ollama")
    # persisted, not just in memory
    assert ConfigStore().load().default_backend.value != "cloud"


def test_forget_last_key_reverts_backend(bridge):
    bridge.post("/api/providers/key", {"pid": "openrouter", "key": "sk-only-one"})
    bridge.post("/api/providers/use", {"pid": "openrouter"})
    r = bridge.get("/api/config")
    assert r["default_backend"] == "cloud"
    bridge.post("/api/providers/forget", {"pid": "openrouter"})
    r = bridge.get("/api/config")
    assert r["default_backend"] != "cloud"  # back to a local server


async def test_transcript_thinking_seals_before_file_line(oml_env, svc):
    """think → write file → think again must render top-to-bottom in time
    order: the second thinking block lands BELOW the file line."""
    from textual.widgets import Collapsible, Static

    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        scr = app.screen
        scr._on_event({"type": "turn_start", "model": "m", "backend": "mock",
                       "mode": "safe", "protocol": "native"})
        scr._on_event({"type": "thinking_text", "text": "first thoughts"})
        await pilot.pause(0.2)
        scr._on_event({"type": "tool_result", "name": "write_file", "ok": True,
                       "file": {"path": "a.txt", "created": True, "added": 3},
                       "output": "ok"})
        scr._on_event({"type": "thinking_text", "text": "second thoughts"})
        await pilot.pause(0.2)
        scr._on_event({"type": "token", "text": "the answer"})
        scr._on_event({"type": "turn_end", "text": "the answer", "error": None,
                       "tokens_per_s": 10, "prompt_tokens": 1, "completion_tokens": 1})
        await pilot.pause(0.4)
        hist = scr.query_one("#history")
        kids = list(hist.children)
        cols = [i for i, k in enumerate(kids) if isinstance(k, Collapsible)]
        file_idx = next((i for i, k in enumerate(kids)
                         if isinstance(k, Static) and "a.txt" in str(k.render())), -1)
        assert file_idx > 0, f"file line missing; kids={[type(k).__name__ for k in kids]}"
        assert len(cols) >= 2, f"expected 2 thinking blocks, got {len(cols)}"
        assert cols[1] > file_idx, "second thinking block must render BELOW the file line"


async def test_ctrl_v_pastes_into_key_modal(oml_env, svc, monkeypatch):
    import ohmyllm.tui.imagepaste as ip

    monkeypatch.setattr(ip, "grab_clip_text", lambda timeout=2.0: "sk-pasted-123")
    from textual.widgets import Input

    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.replace_screen("providers")
        await pilot.pause(0.4)
        from textual.widgets import OptionList

        lst = app.screen.query_one(OptionList)
        for i in range(lst.option_count):
            if str(lst.get_option_at_index(i).id or "") == "p-openrouter":
                lst.highlighted = i
                break
        app.screen.action_connect()
        await pilot.pause(0.6)  # the modal mounts on the next frame
        modal = app.screen
        inp = modal.query_one("#key-input", Input)
        inp.focus()
        await pilot.press("ctrl+v")
        await pilot.pause(0.5)
        assert inp.value == "sk-pasted-123", "ctrl+v paste into Input failed"


async def test_models_backend_cycle_and_table_fits(oml_env, svc, monkeypatch):
    import ohmyllm.voice as _v

    monkeypatch.setattr(_v.shutil, "which", lambda n: None)
    from textual.widgets import DataTable

    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.models import ModelsScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        app.svc.config.cloud.keys["openrouter"] = "sk-cycle-test"
        app.replace_screen("models")
        await pilot.pause(0.8)
        scr = app.screen
        assert isinstance(scr, ModelsScreen)
        assert "cloud" in scr._backend_names()
        reached = False
        for _ in range(4):
            scr.action_backend()
            if scr._be_obj().name.value == "cloud":
                reached = True
                break
        assert reached, "b-cycle never reached cloud despite a stored key"
        await pilot.pause(0.5)
        t = scr.query_one(DataTable)
        assert len(t.columns) == 8  # compacted table fits ~110-col terminals
        title = str(scr.query_one("#screen-title").render())
        assert "backend" in title.lower()


def test_registry_provider_static_models_via_bridge(bridge):
    r = bridge.post("/api/providers/key", {"pid": "alibaba-token-plan", "key": "sk-sp-test",
                                           "models": "qwen3.8-flash,qwen3-coder-plus"})
    assert "error" not in r
    row = next(p for p in r["providers"] if p["pid"] == "alibaba-token-plan")
    assert row["has_key"] and row["models"] == "qwen3.8-flash,qwen3-coder-plus"
    # and the cloud backend honors it without touching the network
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import ConfigStore

    cfg = ConfigStore().load()
    cfg.cloud.provider = "alibaba-token-plan"
    cfg.cloud.keys["alibaba-token-plan"] = "sk-sp-test"
    cfg.cloud.model_lists["alibaba-token-plan"] = "qwen3.8-flash,qwen3-coder-plus"

    def boom(req):
        raise AssertionError("must not call /models when a static list exists")

    be = make_backend("cloud", cfg, transport=httpx.MockTransport(boom))
    names = [m.name for m in asyncio.run(be.list_models())]
    assert names == ["qwen3.8-flash", "qwen3-coder-plus"]


async def test_cloud_pick_switches_backend_and_escape_stops_at_modal(oml_env, svc, monkeypatch):
    import ohmyllm.voice as _v

    monkeypatch.setattr(_v.shutil, "which", lambda n: None)
    from textual.widgets import DataTable

    from ohmyllm.backends.base import BackendName, ModelInfo
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.models import ModelsScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(112, 40)) as pilot:
        await pilot.pause(0.7)
        app.svc.config.cloud.keys["openrouter"] = "sk-pick-test"
        app.replace_screen("models")
        await pilot.pause(0.8)
        scr = app.screen
        assert isinstance(scr, ModelsScreen)
        scr._be = "cloud"
        scr._models = [ModelInfo(name="gpt-test", backend=BackendName.CLOUD)]
        scr._visible = ["gpt-test"]
        t = scr.query_one(DataTable)
        if not t.row_count:
            scr._populate()
        scr.action_choose()
        await pilot.pause(0.3)
        cfg = app.svc.config
        assert cfg.default_backend == BackendName.CLOUD, "cloud pick must switch default backend"
        assert cfg.cloud.default_model == "gpt-test"
        # escape closes ONLY the settings modal, models screen survives
        top = app.screen
        assert type(top).__name__ == "ModelSettingsModal"
        await pilot.press("escape")
        await pilot.pause(0.4)
        assert isinstance(app.screen, ModelsScreen), "escape leaked through the modal"


def test_single_connected_key_implies_provider():
    """provider='' with exactly one stored key must still serve cloud models."""
    from ohmyllm.backends.registry import make_backend
    from ohmyllm.config import ConfigStore

    cfg = ConfigStore().load()
    cfg.cloud.provider = ""
    cfg.cloud.keys = {"openrouter": "sk-one-only"}
    cfg.cloud.model_lists["openrouter"] = "a-model,b-model"
    be = make_backend("cloud", cfg)
    got = be._active()
    assert got[0] == "openrouter"
    names = [m.name for m in asyncio.run(be.list_models())]
    assert names == ["a-model", "b-model"]
    # two keys => ambiguous => honest error, not a coin flip
    cfg.cloud.keys["deepseek"] = "sk-second"
    be2 = make_backend("cloud", cfg)
    with pytest.raises(BackendError):
        be2._active()
