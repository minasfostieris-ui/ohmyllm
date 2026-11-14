"""R4 correctness regressions — Task #81 (reproduce-first).

Confirmed findings from .validation/lucas-r4cor-report.md:
  R4-1 (High) partial config saves rebuild Config from model_dump_json(),
       which drops the excluded runtime flag agent.yolo → silent disarm +
       spurious security retire. Three siblings: POST /api/config/agent,
       POST /api/config/memory, PATCH /api/config.
  R4-2 (Med)  a cancelled/errored turn skips _finish → yolo stays armed →
       the NEXT prompt bypasses approvals.
  R4-3 (Med)  voice stop's 3s cap reports "not recording" while a slow
       recorder later starts and keeps the mic live.
  R4-5 (Med)  a mid-turn skill cast returns 200 but the turn's final save
       clobbers the cast message.
  R4-07       plain /yolo with an invalid argument silently arms (must
       REJECT with a notice; GUI already correct, TUI leg owned by Jimmy).

Every test here FAILS on pristine HEAD 87037b0 and passes after the fix.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time

import pytest

from ohmyllm.agents.runtime import AgentCancelled
from ohmyllm.backends.mock import MockBackend


# ------------------------------------------------------------------ helpers
def _sse(resp, until: str = "done", hook=None, limit: int = 400) -> list[tuple[str, dict]]:
    """Minimal SSE reader (same framing as test_bridge.sse_events)."""
    events: list[tuple[str, dict]] = []
    buf = ""
    raw = resp.read1(65536)
    while raw:
        buf += raw.decode(errors="replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            ev, dat = "", "{}"
            for line in frame.splitlines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    dat = line[6:]
            if not ev:
                continue
            data = json.loads(dat)
            events.append((ev, data))
            if hook:
                hook(ev, data)
            if ev == until:
                resp.close()
                return events
        if len(events) > limit:
            resp.close()
            return events
        raw = resp.read1(65536)
    resp.close()
    return events


def _arm(bridge) -> None:
    r = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
    assert r.get("ok") is True, r
    assert bridge.get("/api/status")["yolo"] is True


def _gen(bridge) -> int:
    return bridge.get("/api/config")["policy_generation"]


def _wait_idle(bridge, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and bridge.get("/api/status")["busy"]:
        time.sleep(0.02)
    assert bridge.get("/api/status")["busy"] is False


# ------------------------------------------------------- R4-1: three siblings
def test_r4_1_agent_partial_save_preserves_yolo(bridge):
    _arm(bridge)
    gen0 = _gen(bridge)
    r = bridge.post("/api/config/agent", {"max_steps": 9})
    assert r.get("ok") is True, r
    # pre-fix: the dump-rebuild drops agent.yolo → silent disarm
    assert bridge.get("/api/status")["yolo"] is True
    # ... and the drop is a security delta → spurious retire / gen bump
    assert _gen(bridge) == gen0
    # explicit yolo edits through the dedicated control must still work
    r = bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
    assert r.get("ok") is True, r
    assert bridge.get("/api/status")["yolo"] is False


def test_r4_1_memory_partial_save_preserves_yolo(bridge):
    _arm(bridge)
    gen0 = _gen(bridge)
    cur = bridge.get("/api/config")["memory"]["enabled"]
    r = bridge.post("/api/config/memory", {"enabled": cur})  # no-op save
    assert r.get("ok") is True, r
    assert bridge.get("/api/status")["yolo"] is True
    assert _gen(bridge) == gen0  # no-op must not retire anything
    # a REAL memory change is a legitimate security delta — yolo still rides along
    r = bridge.post("/api/config/memory", {"enabled": not cur})
    assert r.get("ok") is True, r
    assert bridge.get("/api/status")["yolo"] is True
    assert _gen(bridge) > gen0


def test_r4_1_patch_preserves_yolo(bridge):
    _arm(bridge)
    gen0 = _gen(bridge)
    r = bridge.patch("/api/config", {"ui": {"theme": "mono-amber"}})
    assert r.get("ok") is True, r
    assert bridge.get("/api/status")["yolo"] is True
    assert _gen(bridge) == gen0
    assert bridge.get("/api/config")["ui"]["theme"] == "mono-amber"


def test_r4_1_partial_saves_do_not_cancel_live_turn(bridge, monkeypatch):
    """The HIGH impact: a cosmetic save mid-turn must not retire the live
    runtime and cancel the user's turn (pre-fix: yolo drop → security delta)."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    bridge.post("/api/mode", {"mode": "sandboxed"})
    assert bridge.get("/api/status")["mode"] == "sandboxed"
    _arm(bridge)
    gen0 = _gen(bridge)
    seen: dict = {}

    def hook(ev, data):
        # hook the FIRST token of step 1 so a spurious retire lands before
        # the per-call _check_active → pre-fix the turn is cancelled
        if ev == "token" and "saved" not in seen:
            seen["saved"] = True
            seen["r1"] = bridge.post("/api/config/agent", {"max_steps": 8})
            seen["r2"] = bridge.patch("/api/config", {"ui": {"theme": "mono-amber"}})
            st = bridge.get("/api/status")
            seen["busy"], seen["yolo"] = st["busy"], st["yolo"]
            seen["gen"] = _gen(bridge)

    resp = bridge.req("POST", "/api/message",
                      {"text": 'create file r4live.txt with content "keep"'})
    events = _sse(resp, hook=hook)
    done = [d for e, d in events if e == "done"]
    assert done, events
    assert seen.get("saved"), "no token event to hook mid-turn"
    assert seen["r1"].get("ok") is True and seen["r2"].get("ok") is True
    assert seen["busy"] is True and seen["yolo"] is True
    assert seen["gen"] == gen0
    assert done[-1].get("cancelled") is not True  # pre-fix: spurious retire cancels
    _wait_idle(bridge)
    # the turn completed normally → the one-prompt flag is consumed
    assert bridge.get("/api/status")["yolo"] is False


# ------------------------------------------------- R4-2: disarm on every exit
async def test_r4_2_cancel_disarms_yolo(svc):
    rt = svc.runtime()
    sess = svc.new_session()
    svc.config.agent.yolo = True
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(AgentCancelled):
        await rt.run_turn(sess, "hi", cancel=cancel)
    # pre-fix: cancel skips _finish → the NEXT prompt would bypass approvals
    assert svc.config.agent.yolo is False


async def test_r4_2_error_disarms_yolo(svc, monkeypatch):
    rt = svc.runtime()
    sess = svc.new_session()
    svc.config.agent.yolo = True

    async def boom(*a, **k):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(rt, "_run_turn", boom)
    with pytest.raises(RuntimeError):
        await rt.run_turn(sess, "hi")
    assert svc.config.agent.yolo is False


def test_r4_2_bridge_cancel_disarms_yolo(bridge, monkeypatch):
    monkeypatch.setattr(MockBackend, "delay", 0.2)
    _arm(bridge)
    fired: list = []

    def hook(ev, data):
        if ev == "token" and not fired:
            fired.append(True)
            assert bridge.post("/api/cancel").get("ok") is True

    resp = bridge.req("POST", "/api/message", {"text": "hello"})
    events = _sse(resp, hook=hook)
    done = [d for e, d in events if e == "done"]
    assert fired and done and done[-1].get("cancelled") is True
    _wait_idle(bridge)
    assert bridge.get("/api/status")["yolo"] is False  # pre-fix: stays armed


# --------------------------------------------- R4-3: voice stop/start race
def test_r4_3_voice_stop_start_race_no_live_mic(bridge, monkeypatch, tmp_path):
    import ohmyllm.voice as V

    class SlowRec:
        instances: list = []

        def __init__(self, out):
            self.out = out
            self.running = False
            SlowRec.instances.append(self)

        def start(self):
            time.sleep(4.5)  # lands well AFTER the stop path's 3s cap
            self.running = True
            return None

        def stop(self):
            self.running = False

    monkeypatch.setattr(V, "Recorder", SlowRec)
    monkeypatch.setattr(V, "voice_dir", lambda: tmp_path)
    results: list = []

    def start():
        results.append(bridge.post("/api/voice/record", {"action": "start"}))

    th = threading.Thread(target=start)
    th.start()
    time.sleep(0.4)  # let the start claim talk_pending
    stop = bridge.post("/api/voice/record", {"action": "stop"})
    assert stop.get("note") == "not recording"  # cap elapsed, start still spawning
    th.join(20)
    assert not th.is_alive()
    assert len(SlowRec.instances) == 1
    assert results[0].get("ok") is True
    # pre-fix: the late start publishes talk and keeps the mic live
    assert SlowRec.instances[0].running is False
    stop2 = bridge.post("/api/voice/record", {"action": "stop"})
    assert stop2.get("note") == "not recording"  # pre-fix: talk exists → "too short"


# ------------------------------------------- R4-5: cast survives final save
def test_r4_5_cast_mid_turn_survives_final_save(bridge, oml_env, monkeypatch):
    skill = oml_env["home"] / "skills" / "r4skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: r4skill\ndescription: regression skill\n---\nDo the thing.\n")
    monkeypatch.setattr(MockBackend, "delay", 0.12)
    cast: dict = {}

    def hook(ev, data):
        if ev == "token" and "r" not in cast:
            sid = bridge.get("/api/status")["session"]
            cast["sid"] = sid
            cast["r"] = bridge.post("/api/skills/cast", {"name": "r4skill", "session": sid})

    resp = bridge.req("POST", "/api/message", {"text": "hello"})
    events = _sse(resp, hook=hook)
    assert [d for e, d in events if e == "done"], events
    assert cast.get("r", {}).get("ok") is True, cast
    _wait_idle(bridge)
    sess = bridge.get(f"/api/chats/{cast['sid']}")
    msgs = sess.get("messages") or []
    # pre-fix: the turn's finally-save writes its own stale copy → cast lost
    assert any('<skill name="r4skill">' in str(m.get("content", "")) for m in msgs), \
        "mid-turn cast was clobbered by the turn's final save"


# ----------------------------------------- R4-07: plain /yolo invalid argument
async def test_r4_07_plain_yolo_invalid_arg_rejects(svc):
    from rich.console import Console

    from ohmyllm.plain import PlainApp

    app = PlainApp(svc)
    buf = io.StringIO()
    app.console = Console(file=buf, force_terminal=False, width=200)

    assert await app._slash("/yolo banana") is False
    # pre-fix: an invalid argument falls through to the toggle → silently arms
    assert svc.config.agent.yolo is False
    assert "on or off" in buf.getvalue()

    await app._slash("/yolo on")
    assert svc.config.agent.yolo is True

    # an invalid argument while armed must not change state either
    buf.truncate(0)
    buf.seek(0)
    await app._slash("/yolo nonsense")
    assert svc.config.agent.yolo is True
    assert "on or off" in buf.getvalue()

    await app._slash("/yolo off")
    assert svc.config.agent.yolo is False
    await app._slash("/yolo")  # empty argument still toggles
    assert svc.config.agent.yolo is True
