"""Mock backend — fully local scripted model for tests, demos, and 'mock' dev mode.

It implements real logic (memory-trigger tool calls, streaming, pulls) so the
whole platform can be exercised without a GPU or any network. It is selected
explicitly (backend = "mock"); the app never silently substitutes it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from ..config.models import BackendName, GenerationSettings
from .base import BaseBackend, ChatChunk, ModelInfo, StartResult

MOCK_MODELS = [
    ("mock-chat-1b", 900 * 2**20, "Q4_K_M", 1.0),
    ("mock-tool-7b", 4_400 * 2**20, "Q4_K_M", 7.6),
    ("mock-reason-13b", 7_700 * 2**20, "Q5_K_M", 13.0),
]

_NAME_RX = re.compile(r"(?:my name is|call me|i am|i'm)\s+([A-Z][\w'-]{1,20})", re.IGNORECASE)
_PET_RX = re.compile(r"(?:a|my)\s+(rabbit|cat|dog|hamster|parrot|guinea pig|fox|turtle)\s+(?:named|called)\s+([A-Z][\w'-]{1,20})", re.IGNORECASE)
_PROJECT_RX = re.compile(r"(?:working on|building|creating)\s+(?:a|an)?\s*(.+?)(?:\s+(?:project|called)\s+([A-Za-z][\w-]*))?\s*(?:[.!]|$)", re.IGNORECASE)
_REMEMBER_RX = re.compile(r"\bremember[:\s]+(?:that\s+)?(.+)", re.IGNORECASE)
_FORGET_RX = re.compile(r"\bforget (?:that |about )?(.+)", re.IGNORECASE)
_FILE_RX = re.compile(r"""create (?:a |the )?file (?P<path>[\w./-]+)(?: with(?: the)? content (?P<content>"[^"]*"|'[^']*'))?""", re.IGNORECASE)


class MockBackend(BaseBackend):
    name = BackendName.MOCK
    display_name = "Mock (dev/test)"
    delay: float = 0.0  # per-token latency; class-level so servers-in-process pick it up

    def __init__(self, config, transport=None) -> None:
        super().__init__(config, transport)
        self.extra_models: list[str] = []
        self.script: list[list[ChatChunk]] = []  # canned turns for tests

    # ----------------------------------------------------------- presence
    def is_installed(self) -> bool:
        return True

    async def is_running(self) -> bool:
        return True

    async def health(self) -> tuple[bool, dict[str, Any]]:
        return True, {"mock": True, "version": "0.1"}

    async def ensure_running(self) -> StartResult:
        return StartResult(ok=True, was_already_running=True, message="Mock backend is always available.")

    # ------------------------------------------------------------- models
    async def list_models(self) -> list[ModelInfo]:
        out = []
        for name, size, quant, params in MOCK_MODELS:
            info = ModelInfo(
                name=name, backend=self.name, size_bytes=size, quant=quant, params_b=params,
                ctx_len=8192, tool_capable="tool" in name or params >= 7,
            ).apply_name_heuristics()
            out.append(info)
        for name in self.extra_models:
            out.append(ModelInfo(name=name, backend=self.name, size_bytes=1_000 * 2**20, quant="Q4_K_M", params_b=1.0, tool_capable=None).apply_name_heuristics())
        return out

    # ---------------------------------------------------------------- chat
    def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: GenerationSettings,
        *,
        tools: list[dict[str, Any]] | None = None,
        native_tools: bool = False,
    ) -> AsyncIterator[ChatChunk]:
        return self._stream(model, messages, tools, native_tools, params)

    async def _stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        native: bool,
        params: GenerationSettings | None = None,
    ) -> AsyncIterator[ChatChunk]:
        if self.script:
            for chunk in self.script.pop(0):
                yield chunk
                await asyncio.sleep(0)
            return
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if params is not None and params.ultrathink:
            async for chunk in self._ultrathink_reasoning(str(last_user)):
                yield chunk
        last_u = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        tool_results = [m for m in messages[last_u + 1 :] if m.get("role") in ("tool", "function")]
        if tool_results:
            kinds = ", ".join(sorted({str(m.get("name", "tool")) for m in tool_results}))
            reply = f"Done — I used {kinds} and everything is saved. Anything else?"
            async for chunk in self._emit_text(reply):
                yield chunk
            return
        calls = self._plan_calls(str(last_user), True)
        if calls and not any(call.get("_text_only") for call in calls):
            if native:
                yield ChatChunk(content="", tool_calls=calls, done=True, finish="tool_calls", usage={"eval_count": 32})
            else:
                reply = "Let me save that.\n\n```json\n" + json.dumps({"tool_calls": calls}, indent=0) + "\n```"
                async for chunk in self._emit_text(reply):
                    yield chunk
            return
        reply = self._plain_reply(str(last_user))
        async for chunk in self._emit_text(reply):
            yield chunk

    async def _ultrathink_reasoning(self, question: str) -> AsyncIterator[ChatChunk]:
        """Synthesized deep-reasoning stream so /ultrathink is observable without a reasoning model."""
        lines = [
            "Let me actually think this through instead of answering reflexively.\n",
            f"The request boils down to: {question[:120]}\n",
            "Interpretation A: the literal reading — do exactly what was asked, nothing more.\n",
            "Interpretation B: the intent behind it — what problem is this solving?\n",
            "Assumptions check: workspace state, prior turns, tool availability — all consistent so far.\n",
            "Edge cases: empty input, paths with spaces, a model without tool support, repeated calls.\n",
            "Weighing A vs B... B fits the conversation better. Verifying once more against the last message... yes.\n",
            "I'm confident in the approach now. Composing the answer.\n",
        ]
        for line in lines:
            for i in range(0, len(line), 24):
                yield ChatChunk(reasoning=line[i : i + 24])
                await asyncio.sleep(self.delay)

    def _plan_calls(self, user_text: str, has_tools: bool) -> list[dict[str, Any]]:
        if not has_tools:
            return []
        calls: list[dict[str, Any]] = []
        m = _NAME_RX.search(user_text)
        if m:
            name = m.group(1)
            calls.append({
                "id": "call_name", "type": "function",
                "function": {"name": "soul_append", "arguments": {"section": "User", "text": f"User's name is {name}."}},
            })
        m = _FILE_RX.search(user_text)
        if m:
            content = (m.group("content") or "").strip("\"'").strip()
            calls.append({
                "id": "call_file", "type": "function",
                "function": {"name": "write_file", "arguments": {"path": m.group("path"),
                                                                  "content": content or "hello from the mock"}},
            })
        m = _PET_RX.search(user_text)
        if m:
            animal, pet = m.group(1).title(), m.group(2)
            calls.append({
                "id": "call_pet", "type": "function",
                "function": {"name": "soul_append", "arguments": {"section": "Pets", "text": f"User has a pet {animal.lower()} named {pet}."}},
            })
        m = _PROJECT_RX.search(user_text)
        if m and (m.group(2) or len(m.group(1) or "") > 4):
            what, titled = m.group(1).strip().rstrip(",.;:"), m.group(2)
            text = f"User is working on {what}" + (f" called {titled}" if titled else "") + "."
            calls.append({
                "id": "call_proj", "type": "function",
                "function": {"name": "soul_upsert", "arguments": {"section": "Projects", "text": text}},
            })
        m = _FORGET_RX.search(user_text)
        if m:
            calls.append({
                "id": "call_forget", "type": "function",
                "function": {"name": "soul_forget", "arguments": {"query": m.group(1).strip().rstrip(".!?")}},
            })
            return calls
        m = _REMEMBER_RX.search(user_text)
        if m:
            calls.append({
                "id": "call_rem", "type": "function",
                "function": {"name": "soul_append", "arguments": {"section": "Important Notes", "text": m.group(1).strip().rstrip(".!?") + "."}},
            })
        return calls

    def _plain_reply(self, user_text: str) -> str:
        m = _NAME_RX.search(user_text)
        if m:
            return f"Hello {m.group(1)}! Nice to meet you."
        m = _PET_RX.search(user_text)
        if m:
            return f"{m.group(2)} sounds like a wonderful {m.group(1).lower()}!"
        if re.search(r"\b(hi|hello|hey|yo)\b", user_text, re.IGNORECASE):
            return "Hello! I'm OhMyLLM's mock model — a fully local stand-in so you can explore the interface before wiring up Ollama or LM Studio."
        if _PROJECT_RX.search(user_text):
            return "Noted — I'll keep that project in memory."
        return (
            "Mock reply: this is the built-in dev backend. Configure Ollama or LM Studio "
            "in the Backend screen for real local inference. You said: "
            + (user_text[:200] + "…" if len(user_text) > 200 else user_text)
        )

    async def _emit_text(self, reply: str) -> AsyncIterator[ChatChunk]:
        for tok in re.findall(r"\S+\s*", reply):
            yield ChatChunk(content=tok)
            await asyncio.sleep(self.delay)
        n = len(reply.split())
        yield ChatChunk(done=True, finish="stop", usage={"eval_count": max(n, 8)})

    # ---------------------------------------------------------------- pull
    async def pull(self, name: str, progress: Any = None) -> None:
        total = 10
        for i in range(total + 1):
            if progress:
                progress("downloading mock weights", i / total)
            await asyncio.sleep(0)
        if name not in self.extra_models:
            self.extra_models.append(name)
