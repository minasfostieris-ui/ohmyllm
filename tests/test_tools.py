
import pytest

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
    r6 = await reg.execute("soul_append", {"section": "User", "text": "password=letmein12345"}, ctx)
    assert not r6.ok and "secret" in r6.error.lower()


@pytest.mark.asyncio()
async def test_system_status(ctx):
    reg = build_default_registry()
    ctx.status_provider = lambda: {"active_model": "mock-tool-7b"}
    r = await reg.execute("get_system_status", {}, ctx)
    assert r.ok and "mock-tool-7b" in r.output and "sandboxed" in r.output


@pytest.mark.asyncio()
async def test_http_fetch_disabled_by_policy_and_validates_url(ctx):
    reg = build_default_registry()
    r = await reg.execute("http_fetch", {"url": "ftp://bad"}, ctx)
    assert not r.ok
    # direct handler call with a monkeypatched client
    import httpx

    class FakeResp:
        status_code = 200
        url = "http://x"
        headers = {"content-type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield b"<html><body>Hi <b>there</b></body></html>"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url):
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkey_httpx = pytest.MonkeyPatch()
    monkey_httpx.setattr(httpx, "Client", FakeClient)
    r2 = await reg.execute("http_fetch", {"url": "http://x"}, ctx)
    assert r2.ok and "Hi there" in r2.output
    monkey_httpx.undo()


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
