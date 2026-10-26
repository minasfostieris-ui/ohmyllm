"""Cloud providers: registry, key handling, both wire formats, UI surfaces."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ohmyllm import providers
from ohmyllm.backends.registry import make_backend
from ohmyllm.config import Config


# ---------------------------------------------------------------- registry
def test_registry_shape():
    assert {"openrouter", "anthropic", "openai", "google", "deepseek", "groq"} <= {p.pid for p in providers.PROVIDERS}
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
