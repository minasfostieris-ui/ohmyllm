"""R5 security-review regressions — one test per finding (A01..A29, B8, F17).

FAIL-SAFE (permanent user mandate): every destructive payload here is
DECISION-ONLY — PolicyEngine.evaluate / RiskScorer verdicts on strings.
No payload is ever executed: no subprocess, no real filesystem destruction.
If any safety regression fails open, the real machine is unharmed.

PoC strings mirror Jason's R5 harness (.validation/jason-r5sec-probes.py;
report .validation/jason-r5sec-report.md @ fe4475e). All attacks are
full_access SILENT-bypass regressions: the verdict must become deny or
approval, never silent allow. Controls keep the R3/R4 verdicts intact.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ohmyllm.config.models import Config, PermissionMode
from ohmyllm.safety.normalize import _decode_ansi_c, command_variants, normalize_command
from ohmyllm.safety.policy import PolicyEngine, is_sensitive_path
from ohmyllm.safety.risk import RiskScorer
from ohmyllm.tools.net_tools import _validate_url


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

BS = chr(92)   # backslash
Q = chr(39)    # single quote
DQ = chr(34)   # double quote
NL = chr(10)   # newline

# Jason's quadruple-nesting PoCs (A18/A19) — built exactly like his harness.
T4 = ("bash -c " + Q + "bash -c " + DQ + "bash -c " + BS + DQ
      + "bash -c " + BS * 3 + DQ + "rm -rf /" + BS * 3 + DQ + BS + DQ + DQ + Q)
E4 = ("eval " + Q + "eval " + DQ + "eval " + BS + DQ
      + "eval " + BS * 3 + DQ + "rm -rf /" + BS * 3 + DQ + BS + DQ + DQ + Q)
T3 = "bash -c " + Q + "bash -c " + DQ + "bash -c " + BS + DQ + "rm -rf /" + BS + DQ + DQ + Q


# ---------------------------------------------------------------- cluster 1
# A01-A03: rm operand order must not dodge the catastrophic gate; bare "/"
# is a system/sensitive location regardless of operand position.
@pytest.mark.parametrize("cmd", [
    "rm -rf zzz /",
    "rm zzz -rf /",
    "rm -rf /tmp/zzz /",
    "rm zzz / -rf",
    "rm -r zzz -f /",
    "rm --recursive zzz --force /",
    "rm -rf zzz /.",
    "rm -rf zzz /home",
])
def test_r5_rmorder_catastrophic_any_position(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed, cmd
    assert d.risk >= 9, cmd


@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_root_is_system_and_sensitive(tmp_path):
    e = PolicyEngine(_cfg(FA), tmp_path)
    assert e.is_system("/")
    assert e.is_system("/.")
    assert e.is_sensitive("/")
    assert is_sensitive_path("/", workspace=tmp_path)
    # workspace paths are unaffected
    assert not e.is_system(str(tmp_path / "x"))
    assert not e.is_sensitive(str(tmp_path / "x"))


def test_r5_rmorder_scorer_root_any_position():
    for cmd in ("rm -rf zzz /", "rm zzz -rf /", "rm -rf /tmp/zzz /"):
        r = RiskScorer().score_command(cmd)
        assert r.catastrophic, cmd
        assert r.score >= 10, cmd


def test_r5_rmorder_nonroot_stays_approvable_not_catastrophic(tmp_path):
    # /tmp/zzz alone is NOT root-equivalent: high risk, but not the
    # catastrophic backstop (existing semantics preserved).
    d = _sh(FA, tmp_path, "rm -rf /tmp/zzz")
    assert not d.requires_approval or d.allowed
    assert d.risk < 9 or d.allowed
    r = RiskScorer().score_command("rm -rf /tmp/zzz")
    assert not r.catastrophic


def test_r5_rmorder_workspace_rm_stays_silent(tmp_path):
    d = _sh(FA, tmp_path, "rm -rf zzz")
    assert d.allowed and not d.requires_approval


# ---------------------------------------------------------------- cluster 2
# A04-A13: transparent exec prefixes must not hide the rm verb.
@pytest.mark.parametrize("cmd", [
    "busybox rm -rf /",
    "toybox rm -rf /",
    "exec rm -rf /",
    "command rm -rf /",
    "nice rm -rf /",
    "nohup rm -rf /",
    "time rm -rf /",
    "timeout 5 rm -rf /",
    "stdbuf -oL rm -rf /",
    "ionice -c3 rm -rf /",
])
def test_r5_prefix_catastrophic_blocked(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed, cmd
    assert d.risk >= 9, cmd


@pytest.mark.parametrize("cmd", [
    "nice -n 5 rm -rf /",
    "timeout --signal=KILL 5 rm -rf /",
    "stdbuf -o L rm -rf /",
    "ionice -c 3 rm -rf /",
    "nohup nice -n '5' rm -rf /",
    "sudo busybox rm -rf /",
    "busybox rm -rf /home",
    "env busybox rm -rf /",
])
def test_r5_prefix_flags_and_combos_blocked(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed, cmd
    assert d.risk >= 9, cmd


@pytest.mark.parametrize("cmd", [
    "busybox rm -rf /",
    "toybox rm -rf /home",
    "nice rm -rf /",
    "timeout 5 rm -rf /",
])
def test_r5_prefix_deny_floor_sees_applet(tmp_path, cmd):
    # B8: anchored deny glob rm* must match through applet multiplexing.
    d = _sh(FA, tmp_path, cmd, deny=["rm*"])
    assert not d.allowed
    assert any("rm*" in r for r in d.reasons)


def test_r5_prefix_variants_expose_payload():
    vs = command_variants("busybox rm -rf /")
    assert any(v.startswith("rm -rf") for v in vs), vs
    vs2 = command_variants("timeout 5 rm -rf /home")
    assert any(v.startswith("rm -rf") for v in vs2), vs2


def test_r5_prefix_scorer_positional_verbs():
    r = RiskScorer().score_command("busybox rm -rf /")
    assert r.catastrophic and r.score >= 10
    r2 = RiskScorer().score_command("nice rm -rf zzz /")
    assert r2.catastrophic


def test_r5_prefix_benign_stays_silent(tmp_path):
    for cmd in ("busybox echo hi", "nohup echo hi", "nice echo hi", "time echo hi"):
        d = _sh(FA, tmp_path, cmd)
        assert d.allowed and not d.requires_approval, cmd


def test_r5_prefix_env_control_still_denied(tmp_path):
    # A14 was REFUTED pre-fix (env already handled) — must stay denied.
    d = _sh(FA, tmp_path, "env rm -rf /")
    assert not d.allowed and d.risk >= 9


# ---------------------------------------------------------------- cluster 3
# A15-A17: ANSI-C \cX must decode to control chars (bash: ord(X)&0x1f).
@pytest.mark.parametrize("cmd", [
    "sh -c " + Q + "rm" + BS + "cI-rf" + BS + "cI/" + Q,
    "eval " + Q + "rm" + BS + "cJ-rf" + BS + "cJ/" + Q,
    "rm $" + Q + BS + "cI" + Q + " -rf /",
    "bash -c " + Q + "rm" + BS + "cJ-rf" + BS + "cJ/" + Q,
])
def test_r5_ansic_ctrl_escapes_blocked(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed, cmd
    assert d.risk >= 9, cmd


def test_r5_ansic_decode_unit():
    # bash semantics: \cX -> chr(ord(X) & 0x1f).
    assert _decode_ansi_c("$'a" + BS + "cIb'") == "a\tb"
    assert _decode_ansi_c("$'" + BS + "cJ'") == "\n"
    assert _decode_ansi_c("$'" + BS + "c?'") == "\x7f"
    # normalize collapses the decoded separator whitespace into spaces.
    assert normalize_command("echo $'a" + BS + "cIb'").split() == ["echo", "a", "b"]


def test_r5_ansic_direct_splice_stays_inert(tmp_path):
    # A44 REFUTED: bash never word-splits quoted-expansion results — the
    # splice is ONE nonexistent command name, so allow must be preserved.
    cmd = "rm$" + Q + BS + "cJ" + Q + "-rf$" + Q + BS + "cJ" + Q + "/"
    d = _sh(FA, tmp_path, cmd)
    assert d.allowed and not d.requires_approval


def test_r5_ansic_benign_tab_operand_silent(tmp_path):
    d = _sh(FA, tmp_path, "echo $'a" + BS + "cIb'")
    assert d.allowed and not d.requires_approval


# ---------------------------------------------------------------- cluster 4
# A18-A19: nesting beyond the walk budget must fail closed.
def test_r5_depth_quadruple_bash_denied(tmp_path):
    d = _sh(FA, tmp_path, T4)
    assert not d.allowed
    assert d.risk >= 9


def test_r5_depth_quadruple_eval_denied(tmp_path):
    d = _sh(FA, tmp_path, E4)
    assert not d.allowed
    assert d.risk >= 9


def test_r5_depth_quadruple_deny_floor(tmp_path):
    d = _sh(FA, tmp_path, T4, deny=["rm*"])
    assert not d.allowed


def test_r5_depth_scorer_catastrophic():
    for cmd in (T4, E4):
        r = RiskScorer().score_command(cmd)
        assert r.catastrophic, cmd


def test_r5_depth_triple_control_still_denied(tmp_path):
    # A40 control: triple nesting was already caught — verdict unchanged.
    d = _sh(FA, tmp_path, T3)
    assert not d.allowed and d.risk >= 9


def test_r5_depth_benign_beyond_budget_fail_closed(tmp_path):
    cmd = "bash -c 'bash -c \"bash -c " + BS + "\"bash -c " + BS * 3 + DQ \
        + "echo hi" + BS * 3 + DQ + BS + DQ + DQ + "'"
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed or d.requires_approval


def test_r5_depth_benign_shallow_silent(tmp_path):
    d = _sh(FA, tmp_path, "bash -c 'echo hi'")
    assert d.allowed and not d.requires_approval


# ---------------------------------------------------------------- cluster 5
# A20-A21: process substitution must surface inner writers/verbs.
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_procsub_redirect_inner_writer_approval(tmp_path):
    d = _sh(FA, tmp_path, "echo x > >(tee /etc/passwd)")
    assert d.allowed and d.requires_approval
    assert d.risk >= 9


def test_r5_procsub_shell_consumed_payload_denied(tmp_path):
    d = _sh(FA, tmp_path, "bash <(echo " + Q + "rm -rf /" + Q + ")")
    assert not d.allowed
    assert d.risk >= 9


def test_r5_procsub_inner_verb_deny_floor(tmp_path):
    d = _sh(FA, tmp_path, "cat <(rm -rf /)", deny=["rm*"])
    assert not d.allowed


@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_procsub_input_redirect_inner_writer(tmp_path):
    d = _sh(FA, tmp_path, "cat < <(tee /etc/passwd)")
    assert d.allowed and d.requires_approval
    assert d.risk >= 9


def test_r5_procsub_benign_stays_silent(tmp_path):
    for cmd in ("diff <(sort a.txt) <(sort b.txt)", "echo x > >(cat)",
                "echo $((1+2))", "cat <(echo hello)"):
        d = _sh(FA, tmp_path, cmd)
        assert d.allowed and not d.requires_approval, cmd


def test_r5_procsub_scorer_inner_destruction():
    r = RiskScorer().score_command("bash <(echo 'rm -rf /')")
    assert r.catastrophic


# ---------------------------------------------------------------- cluster 6
# A22-A25: attached -tDIR / --target-directory=DIR destination routing.
@pytest.mark.parametrize("cmd", [
    "cp -t/etc x",
    "mv --target-directory=/etc x",
    "ln -t/etc x y",
    "install -t/etc x",
    "cp --target-directory=/etc x",
    "mv -t/etc x",
    "install --target-directory=/etc x",
])
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_copyt_attached_target_approval(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert d.allowed and d.requires_approval, cmd
    assert d.risk >= 9, cmd


@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_copyt_separated_control_unchanged(tmp_path):
    # A53 control: separated -t already worked.
    d = _sh(FA, tmp_path, "mv -t /etc x")
    assert d.allowed and d.requires_approval and d.risk >= 9


def test_r5_copyt_workspace_target_silent(tmp_path):
    for cmd in ("cp -tsub x", "mv --target-directory=sub x"):
        d = _sh(FA, tmp_path, cmd)
        assert d.allowed and not d.requires_approval, cmd


def test_r5_copyt_sensitive_target_denied(tmp_path):
    d = _sh(FA, tmp_path, "cp -t/root/.ssh x")
    assert not d.allowed


# ---------------------------------------------------------------- cluster 7
# A26-A27: sed --in-place / attached -eSCRIPT keep the file operand visible.
@pytest.mark.parametrize("cmd", [
    "sed --in-place 's/x/y/' /etc/passwd",
    "sed -i -e's/x/y/' /etc/passwd",
    "sed --in-place=.bak 's/x/y/' /etc/passwd",
    "sed -i.bak 's/x/y/' /etc/passwd",
    "sed -i -e 's/x/y/' /etc/passwd",
    "sed -i 's/x/y/' /etc/hosts",
])
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_sedi_system_edit_approval(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert d.allowed and d.requires_approval, cmd
    assert d.risk >= 9, cmd


def test_r5_sedi_workspace_edit_silent(tmp_path):
    for cmd in ("sed -i 's/x/y/' notes.txt", "sed --in-place 's/x/y/' notes.txt",
                "sed -i -e's/x/y/' notes.txt"):
        d = _sh(FA, tmp_path, cmd)
        assert d.allowed and not d.requires_approval, cmd


def test_r5_sedi_scorer_long_form():
    r = RiskScorer().score_command("sed --in-place 's/x/y/' /etc/passwd")
    assert r.score >= 5
    assert any("in-place" in reason for reason in r.reasons)


def test_r5_sedi_sensitive_target_denied(tmp_path):
    d = _sh(FA, tmp_path, "sed -i 's/x/y/' /root/.ssh/authorized_keys")
    assert not d.allowed


# ---------------------------------------------------------------- cluster 8
# A28-A29: perl -i / sponge are write verbs; F17: multicast SSRF.
@pytest.mark.parametrize("cmd", [
    "perl -i -pe 's/x/y/' /etc/passwd",
    "sponge /etc/passwd",
    "perl -i.bak -pe 's/x/y/' /etc/passwd",
    "perl -pi -e 's/x/y/' /etc/passwd",
    "sponge /etc/hosts",
])
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_writers_system_write_approval(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert d.allowed and d.requires_approval, cmd
    assert d.risk >= 9, cmd


def test_r5_writers_benign_silent(tmp_path):
    for cmd in ("perl -pe 's/x/y/' notes.txt", "sponge notes.txt",
                "perl -e 'print 1'"):
        d = _sh(FA, tmp_path, cmd)
        assert d.allowed and not d.requires_approval, cmd


def test_r5_writers_sensitive_denied(tmp_path):
    d = _sh(FA, tmp_path, "perl -i -pe 's/x/y/' /root/.ssh/authorized_keys")
    assert not d.allowed


@pytest.mark.parametrize("host", ["224.0.0.1", "224.0.0.251", "239.1.2.3"])
def test_r5_multicast_denied(host):
    with pytest.raises(ValueError):
        _validate_url("http://" + host + "/", [host])


def test_r5_public_ip_still_allowed():
    scheme, host, port = _validate_url("http://8.8.8.8/", ["8.8.8.8"])
    assert (scheme, host, port) == ("http", "8.8.8.8", 80)


# ---------------------------------------------------------------- controls
# R3/R4 verdicts + FP guards that must survive every cluster fix.
@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "sudo -p 'x y' rm -rf /",
    "sh -c 'rm -rf /'",
    "eval 'rm -rf /'",
    "rm --recursive --force /",
    "echo hi" + NL + "rm -rf /",
    "echo / | xargs rm -rf",
    "find / -delete",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda",
    T3,
])
def test_r5_controls_catastrophic_still_denied(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert not d.allowed, cmd
    assert d.risk >= 9, cmd


@pytest.mark.parametrize("cmd", [
    'echo "rm -rf /"',
    'grep -r "rm -rf /" docs/',
    'git commit -m "rm -rf /"',
    "echo shred /dev/sda",
    "echo hi 2>&1",
    "echo hi >&2",
    "echo hello" + NL + "echo world",
    "rm -rf build",
])
def test_r5_controls_fp_guards_silent(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert d.allowed and not d.requires_approval, cmd


@pytest.mark.parametrize("cmd", [
    "echo hi > /etc/passwd",
    "echo hi 2> /etc/passwd",
    "echo hi >| /etc/passwd",
    "echo hi &> /etc/passwd",
    "exec 3> /etc/passwd",
    "mv x /etc/",
    'cp x "/etc/my dir"',
])
@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_controls_system_redirect_approval(tmp_path, cmd):
    d = _sh(FA, tmp_path, cmd)
    assert d.allowed and d.requires_approval, cmd
    assert d.risk >= 9, cmd


@pytest.mark.skipif(os.name != "posix", reason="Unix binaries / POSIX shell semantics")
def test_r5_controls_other_modes_gate(tmp_path):
    for mode in (DA, SB):
        d = _sh(mode, tmp_path, "rm -rf /")
        assert not d.allowed, mode
    d = _sh(SB, tmp_path, "echo x > /etc/passwd")
    assert not d.allowed


def test_r5_controls_deny_floor_fail_closed(tmp_path):
    d = _sh(FA, tmp_path, "sudo -p 'x rm -rf /", deny=["rm*"])
    assert not d.allowed
    d2 = _sh(FA, tmp_path, "echo hi", deny=[""])
    assert not d2.allowed
