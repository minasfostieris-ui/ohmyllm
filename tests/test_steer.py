"""D6: steer queue — injection at tool boundaries, inert text, caps, bridge, TUI."""

from __future__ import annotations

import asyncio

import pytest

from ohmyllm.agents.runtime import AgentRuntime
from ohmyllm.backends.base import ChatChunk
from ohmyllm.backends.mock import MockBackend
from ohmyllm.config.models import PermissionMode
from ohmyllm.safety import ApprovalOutcome


class RecordingBackend(MockBackend):
    """Mock backend that records every message list it is asked to complete."""

    def __init__(self, config):
        super().__init__(config)
        self.seen: list[list[dict]] = []

    def stream_chat(self, model, messages, params, *, tools=None, native_tools=False):
        self.seen.append([dict(m) for m in messages])
        return super().stream_chat(model, messages, params, tools=tools, native_tools=native_tools)


def _read_call(i):
    return [ChatChunk(content="", tool_calls=[{"id": f"c{i}", "type": "function",
            "function": {"name": "read_file", "arguments": {"path": "a.txt"}}}],
            done=True, finish="tool_calls", usage={})]


@pytest.fixture()
def rig(svc):
    svc.config.permission_mode = PermissionMode.SANDBOXED
    svc.config.agent.native_tools = "force"
    (svc.workspace / "a.txt").write_text("file body")
    be = RecordingBackend(svc.config)
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills)
    return svc, be, rt


def test_steer_injected_at_tool_boundary(rig):
    svc, be, rt = rig
    be.script = [_read_call(1), _read_call(2),
                 [ChatChunk(content="all done", done=True, finish="stop", usage={})]]
    events: list[dict] = []
    boundaries: list[dict] = []

    def on_event(ev):
        events.append(ev)
        if ev.get("type") == "tool_result":
            boundaries.append(ev)
            if len(boundaries) == 1:
                assert rt.steer("STEER-ONE") == 1

    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "read the file twice", on_event=on_event))
    assert res.error is None
    assert len(boundaries) == 2
    roles = [(m.get("role"), str(m.get("content", ""))) for m in sess.messages]
    idx = next(i for i, (r, c) in enumerate(roles) if r == "user" and c == "STEER-ONE")
    assert any(r in ("tool", "function") for r, _ in roles[:idx]), roles
    # the NEXT model call saw the steer; the first one could not have
    assert any(m.get("role") == "user" and m.get("content") == "STEER-ONE" for m in be.seen[1])
    assert not any(m.get("content") == "STEER-ONE" for m in be.seen[0])
    assert {"type": "steer", "text": "STEER-ONE"} in events


def test_steer_cannot_dismiss_approval_or_change_mode(rig):
    svc, be, rt = rig
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "c1", "type": "function",
                   "function": {"name": "run_command", "arguments": {"command": "echo hi"}}}],
                   done=True, finish="tool_calls", usage={})],
        [ChatChunk(content="done", done=True, finish="stop", usage={})],
    ]
    asked: list[str] = []

    async def prompt(req):
        asked.append(req.tool)
        rt.steer("/yolo")
        rt.steer("/mode full_access")
        return ApprovalOutcome.DENIED

    sess = svc.new_session()
    asyncio.run(rt.run_turn(sess, "run echo", prompt_fn=prompt))
    assert asked == ["run_command"]  # the approval was still demanded
    assert svc.config.agent.yolo is False
    assert svc.config.permission_mode is PermissionMode.SANDBOXED
    # slash-looking steer text is recorded as inert literal user content
    assert any(m.get("role") == "user" and m.get("content") == "/yolo" for m in sess.messages)


def test_steer_queue_caps_and_validation(rig):
    svc, be, rt = rig
    be.script = [_read_call(1), [ChatChunk(content="ok", done=True, finish="stop", usage={})]]
    checked: list[bool] = []

    def on_event(ev):
        if ev.get("type") == "tool_result" and not checked:
            for i in range(8):
                assert rt.steer(f"s{i}") == i + 1
            with pytest.raises(ValueError):
                rt.steer("overflow")
            with pytest.raises(ValueError):
                rt.steer("   ")
            with pytest.raises(ValueError):
                rt.steer("x" * 65537)
            checked.append(True)

    sess = svc.new_session()
    asyncio.run(rt.run_turn(sess, "read", on_event=on_event))
    assert checked
    injected = [m for m in sess.messages
                if m.get("role") == "user" and str(m.get("content", "")).startswith("s")]
    assert len(injected) == 8


def test_steer_outside_turn_refused(rig):
    _svc, _be, rt = rig
    assert rt.turn_active is False
    with pytest.raises(RuntimeError):
        rt.steer("hello")


def test_retired_runtime_refuses_steer(rig):
    _svc, _be, rt = rig
    rt.retire()
    with pytest.raises(RuntimeError):
        rt.steer("x")


# ------------------------------------------------------------------ bridge
def test_bridge_steer_no_active_turn(bridge):
    out = bridge.post("/api/steer", {"text": "hi"})
    assert out["__status__"] == 409
    assert "no active turn" in out["error"]
    assert bridge.post("/api/steer", {"text": "hi"}, auth=False)["__status__"] == 401
    assert bridge.post("/api/steer", {"text": "x" * 65537})["__status__"] == 400


# --------------------------------------------------------------------- TUI
@pytest.mark.asyncio()
async def test_tui_steer_idle_notice(oml_env, svc):
    from ohmyllm.tui.app import OhMyLLMApp

    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/steer focus on tests"
        await pilot.press("enter")
        await pilot.pause(0.4)
        texts = " ".join(str(w.render()) for w in app.screen.walk_children())
        assert "no turn is running" in texts
        assert app.active_runtime is None
