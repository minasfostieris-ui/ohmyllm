"""/ultrathink: directive in the prompt, reasoning on the stream, TUI toggle."""
from __future__ import annotations

import asyncio

from ohmyllm.agents.prompts import ULTRATHINK, PromptBuilder
from ohmyllm.backends.mock import MockBackend
from ohmyllm.config import Config, PermissionMode
from ohmyllm.config.models import GenerationSettings


def test_directive_reaches_system_prompt_only_when_on(oml_env, svc):
    pb = PromptBuilder(Config(), svc.registry, workspace="/tmp/ws")
    off = pb.system_prompt(mode=PermissionMode.SAFE, native=True)
    on = pb.system_prompt(mode=PermissionMode.SAFE, native=True, ultrathink=True)
    assert ULTRATHINK not in off
    assert "ULTRATHINK mode (active)" in on


def test_mock_streams_deep_reasoning_when_ultrathink():
    cfg = Config()
    gen = GenerationSettings(ultrathink=True)
    be = MockBackend(cfg)

    async def collect():
        reasoning = []
        async for ch in be.stream_chat("m", [{"role": "user", "content": "why?"}], gen):
            reasoning.append(ch.reasoning)
        return "".join(reasoning)

    text = asyncio.run(collect())
    assert "Interpretation A" in text and len(text) > 200, "ultrathink mock must emit real reasoning"
    assert asyncio.run(collect())  # idempotent across turns


def test_mock_stays_quiet_without_ultrathink():
    cfg = Config()
    gen = GenerationSettings()
    be = MockBackend(cfg)

    async def collect():
        out = []
        async for ch in be.stream_chat("m", [{"role": "user", "content": "why?"}], gen):
            out.append(ch.reasoning)
        return "".join(out)

    assert asyncio.run(collect()) == ""


async def test_tui_ultrathink_command_toggles_and_persists(oml_env, svc):
    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/ultrathink"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.1)
            if app.svc.config.generation.ultrathink:
                break
        assert app.svc.config.generation.ultrathink is True
        assert app.svc.config.generation.thinking is True, "ultrathink implies /think"
        # persisted: a fresh load of the config file sees it
        from ohmyllm import paths
        from ohmyllm.config import ConfigStore

        assert ConfigStore(paths.config_path()).load().generation.ultrathink is True
        # toggle off
        inp.value = "/ultrathink off"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.1)
            if not app.svc.config.generation.ultrathink:
                break
        assert app.svc.config.generation.ultrathink is False


def test_ultrathink_note_lands_on_the_wire_copy_only():
    from ohmyllm.agents.prompts import ULTRATHINK_NOTE
    from ohmyllm.agents.runtime import AgentRuntime
    from ohmyllm.config.models import GenerationSettings

    stored = {"role": "user", "content": "what is 2+2?"}
    wire = [dict(stored)]
    AgentRuntime._ultrathink_note(wire, GenerationSettings(ultrathink=True))
    assert ULTRATHINK_NOTE in wire[0]["content"]
    assert ULTRATHINK_NOTE not in stored["content"], "stored session must stay clean"
    AgentRuntime._ultrathink_note(wire, GenerationSettings(ultrathink=False))
    assert wire[0]["content"].count(ULTRATHINK_NOTE) == 1, "no double-append when off"
