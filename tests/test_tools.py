
import pytest

from ohmyllm.safety import PolicyEngine
from ohmyllm.tools import ToolContext, build_default_registry


@pytest.fixture()
def ctx(oml_env):
    from ohmyllm.config import Config
    from ohmyllm.memory import SoulManager

    cfg = Config()
    soul = SoulManager(oml_env["home"] / "soul.md", oml_env["home"] / "backups")
    soul.ensure()
    return ToolContext(workspace=oml_env["ws"], mode="sandboxed", config=cfg, soul=soul)


@pytest.mark.asyncio()
async def test_file_tools_roundtrip(ctx):
    reg = build_default_registry()
    r = await reg.execute("write_file", {"path": "a.txt", "content": "hello\nworld\n"}, ctx)
    assert r.ok and "hello" in r.output
    r2 = await reg.execute("read_file", {"path": "a.txt"}, ctx)
    assert "world" in r2.output
    r3 = await reg.execute("patch_file", {"path": "a.txt", "old_text": "world", "new_text": "mars"}, ctx)
    assert r3.ok and "+mars" in r3.output.replace("+ mars", "+mars") or "mars" in r3.output
    r4 = await reg.execute("patch_file", {"path": "a.txt", "old_text": "nope", "new_text": "x"}, ctx)
    assert not r4.ok and "not found" in r4.error
    r5 = await reg.execute("list_dir", {"path": "."}, ctx)
    assert "a.txt" in r5.output
    r6 = await reg.execute("search_files", {"query": "mars", "path": "."}, ctx)
    assert "a.txt:1" in r6.output or "a.txt:2" in r6.output


@pytest.mark.asyncio()
async def test_write_creates_backup(ctx):
    reg = build_default_registry()
    await reg.execute("write_file", {"path": "b.txt", "content": "v1"}, ctx)
    r = await reg.execute("write_file", {"path": "b.txt", "content": "v2"}, ctx)
    assert r.ok and "backup" in r.output
    from ohmyllm import paths

    backups = list(paths.file_backups_dir().rglob("b.txt"))
    assert backups and backups[0].read_text() == "v1"
    import os
    import stat

    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


@pytest.mark.asyncio()
async def test_file_tools_reject_symlinks_and_abort_when_backup_fails(ctx, tmp_path, monkeypatch):
    reg = build_default_registry()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")
    (ctx.workspace / "linked.txt").symlink_to(outside)

    read = await reg.execute("read_file", {"path": "linked.txt"}, ctx)
    assert not read.ok and "symlink" in read.error.lower()

    target = ctx.workspace / "safe.txt"
    target.write_text("original")

    def fail_backup(path):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr("ohmyllm.tools.file_tools.backup_file", fail_backup)
    write = await reg.execute("write_file", {"path": "safe.txt", "content": "replacement"}, ctx)
    assert not write.ok
    assert target.read_text() == "original"


@pytest.mark.asyncio()
async def test_write_and_patch_revalidate_approved_object_identity(ctx):
    reg = build_default_registry()
    target = ctx.workspace / "bound.txt"
    target.write_text("approved content")
    write_args = {"path": "bound.txt", "content": "replacement"}
    approved_write = PolicyEngine(ctx.config, ctx.workspace).prepare_args("write_file", write_args)
    target.write_text("attacker replacement")
    write = await reg.execute("write_file", approved_write, ctx)
    assert not write.ok and "identity changed" in write.error
    assert target.read_text() == "attacker replacement"

    approved_patch = PolicyEngine(ctx.config, ctx.workspace).prepare_args("patch_file", {
        "path": "bound.txt", "old_text": "attacker", "new_text": "patched",
    })
    target.write_text("changed again")
    patch = await reg.execute("patch_file", approved_patch, ctx)
    assert not patch.ok and "identity changed" in patch.error
    assert target.read_text() == "changed again"


@pytest.mark.asyncio()
async def test_recursive_search_skips_every_sensitive_target(ctx):
    reg = build_default_registry()
    (ctx.workspace / "safe.txt").write_text("needle-safe")
    (ctx.workspace / "private.pem").write_text("needle-secret")
    nested = ctx.workspace / "nested"
    nested.mkdir()
    (nested / ".env").write_text("needle-env")
    result = await reg.execute("search_files", {"query": "needle", "path": "."}, ctx)
    assert result.ok and "safe.txt" in result.output
    assert "private.pem" not in result.output
    assert ".env" not in result.output
    assert "needle-secret" not in result.output and "needle-env" not in result.output


@pytest.mark.asyncio()
async def test_patch_multiple_match_error(ctx):
    reg = build_default_registry()
    await reg.execute("write_file", {"path": "c.txt", "content": "dup\ndup\n"}, ctx)
    r = await reg.execute("patch_file", {"path": "c.txt", "old_text": "dup", "new_text": "x"}, ctx)
    assert not r.ok and "2 times" in r.error
    r2 = await reg.execute("patch_file", {"path": "c.txt", "old_text": "dup", "new_text": "x", "replace_all": True}, ctx)
    assert r2.ok


@pytest.mark.asyncio()
async def test_run_command_output_and_timeout(ctx):
    reg = build_default_registry()
    r = await reg.execute("run_command", {"command": "echo hello-omllm"}, ctx)
    assert r.ok and "hello-omllm" in r.output and "exit 0" in r.output
    r2 = await reg.execute("run_command", {"command": "sleep 3", "timeout": 0.4}, ctx)
    assert not r2.ok and "timed out" in r2.error
    r3 = await reg.execute("run_command", {"command": "false"}, ctx)
    assert not r3.ok and "exit 1" in r3.output


@pytest.mark.asyncio()
async def test_soul_tools(ctx):
    reg = build_default_registry()
    r = await reg.execute("soul_append", {"section": "User", "text": "User likes turtles"}, ctx)
    assert r.ok and "User likes turtles" in ctx.soul.read()
    r2 = await reg.execute("soul_search", {"query": "turtles"}, ctx)
    assert "turtles" in r2.output
    r3 = await reg.execute("soul_upsert", {"section": "User", "text": "User likes turtles and tortoises"}, ctx)
    assert "updated" in r3.output or "saved" in r3.output
    r4 = await reg.execute("soul_forget", {"query": "turtles"}, ctx)
    assert r4.ok
    r5 = await reg.execute("soul_read", {}, ctx)
    assert r5.ok
    r6 = await reg.execute(
        "soul_append",
        {"section": "User", "text": "password=letmein12345", "force": True},
        ctx,
    )
    assert not r6.ok and "secret" in r6.error.lower()
    r7 = await reg.execute(
        "soul_upsert",
        {"section": "User", "text": "api_key=sk-abcdefghijklmnop", "force": True},
        ctx,
    )
    assert not r7.ok and "secret" in r7.error.lower()


@pytest.mark.asyncio()
async def test_system_status(ctx):
    reg = build_default_registry()
    ctx.status_provider = lambda: {"active_model": "mock-tool-7b"}
    r = await reg.execute("get_system_status", {}, ctx)
    assert r.ok and "mock-tool-7b" in r.output and "sandboxed" in r.output


@pytest.mark.asyncio()
async def test_http_fetch_disabled_is_unconditional(ctx, monkeypatch):
    reg = build_default_registry()
    called = False

    class UnexpectedClient:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr("httpx.Client", UnexpectedClient)
    result = await reg.execute("http_fetch", {"url": "https://example.com"}, ctx)
    assert not result.ok and "disabled" in result.error
    assert not called


@pytest.mark.asyncio()
async def test_http_fetch_denies_dns_and_blocks_private_addresses_redirects_and_proxies(ctx, monkeypatch):
    reg = build_default_registry()
    ctx.config.safety.http_fetch_enabled = True
    ctx.config.safety.http_fetch_allowlist = ["example.com"]
    called = False

    class UnexpectedClient:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr("httpx.Client", UnexpectedClient)
    hostname = await reg.execute("http_fetch", {"url": "https://example.com"}, ctx)
    assert not hostname.ok and "DNS hostnames are denied" in hostname.error
    assert not called

    options = {}

    class FakeResp:
        status_code = 302
        url = "https://93.184.216.34/next"
        headers = {"location": "http://127.0.0.1/private"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b"redirect"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            options.update(kwargs)

        def stream(self, method, url):
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    ctx.config.safety.http_fetch_allowlist = ["93.184.216.34"]
    monkeypatch.setattr("httpx.Client", FakeClient)
    redirect = await reg.execute("http_fetch", {"url": "https://93.184.216.34"}, ctx)
    assert not redirect.ok and "redirects are blocked" in redirect.error
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False

    ctx.config.safety.http_fetch_allowlist = ["127.0.0.1"]
    private = await reg.execute("http_fetch", {"url": "https://127.0.0.1"}, ctx)
    assert not private.ok and "private" in private.error
    assert not (await reg.execute("http_fetch", {"url": "https://user:pass@127.0.0.1"}, ctx)).ok
    assert not (await reg.execute("http_fetch", {"url": "https://127.0.0.1.evil.test"}, ctx)).ok


@pytest.mark.asyncio()
async def test_shell_uses_resolved_cwd_sanitized_environment_and_output_cap(ctx, monkeypatch):
    import os
    import shlex
    import sys

    if os.name != "posix":
        pytest.skip("POSIX shell contract")
    reg = build_default_registry()
    work = ctx.workspace / "child"
    work.mkdir()
    monkeypatch.setenv("OMLLM_TEST_TOKEN", "must-not-leak")
    script = "import os; print(os.getcwd()); print(os.getenv('OMLLM_TEST_TOKEN', 'unset')); print('x'*8000)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    result = await reg.execute("run_command", {"command": command, "cwd": "child"}, ctx)
    assert result.ok
    assert str(work.resolve()) in result.output
    assert "must-not-leak" not in result.output and "unset" in result.output
    assert "bytes truncated" in result.output


@pytest.mark.asyncio()
async def test_shell_timeout_terminates_descendants(ctx):
    import asyncio
    import os

    if os.name != "posix" or not __import__("pathlib").Path("/proc").is_dir():
        pytest.skip("Linux process-group contract")
    reg = build_default_registry()
    pid_file = ctx.workspace / "child.pid"
    command = f"sleep 30 & echo $! > {pid_file}; wait"
    result = await reg.execute("run_command", {"command": command, "timeout": 1}, ctx)
    assert not result.ok and "timed out" in result.error
    child_pid = int(pid_file.read_text().strip())

    state = ""
    for _ in range(20):
        stat_file = __import__("pathlib").Path(f"/proc/{child_pid}/stat")
        if not stat_file.exists():
            break
        state = stat_file.read_text().split()[2]
        if state == "Z":
            break
        await asyncio.sleep(0.05)
    assert state in {"", "Z"}, f"descendant process {child_pid} survived timeout in state {state}"


@pytest.mark.asyncio()
async def test_shell_cleans_descendants_after_leader_exits(ctx):
    import asyncio
    import os

    if os.name != "posix" or not __import__("pathlib").Path("/proc").is_dir():
        pytest.skip("Linux process-group contract")
    reg = build_default_registry()
    pid_file = ctx.workspace / "orphan.pid"
    command = f"sleep 30 & echo $! > {pid_file}; exit 0"
    result = await reg.execute("run_command", {"command": command, "timeout": 5}, ctx)
    assert result.ok
    child_pid = int(pid_file.read_text().strip())
    state = ""
    for _ in range(30):
        stat_file = __import__("pathlib").Path(f"/proc/{child_pid}/stat")
        if not stat_file.exists():
            break
        state = stat_file.read_text().split()[2]
        if state == "Z":
            break
        await asyncio.sleep(0.05)
    assert state in {"", "Z"}, f"descendant process {child_pid} survived leader exit in state {state}"


@pytest.mark.asyncio()
async def test_cancelled_synchronous_mutation_cannot_commit(ctx):
    import asyncio
    import threading

    from ohmyllm.tools import ToolRegistry, ToolSpec

    started = threading.Event()
    release = threading.Event()
    marker = ctx.workspace / "late-commit.txt"

    def delayed_mutation(execution_ctx, _args):
        started.set()
        release.wait(timeout=2)
        execution_ctx.check_cancelled()
        marker.write_text("committed")
        return "committed"

    reg = ToolRegistry([ToolSpec("delayed_mutation", "test mutation", [], delayed_mutation)])

    async def scenario():
        task = asyncio.create_task(reg.execute("delayed_mutation", {}, ctx))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.1)

    await scenario()
    assert not marker.exists()


def test_registry_schemas_shape():
    reg = build_default_registry()
    schemas = reg.native_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert {"soul_append", "soul_upsert", "soul_search", "soul_forget", "soul_read", "read_file",
            "list_dir", "search_files", "write_file", "patch_file", "run_command", "http_fetch",
            "get_system_status"} <= names
    for s in schemas:
        assert s["type"] == "function" and "parameters" in s["function"]
    prev = reg.preview("write_file", {"path": "x", "content": "hello"})
    assert "hello" in prev or "x" in prev


def test_write_file_meta_diff(oml_env, svc):
    import asyncio

    from ohmyllm.tools.spec import ToolContext

    ctx = ToolContext(workspace=svc.workspace, mode="sandboxed", config=svc.config, soul=svc.soul)
    (svc.workspace / "a.txt").write_text("l1\nl2\nl3\n")
    r = asyncio.run(svc.registry.execute("write_file", {"path": "a.txt", "content": "l1\nCHANGED\nl3\nl4\n"}, ctx))
    assert r.ok and r.meta["created"] is False
    assert r.meta["changed"] == 1 and r.meta["added"] == 1 and r.meta["removed"] == 0
    r2 = asyncio.run(svc.registry.execute("write_file", {"path": "b.txt", "content": "x\ny\n"}, ctx))
    assert r2.meta["created"] is True and r2.meta["added"] == 2
