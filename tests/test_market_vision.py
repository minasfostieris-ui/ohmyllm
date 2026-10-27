"""Marketplace catalog/VRAM/download + vision paste plumbing."""
from __future__ import annotations

import asyncio
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from ohmyllm import market


def _mkpng(p: Path, w: int = 1563, h: int = 200) -> Path:
    raw = b"".join(b"\x00" + bytes([x % 256 for x in range(w)]) for _ in range(h))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                  + chunk(b"IDAT", zlib.compress(raw[:1024])) + chunk(b"IEND", b""))
    return p


# ------------------------------------------------------------------ catalog
def test_catalog_curated_entries():
    assert len(market.CATALOG) == 24
    secs = {e.section for e in market.CATALOG}
    assert secs == {"llm", "stt", "tts"}
    assert all(e.mid.startswith("stt-") for e in market.CATALOG if e.section == "stt")
    ids = {e.mid for e in market.CATALOG}
    assert {"qwen38-iq2xs", "qwen38-q6k", "sushi-9b", "gemma4-26b", "gemma4-12b",
            "gpt-oss-20b", "qwen3-coder-30b", "r1-14b", "phi-4"} <= ids
    g12 = next(e for e in market.CATALOG if e.mid == "gemma4-12b")
    assert g12.mtp == "mtp-gemma-4-12B-it.gguf" and g12.mtp_url.endswith(g12.mtp)
    for e in market.CATALOG:
        assert e.size_gb > 0 and e.url.startswith("https://huggingface.co/")
        assert e.file in e.url


def test_resolve_picks_ninfer_only_when_runtime_exists(monkeypatch):
    gemma = next(e for e in market.CATALOG if e.mid == "gemma4-26b")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert market.resolve(gemma).kind == "gguf", "no NInfer runtime -> official GGUF"
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ninfer" if name == "ninfer" else None)
    assert market.resolve(gemma).kind == "ninfer", "NInfer installed -> MTP build"
    assert market.resolve(gemma).file.endswith(".ninfer")


def test_fit_verdict_thresholds():
    vram = 12 * 1024**3
    assert market.fit_verdict(8.4, vram)[0] == "fits"
    assert market.fit_verdict(11.0, vram)[0] == "tight"
    assert market.fit_verdict(22.0, vram)[0] == "no"
    assert market.fit_verdict(8.4, None)[0] == "unknown"


def test_detect_vram_runs_silently():
    v = market.detect_vram_bytes()
    assert v is None or v > 0


# ------------------------------------------------------------------- vision
def test_scan_vision_models_finds_mmproj_siblings(tmp_path):
    d = tmp_path / "qwen-vl-7b"
    d.mkdir()
    (d / "Qwen-VL-7B.Q4_K_M.gguf").write_bytes(b"x")
    (d / "mmproj-Qwen-VL-7B-F16.gguf").write_bytes(b"x")
    plain = tmp_path / "llama-3-8b"
    plain.mkdir()
    (plain / "Llama-3-8B.Q6_K.gguf").write_bytes(b"x")
    found = market.scan_vision_models(tmp_path)
    assert any("qwen-vl" in f for f in found), found
    assert not any("llama-3" in f for f in found)
    assert market.has_vision("qwen-vl-7b-q4", found) is True
    assert market.has_vision("some-llama-3-8b-instruct", found) is False
    assert market.has_vision("mystery-model", found) is None


# ----------------------------------------------------------------- download
async def test_market_download_streams_with_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_MODEL_PATH", str(tmp_path))
    payload = b"\x89PNG" + b"Z" * 200_000
    entry = market.MarketEntry(
        mid="t", title="Test Model", repo="acme/test-gguf", file="test.gguf",
        size_gb=0.0002, blurb="x")

    def handler(req: httpx.Request) -> httpx.Response:
        rng = req.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return httpx.Response(206, content=payload[start:],
                                  headers={"content-length": str(len(payload) - start)})
        return httpx.Response(200, content=payload,
                              headers={"content-length": str(len(payload))})

    state = market.DownloadState(entry=entry)
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    out = await market.download(entry, state, client_factory=factory)
    assert out.done and out.error is None
    dest = market.download_dest(entry) / "test.gguf"
    assert dest.parent.name == "acme__test-gguf", dest
    assert dest.read_bytes() == payload
    assert not (dest.parent / "test.gguf.part").exists()


async def test_market_download_cancel_keeps_part(tmp_path, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_MODEL_PATH", str(tmp_path))
    payload = b"Q" * 500_000

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    entry = market.MarketEntry(mid="t", title="T", repo="a/b", file="big.gguf",
                               size_gb=0.5, blurb="x")
    state = market.DownloadState(entry=entry)
    fired = {"n": 0}

    def cancel():
        fired["n"] += 1
        return fired["n"] > 2  # let a couple of chunks land first

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    out = await market.download(entry, state, cancel=cancel, chunk=4096, client_factory=factory)
    assert out.error == "cancelled" and not out.done
    assert (market.download_dest(entry) / "big.gguf.part").stat().st_size > 0


# ------------------------------------------------------------------ paste
def test_png_size_roundtrip(tmp_path):
    from ohmyllm.tui.imagepaste import data_uri, png_size

    p = _mkpng(tmp_path / "x.png")
    assert png_size(p) == (1563, 200)
    uri = data_uri(p)
    assert uri.startswith("data:image/png;base64,")


async def test_market_download_button_flow(oml_env, svc, monkeypatch):
    # end-to-end: click a card -> progress -> done label (this path once used
    # a nonexistent App.create_task and was never exercised)
    import asyncio as _a

    from textual.widgets import OptionList, ProgressBar, Static

    from ohmyllm import market as m
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.market import MarketScreen

    async def fake_dl(entry, state, cancel=None, **kw):
        state.total = int(entry.size_gb * 1e9)
        for i in range(4):
            state.received = int(state.total * (i + 1) / 4)
            state.mbps = 42.0
            await _a.sleep(0.1)
        state.done = True
        return state

    monkeypatch.setattr(m, "download", fake_dl)
    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        app.replace_screen("market")
        await pilot.pause(0.5)
        screen = app.screen
        assert isinstance(screen, MarketScreen)
        lst = screen.query_one(OptionList)
        lst.highlighted = 1  # 0 is the disabled section header
        await pilot.press("enter")
        await pilot.pause(1.0)
        label = screen.query_one("#market-dl-label", Static)
        txt = str(label.render())
        assert "ready" in txt, txt
        bar = screen.query_one("#market-progress", ProgressBar)
        assert bar.progress == 100


async def test_choose_opens_settings_for_the_cursor_row(oml_env, svc):
    # regression: choose() repopulated the table (cursor reset to row 0) and
    # then re-read the selection — the settings modal opened for the WRONG
    # model (user picked gemma, got "Settings for axon 250m")
    from textual.widgets import DataTable

    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.models import ModelsScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.9)
        app.replace_screen("models")
        await pilot.pause(0.5)
        screen = app.screen
        assert isinstance(screen, ModelsScreen)
        t = screen.query_one(DataTable)
        # offline backend probes cost up to 3 s each and Windows runners can
        # hang on them — wait for the rows, don't encode a platform's timing
        for _ in range(60):
            if t.row_count >= 2:
                break
            await pilot.pause(0.25)
        assert t.row_count >= 2
        names = [r.name for r in screen._rows if r is not None]
        target = names[-1]  # last row — never row 0
        t.move_cursor(row=len(screen._rows) - 1)
        await pilot.pause(0.2)
        assert screen._selected() is not None and screen._selected().name == target
        # any re-populate (refresh, background metadata) must NOT drop the
        # selection to row 0 — that's what opened settings for the wrong model
        screen._populate()
        await pilot.pause(0.2)
        assert screen._selected() is not None and screen._selected().name == target, \
            "cursor selection lost across repopulate"
        screen._open_settings(screen._selected())
        await pilot.pause(0.5)
        modal = [sc for sc in app.screen_stack if type(sc).__name__ == "ModelSettingsModal"]
        assert modal and modal[-1].model.name == target, \
            (modal[-1].model.name if modal else None, target)


async def test_think_hint_is_model_aware(oml_env, svc):
    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        screen = app.screen
        app.session.model = "llama-3.1-8b-instruct"
        assert "no thinking mode" in (screen._think_hint() or "")
        app.session.model = "qwen3.5-9b-sushi-coder-rl"
        assert "supports thinking" in (screen._think_hint() or "")
        app.session.model = "mystery-9b"
        assert screen._think_hint() is None


async def test_market_screen_lists_fits(oml_env, svc):
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
        from textual.widgets import OptionList

        lst = app.screen.query_one(OptionList)
        assert lst.option_count >= 24  # 9 llm + 4 stt + 11 phonon + 3 section headers
        prompts = " ".join(str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count))
        assert "GB" in prompts and "SenseVoice" in prompts and "Phonon" in prompts
        assert "Speech-to-text" in prompts and "Text-to-speech" in prompts


async def test_paste_attaches_and_sends_parts(oml_env, svc, tmp_path, monkeypatch):
    from ohmyllm.tui.app import OhMyLLMApp

    png = _mkpng(tmp_path / "clip.png")
    import ohmyllm.tui.imagepaste as ip

    monkeypatch.setattr(ip, "grab_clip_image", lambda: png)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        screen = app.screen
        app.session.model = "mock-model"
        app.svc._vision_cache = {"mock-model": True}
        screen.action_paste_image()
        await pilot.pause(0.3)
        assert screen._pending_images == [png]
        from ohmyllm.tui.imagepaste import ImageBox

        assert len(screen.query(ImageBox)) == 1
        inp = app.screen.query_one("#chat-input")
        inp.value = "what's in the image?"
        await pilot.press("enter")
        await pilot.pause(1.5)
        msgs = app.session.messages
        user = [m for m in msgs if m["role"] == "user" and isinstance(m["content"], list)]
        assert user, "message must carry OpenAI-style content parts"
        parts = user[0]["content"]
        assert parts[0]["type"] == "text" and parts[1]["image_url"]["url"].startswith("data:image/png")


async def test_ctrl_c_never_opens_quit_modal(oml_env, svc):
    # ctrl+C is the copy chord users reach for; quitting is ctrl+Q only
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.widgets import ConfirmModal

    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        await pilot.press("ctrl+c")
        await pilot.pause(0.4)
        assert not isinstance(app.screen, ConfirmModal), "ctrl+C must not quit"
        assert app.screen.query_one("#chat-input") is not None


async def test_quit_modal_opens_and_responds(oml_env, svc):
    # regression: the binding used to await the modal inside the message pump
    # — ctrl+Q froze the whole UI (nothing clickable, nothing typeable). A
    # plain pilot.press must return and the modal must take input.
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.widgets import ConfirmModal

    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        await pilot.press("ctrl+q")
        await pilot.pause(0.5)
        assert isinstance(app.screen, ConfirmModal), "ctrl+Q must open the quit modal"
        await pilot.press("n")  # cancel
        await pilot.pause(0.5)
        assert app.is_running
        assert type(app.screen).__name__ == "ChatScreen"


async def test_paste_grab_runs_off_the_ui_thread(oml_env, svc, tmp_path, monkeypatch):
    import time as _t

    import ohmyllm.tui.imagepaste as ip
    from ohmyllm.tui.app import OhMyLLMApp

    png = _mkpng(tmp_path / "slow.png")

    def slow_grab():
        _t.sleep(1.5)  # simulate a blocking Wayland clipboard offer
        return png

    monkeypatch.setattr(ip, "grab_clip_image", slow_grab)
    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        screen = app.screen
        app.session.model = "mock-model"
        app.svc._vision_cache = {"mock-model": True}
        screen.action_paste_image()
        await pilot.pause(0.3)
        # UI must still process input while the grab blocks its worker thread
        inp = app.screen.query_one("#chat-input")
        inp.value = "typing works during the grab"
        await pilot.pause(0.2)
        assert inp.value == "typing works during the grab", "UI froze during clipboard grab"
        await pilot.pause(2.0)  # let the slow worker land
        from ohmyllm.tui.imagepaste import ImageBox

        assert len(app.screen.query(ImageBox)) == 1


async def test_models_table_never_overflows_horizontally(oml_env, svc):
    # regression: fixed column widths exceeded narrow terminals, the
    # horizontal scrollbar appeared and model names scrolled off-screen
    from textual.widgets import DataTable

    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.models import ModelsScreen

    app = OhMyLLMApp()
    async with app.run_test(size=(84, 40)) as pilot:
        await pilot.pause(0.9)
        app.replace_screen("models")
        await pilot.pause(0.5)
        screen = app.screen
        assert isinstance(screen, ModelsScreen)
        t = screen.query_one(DataTable)
        for _ in range(60):
            if t.row_count:
                break
            await pilot.pause(0.25)
        assert t.row_count >= 2
        # every column (incl. the last) must fit inside the table body
        assert t.virtual_size.width <= t.size.width
        assert t.styles.scrollbar_size_horizontal == 0


async def test_paste_text_only_when_model_blind(oml_env, svc, tmp_path, monkeypatch):
    from ohmyllm.tui.app import OhMyLLMApp

    png = _mkpng(tmp_path / "clip2.png")
    import ohmyllm.tui.imagepaste as ip

    monkeypatch.setattr(ip, "grab_clip_image", lambda: png)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        screen = app.screen
        app.session.model = "llama-3.1-8b"
        app.svc._vision_cache = {"llama-3.1-8b": False}
        screen.action_paste_image()
        inp = app.screen.query_one("#chat-input")
        inp.value = "describe this"
        await pilot.press("enter")
        await pilot.pause(1.2)
        msgs = app.session.messages
        assert all(isinstance(m["content"], str) for m in msgs if m["role"] == "user"), \
            "non-vision model must receive plain text"


def test_kv_cache_defaults_and_wire():
    import asyncio as _a

    from ohmyllm.backends.lmstudio import LMStudioBackend
    from ohmyllm.config import Config
    from ohmyllm.config.models import GenerationSettings

    gen = GenerationSettings()
    assert gen.kv_cache == "q4_1", "user default: q4_1"

    def _body(kv):
        gen = GenerationSettings(kv_cache=kv)
        cap: dict = {}

        def h(req: httpx.Request) -> httpx.Response:
            cap["b"] = json.loads(req.content.decode())
            return httpx.Response(200, text="data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        be = LMStudioBackend(Config(), transport=httpx.MockTransport(h))

        async def go() -> None:
            async for _ in be.stream_chat("m", [{"role": "user", "content": "x"}], gen):
                pass

        _a.run(go())
        return cap["b"]

    b = _body("q4_1")
    assert b["cache_type_k"] == "q4_1" and b["cache_type_v"] == "q4_1"
    b = _body("f16")
    assert "cache_type_k" not in b


def test_kv_validator_rejects_garbage():
    import pydantic

    from ohmyllm.config.models import GenerationSettings

    with pytest.raises(pydantic.ValidationError):
        GenerationSettings(kv_cache="q99_impossible")


def test_sort_models_keys():
    from ohmyllm.backends.base import ModelInfo
    from ohmyllm.config.models import BackendName
    from ohmyllm.tui.screens.models import sort_models

    ms = [
        ModelInfo(name="zeta-large", backend=BackendName.MOCK, size_bytes=9e9, params_b=9, quant="Q4_K_M"),
        ModelInfo(name="alpha-small", backend=BackendName.MOCK, size_bytes=1e9, params_b=1, quant="IQ2_XS"),
        ModelInfo(name="beta-mid", backend=BackendName.MOCK, size_bytes=5e9, params_b=4, quant="Q6_K"),
    ]
    assert [m.name for m in sort_models(ms, "name")] == ["alpha-small", "beta-mid", "zeta-large"]
    assert sort_models(ms, "size")[0].name == "zeta-large"
    assert sort_models(ms, "quant")[0].name == "alpha-small"
    assert sort_models(ms, "params")[0].name == "zeta-large"


def test_thinking_support_heuristics():
    from ohmyllm.capabilities import base_family, thinking_support

    assert thinking_support("qwen3.8-27b-gsq-rco") == "levels"
    assert thinking_support("llama-3.1-8b-instruct") == "none"
    assert thinking_support("Qwen3.5-9b-Sushi-Coder-RL") == "forced"
    assert thinking_support("mystery-7b") is None
    # fine-tune noise stripped: these all used to be "?"
    assert thinking_support("gemma-4-e2b-uncensored-hauhaucs-aggressive", 2.3) == "on-off"
    assert thinking_support("granite-4.2-8b-uncensored", 8) == "on-off"
    assert thinking_support("mistralai/ministral-3-3b", 3) == "none"
    assert thinking_support("qwen-rp") == "none"
    assert thinking_support("tiny-thing", 0.4) == "none"
    assert base_family("gemma-4-e2b-uncensored-hauhaucs-aggressive").startswith("gemma-4")


def test_scan_all_models_definitive_vision(tmp_path):
    from ohmyllm.market import has_vision, scan_all_models, scan_vision_models

    d = tmp_path / "llama-3.1-8b"
    d.mkdir()
    (d / "llama-3.1-8b-instruct.Q4_K_M.gguf").write_bytes(b"x")
    allm = scan_all_models(tmp_path)
    assert any("llama-3.1-8b" in a for a in allm)
    vis = scan_vision_models(tmp_path)
    # on disk but NO sidecar -> definitive False once the caller checks allm
    assert has_vision("llama-3.1-8b-instruct", vis) is False  # name heuristic
    assert has_vision("brand-new-family-9b", vis) is None     # not on disk: honest ?


def test_params_inference():
    from ohmyllm.backends.base import ModelInfo
    from ohmyllm.config.models import BackendName

    m = ModelInfo(name="axon 250m", backend=BackendName.MOCK, size_bytes=725537218, quant="F16")
    assert m.apply_name_heuristics().params_b == 0.25
    m2 = ModelInfo(name="gemma-convo", backend=BackendName.MOCK, size_bytes=int(4.9e9), quant="Q8_0")
    assert m2.apply_name_heuristics().params_b == 4.9


def test_ollama_quantize_kv_option():
    import json

    from ohmyllm.backends.ollama import OllamaBackend
    from ohmyllm.config import Config
    from ohmyllm.config.models import GenerationSettings

    cfg = Config()

    def run(kv):
        gen = GenerationSettings(kv_cache=kv)
        cap: dict = {}

        def h(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode())
            if "messages" in body:
                cap["b"] = body
            return httpx.Response(200, text=json.dumps(
                {"message": {}, "done": True, "done_reason": "stop", "eval_count": 1}))

        be = OllamaBackend(cfg, transport=httpx.MockTransport(h))

        async def go() -> None:
            async for _ in be.stream_chat("m", [{"role": "user", "content": "x"}], gen):
                pass

        asyncio.run(go())
        return cap["b"]

    assert run("q8_0")["options"]["quantize_kv"] is True
    assert "quantize_kv" not in run("f16")["options"]


class _FakeBe:
    def __init__(self, reply=None, error=None):
        self._reply, self._error = reply, error
        self.name = type("N", (), {"value": "mock"})()

    async def stream_chat(self, model, messages, params, **kw):
        if self._error:
            raise RuntimeError(self._error)
        from ohmyllm.backends.base import ChatChunk

        yield ChatChunk(content=self._reply)


async def test_probe_verdicts(monkeypatch):
    from ohmyllm import vision

    monkeypatch.setattr(vision.random, "randint", lambda a, b: 48213)
    assert await vision.probe_vision(_FakeBe("The digits are 48213"), "m") is True
    assert await vision.probe_vision(_FakeBe("I see a cat"), "m") is False
    # explicit image rejection settles on the FIRST error, no retries
    assert await vision.probe_vision(
        _FakeBe(error="this model does not accept image content"), "m") is False


def test_probe_cache_roundtrip(oml_env):
    from ohmyllm import vision

    assert vision.cached_verdict("mock", "never-tested") is None
    vision.save_verdict("mock:never-tested", True)
    assert vision.cached_verdict("mock", "never-tested") is True
    vision.save_verdict("mock:never-tested", False)
    assert vision.cached_verdict("mock", "never-tested") is False


async def test_download_fetches_companion_and_mtp(tmp_path, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_MODEL_PATH", str(tmp_path))
    files = {"big.gguf": b"W" * 50_000, "mmproj-F16.gguf": b"M" * 5_000,
             "mtp-draft.gguf": b"T" * 3_000}

    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        data = files[name]
        return httpx.Response(200, content=data, headers={"content-length": str(len(data))})

    entry = market.MarketEntry(mid="trio", title="T", repo="a/b", file="big.gguf",
                               size_gb=0.05, blurb="x", companion="mmproj-F16.gguf",
                               mtp="mtp-draft.gguf")
    state = market.DownloadState(entry=entry)
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    out = await market.download(entry, state, client_factory=factory)
    assert out.done and not out.error
    dest = market.download_dest(entry)
    assert (dest / "big.gguf").read_bytes() == files["big.gguf"]
    assert (dest / "mmproj-F16.gguf").read_bytes() == files["mmproj-F16.gguf"]
    assert (dest / "mtp-draft.gguf").read_bytes() == files["mtp-draft.gguf"]
