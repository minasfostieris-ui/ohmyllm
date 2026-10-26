"""Effort ladder across backends + /skill: casting + /skillimport."""
from __future__ import annotations

import asyncio
import json

import httpx

from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.backends.ollama import OllamaBackend
from ohmyllm.config import Config
from ohmyllm.config.models import GenerationSettings


def _lm_body(effort: str | None = None, ultrathink: bool = False) -> dict:
    cfg = Config()
    gen = GenerationSettings()
    if effort is not None:
        gen.effort = effort
    gen.ultrathink = ultrathink
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    be = LMStudioBackend(cfg, transport=httpx.MockTransport(handler))

    async def go() -> None:
        async for _ in be.stream_chat("m", [{"role": "user", "content": "x"}], gen):
            pass

    asyncio.run(go())
    return cap["body"]


def test_ladder_maps_to_wire_levels():
    assert "reasoning_effort" not in _lm_body("off")
    assert _lm_body("low")["reasoning_effort"] == "low"
    assert _lm_body("medium")["reasoning_effort"] == "medium"
    assert _lm_body("high")["reasoning_effort"] == "high"
    assert _lm_body("xhigh")["reasoning_effort"] == "xhigh"
    assert _lm_body("max")["reasoning_effort"] == "xhigh", "max clamps to the server ceiling"


def test_ultrathink_rides_the_ceiling_always():
    body = _lm_body("low", ultrathink=True)
    assert body["reasoning_effort"] == "xhigh", "ultrathink ignores a low /think level"


def test_ollama_think_levels():
    cfg = Config()

    def run(effort: str):
        gen = GenerationSettings(effort=effort)
        cap: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            cap["b"] = json.loads(request.content.decode())
            return httpx.Response(200, text=json.dumps(
                {"message": {}, "done": True, "done_reason": "stop", "eval_count": 1}))

        be = OllamaBackend(cfg, transport=httpx.MockTransport(handler))

        async def go() -> None:
            async for _ in be.stream_chat("q", [{"role": "user", "content": "x"}], gen):
                pass

        asyncio.run(go())
        return cap["b"]

    assert "thinking" not in run("off")
    assert run("low")["thinking"] == "low"
    assert run("high")["thinking"] == "high"
    assert run("xhigh")["thinking"] is True
    assert run("max")["thinking"] is True


def test_mock_ladder_strictly_increases_and_ultrathink_tops():
    cfg = Config()
    counts = []
    for lvl in ["low", "medium", "high", "xhigh", "max"]:
        gen = GenerationSettings(effort=lvl)
        be = MockBackend(cfg)

        async def go(g=gen, b=be) -> int:
            tot = 0
            async for ch in b.stream_chat("m", [{"role": "user", "content": "why?"}], g):
                tot += len(ch.reasoning or "")
            return tot

        counts.append(asyncio.run(go()))
    assert all(a < b for a, b in zip(counts, counts[1:], strict=False)), counts
    gen = GenerationSettings(ultrathink=True)
    be = MockBackend(cfg)

    async def ultra() -> int:
        tot = 0
        async for ch in be.stream_chat("m", [{"role": "user", "content": "why?"}], gen):
            tot += len(ch.reasoning or "")
        return tot

    assert asyncio.run(ultra()) > counts[-1], "ultrathink must out-think max"


def test_legacy_thinking_true_migrates_to_medium():
    gen = GenerationSettings.model_validate({"thinking": True})
    assert gen.effort == "medium"
    gen2 = GenerationSettings.model_validate({"thinking": True, "effort": "off"})
    assert gen2.effort == "off", "explicit effort beats the legacy flag"


async def test_skill_cast_and_menu_and_folder(oml_env, svc, monkeypatch):
    from ohmyllm import paths

    d = paths.skills_dir() / "pdf-tools"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: pdf-tools\ndescription: Handle PDF files like a pro\n---\n"
        "When working with PDFs, always extract text first and verify page count.\n",
        encoding="utf-8")

    opened: list[str] = []
    import subprocess

    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kw: opened.append(str(args[-1])) or type("P", (), {"pid": 1})())

    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        # autocomplete menu surfaces the skill
        inp.value = "/skill:"
        await pilot.pause(0.2)
        menu = app.screen.query_one("#slash-menu")
        ids = [str(menu.get_option_at_index(i).id) for i in range(menu.option_count)]
        assert "/skill:pdf-tools" in ids, ids
        # cast it
        inp.value = "/skill:pdf-tools"
        await pilot.press("enter")
        await pilot.pause(0.5)
        msgs = app.session.messages
        assert any("pdf-tools" in str(m.get("content")) and "extract text first" in str(m.get("content"))
                   for m in msgs), "skill body must land in the session"
        # unknown skill does not crash, just notifies
        inp.value = "/skill:nope-missing"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert not any("nope-missing" in str(m.get("content", "")) and "<skill" in str(m.get("content", ""))
                       for m in app.session.messages)
        # /skillimport opens the folder
        inp.value = "/skillimport"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert opened and "skills" in opened[-1], opened


def test_status_snapshot_carries_effort(svc):
    svc.config.generation.effort = "high"
    snap = svc.status_snapshot()
    assert snap["effort"] == "high"
    assert snap["ultrathink"] is False
