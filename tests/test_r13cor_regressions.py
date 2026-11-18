"""R13 correctness regressions — Task #159 (reproduce-first).

Jude's R13 LOW, pre-existing (.validation/r13-merged-report.md L48-52, HEAD
9439cd1), independently re-reproduced by .validation/taylor_r13_probes.py
(3 CONFIRMED + 5 controls):
  tools/registry.py _tone_create mutated agent.custom_tones/tone then called
  ctx.config_saver() (a PLAIN save) — an injected IO failure returned "tone
  created but config could not be saved" with the tone armed live while disk
  stayed stale, outside the R12 M8 rollback path. The mutator also ignored
  config_mutex (ToolContext had no cm seam).

Fix: ToolContext gains optional config_mutex + config_rollback_saver seams,
wired by Services.runtime() through AgentRuntime into the turn ctx;
_tone_create keeps ALL validation first (H3 zero-side-effect law), then under
config_mutex (NEVER turn_lock) snapshots pre-mutation and persists via the
shared Services._persist_with_rollback; unwired ctxs fall back to config_saver
with the same non-exclude restore of the two touched fields. Runtime exclude
flags are NEVER restored; no new retire (the tool path never retired).

4 falsifiers FAIL on pristine 9439cd1; 4 controls pin behavior that must NOT
change. NEW file (zero diff on existing tests). Guard-compatible: mock
backend, temp OMLLM_HOME, deterministic failure injection + held-lock gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading

from ohmyllm.config.models import Config, PermissionMode
from ohmyllm.config.store import ConfigStore
from ohmyllm.tools import build_default_registry
from ohmyllm.tools.spec import ToolContext

FRAG = "Reply as a 18th-century pirate: nautical metaphors and arr, but technically precise."
ARGS = {"name": "pirate", "instructions": FRAG}


# ------------------------------------------------------------------ helpers
def _disk(svc) -> Config:
    return ConfigStore(svc.config_store.path).load()


def _ctx(svc, saver, wired: bool = True) -> ToolContext:
    """Introspect ToolContext so the SAME pin runs pre-fix (plain saver) and
    post-fix (wired cm + rollback seam) — no TypeError masking the falsifier."""
    svc.config.agent.op_mode = "build"
    kw = {"workspace": svc.workspace, "mode": svc.config.permission_mode.value,
          "config": svc.config, "config_saver": saver}
    if wired:
        names = {f.name for f in dataclasses.fields(ToolContext)}
        if "config_mutex" in names:
            kw["config_mutex"] = getattr(svc, "_config_mutex", None)
        if "config_rollback_saver" in names:
            kw["config_rollback_saver"] = svc._persist_with_rollback
    return ToolContext(**kw)


def _tone(svc, ctx, args=None):
    return asyncio.run(build_default_registry().execute("tone_create", args or ARGS, ctx))


def _fail_save(monkeypatch, svc, always=True):
    orig = svc.save_config
    boom = {"n": 0}

    def failing():
        boom["n"] += 1
        if always or boom["n"] == 1:
            raise OSError("t159: injected disk failure")
        return orig()

    monkeypatch.setattr(svc, "save_config", failing)
    return boom


# ============================================================ falsifiers
def test_tone_failed_save_rolls_back_wired(svc, monkeypatch):
    """Wired path: failed persist must not leave the tone armed live-only."""
    _fail_save(monkeypatch, svc)
    res = _tone(svc, _ctx(svc, svc.save_config))
    assert res.ok is False, res
    assert "could not be saved" in (res.error or ""), res
    assert svc.config.agent.tone == "default", "failed save left live tone armed"
    assert "pirate" not in svc.config.agent.custom_tones
    d = _disk(svc)
    assert d.agent.tone == "default" and "pirate" not in d.agent.custom_tones


def test_tone_failed_save_rolls_back_plain(svc, monkeypatch):
    """Unwired ctx (plain config_saver only): same rollback law via fallback."""
    _fail_save(monkeypatch, svc)
    res = _tone(svc, _ctx(svc, svc.save_config, wired=False))
    assert res.ok is False, res
    assert svc.config.agent.tone == "default"
    assert "pirate" not in svc.config.agent.custom_tones
    d = _disk(svc)
    assert d.agent.tone == "default" and "pirate" not in d.agent.custom_tones


def test_tone_ctx_wiring_from_services(svc):
    """Services.runtime() must wire the cm + shared rollback seam into the runtime."""
    names = {f.name for f in dataclasses.fields(ToolContext)}
    assert "config_mutex" in names and "config_rollback_saver" in names, \
        "ToolContext missing the R13 seams"
    rt = svc.runtime()
    assert rt.config_rollback_saver == svc._persist_with_rollback
    assert rt.config_mutex is getattr(svc, "_config_mutex", None)


def test_tone_serializes_under_config_mutex(svc):
    """While config_mutex is HELD, tone_create must not complete (cm law)."""
    lock = threading.RLock()
    svc._config_mutex = lock
    ctx = _ctx(svc, svc.save_config)
    done: list = []

    def worker():
        res = asyncio.run(build_default_registry().execute("tone_create", ARGS, ctx))
        done.append(res.ok)

    lock.acquire()
    th = threading.Thread(target=worker, daemon=True)
    try:
        th.start()
        th.join(timeout=0.5)
        assert th.is_alive(), "tone_create ran while config_mutex was held"
    finally:
        lock.release()
    th.join(timeout=10)
    assert not th.is_alive(), "worker never finished after release"
    assert done == [True], done
    assert not lock._is_owned()


# ============================================================== controls
def test_tone_benign_persists_live_eq_disk(svc, monkeypatch):
    calls: list = []
    orig = svc.save_config

    def counting():
        calls.append(1)
        return orig()

    # Count the svc-level saver: the wired path persists via the shared helper
    # (svc.save_config), the unwired fallback via ctx.config_saver — same object here.
    monkeypatch.setattr(svc, "save_config", counting)
    res = _tone(svc, _ctx(svc, counting))
    assert res.ok is True, res
    assert calls == [1], calls
    assert svc.config.agent.tone == _disk(svc).agent.tone == "pirate"
    assert "pirate" in _disk(svc).agent.custom_tones


def test_tone_runtime_yolo_never_restored(svc, monkeypatch):
    svc.config.agent.yolo = True  # live-only runtime flag (exclude=True)
    _fail_save(monkeypatch, svc)
    res = _tone(svc, _ctx(svc, svc.save_config))
    assert res.ok is False, res
    assert svc.config.agent.yolo is True, "rollback restored a runtime flag"


def test_tone_validation_zero_side_effects(svc):
    calls: list = []

    def tripwire():
        calls.append(1)
        raise AssertionError("saver called on a rejected tone_create")

    ctx = _ctx(svc, tripwire)
    for args in ({"name": "Default", "instructions": FRAG},
                 {"name": "chill", "instructions": FRAG},
                 {"name": "cool", "instructions": "too short"}):
        assert _tone(svc, ctx, args).ok is False, args
    svc.config.agent.op_mode = "chat"
    res = _tone(svc, ctx)
    assert res.ok is False and "/build" in res.error, res
    svc.config.agent.op_mode = "build"
    svc.config.permission_mode = PermissionMode.STRICT
    assert _tone(svc, _ctx(svc, tripwire)).ok is False
    assert calls == [], calls
    assert svc.config.agent.tone == "default"
    assert svc.config.agent.custom_tones == {}


def test_tone_retry_after_failure_converges(svc, monkeypatch):
    _fail_save(monkeypatch, svc, always=False)
    ctx = _ctx(svc, svc.save_config)
    r1 = _tone(svc, ctx)
    assert r1.ok is False, r1
    r2 = _tone(svc, ctx)
    assert r2.ok is True, r2
    assert svc.config.agent.tone == _disk(svc).agent.tone == "pirate"
    assert "pirate" in _disk(svc).agent.custom_tones
