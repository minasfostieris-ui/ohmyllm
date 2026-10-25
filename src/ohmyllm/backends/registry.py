"""Backend registry: construction + environment probing."""

from __future__ import annotations

from typing import Any

import httpx

from ..config.models import BackendName, Config
from .base import BaseBackend
from .lmstudio import LMStudioBackend
from .mock import MockBackend
from .ollama import OllamaBackend

_CLASSES: dict[BackendName, type[BaseBackend]] = {
    BackendName.OLLAMA: OllamaBackend,
    BackendName.LMSTUDIO: LMStudioBackend,
    BackendName.MOCK: MockBackend,
}


def make_backend(name: BackendName | str, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> BaseBackend:
    key = BackendName(name)
    return _CLASSES[key](config, transport)


async def probe(config: Config, transport: httpx.AsyncBaseTransport | None = None) -> dict[BackendName, dict[str, Any]]:
    """Installed/running status for every backend (cheap: version ping only)."""
    out: dict[BackendName, dict[str, Any]] = {}
    for name in _CLASSES:
        be = make_backend(name, config, transport)
        installed = be.is_installed()
        running, health = await be.health()
        out[name] = {"installed": installed, "running": running, "health": health, "class": be}
    return out


__all__ = ["BaseBackend", "LMStudioBackend", "MockBackend", "OllamaBackend", "make_backend", "probe"]
