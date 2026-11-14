"""R3 correctness regressions — Leo's 21 confirmed findings + U4/U12 contracts.

One test per finding (C1..C21, U4, U12). Every test is fail-safe: read-only
tools inside the tmp workspace, stubbed subprocess/Recorder/download, decision
-only checks — no real destructive payload ever runs.

Falsifiability: each test was observed FAILING on pristine f8d6952 (git archive
+ guarded run) and PASSING on the fixed tree — see .validation/taylor-r3cor-*.log.
"""

import asyncio
import io
import json
import random
import string
import subprocess
import threading
import time

import httpx
import pytest
from rich.console import Console

from ohmyllm.agents.prompts import trim_messages
from ohmyllm.agents.runtime import AgentRuntime
from ohmyllm.agents.validator import _coerce
from ohmyllm.backends.base import BackendError, ChatChunk
from ohmyllm.backends.cloud import CloudBackend
from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.config import Config, ConfigStore, GenerationSettings
from ohmyllm.config.models import PermissionMode
from ohmyllm.plain import PlainApp
from ohmyllm.tools.spec import ParamSpec

# --------------------------------------------------------------------- C1


def _no_orphan_tools(msgs: list[dict]) -> None:
    """Every tool message must sit directly under an assistant anchor that
    declares its id (contiguous tool block) — the OpenAI wire hard-400s else."""
    for i, m in enumerate(msgs):
        if m.get("role") != "tool":
            continue
        j = i - 1
        while j >= 0 and msgs[j].get("role") == "tool":
            j -= 1
        assert j >= 0, f"orphan tool at {i}: no anchor before it"
        anchor = msgs[j]
        assert anchor.get("role") == "assistant" and anchor.get("tool_calls"), \
            f"orphan tool at {i}: preceded by {anchor.get('role')}"
        ids = {tc.get("id") for tc in anchor["tool_calls"]}
        assert m.get("tool_call_id") in ids, f"tool at {i} not declared by its anchor"


def test_c1_trim_never_orphans_tool_messages():
    anchor = {"role": "assistant", "content": "A" * 7000,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "read_file", "arguments": "{}"}}]}
    tool = {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "T" * 100}
    history = [{"role": "system", "content": "s" * 10},
               {"role": "user", "content": "u" * 500},
               anchor, tool, {"role": "user", "content": "latest"}]
    # budget too small for the 7100-char anchor group: BOTH must go, not just
    # the anchor (the old per-message trim kept the orphan tool → wire 400)
    out = trim_messages(history, 1000)
    _no_orphan_tools(out)
    assert not any(m.get("role") == "tool" for m in out)
    assert not any(m.get("tool_calls") for m in out)
    assert out[-1]["content"] == "latest"


def test_c1_trim_keeps_anchor_and_tools_together():
    anchor = {"role": "assistant", "content": "A" * 7000,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "read_file", "arguments": "{}"}}]}
    tool = {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "T" * 100}
    history = [{"role": "system", "content": "s" * 10},
               {"role": "user", "content": "u" * 500},
               anchor, tool, {"role": "user", "content": "latest"}]
    out = trim_messages(history, 7200)  # group fits, older prose does not
    _no_orphan_tools(out)
    assert anchor in out and tool in out
    assert out.index(tool) == out.index(anchor) + 1


def test_c1_trim_strips_leading_orphan_tool():
    # history that ALREADY starts with an orphan tool (older trim residue)
    history = [{"role": "tool", "tool_call_id": "gone", "name": "t", "content": "x"},
               {"role": "user", "content": "u" * 5000},
               {"role": "user", "content": "latest"}]
    out = trim_messages(history, 100)
    _no_orphan_tools(out)
    assert out[-1]["content"] == "latest"


# --------------------------------------------------------------------- C2


class _RecordingBe(MockBackend):
    """Mock backend that records every message list it is asked to complete."""

    def __init__(self, config):
        super().__init__(config)
        self.seen: list[list[dict]] = []

    def stream_chat(self, model, messages, params, *, tools=None, native_tools=False):
        self.seen.append([dict(m) for m in messages])
        return super().stream_chat(model, messages, params, tools=tools, native_tools=native_tools)


def _read_call(i, path="a.txt"):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": "read_file", "arguments": {"path": path}}}


def test_c2_steer_lands_after_whole_tool_batch(svc):
    svc.config.permission_mode = PermissionMode.SANDBOXED
    svc.config.agent.native_tools = "force"
    (svc.workspace / "a.txt").write_text("alpha")
    (svc.workspace / "b.txt").write_text("beta")
    be = _RecordingBe(svc.config)
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills)
    batch = [ChatChunk(content="", tool_calls=[_read_call(0), _read_call(1, "b.txt")],
                       done=True, finish="tool_calls", usage={})]
    be.script = [batch, [ChatChunk(content="all done", done=True, finish="stop", usage={})]]
    steered = []

    def on_event(ev):
        if ev.get("type") == "tool_result" and not steered:
            steered.append(True)
            rt.steer("STEER-BATCH")

    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "read both files", on_event=on_event))
    assert res.error is None
    assert steered
    # session ordering is the source of truth: the two tool results of ONE
    # declared batch must be contiguous, with the steer user message AFTER
    # them (pre-fix: steer drained per-call, landing between c0 and c1)
    msgs = sess.messages
    ai = next(i for i, m in enumerate(msgs)
              if m.get("role") == "assistant" and len(m.get("tool_calls") or []) == 2)
    assert msgs[ai + 1].get("role") == "tool"
    assert msgs[ai + 2].get("role") == "tool"
    assert {msgs[ai + 1].get("tool_call_id"), msgs[ai + 2].get("tool_call_id")} == {"c0", "c1"}
    assert any(m.get("role") == "user" and m.get("content") == "STEER-BATCH"
               for m in msgs[ai + 3:])
    # the NEXT model call saw the steer; the first one could not have
    assert any(m.get("role") == "user" and m.get("content") == "STEER-BATCH" for m in be.seen[1])
    assert not any(m.get("content") == "STEER-BATCH" for m in be.seen[0])


# --------------------------------------------------------------------- C3


async def test_c3_offline_backend_first_message_keeps_repl_alive(svc, monkeypatch):
    app = PlainApp(svc)
    buf = io.StringIO()
    app.console = Console(file=buf, width=200, highlight=False)

    async def dead_turn(*a, **k):
        raise BackendError("cannot connect to backend (offline)")

    monkeypatch.setattr(app.rt, "run_turn", dead_turn)
    lines = iter(["hello", "/quit"])

    def fake_input(prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    rc = await app.run()  # pre-fix: BackendError escaped run() and killed the REPL
    assert rc == 0
    out = buf.getvalue()
    assert "turn failed" in out
    assert "/backend" in out


# --------------------------------------------------------------------- C4


def test_c4_busy_new_chat_409_no_swap(bridge):
    MockBackend.delay = 0.3
    try:
        sid_a = bridge.post("/api/chats", {"first_message": "chat A"})["id"]
        done = {}

        def send():
            resp = bridge.post("/api/message", {"text": "hello", "session": sid_a})
            raw = resp.read().decode()
            resp.close()
            for block in raw.split("\n\n"):
                if block.startswith("event: done"):
                    done.update(json.loads(block.split("data: ", 1)[1]))

        th = threading.Thread(target=send)
        th.start()
        for _ in range(300):
            if bridge.get("/api/status").get("busy"):
                break
            time.sleep(0.02)
        else:
            th.join(30)
            pytest.fail("turn never became busy")
        before = {c["id"] for c in bridge.get("/api/chats")["chats"]}
        r = bridge.post("/api/chats", {"first_message": "chat C"})
        assert r.get("__status__") == 409  # pre-fix: 200 + active-session swap
        assert "turn running" in r.get("error", "")
        assert bridge.get("/api/status")["session"] == sid_a  # no swap
        assert {c["id"] for c in bridge.get("/api/chats")["chats"]} == before
        th.join(30)
        assert not th.is_alive()
        assert done  # the running turn completed normally
    finally:
        MockBackend.delay = 0.0


# --------------------------------------------------------------------- C5


def test_c5_cosmetic_config_does_not_retire_runtime(svc):
    rt = svc.runtime()
    gen0 = svc.config.policy_generation
    cfg = svc.config.model_copy(deep=True)
    cfg.ui.theme = "leo-cosmetic"
    cfg.generation.temperature = 0.9
    svc.apply_config(cfg)
    assert rt._retired is False  # pre-fix: ANY config delta retired the runtime
    assert svc.config.policy_generation == gen0
    assert svc.config.ui.theme == "leo-cosmetic"
    assert svc.config.generation.temperature == 0.9
    # a real security delta must STILL retire (snapshot covers yolo)
    rt2 = svc.runtime()
    cfg2 = svc.config.model_copy(deep=True)
    cfg2.agent.yolo = True
    svc.apply_config(cfg2)
    assert rt2._retired is True
    assert svc.config.policy_generation > gen0


# --------------------------------------------------------------------- C6


async def test_c6_lmstudio_model_state_http_error_is_backend_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    with pytest.raises(BackendError):  # pre-fix: raw httpx.HTTPStatusError
        await be.ensure_loaded("m")


async def test_c6_lmstudio_load_cli_timeout_is_backend_error(monkeypatch):
    def handler(request):
        if request.url.path == "/api/v0/models":
            return httpx.Response(200, json={"data": [{"id": "m", "key": "m", "state": "not-loaded"}]})
        return httpx.Response(404)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    monkeypatch.setattr(be, "lms_cli", lambda: "/fake/bin/lms")  # stub-exec: never run

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="lms", timeout=600)

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(BackendError, match="CLI"):  # pre-fix: raw TimeoutExpired
        await be.ensure_loaded("m")


async def test_c6_lmstudio_stream_connect_error_is_backend_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    with pytest.raises(BackendError):  # pre-fix: raw httpx.ConnectError
        async for _ in be.stream_chat("m", [{"role": "user", "content": "hi"}],
                                      GenerationSettings()):
            pass


# --------------------------------------------------------------------- C7


@pytest.mark.parametrize("section", ["generation", "safety", "ui", "voice", "mail",
                                     "logging", "version"])
def test_c7_model_non_backend_section_400(bridge, section):
    r = bridge.post("/api/model", {"backend": section, "model": "x"})
    assert r.get("__status__") == 400  # pre-fix: AttributeError → 500
    assert "unknown backend" in r.get("error", "")


# --------------------------------------------------------------------- C8


async def test_c8_cloud_clients_never_inherit_proxy_env(monkeypatch):
    captured: list[dict] = []
    real_client = httpx.AsyncClient

    class SpyClient(real_client):
        def __init__(self, *a, **kw):
            captured.append(kw)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", SpyClient)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m", "context_length": 8192}]})
        if request.url.path.endswith("/messages"):
            text = ("event: message_delta\n"
                    'data: {"delta": {"stop_reason": "end_turn"},'
                    ' "usage": {"output_tokens": 2}}\n\n')
        else:
            text = "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys = {"openai": "k"}
    be = CloudBackend(cfg, httpx.MockTransport(handler))
    await be.health()
    await be.list_models()
    msgs = [{"role": "user", "content": "hi"}]
    async for _ in be._stream_openai("http://cloud.local", "k", "m", msgs,
                                     GenerationSettings(), None, False):
        pass
    cfg.cloud.provider = "anthropic"
    cfg.cloud.keys = {"anthropic": "k"}
    async for _ in be._stream_anthropic("http://cloud.local", "k", "m", msgs,
                                        GenerationSettings(), None, False):
        pass
    assert len(captured) == 4  # health, list_models, openai stream, anthropic stream
    for kw in captured:
        assert kw.get("trust_env") is False  # pre-fix: inherited HTTPS_PROXY
        assert kw.get("follow_redirects") is False


# --------------------------------------------------------------------- C9


async def test_c9_cloud_openai_stream_requests_usage():
    captured = {}
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    def handler(request):
        captured["body"] = json.loads(request.content)
        text = "".join(f"data: {p}\n\n" for p in (
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
            json.dumps({"choices": [], "usage": usage}),
            "[DONE]"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys = {"openai": "k"}
    be = CloudBackend(cfg, httpx.MockTransport(handler))
    chunks = [ch async for ch in be._stream_openai(
        "http://cloud.local", "k", "m", [{"role": "user", "content": "hi"}],
        GenerationSettings(), None, False)]
    assert captured["body"].get("stream_options") == {"include_usage": True}
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage  # pre-fix: usage=None on every cloud turn


# --------------------------------------------------------------------- C10


def test_c10_anthropic_assistant_first_history_gets_user_lead():
    be = CloudBackend(Config(), httpx.MockTransport(lambda r: httpx.Response(200)))
    msgs = [{"role": "system", "content": "sys"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "next question"}]
    _system, merged = be._to_anthropic(msgs)
    assert merged[0]["role"] == "user"  # pre-fix: assistant-first → Anthropic 400
    # a normal user-first history must NOT gain a synthetic lead
    normal = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
              {"role": "user", "content": "q2"}]
    _s2, merged2 = be._to_anthropic(normal)
    assert len(merged2) == 3
    assert merged2[0]["content"][0]["text"] == "q"


# --------------------------------------------------------------------- C11


async def test_c11_idle_ctrl_c_is_observable(svc, monkeypatch):
    app = PlainApp(svc)
    buf = io.StringIO()
    app.console = Console(file=buf, width=200, highlight=False)
    captured = {}
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler",
                        lambda sig, cb, *a: captured.setdefault(sig, cb))
    release = threading.Event()

    def fake_input(prompt=""):
        if not release.wait(10):
            raise AssertionError("input never released")
        raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    task = asyncio.create_task(app.run())
    import signal as _signal
    for _ in range(200):
        if _signal.SIGINT in captured:
            break
        await asyncio.sleep(0.02)
    assert _signal.SIGINT in captured, "SIGINT handler never registered"
    captured[_signal.SIGINT]()  # idle: no turn running, _cancel is None
    await asyncio.sleep(0.05)
    release.set()
    rc = await asyncio.wait_for(task, 10)
    assert rc == 0
    assert "Ctrl+C" in buf.getvalue()  # pre-fix: swallowed no-op


# --------------------------------------------------------------------- C12


def _cancel_rig(svc, tmp_path, delay):
    be = MockBackend(svc.config)
    be.delay = delay
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills, state_path=tmp_path / "state.json")
    return be, rt


async def test_c12_user_stop_cancel_publishes_idle(svc, tmp_path):
    _be, rt = _cancel_rig(svc, tmp_path, 0.25)
    cancel = asyncio.Event()
    first = asyncio.Event()
    sess = svc.new_session()

    def on_event(ev):
        if ev.get("type") == "token":
            first.set()

    task = asyncio.create_task(rt.run_turn(sess, "hello there", on_event=on_event, cancel=cancel))
    await asyncio.wait_for(first.wait(), 10)
    cancel.set()
    task.cancel()  # bridge Stop cancels the TASK as well as setting the event
    with pytest.raises(asyncio.CancelledError):
        await task
    snap = json.loads((tmp_path / "state.json").read_text())
    assert snap["state"] == "idle"  # pre-fix: phantom "error" on a user stop


async def test_c12_non_user_cancel_publishes_error(svc, tmp_path):
    _be, rt = _cancel_rig(svc, tmp_path, 0.25)
    first = asyncio.Event()
    sess = svc.new_session()

    def on_event(ev):
        if ev.get("type") == "token":
            first.set()

    task = asyncio.create_task(rt.run_turn(sess, "hello there", on_event=on_event))
    await asyncio.wait_for(first.wait(), 10)
    task.cancel()  # no cancel event → a real failure, not a user stop
    with pytest.raises(asyncio.CancelledError):
        await task
    snap = json.loads((tmp_path / "state.json").read_text())
    assert snap["state"] == "error"


# --------------------------------------------------------------------- C13


def test_c13_int_float_fields_accepted(bridge):
    r = bridge.post("/api/config/agent", {"tool_timeout": 120})
    assert r.get("__status__") != 400  # pre-fix: StrictFloat rejected JSON ints
    assert r.get("ok") is True
    r2 = bridge.post("/api/config/agent", {"compact_ratio": 1})
    # int 1 coerced to float THEN range-validated (le=0.95): a range error
    # proves the coercion ran (pre-fix: a type error before any range check)
    assert r2.get("__status__") == 400
    assert "at most 0.95" in r2.get("error", "")
    assert bridge.post("/api/config/agent", {"compact_ratio": 0.5}).get("ok") is True
    # bool is still rejected for float fields
    r3 = bridge.post("/api/config/agent", {"tool_timeout": True})
    assert r3.get("__status__") == 400


# --------------------------------------------------------------------- C14


class _DupBe(MockBackend):
    """Always asks for the SAME read-only call — the echo breaker must fire."""

    async def _stream(self, model, messages, tools, native, params=None):
        yield ChatChunk(content="", tool_calls=[_read_call(0)],
                        done=True, finish="tool_calls", usage={"eval_count": 5})


def test_c14_duplicate_call_signature_breaks_loop(svc):
    svc.config.permission_mode = PermissionMode.SANDBOXED
    svc.config.agent.native_tools = "force"
    svc.config.agent.agent_loop = True
    svc.config.agent.max_steps = 8
    (svc.workspace / "a.txt").write_text("file body")  # read-only, fail-safe
    be = _DupBe(svc.config)
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills)
    events: list[dict] = []
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "read the file", on_event=events.append))
    breaks = [e for e in events if e.get("type") == "agentloop_echo_break"]
    assert breaks, "duplicate-call echo breaker never fired (dead 'cached' check)"
    assert res.steps == 3  # 1 original + 2 duplicates, not the whole 24-step budget
    assert breaks[0]["steps"] == 3


# --------------------------------------------------------------------- C15


async def test_c15_force_final_uses_cloud_context(svc, monkeypatch):
    import ohmyllm.agents.runtime as RT

    budgets: list[int] = []
    real_trim = RT.trim_messages

    def spy_trim(history, budget):
        budgets.append(budget)
        return real_trim(history, budget)

    monkeypatch.setattr(RT, "trim_messages", spy_trim)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: [DONE]\n\n")

    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys = {"openai": "k"}
    cloud = CloudBackend(cfg, httpx.MockTransport(handler))
    cloud._ctx_map_ = {"m": 200000}
    rt = AgentRuntime(backend=cloud, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul, skills=svc.skills)
    sess = svc.new_session()
    sess.add_message("user", "hi")
    params = GenerationSettings(num_ctx=2048)
    await rt._force_final(sess, "m", params, lambda e: None, None)
    assert budgets[-1] == 600000  # pre-fix: local num_ctx*3 = 6144 for cloud too

    budgets.clear()
    mock = MockBackend(svc.config)
    rt2 = AgentRuntime(backend=mock, registry=svc.registry, policy=svc.policy,
                       approvals=svc.approvals, audit=svc.audit, config=svc.config,
                       soul=svc.soul, skills=svc.skills)
    sess2 = svc.new_session()
    sess2.add_message("user", "hi")
    await rt2._force_final(sess2, "m", params, lambda e: None, None)
    assert budgets[-1] == 6144  # local path unchanged


# --------------------------------------------------------------------- C16


def _sse(*payloads):
    return "".join(f"data: {p}\n\n" for p in payloads)


async def test_c16_indexless_tool_deltas_key_by_server_id():
    def handler(request):
        text = _sse(
            json.dumps({"choices": [{"delta": {"tool_calls": [
                {"id": "srv_a", "function": {"name": "read_file",
                                             "arguments": '{"path": "a.txt"}'}}]},
                "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {"tool_calls": [
                {"id": "srv_b", "function": {"name": "list_dir",
                                             "arguments": '{"path": "."}'}}]},
                "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            "[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    calls = done[0].tool_calls
    # pre-fix: both deltas merged into index 0 → one call with corrupted args
    assert len(calls) == 2
    assert {c["id"] for c in calls} == {"srv_a", "srv_b"}
    by_id = {c["id"]: c for c in calls}
    assert by_id["srv_a"]["function"]["name"] == "read_file"
    assert by_id["srv_a"]["function"]["arguments"] == {"path": "a.txt"}
    assert by_id["srv_b"]["function"]["name"] == "list_dir"
    assert by_id["srv_b"]["function"]["arguments"] == {"path": "."}


# --------------------------------------------------------------------- C17


async def test_c17_midstream_error_no_retry_no_duplicate():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        text = _sse(json.dumps({"choices": [{"delta": {"content": "Hello world. "},
                                             "finish_reason": None}]}))
        text += ('event: error\n'
                 'data: {"error": {"message": "reasoning_effort unsupported"}}\n\n')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    emitted = []
    with pytest.raises(BackendError):
        async for ch in be.stream_chat("m", [{"role": "user", "content": "hi"}],
                                       GenerationSettings(effort="high")):
            if ch.content:
                emitted.append(ch.content)
    assert len(requests) == 1  # pre-fix: retried after streaming → duplicate text
    assert "".join(emitted) == "Hello world. "


async def test_c17_pre_stream_error_still_retries_once():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, text='{"error": "reasoning_effort unsupported"}')
        text = _sse(
            json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}),
            "[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings(effort="high"))]
    assert len(bodies) == 2
    assert "reasoning_effort" not in bodies[1]
    assert "reasoning" not in bodies[1]
    assert "".join(ch.content for ch in chunks) == "ok"  # exactly once


# --------------------------------------------------------------------- C18


def test_c18_market_download_check_then_set_atomic(bridge, monkeypatch):
    import ohmyllm.market as M

    async def fake_download(entry, state, cancel=None, **kw):
        await asyncio.sleep(0.4)  # stub: no network, no real file
        state.done = True
        return state

    monkeypatch.setattr(M, "download", fake_download)
    barrier = threading.Barrier(2, timeout=10)
    results: list[dict] = []

    def post():
        barrier.wait()
        results.append(bridge.post("/api/market/download", {"mid": "stt-sensevoice"}))

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    codes = sorted(r.get("__status__", 200) for r in results)
    assert codes == [200, 409]  # pre-fix: both passed the check → two downloads
    deadline = time.time() + 10
    while time.time() < deadline:
        prog = bridge.get("/api/market/progress")
        if prog.get("done"):
            break
        time.sleep(0.05)
    assert prog.get("done") is True
    assert not prog.get("error")


def test_c18_voice_record_start_atomic(bridge, monkeypatch):
    import ohmyllm.voice as V

    class FakeRec:  # stub: no pw-record/arecord subprocess ever spawns
        instances = 0

        def __init__(self, out):
            self.out = out
            self._running = False
            FakeRec.instances += 1

        @property
        def running(self):
            return self._running

        def start(self):
            time.sleep(0.35)
            self._running = True
            return None

        def stop(self):
            self._running = False

    monkeypatch.setattr(V, "Recorder", FakeRec)
    barrier = threading.Barrier(2, timeout=10)
    results: list[dict] = []

    def post():
        barrier.wait()
        results.append(bridge.post("/api/voice/record", {"action": "start"}))

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert FakeRec.instances == 1  # pre-fix: two recorders spawned, one leaked
    assert sum(1 for r in results if r.get("already")) == 1
    assert all(r.get("ok") for r in results)
    stop = bridge.post("/api/voice/record", {"action": "stop"})
    assert stop.get("__status__") == 500  # fake wav absent → "too short", talk cleared
    assert "too short" in stop.get("error", "")
    stop2 = bridge.post("/api/voice/record", {"action": "stop"})
    assert stop2.get("note") == "not recording"


# --------------------------------------------------------------------- C19


def test_c19_unknown_session_404_not_silent_fallback(bridge):
    sid = bridge.post("/api/chats", {"first_message": "real chat"})["id"]
    r = bridge.post("/api/message", {"text": "MARKER_UNKNOWN", "session": "ffffffffffff"})
    assert r.get("__status__") == 404  # pre-fix: silent fallback into current chat
    assert "unknown session" in r.get("error", "")
    assert bridge.get("/api/status")["session"] == sid
    msgs = bridge.get(f"/api/chats/{sid}")["messages"]
    assert not any("MARKER_UNKNOWN" in str(m.get("content")) for m in msgs)


# --------------------------------------------------------------------- C20


def test_c20_incremental_echo_scan_matches_one_shot():
    from ohmyllm.agents.runtime import _EchoScanner  # lazy: absent pre-fix

    block = "".join(random.Random(7).choices(string.ascii_letters + " ", k=400))
    text = block + block
    one = AgentRuntime._self_repeats(text)
    assert one == 400
    for chunk in (100, 300, 2000):  # repeat must be found across chunk boundaries
        scan = _EchoScanner()
        verdict = None
        for i in range(0, len(text), chunk):
            verdict = scan.feed(text[:i + chunk])
            if verdict is not None:
                break
        assert verdict == one, f"chunk={chunk}"
    rnd = "".join(random.Random(11).choices(string.ascii_letters + string.digits, k=20000))
    assert AgentRuntime._self_repeats(rnd) is None
    scan2 = _EchoScanner()
    assert scan2.feed(rnd) is None


def test_c20_incremental_echo_scan_is_linear():
    from ohmyllm.agents.runtime import _EchoScanner

    # every 300-char window unique with overwhelming probability (random)
    alphabet = string.ascii_letters + string.digits
    text = "".join(random.Random(3).choices(alphabet, k=100000))
    scan = _EchoScanner()
    t0 = time.perf_counter()
    verdict = None
    for i in range(0, len(text), 2000):
        verdict = scan.feed(text[:i + 2000])
        if verdict is not None:
            break
    elapsed = time.perf_counter() - t0
    assert verdict is None
    assert elapsed < 2.0  # pre-fix O(n^2) re-scan: ~50 full passes over 100k


# --------------------------------------------------------------------- C21


@pytest.mark.parametrize("value,expected", [
    ("off", False), ("disabled", False), ("none", False), ("null", False),
    ("nope", False), ("OFF", False), (" false ", False),
    ("on", True), ("enabled", True), ("TRUE", True),
    ("true", True), ("false", False), ("1", True), ("0", False),
    ("yes", True), ("no", False)])
def test_c21_boolean_string_vocabulary(value, expected):
    spec = ParamSpec(name="flag", type="boolean", description="d")
    num, err = _coerce(value, spec)
    assert err is None
    assert num is expected  # pre-fix: bool("off") → True (semantic inversion)


def test_c21_boolean_unknown_string_rejected():
    spec = ParamSpec(name="flag", type="boolean", description="d")
    num, err = _coerce("maybe", spec)
    assert num is None
    assert err
    assert _coerce(2, spec) == (True, None)  # non-str truthiness unchanged
    assert _coerce(True, spec) == (True, None)


# --------------------------------------------------------------------- U4


def test_u4_skill_cast_args_additive(bridge, oml_env):
    skill_dir = oml_env["home"] / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill_text = "---\nname: demo\ndescription: Demo skill for tests\n---\n\nDo the demo thing.\n"
    (skill_dir / "SKILL.md").write_text(skill_text)
    sid = bridge.post("/api/chats", {"first_message": "chat"})["id"]
    legacy = (f'<skill name="demo">\n{skill_text}\n</skill>\n'
              "Apply this skill to my requests in this chat.")
    # no args → byte-identical legacy message (existing field semantics unchanged)
    r = bridge.post("/api/skills/cast", {"name": "demo", "session": sid})
    assert r.get("ok") is True
    msg = bridge.get(f"/api/chats/{sid}")["messages"][-1]["content"]
    assert msg == legacy
    # empty args → still legacy
    bridge.post("/api/skills/cast", {"name": "demo", "session": sid, "args": ""})
    msg2 = bridge.get(f"/api/chats/{sid}")["messages"][-1]["content"]
    assert msg2 == legacy
    # trailing instruction is appended and passed into the invocation
    bridge.post("/api/skills/cast", {"name": "demo", "session": sid,
                                     "args": "focus on the README"})
    msg3 = bridge.get(f"/api/chats/{sid}")["messages"][-1]["content"]
    assert msg3.startswith(legacy)
    assert msg3.endswith("Additional instruction from the user: focus on the README")
    # type + whitelist guards still apply to the new field
    assert bridge.post("/api/skills/cast", {"name": "demo", "session": sid,
                                            "args": 123}).get("__status__") == 400
    assert bridge.post("/api/skills/cast", {"name": "demo", "session": sid,
                                            "bogus": "x"}).get("__status__") == 400


# --------------------------------------------------------------------- U12


def test_u12_plain_help_lists_remember(svc):
    app = PlainApp(svc)
    buf = io.StringIO()
    app.console = Console(file=buf, width=200, highlight=False)
    app._print_help()
    assert "/remember" in buf.getvalue()


# --------------------------------------------------------------------- U3
# Pre-existing yolo wipe (Jimmy's r3ux-u3-probe): arming via /api/config/agent
# persists config.toml (yolo itself is exclude=True), the mtime move makes the
# next /api/status reload from disk, and the wholesale replace resets yolo.


def test_u3_yolo_survives_bridge_disk_sync(bridge, oml_env):
    import os

    # B.serve boots its OWN Services against the isolated home — assert
    # purely over HTTP; the config file is the shared out-of-band channel.
    path = ConfigStore().path
    assert bridge.get("/api/status")["yolo"] is False
    r = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
    assert r.get("ok") is True
    gen = bridge.get("/api/config")["policy_generation"]
    # the arm's own save moved the mtime: the next status syncs from disk.
    # Pre-fix: reload wholesale-replaced the config and wiped yolo -> False.
    assert bridge.get("/api/status")["yolo"] is True
    # an out-of-band save (TUI process) must not disarm it either
    t = path.stat().st_mtime
    os.utime(path, (t + 2, t + 2))
    assert bridge.get("/api/status")["yolo"] is True
    # C5 guard: preserving yolo means NO security delta on a no-op reload
    # (a retire would bump policy_generation)
    assert bridge.get("/api/config")["policy_generation"] == gen
    # a genuine disk edit of OTHER settings must still apply
    store = ConfigStore(path)
    disk = store.load()
    disk.ui.theme = "u3-disk-edit"
    store.save(disk)
    assert bridge.get("/api/status")["yolo"] is True
    assert bridge.get("/api/config")["ui"]["theme"] == "u3-disk-edit"
    # yolo off stays off across the same sync path
    assert bridge.post("/api/config/agent",
                       {"yolo": False, "confirm": True}).get("ok") is True
    t = path.stat().st_mtime
    os.utime(path, (t + 2, t + 2))
    assert bridge.get("/api/status")["yolo"] is False


def test_u3_reload_config_preserves_runtime_flags(svc):
    svc.config.agent.yolo = True  # in-memory arm, exactly like /yolo
    rt = svc.runtime()
    gen = svc.config.policy_generation
    store = svc.config_store
    disk = store.load()
    disk.ui.theme = "u3-core-disk-edit"
    store.save(disk)
    svc.reload_config()
    # pre-fix: the disk copy has no yolo (exclude=True) -> reset to False
    assert svc.config.agent.yolo is True
    # real disk edits still land
    assert svc.config.ui.theme == "u3-core-disk-edit"
    # no security delta -> no runtime retirement, no generation bump (C5)
    assert rt._retired is False
    assert svc.config.policy_generation == gen
    # a genuine SECURITY disk edit must still retire the runtime
    disk2 = store.load()
    disk2.safety.deny = disk2.safety.deny + ["u3-extra"]
    store.save(disk2)
    svc.reload_config()
    assert rt._retired is True
    assert svc.config.agent.yolo is True
