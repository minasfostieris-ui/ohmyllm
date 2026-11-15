"""R5 correctness regressions — Task #91 (reproduce-first).

Confirmed findings from .validation/nick-r5cor-report.md (HEAD fe4475e):
  F1 (High) mid-turn /yolo arm broken on BOTH legs:
     GUI — apply_config retires the running turn; the retired turn's R4-2
       finally-disarm then eats the fresh arm (lost turn + UI lie).
     TUI — direct config mutation leaks the bypass into the CURRENT turn
       (2nd write auto-approved), then the finally-disarm loses the arm.
     CONTRACT: arming/disarming mid-turn must NOT cancel/retire the turn,
     must NOT apply to the current turn (next-prompt), must persist and
     show truthfully in the UI, and the finally-disarm must not kill a
     deferred fresh arm. Fix: defer yolo deltas while busy (Services-owned
     pending), settle at turn end AFTER the finally-disarm, no retire.
  F2 (Med)  _probe_models httpx.get lacks trust_env=False → the key-bearing
     probe inherits env proxies (every other key client forces it off).
  F3 (Med)  lmstudio stream_chat returns on the finish chunk, dropping the
     include_usage trailer in OpenAI spec order (finish → trailer → [DONE])
     → usage=None on every spec-compliant turn.
  F4 (Med)  voice: a 2nd start during a timed-out pending start is told
     already=True but does not clear voice_stop_pending → the late start
     tears the recorder down while the UI believes it is recording.
  L1 (Low)  cast unknown-session returns 400; /api/message returns 404.

Every F1/F2/F3/F4/L1 defect test FAILS on pristine fe4475e; the two control
tests (plain parity, usage-on-finish) pin behavior that must NOT change.
Guard-compatible: no subprocess, loopback bridges on ephemeral ports,
Textual pilots only, fake transports/recorders.
"""

from __future__ import annotations

import io
import json
import threading
import time

import httpx

from ohmyllm.backends.base import ChatChunk
from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.config import Config
from ohmyllm.config.models import GenerationSettings
from ohmyllm.safety import ApprovalOutcome
from ohmyllm.tui.app import OhMyLLMApp


# ------------------------------------------------------------------ helpers
def _sse(resp, until: str = "done", hook=None, limit: int = 400) -> list[tuple[str, dict]]:
    """Minimal SSE reader (same framing as test_r4cor_regressions)."""
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


def _write_call(i: int, path: str) -> dict:
    return {"id": f"call_w{i}", "type": "function",
            "function": {"name": "write_file", "arguments": {"path": path, "content": "r5"}}}


async def _tui_wait_idle(app, pilot, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while app.busy and time.monotonic() < deadline:
        await pilot.pause(0.1)
    assert not app.busy, "TUI turn never finished"


# ------------------------------------------- F1: Services defer mechanism
def test_r5_f1_services_deferred_yolo_contract(svc):
    """The shared choke point: busy deltas are deferred, never live; the
    effective (next-prompt) state is what the UI must show; settle applies
    the pending value AFTER the one-prompt disarm and is idempotent."""
    assert svc.effective_yolo() is False
    assert svc.request_yolo(True, busy=True) is True  # deferred
    assert svc.config.agent.yolo is False  # running turn's policy untouched
    assert svc.effective_yolo() is True
    assert svc.status_snapshot()["yolo"] is True  # truthful UI
    svc.settle_yolo()  # turn end (runs after the runtime finally-disarm)
    assert svc.config.agent.yolo is True
    svc.settle_yolo()  # idempotent
    assert svc.config.agent.yolo is True
    # deferred disarm is symmetric
    assert svc.request_yolo(False, busy=True) is True
    assert svc.config.agent.yolo is True  # current turn keeps its entry arm
    assert svc.status_snapshot()["yolo"] is False
    svc.settle_yolo()
    assert svc.config.agent.yolo is False
    # idle requests apply immediately (no defer)
    assert svc.request_yolo(True, busy=False) is False
    assert svc.config.agent.yolo is True
    assert svc.effective_yolo() is True


async def test_r5_f1_runtime_finally_disarm_spares_deferred_arm(svc):
    """R4-2 finally-disarm × deferred fresh arm: the disarm consumes the
    arm that STARTED the turn; the mid-turn deferred arm must survive."""
    rt = svc.runtime()
    sess = svc.new_session()
    svc.config.agent.yolo = True  # armed for THIS turn
    fired: list = []

    def on_event(ev):
        if ev.get("type") == "token" and not fired:
            fired.append(True)
            svc.request_yolo(True, busy=True)  # mid-turn re-arm → deferred

    await rt.run_turn(sess, "hello", on_event=on_event)
    assert fired, "no token event to hook mid-turn"
    assert svc.config.agent.yolo is False  # finally consumed the entry arm
    svc.settle_yolo()  # the leg's turn-end hook
    assert svc.config.agent.yolo is True  # deferred fresh arm survived


# ------------------------------------------------- F1: live GUI-leg probes
def test_r5_f1_gui_arm_midturn_keeps_turn_and_arm(bridge, oml_env, monkeypatch):
    """Nick P1: arm while an approval is pending. The turn must NOT be
    retired/cancelled, the UI must truthfully show armed, and the arm must
    persist for the NEXT prompt (which then auto-approves)."""
    monkeypatch.setattr(MockBackend, "delay", 0.05)
    gen0 = _gen(bridge)
    seen: dict = {}

    def hook(ev, data):
        if ev == "approval_request" and "arm" not in seen:
            seen["arm"] = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
            st = bridge.get("/api/status")
            seen["yolo_ui"], seen["busy"] = st["yolo"], st["busy"]
            seen["gen"] = _gen(bridge)
            seen["approve"] = bridge.post(f"/api/approval/{data['id']}", {"decision": "approve"})

    resp = bridge.req("POST", "/api/message",
                      {"text": 'create file r5gui.txt with content "keep"'})
    events = _sse(resp, hook=hook)
    done = [d for e, d in events if e == "done"]
    assert seen.get("arm"), "no approval_request to hook mid-turn"
    assert seen["arm"].get("ok") is True, seen["arm"]
    assert seen["yolo_ui"] is True  # truthful UI: armed for the next prompt
    assert seen["busy"] is True
    assert seen["gen"] == gen0  # pre-fix: security delta → retire + gen bump
    assert seen["approve"].get("ok") is True
    assert done, events
    assert done[-1].get("cancelled") is not True  # pre-fix: turn retired
    assert not done[-1].get("error"), done[-1]
    assert (oml_env["ws"] / "r5gui.txt").exists()
    _wait_idle(bridge)
    assert bridge.get("/api/status")["yolo"] is True  # pre-fix: finally ate it
    # NEXT prompt: the persisted arm auto-approves, then is consumed
    resp2 = bridge.req("POST", "/api/message",
                       {"text": 'create file r5gui2.txt with content "next"'})
    events2 = _sse(resp2)
    assert not [e for e, _d in events2 if e == "approval_request"], \
        "the deferred arm did not persist into the next prompt"
    done2 = [d for e, d in events2 if e == "done"]
    assert done2 and not done2[-1].get("error"), events2
    _wait_idle(bridge)
    assert bridge.get("/api/status")["yolo"] is False  # one-prompt contract


def test_r5_f1_gui_disarm_midturn_next_prompt(bridge, monkeypatch):
    """Disarm mid-turn is likewise next-prompt: no retire, the running turn
    completes, and the UI truthfully shows disarmed immediately."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    _arm(bridge)
    gen0 = _gen(bridge)
    seen: dict = {}

    def hook(ev, data):
        if ev == "token" and "off" not in seen:
            seen["off"] = bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
            st = bridge.get("/api/status")
            seen["yolo_ui"], seen["gen"] = st["yolo"], _gen(bridge)

    resp = bridge.req("POST", "/api/message", {"text": "hello"})
    events = _sse(resp, hook=hook)
    done = [d for e, d in events if e == "done"]
    assert seen.get("off", {}).get("ok") is True, seen
    assert seen["yolo_ui"] is False  # truthful: disarmed for the next prompt
    assert seen["gen"] == gen0  # pre-fix: retire + gen bump
    assert done and done[-1].get("cancelled") is not True  # pre-fix: cancelled
    assert not done[-1].get("error"), done[-1]
    _wait_idle(bridge)
    assert bridge.get("/api/status")["yolo"] is False


def test_r5_f1_gui_deferred_arm_survives_cancel(bridge, monkeypatch):
    """The finally-disarm must not kill a deferred fresh arm — even when
    the turn ends via user cancel (R4-2 disarm path)."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    seen: dict = {}

    def hook(ev, data):
        if ev == "token" and "arm" not in seen:
            seen["arm"] = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
            seen["cancel"] = bridge.post("/api/cancel")

    resp = bridge.req("POST", "/api/message", {"text": "hello"})
    events = _sse(resp, hook=hook)
    done = [d for e, d in events if e == "done"]
    assert seen.get("arm", {}).get("ok") is True
    assert seen.get("cancel", {}).get("ok") is True
    assert done and done[-1].get("cancelled") is True
    _wait_idle(bridge)
    # pre-fix: retire + finally-disarm → the fresh arm is silently lost
    assert bridge.get("/api/status")["yolo"] is True


# ------------------------------------------------- F1: live TUI-leg probes
async def test_r5_f1_tui_arm_midturn_next_prompt(oml_env, svc):
    """Nick P2: two write_file calls in ONE turn; /yolo on typed (live
    input+enter) while approving call 1. Contract: call 2 STILL prompts
    (no leak into the current turn), the arm persists for the next prompt,
    and the status bar truthfully shows armed while the turn runs."""
    app = OhMyLLMApp()
    prompts: list[str] = []
    mid: dict = {}
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        be = app.svc.backend()
        assert be.name.value == "mock"
        be.script = [
            [ChatChunk(content="", tool_calls=[_write_call(1, "r5tui_a.txt"),
                                               _write_call(2, "r5tui_b.txt")],
                       done=True, finish="tool_calls")],
            [ChatChunk(content="Done.", done=True, finish="stop")],
        ]

        async def probe_approval(req):
            prompts.append(req.tool)
            if len(prompts) == 1:
                # the user arms YOLO mid-turn, during the first approval
                app.screen.query_one("#chat-input").value = "/yolo on"
                await pilot.press("enter")
                await pilot.pause(0.3)
                mid["live"] = bool(app.svc.config.agent.yolo)
                mid["ui"] = bool(app.status_state()["yolo"])
            return ApprovalOutcome.APPROVED

        app.request_approval = probe_approval
        app.screen.query_one("#chat-input").value = "write the two files"
        await pilot.press("enter")
        await _tui_wait_idle(app, pilot)
        # pre-fix: only 1 prompt — the live mutation bypassed call 2
        assert prompts == ["write_file", "write_file"], prompts
        ws = app.svc.workspace
        assert (ws / "r5tui_a.txt").exists() and (ws / "r5tui_b.txt").exists()
        # pre-fix: the mutation leaked into the CURRENT turn
        assert mid.get("live") is False, "mid-turn arm leaked into the running turn"
        assert mid.get("ui") is True, "status bar must truthfully show the deferred arm"
        # pre-fix: the finally-disarm ate the arm
        assert app.svc.config.agent.yolo is True, "deferred arm lost at turn end"
        assert app.status_state()["yolo"] is True
        # NEXT prompt rides the persisted arm: no approval, then consumed
        app.screen.query_one("#chat-input").value = 'create file r5tui_c.txt with content "z"'
        await pilot.press("enter")
        await _tui_wait_idle(app, pilot)
        assert prompts == ["write_file", "write_file"], "next prompt must auto-approve"
        assert (ws / "r5tui_c.txt").exists()
        assert app.svc.config.agent.yolo is False  # one-prompt contract


async def test_r5_f1_tui_disarm_midturn_next_prompt(oml_env, svc, monkeypatch):
    """Symmetric contract: /yolo off mid-turn must not strip the bypass
    from the CURRENT turn (it keeps its entry policy) and must show
    disarmed in the status bar immediately; live config settles at end."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    app = OhMyLLMApp()
    mid: dict = {}
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        app.svc.config.agent.yolo = True  # armed BEFORE the turn
        orig = app.dispatch_turn_event

        def dispatch(ev):
            if ev.get("type") == "token" and "off" not in mid:
                mid["off"] = True
                app.screen._slash("/yolo off")  # the real TUI slash leg
                mid["live"] = bool(app.svc.config.agent.yolo)
                mid["ui"] = bool(app.status_state()["yolo"])
            return orig(ev)

        app.dispatch_turn_event = dispatch
        app.screen.query_one("#chat-input").value = "hello"
        await pilot.press("enter")
        await _tui_wait_idle(app, pilot)
        assert mid.get("off"), "no token event to hook mid-turn"
        # pre-fix: direct mutation disarmed the RUNNING turn
        assert mid["live"] is True, "mid-turn disarm leaked into the running turn"
        assert mid["ui"] is False, "status bar must truthfully show the deferred disarm"
        assert app.svc.config.agent.yolo is False  # settled at turn end


# --------------------------------------------- F1: plain-leg parity guard
async def test_r5_f1_plain_yolo_one_prompt_parity(svc):
    """Plain has no mid-turn input path (sequential REPL), so the contract
    holds structurally — pin the observable one-prompt behavior through
    the shared helper so the refactor cannot regress it."""
    from rich.console import Console

    from ohmyllm.plain import PlainApp

    app = PlainApp(svc)
    app.console = Console(file=io.StringIO(), force_terminal=False, width=200)
    prompts: list[str] = []

    async def rec_prompt(req):
        prompts.append(req.tool)
        return ApprovalOutcome.APPROVED

    app._prompt = rec_prompt
    await app._slash("/yolo on")
    assert svc.config.agent.yolo is True
    await app._turn('create file r5plain.txt with content "x"')
    assert prompts == []  # yolo bypassed the routine write
    assert svc.config.agent.yolo is False  # consumed by that prompt
    await app._turn('create file r5plain2.txt with content "y"')
    assert prompts == ["write_file"]  # the next prompt asks again


# ------------------------------------------------------- F2: probe trust_env
def test_r5_f2_probe_models_never_uses_env_proxy(bridge, monkeypatch):
    """The key-bearing /models probe must force trust_env=False (and
    follow_redirects=False) like every other key-bearing client."""
    cap: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "m1"}]}

    def fake_get(url, **kw):
        cap["url"], cap["kw"] = url, kw
        return FakeResp()

    monkeypatch.setattr(httpx, "get", fake_get)
    r = bridge.post("/api/providers/key",
                    {"pid": "openai", "key": "sk-r5-probe-test", "probe": True})
    assert r.get("models_ok") is True, r
    assert cap.get("url", "").endswith("/models")
    kw = cap["kw"]
    assert kw.get("trust_env") is False, "key-bearing probe inherits env proxies"
    assert kw.get("follow_redirects") is False
    assert "Authorization" in (kw.get("headers") or {})


# ------------------------------------------- F3: lmstudio usage trailer
def _lmstudio_sse(*payloads: str) -> str:
    return "".join(f"data: {p}\n\n" for p in payloads)


async def test_r5_f3_lmstudio_spec_order_usage_trailer():
    """OpenAI spec order: finish chunk → usage-only trailer → [DONE].
    Pre-fix the finish chunk returns early and usage is lost."""
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    def handler(request):
        text = _lmstudio_sse(
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            json.dumps({"choices": [], "usage": usage}),
            "[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage  # pre-fix: None
    assert "".join(ch.content for ch in chunks) == "hi"


async def test_r5_f3_lmstudio_usage_on_finish_still_works():
    """Control (must keep passing): usage carried ON the finish chunk."""
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    def handler(request):
        text = _lmstudio_sse(
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}),
            "[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage


async def test_r5_f3_lmstudio_stream_end_without_done():
    """A server that ends the stream after the trailer (no [DONE]) must
    still deliver the terminal chunk with usage."""
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    def handler(request):
        text = _lmstudio_sse(
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}),
            json.dumps({"choices": [], "usage": usage}))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage  # pre-fix: None (early return)


async def test_r5_f3_lmstudio_trailer_wait_is_bounded():
    """After finish, a server that never sends [DONE] must not stall the
    turn: the trailer wait is bounded and the terminal chunk still carries
    the stashed usage."""
    usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}

    def handler(request):
        payloads = [
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}),
            json.dumps({"choices": [], "usage": usage}),
        ] + [json.dumps({"choices": []})] * 200  # junk, never [DONE]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=_lmstudio_sse(*payloads))

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage  # pre-fix: None (early return)


# --------------------------------------------- F4: voice second start race
def test_r5_f4_second_start_supersedes_pending_stop(bridge, monkeypatch, tmp_path):
    """Nick P4: start1 slow (4.5s) → stop times out (3s cap, sets
    voice_stop_pending) → start2 told already=True → start1 lands. The
    second press must supersede the stale stop: recorder stays live."""
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

    def start1():
        results.append(bridge.post("/api/voice/record", {"action": "start"}))

    th = threading.Thread(target=start1)
    th.start()
    time.sleep(0.4)  # let start1 claim talk_pending
    stop = bridge.post("/api/voice/record", {"action": "stop"})
    assert stop.get("note") == "not recording"  # cap elapsed → stop pending
    start2 = bridge.post("/api/voice/record", {"action": "start"})
    assert start2.get("already") is True  # start1 still spawning
    th.join(20)
    assert not th.is_alive()
    assert len(SlowRec.instances) == 1
    r1 = results[0]
    assert r1.get("ok") is True
    # pre-fix: the stale stop-pending tears the late recorder down
    assert r1.get("stopped") is not True, "second start must supersede the stale stop"
    assert SlowRec.instances[0].running is True, "UI thinks recording but mic is off"
    # cleanup: release the live recorder (wav absent → 'too short' is fine)
    bridge.post("/api/voice/record", {"action": "stop"})
    assert SlowRec.instances[0].running is False


# --------------------------------------------------- L1: cast 404 parity
def test_r5_l1_cast_unknown_session_is_404(bridge):
    r = bridge.post("/api/skills/cast", {"name": "anything", "session": "no-such-session"})
    assert r.get("__status__") == 404, f"cast unknown-session must match /api/message: {r}"
    # parity anchor: /api/message already answers 404 for the same id
    m = bridge.req("POST", "/api/message", {"text": "hi", "session": "no-such-session"})
    assert m.get("__status__") == 404
