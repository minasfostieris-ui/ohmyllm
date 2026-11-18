"""R9 correctness regressions — Task #129 (reproduce-first).

Confirmed findings from .validation/noah-r9cor-report.md (HEAD 5612ecf),
independently re-reproduced by .validation/taylor_r9_probes.py (dumps match
Noah's n1/n2a/n2b captures exactly):
  N-R9-1 (Med) bridge _agent_config: the under-lock freshness re-gate covers
     ONLY yolo; a stale no-confirm agent echo merges over a CONFIRMED
     tone/op_mode/custom_tones change landed while it blocked -> silent
     unconfirmed revert + spurious retire.
  N-R9-2a (Med) all 5 echo sites merge under turn_lock but apply OUTSIDE it:
     two concurrent echoes merge from the SAME pre-apply live config; the
     last apply wins wholesale and persists -> lost update.
  N-R9-2b (Med) the security-visible face: a stale GENERIC PATCH (protected
     comparison ran at merge time) reverts a confirmed security field via
     the non-exclude fieldcopy + a second retire/approval-wipe (C5 broken
     across requests).

Fix: a DEDICATED config-mutation mutex (never turn_lock — apply stays
outside turn_lock) serializing the FULL merge->validate->apply window at
all 5 echo sites + reload, and a FULL sensitive-key freshness re-gate under
the lock. Every defect pin FAILS on pristine 5612ecf; controls pin behavior
that must NOT change (Noah's cg1-cg8 matrix + confirmed-change landing).
Guard-compatible: mock backend, ephemeral loopback, temp OMLLM_HOME, bounded
gates; no subprocess, no live models. Gated windows use DIRECT svc reads /
state.busy polls — never /api/status GETs (a reload there would need the
config mutex the gated echo holds).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time

import pytest

from ohmyllm.backends.mock import MockBackend


# ------------------------------------------------------------------ helpers
def _sse(resp, until="done", hook=None, limit: int = 400) -> list[tuple[str, dict]]:
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
    """Direct state.busy poll — NO /api/status GET (a status GET after a
    persist triggers _sync_disk_config -> reload_config, which post-fix
    needs the config mutex a gated echo may hold)."""
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


def _fire_on_token(fn):
    fired: list = []
    threads: list = []

    def hook(ev, d):
        if ev == "token" and not fired:
            fired.append(True)
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            threads.append(t)

    return hook, fired, threads


def _arm(bridge) -> None:
    r = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
    assert r.get("ok") is True, r
    bridge.get("/api/status")  # absorb the disk-mtime reload BEFORE gating
    assert bridge.get("/api/status")["yolo"] is True


def _disarm(bridge) -> None:
    bridge.post("/api/config/agent", {"yolo": False, "confirm": True})
    _wait_idle(bridge)


def _set_tone(bridge, tone: str) -> None:
    r = bridge.post("/api/config/agent", {"tone": tone, "confirm": True})
    assert r.get("ok") is True, r


class _FirstLockGate:
    """Hold the FIRST acquisition of the config-mutation mutex after arming
    (post-fix); on pristine 5612ecf (no config_mutex) fall back to turn_lock —
    Noah's exact n1 mechanic. Later acquisitions pass straight through, so B
    lands its confirmed change while A is held. Bounded."""

    def __init__(self, state):
        self.state = state
        self.attr = "config_mutex" if hasattr(state, "config_mutex") else "turn_lock"
        self.real = getattr(state, self.attr)
        self.armed = False
        self.held = threading.Event()
        self.release = threading.Event()
        gate = self

        class GL:
            def __enter__(self):
                if gate.armed and not gate.held.is_set():
                    gate.held.set()
                    gate.release.wait(25)
                gate.real.acquire()
                return self

            def __exit__(self, *a):
                gate.real.release()
                return False

        self.wrapper = GL()

    def install(self):
        setattr(self.state, self.attr, self.wrapper)

    def restore(self):
        self.armed = False
        self.release.set()
        setattr(self.state, self.attr, self.real)


class _ApplyGate:
    """Hold the FIRST svc.apply_config call after arming (Noah's n2a/n2b
    gate_apply mechanic, instance-attribute shadow). Post-fix A holds the
    config mutex while gated, so B serializes behind it; pre-fix B completes
    fully inside the window. Bounded."""

    def __init__(self, svc):
        self.svc = svc
        self.orig = svc.apply_config
        self.armed = False
        self.held = threading.Event()
        self.release = threading.Event()
        gate = self

        def wrapped(cfg, *a, **k):
            if gate.armed and not gate.held.is_set():
                gate.held.set()
                gate.release.wait(25)
            return gate.orig(cfg, *a, **k)

        self.wrapped = wrapped

    def install(self):
        self.armed = True
        self.svc.apply_config = self.wrapped

    def restore(self):
        self.armed = False
        self.release.set()
        with contextlib.suppress(AttributeError):
            del self.svc.apply_config


class _RetireCounter:
    """Count _retire_runtimes calls (instance-attribute shadow): a stale
    echo's spurious SECOND retire is the security-visible harm of N-R9-2b."""

    def __init__(self, svc):
        self.svc = svc
        self.orig = svc._retire_runtimes
        self.n = 0
        counter = self

        def wrapped():
            counter.n += 1
            return counter.orig()

        self.wrapped = wrapped

    def install(self):
        self.svc._retire_runtimes = self.wrapped

    def restore(self):
        with contextlib.suppress(AttributeError):
            del self.svc._retire_runtimes


class _LockGate:
    """Hold the FIRST turn_lock acquisition after arming (r8cor G2b mechanic
    for cg8): the echo passes the pre-lock gate, then blocks gate->lock while
    the turn fully settles. Swaps BOTH state.turn_lock and svc._runtime_lock."""

    def __init__(self, state):
        self.state = state
        self.real = state.turn_lock
        self.armed = False
        self.held = threading.Event()
        self.release = threading.Event()
        gate = self

        class GL:
            def __enter__(self):
                if gate.armed and not gate.held.is_set():
                    gate.held.set()
                    gate.release.wait(25)
                gate.real.acquire()
                return self

            def __exit__(self, *a):
                gate.real.release()
                return False

        self.wrapper = GL()

    def install(self):
        self.state.turn_lock = self.wrapper
        if hasattr(self.state.svc, "_runtime_lock"):
            self.state.svc._runtime_lock = self.wrapper

    def restore(self):
        self.armed = False
        self.release.set()
        self.state.turn_lock = self.real
        if hasattr(self.state.svc, "_runtime_lock"):
            self.state.svc._runtime_lock = self.real


def _run_ab(bridge, gate, a_fn, b_fn, observe: float = 0.5):
    """Start A (gated), wait for the hold, start B in a thread, observe for
    `observe` seconds (pre-fix B completes; post-fix B is serialized), then
    release A and join both. Returns (out, b_completed_before_release)."""
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
    return out, completed


# ============================================================ N-R9-1 (MED)
def test_n1_stale_agent_echo_regated_under_lock(bridge, oml_env):
    """N-R9-1: A echoes the CURRENT tone (no confirm needed at the pre-lock
    gate) and blocks; B lands a CONFIRMED tone change; A resumes. The
    under-lock FULL sensitive-key re-gate must 403 the stale delta — pre-fix
    A merges its stale tone over B's confirmed one (200, revert, retire)."""
    st = _state()
    svc = st.svc
    _set_tone(bridge, "default")
    gate = _FirstLockGate(st)
    out: dict = {}
    try:
        gate.armed = True
        gate.install()
        ta = threading.Thread(
            target=lambda: out.update(a=bridge.post("/api/config/agent", {"tone": "default"})),
            daemon=True)
        ta.start()
        assert gate.held.wait(10), "echo never gated"
        b = bridge.post("/api/config/agent", {"tone": "ruthless", "confirm": True})
        assert b.get("ok") is True, b
        assert svc.config.agent.tone == "ruthless"
        gen_b = svc.config.policy_generation
        rt2 = svc.runtime()
        gate.release.set()
        ta.join(20)
        assert out["a"].get("__status__") == 403, \
            f"N-R9-1: stale no-confirm echo reverted a confirmed tone: {out['a']}"
        assert svc.config.agent.tone == "ruthless", "N-R9-1: confirmed tone reverted"
        assert svc.config.policy_generation == gen_b, "N-R9-1: spurious retire/generation bump"
        assert not rt2._retired, "N-R9-1: fresh runtime retired by the stale echo"
    finally:
        gate.restore()
        _set_tone(bridge, "default")


def test_n1_confirmed_stale_echo_still_applies(bridge, oml_env):
    """Control: the SAME race with confirm:true on A — a confirmed intent
    must still land (the re-gate only blocks UNCONFIRMED deltas)."""
    st = _state()
    svc = st.svc
    _set_tone(bridge, "default")
    gate = _FirstLockGate(st)
    out: dict = {}
    try:
        gate.armed = True
        gate.install()
        ta = threading.Thread(
            target=lambda: out.update(
                a=bridge.post("/api/config/agent", {"tone": "default", "confirm": True})),
            daemon=True)
        ta.start()
        assert gate.held.wait(10), "echo never gated"
        b = bridge.post("/api/config/agent", {"tone": "ruthless", "confirm": True})
        assert b.get("ok") is True, b
        gen_b = svc.config.policy_generation
        gate.release.set()
        ta.join(20)
        assert out["a"].get("ok") is True, out["a"]
        assert svc.config.agent.tone == "default", "confirmed intent did not land"
        assert svc.config.policy_generation == gen_b + 1, "confirmed delta must retire once"
    finally:
        gate.restore()
        _set_tone(bridge, "default")


def test_n1_nonsensitive_echo_not_regated(bridge, oml_env):
    """Control: a NON-sensitive echo (max_steps) racing a confirmed tone
    change must still pass without confirm and must not revert the tone
    (the under-lock merge re-reads live; the re-gate guards sensitive keys
    present in incoming only)."""
    st = _state()
    svc = st.svc
    _set_tone(bridge, "default")
    gate = _FirstLockGate(st)
    out: dict = {}
    try:
        gate.armed = True
        gate.install()
        ta = threading.Thread(
            target=lambda: out.update(a=bridge.post("/api/config/agent", {"max_steps": 7})),
            daemon=True)
        ta.start()
        assert gate.held.wait(10), "echo never gated"
        b = bridge.post("/api/config/agent", {"tone": "ruthless", "confirm": True})
        assert b.get("ok") is True, b
        gen_b = svc.config.policy_generation
        gate.release.set()
        ta.join(20)
        assert out["a"].get("ok") is True, out["a"]
        assert svc.config.agent.max_steps == 7
        assert svc.config.agent.tone == "ruthless", "non-sensitive echo reverted the tone"
        assert svc.config.policy_generation == gen_b, "non-sensitive echo bumped generation"
    finally:
        gate.restore()
        _set_tone(bridge, "default")


# ============================================================ N-R9-2a (MED)
def test_n2a_concurrent_disjoint_echoes_both_survive(bridge, oml_env):
    """N-R9-2a: A (temperature) gated at apply; B (ui panels) runs concurrently.
    Pre-fix both merge from the SAME pre-apply live; A's late wholesale apply
    erases B's edit and persists it. Post-fix the config mutex serializes the
    full merge->apply windows: BOTH edits survive in memory AND on disk."""
    st = _state()
    svc = st.svc
    r = bridge.patch("/api/config", {"generation": {"temperature": 0.7},
                                     "ui": {"show_tool_panels": True}})
    assert r.get("ok") is True, r
    gate = _ApplyGate(svc)
    try:
        gate.install()
        out, _ = _run_ab(
            bridge, gate,
            lambda b: b.patch("/api/config", {"generation": {"temperature": 0.31}}),
            lambda b: b.patch("/api/config", {"ui": {"show_tool_panels": False}}))
        assert out["a"].get("ok") is True, out["a"]
        assert out["b"].get("ok") is True, out["b"]
        assert svc.config.generation.temperature == 0.31
        assert svc.config.ui.show_tool_panels is False, \
            "N-R9-2a: concurrent echo lost B's update (last apply won wholesale)"
        disk = svc.config_store.load()
        assert disk.ui.show_tool_panels is False, "N-R9-2a: lost update PERSISTED"
        assert disk.generation.temperature == 0.31
    finally:
        gate.restore()


def test_n2a_model_echo_serialized(bridge, oml_env):
    """N-R9-2a at the _set_model site: a gated model pick must not erase a
    concurrent generic PATCH (same merge->apply window, same mutex).
    backend=lmstudio: a REAL config section with default_model (mock has no
    section — _set_model would 400 before ever reaching apply)."""
    st = _state()
    svc = st.svc
    r = bridge.patch("/api/config", {"generation": {"temperature": 0.7}})
    assert r.get("ok") is True, r
    gate = _ApplyGate(svc)
    try:
        gate.install()
        out, _ = _run_ab(
            bridge, gate,
            lambda b: b.post("/api/model", {"backend": "lmstudio", "model": "t9model"}),
            lambda b: b.patch("/api/config", {"generation": {"temperature": 0.41}}))
        assert out["a"].get("ok") is True, out["a"]
        assert out["b"].get("ok") is True, out["b"]
        assert svc.config.lmstudio.default_model == "t9model"
        assert svc.config.generation.temperature == 0.41, \
            "N-R9-2a: model echo erased the concurrent temperature edit"
        assert svc.config_store.load().generation.temperature == 0.41
    finally:
        gate.restore()


def test_n2a_backend_echo_serialized(bridge, oml_env):
    """N-R9-2a at the _backend site: a gated backend pick must not erase a
    concurrent generic PATCH (the await health/start stays outside the mutex)."""
    st = _state()
    svc = st.svc
    r = bridge.patch("/api/config", {"ui": {"show_tool_panels": True}})
    assert r.get("ok") is True, r
    gate = _ApplyGate(svc)
    try:
        gate.install()
        out, _ = _run_ab(
            bridge, gate,
            lambda b: b.post("/api/backend", {"name": "mock"}),
            lambda b: b.patch("/api/config", {"ui": {"show_tool_panels": False}}))
        assert out["a"].get("ok") is True, out["a"]
        assert out["b"].get("ok") is True, out["b"]
        assert svc.config.ui.show_tool_panels is False, \
            "N-R9-2a: backend echo erased the concurrent ui edit"
        assert svc.config_store.load().ui.show_tool_panels is False
    finally:
        gate.restore()


def test_n2a_sequential_disjoint_both_survive(bridge, oml_env):
    """Control (Noah cg6): sequential disjoint edits both survive — the
    serialization must not change the no-concurrency path."""
    st = _state()
    svc = st.svc
    r1 = bridge.patch("/api/config", {"generation": {"temperature": 0.41}})
    r2 = bridge.patch("/api/config", {"ui": {"show_tool_panels": False}})
    assert r1.get("ok") is True and r2.get("ok") is True
    assert svc.config.generation.temperature == 0.41
    assert svc.config.ui.show_tool_panels is False
    bridge.patch("/api/config", {"ui": {"show_tool_panels": True}})


# ============================================================ N-R9-2b (MED)
def test_n2b_generic_echo_cannot_revert_confirmed_tone(bridge, oml_env):
    """N-R9-2b: A = generation-only PATCH (agent delta ZERO at merge time)
    gated at apply; B = CONFIRMED tone change. Pre-fix B lands inside the
    window and A's stale fieldcopy reverts the tone + a SECOND spurious
    retire (gen bump, approvals wiped). Post-fix B serializes behind A:
    tone stays ruthless, exactly ONE retire (B's legitimate one)."""
    st = _state()
    svc = st.svc
    _set_tone(bridge, "default")
    gen0 = svc.config.policy_generation
    gate = _ApplyGate(svc)
    counter = _RetireCounter(svc)
    try:
        gate.install()
        counter.install()
        out, _ = _run_ab(
            bridge, gate,
            lambda b: b.patch("/api/config", {"generation": {"temperature": 0.33}}),
            lambda b: b.post("/api/config/agent", {"tone": "ruthless", "confirm": True}))
        assert out["a"].get("ok") is True, out["a"]
        assert out["b"].get("ok") is True, out["b"]
        assert svc.config.agent.tone == "ruthless", \
            "N-R9-2b: stale generic echo reverted a CONFIRMED tone without confirm"
        assert svc.config_store.load().agent.tone == "ruthless", "N-R9-2b: revert persisted"
        assert counter.n == 1, f"N-R9-2b: spurious second retire (retire calls={counter.n})"
        assert svc.config.policy_generation == gen0 + 1, "N-R9-2b: spurious generation bump"
    finally:
        gate.restore()
        counter.restore()
        _set_tone(bridge, "default")


def test_n2b_generic_echo_cannot_revert_confirmed_op_mode(bridge, oml_env):
    """N-R9-2b variant: the confirmed security field is op_mode (chat/plan/
    build gates writes+shell) — a stale generic echo must not revert it."""
    st = _state()
    svc = st.svc
    gen0 = svc.config.policy_generation
    gate = _ApplyGate(svc)
    counter = _RetireCounter(svc)
    try:
        gate.install()
        counter.install()
        out, _ = _run_ab(
            bridge, gate,
            lambda b: b.patch("/api/config", {"generation": {"temperature": 0.34}}),
            lambda b: b.post("/api/config/agent", {"op_mode": "plan", "confirm": True}))
        assert out["a"].get("ok") is True, out["a"]
        assert out["b"].get("ok") is True, out["b"]
        assert svc.config.agent.op_mode == "plan", \
            "N-R9-2b: stale generic echo reverted a CONFIRMED op_mode"
        assert svc.config_store.load().agent.op_mode == "plan"
        assert counter.n == 1, f"N-R9-2b: spurious second retire (retire calls={counter.n})"
        assert svc.config.policy_generation == gen0 + 1
    finally:
        gate.restore()
        counter.restore()
        r = bridge.post("/api/config/agent", {"op_mode": "build", "confirm": True})
        assert r.get("ok") is True, r


def test_n2b_memory_echo_serialized(bridge, oml_env):
    """N-R9-2b at the _memory_config site: a gated memory-policy echo must
    not revert a concurrent CONFIRMED tone change (and must still land its
    own edit)."""
    st = _state()
    svc = st.svc
    _set_tone(bridge, "default")
    gate = _ApplyGate(svc)
    try:
        gate.install()
        out, _ = _run_ab(
            bridge, gate,
            lambda b: b.post("/api/config/memory", {"max_inject_chars": 5000}),
            lambda b: b.post("/api/config/agent", {"tone": "ruthless", "confirm": True}))
        assert out["a"].get("ok") is True, out["a"]
        assert out["b"].get("ok") is True, out["b"]
        assert svc.config.agent.tone == "ruthless", \
            "N-R9-2b: memory echo reverted a CONFIRMED tone"
        assert svc.config.memory.max_inject_chars == 5000
        disk = svc.config_store.load()
        assert disk.agent.tone == "ruthless"
        assert disk.memory.max_inject_chars == 5000
    finally:
        gate.restore()
        _set_tone(bridge, "default")


# ==================================================== Noah control matrix
def test_cg1_idle_noconfirm_yolo_arm_403(bridge):
    """Control (Noah cg1 / G2c): idle no-confirm yolo arm stays 403."""
    r = bridge.post("/api/config/agent", {"yolo": True})
    assert r.get("__status__") == 403
    assert bridge.get("/api/status")["yolo"] is False


def test_cg2_busy_armed_noconfirm_echo_403(bridge, oml_env, monkeypatch):
    """Control (Noah cg2 / G2 settled baseline): a no-confirm True echo
    during an armed busy turn is 403 and the grant stays consumed."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    out: dict = {}
    try:
        _arm(bridge)
        hook, fired, threads = _fire_on_token(
            lambda: out.update(echo=bridge.post("/api/config/agent", {"yolo": True})))
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        for t in threads:
            t.join(20)
        _wait_idle(bridge)
        assert out.get("echo", {}).get("__status__") == 403, out.get("echo")
        assert bridge.get("/api/status")["yolo"] is False
    finally:
        _disarm(bridge)


def test_cg3_idle_armed_echo_confirm_free_no_retire(bridge):
    """Control (Noah cg3): idle unconsumed True==True echo stays
    confirm-free and never retires/bumps."""
    st = _state()
    try:
        _arm(bridge)
        gen0 = st.svc.config.policy_generation
        r = bridge.post("/api/config/agent", {"yolo": True})
        assert r.get("ok") is True, r
        assert bridge.get("/api/status")["yolo"] is True
        assert st.svc.config.policy_generation == gen0
    finally:
        _disarm(bridge)


def test_cg4_deferred_pending_gate_match_flip_settle(bridge, oml_env, monkeypatch):
    """Control (Noah cg4 / F1): a confirmed mid-turn arm DEFERS; the pending
    baseline gates echoes (match 200 / flip 403); settle arms the next prompt."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    out: dict = {}

    def seq(bridge):
        out["arm"] = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
        out["match"] = bridge.post("/api/config/agent", {"yolo": True})
        out["flip"] = bridge.post("/api/config/agent", {"yolo": False})

    try:
        hook, fired, threads = _fire_on_token(lambda: seq(bridge))
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        for t in threads:
            t.join(20)
        _wait_idle(bridge)
        assert out.get("arm", {}).get("yolo_deferred") is True, out.get("arm")
        assert out.get("match", {}).get("ok") is True, out.get("match")
        assert out.get("flip", {}).get("__status__") == 403, out.get("flip")
        assert bridge.get("/api/status")["yolo"] is True, "deferred arm did not settle"
    finally:
        _disarm(bridge)


def test_cg5_reload_carry_marker_never_persisted(bridge, oml_env):
    """Control (Noah cg5): reload carries the idle arm, no retire, and the
    provenance marker never reaches disk — the mutex must not change this."""
    st = _state()
    svc = st.svc
    try:
        _arm(bridge)
        rt = svc.runtime()
        gen0 = svc.config.policy_generation
        svc.reload_config()
        assert svc.config.agent.yolo is True
        assert svc.config.policy_generation == gen0
        assert not rt._retired
        assert "_omllm_carry_runtime" not in svc.config_store.path.read_text()
    finally:
        _disarm(bridge)


@pytest.mark.skipif(os.name == "nt", reason="Windows hosted-runner latency: /api/status p95 <10ms is Linux-calibrated")
def test_cg7_status_perf_p95(bridge):
    """Control (Noah cg7): /api/status p95 stays under 10ms (n=200) — the
    config mutex must not slow the status path (it never takes it unless
    the disk mtime moved)."""
    lat = []
    for _ in range(200):
        t0 = time.perf_counter()
        bridge.get("/api/status")
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    p95 = lat[int(len(lat) * 0.95)]
    assert p95 < 10, f"/api/status p95 regressed: {p95:.2f}ms"


def test_cg8_consumed_rearm_recheck_403(bridge, oml_env, monkeypatch):
    """Control (Noah cg8 / G2b): the under-lock yolo freshness recheck still
    refuses a consumed-grant rearm after a gate->lock settle race."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    st = _state()
    gate = _LockGate(st)
    out: dict = {}
    try:
        _arm(bridge)
        gate.armed = True
        gate.install()
        t = threading.Thread(
            target=lambda: out.update(echo=bridge.post("/api/config/agent", {"yolo": True})),
            daemon=True)
        t.start()
        assert gate.held.wait(10), "echo never blocked gate->lock"
        _sse(bridge.post("/api/message", {"text": "hello"}))
        _wait_state_idle(st)
        assert st.svc.config.agent.yolo is False, "one-prompt arm not consumed"
        gate.release.set()
        t.join(20)
        assert out.get("echo", {}).get("__status__") == 403, out.get("echo")
        assert st.svc.config.agent.yolo is False, "consumed grant resurrected"
    finally:
        gate.restore()
        _disarm(bridge)


def test_ctl_confirmed_sensitive_changes_land(bridge):
    """Control: legitimate CONFIRMED tone/op_mode/custom_tones changes still
    land through the dedicated control (the re-gate must not over-block)."""
    st = _state()
    svc = st.svc
    gen0 = svc.config.policy_generation
    r = bridge.post("/api/config/agent", {"tone": "ruthless", "confirm": True})
    assert r.get("ok") is True, r
    assert svc.config.agent.tone == "ruthless"
    r = bridge.post("/api/config/agent", {"op_mode": "plan", "confirm": True})
    assert r.get("ok") is True, r
    assert svc.config.agent.op_mode == "plan"
    r = bridge.post("/api/config/agent", {"custom_tones": {"t9": "be terse"}, "confirm": True})
    assert r.get("ok") is True, r
    assert svc.config.agent.custom_tones == {"t9": "be terse"}
    assert svc.config.policy_generation > gen0, "confirmed security deltas must retire"
    # deep_merge keeps sibling keys by design — a confirmed UPDATE lands;
    # clearing via {} is not the merge contract (existing semantics).
    r = bridge.post("/api/config/agent", {"tone": "default", "op_mode": "build",
                                          "custom_tones": {"t9": "updated"}, "confirm": True})
    assert r.get("ok") is True, r
    assert svc.config.agent.tone == "default"
    assert svc.config.agent.op_mode == "build"
    assert svc.config.agent.custom_tones == {"t9": "updated"}
