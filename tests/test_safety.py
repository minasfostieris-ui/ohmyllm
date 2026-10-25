import pytest

from ohmyllm.config import Config, PermissionMode
from ohmyllm.safety import PolicyEngine


@pytest.fixture()
def cfg(tmp_path):
    c = Config()
    c.workspace = str(tmp_path)
    return c, tmp_path


def engine(cfg, mode):
    c, ws = cfg
    c.permission_mode = mode
    return PolicyEngine(c, ws)


# ------------------------------------------------------------------ matrix
def test_strict_blocks_everything_modifying(cfg):
    pe = engine(cfg, PermissionMode.STRICT)
    assert not pe.evaluate("run_command", {"command": "ls"}).allowed
    assert not pe.evaluate("write_file", {"path": "a.txt", "content": "x"}).allowed
    assert not pe.evaluate("soul_append", {"text": "hi"}).allowed
    assert pe.evaluate("soul_append", {"text": "hi"}, user_requested_memory=True).allowed
    assert pe.evaluate("read_file", {"path": "notes.md"}).allowed
    assert pe.evaluate("get_system_status", {}).allowed


def test_safe_read_only_plus_memory(cfg):
    pe = engine(cfg, PermissionMode.SAFE)
    assert pe.evaluate("read_file", {"path": "x"}).allowed
    assert pe.evaluate("soul_append", {"text": "hi"}).allowed
    assert not pe.evaluate("write_file", {"path": "x", "content": "y"}).allowed
    assert not pe.evaluate("run_command", {"command": "echo hi"}).allowed
    assert not pe.evaluate("http_fetch", {"url": "http://x"}).allowed


def test_sandboxed_workspace_confinement(cfg):
    pe = engine(cfg, PermissionMode.SANDBOXED)
    assert pe.evaluate("write_file", {"path": "inside.txt", "content": "x"}).allowed
    assert pe.evaluate("write_file", {"path": "inside.txt", "content": "x"}).requires_approval
    import os
    sys_file = r"C:\Windows\System32\drivers\hosts" if os.name == "nt" else "/etc/passwd"
    assert not pe.evaluate("write_file", {"path": sys_file, "content": "x"}).allowed
    # sandboxed ASKS for anything outside the workspace; it no longer forbids it
    d = pe.evaluate("read_file", {"path": "/etc/hosts"})
    assert d.allowed and d.requires_approval
    d = pe.evaluate("write_file", {"path": "/tmp/elsewhere/x.txt", "content": "x"})
    assert d.allowed and d.requires_approval
    sys_lib = r"C:\Program Files\lib\lib.so" if os.name == "nt" else "/usr/lib/lib.so"
    assert not pe.evaluate("write_file", {"path": sys_lib, "content": "x"}).allowed
    assert not pe.evaluate("http_fetch", {"url": "https://x"}).allowed


def test_sandboxed_command_allowlist(cfg):
    pe = engine(cfg, PermissionMode.SANDBOXED)
    assert pe.evaluate("run_command", {"command": "ls -la"}).allowed
    assert pe.evaluate("run_command", {"command": "python -c 'print(1)'"}).requires_approval
    assert not pe.evaluate("run_command", {"command": "curl https://x"}).allowed  # not allowlisted
    assert not pe.evaluate("run_command", {"command": "rm -rf /"}).allowed
    assert not pe.evaluate("run_command", {"command": "git push --force"}).allowed
    assert pe.evaluate("run_command", {"command": "git status"}).allowed
    import os
    sys_out = r"C:\Windows\System32\out.txt" if os.name == "nt" else "/etc/out"
    assert not pe.evaluate("run_command", {"command": f"cat f > {sys_out}"}).allowed


def test_dangerous_needs_approvals(cfg):
    pe = engine(cfg, PermissionMode.DANGEROUS)
    d = pe.evaluate("read_file", {"path": "/etc/hosts"})
    assert d.allowed and d.requires_approval
    assert not pe.evaluate("read_file", {"path": "~/.ssh/id_rsa"}).allowed  # sensitive blocked
    d2 = pe.evaluate("run_command", {"command": "sudo rm -f file"})
    assert d2.allowed is True and d2.requires_approval is True
    assert pe.evaluate("http_fetch", {"url": "http://x"}).requires_approval


def test_full_access_phrase_gated_and_catastrophic(cfg):
    pe = engine(cfg, PermissionMode.FULL_ACCESS)
    assert pe.evaluate("run_command", {"command": "pytest -q"}).allowed
    assert not pe.evaluate("run_command", {"command": "rm -rf /"}).allowed  # blocked, no override
    c, ws = cfg
    c.safety.catastrophic_override = True
    pe2 = PolicyEngine(c, ws)
    assert pe2.evaluate("run_command", {"command": "rm -rf /"}).allowed
    # sensitive ssh keys still denied even in full_access unless explicitly allowed
    assert not pe.evaluate("read_file", {"path": "~/.ssh/id_rsa"}).allowed
    c.safety.allow_sensitive_paths = [str(ws / "safe-ssh")]
    pe3 = PolicyEngine(c, ws)
    assert pe3.evaluate("read_file", {"path": "safe-ssh/id"}).allowed


def test_model_cannot_change_mode(cfg):
    pe = engine(cfg, PermissionMode.SANDBOXED)
    # policy reads from config only; a "tool" trying to change mode isn't in the registry
    d = pe.evaluate("set_permission_mode", {"mode": "full_access"})
    assert not d.allowed


def test_sensitive_redaction_in_paths(cfg):
    pe = engine(cfg, PermissionMode.SANDBOXED)
    assert not pe.evaluate("read_file", {"path": "~/.bash_history"}).allowed
    assert not pe.evaluate("search_files", {"query": "x", "path": "~/.ssh"}).allowed


def test_extra_allowlist_extends_sandbox(cfg):
    c, ws = cfg
    c.safety.command_allowlist_extra = ["htop"]
    c.permission_mode = PermissionMode.SANDBOXED
    pe = PolicyEngine(c, ws)
    d = pe.evaluate("run_command", {"command": "htop"})
    assert d.allowed and d.risk < 3  # extras extend the sandbox allowlist
