"""Cloud providers: registry, key handling, both wire formats, UI surfaces."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ohmyllm import providers
from ohmyllm.backends.registry import make_backend
from ohmyllm.config import BackendName, Config


# ---------------------------------------------------------------- registry
def test_registry_shape():
    assert {"openrouter", "anthropic", "openai", "google", "deepseek", "groq"} <= {p.pid for p in providers.PROVIDERS}
    assert {"alibaba-token-plan", "alibaba-coding-plan", "alibaba", "moonshot", "zai",
            "nvidia", "github", "huggingface", "perplexity", "cohere", "sambanova",
            "lambda", "nebius", "minimax"} <= {p.pid for p in providers.PROVIDERS}
    tp = providers.get("alibaba-token-plan")
    assert tp and tp.base_url == "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    payg = providers.get("alibaba")
    assert payg and payg.env_var == "DASHSCOPE_API_KEY"
    anth = providers.get("anthropic")
    assert anth and anth.kind == "anthropic"
    orr = providers.get("openrouter")
    assert orr and orr.popular and orr.base_url.endswith("/v1")
    assert providers.get("nope") is None


def test_resolve_custom():
    base, kind, name = providers.resolve("custom-homevllm", {"custom-homevllm": "http://192.168.1.5:8000/v1"}, {})
    assert base == "http://192.168.1.5:8000/v1" and kind == "openai"
    _, _, name = providers.resolve("custom-homevllm", {}, {"custom-homevllm": "Home vLLM"})
    assert name == "Home vLLM"


def test_key_env_precedence(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert providers.key_for("openai", {"openai": "sk-file"}) == "sk-env"
    monkeypatch.setenv("OMLLM_KEY_OPENAI", "sk-omllm")
    assert providers.key_for("openai", {"openai": "sk-file"}) == "sk-omllm"
    assert providers.key_for("groq", {"groq": "g-file"}) == "g-file"
    assert providers.key_for("groq", {}) == ""


def test_mask_never_leaks():
    m = providers.mask("sk-proj-supersecretvalue1234")
    assert "supersecret" not in m and m.startswith("sk-p") and m.endswith("1234")
    assert providers.mask("") == ""


# ------------------------------------------------------------ cloud backend
def _cfg(pid, key):
    cfg = Config()
    cfg.cloud.provider = pid
    cfg.cloud.keys[pid] = key
    return cfg


def _sse(events):
    text = "".join("data: " + json.dumps(o) + "\n\n" for o in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())


def test_cloud_openai_stream_tools_usage():
    def handler(req):
        assert req.headers["authorization"] == "Bearer sk-or-x"
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "a/b"}]})
        return _sse([
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": '{"p'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '": 1}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 9}},
        ])

    cfg = _cfg("openrouter", "sk-or-x")
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))

    async def go():
        out = []
        async for ch in be.stream_chat("a/b", [{"role": "user", "content": "?"}], cfg.generation,
                                       tools=[{"type": "function", "function": {"name": "read"}}], native_tools=True):
            out.append(ch)
        return out

    chunks = asyncio.run(go())
    assert "".join(c.content for c in chunks) == "Hi"
    last = chunks[-1]
    assert last.tool_calls[0]["function"] == {"name": "read", "arguments": '{"p": 1}'}
    assert last.usage == {"total_tokens": 9}


def test_cloud_anthropic_translation():
    seen = {}

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "claude-x"}]})
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Yo"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        ]
        text = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())

    cfg = _cfg("anthropic", "sk-ant-y")
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "res", "name": "t"}]

    async def go():
        return [c async for c in be.stream_chat("claude-x", msgs, cfg.generation,
                                                tools=[{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}], native_tools=True)]

    chunks = asyncio.run(go())
    assert "".join(c.content for c in chunks) == "Yo"
    assert seen["headers"]["x-api-key"] == "sk-ant-y"
    body = seen["body"]
    assert body["system"] == "sys"
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    # assistant tool_calls → tool_use block; tool msg → tool_result block
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"


def test_no_provider_selected_is_honest():
    cfg = Config()
    assert cfg.cloud.provider == ""
    be = make_backend("cloud", cfg)

    async def go():
        return await be.health()

    ok, info = asyncio.run(go())
    assert not ok and "no provider connected" in info["error"]


def test_custom_static_model_list_no_http():
    def boom(req):
        raise AssertionError("must not hit the network when a static list exists")

    cfg = Config()
    cfg.cloud.provider = "custom-home"
    cfg.cloud.keys["custom-home"] = "k"
    cfg.cloud.customs = {"custom-home": "Home vLLM"}
    cfg.cloud.bases = {"custom-home": "http://192.168.1.5:8000/v1"}
    cfg.cloud.model_lists = {"custom-home": "qwen3-coder,llama-3.3-70b"}
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(boom))
    models = asyncio.run(be.list_models())
    assert [m.name for m in models] == ["qwen3-coder", "llama-3.3-70b"]
    ok, info = asyncio.run(be.health())
    assert ok and info["models"] == 2


def test_cloud_requires_key():
    cfg = Config()
    cfg.cloud.provider = "openai"
    be = make_backend("cloud", cfg)
    assert be.is_installed() is False

    async def go():
        return await be.list_models()

    with pytest.raises(Exception) as e:
        asyncio.run(go())
    assert "key" in str(e.value).lower()


# ------------------------------------------------------------------- config
def test_cloud_keys_roundtrip_and_perms(svc):
    from ohmyllm.config import ConfigStore

    svc.config.cloud.keys["groq"] = "gsk-test"
    svc.config.cloud.provider = "groq"
    svc.save_config()
    again = ConfigStore().load()
    assert again.cloud.keys["groq"] == "gsk-test" and again.cloud.provider == "groq"
    import os

    if os.name == "posix":  # NTFS has no POSIX modes — chmod is a no-op there
        mode = oct(os.stat(ConfigStore().path).st_mode & 0o777)
        assert mode == "0o600"


# ---------------------------------------------------------------------- TUI
async def test_providers_screen_connect_use(oml_env, svc, monkeypatch):
    from ohmyllm.tui.app import OhMyLLMApp
    from ohmyllm.tui.screens.providers import ProvidersScreen

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = OhMyLLMApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.7)
        inp = app.screen.query_one("#chat-input")
        inp.value = "/providers"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert isinstance(app.screen, ProvidersScreen)
        from textual.widgets import OptionList

        lst = app.screen.query_one(OptionList)
        prompts = " ".join(str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count))
        assert "OpenRouter" in prompts and "Anthropic" in prompts and "Popular" in prompts
        # select openrouter and store a key directly (modal covered by bridge tests)
        app.screen._store("openrouter", "sk-tui-test1234")
        await pilot.pause(0.6)
        assert app.svc.config.cloud.keys["openrouter"] == "sk-tui-test1234"
        # use it
        for i in range(lst.option_count):
            if str(lst.get_option_at_index(i).id) == "p-openrouter":
                lst.highlighted = i
                break
        app.screen.action_use()
        await pilot.pause(0.4)
        assert app.svc.config.cloud.provider == "openrouter"
        assert app.svc.config.default_backend.value == "cloud"


def test_cloud_ctx_from_provider_metadata():
    def handler(req):
        return httpx.Response(200, json={"data": [
            {"id": "deepseek/deepseek-chat", "context_length": 163840},
            {"id": "openai/gpt-4o", "context_length": 128000},
            {"id": "no-meta-model"},
        ]})

    cfg = _cfg("openrouter", "sk-or-x")
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))
    models = asyncio.run(be.list_models())
    by = {m.name: m for m in models}
    assert by["deepseek/deepseek-chat"].ctx_len == 163840
    assert by["openai/gpt-4o"].ctx_len == 128000
    assert by["no-meta-model"].ctx_len is None
    # the cache the ctx% bar + compaction read is populated
    assert be._ctx_map()["openai/gpt-4o"] == 128000


def test_cloud_ctx_name_heuristics():
    def handler(req):
        return httpx.Response(200, json={"data": [
            {"id": "vendor/llama-3.1-8b-128k"},
            {"id": "vendor/qwen-long-1m"},
            {"id": "vendor/phi-4-mini"},
        ]})

    cfg = _cfg("openrouter", "sk-or-x")
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(handler))
    by = {m.name: m for m in asyncio.run(be.list_models())}
    assert by["vendor/llama-3.1-8b-128k"].ctx_len == 131072
    assert by["vendor/qwen-long-1m"].ctx_len == 1048576
    assert by["vendor/phi-4-mini"].ctx_len is None  # "mini" is not a window


def test_status_snapshot_uses_cloud_window(oml_env, svc):
    svc.config.default_backend = BackendName.CLOUD
    svc.config.cloud.provider = "openrouter"
    svc.config.cloud.keys["openrouter"] = "sk-or-x"
    svc.config.cloud.model_lists["openrouter"] = "vendor/big-model-256k"
    svc.config.generation.num_ctx = 4096
    be = svc.backend("cloud")  # constructs + caches; static list path
    asyncio.run(be.list_models())
    svc.config.cloud.default_model = "vendor/big-model-256k"
    snap = svc.status_snapshot(ctx_used=1000)
    assert snap["num_ctx"] == 262144
    assert snap["ctx_pct"] == 0  # 1000 / 256k — not the old 4096 panic


def test_runtime_ctx_budget_prefers_cloud_window(oml_env, svc):
    from ohmyllm.agents import AgentRuntime

    svc.config.default_backend = BackendName.CLOUD
    be = svc.backend("cloud")
    be._ctx_map()["vendor/x"] = 131072
    rt = AgentRuntime(backend=be, registry=svc.registry, policy=svc.policy,
                      approvals=svc.approvals, audit=svc.audit, config=svc.config,
                      soul=svc.soul)
    assert rt._ctx_budget("vendor/x", svc.config.generation) == 131072
    assert rt._ctx_budget("unknown", svc.config.generation) == max(2048, svc.config.generation.num_ctx)


def test_static_list_ctx_suffix():
    cfg = _cfg("alibaba-token-plan", "sk-x")
    cfg.cloud.model_lists["alibaba-token-plan"] = "qwen3.8-flash:256k, glm-5.2:1m, plain-model"
    be = make_backend("cloud", cfg, transport=httpx.MockTransport(
        lambda r: httpx.Response(404)))
    by = {m.name: m for m in asyncio.run(be.list_models())}
    assert by["qwen3.8-flash"].ctx_len == 262144
    assert by["glm-5.2"].ctx_len == 1048576
    assert by["plain-model"].ctx_len is None
    assert be._ctx_map()["qwen3.8-flash"] == 262144
