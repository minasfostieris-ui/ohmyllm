"""ToolCallValidator: schema/type checks before anything can execute."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools.registry import ToolRegistry
from ..tools.spec import ParamSpec
from .parser import ParsedCall


@dataclass
class ValidationResult:
    ok: bool
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def error_text(self) -> str:
        return "; ".join(self.errors) or "invalid tool call"


def _coerce(value: Any, spec: ParamSpec) -> tuple[Any, str | None]:
    if value is None:
        return (spec.default, None) if not spec.required else (None, f"missing required parameter '{spec.name}'")
    want = spec.type
    if want == "string":
        return (value if isinstance(value, str) else str(value), None)
    if want in ("integer", "number"):
        if isinstance(value, bool):
            return None, f"parameter '{spec.name}' must be {want}, got boolean"
        try:
            num = int(value) if want == "integer" else float(value)
            if want == "integer" and isinstance(value, float) and not value.is_integer():
                num = int(round(num))  # tolerate 4096.0 style
            return num, None
        except (TypeError, ValueError):
            return None, f"parameter '{spec.name}' must be {want}, got {value!r}"
    if want == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.lower() in ("true", "1", "yes"), None
        return bool(value), None
    if want == "object":
        if isinstance(value, dict):
            return value, None
        if isinstance(value, str):
            from .parser import repair_json

            obj, err = repair_json(value)
            return (obj, None) if isinstance(obj, dict) else (None, f"parameter '{spec.name}' must be an object")
        return None, f"parameter '{spec.name}' must be an object"
    if want == "array":
        if isinstance(value, list):
            return value, None
        return [value], None
    return value, None


# Small models constantly rename parameters (file_path vs path, cmd vs command).
# Fold those aliases onto the real spec names instead of burning retry loops.
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "filename", "file_name", "file", "target",
             "target_path", "destination", "dest", "dir", "directory", "folder"),
    "content": ("text", "body", "data", "file_content", "contents", "code", "payload"),
    "command": ("cmd", "shell", "bash", "script", "exec", "shell_command"),
    "query": ("search", "search_term", "q", "term", "pattern", "search_query"),
    "url": ("uri", "link", "href", "address"),
    "name": ("skill", "tool_name", "id"),
    "section": ("heading", "category", "group"),
    "text": ("content", "body", "entry", "value", "note"),
    "old_text": ("old", "old_string", "old_str", "find", "from", "search"),
    "new_text": ("new", "new_string", "new_str", "replace", "replacement", "to"),
    "offset": ("start", "start_line", "line", "from_line"),
    "timeout": ("timeout_s", "timeout_seconds", "secs", "seconds"),
    "max_lines": ("limit", "lines", "n_lines"),
    "max_results": ("limit", "count", "n"),
}


def _fold_aliases(provided: dict[str, object], names: set[str]) -> dict[str, object]:
    out = dict(provided)
    for key in list(out):
        if key in names:
            continue
        kl = str(key).lower().replace("-", "_")
        for target, aliases in _PARAM_ALIASES.items():
            if target not in names or target in out:
                continue
            if kl == target or kl in aliases or kl.endswith("_" + target):
                out[target] = out.pop(key)
                break
    return out


class ToolCallValidator:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def validate(self, call: ParsedCall) -> ValidationResult:
        if call.parse_error:
            return ValidationResult(False, call.name, call.args, [f"unparseable arguments: {call.parse_error}"])
        spec = self.registry.get(call.name)
        if spec is None:
            return ValidationResult(
                False, call.name, call.args,
                [f"unknown tool {call.name!r}. Available: {', '.join(self.registry.names())}"],
            )
        errors: list[str] = []
        args: dict[str, Any] = {}
        provided = _fold_aliases(dict(call.args or {}), {p.name for p in spec.params})
        for p in spec.params:
            if p.name in provided:
                coerced, err = _coerce(provided.pop(p.name), p)
                if err:
                    errors.append(err)
                else:
                    args[p.name] = coerced
            elif p.required:
                errors.append(f"missing required parameter '{p.name}'")
            elif p.default is not None:
                args[p.name] = p.default
        for p in spec.params:
            if p.enum and p.name in args and not isinstance(args[p.name], (dict, list)):
                if str(args[p.name]).lower() not in {e.lower() for e in p.enum}:
                    args[p.name] = _closest(str(args[p.name]), p.enum) or args[p.name]
        args.update(provided)  # tolerate extra keys (small models love them)
        if errors:
            sig = ", ".join(
                f"{p.name}:{p.type}{' (required)' if p.required else ''}" for p in spec.params
            )
            errors.append(f"call {call.name} with exactly these parameters: {sig}")
        return ValidationResult(not errors, call.name, args, errors)


def _closest(value: str, options: tuple[str, ...]) -> str | None:
    v = value.lower().replace("_", " ")
    for o in options:
        if o.lower() == v or o.lower() in v or v in o.lower():
            return o
    return None
