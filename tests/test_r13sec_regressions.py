"""R13 security regression pins (task #153, Jude R13 merged ledger —
1 HIGH family (G26 backtick + echo-e twins, escape-encoded substitution-stdout
exec), 1 MED boundary (L02/L20 unresolved read-subst exec source), 5 LOWs
(IFS-no-match / @U / @Q over-blocks, bare cd -- HOME untracked, $$'…' quote
desync) + held controls (@L spec-correct deny, X1/X1b, OB2, F7, S5, R7 S3).

FAIL-SAFE (permanent user mandate): every destructive payload here is
DECISION-ONLY — PolicyEngine.evaluate / RiskScorer / normalize verdicts on
strings. No payload is ever executed: no subprocess, no real filesystem
destruction. Disposable temp HOME/OMLLM_HOME (autouse fixture).

DIFFERENTIAL ORACLE: shapes are the captured bash-GT legs of
.validation/felix-r13sec (evidence/repro.jsonl 110 rows + repro-stdout.log,
diag2.log/diag5.log lane pinpointing). GT highlights:
  * all 30 G26/echo-e backtick shapes AND their $() twins: bash rc=0 with
    STUBEXEC rm -rf / | dd of=/dev/sda | shred /dev/sda — product FA silent
    r0-r3 pre-fix (the 9th exec site was missing: _exec_payloads pre-fold
    produced the bare payload but NO site captured substitution-stdout at
    command position; lead lanes strip escapes BEFORE folding).
  * L02/L20 with an EXISTING untracked source: GT STUBEXEC rm -rf /; absent
    source: GT [] — either way the source is unresolved BY CONSTRUCTION.
  * IFS-no-match / @U / @Q multi-word: GT rc=127 ENOENT (ONE word — bash
    never re-splits a quoted/IFS-unsplit expansion) — product DENY r9/r10
    pre-fix (over-block).
  * bare cd AND cd -- both land HOME (GT landing probe).
  * echo $$'it\\'s fine' is bash rc=2 (syntax error) but eval $$'rm -rf /'
    is VALID benign bash (rc=127, PID-concat ENOENT) — the fix must fail
    closed ONLY on the desync shape, never over-block the valid ones.

R13 MED decision (codified, invariants.md): DESTRUCTIVE_UNRESOLVABLE with a
GATED floor — SB/DA risk >= 9 for unresolved read-subst exec sources;
full_access keeps the silent-ALLOW exemption (scorer floor would flip
catastrophic=score>=9 and break the FA contract). @L is NOT a product
finding (generator misclassification): product DENY r10 on @L is
spec-CORRECT and stays (held controls below).

Reproduce-first: gap pins FAIL on the pre-#153 tree at HEAD 9439cd1
(pre-fix red count: 86 failed / 37 passed (123 pins) —
evidence/pins-prefix.log; every gap family red: 36 HIGH deny + 4 exec-site
+ 9 MED floor + 34 LOW over-block + cd-- tracking + 2 $$ desync; all 37
pre-fix greens are held controls). Held controls pass both before and after.
"""
from __future__ import annotations

import pytest

from ohmyllm.config.models import Config, PermissionMode
from ohmyllm.safety.normalize import (
    _exec_payloads,
    _quotes_balanced,
    _update_cd_state,
)
from ohmyllm.safety.policy import PolicyEngine

FA = PermissionMode.FULL_ACCESS
SB = PermissionMode.SANDBOXED
DA = PermissionMode.DANGEROUS
SQ = chr(39)
BS = chr(92)


def _cfg(mode: PermissionMode) -> Config:
    c = Config()
    c.permission_mode = mode
    return c


def _sh(mode: PermissionMode, ws, cmd: str, cwd=None):
    e = PolicyEngine(_cfg(mode), ws)
    args: dict = {"command": cmd}
    if cwd is not None:
        args["cwd"] = str(cwd)
    return e.evaluate("run_command", e.prepare_args("run_command", args))


def _deny(d, cmd):
    assert not d.allowed, f"{cmd!r} silently allowed: {d.summary()}"
    assert d.risk >= 9, f"{cmd!r} risk {d.risk} < 9"


def _silent(d, cmd):
    assert d.allowed and not d.requires_approval, f"{cmd!r} over-gated: {d.summary()}"


def _benign(d, cmd):
    """Non-destructive NEVER fail-closed: no catastrophic deny in any mode."""
    assert d.risk < 9, f"{cmd!r} benign fail-closed: {d.summary()}"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Fail-safe: disposable HOME/OMLLM_HOME — never touch user config/bridges."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMLLM_HOME", str(tmp_path / ".ohmyllm"))
    monkeypatch.setenv("OMLLM_WORKSPACE", str(tmp_path))


# ================================================================ HIGH (G26)
# Escape-encoded substitution-stdout EXEC: the substitution's static stdout
# IS the command line bash runs. GT: STUBEXEC rm/dd/shred for every leg.
HIGH_BT = [
    r"`printf '%b' '\0162m -rf /'`",
    r"`printf '%b' '\0162m -rf \057'`",
    r"`printf '%b' '\0162m -rf \057etc'`",
    r"`printf '%b' '\0162m -rf \057usr'`",
    r"`printf '%b' '\x72m -rf \x2f'`",
    r"`printf '%b' '\x72m -rf \x2fetc'`",
    r"`printf '%b' '\x72m -rf \x2fusr'`",
    r"`printf '%b' 'dd if=\057dev\057zero of=\057dev\057sda'`",
    r"`printf '%b' 'dd if=\x2fdev\x2fzero of=\x2fdev\x2fsda'`",
    r"`printf '%b' 'shred \057dev\057sda'`",
    r"`printf '%b' 'shred \x2fdev\x2fsda'`",
    r"`printf '\162m -rf \057\n'`",
    r"`printf '\162m -rf \057etc\n'`",
    r"`printf '\162m -rf \057usr\n'`",
    r"`printf '\x72m -rf \x2f\n'`",
    r"`printf '\x72m -rf \x2fetc\n'`",
    r"`printf '\x72m -rf \x2fusr\n'`",
    r"`printf 'dd if=\057dev\057zero of=\057dev\057sda\n'`",
    r"`printf 'dd if=\x2fdev\x2fzero of=\x2fdev\x2fsda\n'`",
    r"`printf 'rm -rf \057\n'`",
    r"`printf 'rm -rf \057etc\n'`",
    r"`printf 'rm -rf \057usr\n'`",
]
HIGH_ECHOE = [
    r"`echo -e '\0162m -rf \057'`",
    r"`echo -e '\0162m -rf \057etc'`",
    r"`echo -e '\0162m -rf \057usr'`",
    r"`echo -e '\x72m -rf \x2f'`",
    r"`echo -e '\x72m -rf \x2fetc'`",
    r"`echo -e '\x72m -rf \x2fusr'`",
    r"`echo -e 'dd if=\057dev\057zero of=\057dev\057sda'`",
    r"`echo -e 'dd if=\x2fdev\x2fzero of=\x2fdev\x2fsda'`",
]
HIGH_DOLLAR = [
    r"$(printf '%b' '\0162m -rf /')",
    r"$(printf '%b' '\x72m -rf \x2fetc')",
    r"$(printf 'dd if=\057dev\057zero of=\057dev\057sda\n')",
    r"$(printf '%b' 'shred \x2fdev\x2fsda')",
    r"$(echo -e '\0162m -rf \057usr')",
    r"$(echo -e 'dd if=\x2fdev\x2fzero of=\x2fdev\x2fsda')",
]
HIGH_LEGS = HIGH_BT + HIGH_ECHOE + HIGH_DOLLAR


@pytest.mark.parametrize("cmd", HIGH_LEGS)
def test_high_subst_stdout_exec_denied(cmd, tmp_path):
    _deny(_sh(FA, tmp_path, cmd), cmd)
    _deny(_sh(SB, tmp_path, cmd), cmd)
    _deny(_sh(DA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd,payload", [
    (r"`printf '%b' '\0162m -rf /'`", "rm -rf /"),
    (r"`echo -e '\x72m -rf \x2fetc'`", "rm -rf /etc"),
    (r"$(printf 'dd if=\057dev\057zero of=\057dev\057sda\n')",
     "dd if=/dev/zero of=/dev/sda"),
    (r"`printf '%b' 'shred \x2fdev\x2fsda'`", "shred /dev/sda"),
])
def test_high_payload_reaches_exec_site(cmd, payload):
    """Structural pin: the 9th exec site feeds the ONE shared enumerator —
    all 3 lanes (decision/exposure/scorer) consume this list."""
    assert payload in _exec_payloads(cmd), \
        f"{cmd!r}: decoded stdout {payload!r} missing from _exec_payloads"


@pytest.mark.parametrize("cmd", [
    r"`echo -e '\0145cho PWNED-ECHOE'`",
    r"$(printf '%b' '\0145cho PWNED-PRINTF')",
])
def test_high_harmless_twins_never_gated(cmd, tmp_path):
    _silent(_sh(FA, tmp_path, cmd), cmd)
    _benign(_sh(SB, tmp_path, cmd), cmd)
    _benign(_sh(DA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "`echo rm -rf /`",
    "$(echo rm -rf /)",
])
def test_high_plain_literal_held(cmd, tmp_path):
    """Plain-literal substitution stdout was already folded (r10) — held."""
    _deny(_sh(FA, tmp_path, cmd), cmd)
    _deny(_sh(SB, tmp_path, cmd), cmd)
    _deny(_sh(DA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "`echo hello world`",
    "$(printf '%s' ok)",
])
def test_high_benign_subst_held(cmd, tmp_path):
    _silent(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ MED (L02/L20)
# Unresolved read-subst EXEC source: $(cat f)/$(<f) feeding an exec consumer.
# Gated floor (SB/DA >= 9); full_access silent-ALLOW exemption is CODIFIED.
MED_LEGS = [
    "X=$(</tmp/t); sh <(echo $X)",
    "X=$(cat /tmp/t); sh <(echo $X)",
    "X=$(cat /var/tmp/t); sh <(echo $X)",
    'X=$(</tmp/t); eval "$X"',
    'X=$(</tmp/t); sh -c "$X"',
    'X=$(cat /tmp/t); eval "$X"',
    'X=$(cat /tmp/t); sh -c "$X"',
    'X=$(cat /var/tmp/t); eval "$X"',
    'X=$(cat /var/tmp/t); sh -c "$X"',
]


@pytest.mark.parametrize("cmd", MED_LEGS)
def test_med_unres_source_gated_floor(cmd, tmp_path):
    d = _sh(SB, tmp_path, cmd)
    assert not d.allowed and d.risk >= 9, \
        f"{cmd!r} SB floor violated: {d.summary()}"
    d = _sh(DA, tmp_path, cmd)
    assert d.requires_approval and d.risk >= 9, \
        f"{cmd!r} DA floor violated: {d.summary()}"


@pytest.mark.parametrize("cmd", MED_LEGS)
def test_med_fa_exemption_silent(cmd, tmp_path):
    """Codified exemption: full_access silent-ALLOW is preserved (the floor
    is GATED — a scorer floor would flip catastrophic and break FA)."""
    _silent(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    # tracked source folds to its content — non-destructive content stays
    # silent (the floor is for UNRESOLVED sources only)
    "echo 'echo hi' > f.txt; X=$(cat f.txt); eval \"$X\"",
    # read-only consumer: an unresolved source that is never EXECUTED is
    # outside the boundary (cat/echo are not exec sites)
    'X=$(cat /tmp/t); echo "$X"',
    # printf format-reuse control (GT rc=127): held silent
    "printf '%s 'rm -rf / | sh",
])
def test_med_boundary_held(cmd, tmp_path):
    _silent(_sh(FA, tmp_path, cmd), cmd)


def test_med_tracked_debris_coproc_held(tmp_path):
    """R13 repro held control: tracked f.txt folds; rm -rf on the folded
    NON-root operands is approvable debris, never the unresolved floor."""
    cmd = "echo 'dd if=/dev/zero of=/dev/sda' > f.txt; X=$(cat f.txt); coproc rm -rf $X"
    _silent(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ LOW over-blocks
# GT rc=127 ENOENT for every leg: ONE nonexistent command name. Bash never
# re-splits an IFS-unsplit / @U-uppercased / @Q-quoted expansion result.
LOW_IFS = [
    "IFS=:; X='dd if=/dev/zero of=/dev/sda'; $X",
    "IFS=:; X='mkfs.ext4 /dev/sda'; $X",
    "IFS=:; X='rm -rf /'; $X",
    "IFS=:; X='rm -rf /etc'; $X",
    "IFS=:; X='rm -rf /usr'; $X",
    "IFS=:; X='shred /dev/sda'; $X",
]
LOW_ATU = [
    "X='rm -rf /'; echo ${X@U} | sh",
    'X=\'rm -rf /\'; sh -c "${X@U}"',
    "X='rm -rf /etc'; echo ${X@U} | sh",
    'X=\'rm -rf /etc\'; sh -c "${X@U}"',
    "X='rm -rf /usr'; echo ${X@U} | sh",
    'X=\'rm -rf /usr\'; sh -c "${X@U}"',
]
LOW_ATQ = [
    "X='dd if=/dev/zero of=/dev/sda'; echo ${X@Q} | sh",
    "X='mkfs.ext4 /dev/sda'; echo ${X@Q} | sh",
    'X=\'mkfs.ext4 /dev/sda\'; sh -c "${X@Q}"',
    "X='shred /dev/sda'; echo ${X@Q} | sh",
    'X=\'shred /dev/sda\'; sh -c "${X@Q}"',
]
LOW_BENIGN_LEGS = LOW_IFS + LOW_ATU + LOW_ATQ


@pytest.mark.parametrize("cmd", LOW_BENIGN_LEGS)
def test_low_overblock_fa_silent(cmd, tmp_path):
    _silent(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", LOW_BENIGN_LEGS)
def test_low_overblock_gated_benign(cmd, tmp_path):
    _benign(_sh(SB, tmp_path, cmd), cmd)
    _benign(_sh(DA, tmp_path, cmd), cmd)


# ---------------------------------------------------------------- held controls
ATL_LEGS = [
    "X='rm -rf /'; echo ${X@L} | sh",
    'X=\'rm -rf /\'; sh -c "${X@L}"',
    "X='rm -rf /etc'; echo ${X@L} | sh",
    'X=\'rm -rf /etc\'; sh -c "${X@L}"',
    "X='rm -rf /usr'; echo ${X@L} | sh",
    'X=\'rm -rf /usr\'; sh -c "${X@L}"',
]


@pytest.mark.parametrize("cmd", ATL_LEGS + [
    "IFS=,; X=rm,-rf,/; $X",              # R7 S3: delimiter PRESENT splits
    'X=rm; eval "${X@Q} -rf /"',          # X1b: single-word @Q + eval reparse
    "RM=rm; $RM -rf /",                   # case fix boundary: VALUE lowercase
])
def test_low_held_deny(cmd, tmp_path):
    """@L is spec-CORRECT deny (generator misclassification, not a product
    finding); IFS-delimiter-present / X1b / lowercase-value indirection stay
    denied — the over-block fixes must never weaken these."""
    _deny(_sh(FA, tmp_path, cmd), cmd)
    _deny(_sh(SB, tmp_path, cmd), cmd)
    _deny(_sh(DA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    'X=\'rm -rf /\'; eval "${X@Q}"',      # X1 superseded: ONE quoted word
    "IFS=; X='rm -rf /'; $X",             # R12 OB2: empty IFS, no split
])
def test_low_held_benign(cmd, tmp_path):
    _silent(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ LOW cd --
def test_cd_bare_home_tracked():
    """GT: bare cd AND cd -- land HOME — the landing must be tracked."""
    assert _update_cd_state("cd --", None, "", False) == ("~", "", True)
    assert _update_cd_state("cd", None, "", False) == ("~", "", True)
    assert _update_cd_state("cd -L", None, "", False) == ("~", "", True)


def test_cd_oldpwd_untracked_held():
    """cd - (OLDPWD) stays untracked — held (R12 F7 boundary)."""
    assert _update_cd_state("cd -", None, "", False) == (None, "", False)


@pytest.mark.parametrize("cmd", [
    "cd --; rm -rf docs",
    "cd --; touch pwned.txt",
])
def test_cd_bare_follow_benign_held(cmd, tmp_path):
    """HOME landing keeps relative follow-ups non-catastrophic (the ~
    placeholder never collapses workspace-relative cleanup into a deny)."""
    _silent(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "cd -- /etc; rm -rf .",
    "cd; cd /etc; rm -rf .",
])
def test_cd_absolute_chain_deny_held(cmd, tmp_path):
    _deny(_sh(FA, tmp_path, cmd), cmd)
    _deny(_sh(SB, tmp_path, cmd), cmd)
    _deny(_sh(DA, tmp_path, cmd), cmd)


# ================================================================ LOW $$ desync
DESYNC = "echo $$" + SQ + "it" + BS + SQ + "s fine" + SQ
DESYNC_WRAPPER = "sh -c $$" + SQ + "echo it" + BS + SQ + "s fine" + SQ
VALID_EVAL = "eval $$" + SQ + "rm -rf /" + SQ
VALID_ECHO = "echo $$" + SQ + "foo" + SQ
S5_ANSI = "sh -c $" + SQ + "echo it" + BS + SQ + "s fine" + SQ


def test_dollar_dollar_desync_fail_closed():
    """GT: echo $$'it\\'s fine' is bash rc=2 — $$ is the complete PID
    parameter, the following '…' is a PLAIN SQ span: \\' closes early and
    the trailing quote is unclosed. The product must not read it balanced."""
    assert _quotes_balanced(DESYNC) is False


@pytest.mark.parametrize("cmd", [DESYNC_WRAPPER])
def test_dollar_dollar_wrapper_desync_denied(cmd, tmp_path):
    """Invalid bash + wrapper flag boundaries unknowable → fail closed."""
    _deny(_sh(FA, tmp_path, cmd), cmd)
    _deny(_sh(SB, tmp_path, cmd), cmd)
    _deny(_sh(DA, tmp_path, cmd), cmd)


def test_dollar_dollar_valid_never_overblocked(tmp_path):
    """GT: eval $$'rm -rf /' is VALID benign bash (rc=127 PID-concat
    ENOENT); echo $$'foo' rc=0. Plain-SQ reading keeps them balanced."""
    assert _quotes_balanced(VALID_EVAL) is True
    assert _quotes_balanced(VALID_ECHO) is True
    _silent(_sh(FA, tmp_path, VALID_EVAL), VALID_EVAL)
    _benign(_sh(SB, tmp_path, VALID_EVAL), VALID_EVAL)
    _benign(_sh(DA, tmp_path, VALID_EVAL), VALID_EVAL)
    _silent(_sh(FA, tmp_path, VALID_ECHO), VALID_ECHO)


def test_s5_single_dollar_ansi_held(tmp_path):
    """R12 S5/OB1: single-$ ANSI-C escaped inner quote stays balanced."""
    assert _quotes_balanced(S5_ANSI) is True
    _silent(_sh(FA, tmp_path, S5_ANSI), S5_ANSI)
