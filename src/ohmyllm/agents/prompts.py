"""PromptBuilder: base system prompt + soul.md injection + tool protocol."""

from __future__ import annotations

from typing import Any

from ..config.models import Config, PermissionMode
from ..memory import SoulManager
from ..tools.registry import ToolRegistry

BASE_SYSTEM = """You are OhMyLLM, a local-first AI agent running entirely on this user's machine.
You are helpful, concise and practical. You prefer doing over explaining.

Ground rules:
- Everything you see stays local. Never invent facts about the user; rely on the memory block.
- When you learn a durable fact (name, pet, project, preference, workflow, recurring constraint),
  save it with the memory tools (soul_append / soul_upsert; check with soul_search first).
  Never store passwords, tokens or secrets unless the user explicitly insists.
- When the user asks you to forget something, use soul_forget.
- The MEMORY section injected below already contains everything stored about the user.
  For questions about the user (name, pets, preferences, projects), ANSWER FROM IT
  DIRECTLY — do NOT search or read files to "verify" memory; the files are not the source.
- Prefer small, verifiable steps. Use tools instead of guessing file contents, but never
  re-search for something you already found this turn.
- You have NO power to change the permission mode; if an action is blocked, explain what
  the user can adjust in Settings — do not attempt workarounds."""

ULTRATHINK_NOTE = ("<system-directive>ULTRATHINK MODE: before answering, reason at length "
                   "and with maximum care. Enumerate every interpretation, check each "
                   "assumption, consider edge cases, and verify your final result by "
                   "re-deriving it a second, different way. Be exhaustive; do not rush."
                   "</system-directive>")

ULTRATHINK = """## ULTRATHINK mode (active)

The user asked you to think this one through properly. Before answering or
calling any tool:
- Work through the problem step by step in your reasoning; write the steps
  out, do not skip to a conclusion.
- Enumerate at least two plausible alternative interpretations or
  approaches, and say why you chose yours.
- State the assumptions you are making and check each one against the
  conversation or the files before relying on it.
- For anything non-trivial, consider edge cases, failure modes, and what
  could go wrong before acting.
- Verify your own answer: re-derive the key facts, and if something does
  not check out, revise before committing.
Be thorough over fast. The user prefers a long, careful answer to a quick
shallow one."""

MEMORY_GUIDE = """\
## Memory (soul.md)
{content}"""

JSON_PROTOCOL = """\
## Tool usage protocol
You may call tools by emitting ONE fenced JSON block like:

```json
{"tool_calls": [{"name": "soul_append", "arguments": {"section": "User", "text": "..."}}]}
```

Rules:
- One or more calls per block; use ONLY the tool names listed below.
- After a tool result arrives, continue reasoning or answer the user.
- If you do not need a tool, just answer normally (no JSON block).

## Available tools
{catalog}"""

REACT_PROTOCOL = """\
## Tool usage protocol (ReAct)
Think step by step, then act:

Thought: <why a tool helps>
Action: <tool name>
Action Input: <JSON object of arguments>

When you no longer need tools, reply:
Final Answer: <your answer to the user>

Use ONLY the tool names listed below. Keep thoughts short.

## Available tools
{catalog}"""


class PromptBuilder:
    def __init__(self, config: Config, registry: ToolRegistry, soul: SoulManager | None = None,
                 skills: Any = None, workspace: Any = None) -> None:
        self.config = config
        self.registry = registry
        self.soul = soul
        self.skills = skills
        self.workspace = workspace

    def catalog(self) -> str:
        lines = []
        for s in self.registry.specs():
            params = ", ".join(
                f"{p.name}: {p.type}" + ("" if p.required else "?") for p in s.params
            )
            lines.append(f"- {s.name}({params}) — {s.description}")
            if s.examples and s.examples != "{}":
                lines.append(f"  example: {s.examples}")
        return "\n".join(lines)

    def system_prompt(
        self,
        *,
        mode: PermissionMode,
        native: bool,
        allowed_tools: list[str] | None = None,
        ultrathink: bool = False,
    ) -> str:
        parts = [BASE_SYSTEM]
        if self.workspace:
            parts.append(
                f"Workspace root: {self.workspace}\n"
                "Relative paths resolve inside it. In sandboxed mode everything under the "
                "workspace is yours to read and write (writes may ask once) — never claim a "
                "path inside it is 'outside your workspace'. Anything outside needs approval."
            )
        if self.config.memory.enabled and self.soul is not None:
            block = self.soul.for_injection(self.config.memory.max_inject_chars)
            if block:
                parts.append(MEMORY_GUIDE.format(content=block))
        if not native:
            proto = REACT_PROTOCOL if mode is not PermissionMode.FULL_ACCESS else JSON_PROTOCOL
            parts.append(proto.format(catalog=self.catalog()))
        if self.skills is not None:
            try:
                rows = self.skills.list()
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                lines = "\n".join(f"- {s.name}: {s.description}" for s in rows[:40])
                parts.append(
                    "## Skills (imported guides)\n"
                    "When the task matches a skill below, call skills_read(name) FIRST and follow it.\n"
                    + lines
                )
        mode_note = {
            PermissionMode.STRICT: "Current mode: strict — chat only; memory writes only on explicit user request.",
            PermissionMode.SAFE: "Current mode: safe — read-only tools.",
            PermissionMode.SANDBOXED: "Current mode: sandboxed — workspace-scoped tools; writes may ask the user.",
            PermissionMode.DANGEROUS: "Current mode: dangerous — elevated access, approvals on risky actions.",
            PermissionMode.FULL_ACCESS: "Current mode: full_access — no routine prompts.",
        }[mode]
        parts.append(mode_note)
        if ultrathink:
            parts.append(ULTRATHINK)
        return "\n\n".join(parts)

    def assemble(self, system: str, history: list[dict]) -> list[dict]:
        return [{"role": "system", "content": system}, *history]


def trim_messages(history: list[dict], budget_chars: int) -> list[dict]:
    """Keep the newest messages that fit; collapse the rest into a note."""
    total = sum(len(str(m.get("content", ""))) for m in history)
    if total <= budget_chars:
        return history
    kept: list[dict] = []
    used = 0
    for m in reversed(history):
        size = len(str(m.get("content", "")))
        if used + size > budget_chars and kept:
            break
        kept.append(m)
        used += size
    dropped = len(history) - len(kept)
    kept.reverse()
    if kept and kept[0].get("role") == "system":
        kept = kept[1:]
        dropped += 1
    marker = {"role": "system", "content": f"[{dropped} earlier message(s) trimmed to fit the context window]"}
    return [marker, *kept]
