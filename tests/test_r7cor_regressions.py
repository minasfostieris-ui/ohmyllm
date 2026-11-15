"""R7 correctness regressions — Task #111 (reproduce-first).

Confirmed findings from .validation/sam-r7cor-report.md (HEAD c8ed95d):
  Y1 (High) _set_model: a stale model_copy(deep=True) taken OUTSIDE turn_lock
     re-arms a CONSUMED one-prompt yolo when apply_config lands after the turn
     settles → the next prompt auto-approves a file write with ZERO approvals
     (fail-open). _config/_memory_config share the stale-copy class.
  Y2/Y3 (Med) same stale-copy wipe destroys a SETTLED deferred yolo arm
     (_set_model / _config) — breaks the R5 F1 truthful next-prompt guarantee.
  Y5 (Med) an in-flight _set_model stale copy wipes an IDLE yolo arm (the arm
     lands after the copy, before the apply).
  T5/T12 (Low) lmstudio/cloud: the usage trailer on EXACTLY the 64th
     post-finish line is dropped — the cap check precedes the parse of that
     line (parse-before-cap fixes both).
  R7-04 (Low, Chloe cross-lane) do_GET has no 404 fallback: unknown GET routes
     return zero bytes/close instead of the JSON 404 POST/PATCH/DELETE send.

Every defect test FAILS on pristine c8ed95d; the two controls pin behavior
that must NOT change (loop-serialized _backend switch, usage under the cap).
Gated repro mechanics reused from Sam's sam_r7_yolo.py / sam_r7_trailer.py:
Services.apply_config is held to widen the real copy->apply window on the
threaded HTTP server; nothing else is stubbed. Guard-compatible: no subprocess,
ephemeral loopback, fake transports, temp OMLLM_HOME, fake-root write targets.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest

from ohmyllm.backends.base import ChatChunk
from ohmyllm.backends.cloud import CloudBackend
from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.config import Config
from ohmyllm.config.models import GenerationSettings
from ohmyllm.core import Services


# ------------------------------------------------------------------ helpers
def _sse(resp, until="done", hook=None, limit: int = 400) -> list[tuple[str, dict]]:
    """Minimal SSE reader (same framing as test_r6cor_regressions); `until`
    may be a tuple of terminal event names."""
    terminals = (until,) if isinstance(until, str) else tuple(until)
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
            if ev in terminals:
                resp.close()
                return events
        if len(events) > limit:
            resp.close()
            return events
        raw = resp.read1(65536)
    resp.close()
    return events


def _wait_idle(bridge, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and bridge.get("/api/status")["busy"]:
        time.sleep(0.02)
    assert bridge.get("/api/status")["busy"] is False, "turn never went idle"


def _wait_state_idle(state, timeout: float = 15.0) -> None:
    """Direct state.busy poll — NO /api/status GET: a status GET after any
    config persist triggers _sync_disk_config -> reload_config -> an EXTRA
    apply_config call, which would poison a gated repro's call sequence."""
    deadline = time.time() + timeout
    while time.time() < deadline and state.busy:
        time.sleep(0.02)
    assert state.busy is False, "turn never went idle"


_P_CONTENT = json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]})
_P_FINISH = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


async def _consume(be) -> list[ChatChunk]:
    return [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]


def _cloud_cfg() -> Config:
    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys = {"openai": "sk-r7-test"}
    return cfg


_STATES: list = []


@pytest.fixture(autouse=True)
def _capture_state(monkeypatch):
    """Capture the BridgeState the bridge fixture boots (Cindy's proven
    mechanism: patched __init__). Autouse runs before the bridge fixture."""
    from ohmyllm import bridge as B

    orig = B.BridgeState.__init__

    def cap(self, *a, **k):
        orig(self, *a, **k)
        _STATES.append(self)

    monkeypatch.setattr(B.BridgeState, "__init__", cap)
    yield
    _STATES.clear()


def _state():
    assert _STATES, "bridge state never captured"
    return _STATES[-1]


class _ApplyGate:
    """Sam's gated repro (sam_r7_yolo.py ApplyGate), pytest-adapted: hold
    apply_config calls until release — while busy, or the Nth call
    unconditionally. Only widens the real copy->apply window; the source
    tree is untouched. Bounded waits keep a broken fix from hanging CI."""

    def __init__(self, monkeypatch, state, *, busy_only: bool = True, hold_on_call: int = 1):
        self.orig = Services.apply_config
        self.armed = False
        self.release = threading.Event()
        self.held = threading.Event()
        self.calls = 0
        self.hold_on_call = hold_on_call
        gate = self

        def wrapped(selfsvc, cfg, **kw):
            if gate.armed and (not busy_only or getattr(state, "busy", False)):
                gate.calls += 1
                if gate.calls == gate.hold_on_call:
                    gate.held.set()
                    gate.release.wait(25)
            return gate.orig(selfsvc, cfg, **kw)

        monkeypatch.setattr(Services, "apply_config", wrapped)

    def restore(self):
        self.armed = False
        self.release.set()


class _ContentGate:
    """Hold apply_config iff the incoming config matches a predicate (the
    stale endpoint's signature delta). Content-based, so unrelated
    reload_config applies (disk-mtime sync on /api/status) never perturb
    the repro — Sam's call-count gate adapted for the pytest leg."""

    def __init__(self, monkeypatch, match):
        self.orig = Services.apply_config
        self.release = threading.Event()
        self.held = threading.Event()
        gate = self

        def wrapped(selfsvc, cfg, **kw):
            if match(cfg):
                gate.held.set()
                gate.release.wait(25)
            return gate.orig(selfsvc, cfg, **kw)

        monkeypatch.setattr(Services, "apply_config", wrapped)

    def restore(self):
        self.release.set()


def _prompt_approvals(bridge, text: str, *, auto_approve: bool = True):
    approvals: list = []

    def hook(ev, d):
        if ev == "approval_request":
            approvals.append(d)
            if auto_approve:
                bridge.post(f"/api/approval/{d['id']}", {"decision": "approve"})

    events = _sse(bridge.post("/api/message", {"text": text}),
                  until=("done", "approval_request"), hook=hook)
    _wait_idle(bridge)
    return events, approvals


def _fire_on_token(fn):
    """SSE hook: run fn on a daemon thread at the FIRST token (mid-turn)."""
    fired: list = []
    threads: list = []

    def hook(ev, d):
        if ev == "token" and not fired:
            fired.append(True)
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            threads.append(t)

    return hook, fired, threads


def _restore_mock(bridge):
    """Harness cleanup: Config has NO mock section, so /api/model cannot
    target mock (Sam's harness note). Flip the bridge's own Services back
    directly and persist, so a later disk-mtime reload cannot resurrect
    the lmstudio default. Bypasses apply_config on purpose — the gated
    repro's call sequence must stay undisturbed."""
    from ohmyllm.config import BackendName

    svc = _state().svc
    if svc.config.default_backend is not BackendName.MOCK:
        svc.config.default_backend = BackendName.MOCK
        svc._backend_cache.clear()
        svc.save_config()


def _cap_payloads(junk: int) -> list[str]:
    """content, finish, `junk` comment lines, then the usage trailer."""
    return ([_P_CONTENT, _P_FINISH] + [": junk"] * junk
            + [json.dumps({"choices": [], "usage": _USAGE})])


class _CapStream(httpx.AsyncByteStream):
    """SSE payloads + [DONE], then a short open tail so the line cap (not the
    stream end) deterministically terminates the post-finish read."""

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = payloads

    async def __aiter__(self):
        for p in self._payloads:
            yield f"data: {p}\n\n".encode()
        yield b"data: [DONE]\n\n"
        await asyncio.sleep(0.2)


class _CapTransport(httpx.AsyncBaseTransport):
    def __init__(self, payloads: list[str]) -> None:
        self._payloads = payloads

    async def handle_async_request(self, request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_CapStream(self._payloads))


# ------------------------------- Y1: fail-open resurrection (HIGH)
def test_r7_y1_setmodel_no_yolo_resurrection(bridge, oml_env, monkeypatch):
    """A CONSUMED one-prompt yolo must stay disarmed when a model switch's
    apply_config lands after the turn settles. Pre-fix the stale copy carries
    yolo=True and re-arms it: prompt2 auto-approves with ZERO approvals."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    gate = _ApplyGate(monkeypatch, _state())
    out: dict = {}
    try:
        arm = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
        assert arm.get("ok") is True, arm
        assert bridge.get("/api/status")["yolo"] is True
        gate.armed = True
        hook, fired, threads = _fire_on_token(
            lambda: out.update(model=bridge.post("/api/model",
                                                 {"backend": "lmstudio", "model": "r7-stale"})))
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        assert gate.held.wait(10), "apply_config never reached the gate"
        _wait_idle(bridge)  # turn done: finally-disarm + settle ran
        assert bridge.get("/api/status")["yolo"] is False, "one-prompt arm not consumed"
        gate.release.set()
        for t in threads:
            t.join(20)
        assert out.get("model", {}).get("ok") is True, out.get("model")
        # pre-fix: the stale apply resurrects yolo here (fail-open)
        assert bridge.get("/api/status")["yolo"] is False, \
            "R7-Y1: stale _set_model copy re-armed a CONSUMED one-prompt yolo"
        _restore_mock(bridge)
        _, approvals = _prompt_approvals(bridge, 'create file r7y1.txt with content "x"',
                                         auto_approve=False)
        if approvals:
            bridge.post(f"/api/approval/{approvals[0]['id']}", {"decision": "deny"})
            _wait_idle(bridge)
        assert approvals, "R7-Y1: prompt2 auto-approved a file write with ZERO approvals"
    finally:
        gate.restore()
        _restore_mock(bridge)
        bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
        _wait_idle(bridge)


# ------------------------------- Y2/Y3: settled deferred-arm wipe (MED)
def _deferred_arm_survives(bridge, oml_env, monkeypatch, fire, fname):
    # NOTE: the bridge boots its OWN Services (BridgeState); the settled
    # flag must be read from _state().svc, not the conftest svc fixture.
    bsvc = _state().svc
    gate = _ApplyGate(monkeypatch, _state(), hold_on_call=2)
    gate.armed = True
    out: dict = {}
    try:
        assert bridge.get("/api/status")["yolo"] is False

        def arm_then_stale():
            out["arm"] = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
            fire(bridge, out)

        hook, fired, threads = _fire_on_token(arm_then_stale)
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        assert gate.held.wait(10), "stale apply never reached the gate"
        assert out.get("arm", {}).get("yolo_deferred") is True, \
            f"arm not deferred: {out.get('arm')} calls={gate.calls}"
        assert bridge.get("/api/status")["yolo"] is True  # truthful effective UI
        _wait_idle(bridge)
        assert bsvc.config.agent.yolo is True, \
            f"deferred arm never settled (calls={gate.calls}, arm={out.get('arm', {}).get('yolo_deferred')})"
        gate.release.set()
        for t in threads:
            t.join(20)
        # pre-fix: the stale apply wipes the settled arm here
        assert bridge.get("/api/status")["yolo"] is True, \
            f"{fname}: stale config copy wiped a settled deferred yolo arm"
        _restore_mock(bridge)
        _, approvals = _prompt_approvals(bridge, f'create file {fname} with content "w"')
        assert not approvals, f"{fname}: next prompt asked approval while UI showed armed"
        assert (oml_env["ws"] / fname).exists()
        _wait_idle(bridge)
        # F1 one-prompt contract: turn2 consumed the arm
        assert bridge.get("/api/status")["yolo"] is False
    finally:
        gate.restore()
        _restore_mock(bridge)
        bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
        _wait_idle(bridge)


def test_r7_y2_setmodel_deferred_arm_survives(bridge, oml_env, monkeypatch):
    # long turn: the fire thread's arm POST must land MID-turn (deferred),
    # not after the finally — same timing margin as Sam's probe (delay 0.15
    # over the ~24-token hello reply).
    monkeypatch.setattr(MockBackend, "delay", 0.3)
    _deferred_arm_survives(
        bridge, oml_env, monkeypatch,
        lambda b, out: out.update(extra=b.post("/api/model",
                                               {"backend": "lmstudio", "model": "r7-stale"})),
        "r7y2.txt")


def test_r7_y3_config_deferred_arm_survives(bridge, oml_env, monkeypatch):
    monkeypatch.setattr(MockBackend, "delay", 0.3)
    _deferred_arm_survives(
        bridge, oml_env, monkeypatch,
        lambda b, out: out.update(extra=b.patch("/api/config",
                                                {"generation": {"temperature": 0.7}})),
        "r7y3.txt")


# ------------------------------- Y5: idle-arm wipe (MED) + memory neighbor
def _idle_arm_survives(bridge, oml_env, monkeypatch, fire, fname):
    gate = _ApplyGate(monkeypatch, _state(), busy_only=False)
    out: dict = {}
    try:
        assert bridge.get("/api/status")["yolo"] is False
        gate.armed = True
        t = threading.Thread(target=lambda: fire(bridge, out), daemon=True)
        t.start()
        assert gate.held.wait(10), "apply never gated"
        arm = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
        assert arm.get("ok") is True, arm
        assert bridge.get("/api/status")["yolo"] is True
        gate.release.set()
        t.join(20)
        assert not t.is_alive()
        # pre-fix: the in-flight stale copy wipes the idle arm here
        assert bridge.get("/api/status")["yolo"] is True, \
            f"{fname}: in-flight stale copy wiped an IDLE yolo arm"
        _restore_mock(bridge)
        _, approvals = _prompt_approvals(bridge, f'create file {fname} with content "q"')
        assert not approvals, f"{fname}: next prompt asked approval while UI showed armed"
        assert (oml_env["ws"] / fname).exists()
        _wait_idle(bridge)
        assert bridge.get("/api/status")["yolo"] is False
    finally:
        gate.restore()
        _restore_mock(bridge)
        bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
        _wait_idle(bridge)


def test_r7_y5_setmodel_idle_arm_survives(bridge, oml_env, monkeypatch):
    _idle_arm_survives(
        bridge, oml_env, monkeypatch,
        lambda b, out: out.update(model=b.post("/api/model",
                                               {"backend": "lmstudio", "model": "r7-stale"})),
        "r7y5.txt")


def test_r7_memory_config_idle_arm_survives(bridge, oml_env, monkeypatch):
    """_memory_config shares the stale-copy class (Sam: same-class neighbor,
    not separately probed) — the unified fix must cover it too."""
    _idle_arm_survives(
        bridge, oml_env, monkeypatch,
        lambda b, out: out.update(mem=b.post("/api/config/memory", {"agent_writes": False})),
        "r7mem.txt")


# ------------------------------- T5/T12: usage on exactly line 64 (LOW)
async def test_r7_t5_lmstudio_usage_on_line64():
    """The cap bounds the WAIT, not the parse of the boundary line: a usage
    trailer on EXACTLY the 64th post-finish line must still land on the
    terminal chunk (pre-fix the cap fires before the parse → usage dropped)."""
    be = LMStudioBackend(Config(), _CapTransport(_cap_payloads(63)))
    chunks = await asyncio.wait_for(_consume(be), 6.0)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert "".join(ch.content for ch in chunks) == "hi"
    assert done[0].usage == _USAGE, "R7-T5: usage trailer on line 64 dropped (cap before parse)"


async def test_r7_t12_cloud_usage_on_line64():
    """cloud parity: same cap-before-parse order drops usage on line 64."""
    be = CloudBackend(_cloud_cfg(), _CapTransport(_cap_payloads(63)))
    chunks = await asyncio.wait_for(_consume(be), 6.0)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert "".join(ch.content for ch in chunks) == "hi"
    assert done[0].usage == _USAGE, "R7-T12: cloud usage trailer on line 64 dropped"


# ------------------------------- R7-04: GET 404 parity (LOW, Chloe)
def test_r7_04_get_unknown_route_json_404(bridge):
    """Unknown GET routes must answer the same JSON 404 as POST/PATCH/DELETE —
    pre-fix do_GET falls through and the connection closes with zero bytes."""
    port = int(bridge.base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        req = (f"GET /api/definitely-not-a-route HTTP/1.1\r\n"
               f"Host: 127.0.0.1:{port}\r\n"
               f"X-OMLLM-Token: {bridge.token}\r\n"
               "Connection: close\r\n\r\n")
        s.sendall(req.encode())
        s.settimeout(10)
        data = b""
        while True:
            try:
                part = s.recv(65536)
            except TimeoutError:
                break
            if not part:
                break
            data += part
    assert data.startswith(b"HTTP/"), "R7-04: GET fell through — zero bytes, connection closed"
    assert b"404" in data.split(b"\r\n", 1)[0], data[:120]
    body = json.loads(data.split(b"\r\n\r\n", 1)[1])
    assert body == {"error": "no route"}
    # parity with the other verbs (already correct pre-fix)
    assert bridge.post("/api/definitely-not-a-route").get("__status__") == 404
    assert bridge.delete("/api/definitely-not-a-route").get("__status__") == 404


# ------------------------------- controls (must stay green pre AND post)
def test_r7_ctl_y4_backend_switch_deferred_arm(bridge, svc, oml_env, monkeypatch):
    """Sam Y4 control: the loop-serialized _backend switch preserves a
    mid-turn deferred arm (no copy->apply await gap). The unified turn_lock
    hardening of _backend must not change this outcome."""
    monkeypatch.setattr(MockBackend, "delay", 0.3)
    out: dict = {}

    def arm_switch():
        out["arm"] = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
        out["sw"] = bridge.post("/api/backend", {"name": "lmstudio"})

    try:
        hook, fired, threads = _fire_on_token(arm_switch)
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        for t in threads:
            t.join(30)
            assert not t.is_alive()
        _wait_idle(bridge)
        assert out.get("arm", {}).get("yolo_deferred") is True, out.get("arm")
        assert bridge.get("/api/status")["yolo"] is True
        # restore AFTER the switch landed (joining above) — else the two
        # racing POSTs can leave lmstudio as the default for prompt2
        _restore_mock(bridge)
        events2, approvals = _prompt_approvals(bridge, 'create file r7y4.txt with content "v"')
        assert not approvals
        assert (oml_env["ws"] / "r7y4.txt").exists(), \
            f"auto-approved turn wrote no file: {[(e, d) for e, d in events2 if e in ('done', 'tool_result', 'error')]}"
        _wait_idle(bridge)
        assert bridge.get("/api/status")["yolo"] is False
    finally:
        _restore_mock(bridge)
        bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
        _wait_idle(bridge)


async def test_r7_ctl_usage_on_line63_both_backends():
    """Sam T6 parity: a usage trailer just UNDER the cap (line 63, 62 junk
    lines before it) survives on both backends — the parse path the T5/T12
    fix must not disturb."""
    for be in (LMStudioBackend(Config(), _CapTransport(_cap_payloads(62))),
               CloudBackend(_cloud_cfg(), _CapTransport(_cap_payloads(62)))):
        chunks = await asyncio.wait_for(_consume(be), 6.0)
        done = [ch for ch in chunks if ch.done]
        assert len(done) == 1
        assert "".join(ch.content for ch in chunks) == "hi"
        assert done[0].usage == _USAGE
