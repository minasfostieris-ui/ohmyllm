"""Minimal TOML writing + reading for OhMyLLM config.

`tomllib` (stdlib) handles reading. Writing is done by a small emitter so the
package keeps zero extra dependencies; the output is valid TOML 1.0 for the
subset of values the config model uses (scalars, inline scalar lists, tables,
array-of-tables, None values skipped).
"""

from __future__ import annotations

import contextlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        # json escapes <0x20 but NOT DEL/C1 — TOML forbids raw control chars in
        # basic strings, so a stray \x7f in a provider name used to produce a
        # file tomllib refuses to load (and load() then silently resets to
        # defaults — wiping every API key). Escape everything non-printable.
        out = json.dumps(v, ensure_ascii=False)
        for bad in [chr(c) for c in range(0x7F, 0xA0)]:
            out = out.replace(bad, f"\\u{ord(bad):04x}")
        return out
    raise TypeError(f"unsupported TOML scalar: {type(v).__name__}")


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (bool, int, float, str))


def _inline_list(v: list[Any]) -> str:
    inner = ", ".join(_scalar(x) if _is_scalar(x) else json.dumps(x) for x in v)
    return f"[{inner}]"


def _emit(data: Mapping[str, Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    scalars: list[tuple[str, Any]] = []
    tables: list[tuple[str, Mapping[str, Any]]] = []
    aots: list[tuple[str, list[Any]]] = []
    for key, value in data.items():
        if value is None:
            continue
        name = key if _needs_quotes(key) is False else json.dumps(key)
        if isinstance(value, Mapping):
            tables.append((name, value))
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(x, Mapping) for x in value)
        ):
            aots.append((name, value))
        elif isinstance(value, list):
            scalars.append((name, _inline_list(value) if value else "[]"))
        elif _is_scalar(value):
            scalars.append((name, _scalar(value)))
        else:
            scalars.append((name, json.dumps(value, ensure_ascii=False)))
    if prefix and (scalars or not tables and not aots):
        lines.append(f"[{prefix}]")
    for name, val in scalars:
        lines.append(f"{name} = {val}")
    for name, sub in tables:
        if lines:
            lines.append("")
        full = f"{prefix}.{name}" if prefix else name
        lines.extend(_emit(sub, full))
    for name, items in aots:
        full = f"{prefix}.{name}" if prefix else name
        for item in items:
            if lines:
                lines.append("")
            lines.append(f"[[{full}]]")
            lines.extend(_emit(item, ""))
    return lines


def _needs_quotes(key: str) -> bool:
    # TOML bare keys are ASCII-only: non-ASCII isalnum() (e.g. "ключ") is NOT
    # bare-safe — it must be quoted or tomllib rejects the file
    if not key:
        return True
    return not all(c.isascii() and (c.isalnum() or c in "_-") for c in key)


def dumps(data: Mapping[str, Any]) -> str:
    lines = _emit(data)
    return "\n".join(lines).strip("\n") + "\n" if lines else ""


def loads(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def load_file(path: Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def dump_file(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

    # unique temp + 0600 from creation: concurrent saves must not interleave on
    # one fixed .tmp name, and the file may hold provider API keys
    target = path.resolve() if path.is_symlink() else path  # dotfiles users symlink config.toml
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):  # NTFS has no POSIX modes
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dumps(data))
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
