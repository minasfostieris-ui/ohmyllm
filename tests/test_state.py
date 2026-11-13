"""D4: machine state — lifecycle, approval id, injectable clock, CLI + bridge."""

from __future__ import annotations

import asyncio
import json
import stat
import time

from ohmyllm import paths
from ohmyllm.agents.runtime import AgentRuntime
from ohmyllm.backends.base import ChatChunk
from ohmyllm.config.models import PermissionMode
from ohmyllm.safety import ApprovalOutcome
from ohmyllm.secureio import atomic_write, canonical_json

CONTRACT_KEYS = {"state", "session_id", "turn", "backend", "model",
                 "pending_approval_id", "seq", "ts"}
# Task #14 F6: the bridge /api/state snapshot carries one ADDITIVE key on top
# of the durable contract — "stale" (crash detection, same 300s CLI threshold).
BRIDGE_CONTRACT_KEYS = CONTRACT_KEYS | {"stale"}


def _rt(svc, **kw):
    return AgentRuntime(backend=svc.backend(), registry=svc.registry, policy=svc.policy,
                        approvals=svc.approvals, audit=svc.audit, config=svc.config,
                        soul=svc.soul, skills=svc.skills, **kw)


def _shell_script():
    return [
        [ChatChunk(content="", tool_calls=[{"id": "c1", "type": "function",
                   "function": {"name": "run_command", "arguments": {"command": "echo hi"}}}],
                   done=True, finish="tool_calls", usage={})],
        [ChatChunk(content="ok", done=True, finish="stop", usage={})],
    ]


def test_lifecycle_schema_and_permissions(svc, tmp_path):
    ticks = iter([1000.0 + i for i in range(50)])
    sp = tmp_path / "state.json"
    rt = _rt(svc, clock=lambda: next(ticks), state_path=sp)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "hello there"))
    assert res.error is None
    data = json.loads(sp.read_text())
    assert set(data) == CONTRACT_KEYS
    assert data["state"] == "done"
    assert data["session_id"] == sess.id
    assert data["turn"] == 1
    assert data["backend"] == "mock"
    assert data["pending_approval_id"] is None
    assert data["seq"] >= 2
    assert data["ts"] >= 1000.0  # injectable clock honored
    assert stat.S_IMODE(sp.stat().st_mode) == 0o600
    assert rt.turn_active is False
    assert rt.machine_state()["state"] == "done"


def test_state_never_contains_message_content(svc, tmp_path):
    sp = tmp_path / "state.json"
    rt = _rt(svc, state_path=sp)
    sess = svc.new_session()
    asyncio.run(rt.run_turn(sess, "my secret password is HUNTER2-SECRET"))
    blob = sp.read_text()
    assert "HUNTER2-SECRET" not in blob
    assert "password" not in blob


def test_blocked_on_approval_includes_id(svc, tmp_path):
    svc.config.permission_mode = PermissionMode.SANDBOXED
    svc.config.agent.native_tools = "force"
    sp = tmp_path / "state.json"
    rt = _rt(svc, state_path=sp)
    svc.backend().script = _shell_script()
    seen: dict = {}

    async def prompt(req):
        seen["live"] = rt.machine_state()
        seen["disk"] = json.loads(sp.read_text())
        seen["tool"] = req.tool
        return ApprovalOutcome.DENIED

    sess = svc.new_session()
    asyncio.run(rt.run_turn(sess, "run echo", prompt_fn=prompt))
    assert seen["tool"] == "run_command"
    for snap in (seen["live"], seen["disk"]):
        assert snap["state"] == "blocked"
        assert isinstance(snap["pending_approval_id"], str) and snap["pending_approval_id"]
        assert snap["session_id"] == sess.id
    final = json.loads(sp.read_text())
    assert final["state"] == "done"
    assert final["pending_approval_id"] is None


def test_error_state_published(svc, tmp_path):
    sp = tmp_path / "state.json"
    rt = _rt(svc, state_path=sp)
    be = svc.backend()

    def broken(*a, **k):
        raise RuntimeError("synthetic boom")

    be.stream_chat = broken
    sess = svc.new_session()
    try:
        asyncio.run(rt.run_turn(sess, "hello"))
    except RuntimeError:
        pass
    assert json.loads(sp.read_text())["state"] == "error"
    assert rt.turn_active is False


# --------------------------------------------------------------------- CLI
def _invoke(args, **kw):
    from typer.testing import CliRunner

    from ohmyllm.cli import app

    return CliRunner().invoke(app, args, **kw)


def _stderr_text(r):
    try:
        return r.stderr
    except (AttributeError, ValueError):
        return r.output


def test_cli_state_missing_is_idle(oml_env):
    r = _invoke(["state", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert set(data) == CONTRACT_KEYS
    assert data["state"] == "idle" and data["seq"] == 0 and data["session_id"] is None


def test_cli_state_after_run(oml_env):
    _invoke(["init", "--backend", "mock"])
    r0 = _invoke(["run", "hello state"])
    assert r0.exit_code == 0, r0.output
    data = json.loads(_invoke(["state", "--json"]).output)
    assert data["state"] == "done" and data["backend"] == "mock"


def test_cli_state_stale_warning(oml_env):
    snap = {"state": "working", "session_id": "abc", "turn": 1, "backend": "mock",
            "model": "m", "pending_approval_id": None, "seq": 3, "ts": time.time() - 4000}
    atomic_write(paths.global_home() / "state.json", canonical_json(snap) + "\n", mode=0o600)
    r = _invoke(["state"])
    assert r.exit_code == 0
    assert "stale" in _stderr_text(r)


# ------------------------------------------------------------------ bridge
def test_bridge_state_contract_and_auth(bridge):
    out = bridge.get("/api/state")
    assert set(out) == BRIDGE_CONTRACT_KEYS
    assert out["state"] in {"idle", "working", "blocked", "done", "error"}
    assert out["stale"] is False  # no durable snapshot yet ⇒ idle defaults
    assert bridge.get("/api/state", auth=False)["__status__"] == 401


def test_bridge_state_reads_durable_file(bridge, oml_env):
    snap = {"state": "done", "session_id": "s1", "turn": 2, "backend": "mock",
            "model": "mock-tool-7b", "pending_approval_id": None, "seq": 7, "ts": time.time()}
    atomic_write(paths.global_home() / "state.json", canonical_json(snap) + "\n", mode=0o600)
    assert bridge.get("/api/state") == {**snap, "stale": False}


def test_bridge_state_flags_stale_working_snapshot(bridge, oml_env):
    """Task #14 F6: a crashed agent's old 'working' snapshot must not read as
    live progress — same 300s threshold the CLI state command warns on."""
    snap = {"state": "working", "session_id": "s1", "turn": 2, "backend": "mock",
            "model": "m", "pending_approval_id": None, "seq": 7, "ts": time.time() - 4000}
    atomic_write(paths.global_home() / "state.json", canonical_json(snap) + "\n", mode=0o600)
    out = bridge.get("/api/state")
    assert out["stale"] is True
    assert out["state"] == "working"  # snapshot itself is preserved verbatim
    fresh = {**snap, "state": "blocked", "ts": time.time()}
    atomic_write(paths.global_home() / "state.json", canonical_json(fresh) + "\n", mode=0o600)
    assert bridge.get("/api/state")["stale"] is False
    done_old = {**snap, "state": "done"}
    atomic_write(paths.global_home() / "state.json", canonical_json(done_old) + "\n", mode=0o600)
    assert bridge.get("/api/state")["stale"] is False  # only working/blocked go stale
