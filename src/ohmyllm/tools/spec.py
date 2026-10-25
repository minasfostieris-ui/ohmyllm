"""Tool specs + execution context shared by all built-in tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.models import Config
from ..memory import SoulManager

JSON_TYPES = {"string", "integer", "number", "boolean", "object", "array"}


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str  # JSON-schema type
    description: str
    required: bool = True
    enum: tuple[str, ...] | None = None
    default: Any = None


@dataclass
class ToolContext:
    """Everything a tool handler may legitimately need."""

    workspace: Path
    mode: str
    config: Config
    soul: SoulManager | None = None
    skills: Any = None  # SkillManager | None (Any avoids the import cycle)
    status_provider: Callable[[], dict[str, Any]] | None = None
    user_requested_memory: bool = False


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    error: str | None = None
    meta: dict = field(default_factory=dict)  # {"path","created","added","removed","changed"}

    def as_message(self) -> str:
        return self.output if self.ok else f"ERROR: {self.error or self.output}"


@dataclass
class ToolSpec:
    name: str
    description: str
    params: list[ParamSpec]
    handler: Callable[[ToolContext, dict[str, Any]], Any]  # sync or async -> str|ToolResult
    preview: Callable[[dict[str, Any]], str] | None = None
    examples: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def json_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for p in self.params:
            entry: dict[str, Any] = {"type": p.type if p.type in JSON_TYPES else "string", "description": p.description}
            if p.enum:
                entry["enum"] = list(p.enum)
            props[p.name] = entry
        required = [p.name for p in self.params if p.required]
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema


def arg(name: str, ptype: str, desc: str, **kw: Any) -> ParamSpec:
    return ParamSpec(name=name, type=ptype, description=desc, **kw)
