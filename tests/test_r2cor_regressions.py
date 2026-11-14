"""R2 correctness regressions — Oscar's fresh red-team (task #54).

One test per finding: C1 loop floor, C2 cloud read cap + Stop task cancel,
C3 Anthropic strict alternation, W4 cross-backend /api/model, W5 ollama
think wire key, W6 lmstudio stream usage, W7 plain /rename + slash boundary,
S8 validator round-before-int, S9 cross-session busy 409.
"""

import asyncio
import json
import threading
import time

import httpx
import pytest

from ohmyllm import bridge as B
from ohmyllm.agents.validator import _coerce
from ohmyllm.backends.base import ChatChunk
from ohmyllm.backends.cloud import CloudBackend
from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.backends.ollama import OllamaBackend
from ohmyllm.config import Config, GenerationSettings
from ohmyllm.plain import PlainApp
from ohmyllm.tools.spec import ParamSpec

# --------------------------------------------------------------------- C1


class _LoopBe(MockBackend):
    """Every turn asks for another tool call — the loop must ride the floor."""

    def __init__(self, config, transport=None):
        super().__init__(config, transport)
        self.n = 0

    async def _stream(self, model, messages, tools, native, params=None):
        self.n += 1
        yield ChatChunk(
            content="",
            tool_calls=[{"id": f"c{self.n}", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": {"path": f"f{self.n}.txt"}}}],
            done=True, finish="tool_calls", usage={"eval_count": 5})


async def test_c1_agent_loop_floor_holds(svc):
    svc.config.agent.agent_loop = True
    svc.config.agent.max_steps = 8
    svc.config.agent.native_tools = "force"
    be = _LoopBe(svc.config)
    svc._backend_cache["mock"] = be
    res = await svc.runtime().run_turn(svc.new_session(), "do many things")
    assert res.steps == 24  # the floor, not the 8-step setting
    assert be.n >= 24


async def test_c1_non_loop_honors_max_steps(svc):
    svc.config.agent.agent_loop = False
    svc.config.agent.max_steps = 8
    svc.config.agent.native_tools = "force"
    be = _LoopBe(svc.config)
    svc._backend_cache["mock"] = be
    res = await svc.runtime().run_turn(svc.new_session(), "do many things")
    assert res.steps == 8


# --------------------------------------------------------------------- C2


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("timeout,expected", [(2.0, 300.0), (600.0, 600.0), (5000.0, 1800.0)])
async def test_c2_cloud_read_gap_bounded(provider, timeout, expected):
    cfg = Config()
    cfg.cloud.provider = provider
    cfg.cloud.keys = {provider: "k"}
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        if provider == "openai":
            text = "data: [DONE]\n\n"
        else:
            text = ("event: message_delta\n"
                    'data: {"delta": {"stop_reason": "end_turn"},'
                    ' "usage": {"output_tokens": 2}}\n\n')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = CloudBackend(cfg, httpx.MockTransport(handler))
    gen = GenerationSettings(timeout=timeout)
    msgs = [{"role": "user", "content": "hi"}]
    if provider == "openai":
        stream = be._stream_openai("http://cloud.local", "k", "m", msgs, gen, None, False)
    else:
        stream = be._stream_anthropic("http://cloud.local", "k", "m", msgs, gen, None, False)
    chunks = [ch async for ch in stream]
    assert seen["timeout"]["read"] == expected  # was None: silent provider = forever hang
    assert chunks[-1].done


async def test_c2_cancel_active_cancels_turn_task(oml_env):
    """Stop must cancel the turn TASK, not just set an event a silent
    provider read never observes (bridge.cancel_active -> active.cancel())."""
    state = B.BridgeState()
    started = asyncio.Event()

    async def slow_turn():
        started.set()
        await asyncio.sleep(30)  # silent stall: no chunks, no cancel checks

    state.loop = asyncio.get_running_loop()
    state.cancel = asyncio.Event()
    fut = asyncio.run_coroutine_threadsafe(slow_turn(), state.loop)
    state.active_turn = fut
    try:
        await asyncio.wait_for(started.wait(), 5)
        state.cancel_active()
        assert state.cancel.is_set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.wrap_future(fut), 5)
    finally:
        fut.cancel()


# --------------------------------------------------------------------- C3


def test_c3_anthropic_strict_alternation():
    cfg = Config()
    cfg.cloud.provider = "anthropic"
    cfg.cloud.keys = {"anthropic": "k"}
    be = CloudBackend(cfg)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do two things"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"x"}'}},
            {"id": "b", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"y"}'}}]},
        {"role": "tool", "content": "r1", "tool_call_id": "a", "name": "read_file"},
        {"role": "tool", "content": "r2", "tool_call_id": "b", "name": "read_file"},
        {"role": "user", "content": "continue"},
    ]
    system, out = be._to_anthropic(msgs)
    assert system == "sys"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    tail = out[-1]["content"]
    assert [blk["type"] for blk in tail] == ["tool_result", "tool_result", "text"]
    assert [blk["tool_use_id"] for blk in tail[:2]] == ["a", "b"]
    assert tail[2]["text"] == "continue"


def test_c3_anthropic_merges_consecutive_assistants():
    cfg = Config()
    cfg.cloud.provider = "anthropic"
    be = CloudBackend(cfg)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    _, out = be._to_anthropic(msgs)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert [blk["text"] for blk in out[1]["content"]] == ["one", "two"]


# ------------------------------------------------------------------ W4/S9


def test_w4_set_model_switches_default_backend(bridge):
    assert bridge.get("/api/status")["backend"] == "mock"
    r = bridge.post("/api/model", {"backend": "lmstudio", "model": "some-lm-model"})
    assert r.get("ok") is True
    st = bridge.get("/api/status")
    assert st["backend"] == "lmstudio"  # was: silent no-op, next turn ran on mock
    assert st["model"] == "some-lm-model"
    # same-backend pick keeps working
    r = bridge.post("/api/model", {"backend": "lmstudio", "model": "other"})
    assert r.get("ok") is True
    assert bridge.get("/api/status")["model"] == "other"
    # bad requests still rejected
    assert bridge.post("/api/model", {"backend": "nope", "model": "x"}).get("__status__") == 400
    assert bridge.post("/api/model", {"backend": "mock", "model": ""}).get("__status__") == 400


def test_s9_cross_session_message_while_busy_409(bridge):
    MockBackend.delay = 0.3
    try:
        sid_a = bridge.post("/api/chats", {"first_message": "chat A"})["id"]
        sid_b = bridge.post("/api/chats", {"first_message": "chat B"})["id"]
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
        r = bridge.post("/api/message", {"text": "MARKER_XYZ", "session": sid_b})
        assert r.get("__status__") == 409  # was 202: misrouted into chat A
        assert "another chat" in r.get("error", "")
        # same-chat interjection still queues
        r2 = bridge.post("/api/message", {"text": "same chat note", "session": sid_a})
        assert r2.get("queued") is True
        th.join(30)
        assert not th.is_alive()
        assert done  # the turn completed normally
        for sid in (sid_a, sid_b):
            msgs = bridge.get(f"/api/chats/{sid}")["messages"]
            assert not any("MARKER_XYZ" in str(m.get("content")) for m in msgs)
    finally:
        MockBackend.delay = 0.0


# --------------------------------------------------------------------- W5


@pytest.mark.parametrize("effort,expected", [
    ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", True), ("max", True)])
async def test_w5_ollama_think_wire_key(effort, expected):
    captured = {}

    async def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"template": "{{ if .Think }}think{{ end }}"})
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=json.dumps(
            {"message": {"content": "ok"}, "done": True, "eval_count": 3}) + "\n")

    be = OllamaBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "qwen3", [{"role": "user", "content": "hi"}], GenerationSettings(effort=effort))]
    assert captured["body"].get("think") == expected  # was "thinking": ignored by ollama
    assert "thinking" not in captured["body"]
    assert chunks[-1].done


async def test_w5_ollama_400_retry_drops_think():
    bodies = []

    async def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"template": "{{ if .Think }}think{{ end }}"})
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, text='{"error": "model does not support thinking"}')
        return httpx.Response(200, text=json.dumps(
            {"message": {"content": "ok"}, "done": True, "eval_count": 3}) + "\n")

    be = OllamaBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "qwen3", [{"role": "user", "content": "hi"}], GenerationSettings(effort="high"))]
    assert bodies[0].get("think") == "high"
    assert "think" not in bodies[1]  # retry guard must strip the NEW key
    assert chunks[-1].done
    assert be._think_ok.get("qwen3") is False


# --------------------------------------------------------------------- W6


def _sse(*payloads):
    return "".join(f"data: {p}\n\n" for p in payloads)


async def test_w6_lmstudio_usage_on_done_exit():
    captured = {}
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    def handler(request):
        captured["body"] = json.loads(request.content)
        text = _sse(
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
            json.dumps({"choices": [], "usage": usage}),
            "[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    assert captured["body"]["stream_options"] == {"include_usage": True}
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage  # was None: [DONE] terminal carried no usage
    assert len(chunks) == 2  # the usage-only trailer emitted no phantom chunk


async def test_w6_lmstudio_usage_on_finish_exit():
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    def handler(request):
        text = _sse(
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
            json.dumps({"choices": [], "usage": usage}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "[DONE]")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].finish == "stop"
    assert done[0].usage == usage  # stashed trailer attaches on the finish exit too


# --------------------------------------------------------------------- W7


async def test_w7_rename_flattens_multimodal(svc):
    app = PlainApp(svc)
    app.sess.add_message("user", [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
    await app._slash("/rename")  # was TypeError: sequence item 0: expected str, list
    assert "look" in app.sess.title.lower()


async def test_w7_slash_exception_does_not_kill_repl(svc, monkeypatch):
    app = PlainApp(svc)
    printed = []
    monkeypatch.setattr(app.console, "print",
                        lambda *a, **k: printed.append(str(a[0]) if a else ""))

    async def boom(line):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(app, "_slash", boom)
    lines = iter(["/boom"])

    def fake_input(prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    rc = await app.run()
    assert rc == 0  # EOF exit, not a crash
    assert any("command failed" in p and "kaboom" in p for p in printed)


# --------------------------------------------------------------------- S8


@pytest.mark.parametrize("value,expected", [
    (4096.7, 4097), (4096.2, 4096), (4096.0, 4096), ("4096.7", 4097),
    (4097, 4097), (2**53 + 1, 2**53 + 1)])
def test_s8_integer_coercion_rounds_before_int(value, expected):
    spec = ParamSpec(name="max_tokens", type="integer", description="d")
    num, err = _coerce(value, spec)
    assert err is None
    assert num == expected  # was int() first: 4096.7 -> 4096 (round() was dead code)


def test_s8_integer_coercion_still_rejects_junk():
    spec = ParamSpec(name="max_tokens", type="integer", description="d")
    for bad in (True, "abc", object()):
        num, err = _coerce(bad, spec)
        assert num is None
        assert err
