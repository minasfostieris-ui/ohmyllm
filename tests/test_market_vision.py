"""Marketplace catalog/VRAM/download + vision paste plumbing."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import httpx

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
    assert len(market.CATALOG) == 8
    ids = {e.mid for e in market.CATALOG}
    assert {"qwen38-iq2xs", "qwen38-q6k", "sushi-9b", "gemma4-26b",
            "gpt-oss-20b", "qwen3-coder-30b", "r1-14b", "phi-4"} <= ids
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
    dest = tmp_path / "ohmyllm-market" / "test.gguf"
    assert dest.read_bytes() == payload
    assert not (tmp_path / "ohmyllm-market" / "test.gguf.part").exists()


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
    assert (tmp_path / "ohmyllm-market" / "big.gguf.part").stat().st_size > 0


# ------------------------------------------------------------------ paste
def test_png_size_roundtrip(tmp_path):
    from ohmyllm.tui.imagepaste import data_uri, png_size

    p = _mkpng(tmp_path / "x.png")
    assert png_size(p) == (1563, 200)
    uri = data_uri(p)
    assert uri.startswith("data:image/png;base64,")


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
        assert lst.option_count >= 4
        txt = str(lst.get_option_at_index(0).prompt)
        assert "GB" in txt


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
    import asyncio

    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.widgets import ConfirmModal

    app = OhMyLLMApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.7)
        # bindings that await a modal must run outside the pilot's message
        # pump (same idiom as the established quit test)
        task = asyncio.create_task(app.action_safe_quit())
        await pilot.pause(0.5)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("n")  # cancel
        await task
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
