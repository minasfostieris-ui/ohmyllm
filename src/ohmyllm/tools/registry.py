"""ToolRegistry: spec catalog, native schemas, prompt block, dispatch."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from .file_tools import (
    list_dir,
    patch_file,
    preview_patch,
    preview_write,
    read_file,
    search_files,
    write_file,
)
from .net_tools import http_fetch
from .shell_tools import preview_shell, run_command
from .soul_tools import soul_append, soul_forget, soul_read, soul_search, soul_upsert
from .spec import ToolContext, ToolResult, ToolSpec, arg
from .system_tools import get_system_status

_SECTIONS_ENUM = ("User", "Projects", "Preferences", "Pets", "Lessons", "Important Notes")

def _skills_list(ctx, args: dict) -> ToolResult:
    mgr = ctx.skills
    if mgr is None:
        return ToolResult(False, "", "skills are unavailable")
    rows = mgr.list()
    if not rows:
        return ToolResult(True, "no skills imported yet — the user can add some with `oml skills import <folder>`")
    return ToolResult(True, "\n".join(f"{s.name} — {s.description} (dir: {s.dir})" for s in rows))


def _skills_read(ctx, args: dict) -> ToolResult:
    mgr = ctx.skills
    if mgr is None:
        return ToolResult(False, "", "skills are unavailable")
    name = str(args.get("name") or "")
    text = mgr.read(name)
    if text is None:
        avail = ", ".join(s.name for s in mgr.list()[:15])
        return ToolResult(False, "", f"no skill named {name!r}. available: {avail or 'none'}")
    return ToolResult(True, text)

SPECS: list[ToolSpec] = [
    ToolSpec(
        "soul_read", "Read the full persistent memory file (soul.md).", [],
        soul_read, examples="{}",
    ),
    ToolSpec(
        "soul_search", "Search soul.md memory entries.",
        [arg("query", "string", "text to look for"),
         arg("section", "string", "limit to one section", required=False, enum=_SECTIONS_ENUM)],
        soul_search, examples='{"query": "rabbit"}',
    ),
    ToolSpec(
        "soul_append", "Append a concise durable memory (facts, preferences, projects, pets). Never store secrets.",
        [arg("section", "string", "target section", required=False, enum=_SECTIONS_ENUM, default="Important Notes"),
         arg("text", "string", "one-sentence memory, third person, factual"),
         arg("force", "boolean", "only when the user explicitly asked to store a secret", required=False)],
        soul_append, examples='{"section": "User", "text": "The user is named Minas."}',
    ),
    ToolSpec(
        "soul_upsert", "Save a memory, updating a similar existing entry instead of duplicating it.",
        [arg("section", "string", "target section", required=False, enum=_SECTIONS_ENUM, default="Projects"),
         arg("text", "string", "one-sentence memory"),
         arg("force", "boolean", "explicit-secret consent", required=False)],
        soul_upsert, examples='{"section": "Projects", "text": "User is working on a CLI project called OhMyLLM."}',
    ),
    ToolSpec(
        "soul_forget", "Remove a memory entry the user asked to forget.",
        [arg("query", "string", "text of the memory to remove (or its distinctive words)"),
         arg("section", "string", "section hint", required=False, enum=_SECTIONS_ENUM),
         arg("index", "integer", "entry number from soul_search", required=False)],
        soul_forget, examples='{"query": "rabbit"}',
    ),
    ToolSpec(
        "read_file", "Read a file: text (line-numbered), images, audio (transcribed) and video (sampled frames + audio transcript) — media is shown when the model supports it.",
        [arg("path", "string", "file path"),
         arg("offset", "integer", "1-based start line", required=False),
         arg("max_lines", "integer", "lines to return (default 400)", required=False)],
        read_file, examples='{"path": "src/main.py", "max_lines": 120}',
    ),
    ToolSpec(
        "list_dir", "List a directory (names, types, sizes).",
        [arg("path", "string", "directory path ('.' for workspace root)", required=False),
         arg("limit", "integer", "max entries", required=False)],
        list_dir, examples='{"path": "src"}',
    ),
    ToolSpec(
        "search_files", "Search file contents under a path; returns file:line: text.",
        [arg("query", "string", "substring (or regex when regex=true)"),
         arg("path", "string", "root directory", required=False),
         arg("glob", "string", "filename filter like '*.py'", required=False),
         arg("regex", "boolean", "treat query as regex", required=False),
         arg("case_sensitive", "boolean", "case sensitive match", required=False),
         arg("max_results", "integer", "default 80", required=False)],
        search_files, examples='{"query": "def main", "glob": "*.py"}',
    ),
    ToolSpec(
        "write_file", "Create/overwrite (or append to) a file inside the workspace. Backups and a diff are produced automatically.",
        [arg("path", "string", "file path"),
         arg("content", "string", "full file content (or text to append)"),
         arg("append", "boolean", "append instead of overwrite", required=False)],
        write_file, preview=preview_write, examples='{"path": "notes.md", "content": "# Notes\\n"}',
    ),
    ToolSpec(
        "patch_file", "Replace exact text inside a file (unique match required unless replace_all).",
        [arg("path", "string", "file path"),
         arg("old_text", "string", "exact existing text"),
         arg("new_text", "string", "replacement text"),
         arg("replace_all", "boolean", "replace every occurrence", required=False)],
        patch_file, preview=preview_patch, examples='{"path": "app.py", "old_text": "port=80", "new_text": "port=8080"}',
    ),
    ToolSpec(
        "run_command", "Run a shell command (subject to the permission mode and allowlist). Returns exit code, stdout, stderr.",
        [arg("command", "string", "exact command line"),
         arg("cwd", "string", "working directory (default: workspace)", required=False),
         arg("timeout", "number", "seconds (default from settings)", required=False)],
        run_command, preview=preview_shell, examples='{"command": "pytest -q tests/unit"}',
    ),
    ToolSpec(
        "http_fetch", "Fetch a URL (disabled by default; needs user approval).",
        [arg("url", "string", "http(s) URL"),
         arg("timeout", "number", "seconds", required=False)],
        http_fetch, examples='{"url": "https://example.com"}',
    ),
    ToolSpec(
        "get_system_status", "Report backend, model, permission mode, soul state, GPU and paths.", [],
        get_system_status, examples="{}",
    ),
    ToolSpec(
        "skills_list", "List imported skill guides (name — when to use them).", [],
        _skills_list, examples="{}",
    ),
    ToolSpec(
        "skills_read", "Read one imported skill's full instructions. Call BEFORE doing work in its domain.",
        [arg("name", "string", "skill name from skills_list")],
        _skills_read, examples='{"name": "frontend"}',
    ),
]


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for s in specs if specs is not None else SPECS:
            self.register(s)

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    # ------------------------------------------------------------- schemas
    def native_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.json_schema(),
                },
            }
            for s in self._specs.values()
        ]

    def preview(self, name: str, args: dict[str, Any]) -> str:
        spec = self._specs.get(name)
        if spec and spec.preview:
            try:
                return spec.preview(args)
            except Exception as exc:  # noqa: BLE001 - preview is best-effort
                return f"(preview failed: {exc})"
        try:
            return "args: " + _compact(args)
        except Exception:  # noqa: BLE001
            return "(no preview)"

    async def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(False, "", f"unknown tool {name!r}")
        try:
            result = spec.handler(ctx, args)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool crash becomes a tool error
            return ToolResult(False, "", f"tool {name} crashed: {type(exc).__name__}: {exc}")
        if isinstance(result, ToolResult):
            return result
        return ToolResult(True, str(result))


def _compact(args: dict[str, Any]) -> str:
    import json

    return json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)[:300]


def build_default_registry() -> ToolRegistry:
    return ToolRegistry()
