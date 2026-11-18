"""R12 correctness regressions — Task #150 (reproduce-first).

Miles's R11 C5 LOW (.validation/r11cor-report.md, HEAD 6d58614 = origin/main;
repro .validation/r11cor/c5-m8-residue-direct-save-paths.json), independently
re-reproduced by .validation/taylor_r12_probes.py (6 CONFIRMED + 6 controls):
  M8 failed-save snapshot/rollback lived ONLY in core._apply_config_inner.
  core.set_mode + bridge providers key/use/forget/custom mutated live THEN
  called save_config() directly — an injected IO failure returned 500 with
  live!=disk (mode live strict / disk sandboxed; key live set / disk null;
  use live cloud / disk mock; forget live gone / disk present; custom live
  present / disk absent; policy_generation bumped live-only).

Fix: ONE shared Service._persist_with_rollback(pre_apply) helper (the exact
R10 M8 non-exclude restore loop) used by _apply_config_inner, set_mode and
all four provider mutators. Laws: runtime exclude flags NEVER restored;
retire/invalidate stay fail-closed (pre-save, never un-retired);
policy_generation rolls back with the snapshot; config_mutex routing
unchanged (NEVER turn_lock); apply stays outside turn_lock.

Pins live in this NEW file (not appended to r10cor) so the zero-diff law on
existing tests is trivially auditable. 6 falsifiers FAIL on pristine
6d58614; 4 controls pin behavior that must NOT change. Guard-compatible:
mock backend, ephemeral loopback, temp OMLLM_HOME; failure injection is
deterministic (no races, no sleeps).
"""

from __future__ import annotations

import contextlib

import pytest

from ohmyllm.config.models import Config
from ohmyllm.config.store import ConfigStore

# ------------------------------------------------------------------ helpers
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


def _fail_save(monkeypatch, svc, always=True):
    """Inject an IO failure at the persist point (svc.save_config)."""
    orig = svc.save_config
    boom = {"n": 0}

    def failing_save():
        boom["n"] += 1
        if always or boom["n"] == 1:
            raise OSError("t150: injected disk failure")
        return orig()

    monkeypatch.setattr(svc, "save_config", failing_save)
    return boom


# ============================================================ C5 falsifiers
def test_c5_set_mode_failed_save_rolls_back(bridge, monkeypatch):
    """set_mode: a failed persist must not leave live strict / disk sandboxed."""
    st = _state()
    svc = st.svc
    _fail_save(monkeypatch, svc, always=False)  # fail once, retry succeeds
    r = bridge.post("/api/mode", {"mode": "strict"})
    assert r.get("__status__") == 500, r
    assert not st.config_mutex._is_owned(), "config mutex left held"
    assert not st.turn_lock._is_owned(), "turn lock left held"
    assert svc.config.permission_mode.value == "sandboxed", \
        "failed save left live mode flipped"
    assert _disk(svc).permission_mode.value == "sandboxed"
    r2 = bridge.post("/api/mode", {"mode": "strict"})
    assert r2.get("ok") is True, r2
    assert svc.config.permission_mode.value == "strict"
    assert _disk(svc).permission_mode.value == "strict"


def test_c5_policy_generation_rolls_back_with_snapshot(bridge, monkeypatch):
    """The gen bump is part of the snapshot: a failed save restores it."""
    st = _state()
    svc = st.svc
    gen0 = svc.config.policy_generation
    assert _disk(svc).policy_generation == gen0
    _fail_save(monkeypatch, svc)
    r = bridge.post("/api/mode", {"mode": "strict"})
    assert r.get("__status__") == 500, r
    assert svc.config.policy_generation == gen0, "gen residue after failed save"
    assert _disk(svc).policy_generation == gen0


def test_c5_providers_key_failed_save_rolls_back(bridge, monkeypatch):
    """providers/key: live key set / disk null is the Miles repro shape."""
    st = _state()
    svc = st.svc
    _fail_save(monkeypatch, svc, always=False)
    r = bridge.post("/api/providers/key", {"pid": "openrouter", "key": "sk-t150-K"})
    assert r.get("__status__") == 500, r
    assert not st.config_mutex._is_owned(), "config mutex left held"
    assert svc.config.cloud.keys.get("openrouter") is None, \
        "failed save left live key set"
    assert _disk(svc).cloud.keys.get("openrouter") is None
    assert svc.config.cloud.provider == _disk(svc).cloud.provider
    r2 = bridge.post("/api/providers/key", {"pid": "openrouter", "key": "sk-t150-K"})
    assert r2.get("__status__") is None, r2
    assert svc.config.cloud.keys.get("openrouter") == "sk-t150-K"
    assert _disk(svc).cloud.keys.get("openrouter") == "sk-t150-K"


def test_c5_providers_use_failed_save_rolls_back(bridge, monkeypatch):
    """providers/use: live cloud / disk mock must roll back to the disk state."""
    st = _state()
    svc = st.svc
    r0 = bridge.post("/api/providers/key", {"pid": "groq", "key": "sk-t150-U"})
    assert r0.get("__status__") is None, r0
    _fail_save(monkeypatch, svc)
    r = bridge.post("/api/providers/use", {"pid": "groq"})
    assert r.get("__status__") == 500, r
    assert svc.config.default_backend.value == _disk(svc).default_backend.value == "mock", \
        "failed use left live backend flipped"
    assert svc.config.cloud.provider == _disk(svc).cloud.provider


def test_c5_providers_forget_failed_save_rolls_back(bridge, monkeypatch):
    """providers/forget: a failed save must not drop the key live-only."""
    st = _state()
    svc = st.svc
    r0 = bridge.post("/api/providers/key", {"pid": "groq", "key": "sk-t150-F"})
    assert r0.get("__status__") is None, r0
    _fail_save(monkeypatch, svc)
    r = bridge.post("/api/providers/forget", {"pid": "groq"})
    assert r.get("__status__") == 500, r
    assert svc.config.cloud.keys.get("groq") == "sk-t150-F", \
        "failed forget dropped the live key"
    assert _disk(svc).cloud.keys.get("groq") == "sk-t150-F"
    assert svc.config.cloud.provider == _disk(svc).cloud.provider


def test_c5_providers_custom_failed_save_rolls_back(bridge, monkeypatch):
    """providers/custom: a failed save must not leave a live-only custom pid."""
    st = _state()
    svc = st.svc
    _fail_save(monkeypatch, svc)
    r = bridge.post("/api/providers/custom",
                    {"name": "T150", "base_url": "http://127.0.0.1:9/v1"})
    assert r.get("__status__") == 500, r
    assert len(svc.config.cloud.customs) == 0, "failed custom left live pid"
    assert len(svc.config.cloud.bases) == 0
    d = _disk(svc)
    assert len(d.cloud.customs) == 0 and len(d.cloud.bases) == 0


# =============================================================== C5 controls
def test_c5_runtime_yolo_never_restored(bridge, monkeypatch):
    """Rollback restores non-exclude fields ONLY — a live runtime flag stays."""
    st = _state()
    svc = st.svc
    svc.config.agent.yolo = True  # live-only (exclude=True), never on disk
    _fail_save(monkeypatch, svc)
    r = bridge.post("/api/providers/key", {"pid": "openrouter", "key": "sk-t150-Y"})
    assert r.get("__status__") == 500, r
    assert svc.config.agent.yolo is True, "rollback restored a runtime flag"


def test_c5_failed_mode_save_still_retires(bridge, monkeypatch):
    """Fail-closed: the mode delta retires BEFORE save; rollback never un-retires."""
    st = _state()
    svc = st.svc
    rc = _RetireCounter(svc)
    rc.install()
    try:
        _fail_save(monkeypatch, svc)
        r = bridge.post("/api/mode", {"mode": "strict"})
        assert r.get("__status__") == 500, r
        assert rc.n >= 1, "mode change did not retire on failed save"
    finally:
        rc.restore()


def test_c5_benign_provider_saves_live_eq_disk(bridge):
    """No failure: all four mutators persist and live==disk."""
    st = _state()
    svc = st.svc
    r1 = bridge.post("/api/providers/key", {"pid": "groq", "key": "sk-t150-B"})
    assert r1.get("__status__") is None, r1
    r2 = bridge.post("/api/providers/use", {"pid": "groq"})
    assert r2.get("__status__") is None, r2
    r3 = bridge.post("/api/providers/custom",
                     {"name": "B", "base_url": "http://127.0.0.1:9/v1"})
    assert r3.get("__status__") is None, r3
    r4 = bridge.post("/api/providers/forget", {"pid": "groq"})
    assert r4.get("__status__") is None, r4
    d = _disk(svc)
    assert svc.config.cloud.keys.get("groq") == d.cloud.keys.get("groq") is None
    assert svc.config.default_backend == d.default_backend
    assert svc.config.cloud.customs == d.cloud.customs
    assert len(d.cloud.customs) == 1


def test_c5_apply_path_m8_rollback_intact(bridge, monkeypatch):
    """Refactor guard: _apply_config_inner keeps the exact R10 M8 semantics."""
    st = _state()
    svc = st.svc
    _fail_save(monkeypatch, svc, always=False)
    r = bridge.post("/api/config/memory", {"enabled": False})
    assert r.get("__status__") == 500, r
    assert not st.config_mutex._is_owned()
    assert not st.turn_lock._is_owned()
    assert svc.config.memory.enabled is True, "apply-path rollback broke"
    assert _disk(svc).memory.enabled is True
    r2 = bridge.post("/api/config/memory", {"enabled": False})
    assert r2.get("ok") is True, r2
    assert svc.config.memory.enabled is False
    assert _disk(svc).memory.enabled is False
