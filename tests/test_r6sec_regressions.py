"""R6 security-review regressions — Ethan findings E1..E8 + FAMILY fuzz battery.

FAIL-SAFE (permanent user mandate): every destructive payload here is
DECISION-ONLY — PolicyEngine.evaluate / RiskScorer / command_variants verdicts
on strings. No payload is ever executed: no subprocess, no real filesystem
destruction, no real feed files. Paths are disposable fakes; if any safety
regression fails open, the real machine is unharmed.

Reproduce-first: the E1-E8 repros mirror .validation/ethan-r6sec-report.md and
FAIL on the pristine R5 tree (HEAD 2dbd215). The fuzz battery permutes the
SAME grammar families (dot-suffix root operands, clustered flags, attached
values, quoted assignments, wrapper spellings) so R7 cannot reopen the family
with another spelling. Held controls + FP guards keep R3/R4/R5 verdicts intact.
"""
from __future__ import annotations

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


def _deny(d, cmd):
    assert not d.allowed, f"{cmd!r} silently allowed: {d.summary()}"
    assert d.risk >= 9, f"{cmd!r} risk {d.risk} < 9"


def _approval(d, cmd):
    assert d.requires_approval, f"{cmd!r} not approval-gated: {d.summary()}"


def _silent(d, cmd):
    assert d.allowed and not d.requires_approval, f"{cmd!r} over-gated: {d.summary()}"


# ================================================================ E1 (HIGH)
# Dot-suffix root operands: /home/. == /home, ~/. == entire home. The shared
# root-operand grammar must consume dot components in EVERY position.
@pytest.mark.parametrize("cmd", [
    "rm -rf /home/.",
    "rm -rf /usr/.",
    "rm -rf /etc/./",
    "rm -rf /var/.",
    "rm -rf /home/./",
    "rm -rf /./home",
    'rm -rf "$HOME/."',
    "rm -rf ~/.",
    "rm -rf ~",
    "rm --recursive --force /home/.",
    "rm -rf /home/..",
    "rm -rf //home/.",
])
def test_r6_e1_dot_suffix_root_denied(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf /home/.",
    "rm -rf /etc/./",
    "rm -rf ~/.",
    'rm -rf "$HOME/."',
])
def test_r6_e1_scorer_catastrophic(cmd):
    r = RiskScorer().score_command(cmd)
    assert r.catastrophic and r.score >= 10, cmd


# E1 FUZZ: every top-level dir x dot-suffix x flag spelling.
_E1_DIRS = ["home", "usr", "etc", "var", "boot", "opt", "srv"]
_E1_SUFFIX = ["", "/.", "/./", "/..", "/../", "/./."]
_E1_FORMS = ["rm -rf", "rm -fr", "rm --recursive --force"]


@pytest.mark.parametrize("d", _E1_DIRS)
@pytest.mark.parametrize("suf", _E1_SUFFIX)
@pytest.mark.parametrize("form", _E1_FORMS)
def test_r6_e1_fuzz_dirs(tmp_path, d, suf, form):
    cmd = f"{form} /{d}{suf}"
    _deny(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("op", [
    "/./home", "//home", "//home/.", "/home//.", "/../home", "/home/./.",
    "~", "~/", "~/.", "~/.","~/./", "~/..", "$HOME", "$HOME/.", "$HOME/./",
])
def test_r6_e1_fuzz_root_spellings(tmp_path, op):
    cmd = f"rm -rf {op}"
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ E2 (HIGH)
# exec -a NAME hides the payload verb. -a is a required-arg flag of exec.
@pytest.mark.parametrize("cmd", [
    "exec -a foo rm -rf /",
    "exec -a 'my name' rm -rf /home",
    "nohup exec -a foo rm -rf /",
    "exec -afoo rm -rf /",
    "exec -a foo rm -rf /etc/.",
])
def test_r6_e2_exec_a_denied(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


def test_r6_e2_exec_a_deny_floor(tmp_path):
    d = _sh(FA, tmp_path, "exec -a foo rm -rf /tmp/ethan-x", deny=["rm*"])
    assert not d.allowed


def test_r6_e2_exec_a_exposes_rm_variant():
    vs = command_variants("exec -a foo rm -rf /")
    assert any(v.split()[:1] == ["rm"] for v in vs), vs


# ================================================================ E3 (HIGH)
# Attached env -S / --split-string quote-splice fabricates a cluster. The -S
# value is a NESTED COMMAND STRING and must be walked (attached or separated).
@pytest.mark.parametrize("cmd", [
    "env -S'rm -rf /'",
    'env -S"rm -rf /"',
    "env -S 'rm -rf /'",
    "env --split-string='rm -rf /'",
    "env --split-string 'rm -rf /'",
    "env -Srm -rf /",
    "env -u FOO -S'rm -rf /'",
    "env -S'rm -rf /home'",
    "env -S'busybox rm -rf /'",
])
def test_r6_e3_env_split_denied(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


def test_r6_e3_env_split_deny_floor(tmp_path):
    d = _sh(FA, tmp_path, "env -S'rm -rf /tmp/ethan-x'", deny=["rm*"])
    assert not d.allowed


def test_r6_e3_env_split_exposes_rm_variant():
    vs = command_variants("env -S'rm -rf /'")
    assert any(v.split()[:1] == ["rm"] for v in vs), vs


def test_r6_e3_scorer_catastrophic():
    r = RiskScorer().score_command("env -S'rm -rf /'")
    assert r.catastrophic and r.score >= 10


# ================================================================ E4 (HIGH)
# Quoted multi-word assignment values must resolve FULLY (quote-aware on RAW).
@pytest.mark.parametrize("cmd", [
    'X="rm -rf /"; $X',
    "X='rm -rf /'; $X",
    'X="rm -rf /home"; $X',
    'X="rm -rf"; $X /',
    'X="rm -rf /"; Y=$X; $Y',
    'X=echo; X="rm -rf /"; $X',
    'X="rm -rf /etc/."; $X',
])
def test_r6_e4_quoted_assign_denied(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


def test_r6_e4_quoted_assign_deny_floor(tmp_path):
    d = _sh(FA, tmp_path, 'X="rm -rf /tmp/ethan-x"; $X', deny=["rm*"])
    assert not d.allowed


def test_r6_e4_expands_full_reference():
    n = normalize_command('X="rm -rf /"; $X')
    assert n.split(";")[-1].strip() == "rm -rf /", n


# ================================================================ E5 (HIGH)
# sed clustered -i (-ni/-Ei) is in-place: system writes must approval-gate.
@pytest.mark.parametrize("cmd", [
    "sed -ni s/a/b/ /etc/passwd",
    "sed -Ei s/a/b/ /etc/passwd",
    "sed -n -i s/a/b/ /etc/passwd",
    "sed -i s/a/b/ /etc/passwd",
    "sed -ni.bak s/a/b/ /etc/passwd",
    "sed -i.bak s/a/b/ /etc/passwd",
])
def test_r6_e5_sed_cluster_inplace_approval(tmp_path, cmd):
    _approval(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("pre", ["", "n", "E", "nE", "En", "r", "s", "u", "z"])
def test_r6_e5_fuzz_sed_clusters(tmp_path, pre):
    cmd = f"sed -{pre}i s/a/b/ /etc/passwd"
    _approval(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ E6 (HIGH)
# cp/mv/ln clustered -t (-rt/-vt/-st/-at) routes the destination to the
# target dir; system targets must approval-gate. Uppercase -T is boolean.
@pytest.mark.parametrize("cmd", [
    "cp -rt /etc probe_src",
    "mv -vt /etc probe_src",
    "ln -st /etc probe_src",
    "cp -at /etc probe_src",
    "cp -t/etc probe_src",
    "cp -rt/etc probe_src",
    "cp --target-directory=/etc probe_src",
])
def test_r6_e6_copy_cluster_target_approval(tmp_path, cmd):
    _approval(_sh(FA, tmp_path, cmd), cmd)


@pytest.mark.parametrize("verb", ["cp", "mv", "ln"])
@pytest.mark.parametrize("pre", ["", "r", "v", "a", "rv", "fa", "n"])
def test_r6_e6_fuzz_copy_clusters(tmp_path, verb, pre):
    cmd = f"{verb} -{pre}t /etc probe_src"
    _approval(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ E7 (HIGH)
# install -D is BOOLEAN (must not swallow -t); -Dt cluster routes target.
@pytest.mark.parametrize("cmd", [
    "install -D -t /etc probe_src",
    "install -Dt /etc probe_src",
    "install -D -t/etc probe_src",
    "install -d /etc/probe_dir",
    "install --directory /etc/probe_dir",
    "install -m 755 -t /etc probe_src",
])
def test_r6_e7_install_target_approval(tmp_path, cmd):
    _approval(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ E8 (MED)
# xargs -a FILE / --arg-file feeds unresolvable operands to a destructive
# payload -> fail closed (catastrophic), never silent-allow.
@pytest.mark.parametrize("cmd", [
    "xargs -a /tmp/ethan-r6-feed rm -rf",
    "xargs --arg-file=/tmp/ethan-r6-feed rm -rf",
    "xargs --arg-file /tmp/ethan-r6-feed rm -rf",
    "xargs -ra /tmp/ethan-r6-feed rm -rf",
    "xargs -a /tmp/ethan-r6-feed shred",
    "xargs -a /tmp/ethan-r6-feed dd",
])
def test_r6_e8_xargs_argfile_fail_closed(tmp_path, cmd):
    _deny(_sh(FA, tmp_path, cmd), cmd)


# ================================================================ HELD controls
# These PASS on the pristine R5 tree and MUST stay green after the family fix.
@pytest.mark.parametrize("cmd", [
    "rm -rf //",            # N03 resolved-root sensitive invariant
    "rm -rf /home/..",      # N15a
    'X="rm -rf"; $X /',     # N07a split assignment
    "env -S 'rm -rf /'",    # K06c separated env -S
])
def test_r6_held_catastrophic_controls(tmp_path, cmd):
    # Denial is the security property; // and /home/.. are caught by the
    # resolved-root sensitive invariant (risk 8) pre-fix and the catastrophic
    # gate (risk 10) post-fix — both deny.
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed, f"{cmd!r} allowed: {d.summary()}"


def test_r6_held_exec_fd_redirect_approval(tmp_path):
    # N11: fd-prefixed redirects ARE extracted.
    _approval(_sh(FA, tmp_path, "exec 3>/etc/passwd"), "exec 3>/etc/passwd")


@pytest.mark.parametrize("cmd", ["rm", "rm -rf", "rm -f"])
def test_r6_held_truncated_rm_deny_floor(tmp_path, cmd):
    # F03/F05: rm* glob still matches operand-less rm.
    d = _sh(FA, tmp_path, cmd, deny=["rm*"])
    assert not d.allowed


def test_r6_held_fullwidth_inert(tmp_path):
    # U01: fullwidth ｒｍ is a nonexistent command — inert allow.
    _silent(_sh(FA, tmp_path, "ｒｍ -rf /"), "fullwidth")


def test_r6_held_attached_cp_target_approval(tmp_path):
    # C10: attached -t/etc was already correct in R5.
    _approval(_sh(FA, tmp_path, "cp -t/etc probe_src"), "cp -t/etc")


def test_r6_held_separated_sed_inplace_approval(tmp_path):
    # N08c: separated -n -i was already correct in R5.
    _approval(_sh(FA, tmp_path, "sed -n -i s/a/b/ /etc/passwd"), "sed -n -i")


# ================================================================ FP guards
# The family fix must NOT over-gate benign commands (silent in full_access).
@pytest.mark.parametrize("cmd", [
    "rm -rf zzz",                       # workspace cleanup
    "rm -rf /home/user",                # subpath, NOT root-equivalent
    "rm -rf /home/user/.",              # dot-suffix on a SUBPATH stays scoped
    'echo "rm -rf /"',                  # quoted literal
    "echo rm -rf /",                    # echo verb, not rm
    'msg="rm -rf /"; echo "$msg"',      # echo of a quoted assignment
    'X="hello world"; echo $X',         # benign multi-word assignment
    "sed -e 's/i/x/' notes.txt",        # NO in-place: -e script, i is literal
    "sed -ne 's/i/x/' notes.txt",       # -n -e: still no in-place
    "sed -n 's/i/x/' notes.txt",        # read-only sed
    "cp -T /etc probe_src",             # -T boolean: dest is probe_src (workspace)
    "cp -rT x /etc",                    # -T boolean: dest /etc -> approval (below)
    "install -D src dest",              # -D boolean, dest workspace
    "install -d a b",                   # workspace dirs
    "env -S'echo hello'",               # benign nested command
    "env FOO=bar echo hi",              # env assignment
    "exec -a foo echo hi",              # exec -a benign verb
    "exec echo hi",                     # plain exec
    "xargs -a /tmp/feed echo",          # non-destructive payload
    "xargs -a /tmp/feed ls",            # non-destructive payload
])
def test_r6_fp_guards_silent(tmp_path, cmd):
    if cmd == "cp -rT x /etc":
        _approval(_sh(FA, tmp_path, cmd), cmd)
    else:
        _silent(_sh(FA, tmp_path, cmd), cmd)


def test_r6_fp_subpath_not_catastrophic():
    r = RiskScorer().score_command("rm -rf /home/user")
    assert not r.catastrophic
    r2 = RiskScorer().score_command("rm -rf /home/user/.")
    assert not r2.catastrophic


def test_r6_fp_sed_script_i_not_inplace(tmp_path):
    # The cluster parser must stop at -e (arg-taking): 's/i/x/' is a script,
    # not an in-place flag. No write target -> silent.
    _silent(_sh(FA, tmp_path, "sed -e 's/i/x/' notes.txt"), "sed -e")


# ================================================================ mode matrix
# The backstop holds in EVERY mode (DA approval-gates, SB/FA deny catastrophe).
@pytest.mark.parametrize("cmd", [
    "rm -rf /home/.",
    "exec -a foo rm -rf /",
    "env -S'rm -rf /'",
    'X="rm -rf /"; $X',
])
def test_r6_modes_catastrophic_never_silent(tmp_path, cmd):
    for mode in (FA, SB, DA):
        d = _sh(mode, tmp_path, cmd)
        assert not (d.allowed and not d.requires_approval), f"{mode} {cmd}: {d.summary()}"
