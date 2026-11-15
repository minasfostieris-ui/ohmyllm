"""R7 gate blocker 1 regressions — Task #118 (ordering contract).

Pins the ORDERING between the security-snapshot retirement compare and the R7
carry-live of runtime-only flags (agent.yolo) in Services.apply_config, so the
Task #111 fix and the R1-H2 / R3-C5 retirement pins can never regress silently:

  * INTENTIONAL yolo delta (direct caller, carry_runtime=False default): the
    incoming delta MUST be visible to _security_snapshot -> retire live runtimes
    AND apply the incoming yolo. The #111 UNCONDITIONAL carry ran before the
    compare and hid this delta (gate run 1 FAIL: H2 + C5). Making the carry
    unconditional again re-breaks these tests.
  * Runtime ECHO (bridge config-mutation endpoint, carry_runtime=True): the LIVE
    yolo MUST win at apply time so a stale in-flight copy never resurrects a
    CONSUMED one-prompt arm (R7-Y1 fail-open) nor wipes a SETTLED/idle arm
    (R7-Y2/Y3/Y5), and a pure echo never spuriously retires (R3-C5 / F1).
    Removing the carry re-opens Y1/Y2/Y3/Y5.

test_retirement_then_carry_ordering_together pins BOTH directions in one
sequence (intentional retire -> consume -> stale echo no-resurrect -> intentional
retire again). The bridge wiring (endpoints pass carry_runtime=True) is pinned
end-to-end by tests/test_r7cor_regressions.py (Y1/Y2/Y3/Y5 gated repros).

Guard-compatible: no subprocess, temp OMLLM_HOME (oml_env/svc fixtures), fake
runtimes, no live model calls, no real payloads.
"""

from __future__ import annotations

from ohmyllm.config import Config


# ----------------------------------------------------- intentional delta path
def test_intentional_yolo_arm_retires_and_applies(svc):
    """Default carry_runtime=False: a fresh yolo arm is an intentional security
    delta — the snapshot must see it, retire the runtime, and APPLY the arm."""
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    assert svc.config.agent.yolo is False
    n = Config()
    n.agent.yolo = True
    svc.apply_config(n)  # default: NO runtime carry
    assert rt._retired is True, "intentional yolo delta must retire (snapshot must see it)"
    assert svc.config.agent.yolo is True, "intentional arm must be applied, not carried away"
    assert svc.config.policy_generation > gen0


def test_intentional_yolo_disarm_retires_and_applies(svc):
    """Symmetry: an intentional True->False disarm is also a security delta."""
    svc.config.agent.yolo = True
    rt = svc.runtime()
    n = Config()
    n.agent.yolo = False
    svc.apply_config(n)
    assert rt._retired is True
    assert svc.config.agent.yolo is False


def test_cosmetic_delta_does_not_retire(svc):
    """R3-C5: a non-security delta applies in place, no retire, no gen bump."""
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    cfg = svc.config.model_copy(deep=True)
    cfg.ui.theme = "gate-cosmetic"
    cfg.generation.temperature = 0.7
    svc.apply_config(cfg)
    assert rt._retired is False
    assert svc.config.policy_generation == gen0
    assert cfg.ui.theme == "gate-cosmetic"
    assert svc.config.generation.temperature == 0.7


# ----------------------------------------------------------- runtime echo path
def test_cosmetic_echo_does_not_retire(svc):
    """A bridge echo (carry_runtime=True) with no yolo change must not retire."""
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    cfg = svc.config.model_copy(deep=True)
    cfg.ui.theme = "gate-echo"
    cfg.generation.temperature = 0.6
    svc.apply_config(cfg, carry_runtime=True)
    assert rt._retired is False
    assert svc.config.policy_generation == gen0
    assert svc.config.ui.theme == "gate-echo"


def test_stale_echo_no_resurrection_and_no_spurious_retire(svc):
    """R7-Y1: a stale echo of a CONSUMED arm must not resurrect it, and a pure
    echo must not spuriously retire (the carry precedes the snapshot compare)."""
    svc.config.agent.yolo = True   # armed
    svc.config.agent.yolo = False  # one-prompt consumption (turn ended)
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    stale = svc.config.model_copy(deep=True)
    stale.agent.yolo = True        # stale echo of the pre-consumption arm
    stale.ui.theme = "gate-stale"
    svc.apply_config(stale, carry_runtime=True)
    assert svc.config.agent.yolo is False, "R7-Y1: stale echo resurrected a CONSUMED arm"
    assert rt._retired is False, "a pure runtime echo must not spuriously retire"
    assert svc.config.policy_generation == gen0
    assert svc.config.ui.theme == "gate-stale"  # cosmetic delta still applied


def test_stale_echo_preserves_live_arm(svc):
    """R7-Y2/Y3/Y5: a stale echo from before an arm must not wipe the live arm
    (settled deferred arm or idle arm)."""
    svc.config.agent.yolo = True  # settled / idle arm
    rt = svc.runtime()
    stale = svc.config.model_copy(deep=True)
    stale.agent.yolo = False      # stale echo from before the arm
    svc.apply_config(stale, carry_runtime=True)
    assert svc.config.agent.yolo is True, "R7-Y2/Y3/Y5: stale echo wiped a live arm"
    assert rt._retired is False


def test_reload_noop_preserves_arm_and_does_not_retire(svc):
    """R3-U3 + C5: reload_config pre-carries the live yolo (excluded from disk),
    so a no-op reload applies the live value -> no delta -> no retire."""
    svc.config.agent.yolo = True
    svc.save_config()
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    svc.reload_config()
    assert svc.config.agent.yolo is True, "reload wiped the live runtime arm"
    assert rt._retired is False
    assert svc.config.policy_generation == gen0


# ----------------------------------------------- THE ordering pin (both ways)
def test_retirement_then_carry_ordering_together(svc):
    """Intentional retire -> consume -> stale echo no-resurrect -> intentional
    retire again. Fails if the carry becomes unconditional (step 1/4 hidden) OR
    if the carry is removed (step 3 resurrects). Pins the ordering permanently."""
    # 1) intentional arm retires + applies (H2/C5 direction)
    rt1 = svc.runtime()
    n = Config()
    n.agent.yolo = True
    svc.apply_config(n)
    assert rt1._retired is True and svc.config.agent.yolo is True
    # 2) one-prompt consumption disarms the live flag
    svc.config.agent.yolo = False
    # 3) a stale echo of the arm must NOT resurrect (Y1) nor retire the echo
    rt2 = svc.runtime()
    stale = svc.config.model_copy(deep=True)
    stale.agent.yolo = True
    svc.apply_config(stale, carry_runtime=True)
    assert svc.config.agent.yolo is False, "ordering regressed: stale echo resurrected"
    assert rt2._retired is False, "ordering regressed: pure echo spuriously retired"
    # 4) a fresh intentional delta STILL retires after the echo (snapshot intact)
    rt3 = svc.runtime()
    n2 = Config()
    n2.agent.yolo = True
    svc.apply_config(n2)
    assert rt3._retired is True and svc.config.agent.yolo is True


def test_marker_echo_carries_live_positional(svc):
    """The bridge marks the cfg (_omllm_carry_runtime) and calls apply_config
    POSITIONALLY so duck-typed fakes (lambda n: ...) keep working. The marker
    must trigger the live carry exactly like the kwarg — pins the mechanism
    that keeps test_r1sec H2's fake apply_config compatible (gate blocker 1)."""
    svc.config.agent.yolo = False  # consumed one-prompt arm
    rt = svc.runtime()
    stale = svc.config.model_copy(deep=True)
    stale.agent.yolo = True        # stale echo of the pre-consumption arm
    object.__setattr__(stale, "_omllm_carry_runtime", True)
    svc.apply_config(stale)        # positional, no kwarg — exactly like the bridge
    assert svc.config.agent.yolo is False, "marker echo resurrected a CONSUMED arm"
    assert rt._retired is False, "marker echo must not spuriously retire"
    # the marker never leaks into the serialized config (protected compare safe)
    import json
    assert "_omllm_carry_runtime" not in json.loads(stale.model_dump_json())
