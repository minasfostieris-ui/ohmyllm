import asyncio
import json

import httpx
import pytest

from ohmyllm.backends import LMStudioBackend, OllamaBackend, make_backend
from ohmyllm.config import Config


def ollama_transport(routes: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.method + " " + request.url.path
        for k, v in routes.items():
            if k == key or k.endswith(" " + request.url.path):
                if callable(v):
                    return v(request)
                return v
        return httpx.Response(404, json={"error": "no route " + key})

    return httpx.MockTransport(handler)


TAGS = {"models": [
    {"name": "qwen2.5:7b-instruct-q4_K_M", "model": "qwen2.5:7b-instruct-q4_K_M",
     "size": 4_700_000_000, "digest": "abc", "modified_at": "2025-01-02T03:04:05Z",
     "details": {"quantization_level": "Q4_K_M", "parameter_size": "7.6B"}},
    {"name": "tiny:1b", "model": "tiny:1b", "size": 900_000_000,
     "details": {"quantization_level": "Q4_0", "parameter_size": "1B"}},
]}


def test_ollama_health_and_tags():
    cfg = Config()
    be = OllamaBackend(cfg, transport=ollama_transport({
        "GET /api/version": httpx.Response(200, json={"version": "0.3.5"}),
        "GET /api/tags": httpx.Response(200, json=TAGS),
    }))
    ok, info = __import__("asyncio").run(be.health())
    assert ok and info["version"] == "0.3.5"
    models = __import__("asyncio").run(be.list_models())
    assert models[0].quant == "Q4_K_M"
    assert models[0].params_b == 7.6
    assert models[0].tool_capable is True
    assert models[1].tool_capable is False  # 1B too small
    assert "below the reliability floor" in models[1].why_tool_weak()


def test_ollama_chat_stream_and_tools():
    cfg = Config()

    def chat(request):
        body = json.loads(request.content)
        assert body["model"] == "m"
        assert body["options"]["num_ctx"] == cfg.generation.num_ctx
        if body.get("tools"):
            payload = (
                '{"model":"m","message":{"role":"assistant","content":"","tool_calls":'
                '[{"function":{"name":"soul_append","arguments":{"section":"User","text":"x"}}}]},"done":true}\n'
            )
        else:
            payload = (
                '{"message":{"role":"assistant","content":"Hel"}}\n'
                '{"message":{"role":"assistant","content":"lo"}}\n'
                '{"message":{"role":"assistant","content":""},"done":true,"eval_count":12,"prompt_eval_count":30}\n'
            )
        return httpx.Response(200, content=payload.encode())


    async def drain(gen):
        chunks = []
        async for c in gen:
            chunks.append(c)
        return chunks

    be = OllamaBackend(cfg, transport=ollama_transport({"POST /api/chat": chat}))
    chunks = asyncio.run(drain(be.stream_chat("m", [{"role": "user", "content": "hi"}], cfg.generation)))
    assert "".join(c.content for c in chunks) == "Hello"
    assert chunks[-1].usage["eval_count"] == 12
    tc = asyncio.run(drain(be.stream_chat("m", [{"role": "user", "content": "hi"}], cfg.generation,
                            tools=[{"type": "function", "function": {"name": "soul_append", "parameters": {}}}],
                            native_tools=True)))
    assert tc[-1].tool_calls and tc[-1].tool_calls[0]["function"]["name"] == "soul_append"


def test_ollama_pull_progress():
    cfg = Config()

    def pull(request):
        lines = [
            json.dumps({"status": "pulling manifest"}),
            json.dumps({"status": "downloading", "total": 100, "completed": 40}),
            json.dumps({"status": "success"}),
        ]
        return httpx.Response(200, content=("\n".join(lines)).encode())

    seen = []
    be = OllamaBackend(cfg, transport=ollama_transport({"POST /api/pull": pull}))

    asyncio.run(be.pull("qwen2.5:7b", progress=lambda s, p: seen.append((s, p))))
    assert ("pulling manifest", None) in seen
    assert ("downloading", 0.4) in seen
    assert seen[-1][0] == "success"


def test_default_config_never_autospawns_servers():
    """Starting oml must not launch LM Studio/Ollama behind the user's back:
    both auto_start flags default false, the probe helper agrees, and
    ensure_running on a dead-but-installed Ollama declines to spawn."""
    import shutil

    cfg = Config()
    assert cfg.ollama.auto_start is False
    assert cfg.lmstudio.auto_start is False
    from ohmyllm.config import ConfigStore
    from ohmyllm.core import Services
    svc = Services(ConfigStore(), cfg)
    assert svc.may_autostart("ollama") is False
    assert svc.may_autostart("lmstudio") is False

    calls: list = []
    real_popen = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        raise AssertionError("subprocess launched despite auto_start=false")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/ollama")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    be = OllamaBackend(cfg, transport=ollama_transport({
        "GET /api/version": httpx.Response(500),
    }))
    res = asyncio.run(be.ensure_running())
    assert res.ok is False
    assert "Auto-start is disabled" in res.message
    assert calls == []
    monkeypatch.undo()
    assert real_popen is asyncio.create_subprocess_exec


def test_ollama_ensure_running_when_live():
    cfg = Config()
    be = OllamaBackend(cfg, transport=ollama_transport({
        "GET /api/version": httpx.Response(200, json={"version": "0.3.5"}),
    }))

    res = asyncio.run(be.ensure_running())
    assert res.ok and res.was_already_running


def test_ollama_missing_binary_instructions(monkeypatch):
    cfg = Config()
    monkeypatch.setattr("ohmyllm.backends.ollama.shutil.which", lambda x: None)
    be = OllamaBackend(cfg, transport=ollama_transport({"GET /api/version": httpx.Response(500)}))

    res = asyncio.run(be.ensure_running())
    assert not res.ok and any("ollama.com" in i for i in res.instructions)


SSE = (
    b'data: {"choices":[{"delta":{"content":"Di"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"ng!"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":3}}\n\n'
    b"data: [DONE]\n\n"
)


def test_lmstudio_stream_sse():
    cfg = Config()

    def chat(request):
        body = json.loads(request.content)
        if body.get("tools"):
            chunks = [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"soul_search","arguments":"{\\"qu"}}]}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ery\\":\\"x\\"}"}}]}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
            ]
            return httpx.Response(200, content=("\n\n".join(chunks) + "\n\n").encode(),
                    headers={"content-type": "text/event-stream"})
        return httpx.Response(200, content=SSE,
                              headers={"content-type": "text/event-stream"})


    async def drain(gen):
        return [c async for c in gen]

    be = LMStudioBackend(cfg, transport=ollama_transport({
        "GET /v1/models": httpx.Response(200, json={"data": [{"id": "local/model-1", "created": 1}]}),
        "GET /api/v0/models": httpx.Response(404),
        "POST /v1/chat/completions": chat,
    }))
    text = asyncio.run(drain(be.stream_chat("local/model-1", [{"role": "user", "content": "x"}], cfg.generation)))
    assert "".join(c.content for c in text) == "Ding!"
    tc = asyncio.run(drain(be.stream_chat("local/model-1", [{"role": "user", "content": "x"}], cfg.generation,
                            tools=[{"type": "function", "function": {"name": "soul_search", "parameters": {}}}],
                            native_tools=True)))[-1]
    assert tc.tool_calls[0]["function"]["name"] == "soul_search"
    assert tc.tool_calls[0]["function"]["arguments"] == {"query": "x"}


def test_lmstudio_models_rich_metadata():
    cfg = Config()
    be = LMStudioBackend(cfg, transport=ollama_transport({
        "GET /v1/models": httpx.Response(200, json={"data": [{"id": "qwen2.5-coder", "context_length": 16384}]}),
        "GET /api/v0/models": httpx.Response(200, json={"data": [{
            "id": "qwen2.5-coder", "quantization": "Q4_K_M", "vram_required": 4_700_000_000,
            "capabilities": ["tools"], "ctx_length": 32768, "loaded": True,
        }]}),
    }))

    models = asyncio.run(be.list_models())
    m = models[0]
    assert m.quant == "Q4_K_M" and m.size_bytes == 4_700_000_000
    assert m.tool_capable is True and m.ctx_len == 16384 and m.loaded is True


def test_mock_backend_memories_and_pull():
    cfg = Config()

    be = make_backend("mock", cfg)

    async def collect(user_text, tools=None, native=False):
        parts, calls = [], []
        async for c in be.stream_chat("mock-tool-7b", [{"role": "user", "content": user_text}], cfg.generation,
                                      tools=tools, native_tools=native):
            parts.append(c.content)
            if c.tool_calls:
                calls = c.tool_calls
        return "".join(parts), calls

    schema = [{"type": "function", "function": {"name": "soul_append", "parameters": {}}}]
    text, calls = asyncio.run(collect("hello my name is Minas", tools=schema, native=True))
    assert calls and calls[0]["function"]["name"] == "soul_append"
    t2, c2 = asyncio.run(collect("I have a rabbit named Bnuy", tools=schema, native=False))
    assert "soul_append" in t2 and "{" in t2  # fenced JSON text protocol
    seen = []
    asyncio.run(be.pull("new-mock-model", progress=lambda s, p: seen.append(p)))
    names = [m.name for m in asyncio.run(be.list_models())]
    assert "new-mock-model" in names


def _sse_body(deltas: list[dict]) -> str:
    lines = []
    for d in deltas:
        lines.append("data: " + json.dumps({"choices": [d]}) + "\n")
    lines.append("data: [DONE]\n")
    return "\n".join(lines)


def _lmstudio_stream(deltas: list[dict], captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text=_sse_body(deltas), headers={"content-type": "text/event-stream"})
    return httpx.MockTransport(handler)


def test_lmstudio_sends_unload_false():

    cap: dict = {}
    be = LMStudioBackend(Config(), transport=_lmstudio_stream(
        [{"delta": {"content": "hi"}, "finish_reason": None},
         {"delta": {}, "finish_reason": "stop"}], cap))
    out = asyncio.run(_collect(be, "m"))
    assert out == "hi"
    assert cap["body"]["unload_model"] is False, "model must stay resident between prompts"


def test_lmstudio_reasoning_only_falls_back_to_content():

    cap: dict = {}
    be = LMStudioBackend(Config(), transport=_lmstudio_stream(
        [{"delta": {"reasoning": "I am "}, "finish_reason": None},
         {"delta": {"reasoning": "a local model"}, "finish_reason": None},
         {"delta": {}, "finish_reason": "stop"}], cap))
    out = asyncio.run(_collect(be, "gpt-oss"))
    assert out == "I am a local model", "reasoning channel must surface when content is empty"


def test_lmstudio_content_wins_over_reasoning():

    cap: dict = {}
    be = LMStudioBackend(Config(), transport=_lmstudio_stream(
        [{"delta": {"reasoning": "secret thoughts"}, "finish_reason": None},
         {"delta": {"content": "real answer"}, "finish_reason": None},
         {"delta": {}, "finish_reason": "stop"}], cap))
    out = asyncio.run(_collect(be, "gpt-oss"))
    assert out == "real answer", "content must not be polluted with reasoning when present"


async def _collect(be, model: str) -> str:
    parts = []
    async for ch in be.stream_chat(model, [{"role": "user", "content": "x"}], Config().generation):
        parts.append(ch.content)
    return "".join(parts)


def test_ollama_thinking_only_fallback_and_keepalive():

    lines = [
        json.dumps({"message": {"thinking": "hmm "}, "done": False}),
        json.dumps({"message": {"thinking": "let me see"}, "done": False}),
        json.dumps({"message": {}, "done": True, "done_reason": "stop", "eval_count": 5}),
    ]
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="\n".join(lines))

    be = OllamaBackend(Config(), transport=httpx.MockTransport(handler))
    out = asyncio.run(_collect(be, "qwen3:8b"))
    assert out == "hmm let me see"
    assert cap["body"]["keep_alive"] == "30m"


def test_lmstudio_surfaces_sse_error_events():

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='event: error\ndata: {"error":{"message":"Model is unloaded."}}\n\n',
        )

    be = LMStudioBackend(Config(), transport=httpx.MockTransport(handler))
    try:
        asyncio.run(_collect(be, "m"))
        raise AssertionError("BackendError expected")
    except Exception as exc:
        assert "Model is unloaded" in str(exc), f"real error must reach the user, got: {exc}"


def test_thinking_flags_reach_the_wire():

    cap: dict = {}
    cfg = Config()
    cfg.generation.effort = "medium"
    be = LMStudioBackend(cfg, transport=_lmstudio_stream(
        [{"delta": {"content": "x"}, "finish_reason": "stop"}], cap))

    async def _run(backend, model):
        parts = []
        async for ch in backend.stream_chat(model, [{"role": "user", "content": "x"}], cfg.generation):
            parts.append(ch.content)
        return "".join(parts)

    asyncio.run(_run(be, "m"))
    assert cap["body"]["reasoning_effort"] == "medium"

    lines = [json.dumps({"message": {"content": "y"}, "done": True, "eval_count": 2,
                         "eval_duration": 1_000_000_000})]

    def handler(request: httpx.Request) -> httpx.Response:
        cap["b2"] = json.loads(request.content.decode())
        return httpx.Response(200, text="\n".join(lines))

    be2 = OllamaBackend(cfg, transport=httpx.MockTransport(handler))
    asyncio.run(_run(be2, "qwen3"))
    assert cap["b2"]["thinking"] == "medium"  # ollama accepts graded think levels


def test_ultrathink_escalates_effort_and_budget():

    cap: dict = {}
    cfg = Config()
    cfg.generation.ultrathink = True  # note: thinking stays False on purpose
    be = LMStudioBackend(cfg, transport=_lmstudio_stream(
        [{"delta": {"content": "x"}, "finish_reason": "stop"}], cap))

    async def _run(backend, model):
        async for _ in backend.stream_chat(model, [{"role": "user", "content": "x"}], cfg.generation):
            pass

    asyncio.run(_run(be, "m"))
    assert cap["body"]["reasoning_effort"] == "xhigh", "ultrathink must ride the ceiling"
    assert cap["body"]["reasoning"] == {"enabled": True, "effort": "xhigh"}
    assert cap["body"]["max_tokens"] >= 16384, "ultrathink must raise the output budget"

    lines = [json.dumps({"message": {"content": "y"}, "done": True, "eval_count": 2,
                         "eval_duration": 1_000_000_000})]

    def handler(request: httpx.Request) -> httpx.Response:
        cap["b2"] = json.loads(request.content.decode())
        return httpx.Response(200, text="\n".join(lines))

    be2 = OllamaBackend(cfg, transport=httpx.MockTransport(handler))
    asyncio.run(_run(be2, "qwen3"))
    assert cap["b2"]["thinking"] is True, "ultrathink implies the thinking channel"
    assert cap["b2"]["options"]["num_predict"] >= 16384


def test_lmstudio_streams_tool_args_progress():

    frags = ['{"path": "a.txt"', ', "content": "' + ("z" * 320) + "hello world", '"}']
    def handler(request: httpx.Request) -> httpx.Response:
        lines = []
        for i, f in enumerate(frags):
            delta = {"tool_calls": [{"index": 0, "function": {"name": "write_file" if i == 0 else "", "arguments": f}}]}
            lines.append("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n")
        lines.append('data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}\n')
        lines.append("data: [DONE]\n")
        return httpx.Response(200, text="\n".join(lines),
                             headers={"content-type": "text/event-stream"})

    be = LMStudioBackend(Config(), transport=httpx.MockTransport(handler))

    async def _run():
        progress = []
        async for ch in be.stream_chat("m", [{"role": "user", "content": "x"}], Config().generation,
                                       tools=[{"function": {"name": "write_file"}}], native_tools=True):
            if ch.tool_progress:
                progress.append(ch.tool_progress)
        return progress

    prog = asyncio.run(_run())
    assert prog and prog[0]["name"] == "write_file"
    assert "hello world" in prog[-1]["tail"], prog[-1]["tail"][-40:]


def test_lmstudio_ejects_previous_model_on_switch():
    """Switching models must free VRAM: the model OhMyLLM loaded before gets
    ejected when a different one is selected."""

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v0/models":
            return httpx.Response(200, json={"data": [
                {"id": "a", "key": "a", "state": "loaded"},
                {"id": "b", "key": "b", "state": "loaded"},
            ]})
        if path in ("/api/v1/models/eject", "/api/v1/models/load"):
            calls.append((path.rsplit("/", 1)[-1], json.loads(request.content.decode())["model"]))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    be = LMStudioBackend(Config(), transport=httpx.MockTransport(handler))
    asyncio.run(be.ensure_loaded("a"))
    asyncio.run(be.ensure_loaded("b"))
    assert ("eject", "a") in calls, f"previous model was never ejected: {calls}"
    assert ("eject", "b") not in calls, "the active model must not be ejected"


def test_lmstudio_loads_with_gpu_offload_flags(monkeypatch):
    """REST load can't control GPU offload — a 9B must not silently run on CPU.
    ensure_loaded must prefer `lms load --gpu max --context-length N`."""
    import subprocess

    seen: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        seen.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    be = LMStudioBackend(Config(), transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    monkeypatch.setattr(be, "lms_cli", lambda: "/usr/bin/lms")
    states = iter(["not-loaded", "loaded", "loaded", "loaded"])

    async def fake_state(model: str):
        return "", next(states)

    monkeypatch.setattr(be, "_v0_state", fake_state)
    asyncio.run(be.ensure_loaded("big9b"))
    assert seen, "lms CLI was never invoked"
    args = seen[0]
    assert "--gpu" in args and "max" in args, f"missing full GPU offload: {args}"
    assert "--context-length" in args, f"missing context override: {args}"


def test_ollama_unloads_previous_model_on_switch():
    """Ollama's keep_alive holds the old model on the GPU; switching must
    send keep_alive=0 for the previous model."""

    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append((request.url.path, body.get("model"), body.get("keep_alive")))
        if request.url.path == "/api/chat":
            line = json.dumps({"message": {}, "done": True, "done_reason": "stop", "eval_count": 1})
            return httpx.Response(200, text=line + "\n")
        return httpx.Response(200, json={"status": "ok"})

    be = OllamaBackend(Config(), transport=httpx.MockTransport(handler))
    asyncio.run(_collect(be, "a"))
    asyncio.run(_collect(be, "b"))
    assert ("/api/generate", "a", 0) in seen, f"previous model never unloaded: {seen}"
    assert not any(p == "/api/generate" and m == "b" for p, m, _ in seen), \
        "the active model must stay loaded"
