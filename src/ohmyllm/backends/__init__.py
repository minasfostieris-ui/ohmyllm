"""Backend layer (Ollama, LM Studio, Mock)."""

from .base import (
    BackendError,
    BaseBackend,
    ChatChunk,
    ModelInfo,
    ProgressFn,
    StartResult,
    tool_schema,
)
from .lmstudio import LMStudioBackend
from .mock import MockBackend
from .ollama import OllamaBackend
from .registry import make_backend, probe

__all__ = [
    "BackendError",
    "BaseBackend",
    "ChatChunk",
    "LMStudioBackend",
    "MockBackend",
    "ModelInfo",
    "OllamaBackend",
    "ProgressFn",
    "StartResult",
    "make_backend",
    "probe",
    "tool_schema",
]
