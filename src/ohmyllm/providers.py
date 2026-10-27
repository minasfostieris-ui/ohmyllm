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
    Provider("alibaba-token-plan", "Alibaba Token Plan", "Qwen3 coding models — fixed-price subscription key",
             base_url="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
             popular=True, signup_url="https://modelstudio.console.alibabacloud.com",
             env_var="ALIBABA_TOKEN_PLAN_API_KEY"),
    Provider("alibaba-coding-plan", "Alibaba Coding Plan", "coding-tool plan (sk-sp-… keys)",
             base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
             popular=True, signup_url="https://modelstudio.console.alibabacloud.com/cn-beijing/subscription/coding-plan",
             env_var="ALIBABA_CODING_PLAN_API_KEY"),
    Provider("alibaba", "Alibaba PAYG (DashScope)", "pay-as-you-go Qwen3 family incl. vision + audio",
             base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", popular=True,
             signup_url="https://modelstudio.console.alibabacloud.com", env_var="DASHSCOPE_API_KEY"),
    Provider("moonshot", "Moonshot Kimi", "long-context Kimi models",
             base_url="https://api.moonshot.ai/v1", popular=True,
             signup_url="https://platform.moonshot.ai/console/api-keys", env_var="MOONSHOT_API_KEY"),
    Provider("zai", "Z.ai (GLM)", "GLM-4.6 family — agentic + coding",
             base_url="https://api.z.ai/api/paas/v4", popular=True,
             signup_url="https://z.ai/manage-apikey/apikey-list", env_var="ZAI_API_KEY"),
    Provider("nvidia", "NVIDIA NIM", "open models on NVIDIA hardware, generous free tier",
             base_url="https://integrate.api.nvidia.com/v1",
             signup_url="https://build.nvidia.com", env_var="NVIDIA_API_KEY"),
    Provider("github", "GitHub Models", "frontier models with your GitHub token",
             base_url="https://models.github.ai/inference/v1",
             signup_url="https://github.com/settings/tokens", env_var="GITHUB_TOKEN"),
    Provider("huggingface", "Hugging Face", "router over open + partner models",
             base_url="https://router.huggingface.co/v1",
             signup_url="https://huggingface.co/settings/tokens", env_var="HF_TOKEN"),
    Provider("perplexity", "Perplexity", "Sonar search-grounded answers",
             base_url="https://api.perplexity.ai",
             signup_url="https://www.perplexity.ai/settings/api", env_var="PERPLEXITY_API_KEY"),
    Provider("cohere", "Cohere", "Command family — RAG + tool use",
             base_url="https://api.cohere.ai/compatibility/v1",
             signup_url="https://dashboard.cohere.com/api-keys", env_var="COHERE_API_KEY"),
    Provider("sambanova", "SambaNova", "very fast open-model inference",
             base_url="https://api.sambanova.ai/v1",
             signup_url="https://cloud.sambanova.ai/apitoken", env_var="SAMBANOVA_API_KEY"),
    Provider("lambda", "Lambda AI", "GPU-cloud inference API",
             base_url="https://api.lambda.ai/v1",
             signup_url="https://cloud.lambdalabs.com/api-keys", env_var="LAMBDA_API_KEY"),
    Provider("nebius", "Nebius Token Factory", "open models at high speed",
             base_url="https://api.tokenfactory.nebius.com/v1",
             signup_url="https://nebius.ai", env_var="NEBIUS_API_KEY"),
    Provider("minimax", "MiniMax", "M-series long-context coding models",
             base_url="https://api.minimax.io/v1",
             signup_url="https://www.minimax.io/platform/interface-management", env_var="MINIMAX_API_KEY"),
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


def any_key(cl) -> bool:
    """True when at least one provider (registry or custom) has a usable key."""
    pids = {p.pid for p in PROVIDERS} | set(cl.keys) | set(cl.customs)
    return any(key_for(pid, cl.keys) for pid in pids)


def best_local_backend(cfg):
    """Sane backend to fall back to when cloud is unusable: whichever local
    server the user actually configured, else ollama."""
    from .config.models import BackendName

    lm = cfg.lmstudio
    if lm is not None and (lm.default_model or lm.base_url):
        return BackendName.LMSTUDIO
    return BackendName.OLLAMA


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:  # first4+last4 on a 9-char key would reveal 8 of them
        return "•" * min(len(key), 8)
    return f"{key[:4]}…{key[-4:]}"
