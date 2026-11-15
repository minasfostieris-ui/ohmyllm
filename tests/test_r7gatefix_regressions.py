"""R7 gate-fix regressions (task #119, Chris gate blocker 2 — Alex H1).

FAIL-SAFE (permanent user mandate): every destructive payload here is
DECISION-ONLY — PolicyEngine.evaluate / RiskScorer / command_variants verdicts
on strings. No payload is ever executed: no subprocess, no real filesystem
destruction. Paths are disposable fakes (tmp_path).

Root cause (Chris gate run 1, Alex battery leg 27/28): Alex's H1 policy shape
``echo "a\\"; X="rm -rf /"; $X`` (ONE literal backslash) desyncs the R7 S6
bash-parity scanner: the escaped quote keeps the string open, the unclosed
tail merges everything into one inert echo-led segment, and the X assignment
+ $X expansion are never extracted (silent FA risk 6). The product shell
/bin/sh EXECUTES the stub-proven sibling (TWO backslashes, alex_r7_stub.sh:
STUB rm [-rf] [/], .validation/alex-r7sec-preserved/stub_exec.txt). The fix
adds a conservative product-shell fail-closed interpretation pass (odd
backslash run + non-special lead = potential terminator) UNIONED with the
bash-parity baseline — NOT a revert (Terry verdict). Destructive verbs with
unresolvable operands fail closed; non-destructive commands never do.

Reproduce-first: the H1 repros FAIL on the pre-#119 R7 tree; held controls and
non-destructive FP guards pass both before and after (never fail closed).

NB: named test_r7gatefix (not test_r7gate) — Taylor's task #118 owns
tests/test_r7gate_regressions.py (H2 ordering pins) this wave.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ohmyllm.config.models import Config, PermissionMode
from ohmyllm.safety.normalize import (
    _assignment_values_raw,
    _merged_assignments,
    command_variants,
    normalize_command,
    unresolved_destructive_operands,
)
from ohmyllm.safety.policy import PolicyEngine
from ohmyllm.safety.risk import RiskScorer


def _cfg(mode: PermissionMode) -> Config:
    c = Config()
    c.permission_mode = mode
    return c


def _sh(mode: PermissionMode, ws: Path, cmd: str, cwd: Path | None = None,
        deny: list[str] | None = None):
    c = _cfg(mode)
    if deny is not None:
        c.safety.deny = deny
    e = PolicyEngine(c, ws)
    args: dict = {"command": cmd}
    if cwd is not None:
        args["cwd"] = str(cwd)
    return e.evaluate("run_command", e.prepare_args("run_command", args))


FA = PermissionMode.FULL_ACCESS
SB = PermissionMode.SANDBOXED
DA = PermissionMode.DANGEROUS

Q = chr(39)    # single quote
DQ = chr(34)   # double quote
BS = chr(92)   # backslash


def _deny(d, cmd):
    assert not d.allowed, f"{cmd!r} silently allowed: {d.summary()}"
    assert d.risk >= 9, f"{cmd!r} risk {d.risk} < 9"


def _approval(d, cmd):
    assert d.requires_approval, f"{cmd!r} not approval-gated: {d.summary()}"


def _silent(d, cmd):
    assert d.allowed and not d.requires_approval, f"{cmd!r} over-gated: {d.summary()}"


def _never_silent(d, cmd):
    assert not (d.allowed and not d.requires_approval), \
        f"{cmd!r} silent in a non-FA mode: {d.summary()}"


# Alex's EXACT H1 policy-probe shape (ONE literal backslash): bash parity
# keeps the quote open (silent FA risk 6 pre-fix); POSIX /bin/sh closes it
# and executes rm -rf / (stub proof). Must be blocked or approval-gated.
H1 = "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X"
# Alex's stub-exec shape (TWO literal backslashes, alex_r7_stub.sh L25):
# /bin/sh executes it; bash parity already closes the quote (S6 even pin).
H1_STUB = "echo " + DQ + "a" + BS + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X"


# ============================================================ H1 repros
def test_h1_policy_shape_denied_fa(tmp_path):
    _deny(_sh(FA, tmp_path, H1), H1)


def test_h1_stub_shape_denied_fa(tmp_path):
    _deny(_sh(FA, tmp_path, H1_STUB), H1_STUB)


@pytest.mark.parametrize("mode", [SB, DA])
def test_h1_never_silent(tmp_path, mode):
    _never_silent(_sh(mode, tmp_path, H1), H1)
    _never_silent(_sh(mode, tmp_path, H1_STUB), H1_STUB)


def test_h1_deny_floor(tmp_path):
    d = _sh(FA, tmp_path, H1, deny=["rm*"])
    assert not d.allowed, f"H1 escaped the deny floor: {d.summary()}"


# Backslash-run family: EVERY run length before the closing quote exposes the
# assignment under POSIX semantics (odd runs: literal backslash + close; even
# runs: escaped backslash + close). Bash parity only saw the even runs.
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
def test_h1_backslash_run_family(tmp_path, n):
    cmd = "echo " + DQ + "a" + BS * n + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("prefix", ["echo", "printf", "echo -n", "true"])
def test_h1_nonassignment_lead(tmp_path, prefix):
    cmd = prefix + " " + DQ + "a" + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("target", ["/", "/home", "/bin", "/etc"])
def test_h1_targets(tmp_path, target):
    cmd = "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "rm -rf " + target + DQ + "; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "sh -c " + Q + H1 + Q,
    "bash -c " + Q + H1 + Q,
    "eval " + Q + H1 + Q,
    "busybox sh -c " + Q + H1 + Q,
    "echo " + DQ + "a" + BS + DQ + "; export X=" + DQ + "rm -rf /" + DQ + "; $X",
    "echo " + DQ + "a" + BS + DQ + "; X=" + Q + "rm -rf /" + Q + "; $X",
])
def test_h1_wrappers_and_variants(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


# Mixed tails: the POSIX-closed double quote exposes a single-quoted or
# unquoted assignment tail; unclosed-quote debris must fail closed too.
@pytest.mark.parametrize("cmd", [
    # mixed double/single tail (POSIX closes the double quote at \")
    "echo " + DQ + "a" + BS + DQ + "; X=" + Q + "rm -rf /" + Q + "; $X",
    # mixed single-quote lead + H1 double-quote tail: the POSIX/fail-closed
    # read closes the odd-backslash quote, exposing the assignment segment.
    "echo " + Q + "a" + Q + "; echo " + DQ + "b" + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X",
    # genuinely unclosed double-quote tail: the assignment/expansion cannot
    # be resolved under product-shell semantics -> destructive fail closed.
    "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "rm -rf /; $X",
])
def test_h1_mixed_unclosed_tails(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert not (d.allowed and not d.requires_approval), \
        f"{cmd!r} silent catastrophic bypass: {d.summary()}"


# ==================================================== grammar-level proofs
def test_h1_posix_assignment_extraction():
    # bash parity: the quote stays open -> X is NOT a command-position
    # assignment. POSIX pass: the quote closes -> X resolves fully.
    assert _assignment_values_raw(H1) == {}
    assert _assignment_values_raw(H1, posix=True) == {"X": "rm -rf /"}
    merged = _merged_assignments(H1, normalize_command(H1))
    assert merged.get("X") == "rm -rf /"


def test_h1_normalize_resolves_expansion():
    assert "rm -rf /" in normalize_command(H1)
    assert any("rm -rf /" in v for v in command_variants(H1))


def test_h1_unresolved_destructive_fail_closed():
    # H1 itself resolves to a LITERAL rm -rf / (catastrophic-rule lane, see
    # test_h1_policy_shape_denied_fa). When the POSIX-exposed expansion also
    # carries a genuinely unresolvable operand, the fail-closed scan must see
    # it — the bash-parity pass alone sees only the inert echo lead.
    assert unresolved_destructive_operands(H1 + " $UNSET_TARGET")
    # Non-destructive expansion: NEVER fail closed on either pass.
    assert not unresolved_destructive_operands(
        "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "echo hi" + DQ + "; $X")


def test_h1_risk_catastrophic():
    assert RiskScorer().score_command(H1).score >= 9


# ============================================== held controls (bash parity)
# The S6 baseline and single-quote semantics are UNCHANGED by the POSIX pass.
@pytest.mark.parametrize("cmd", [
    "X=" + DQ + "rm -rf /" + DQ + "; $X",                     # R6 E4 baseline
    "export X=" + DQ + "rm -rf /" + DQ + "; $X",              # S6 export
    "echo " + DQ + "a" + BS + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X",
    "echo " + DQ + "a" + BS + DQ + "b" + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X",
    # single-quote tail: backslash is literal in '...' under BOTH shells —
    # the quote closes and the assignment was always visible (held deny).
    "echo " + Q + "a" + BS + Q + "; X=" + Q + "rm -rf /" + Q + "; $X",
])
def test_held_bash_parity_denies(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ======================================= non-destructive controls (NEVER
# fail closed — the POSIX pass must not over-block benign commands).
@pytest.mark.parametrize("cmd", [
    H1.replace("rm -rf /", "hello world"),
    "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "echo hi" + DQ + "; $X",
    "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; echo done",
    "echo " + DQ + "a" + BS + "; X=" + DQ + "hello" + DQ + "; echo $X",
    "echo " + Q + "a" + BS + Q + "; X=" + Q + "hello" + Q + "; echo $X",
    "echo it's fine",                                          # R3 S2q control
    "echo " + DQ + "unterminated",                             # inert echo tail
    "echo " + DQ + "a" + BS + DQ + "; X=" + DQ + "rm -rf /; echo ok",
])
def test_non_destructive_never_fail_closed(tmp_path, cmd):
    _silent(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "IFS=,; X=a,b; echo $X",
    "export X=" + DQ + "echo hi" + DQ + "; $X",
    "rm -rf /home/user/project",
    "rm -rf *.bak",
])
def test_prior_fp_guards_held(tmp_path, cmd):
    _silent(_sh(FA, tmp_path, cmd), cmd)
