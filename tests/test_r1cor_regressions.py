"""Task #47 regressions — Daniel's final correctness report (D1-D18, minus
Jimmy's D12/K12 GUI steer event) plus Kim's findings that live in Taylor's
files (K10 server copy, K13-K16, K19, K25, K28, K29, K36, K38).

One test per finding; PoC counterparts live in /tmp/r1cor-probes and are
re-run by .validation/run-taylor-r1cor.sh. Everything here is offline:
MockTransport backends, the mock backend, loopback bridges on ephemeral
ports, and faked subprocesses — guard-compatible with verify_isolated.py.
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import threading
import time
import types
import webbrowser
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from ohmyllm import bridge as B
from ohmyllm import cli, paths, skills, verbs
from ohmyllm.agents.parser import ToolCallParser
from ohmyllm.agents.runtime import AgentRuntime
from ohmyllm.backends.base import BackendError, ChatChunk
from ohmyllm.backends.cloud import CloudBackend
from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.backends.ollama import OllamaBackend
from ohmyllm.config import Config, ConfigStore, GenerationSettings, PermissionMode
from ohmyllm.plain import PlainApp
from ohmyllm.safety.approvals import ApprovalOutcome, ApprovalRequest
from ohmyllm.safety.policy import Decision
from ohmyllm.secureio import atomic_write
from ohmyllm.tui.app import OhMyLLMApp
from ohmyllm.tui.screens.chat import ChatScreen
from ohmyllm.tui.screens.help import HELP
from ohmyllm.tui.widgets import ApprovalModal

runner = CliRunner()


def make_rt(svc):
    be = svc.backend()
    rt = AgentRuntime(
        backend=be, registry=svc.registry, policy=svc.policy,
        approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul,
    )
    rt._probe = be
    return rt


# --------------------------------------------------------------------- D1
def test_d1_multimodal_content_survives_outgoing(oml_env, svc):
    rt = make_rt(svc)
    sess = svc.new_session()
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    sess.add_message("user", parts)
    sess.add_message("assistant", [])  # no visible parts → skipped
    sess.add_message("assistant", [{"type": "text", "text": "visible"}])
    out = rt._outgoing(sess)
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"] == parts  # verbatim, never str()-flattened
    assert len(out) == 2
    assert out[1]["content"] == [{"type": "text", "text": "visible"}]


# --------------------------------------------------------------------- D2
async def _cloud_tool_calls(frames):
    c = Config()
    c.cloud.provider = "openai"
    c.cloud.keys = {"openai": "k"}
    body = "".join(
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [t]}}]}) + "\n\n"
        for t in frames
    ) + "data: [DONE]\n\n"
    tr = httpx.MockTransport(lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=body))
    b = CloudBackend(c, tr)
    chunks = [x async for x in b._stream_openai(
        "http://x", "k", "m", [], GenerationSettings(), None, True)]
    tool_chunks = [x for x in chunks if x.tool_calls]
    assert tool_chunks, "terminal tool_calls chunk vanished"
    return tool_chunks[-1].tool_calls


async def test_d2_cloud_parallel_calls_keyed_by_index(oml_env):
    # Daniel's PoC: two parallel calls, continuations carry index but no id.
    calls = await _cloud_tool_calls([
        {"index": 0, "id": "a", "function": {"name": "f", "arguments": "A1"}},
        {"index": 1, "id": "b", "function": {"name": "g", "arguments": "B1"}},
        {"index": 0, "function": {"arguments": "A2"}},
        {"index": 1, "function": {"arguments": "B2"}},
    ])
    by_name = {tc["function"]["name"]: tc["function"]["arguments"] for tc in calls}
    assert by_name == {"f": "A1A2", "g": "B1B2"}
    assert {tc["id"] for tc in calls} == {"a", "b"}


async def test_d2_cloud_index_only_no_ids(oml_env):
    calls = await _cloud_tool_calls([
        {"index": 0, "function": {"name": "f", "arguments": "X1"}},
        {"index": 1, "function": {"name": "g", "arguments": "Y1"}},
        {"index": 0, "function": {"arguments": "X2"}},
        {"index": 1, "function": {"arguments": "Y2"}},
    ])
    by_name = {tc["function"]["name"]: tc["function"]["arguments"] for tc in calls}
    assert by_name == {"f": "X1X2", "g": "Y1Y2"}


async def test_d2_cloud_continuation_without_id_or_index(oml_env):
    calls = await _cloud_tool_calls([
        {"index": 0, "id": "a", "function": {"name": "f", "arguments": "A1"}},
        {"function": {"arguments": "A2"}},
    ])
    assert len(calls) == 1
    assert calls[0]["function"]["arguments"] == "A1A2"


# ------------------------------------------------------------------- D3/D4
def test_d3_plain_mode_rebuilds_runtime(oml_env, svc):
    app = PlainApp(svc)
    old = app.rt
    app._mode("strict")
    assert app.rt is not old
    assert old._retired is True
    assert app.rt._retired is False
    assert app.rt.policy.mode is PermissionMode.STRICT


def test_d4_plain_cross_backend_model_rebinds_runtime(oml_env, svc):
    app = PlainApp(svc)
    old = app.rt
    app._set_default("lmstudio", "m")
    assert svc.config.default_backend.value == "lmstudio"
    assert svc.config.lmstudio.default_model == "m"
    assert app.rt is not old
    assert app.rt.backend.name.value == "lmstudio"


# --------------------------------------------------------------------- D5
async def test_d5_usage_aggregated_across_steps(oml_env, svc):
    svc.config.agent.agent_loop = False
    (svc.workspace / "data.txt").write_text("payload")
    be = svc.backend()
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "r1", "function": {
            "name": "read_file", "arguments": {"path": "data.txt"}}}],
            done=True, usage={"eval_count": 32})],
        [ChatChunk(content="All done."), ChatChunk(done=True, usage={"eval_count": 9})],
    ]
    rt = make_rt(svc)
    sess = svc.new_session()
    res = await rt.run_turn(sess, "read data.txt", model="mock-tool-7b")
    assert res.error is None
    assert res.calls and res.calls[0]["status"] == "ok"
    assert res.usage.get("eval_count") == 41  # 32 + 9, not last-step-only
    assert "tokens_per_s" in res.usage
    # the accumulator resets per turn
    be.script = [[ChatChunk(content="Again."), ChatChunk(done=True, usage={"eval_count": 5})]]
    res2 = await rt.run_turn(sess, "next", model="mock-tool-7b")
    assert res2.usage.get("eval_count") == 5


async def test_d5_usage_normalizes_openai_counters(oml_env, svc):
    svc.config.agent.agent_loop = False
    be = svc.backend()
    be.script = [[ChatChunk(content="hi"), ChatChunk(
        done=True, usage={"completion_tokens": 7, "prompt_tokens": 11})]]
    rt = make_rt(svc)
    res = await rt.run_turn(svc.new_session(), "hello", model="mock-chat-1b")
    assert res.usage.get("eval_count") == 7
    assert res.usage.get("prompt_eval_count") == 11
    assert res.usage.get("total_tokens") == 18


# --------------------------------------------------------------------- D6
async def test_d6_lmstudio_v0_404_cached_fallback(oml_env):
    hits = []

    def h(r):
        hits.append(r.url.path)
        return httpx.Response(404, text="no")

    b = LMStudioBackend(Config(), httpx.MockTransport(h))
    await b.ensure_loaded("m")  # must not raise on old servers
    await b.ensure_loaded("m")  # second call must not re-probe v0
    assert hits == ["/api/v0/models"]


# --------------------------------------------------------------------- D7
async def _cloud_list(response):
    c = Config()
    c.cloud.provider = "openai"
    c.cloud.keys = {"openai": "k"}
    b = CloudBackend(c, httpx.MockTransport(lambda r: response))
    return await b.list_models()


async def test_d7_cloud_list_models_shape_validation(oml_env):
    with pytest.raises(BackendError) as ei:
        await _cloud_list(httpx.Response(200, json=[{"id": "m"}]))
    assert "unexpected model list shape (list)" in str(ei.value)

    with pytest.raises(BackendError) as ei:
        await _cloud_list(httpx.Response(200, json={"data": "nope"}))
    assert "data is str" in str(ei.value)

    with pytest.raises(BackendError) as ei:
        await _cloud_list(httpx.Response(200, text="<html>login page</html>"))
    assert "invalid JSON" in str(ei.value)

    models = await _cloud_list(httpx.Response(200, json={"data": [None, 42, {"id": "good"}]}))
    assert [m.name for m in models] == ["good"]


# --------------------------------------------------------------------- D8
def test_d8_delete_active_chat_never_resurrects(bridge):
    orig_delay = MockBackend.delay
    MockBackend.delay = 0.3
    try:
        c = bridge

        def send():
            resp = c.post("/api/message", {"text": "hello", "new": True})
            try:
                resp.read()  # drain the SSE stream until the turn unwinds
            finally:
                resp.close()

        t = threading.Thread(target=send, daemon=True)
        t.start()
        sid = None
        for _ in range(300):
            st = c.get("/api/state")
            if st.get("state") == "working" and st.get("session_id"):
                sid = st["session_id"]
                break
            time.sleep(0.02)
        assert sid, "turn never reached working state"
        d = c.delete(f"/api/chats/{sid}?export=0")
        assert d.get("__status__") is None  # 200 {"trashed": ...}
        t.join(30)
        assert not t.is_alive()
        after = c.get(f"/api/chats/{sid}")
        assert after.get("__status__") == 404, "deleted chat resurrected on finally-save"
    finally:
        MockBackend.delay = orig_delay


# ------------------------------------------------------------------ D9/D10
def test_d9_run_timeout_cancels_orphan(monkeypatch):
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    started = threading.Event()
    finished = threading.Event()

    async def slow():
        started.set()
        await asyncio.sleep(0.4)
        finished.set()

    real = asyncio.run_coroutine_threadsafe
    holder = {}

    class Proxy:
        def __init__(self, f):
            self.f = f

        def result(self, timeout=None):
            started.wait(2)  # a real timeout fires while the task is running
            raise TimeoutError()

        def cancel(self):
            return self.f.cancel()

    def wrap(coro, lp):
        f = real(coro, lp)
        holder["f"] = f
        return Proxy(f)

    monkeypatch.setattr(B.asyncio, "run_coroutine_threadsafe", wrap)
    h = types.SimpleNamespace(state=types.SimpleNamespace(loop=loop), _json=lambda *a: None)
    try:
        with pytest.raises(TimeoutError):
            B.Handler._run(h, slow())
        time.sleep(0.15)  # let the loop thread process the cancellation
        assert started.is_set()
        assert holder["f"].cancelled(), "timed-out request kept running orphaned"
        assert not finished.wait(0.6), "orphan completed after the timeout"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)
        loop.close()


def test_d10_pull_disconnect_cancels_orphan(monkeypatch):
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    pstarted = threading.Event()
    pfinished = threading.Event()

    class Be:
        async def pull(self, name, prog):
            pstarted.set()
            prog("x", 0.1)
            await asyncio.sleep(0.4)
            pfinished.set()

    real = asyncio.run_coroutine_threadsafe
    holder = {}

    def wrap(coro, lp):
        f = real(coro, lp)
        holder["f"] = f
        return f

    monkeypatch.setattr(B.asyncio, "run_coroutine_threadsafe", wrap)

    class H2:
        def __init__(self):
            self.state = types.SimpleNamespace(
                loop=loop, svc=types.SimpleNamespace(backend=lambda name=None: Be()))

        def _sse_init(self):
            pass

        def _sse(self, kind, payload):
            raise BrokenPipeError()

        def _json(self, *a):
            pass

    try:
        B.Handler._pull(H2(), {"backend": "mock", "name": "x"})
        time.sleep(0.15)
        assert pstarted.is_set()
        assert holder["f"].cancelled(), "disconnected pull kept running orphaned"
        assert not pfinished.wait(0.6), "orphan pull completed after disconnect"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)
        loop.close()


# --------------------------------------------------------------------- D11
async def test_d11_midturn_chat_switch_refused(oml_env, svc):
    orig_delay = MockBackend.delay
    MockBackend.delay = 0.12
    try:
        app = OhMyLLMApp()
        async with app.run_test(size=(140, 46)) as pilot:
            await pilot.pause(0.7)
            old_sid = app.session.id
            other = svc.new_session()
            inp = app.screen.query_one("#chat-input")
            inp.value = "hello"
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if app.busy and app.turn_text:
                    break
            assert app.busy
            assert app.turn_session_id == old_sid
            app.new_chat()  # must be refused mid-turn
            await pilot.pause(0.2)
            assert app.session.id == old_sid
            assert app.load_session(other.id) is False
            assert app.session.id == old_sid
            for _ in range(200):
                await pilot.pause(0.1)
                if not app.busy:
                    break
            assert not app.busy
            assert not app.turn_active
            # the reply landed in the OLD session, not in a stray buffer
            old = app.svc.sessions.get(old_sid)
            assert any(m.get("role") == "assistant" and str(m.get("content")).strip()
                       for m in old.messages)
            # switching after the turn is allowed and starts clean
            app.new_chat()
            await pilot.pause(0.3)
            assert app.session.id != old_sid
            live = any("msg-assistant" in (w.classes or set())
                       for w in app.screen.walk_children())
            assert not live, "old turn buffers bled into the new chat"
    finally:
        MockBackend.delay = orig_delay


async def test_d11_backend_error_does_not_strand_turn(oml_env, svc, monkeypatch):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        be = app.svc.backend()

        async def boom(model, messages, params, **kw):
            await asyncio.sleep(0.15)
            raise BackendError("simulated backend failure")
            yield ChatChunk()  # pragma: no cover — makes boom an async generator

        monkeypatch.setattr(be, "stream_chat", boom)
        inp = app.screen.query_one("#chat-input")
        inp.value = "hello"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if app.busy:
                break
        for _ in range(200):
            await pilot.pause(0.1)
            if not app.busy:
                break
        assert not app.busy
        # the runtime's backend-error path emits no turn_end — the worker's
        # finally must clear the flag or the UI strands mid-turn forever
        assert not app.turn_active


# --------------------------------------------------------------------- D13
async def test_d13_ollama_enrichment_bounded_parallelism(oml_env):
    reqs = []
    inflight = 0
    peak = 0

    async def h(r):
        nonlocal inflight, peak
        reqs.append(r.url.path)
        if r.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": f"m{i}"} for i in range(6)]})
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return httpx.Response(200, json={})

    b = OllamaBackend(Config(), httpx.MockTransport(h))
    models = await b.list_models()
    assert len(models) == 6
    assert len(reqs) == 7  # one /api/tags + one /api/show per model
    assert peak > 1, "enrichment is still serial"
    assert peak <= 4, "enrichment is unbounded"


# --------------------------------------------------------------------- D14
def test_d14_functions_fence_parsed():
    p = ToolCallParser()
    payload = json.dumps({"name": "write_file", "arguments": {"path": "a", "content": "x"}})
    for fence in ("functions.write_file", "json", "tool"):
        text = f"```{fence}\n{payload}\n```"
        report = p.from_text(text)
        assert report.calls, f"fence {fence!r} was not parsed"
        assert report.calls[0].name == "write_file"


# --------------------------------------------------------------------- D15
def test_d15_heartbeat_cancelled_on_stop(oml_env, svc, monkeypatch):
    holder = {}
    orig = asyncio.new_event_loop

    def nl():
        lp = orig()
        holder["l"] = lp
        return lp

    monkeypatch.setattr(asyncio, "new_event_loop", nl)
    stop = threading.Event()
    pf = oml_env["home"] / "d15.port"
    th = threading.Thread(
        target=lambda: B.serve(workspace=str(oml_env["ws"]), port=0,
                               write_port_file=str(pf), stop=stop),
        daemon=True)
    th.start()
    for _ in range(400):
        if pf.exists():
            try:
                json.loads(pf.read_text())
                break
            except ValueError:
                pass
        time.sleep(0.05)
    assert pf.exists(), "bridge never wrote the port file"
    stop.set()
    th.join(10)
    assert not th.is_alive(), "serve thread refused to stop"
    lp = holder["l"]
    pending = [t for t in asyncio.all_tasks(lp) if not t.done()]
    assert pending == [], f"heartbeat/tasks left pending: {pending}"


# --------------------------------------------------------------------- D16
def test_d16_failed_import_leaves_no_stage(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n")
    (src / "a.txt").write_text("a")
    (src / "b.txt").write_text("b")
    mgr = skills.SkillManager(root)
    orig_copy2 = shutil.copy2

    def bad(s, d, *a, **k):
        if str(s).endswith("b.txt"):
            raise OSError("disk full")
        return orig_copy2(s, d, *a, **k)

    orig_ct = shutil.copytree

    def ct(s, d, *a, **k):
        k["copy_function"] = bad
        return orig_ct(s, d, *a, **k)

    monkeypatch.setattr(skills.shutil, "copytree", ct)
    with pytest.raises(OSError):
        mgr._copy_skill(src)
    assert list(root.glob(".import-*")) == [], "half-written stage directory leaked"
    assert not (root / "demo").exists()


# --------------------------------------------------------------------- D17
async def test_d17_evidence_output_recorded_and_cited(oml_env, svc):
    marker = "UNIQUE_EVIDENCE_STRING_12345"
    (svc.workspace / "evidence.txt").write_text(marker)
    svc.config.agent.agent_loop = True
    be = svc.backend()
    final = f"Here is what read_file returned: evidence.txt (1 lines):\n    1| {marker}"
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "r1", "function": {
            "name": "read_file", "arguments": {"path": "evidence.txt"}}}],
            done=True, usage={"eval_count": 32})],
        [ChatChunk(content=final), ChatChunk(done=True, usage={"eval_count": 9})],
        [ChatChunk(content=final), ChatChunk(done=True, usage={"eval_count": 9})],
    ]
    rt = make_rt(svc)
    res = await rt.run_turn(svc.new_session(), "read evidence.txt", model="mock-tool-7b")
    assert res.error is None
    assert res.calls and res.calls[0]["name"] == "read_file"
    assert marker in res.calls[0].get("output", ""), "tool record lost its output"
    assert AgentRuntime._cites_fresh_evidence(res.text, res.calls) == "read_file"
    assert res.steps == 3  # tool step + completion audit + gated final
    # the heuristic itself: citing the output counts even without the tool name
    calls = [{"name": "read_file", "status": "ok", "output": marker}]
    assert AgentRuntime._cites_fresh_evidence(f"the file says {marker}", calls) == "read_file"

    # blocked and denied records carry output too (same heuristic fuel)
    svc.config.agent.agent_loop = False
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "b1", "function": {
            "name": "write_file",
            "arguments": {"path": "/tmp/omllm-r1cor-outside.txt", "content": "x"}}}],
            done=True)],
        [ChatChunk(content="That was blocked."), ChatChunk(done=True)],
    ]
    res2 = await rt.run_turn(svc.new_session(), "write outside", model="mock-tool-7b")
    assert res2.calls[0]["status"] == "blocked"
    assert "BLOCKED BY PERMISSION ENGINE" in res2.calls[0].get("output", "")

    async def deny(req):
        return ApprovalOutcome.DENIED

    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "d1", "function": {
            "name": "write_file", "arguments": {"path": "notes.txt", "content": "x"}}}],
            done=True)],
        [ChatChunk(content="You denied it."), ChatChunk(done=True)],
    ]
    res3 = await rt.run_turn(svc.new_session(), "write notes", model="mock-tool-7b",
                             prompt_fn=deny)
    assert res3.calls[0]["status"] == "denied"
    assert res3.calls[0].get("output") == "denied"


# ---------------------------------------------------------------- D18/K16
async def test_d18_backend_cloud_slash_applies(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/backend cloud"
        await pilot.press("enter")
        await pilot.pause(1.0)
        assert app.svc.config.default_backend.value == "cloud"


# --------------------------------------------------------------------- K10
def test_k10_config_400_is_human_sentence(bridge):
    r = bridge.patch("/api/config", {"generation": {"temperature": 9.0}})
    assert r.get("__status__") == 400
    err = r.get("error", "")
    assert "generation.temperature must be at most 2.0" in err
    assert "ValidationError" not in err
    assert "pydantic" not in err.lower()
    r2 = bridge.post("/api/config/agent", {"max_steps": 999, "confirm": True})
    assert r2.get("__status__") == 400
    assert "agent.max_steps must be at most 64" in r2.get("error", "")


def test_k10_validation_sentence_plain_errors():
    assert B._validation_sentence(ValueError("boom")) == "boom"
    assert B._validation_sentence(ValueError("")) == "invalid configuration"


# --------------------------------------------------------------------- K13
async def test_k13_shell_approval_modal_one_shot(oml_env, svc):
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        req = ApprovalRequest(tool="run_command", args={"command": "ls"},
                              decision=Decision(True, True, 5, ("mode=sandboxed",)))
        modal = ApprovalModal(req)
        app.push_screen(modal)
        await pilot.pause(0.3)
        assert modal.query("#ok")
        assert modal.query("#deny")
        assert not modal.query("#session"), "shell modal offers a lying session button"
        assert not modal.query("#always"), "shell modal offers a lying permanent button"
        modal.action_allow_session()
        modal.action_always()
        await pilot.pause(0.2)
        assert app.screen is modal, "a/p dismissed the shell modal instead of warning"
        modal.dismiss(ApprovalOutcome.DENIED)
        await pilot.pause(0.2)
        results = []
        req2 = ApprovalRequest(tool="write_file", args={"path": "a.txt", "content": "x"},
                               decision=Decision(True, True, 3, ("mode=sandboxed",)))
        modal2 = ApprovalModal(req2)
        app.push_screen(modal2, callback=results.append)
        await pilot.pause(0.3)
        assert modal2.query("#session")
        assert modal2.query("#always")
        modal2.action_allow_session()
        await pilot.pause(0.2)
        assert results == [ApprovalOutcome.APPROVED_SESSION]


# ---------------------------------------------------------------- K14/K28
def test_k14_help_ctrl_c_never_quits():
    assert "| `Ctrl+C` | copy the current selection (never quits) |" in HELP
    assert "| `Ctrl+Q` | quit (with confirmation) |" in HELP
    assert "`Ctrl+C` / `Ctrl+Q`" not in HELP


def test_k28_help_lists_every_slash_command():
    for cmd, _args, _desc in ChatScreen.SLASH:
        assert cmd in HELP, f"{cmd} missing from the help screen"


# --------------------------------------------------------------------- K15
async def test_k15_plain_yolo_copy_matches_policy(oml_env, svc):
    app = PlainApp(svc)
    buf = io.StringIO()
    app.console = Console(file=buf, width=200)
    app._print_help()
    await app._slash("/yolo on")
    out = buf.getvalue()
    assert "auto-approves routine" in out
    assert "shell/network/mail still ask" in out
    assert "skips approvals" not in out
    assert "skip approvals" not in out


# --------------------------------------------------------------------- K19
def test_k19_run_yes_help_scopes_to_non_shell():
    res = runner.invoke(cli.app, ["run", "--help"])
    assert res.exit_code == 0
    out = " ".join(res.output.split())
    assert "non-shell" in out
    assert "shell commands still ask" in out


# --------------------------------------------------------------------- K25
def test_k25_verbs_docstring_no_sync_claim():
    doc = verbs.__doc__ or ""
    assert "same answer" not in doc
    assert "TUI" in doc
    assert "app.js" in doc


# --------------------------------------------------------------------- K29
def test_k29_models_offline_sentence(oml_env, monkeypatch):
    async def down(be):
        return (False, {})

    monkeypatch.setattr(cli, "_health", down)
    res = runner.invoke(cli.app, ["models", "--backend", "lmstudio"])
    assert res.exit_code == 2
    out = " ".join(res.output.split())
    assert "the model list needs a live server" in out


# --------------------------------------------------------------------- K36
GUI_TOKEN = "ab" * 16
GUI_PORT = 43210


@pytest.fixture()
def gui_home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.paths, "global_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def gui_spies(monkeypatch):
    """Fake every launch (guard-compatible): nothing real spawns or opens."""
    opened, calls = [], []

    def fake_open(url, *a, **k):
        opened.append(url)
        return True

    class FakeChild:
        def __init__(self, argv, **kw):
            self.argv = list(argv)
            self.pid = os.getpid()
            self.returncode = None
            pf = Path(argv[argv.index("--port-file") + 1])
            atomic_write(pf, json.dumps(
                {"version": 1, "port": GUI_PORT, "token": GUI_TOKEN, "pid": self.pid},
                separators=(",", ":")), mode=0o600)

        def poll(self):
            return 0

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def communicate(self, input=None, timeout=None):
            return (b"", b"")

        def send_signal(self, sig):
            pass

    def fake_popen(argv, **kw):
        calls.append(list(argv))
        return FakeChild(argv, **kw)

    def fake_kill(pid, sig):
        if sig == 0:
            return
        raise AssertionError(f"test must never send real signal {sig}")

    monkeypatch.setattr(webbrowser, "open", fake_open)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "kill", fake_kill)
    return opened, calls


def test_k36_no_open_prints_full_tokenized_url(gui_home, gui_spies, monkeypatch):
    opened, calls = gui_spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)
    monkeypatch.setattr(cli, "console", Console(width=200))
    res = runner.invoke(cli.app, ["gui", "--web", "--no-open"])
    assert res.exit_code == 0, res.output
    assert opened == [], "--no-open must never launch a browser"
    assert len(calls) == 1
    assert f"http://127.0.0.1:{GUI_PORT}/#token={GUI_TOKEN}" in res.output
    assert "keep it private" in res.output


# --------------------------------------------------------------------- K38
def test_k38_mail_status_fully_masks_password(oml_env):
    store = ConfigStore(paths.config_path())
    cfg = store.load()
    cfg.mail.address = "user@example.com"
    cfg.mail.password = "ZqUnique99"
    store.save(cfg)
    res = runner.invoke(cli.app, ["mail", "status"])
    assert res.exit_code == 0
    assert "••••••••" in res.output
    assert "Zq" not in res.output, "mail status leaked a password prefix"
