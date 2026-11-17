"""R10 correctness regressions — Task #141 (reproduce-first).

Noel's FINAL R10 ledger (.validation/noel-r10cor-report.md, HEAD 47a8457),
Terry-audited 19/19 REAL (.validation/terry-r10audit-report.md), independently
re-reproduced by .validation/taylor_r10_probes.py (12 CONFIRMED + 8 controls):
  N-R10-1 (High) bridge _agent_config arms request_yolo BEFORE
     Config.model_validate: a 400-rejected agent save leaves yolo armed
     (idle live / busy _yolo_pending that settles at turn end).
  N-R10-2 (High) core.set_mode + bridge providers/* mutate live config
     OUTSIDE config_mutex: a stale gated echo/reload reverts a concurrent
     key save / mode tightening / forget (M4b/M5b/M6/M11).
  M8 (Low) failed save_config leaves live mutated while disk stale.
  N-R10-3 (Med) cloud fallback key min(pending)-1 is POSITIVE after an
     indexed slot; a later real index collides (E7 namespace break).
  N-R10-4 (Low) ollama pull: non-dict ndjson line / str completed crash.
  N-R10-5 (Low) parser non-dict function -> AttributeError; validator
     integer inf/1e999 -> OverflowError (D7 family).
  N-R10-6 (Low) D8 tombstone check-then-save race: DELETE between check
     and write resurrects a deleted chat.

Every defect pin FAILS on pristine 47a8457; controls pin behavior that must
NOT change (F1/Y1 arm semantics, negative namespace, marker/C5 laws are
covered by the r7-r9 batteries re-run alongside). Guard-compatible: mock
backend / MockTransport, ephemeral loopback, temp OMLLM_HOME, bounded gates;
no subprocess, no live models. Gated windows use DIRECT svc/state reads —
never /api/status GETs (a reload there would need the config mutex the
gated echo holds). N-R10-6 uses Terry's gated-save shape: the DELETE runs
on a SEPARATE thread (a same-thread DELETE inside the save wrapper would
deadlock against the fixed turn_lock-held save).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time

import pytest

from ohmyllm.config.models import Config, GenerationSettings
from ohmyllm.config.store import ConfigStore


# ------------------------------------------------------------------ helpers
def _sse(resp, until="done", limit: int = 400) -> list[tuple[str, dict]]:
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
            events.append((ev, json.loads(dat)))
            if ev in terminals:
                resp.close()
                return events
        if len(events) > limit:
            resp.close()
            return events
        raw = resp.read1(65536)
    resp.close()
    return events


def _wait_state_idle(state, timeout: float = 15.0) -> None:
    """Direct state.busy poll — NO /api/status GET during gated windows."""
    deadline = time.time() + timeout
    while time.time() < deadline and state.busy:
        time.sleep(0.02)
    assert state.busy is False, "turn never went idle"


_STATES: list = []


@pytest.fixture(autouse=True)
def _capture_state(monkeypatch):
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


def _disk(svc) -> Config:
    return ConfigStore(svc.config_store.path).load()


class _RetireCounter:
    def __init__(self, svc):
        self.orig = svc._retire_runtimes
        self.n = 0
        counter = self

        def wrapped():
            counter.n += 1
            return counter.orig()

        self.wrapped = wrapped
        self.svc = svc

    def install(self):
        self.svc._retire_runtimes = self.wrapped

    def restore(self):
        with contextlib.suppress(AttributeError):
            del self.svc._retire_runtimes


class _CopyGate:
    """Hold the FIRST Config.model_copy after arming (Noel gated_copy): the
    echo's merge copy is frozen pre-B; post-fix B serializes behind the cm
    the echo holds, pre-fix B lands inside the window. Bounded."""

    def __init__(self, monkeypatch):
        self.orig = Config.model_copy
        self.armed = False
        self.held = threading.Event()
        self.release = threading.Event()
        gate = self

        def wrapped(cfg_self, *a, **k):
            out = gate.orig(cfg_self, *a, **k)
            if gate.armed and not gate.held.is_set():
                gate.held.set()
                gate.release.wait(25)
            return out

        monkeypatch.setattr(Config, "model_copy", wrapped)

    def restore(self):
        self.armed = False
        self.release.set()


def _run_ab(bridge, gate, a_fn, b_fn, observe: float = 0.5):
    """A gated in a thread; B in a second thread; bounded observation (the
    verdict comes from FINAL state, never from the observation)."""
    out: dict = {}
    b_done = threading.Event()

    def do_a():
        out["a"] = a_fn(bridge)

    ta = threading.Thread(target=do_a, daemon=True)
    ta.start()
    assert gate.held.wait(10), "A never reached the gate"

    def do_b():
        out["b"] = b_fn(bridge)
        b_done.set()

    tb = threading.Thread(target=do_b, daemon=True)
    tb.start()
    b_done.wait(observe)
    completed = b_done.is_set()
    gate.release.set()
    ta.join(20)
    tb.join(20)
    assert not ta.is_alive() and not tb.is_alive(), "gated threads never finished"
    return out, completed


def _echo(bridge):
    # backend ollama — "mock" has no Config section (400 before apply).
    return bridge.post("/api/model", {"backend": "ollama", "model": "t141-gate"})


# ============================================================ N-R10-1 (HIGH)
def test_n1_idle_rejected_save_has_no_arm_side_effects(bridge):
    """400-rejected agent save must not arm yolo live (arm-before-validate)."""
    st = _state()
    svc = st.svc
    assert svc.config.agent.yolo is False
    gen0 = svc.config.policy_generation
    rc = _RetireCounter(svc)
    rc.install()
    try:
        r = bridge.post("/api/config/agent",
                        {"yolo": True, "confirm": True, "max_steps": -1})
        assert r.get("__status__") == 400, r
        assert svc.config.agent.yolo is False, "rejected save armed yolo live"
        assert svc._yolo_pending is None
        assert svc.config.policy_generation == gen0
        assert rc.n == 0, "rejected save retired runtimes"
    finally:
        rc.restore()


def test_n1_busy_rejected_save_leaves_no_pending_arm(bridge):
    """Busy path: the 400 must not leave _yolo_pending to settle at turn end."""
    st = _state()
    svc = st.svc
    st.busy = True
    try:
        r = bridge.post("/api/config/agent",
                        {"yolo": True, "confirm": True, "max_steps": -1})
        assert r.get("__status__") == 400, r
        assert svc._yolo_pending is None, "rejected busy save left a deferred arm"
        svc.settle_yolo()
        assert svc.config.agent.yolo is False, "settled pending armed the next prompt"
    finally:
        st.busy = False
        svc._yolo_pending = None


def test_n1_arm_controls_unchanged(bridge):
    """F1/Y1: valid confirmed arm works; invalid-noyolo 400; unconfirmed 403."""
    st = _state()
    svc = st.svc
    r = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
    assert r.get("ok") is True, r
    assert svc.config.agent.yolo is True
    bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
    r = bridge.post("/api/config/agent", {"max_steps": -1})
    assert r.get("__status__") == 400
    assert svc.config.agent.yolo is False
    r = bridge.post("/api/config/agent", {"yolo": True})
    assert r.get("__status__") == 403
    assert svc.config.agent.yolo is False
    assert svc._yolo_pending is None


# ============================================================ N-R10-2 (HIGH)
def test_n2_keysave_survives_gated_echo(bridge, monkeypatch):
    """M4b: a stale gated model echo must not revert a concurrent key save."""
    st = _state()
    svc = st.svc
    gate = _CopyGate(monkeypatch)
    try:
        gate.armed = True
        out, _ = _run_ab(
            bridge, gate, _echo,
            lambda b: b.post("/api/providers/key",
                             {"pid": "openrouter", "key": "sk-t141-K1"}))
        assert out["a"].get("ok") is True, out["a"]
        assert svc.config.cloud.keys.get("openrouter") == "sk-t141-K1", \
            "stale echo reverted the key save LIVE"
        assert _disk(svc).cloud.keys.get("openrouter") == "sk-t141-K1", \
            "stale echo reverted the key save ON DISK"
    finally:
        gate.restore()
        bridge.post("/api/providers/forget", {"pid": "openrouter"})


def test_n2_mode_tightening_survives_gated_echo(bridge, monkeypatch):
    """M5b: a stale gated echo must not revert a strict tightening (fail-open)."""
    st = _state()
    svc = st.svc
    mode0 = svc.config.permission_mode.value
    gate = _CopyGate(monkeypatch)
    try:
        gate.armed = True
        out, _ = _run_ab(
            bridge, gate, _echo,
            lambda b: b.post("/api/mode", {"mode": "strict"}))
        assert out["a"].get("ok") is True, out["a"]
        assert svc.config.permission_mode.value == "strict", \
            "stale echo reverted the mode tightening LIVE"
        assert _disk(svc).permission_mode.value == "strict", \
            "stale echo reverted the mode tightening ON DISK"
    finally:
        gate.restore()
        bridge.post("/api/mode", {"mode": mode0})


def test_n2_keysave_survives_gated_reload(bridge, monkeypatch):
    """M6: cm-serialized reload must not revert a concurrent key save."""
    st = _state()
    svc = st.svc
    orig_load = svc.config_store.load
    held = threading.Event()
    release = threading.Event()

    def gated_load(*a, **k):
        out = orig_load(*a, **k)
        if not held.is_set():
            held.set()
            release.wait(25)
        return out

    monkeypatch.setattr(svc.config_store, "load", gated_load)
    out: dict = {}
    b_done = threading.Event()
    try:
        tr = threading.Thread(target=lambda: svc.reload_config(), daemon=True)
        tr.start()
        assert held.wait(10), "reload never loaded disk"

        def do_b():
            out["b"] = bridge.post("/api/providers/key",
                                   {"pid": "openai", "key": "sk-t141-K2"})
            b_done.set()

        tb = threading.Thread(target=do_b, daemon=True)
        tb.start()
        b_done.wait(0.5)
        release.set()
        tr.join(20)
        tb.join(20)
        assert not tr.is_alive() and not tb.is_alive()
        assert svc.config.cloud.keys.get("openai") == "sk-t141-K2", \
            "stale disk reload reverted the key save LIVE"
        assert _disk(svc).cloud.keys.get("openai") == "sk-t141-K2"
    finally:
        release.set()
        bridge.post("/api/providers/forget", {"pid": "openai"})


def test_n2_forget_survives_gated_echo(bridge, monkeypatch):
    """M11: a stale gated echo must not resurrect a forgotten API key."""
    st = _state()
    svc = st.svc
    bridge.post("/api/providers/key", {"pid": "groq", "key": "sk-t141-K3"})
    assert svc.config.cloud.keys.get("groq") == "sk-t141-K3"
    gate = _CopyGate(monkeypatch)
    try:
        gate.armed = True
        out, _ = _run_ab(
            bridge, gate, _echo,
            lambda b: b.post("/api/providers/forget", {"pid": "groq"}))
        assert out["a"].get("ok") is True, out["a"]
        assert svc.config.cloud.keys.get("groq") is None, \
            "stale echo resurrected the forgotten key LIVE"
        assert _disk(svc).cloud.keys.get("groq") is None, \
            "stale echo resurrected the forgotten key ON DISK"
    finally:
        gate.restore()
        bridge.post("/api/providers/forget", {"pid": "groq"})


def test_n2_sequential_mutators_control(bridge):
    """Sequential key save / mode / forget stay consistent live+disk."""
    st = _state()
    svc = st.svc
    mode0 = svc.config.permission_mode.value
    bridge.post("/api/providers/key", {"pid": "groq", "key": "sk-t141-SEQ"})
    assert svc.config.cloud.keys.get("groq") == "sk-t141-SEQ"
    assert _disk(svc).cloud.keys.get("groq") == "sk-t141-SEQ"
    r = bridge.post("/api/mode", {"mode": "strict"})
    assert r.get("ok") is True, r
    assert svc.config.permission_mode.value == "strict"
    assert _disk(svc).permission_mode.value == "strict"
    bridge.post("/api/providers/forget", {"pid": "groq"})
    assert svc.config.cloud.keys.get("groq") is None
    assert _disk(svc).cloud.keys.get("groq") is None
    bridge.post("/api/mode", {"mode": mode0})


# ================================================================== M8 (LOW)
def test_m8_failed_save_rolls_back_live(bridge, monkeypatch):
    """A failed persist must not leave live flipped while disk is stale."""
    st = _state()
    svc = st.svc
    orig_save = svc.save_config
    boom = {"n": 0}

    def failing_save():
        boom["n"] += 1
        if boom["n"] == 1:
            raise OSError("t141: injected disk failure")
        return orig_save()

    monkeypatch.setattr(svc, "save_config", failing_save)
    r = bridge.post("/api/config/memory", {"enabled": False})
    assert r.get("__status__") == 500, r
    assert not st.config_mutex._is_owned(), "config mutex left held"
    assert not st.turn_lock._is_owned(), "turn lock left held"
    assert svc.config.memory.enabled is True, "failed save left live mutated"
    assert _disk(svc).memory.enabled is True
    r2 = bridge.post("/api/config/memory", {"enabled": True})
    assert r2.get("ok") is True, r2
    assert svc.config.memory.enabled is True
    assert _disk(svc).memory.enabled is True


def test_m8_failed_security_save_still_retires(bridge, monkeypatch):
    """Fail-closed: a security delta retires even when the persist fails."""
    st = _state()
    svc = st.svc
    rc = _RetireCounter(svc)
    rc.install()
    orig_save = svc.save_config

    def failing_save():
        raise OSError("t141: injected disk failure")

    monkeypatch.setattr(svc, "save_config", failing_save)
    try:
        r = bridge.post("/api/config/memory", {"enabled": False})
        assert r.get("__status__") == 500, r
        assert rc.n >= 1, "security delta did not retire on failed save"
        assert svc.config.memory.enabled is True  # rolled back
    finally:
        rc.restore()
        monkeypatch.setattr(svc, "save_config", orig_save)


# ============================================================ N-R10-3 (MED)
def _tcline(tid=None, idx=None, name=None, args=None):
    tc = {}
    if tid is not None:
        tc["id"] = tid
    if idx is not None:
        tc["index"] = idx
    fn = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    tc["function"] = fn
    return "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [tc]}}]})


def _fin(reason):
    return "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]})


def _cloud_calls(monkeypatch, lines):
    import httpx

    from ohmyllm.backends.cloud import CloudBackend

    cfg = Config()
    cfg.cloud.provider = "custom-t141"
    cfg.cloud.keys["custom-t141"] = "sk-t141-fake"
    cfg.cloud.bases["custom-t141"] = "http://127.0.0.1:9/v1"
    cfg.cloud.customs["custom-t141"] = "T141Stub"
    body = ("\n".join(lines) + "\n").encode()
    be = CloudBackend(cfg, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                 content=body)))

    async def collect():
        return [ch async for ch in be.stream_chat("m", [{"role": "user", "content": "hi"}],
                                                  GenerationSettings())]

    chunks = asyncio.run(collect())
    done = [ch for ch in chunks if ch.done]
    return done[0].tool_calls if done else None


@pytest.mark.parametrize("hi,lo", [(5, 4), (7, 6)])
def test_n3_fallback_key_stays_negative_after_indexed(monkeypatch, hi, lo):
    """E7: id-only fallback after an indexed slot must not collide with a
    later real index (min(pending)-1 was POSITIVE)."""
    lines = [_tcline(idx=hi, tid="A", name="alpha", args='{"a":1}'),
             _tcline(tid="B", name="beta", args='{"b":2}'),
             _tcline(idx=lo, tid="C", name="gamma", args='{"g":3}'),
             _fin("tool_calls"), "data: [DONE]"]
    calls = _cloud_calls(monkeypatch, lines)
    assert calls is not None and len(calls) == 3, f"wire calls lost: {calls}"
    by_name = {c["function"]["name"]: c for c in calls}
    assert set(by_name) == {"alpha", "beta", "gamma"}
    assert by_name["gamma"]["id"] == "C"
    assert by_name["gamma"]["function"]["arguments"] == '{"g":3}'
    assert by_name["beta"]["function"]["arguments"] == '{"b":2}'


def test_n3_namespace_controls(monkeypatch):
    """id-first negative namespace + pure-indexed streams deliver 3 calls."""
    lines = [_tcline(tid="A", name="alpha", args='{"a":1}'),
             _tcline(tid="B", name="beta", args='{"b":2}'),
             _tcline(idx=0, tid="C", name="gamma", args='{"g":3}'),
             _fin("tool_calls"), "data: [DONE]"]
    calls = _cloud_calls(monkeypatch, lines)
    assert calls is not None and len(calls) == 3
    lines2 = [_tcline(idx=0, tid="A", name="alpha", args='{"a":1}'),
              _tcline(idx=1, tid="B", name="beta", args='{"b":2}'),
              _tcline(idx=2, tid="C", name="gamma", args='{"g":3}'),
              _fin("tool_calls"), "data: [DONE]"]
    calls2 = _cloud_calls(monkeypatch, lines2)
    assert calls2 is not None and len(calls2) == 3


# ============================================================ N-R10-4 (LOW)
def _pull(lines, progress=None):
    import httpx

    from ohmyllm.backends.ollama import OllamaBackend

    body = ("\n".join(lines) + "\n").encode()
    be = OllamaBackend(Config(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "application/x-ndjson"},
                                 content=body)))
    asyncio.run(be.pull("m", progress=progress))


def test_n4_pull_shape_guards():
    """Non-dict ndjson lines are skipped; str/bool completed -> pct None,
    never a raw AttributeError/TypeError."""
    good = json.dumps({"status": "downloading", "total": 10, "completed": 5})
    seen: list = []
    _pull([good, "[1,2]", "true",
           json.dumps({"status": "downloading", "total": 10, "completed": "5"}),
           json.dumps({"status": "downloading", "total": 10, "completed": True}),
           json.dumps({"status": "success"})],
          progress=lambda s, p: seen.append((s, p)))
    assert ("downloading", 0.5) in seen
    str_pct = [p for s, p in seen if s == "downloading" and p != 0.5]
    assert all(p is None for p in str_pct), f"str/bool completed produced pct: {seen}"


def test_n4_pull_progress_and_error_control():
    from ohmyllm.backends.base import BackendError

    seen: list = []
    _pull([json.dumps({"status": "downloading", "total": 10, "completed": 5}),
           json.dumps({"status": "success"})],
          progress=lambda s, p: seen.append((s, p)))
    assert ("downloading", 0.5) in seen
    with pytest.raises(BackendError):
        _pull([json.dumps({"error": "pull failed"})])


# ============================================================ N-R10-5 (LOW)
def test_n5_parser_nondict_function_is_malformed():
    """D7: a non-dict function value is a malformed parse, never a crash."""
    from ohmyllm.agents.parser import ToolCallParser

    p = ToolCallParser()
    poison = "```json\n" + json.dumps({"name": None, "function": "oops"}) + "\n```"
    rep = p.from_text(poison)  # must not raise (pre-fix: AttributeError)
    assert len(rep.calls) == 0
    poison2 = "```json\n" + json.dumps({"name": "", "tool": "", "function": 42}) + "\n```"
    rep2 = p.from_text(poison2)
    assert len(rep2.calls) == 0
    good = "```json\n" + json.dumps({"name": "read_file", "arguments": {"path": "x"}}) + "\n```"
    rep3 = p.from_text(good)
    assert len(rep3.calls) == 1 and rep3.calls[0].name == "read_file"


def test_n5_validator_integer_overflow_is_validation_error(svc):
    """D7: inf/1e999 integer coercion -> validation error (retry), not
    OverflowError; nan stays rejected; int passthrough precision unchanged."""
    from ohmyllm.agents.parser import ParsedCall

    v = svc.runtime().validator
    for val in (float("inf"), "1e999"):
        res = v.validate(ParsedCall(name="read_file",
                                    args={"path": "x", "max_lines": val}, source="native"))
        assert res.ok is False, f"{val!r} accepted"
    r_nan = v.validate(ParsedCall(name="read_file",
                                  args={"path": "x", "max_lines": float("nan")},
                                  source="native"))
    assert r_nan.ok is False
    r_int = v.validate(ParsedCall(name="read_file",
                                  args={"path": "x", "max_lines": 120}, source="native"))
    assert r_int.ok is True
    assert r_int.args.get("max_lines") == 120


# ============================================================ N-R10-6 (LOW)
def test_n6_delete_in_save_window_is_atomic(bridge, monkeypatch):
    """D8: DELETE landing between the tombstone check and the finally-save
    must not resurrect the chat. Terry's shape: gate the finally-save, run
    DELETE on a SEPARATE thread (post-fix it waits on turn_lock), release,
    then assert the file stays gone."""
    st = _state()
    svc = st.svc
    store = svc.sessions
    sess = store.create(first_message="race target")
    sid = sess.id
    sess.title = "t141 titled"  # skip the mid-turn rename-save window
    store.save(sess)
    orig_save = store.save
    held = threading.Event()
    release = threading.Event()
    fired = {"v": False}

    def gated_save(s):
        if s.id == sid and not fired["v"]:
            fired["v"] = True
            held.set()
            release.wait(25)
        return orig_save(s)

    monkeypatch.setattr(store, "save", gated_save)
    out: dict = {}
    d_done = threading.Event()

    def turner():
        resp = bridge.req("POST", "/api/message", {"text": "hello", "session": sid})
        _sse(resp)

    def deleter():
        out["del"] = bridge.delete(f"/api/chats/{sid}")
        d_done.set()

    try:
        tt = threading.Thread(target=turner, daemon=True)
        tt.start()
        assert held.wait(20), "finally-save never reached the gate"
        td = threading.Thread(target=deleter, daemon=True)
        td.start()
        d_done.wait(0.5)  # diagnostic only — verdict from final state
        release.set()
        tt.join(25)
        td.join(15)
        assert not tt.is_alive() and not td.is_alive()
        _wait_state_idle(st)
        assert fired["v"]
        assert sid in st.deleted_sessions, "DELETE never tombstoned"
        assert not (store.dir / f"{sid}.json").exists(), "deleted chat resurrected"
        assert not any(x.id == sid for x in store.list())
    finally:
        release.set()


def test_n6_preexisting_tombstone_blocks_save(bridge):
    """Control: a tombstone set BEFORE the turn blocks the finally-save."""
    st = _state()
    svc = st.svc
    store = svc.sessions
    sess = store.create(first_message="control target")
    sid = sess.id
    before = (store.dir / f"{sid}.json").read_text()
    st.deleted_sessions.add(sid)
    try:
        resp = bridge.req("POST", "/api/message", {"text": "hello", "session": sid})
        _sse(resp)
        _wait_state_idle(st)
        assert (store.dir / f"{sid}.json").read_text() == before
    finally:
        st.deleted_sessions.discard(sid)
        store.delete(sid)
