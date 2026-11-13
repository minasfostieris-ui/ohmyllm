import asyncio

import pytest

from ohmyllm.config import Config, PermissionMode
from ohmyllm.safety import ApprovalManager, ApprovalOutcome, Decision, PolicyEngine


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
    # Sandboxed reads outside the workspace ASK (allowed + requires_approval):
    # docs/PERMISSIONS.md ("outside asks") and scripts/e2e_check.py:73 are the
    # audited source of truth. Reads still require explicit consent; the write
    # confinement below stays hard-blocked and cannot be overridden by a prompt.
    d = pe.evaluate("read_file", {"path": "/etc/hosts"})
    assert d.allowed and d.requires_approval
    d = pe.evaluate("write_file", {"path": "/tmp/elsewhere/x.txt", "content": "x"})
    assert not d.allowed
    sys_lib = r"C:\Program Files\lib\lib.so" if os.name == "nt" else "/usr/lib/lib.so"
    assert not pe.evaluate("write_file", {"path": sys_lib, "content": "x"}).allowed
    assert not pe.evaluate("http_fetch", {"url": "https://x"}).allowed


def test_sensitive_paths_are_component_aware_and_symlink_safe(cfg):
    c, ws = cfg
    pe = engine(cfg, PermissionMode.SANDBOXED)
    for name in (".env", ".env.local", ".env-production", "service.env"):
        assert not pe.evaluate("read_file", {"path": str(ws / name)}).allowed

    allowed = ws / "safe-env"
    sibling = ws / "safe-env-stolen"
    c.safety.allow_sensitive_paths = [str(allowed)]
    pe = PolicyEngine(c, ws)
    assert pe.evaluate("read_file", {"path": str(allowed / ".env")}).allowed
    assert not pe.evaluate("read_file", {"path": str(sibling / ".env")}).allowed

    outside = ws.parent / "outside"
    outside.mkdir()
    (ws / "linked").symlink_to(outside, target_is_directory=True)
    prepared = pe.prepare_args("read_file", {"path": "linked/secret.txt"})
    assert not pe.evaluate("read_file", prepared).allowed


def test_sandboxed_command_allowlist(cfg):
    pe = engine(cfg, PermissionMode.SANDBOXED)
    for command in ("ls -la", "git status"):
        decision = pe.evaluate("run_command", {"command": command})
        assert decision.allowed and decision.requires_approval
    # python is in SAFE_COMMANDS so it passes the allowlist gate, but the
    # hardened policy ALWAYS requires fresh one-shot approval for shell commands
    # in sandboxed mode (line 255 of policy.py).  It is not outright blocked.
    d = pe.evaluate("run_command", {"command": "python -c 'print(1)'"})
    assert d.allowed and d.requires_approval
    assert not pe.evaluate("run_command", {"command": "sh -c 'ls'"}).allowed
    assert not pe.evaluate("run_command", {"command": "curl https://x"}).allowed  # not allowlisted
    assert not pe.evaluate("run_command", {"command": "rm -rf /"}).allowed
    assert not pe.evaluate("run_command", {"command": "git push --force"}).allowed
    import os
    sys_out = r"C:\Windows\System32\out.txt" if os.name == "nt" else "/etc/out"
    assert not pe.evaluate("run_command", {"command": f"cat f > {sys_out}"}).allowed


def test_shell_cwd_is_resolved_and_every_allowed_form_is_one_shot(cfg):
    _, ws = cfg
    subdir = ws / "subdir"
    subdir.mkdir()
    pe = engine(cfg, PermissionMode.SANDBOXED)
    prepared = pe.prepare_args("run_command", {"command": "ls\nprintf safe", "cwd": "subdir"})
    assert prepared["cwd"] == str(subdir.resolve())
    decision = pe.evaluate("run_command", prepared)
    assert not decision.allowed or decision.requires_approval

    substituted = pe.prepare_args("run_command", {"command": "echo $(printf safe)"})
    decision = pe.evaluate("run_command", substituted)
    assert not decision.allowed or decision.requires_approval


def test_dangerous_needs_approvals(cfg):
    pe = engine(cfg, PermissionMode.DANGEROUS)
    d = pe.evaluate("read_file", {"path": "/etc/hosts"})
    assert d.allowed and d.requires_approval
    assert not pe.evaluate("read_file", {"path": "~/.ssh/id_rsa"}).allowed  # sensitive blocked
    d2 = pe.evaluate("run_command", {"command": "sudo rm -f file"})
    assert d2.allowed is True and d2.requires_approval is True
    assert not pe.evaluate("http_fetch", {"url": "http://x"}).allowed
    c, _ = cfg
    c.safety.http_fetch_enabled = True
    c.safety.http_fetch_allowlist = ["example.com"]
    assert pe.evaluate("http_fetch", {"url": "https://example.com"}).requires_approval


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


@pytest.mark.parametrize("outcome", list(ApprovalOutcome))
def test_approval_coroutine_outcome_and_task_cleanup(outcome):
    manager = ApprovalManager()
    decision = Decision(allowed=True, requires_approval=True)
    args = {"path": "note.txt", "content": "test"}

    async def prompt(req):
        task = asyncio.current_task()
        assert isinstance(task, asyncio.Task)
        assert manager._pending == {task: asyncio.get_running_loop()}
        assert req.tool == "write_file" and req.args == args
        return outcome

    assert asyncio.run(manager.decide("write_file", args, decision, prompt)) is outcome
    assert not manager._pending
    assert manager.preapproved("write_file", args) == (
        outcome in (ApprovalOutcome.APPROVED_SESSION, ApprovalOutcome.APPROVED_ALWAYS)
    )
    assert bool(manager.permanent_rules) == (outcome is ApprovalOutcome.APPROVED_ALWAYS)


def test_approval_coroutine_exception_denies_and_cleans_up():
    manager = ApprovalManager()
    decision = Decision(allowed=True, requires_approval=True)

    async def prompt(req):
        raise RuntimeError("synthetic prompt failure")

    assert asyncio.run(manager.decide("write_file", {}, decision, prompt)) is ApprovalOutcome.DENIED
    assert not manager._pending
    assert not manager.preapproved("write_file", {})
    assert not manager.permanent_rules


@pytest.mark.parametrize("cancellation", ["caller", "invalidation", "resisted_invalidation"])
def test_approval_coroutine_cancellation_and_pending_cleanup(cancellation):
    async def scenario():
        manager = ApprovalManager()
        decision = Decision(allowed=True, requires_approval=True)
        started = asyncio.Event()
        cleaned_up = asyncio.Event()
        never = asyncio.Event()
        prompt_tasks = []

        async def prompt(req):
            prompt_tasks.append(asyncio.current_task())
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                if cancellation == "resisted_invalidation":
                    return ApprovalOutcome.APPROVED_ALWAYS
                raise
            finally:
                cleaned_up.set()
            return ApprovalOutcome.APPROVED

        pending = asyncio.create_task(manager.decide("write_file", {}, decision, prompt))
        await asyncio.wait_for(started.wait(), timeout=1)
        prompt_task, = prompt_tasks
        assert isinstance(prompt_task, asyncio.Task)
        assert manager._pending == {prompt_task: asyncio.get_running_loop()}
        if cancellation == "caller":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=1)
        else:
            manager.invalidate_all()
            assert await asyncio.wait_for(pending, timeout=1) is ApprovalOutcome.DENIED
        assert cleaned_up.is_set()
        assert prompt_task.done()
        assert prompt_task.cancelled() == (cancellation != "resisted_invalidation")
        assert not manager._pending
        assert not manager.preapproved("write_file", {})
        assert not manager.permanent_rules

    asyncio.run(scenario())


def test_approval_coroutine_invalidated_before_pending_registration(monkeypatch):
    async def scenario():
        manager = ApprovalManager()
        decision = Decision(allowed=True, requires_approval=True)
        loop = asyncio.get_running_loop()
        create_task = loop.create_task
        prompt_started = False
        prompt_tasks = []

        async def prompt(req):
            nonlocal prompt_started
            prompt_started = True
            return ApprovalOutcome.APPROVED_ALWAYS

        def invalidate_during_creation(coro, **kwargs):
            task = create_task(coro, **kwargs)
            prompt_tasks.append(task)
            manager.invalidate_all()
            return task

        with monkeypatch.context() as patch:
            patch.setattr(loop, "create_task", invalidate_during_creation)
            outcome = await manager.decide("write_file", {}, decision, prompt)
        assert outcome is ApprovalOutcome.DENIED
        assert not prompt_started
        assert len(prompt_tasks) == 1 and prompt_tasks[0].cancelled()
        assert not manager._pending
        assert not manager.preapproved("write_file", {})
        assert not manager.permanent_rules

    asyncio.run(scenario())
