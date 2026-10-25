"""Backend abstraction: Ollama / LM Studio / Mock all implement this."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config.models import BackendName, Config, GenerationSettings
from ..vram.estimator import VramEstimate, estimate_vram, model_size_and_quant_from_name

#: Model name fragments that are known to handle OpenAI-style tool calling well.
TOOL_CAPABLE_HINTS = (
    "qwen2.5", "qwen3", "qwen2", "llama-3.1", "llama3.1", "llama-3.2", "llama3.2",
    "llama-3.3", "llama3.3", "mistral-nemo", "ministral-3", "mistral-small-3",
    "hermes-3", "hermes4", "command-r", "firefunction", "granite3", "phi4",
    "nemotron", "magistral", "devstral",
)
TOO_SMALL_PARAMS_B = 2.5


class BackendError(RuntimeError):
    pass


@dataclass
class ModelInfo:
    name: str
    backend: BackendName
    size_bytes: int | None = None
    quant: str | None = None
    params_b: float | None = None
    ctx_len: int | None = None
    layers: int | None = None
    path: str | None = None
    modified: float | None = None
    tool_capable: bool | None = None  # None = unknown / needs test
    loaded: bool | None = None
    notes: list[str] = field(default_factory=list)

    def apply_name_heuristics(self) -> ModelInfo:
        if self.quant is None or self.params_b is None:
            q, p = model_size_and_quant_from_name(self.name)
            self.quant = self.quant or q
            self.params_b = self.params_b or p
        if self.tool_capable is None:
            low = self.name.lower()
            if any(h in low for h in TOOL_CAPABLE_HINTS):
                self.tool_capable = True
            elif self.params_b is not None and self.params_b < TOO_SMALL_PARAMS_B:
                self.tool_capable = False
        return self

    def why_tool_weak(self) -> str:
        if self.tool_capable:
            return ""
        low = self.name.lower()
        if self.params_b is not None and self.params_b < TOO_SMALL_PARAMS_B:
            return (
                f"~{self.params_b:g}B params is below the reliability floor for tool calling; "
                "use ReAct text mode or a larger model"
            )
        if any(h in low for h in TOOL_CAPABLE_HINTS):
            return "known tool-capable family (enable native tools after a test ping)"
        return (
            "no evidence of trained function-calling support; OhMyLLM will use the fenced-JSON "
            "/ ReAct protocol, which needs a model that follows format reliably (generally >=7B)"
        )

    def vram_estimate(self, gen: GenerationSettings, gpu_total: int | None = None, gpu_free: int | None = None) -> VramEstimate | None:
        if not self.size_bytes:
            return None
        return estimate_vram(
            self.size_bytes,
            num_ctx=gen.num_ctx,
            quant=self.quant,
            params_b=self.params_b,
            total_layers=self.layers,
            num_gpu_layers=gen.num_gpu,
            batch=gen.num_batch,
            gpu_total_bytes=gpu_total,
            gpu_free_bytes=gpu_free,
        )


@dataclass
class ChatChunk:
    """One streamed unit from the backend."""

    content: str = ""
    reasoning: str = ""  # thinking/reasoning channel (not part of the answer)
    tool_progress: dict[str, Any] | None = None  # {name, chars, tail} — args streaming in live
    tool_calls: list[dict[str, Any]] | None = None
    done: bool = False
    finish: str | None = None
    usage: dict[str, Any] | None = None  # eval_count / prompt_eval_count / total_tokens


@dataclass
class StartResult:
    ok: bool
    was_already_running: bool = False
    started_process: bool = False
    message: str = ""
    instructions: list[str] = field(default_factory=list)


ProgressFn = Callable[[str, float | None], None]


class BaseBackend(ABC):
    name: BackendName
    display_name: str = "backend"

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    # ------------------------------------------------------------- presence
    @abstractmethod
    def is_installed(self) -> bool: ...

    @abstractmethod
    async def is_running(self) -> bool: ...

    @abstractmethod
    async def health(self) -> tuple[bool, dict[str, Any]]: ...

    @abstractmethod
    async def ensure_running(self) -> StartResult: ...

    # --------------------------------------------------------------- models
    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    async def get_model(self, name: str) -> ModelInfo | None:
        for m in await self.list_models():
            if m.name == name:
                return m
        return None

    def supports_native_tools(self, model: ModelInfo | None = None) -> bool:
        return bool(model and model.tool_capable)

    # ---------------------------------------------------------------- chat
    @abstractmethod
    def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: GenerationSettings,
        *,
        tools: list[dict[str, Any]] | None = None,
        native_tools: bool = False,
    ) -> AsyncIterator[ChatChunk]: ...

    async def generate(self, model: str, messages: list[dict[str, Any]], params: GenerationSettings) -> tuple[str, float]:
        """Non-streaming convenience; returns (text, duration_s)."""
        start = time.perf_counter()
        parts: list[str] = []
        async for chunk in self.stream_chat(model, messages, params):
            parts.append(chunk.content)
        return "".join(parts), time.perf_counter() - start

    # ---------------------------------------------------------------- pull
    async def pull(self, name: str, progress: ProgressFn | None = None) -> None:
        raise BackendError(f"{self.display_name} does not support automated pulls")

    # ------------------------------------------------------------- helpers
    def _client(self, base_url: str, timeout: float | httpx.Timeout = 30.0, **kw: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=self._transport,
            **kw,
        )

    def _gen(self, model: str) -> GenerationSettings:
        return self.config.settings_for(self.name, model)


def tool_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
