"""Cloud provider registry: where API keys live and how to reach each service.

Providers are OpenAI-compatible chat endpoints by default; the handful that
use their own wire format (Anthropic) declare ``kind`` and the cloud backend
translates. Keys are stored in config.toml under [cloud] (never sent back to
the UI unmasked) — or left in the environment: OMLLM_KEY_<PROVIDER_ID> also
works and wins over the file, so CI/secrets managers can inject them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    pid: str
    name: str
    blurb: str
    base_url: str = ""              # OpenAI-compatible root (…/v1)
    kind: str = "openai"             # openai | anthropic
    popular: bool = False
    signup_url: str = ""
    env_var: str = ""                # extra well-known env var checked
    recommended: bool = False


PROVIDERS: list[Provider] = [
    Provider("openrouter", "OpenRouter", "one key, hundreds of models — free tier to test",
             base_url="https://openrouter.ai/api/v1", popular=True,
             signup_url="https://openrouter.ai/keys", env_var="OPENROUTER_API_KEY",
             recommended=True),
    Provider("anthropic", "Anthropic", "Claude — strong reasoning + tool use",
             base_url="https://api.anthropic.com/v1", kind="anthropic", popular=True,
             signup_url="https://console.anthropic.com/settings/keys", env_var="ANTHROPIC_API_KEY"),
    Provider("openai", "OpenAI", "GPT models incl. the open-weight gpt-oss family",
             base_url="https://api.openai.com/v1", popular=True,
             signup_url="https://platform.openai.com/api-keys", env_var="OPENAI_API_KEY"),
    Provider("google", "Google Gemini", "Flash/Pro via the OpenAI-compatible endpoint",
             base_url="https://generativelanguage.googleapis.com/v1beta/openai", popular=True,
             signup_url="https://aistudio.google.com/apikey", env_var="GEMINI_API_KEY"),
    Provider("deepseek", "DeepSeek", "cheap frontier-class reasoning + chat models",
             base_url="https://api.deepseek.com/v1", popular=True,
             signup_url="https://platform.deepseek.com/api_keys", env_var="DEEPSEEK_API_KEY"),
    Provider("opencode", "OpenCode Zen", "curated, reliability-tested model routes",
             base_url="https://opencode.ai/zen/v1", popular=True,
             signup_url="https://opencode.ai/auth", recommended=True),
    Provider("groq", "Groq", "Llama-family inference at very high tokens/s",
             base_url="https://api.groq.com/openai/v1",
             signup_url="https://console.groq.com/keys", env_var="GROQ_API_KEY"),
    Provider("mistral", "Mistral", "Mistral + Magistral, strong multilingual",
             base_url="https://api.mistral.ai/v1",
             signup_url="https://console.mistral.ai/api-keys", env_var="MISTRAL_API_KEY"),
    Provider("xai", "xAI", "Grok models", base_url="https://api.x.ai/v1",
             signup_url="https://console.x.ai", env_var="XAI_API_KEY"),
    Provider("together", "Together AI", "open-weight models, huge catalog",
             base_url="https://api.together.xyz/v1",
             signup_url="https://api.together.xyz/settings/api-keys", env_var="TOGETHER_API_KEY"),
    Provider("fireworks", "Fireworks", "fast open-weight serving",
             base_url="https://api.fireworks.ai/inference/v1",
             signup_url="https://app.fireworks.ai/settings/users/api-keys", env_var="FIREWORKS_API_KEY"),
    Provider("cerebras", "Cerebras", "Llama at >1000 tok/s",
             base_url="https://api.cerebras.ai/v1",
             signup_url="https://cloud.cerebras.ai", env_var="CEREBRAS_API_KEY"),
    Provider("vercel", "Vercel AI Gateway", "one gateway, many vendors",
             base_url="https://ai-gateway.vercel.sh/v1",
             signup_url="https://vercel.com/dashboard", env_var="AI_GATEWAY_API_KEY"),
    Provider("302ai", "302.AI", "billed-per-use access to many vendors",
             base_url="https://api.302.ai/v1",
             signup_url="https://dash.302.ai"),
]

_BY_ID = {p.pid: p for p in PROVIDERS}


def get(pid: str) -> Provider | None:
    return _BY_ID.get(pid)


def is_custom(pid: str) -> bool:
    return pid.startswith("custom-")


def custom_name(pid: str) -> str:
    return re.sub(r"^custom-", "", pid).replace("-", " ").title() or "Custom provider"


def resolve(pid: str, bases: dict[str, str], customs: dict[str, str]) -> tuple[str, str, str] | None:
    """(base_url, kind, display_name) for a provider id incl. customs."""
    p = _BY_ID.get(pid)
    if p is not None:
        return bases.get(pid) or p.base_url, p.kind, p.name
    if is_custom(pid):
        base = bases.get(pid, "")
        return base, "openai", customs.get(pid) or custom_name(pid)
    return None


def key_for(pid: str, keys: dict[str, str]) -> str:
    """Env (OMLLM_KEY_<ID> or the provider's own var) wins over the config file."""
    env = "OMLLM_KEY_" + pid.upper().replace("-", "_")
    v = os.environ.get(env, "")
    if v:
        return v
    p = _BY_ID.get(pid)
    if p and p.env_var:
        v = os.environ.get(p.env_var, "")
        if v:
            return v
    return keys.get(pid, "")


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"
