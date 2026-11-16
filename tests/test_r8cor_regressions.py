"""R8 correctness regressions — Task #123 (reproduce-first).

Confirmed findings from .validation/grace-r8cor-report.md (HEAD e263d12),
independently re-reproduced by .validation/taylor_r8_probes.py:
  G2 (High) bridge _agent_config: the confirm-gate compares the CURRENT-turn
     effective yolo state, so a no-confirm {yolo:true} echo during an armed
     turn passes and request_yolo defers/arms — a CONSUMED one-prompt grant
     is extended (G2a) or resurrected after a gate->lock settle race (G2b).
  G3 (High) core.apply_config carry->snapshot->fieldcopy: three live reads
     outside turn_lock; a settle landing mid-apply resurrects a consumed arm
     (+ spurious retire). reload_config shares the hole.
  G1 (Med)  TUI settings save rebuilds cfg from model_dump (yolo exclude=True
     -> absent) and applies with no carry: wipes an armed yolo, spurious
     retire, and mid-turn CANCELS the running turn.
  G4 (Low)  _sync_disk_config advances cfg_mtime BEFORE the suppressed reload:
     a failed reload is never retried (silent stale config).

Every defect pin FAILS on pristine e263d12 (G1 pins fall back to the exact
pristine save tail when _commit_settings does not exist yet); controls pin
behavior that must NOT change (F1 stale-echo carry, intentional-delta retire,
legitimate confirmed arms, idle no-change echo, G4 success path).
Guard-compatible: mock backend, ephemeral loopback, temp OMLLM_HOME, bounded
gates, fake-root write targets; no subprocess, no live models.
"""

from __future__ import annotations

import json
import threading
import time
import types

import pytest

from ohmyllm.backends.mock import MockBackend
from ohmyllm.config import Config, deep_merge
from ohmyllm.core import Services


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
    persist triggers _sync_disk_config -> reload_config -> an EXTRA apply,
    poisoning gated call sequences)."""
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
    fired: list = []
    threads: list = []

    def hook(ev, d):
        if ev == "token" and not fired:
            fired.append(True)
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            threads.append(t)

    return hook, fired, threads


def _fire_first(fn):
    """Fire at the FIRST SSE event of any kind (turn start — Grace g1b2)."""
    fired: list = []
    threads: list = []

    def hook(ev, d):
        if not fired:
            fired.append(ev)
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


class _SnapshotGate:
    """Hold the Nth _security_snapshot call of the bridge's Services after
    arming (Grace g3 mechanic, pytest-adapted). call 1 = old_security,
    call 2 = new_security: holding call 2 lands a full turn settle INSIDE the
    carry->copy window on both the pristine and the fixed tree. Bounded."""

    def __init__(self, monkeypatch, svc, hold_on_call: int = 2):
        orig = Services._security_snapshot
        self.armed = False
        self.calls = 0
        self.hold_on_call = hold_on_call
        self.held = threading.Event()
        self.release = threading.Event()
        gate = self

        def snap(cfg):
            if gate.armed:
                gate.calls += 1
                if gate.calls == gate.hold_on_call:
                    gate.held.set()
                    gate.release.wait(25)
            return orig(cfg)

        monkeypatch.setattr(svc, "_security_snapshot", snap, raising=False)

    def restore(self):
        self.armed = False
        self.release.set()


class _LockGate:
    """Hold the FIRST turn_lock acquisition after arming (Grace g2b mechanic):
    the echo passes the pre-lock gate, then blocks gate->lock while the turn
    fully settles. Bounded; later acquisitions pass straight through."""

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
        # post-fix the core snapshots under the same lock — keep them identical
        if hasattr(self.state.svc, "_runtime_lock"):
            self.state.svc._runtime_lock = self.wrapper

    def restore(self):
        self.armed = False
        self.release.set()
        self.state.turn_lock = self.real
        if hasattr(self.state.svc, "_runtime_lock"):
            self.state.svc._runtime_lock = self.real


def _tui_payload(svc) -> dict:
    """The exact settings-screen payload shape (no yolo field exists there)."""
    live = svc.config.model_dump(mode="json")
    a = live["agent"]
    keys = ("max_steps", "tool_timeout", "native_tools", "thinking_visible", "agent_loop", "tone")
    return {"agent": {k: a[k] for k in keys}, "ui": live["ui"]}


def _fake_screen(svc):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(svc=svc, refresh_chrome=lambda: None,
                                  notify=lambda *a, **k: None),
        _show_errors=lambda errs: (_ for _ in ()).throw(AssertionError(f"settings errors: {errs}")),
        _selected_theme=lambda: "electric-cyan",
        action_close=lambda: None,
    )


def _tui_save(svc, payload) -> None:
    """Exercise the REAL TUI save tail. Pre-fix (_commit_settings absent) run
    the exact pristine e263d12 tail so the pin is a genuine falsifier."""
    from ohmyllm.tui.screens.settings import SettingsScreen

    commit = getattr(SettingsScreen, "_commit_settings", None)
    if commit is not None:
        commit(_fake_screen(svc), payload)
    else:
        live = svc.config.model_dump(mode="json")
        svc.apply_config(Config.model_validate(deep_merge(live, payload)))


# ============================================================ G2 (HIGH)
def test_g2a_noconfirm_echo_during_armed_turn_requires_confirm(bridge, oml_env, monkeypatch):
    """G2a: a no-confirm {yolo:true} echo mid armed turn must be 403 — the
    live True is the CURRENT turn's one-prompt grant, already being consumed;
    deferring it arms a prompt the user never confirmed. Pre-fix: 200 +
    yolo_deferred, next prompt auto-approves with ZERO approvals."""
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
        assert out.get("echo", {}).get("__status__") == 403, \
            f"G2a: no-confirm yolo echo during armed turn passed the gate: {out.get('echo')}"
        assert bridge.get("/api/status")["yolo"] is False, "G2a: consumed grant extended"
        _, approvals = _prompt_approvals(bridge, 'create file t8g2a.txt with content "x"',
                                         auto_approve=False)
        if approvals:
            bridge.post(f"/api/approval/{approvals[0]['id']}", {"decision": "deny"})
            _wait_idle(bridge)
        assert approvals, "G2a: next prompt auto-approved a file write with ZERO approvals"
    finally:
        _disarm(bridge)


def test_g2b_gate_to_lock_race_no_resurrection(bridge, oml_env, monkeypatch):
    """G2b: the echo passes the pre-lock gate while idle-armed (True==True),
    blocks on turn_lock, the armed turn fully settles (live False), then the
    echo acquires the lock. The under-lock freshness re-check must 403 —
    pre-fix request_yolo(True, busy=False) resurrects the CONSUMED grant."""
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
        assert out.get("echo", {}).get("__status__") == 403, \
            f"G2b: stale echo re-armed after settle without confirm: {out.get('echo')}"
        assert st.svc.config.agent.yolo is False, "G2b: consumed grant resurrected"
        _, approvals = _prompt_approvals(bridge, 'create file t8g2b.txt with content "x"',
                                         auto_approve=False)
        if approvals:
            bridge.post(f"/api/approval/{approvals[0]['id']}", {"decision": "deny"})
            _wait_idle(bridge)
        assert approvals, "G2b: next prompt auto-approved with ZERO approvals"
    finally:
        gate.restore()
        _disarm(bridge)


def test_g2c_idle_noconfirm_arm_refused(bridge):
    """Control (Grace g2c): idle no-confirm yolo arm stays 403."""
    r = bridge.post("/api/config/agent", {"yolo": True})
    assert r.get("__status__") == 403
    assert bridge.get("/api/status")["yolo"] is False


def test_g2_confirmed_idle_arm_allowed(bridge):
    """Control: a legitimate CONFIRMED idle arm still passes the gate."""
    try:
        r = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
        assert r.get("ok") is True, r
        assert bridge.get("/api/status")["yolo"] is True
    finally:
        _disarm(bridge)


def test_g2_confirmed_busy_arm_defers_and_lands(bridge, oml_env, monkeypatch):
    """Control: a CONFIRMED mid-turn arm still defers (F1), settles True, and
    the next prompt runs with zero approvals — the gate must not over-block."""
    monkeypatch.setattr(MockBackend, "delay", 0.15)
    out: dict = {}
    try:
        hook, fired, threads = _fire_on_token(
            lambda: out.update(arm=bridge.post("/api/config/agent",
                                               {"yolo": True, "confirm": True})))
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        for t in threads:
            t.join(20)
        _wait_idle(bridge)
        assert out.get("arm", {}).get("yolo_deferred") is True, out.get("arm")
        assert bridge.get("/api/status")["yolo"] is True
        _, approvals = _prompt_approvals(bridge, 'create file t8g2ok.txt with content "w"')
        assert not approvals, "confirmed deferred arm did not land"
        assert (oml_env["ws"] / "t8g2ok.txt").exists()
        _wait_idle(bridge)
        assert bridge.get("/api/status")["yolo"] is False  # one-prompt consumed
    finally:
        _disarm(bridge)


def test_g2_idle_armed_noconfirm_echo_still_allowed(bridge):
    """Control: idle live True == requested True is NOT a consumed-grant
    extension — the no-change echo exemption stays confirm-free."""
    try:
        _arm(bridge)
        r = bridge.post("/api/config/agent", {"yolo": True})
        assert r.get("ok") is True, r
        assert bridge.get("/api/status")["yolo"] is True
    finally:
        _disarm(bridge)


# ============================================================ G3 (HIGH)
def _g3_settle_midapply(bridge, oml_env, monkeypatch, fire, fname):
    monkeypatch.setattr(MockBackend, "delay", 0.25)
    st = _state()
    bsvc = st.svc
    gate = _SnapshotGate(monkeypatch, bsvc, hold_on_call=2)
    out: dict = {}
    try:
        _arm(bridge)
        rt = bsvc.runtime()
        gate.armed = True
        hook, fired, threads = _fire_on_token(lambda: fire(bridge, bsvc, out))
        _sse(bridge.post("/api/message", {"text": "hello"}), hook=hook)
        assert fired, "no token"
        assert gate.held.wait(10), "snapshot never gated"
        _wait_state_idle(st)  # turn settles INSIDE the carry->copy window
        assert bsvc.config.agent.yolo is False, "one-prompt arm not consumed"
        gen0 = bsvc.config.policy_generation
        gate.release.set()
        for t in threads:
            t.join(20)
        assert bsvc.config.agent.yolo is False, \
            "G3: settle mid-apply resurrected a CONSUMED one-prompt yolo"
        assert bsvc.config.policy_generation == gen0, "G3: spurious security retire/bump"
        assert not rt._retired, "G3: echo retired the runtime"
        _, approvals = _prompt_approvals(bridge, f'create file {fname} with content "x"',
                                         auto_approve=False)
        if approvals:
            bridge.post(f"/api/approval/{approvals[0]['id']}", {"decision": "deny"})
            _wait_idle(bridge)
        assert approvals, "G3: next prompt auto-approved with ZERO approvals"
    finally:
        gate.restore()
        _disarm(bridge)


def test_g3_echo_settle_midapply_no_resurrection(bridge, oml_env, monkeypatch):
    """G3: marked PATCH echo held between the snapshots while the turn
    settles — the carried pre-settle True must never be written back."""
    _g3_settle_midapply(
        bridge, oml_env, monkeypatch,
        lambda b, svc, out: out.update(patch=b.patch("/api/config",
                                                     {"generation": {"temperature": 0.7}})),
        "t8g3.txt")


def test_g3_reload_settle_midapply_no_resurrection(bridge, oml_env, monkeypatch):
    """G3 reload leg: reload_config pre-carries then applies — the same
    three-instant hole. A disk reload racing turn end must not resurrect."""
    _g3_settle_midapply(
        bridge, oml_env, monkeypatch,
        lambda b, svc, out: out.update(reload=svc.reload_config() is not None),
        "t8g3r.txt")


def test_g3c_idle_echo_preserves_disarm(bridge):
    """Control (Grace g3c): an idle marked echo preserves the disarmed state."""
    gen0 = _state().svc.config.policy_generation
    r = bridge.patch("/api/config", {"generation": {"temperature": 0.7}})
    assert r.get("ok") is True, r
    assert bridge.get("/api/status")["yolo"] is False
    assert _state().svc.config.policy_generation == gen0


def test_g3_stale_marked_echo_still_carries_idle_arm(svc):
    """Control (F1/Y5): a stale marked copy (yolo False) applied after an
    idle arm must still carry the live True — the G3 hardening must not
    break the unified carry contract."""
    svc.request_yolo(True, busy=False)
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    stale = svc.config.model_copy(deep=True)
    stale.agent.yolo = False
    object.__setattr__(stale, "_omllm_carry_runtime", True)
    svc.apply_config(stale)
    assert svc.config.agent.yolo is True, "stale echo wiped an idle arm"
    assert svc.config.policy_generation == gen0
    assert not rt._retired


# ============================================================ G1 (MED)
def test_g1a_tui_save_preserves_idle_arm(svc):
    """G1a: a TUI settings save (no yolo field in the payload) must carry the
    live arm — pre-fix the dump-rebuild wipes it + spurious retire."""
    svc.request_yolo(True, busy=False)
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    _tui_save(svc, _tui_payload(svc))
    assert svc.config.agent.yolo is True, "G1a: TUI save wiped an armed yolo"
    assert svc.effective_yolo() is True
    assert svc.config.policy_generation == gen0, "G1a: spurious security retire"
    assert not rt._retired, "G1a: TUI save retired the runtime"
    assert "_omllm_carry_runtime" not in svc.config.model_dump_json()
    assert "_omllm_carry_runtime" not in svc.config_store.path.read_text()


def test_g1b2_tui_save_midturn_never_cancels(bridge, oml_env, monkeypatch):
    """G1b2: a TUI settings save fired at turn start must NOT cancel the
    running armed turn (C5/F1) — pre-fix: done cancelled + gen bump + wipe."""
    monkeypatch.setattr(MockBackend, "delay", 0.25)
    st = _state()
    bsvc = st.svc
    try:
        _arm(bridge)
        gen0 = bsvc.config.policy_generation
        payload = _tui_payload(bsvc)
        hook, fired, threads = _fire_first(lambda: _tui_save(bsvc, payload))
        resp = bridge.post("/api/message", {"text":
                           'create file t8g1a.txt with content "x" then create file t8g1b.txt with content "y"'})
        ev = _sse(resp, hook=hook)
        assert fired, "no event"
        for t in threads:
            t.join(20)
        _wait_idle(bridge)
        done = [d for e, d in ev if e == "done"]
        assert done, "no done event"
        assert done[0].get("cancelled") is not True and not done[0].get("error"), \
            f"G1b2: TUI settings save CANCELLED the running turn: {done[0]}"
        assert bsvc.config.policy_generation == gen0, "G1b2: spurious retire mid-turn"
        assert bridge.get("/api/status")["yolo"] is False  # one-prompt consumed normally
        # the mock issues ONE file call per prompt (_FILE_RX.search) — the
        # first file proves the turn ran to completion uncancelled
        assert (oml_env["ws"] / "t8g1a.txt").exists()
    finally:
        _disarm(bridge)


def test_g1c_bridge_echo_preserves_arm_no_marker_leak(bridge):
    """Control (Grace g1c): the bridge parity path keeps the arm; the carry
    marker never leaks to disk or dump."""
    try:
        _arm(bridge)
        live = bridge.get("/api/config")
        r = bridge.patch("/api/config", {"agent": live["agent"], "ui": live["ui"]})
        assert r.get("ok") is True, r
        assert bridge.get("/api/status")["yolo"] is True
        bsvc = _state().svc
        assert "_omllm_carry_runtime" not in bsvc.config_store.path.read_text()
        assert "_omllm_carry_runtime" not in bsvc.config.model_dump_json()
    finally:
        _disarm(bridge)


# ============================================================ G4 (LOW)
def _g4_fake_state(tmp_path, reload):
    from ohmyllm import bridge as B

    p = tmp_path / "t8-g4-config.toml"
    p.write_text("version = 1\n")
    import os

    os.utime(p, (200, 200))
    fake = types.SimpleNamespace(state=types.SimpleNamespace(
        svc=types.SimpleNamespace(config_store=types.SimpleNamespace(path=p),
                                  reload_config=reload),
        cfg_mtime=100.0))
    return B, fake


def test_g4_failed_reload_retried_until_success(tmp_path):
    """G4: a failed reload must NOT advance cfg_mtime — the next status poll
    retries while the file is unchanged. Pre-fix: 1 attempt, mtime advanced."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient load failure")

    B, fake = _g4_fake_state(tmp_path, flaky)
    for _ in range(4):
        B.Handler._sync_disk_config(fake)
    assert len(calls) == 3, f"G4: failed reload never retried (attempts={len(calls)})"
    assert fake.state.cfg_mtime == 200.0, "mtime must advance after the SUCCESSFUL reload"


def test_g4_success_advances_mtime_once(tmp_path):
    """Control: a successful reload advances mtime; unchanged file -> no re-reload."""
    calls = []
    B, fake = _g4_fake_state(tmp_path, lambda: calls.append(1))
    B.Handler._sync_disk_config(fake)
    B.Handler._sync_disk_config(fake)
    assert len(calls) == 1
    assert fake.state.cfg_mtime == 200.0


# ============================================================ cross-cutting control
def test_ctl_intentional_yolo_delta_still_retires(svc):
    """Control (R1-H2 / R3-C5): a DIRECT intentional yolo delta (no marker,
    carry default False) must still retire runtimes and apply — the G3
    atomic-carry hardening must never leak into the intentional path."""
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    n = svc.config.model_copy(deep=True)
    n.agent.yolo = True
    svc.apply_config(n)
    assert rt._retired, "intentional yolo delta no longer retires"
    assert svc.config.agent.yolo is True
    assert svc.config.policy_generation > gen0
