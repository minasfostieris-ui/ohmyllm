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


DRAFT = ("The answer requires careful analysis of the farm situation. " * 3)
REFINED = "REFINED: 34 legs. Two derivations agree; the draft miscounted the goats."


def _rt_with_script(svc, chunks_script):
    from ohmyllm.agents.runtime import AgentRuntime
    from ohmyllm.backends.base import ChatChunk

    be = svc.backend()
    be.script = [[ChatChunk(content=c, done=True)] for c in chunks_script]
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul)
    return rt, be


def test_ultrathink_refines_the_draft(oml_env, svc):
    svc.config.generation.ultrathink = True
    rt, be = _rt_with_script(svc, [DRAFT, REFINED])
    sess = svc.new_session()
    events: list[dict] = []
    res = asyncio.run(rt.run_turn(sess, "farm riddle?", model="m", on_event=events.append))
    assert any(e.get("type") == "ultrathink_review" for e in events), "review event missing"
    last = [m for m in sess.messages if m.get("role") == "assistant"][-1]
    assert last["content"] == REFINED, "session must store the refined answer, not the draft"
    assert not any(m.get("role") == "assistant" and m.get("content") == DRAFT for m in sess.messages)
    assert res.text == REFINED
    # the review call carried the draft + the attack instruction on the wire
    assert not be.script, "second pass must have run"


def test_ultrathink_skips_refine_for_short_answers(oml_env, svc):
    svc.config.generation.ultrathink = True
    rt, be = _rt_with_script(svc, ["4.", "DECOY-MUST-NOT-RUN"])
    sess = svc.new_session()
    events: list[dict] = []
    asyncio.run(rt.run_turn(sess, "2+2?", model="m", on_event=events.append))
    assert not any(e.get("type") == "ultrathink_review" for e in events)
    assert len(be.script) == 1, "tiny answers must not trigger the review pass"


def test_no_refine_without_ultrathink(oml_env, svc):
    svc.config.generation.ultrathink = False
    rt, be = _rt_with_script(svc, [DRAFT])
    sess = svc.new_session()
    events: list[dict] = []
    asyncio.run(rt.run_turn(sess, "farm riddle?", model="m", on_event=events.append))
    assert not any(e.get("type") == "ultrathink_review" for e in events)
    last = [m for m in sess.messages if m.get("role") == "assistant"][-1]
    assert last["content"].startswith("The answer requires")


def test_refine_failure_keeps_draft(oml_env, svc):
    from ohmyllm.agents.runtime import AgentRuntime
    from ohmyllm.backends.base import ChatChunk

    svc.config.generation.ultrathink = True

    class Boom(svc.backend().__class__):
        async def _stream(self, model, messages, tools, native, params=None):
            if self._calls == 0:
                self._calls += 1
                yield ChatChunk(content=DRAFT, done=True)
            raise RuntimeError("backend exploded mid-review")
        def __init__(self, config, transport=None):
            super().__init__(config, transport)
            self._calls = 0

    be = Boom(svc.config)
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "farm riddle?", model="m"))
    assert DRAFT.strip() in res.text, "a failed review must never eat the draft"
