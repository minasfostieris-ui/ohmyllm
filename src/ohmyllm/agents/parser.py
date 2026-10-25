"""ToolCallParser: native calls, fenced JSON, ReAct — with JSON repair."""

from __future__ import annotations

import ast
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"  # native|json|react
    raw: str = ""
    id: str = field(default_factory=lambda: f"call_{secrets.token_hex(4)}")
    parse_error: str | None = None


@dataclass
class ParseReport:
    calls: list[ParsedCall]
    leftover_text: str
    had_malformed: bool = False
    malformed_reason: str = ""


_FENCE_RX = re.compile(r"```(?:json|JSON|tool|tool_call|functions\\.\\w+)?\s*\n(.*?)```", re.DOTALL)
_REACT_RX = re.compile(
    r"Action\s*:\s*['\"`]?([A-Za-z_][\w.]*)['\"`]?\s*\n+?\s*Action\s*Input\s*:\s*(.*?)(?=\n\s*(?:Thought|Action|Final Answer)\s*:|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_FINAL_RX = re.compile(r"Final Answer\s*:\s*(.*)", re.DOTALL | re.IGNORECASE)


def repair_json(raw: str) -> tuple[Any | None, str]:
    """Best-effort JSON salvage for weak local models. Returns (obj, error)."""
    text = raw.strip()
    err = "not valid JSON"
    # 1. direct: strict JSON, then python-literal style dicts
    obj, e = _try(text)
    if e is None:
        return obj, ""
    err = e
    obj, e = _try_python(text)
    if e is None:
        return obj, ""
    # 2. extract outermost braces
    start = text.find("{")
    end = text.rfind("}")
    outer = text[start : end + 1] if start >= 0 and end > start else ""
    # 3. normalize common damage
    fixes: list[str] = []
    base = outer or text
    s = base
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    s = re.sub(r",\s*([}\]])", r"\1", s)                      # trailing commas
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][\w.-]*)(\s*:)', r'\1"\2"\3', s)  # unquoted keys
    s = re.sub(r"(:\s*)'([^'\\]*)'", r'\1"\2"', s)            # single-quoted values
    s = re.sub(r"([{,]\s*)'([^'\\]*)'(\s*:)", r'\1"\2"\3', s)  # single-quoted keys
    s2 = _balance(s)
    s_py = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", s))
    s_py = re.sub(r"\bNone\b", "null", s_py)
    fixes.extend([s2, _balance(s_py), re.sub(r"\n\s*\n", "\n", s2)])
    for cand in fixes:
        obj, e = _try(cand)
        if e is None:
            return obj, ""
        err = e or err
        obj, e = _try_python(cand)
        if e is None:
            return obj, ""
    # 4. truncated json: cut to last complete brace pair
    if outer:
        trimmed = outer[: outer.rfind("}") + 1] if "}" in outer else ""
        for cand in (trimmed, _balance(trimmed)):
            if cand:
                obj, e = _try(cand)
                if e is None:
                    return obj, ""
    return None, err or "not valid JSON"


def _try(text: str) -> tuple[Any | None, str | None]:
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _try_python(text: str) -> tuple[Any | None, str | None]:
    try:
        return ast.literal_eval(text), None
    except (ValueError, SyntaxError) as exc:
        return None, str(exc)


def _balance(text: str) -> str:
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    closing = "".join(("}" if c == "{" else "]") for c in reversed(stack))
    if in_str:
        text += '"'
    return text + closing


class ToolCallParser:
    """Turns model output into ParsedCall lists from any protocol."""

    # ------------------------------------------------------------ native
    def from_native(self, tool_calls: list[dict[str, Any]]) -> list[ParsedCall]:
        out: list[ParsedCall] = []
        for tc in tool_calls or []:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or tc.get("name") or "")
            raw_args = fn.get("arguments", tc.get("arguments", {}))
            args: dict[str, Any]
            err: str | None = None
            if isinstance(raw_args, str):
                obj, err = self._coerce_obj(raw_args)
                args = obj if isinstance(obj, dict) else {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args, err = {}, "arguments not an object"
            call = ParsedCall(name=name, args=args, source="native", raw=str(raw_args), parse_error=err)
            if tc.get("id"):
                call.id = str(tc["id"])
            out.append(call)
        return out

    # ---------------------------------------------------------------- text
    def from_text(self, text: str) -> ParseReport:
        calls: list[ParsedCall] = []
        had_malformed = False
        reason = ""
        leftover = text

        # fenced JSON blocks
        for m in _FENCE_RX.finditer(text):
            body = m.group(1).strip()
            if not (body.startswith("{") or body.startswith("[")):
                continue
            obj, err = self._coerce_obj(body)
            if err:
                had_malformed, reason = True, err
                continue
            for call in self._calls_from_obj(obj, source="json", raw=body):
                calls.append(call)
            leftover = leftover.replace(m.group(0), "").strip()

        if calls:
            return ParseReport(calls, leftover, had_malformed, reason)

        # ReAct
        for m in _REACT_RX.finditer(text):
            name = m.group(1)
            body = m.group(2).strip()
            args: dict[str, Any] = {}
            if body and body not in ("{}", "{}\n"):
                obj, err = self._coerce_obj(body)
                if err:
                    calls.append(ParsedCall(name=name, args={}, source="react", raw=body, parse_error=err))
                    had_malformed, reason = True, err
                    continue
                args = obj if isinstance(obj, dict) else {"value": obj}
            calls.append(ParsedCall(name=name, args=args, source="react", raw=body))
            leftover = leftover.replace(m.group(0), "").strip()
        if calls:
            fm = _FINAL_RX.search(text)
            if fm:
                leftover = fm.group(1).strip()
            return ParseReport(calls, leftover, had_malformed, reason)

        # bare trailing JSON object that looks like a tool call
        stripped = text.strip()
        if stripped.startswith("{") and ("tool" in stripped or "name" in stripped or "function" in stripped):
            obj, err = self._coerce_obj(stripped)
            if not err:
                calls = self._calls_from_obj(obj, source="json", raw=stripped)
                if calls:
                    return ParseReport(calls, "", had_malformed, reason)
            else:
                had_malformed, reason = True, err
        return ParseReport([], text.strip(), had_malformed, reason)

    # -------------------------------------------------------------- helpers
    def _coerce_obj(self, body: str) -> tuple[dict[str, Any] | list | None, str | None]:
        obj, err = repair_json(body)
        if err:
            return None, err
        if isinstance(obj, (dict, list)):
            return obj, None
        return None, "JSON is not an object"

    def _calls_from_obj(self, obj: Any, source: str, raw: str) -> list[ParsedCall]:
        out: list[ParsedCall] = []
        if isinstance(obj, list):
            for item in obj:
                out.extend(self._calls_from_obj(item, source, raw))
            return out
        if not isinstance(obj, dict):
            return out
        if "tool_calls" in obj and isinstance(obj["tool_calls"], list):
            for tc in obj["tool_calls"]:
                out.extend(self._calls_from_obj(tc, source, raw))
            return out
        if "function_call" in obj and isinstance(obj["function_call"], dict):
            out.extend(self._calls_from_obj(obj["function_call"], source, raw))
            return out
        name = obj.get("name") or obj.get("tool") or (obj.get("function") or {}).get("name")
        if not name:
            return out
        args: Any = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if (obj.get("function") or {}).get("arguments") is not None:
            args = obj["function"]["arguments"]
        if isinstance(args, str):
            parsed, err = self._coerce_obj(args)
            args = parsed if isinstance(parsed, dict) else ({"_raw": args} if err else {})
        if not isinstance(args, dict):
            args = {"value": args}
        out.append(ParsedCall(name=str(name), args=args, source=source, raw=raw))
        return out
