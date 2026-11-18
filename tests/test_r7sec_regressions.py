"""R7 security regressions (task #108, Alex S1-S7 / Terry 6/6 REAL).

FAIL-SAFE (permanent user mandate): every destructive payload here is
DECISION-ONLY — PolicyEngine.evaluate / RiskScorer / command_variants verdicts
on strings. No payload is ever executed: no subprocess, no real filesystem
destruction. Paths are disposable fakes (tmp_path); if any safety regression
fails open, the real machine is unharmed.

Reproduce-first: the S1-S7 repros mirror .validation/alex-r7sec-report.md and
FAIL on the pristine R6 tree (HEAD c8ed95d). The fuzz battery permutes the
SAME expansion families (brace lists, first-component globs, custom IFS,
~user, export-led assignments, escaped-backslash parity) so R8 cannot reopen
the family with another spelling. Held controls + FP guards keep R3-R6
verdicts intact — fail-closed must NOT over-block.
"""
from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest

from ohmyllm.config.models import Config, PermissionMode
from ohmyllm.safety.normalize import command_variants, normalize_command
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


# ================================================================ S1 (HIGH)
# Brace expansion: /{home,usr} and {rm,-rf,/} are REAL execution paths
# (/bin/sh -> bash keeps brace expansion). The shared grammar must expand
# comma lists before the catastrophic gate.
@pytest.mark.parametrize("cmd", [
    "rm -rf /{home,usr}",
    "{rm,-rf,/}",
    "rm -rf /{etc,home,usr}",
    "rm -rf {/,/home}",
    "rm -rf /{home,usr}/.",
    "rm -rf /ho{me,me}",
    "rm -rf /{ho{me,usr},etc}",
    "rm -rf {/home,/usr}",
    "rm -rf {//home,//usr}",
    "sh -c " + Q + "rm -rf /{home,usr}" + Q,
    "bash -c " + DQ + "{rm,-rf,/}" + DQ,
    "eval {rm,-rf,/}",
    "busybox rm -rf /{home,usr}",
    "rm -rf /{home,}",
])
def test_s1_brace_root_denied_fa(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


# S1 fuzz: dir pairs x operand spellings (family, not instances).
_S1_DIRS = ["home", "usr", "etc", "var", "bin", "root"]
_S1_PAIRS = list(itertools.combinations(_S1_DIRS, 2))


@pytest.mark.parametrize("a,b", _S1_PAIRS)
@pytest.mark.parametrize("form", ["slash", "bare", "double"])
def test_s1_brace_fuzz_pairs(tmp_path, a, b, form):
    if form == "slash":
        cmd = f"rm -rf /{{{a},{b}}}"
    elif form == "bare":
        cmd = f"rm -rf {{/{a},/{b}}}"
    else:
        cmd = f"rm -rf {{//{a},//{b}}}"
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("target", ["/", "/home", "/usr", "/bin", "/root"])
def test_s1_brace_command_word(tmp_path, target):
    cmd = "{rm,-rf," + target + "}"
    _deny(_sh(FA, tmp_path, cmd), cmd)


# S1 fail-closed: unresolvable brace forms (range syntax, word-cap overflow)
# on a DESTRUCTIVE verb must gate — static resolution cannot prove safety.
@pytest.mark.parametrize("cmd", [
    "rm -rf /{a..z}",
    "rm -rf /{1..9}",
    "rm -rf /{" + ",".join(f"d{i}" for i in range(70)) + "}",
    "sh -c " + Q + "rm -rf /{a..z}" + Q,
])
def test_s1_brace_unresolvable_fail_closed(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ S2 (HIGH)
# Pathname-glob root-equivalents: /hom* /?ome /[h]ome /us* expand to top-level
# system dirs at runtime. The glob must sit in the FIRST path component.
@pytest.mark.parametrize("cmd", [
    "rm -rf /hom*",
    "rm -rf /?ome",
    "rm -rf /[h]ome",
    "rm -rf /us*",
    "rm -rf /et?",
    "rm -rf /[u]sr",
    "rm -rf /tmp*",
    "rm -rf /[h]ome/.",
])
def test_s2_glob_root_denied_fa(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


_S2_DIRS = ["home", "usr", "etc", "var", "boot", "opt", "srv", "bin",
            "sbin", "lib", "lib64", "root", "sys", "proc", "dev", "run"]


def _glob_spellings(d: str) -> list[str]:
    return ["/" + d[:3] + "*", "/?" + d[1:], "/[" + d[0] + "]" + d[1:], "/" + d + "*"]


@pytest.mark.parametrize("spell", [s for d in _S2_DIRS for s in _glob_spellings(d)])
def test_s2_glob_fuzz_spellings(tmp_path, spell):
    cmd = "rm -rf " + spell
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ S3 (HIGH)
# Custom IFS word-splitting: IFS=,; X=rm,-rf,/; $X runs rm -rf / at runtime.
# Substituted values must be split on the assigned IFS.
@pytest.mark.parametrize("cmd", [
    "IFS=,; X=rm,-rf,/; $X",
    "IFS=,; X=rm,-rf,/home; $X",
    "IFS=:; X=rm:-rf:/; $X",
    "IFS=,; X=rm,-rf,/{home,usr}; $X",
    "IFS=,; X=rm,-rf; $X /{home,usr}",
    "IFS=,; X=rm,-rf,/bin; $X",
])
def test_s3_ifs_split_denied_fa(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("delim", [",", ":", "@", "%", "^", "+"])
@pytest.mark.parametrize("target", ["/", "/home", "/usr", "/bin", "/root", "/lib64"])
def test_s3_ifs_fuzz_delimiters(tmp_path, delim, target):
    cmd = f"IFS={delim}; X=rm{delim}-rf{delim}{target}; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ S4 (HIGH)
# ROOT_DIRS_RX completeness: every top-level system dir is root-equivalent.
@pytest.mark.parametrize("d", ["bin", "root", "lib", "sbin", "lib64",
                               "sys", "proc", "dev", "run"])
def test_s4_missing_dirs_denied_fa(tmp_path, d):
    cmd = "rm -rf /" + d
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf /bin/.",
    "rm -rf //lib64/./",
    "rm -rf /./root",
    "rm -rf /sys/.",
    "rm -rf /dev/.",
    "rm -rf /run/.",
])
def test_s4_dot_spellings_denied_fa(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ S5 (HIGH)
# ~user tilde: ~root == /root (grammar fold); any other WHOLE ~user home is
# unresolvable (the user's home dir is unknowable) -> fail closed.
@pytest.mark.parametrize("cmd", [
    "rm -rf ~root",
    "rm -rf ~root/",
    "rm -rf ~root/.",
    "rm -rf ~alice",
    "rm -rf ~bob/.",
])
def test_s5_tilde_denied_fa(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("user", ["root", "alice", "bob", "test-user", "u_1", "svc.backup"])
@pytest.mark.parametrize("suffix", ["", "/", "/."])
def test_s5_tilde_fuzz_users(tmp_path, user, suffix):
    cmd = "rm -rf ~" + user + suffix
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ S6 (HIGH)
# EXTEND the R6 E4 extractor (Terry: UNCOVERED VARIANT, not a regression):
# export-led assignments + escaped-backslash quote desync. The pristine E4
# baseline (X="rm -rf /"; $X) stays DENIED — see held controls below.
@pytest.mark.parametrize("cmd", [
    "export X=" + DQ + "rm -rf /" + DQ + "; $X",
    "echo " + DQ + "a" + BS + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X",
    "export X=" + Q + "rm -rf /" + Q + "; $X",
    "echo " + DQ + "a" + BS + BS + DQ + "; export X=" + DQ + "rm -rf /" + DQ + "; $X",
])
def test_s6_repros_denied_fa(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("prefix", ["export", "declare", "typeset", "local", "readonly"])
@pytest.mark.parametrize("quote", [DQ, Q])
@pytest.mark.parametrize("target", ["/", "/home", "/bin"])
def test_s6_export_fuzz_prefixes(tmp_path, prefix, quote, target):
    cmd = f"{prefix} X={quote}rm -rf {target}{quote}; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("n", [2, 4, 6])
def test_s6_escape_parity_even(tmp_path, n):
    # Even backslash run: the quote CLOSES (bash parity) — the following
    # assignment is a real segment and $X resolves to rm -rf /.
    cmd = "echo " + DQ + "a" + BS * n + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


def test_s6_escape_parity_odd_inside_string(tmp_path):
    # Odd backslash run INSIDE the string: escaped quote, string continues,
    # closes at the next quote — X is still a real assignment afterwards.
    cmd = "echo " + DQ + "a" + BS + DQ + "b" + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X"
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ S7 (MED)
# Write-verb alignment: truncate/chmod/chown on system files must get the
# SAME system-location approval an equivalent redirect gets (never silent
# in FA). Sensitive-path DENY is intentionally NOT extended to /etc/passwd
# (R6/Terry control: sed -ni on /etc/passwd stays approval, not deny).
@pytest.mark.parametrize("cmd", [
    "truncate -s 0 /etc/passwd",
    "truncate -s 0 /etc/shadow",
    "truncate --size=0 /etc/passwd",
    "truncate -s 0 /etc/passwd /etc/shadow",
    "chmod 777 /etc/shadow",
    "chmod 777 /etc/passwd",
    "chmod -R 777 /etc",
    "chmod u+s /usr/bin/x",
    "chown root:root /etc/passwd",
    "chown -R root /etc",
    "sudo truncate -s 0 /etc/passwd",
])
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_s7_system_write_approval_fa(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    _approval(d, cmd)
    assert d.risk >= 9, f"{cmd!r} risk {d.risk} < 9"


@pytest.mark.parametrize("cmd", [
    "truncate -s 0 /etc/passwd",
    "chmod 777 /etc/shadow",
    "chown root:root /etc/passwd",
])
def test_s7_sandboxed_denied(tmp_path, cmd):
    d = _sh(SB, tmp_path, cmd)
    assert not d.allowed, f"{cmd!r} allowed in sandbox: {d.summary()}"


@pytest.mark.parametrize("cmd", [
    "truncate -s 0 /etc/passwd",
    "chmod 777 /etc/shadow",
    "chown root:root /etc/passwd",
])
def test_s7_dangerous_approval(tmp_path, cmd):
    _approval(_sh(DA, tmp_path, cmd), cmd)


# ============================================== mode matrix (never silent)
_MATRIX_CMDS = [
    "rm -rf /{home,usr}",
    "{rm,-rf,/}",
    "rm -rf /hom*",
    "IFS=,; X=rm,-rf,/; $X",
    "rm -rf /bin",
    "rm -rf ~root",
    "export X=" + DQ + "rm -rf /" + DQ + "; $X",
    "echo " + DQ + "a" + BS + BS + DQ + "; X=" + DQ + "rm -rf /" + DQ + "; $X",
]


@pytest.mark.parametrize("cmd", _MATRIX_CMDS)
@pytest.mark.parametrize("mode", [SB, DA])
def test_r7_matrix_never_silent(tmp_path, cmd, mode):
    _never_silent(_sh(mode, tmp_path, cmd), cmd)


# ==================================================== grammar-level proofs
def test_s1_variants_expand_braces():
    assert "rm -rf /home /usr" in command_variants("rm -rf /{home,usr}")


def test_s1_normalize_brace_command_word():
    assert normalize_command("{rm,-rf,/}") == "rm -rf /"


def test_s3_normalize_ifs_split():
    assert "rm -rf /" in normalize_command("IFS=,; X=rm,-rf,/; $X")


def test_s6_normalize_export_assignment():
    assert "rm -rf /" in normalize_command("export X=" + DQ + "rm -rf /" + DQ + "; $X")


@pytest.mark.parametrize("cmd", ["rm -rf /hom*", "rm -rf /bin", "rm -rf ~root"])
def test_r7_risk_scores_catastrophic(cmd):
    assert RiskScorer().score_command(cmd).score >= 9, cmd


# ======================================================== held controls
# Every prior-round control must still hold — fail-closed must not over-block.
@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf /home",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf " + DQ + "$HOME" + DQ,
    "rm -rf /home/.",
    "X=" + DQ + "rm -rf /" + DQ + "; $X",          # R6 E4 baseline (Terry)
    "busybox rm -rf /home",
    "exec -a name rm -rf /home",
    "env -S " + Q + "rm -rf /home" + Q,
    "rm -rf $UNSET_TARGET",                        # J-family fail-closed
    "rm -rf $((0))",
    "rm -rf `echo /`",
    "chmod 777 /",
])
def test_r7_held_denies(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf /home",
    "rm -rf /{home,usr}",
    "rm -rf /hom*",
])
def test_r7_deny_floor_held(tmp_path, cmd):
    # Alex DF1-DF3: safety.deny=["rm*"] blocks plain/brace/glob spellings.
    d = _sh(FA, tmp_path, cmd, deny=["rm*"])
    assert not d.allowed, f"{cmd!r} escaped the deny floor: {d.summary()}"


@pytest.mark.parametrize("cmd", [
    "sed -ni s/a/b/ /etc/passwd",                  # Terry R7-CTL: approval, NOT deny
    "cp -t /etc file.txt",
    "echo x > /etc/passwd",                        # Alex K1 redirect parity
])
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r7_held_approvals(tmp_path, cmd):
    _approval(_sh(FA, tmp_path, cmd), cmd)


# ========================================================== FP guards
# Benign expansion-class usage must stay SILENT in full_access (no over-block).
@pytest.mark.parametrize("cmd", [
    "echo {a,b}",
    "echo {1..3}",                                 # range on a NON-destructive verb
    "touch {a,b}.txt",
    "mkdir -p {src,tests}",
    "rm -rf {build,dist}",                         # relative workspace cleanup
    "rm -rf /tmp/*",                               # glob in component 2
    "rm -rf /tmp/build*",
    "rm -rf /home/user/*",
    "rm -rf /home/user/project",
    "rm -rf *.bak",                                # relative glob
    "rm -rf /dev/null",                            # device node, not /dev itself
    "rm -rf ~alice/project",                       # ~user SUBPATH, not whole home
    "IFS=,; X=a,b; echo $X",
    "IFS=; X=rm,-rf,/; $X",                        # empty IFS = NO split (bash)
    "export X=" + DQ + "echo hi" + DQ + "; $X",
    "truncate -s 0 data.log",
    "chmod +x script.sh",
    "chmod 644 notes.txt",
    "chown user file.txt",
    "ls ~alice",
])
def test_r7_fp_guards_silent_fa(tmp_path, cmd):
    _silent(_sh(FA, tmp_path, cmd), cmd)
