"""AgentRuntime: stream → parse → validate → POLICY → approve → execute → loop.

Invariant: no tool handler runs unless PolicyEngine said yes (and, when the
policy demands it, the ApprovalManager said yes too). The model never holds
the permission mode; config does.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import guard as guard_mod
from ..backends.base import BackendError, BaseBackend
from ..config.models import Config, PermissionMode
from ..memory import SoulManager
from ..safety.approvals import ApprovalManager, ApprovalOutcome, PromptFn
from ..safety.audit import AuditLogger
from ..safety.policy import Decision, PolicyEngine
from ..sessions import Session
from ..tools.registry import ToolRegistry
from ..tools.spec import ToolContext, ToolResult
from .parser import ParsedCall, ToolCallParser
from .prompts import PromptBuilder, trim_messages
from .validator import ToolCallValidator

CORRECTION_MSG = (
    "Your previous tool call could not be used: {reason}. "
    "Emit a corrected tool call (exact JSON, one of the listed tools), or answer directly."
)

OnEvent = Callable[[dict[str, Any]], None] | None


@dataclass
class TurnResult:
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    error: str | None = None
    cancelled: bool = False
    tokens_per_s: float = 0.0
    ttft_s: float = 0.0  # time to first token (prefill + queue + load)
    reasoning: str = ""  # thinking-channel text (for the expandable UI block)
    tokens: int = 0  # generated tokens incl. reasoning (reported or estimated)
    usage: dict[str, Any] = field(default_factory=dict)


class AgentCancelled(asyncio.CancelledError):
    pass


class AgentRuntime:
    def __init__(
        self,
        *,
        backend: BaseBackend,
        registry: ToolRegistry,
        policy: PolicyEngine,
        approvals: ApprovalManager,
        audit: AuditLogger,
        config: Config,
        soul: SoulManager | None,
        skills: Any = None,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.config = config
        self.soul = soul
        self.parser = ToolCallParser()
        self.validator = ToolCallValidator(registry)
        self.skills = skills
        self.prompt_builder = PromptBuilder(config, registry, soul, skills=skills,
                                            workspace=policy.workspace)
        self.last_usage: dict[str, Any] = {}

    # ---------------------------------------------------------------- turn
    async def run_turn(
        self,
        session: Session,
        user_text: str,
        *,
        model: str | None = None,
        prompt_fn: PromptFn | None = None,
        on_event: OnEvent = None,
        cancel: asyncio.Event | None = None,
        user_requested_memory: bool = False,
        interjections: list[str] | None = None,
    ) -> TurnResult:
        model = model or self._default_model()
        if not model:
            model = await self._auto_model()
        params = self.config.settings_for(self.backend.name, model)
        mode = self.policy.mode
        session.backend = self.backend.name.value
        session.model = model
        session.permission_mode = mode.value
        session.add_message("user", user_text)
        emit = on_event or (lambda _e: None)
        result = TurnResult()
        self._invalid_counts: dict[tuple[str, str], int] = {}
        ctx = ToolContext(
            workspace=self.policy.workspace,
            mode=mode.value,
            config=self.config,
            soul=self.soul,
            skills=getattr(self, "skills", None),
            status_provider=self._status_provider(model),
            user_requested_memory=user_requested_memory,
        )
        native_pref = self.config.agent.native_tools
        native = native_pref == "force"
        if native_pref == "off":
            native = False
        elif native_pref == "auto":
            try:
                info = await self.backend.get_model(model)
            except BackendError:
                info = None
            native = bool(info is not None and info.tool_capable)
        emit({"type": "turn_start", "model": model, "backend": self.backend.name.value, "mode": mode.value, "protocol": "native" if native else "text"})

        retries = 0
        total_tokens = 0
        gen_time = 0.0
        args_chars = 0
        turn_ttft: float | None = None
        eval_count = 0
        eval_ns = 0
        reasoning_chars = 0
        reasoning_text: list[str] = []
        self._turn_cache: dict[str, str] = {}
        final_text = ""
        step = 0
        while step < self.config.agent.max_steps:
            self._check_cancel(cancel)
            step += 1
            result.steps = step
            # user messages typed mid-turn land in the NEXT model call,
            # alongside the tool results produced so far
            while interjections:
                inj = str(interjections.pop(0)).strip()
                if inj:
                    session.add_message("user", inj)
                    emit({"type": "interjection", "text": inj})
            await self._maybe_compact(session, model, params, emit)
            system = self.prompt_builder.system_prompt(mode=mode, native=bool(native), ultrathink=params.ultrathink)
            history = trim_messages(self._outgoing(session), max(4000, params.num_ctx * 3))
            messages = self.prompt_builder.assemble(system, history)
            self._ultrathink_note(messages, params)
            assistant_text = ""
            native_calls: list[dict[str, Any]] = []
            started = time.perf_counter()
            first_at: float | None = None
            thinking_seen = False
            finish_reason = ""
            try:
                async for chunk in self.backend.stream_chat(
                    model, messages, params,
                    tools=self.registry.native_schemas() if native else None,
                    native_tools=bool(native),
                ):
                    self._check_cancel(cancel)
                    if chunk.reasoning:
                        first_at = first_at or time.perf_counter()
                        reasoning_chars += len(chunk.reasoning)
                        if len(reasoning_text) < 400:
                            reasoning_text.append(chunk.reasoning)
                        if not thinking_seen:
                            thinking_seen = True
                            emit({"type": "thinking", "step": step})
                        emit({"type": "thinking_text", "text": chunk.reasoning})
                    if chunk.content:
                        first_at = first_at or time.perf_counter()
                        assistant_text += chunk.content
                        emit({"type": "token", "text": chunk.content})
                    if chunk.tool_progress:
                        args_chars = max(args_chars, int(chunk.tool_progress.get("chars") or 0))
                        emit({"type": "tool_stream", "name": chunk.tool_progress.get("name", ""),
                              "chars": chunk.tool_progress.get("chars", 0),
                              "tail": chunk.tool_progress.get("tail", ""),
                              "content_tail": chunk.tool_progress.get("content_tail", "")})
                    if chunk.tool_calls:
                        native_calls.extend(chunk.tool_calls)
                    if chunk.done:
                        finish_reason = chunk.finish or ""
                        if chunk.usage:
                            self.last_usage = {k: v for k, v in chunk.usage.items() if v is not None}
                            # LM Studio reports completion_tokens; Ollama reports eval_count
                            comp = int(self.last_usage.get("eval_count")
                                       or self.last_usage.get("completion_tokens") or 0)
                            total_tokens += comp
                            eval_count += comp
                            eval_ns += int(self.last_usage.get("eval_duration_ns") or 0)
                        break
            except BackendError as exc:
                result.error = str(exc)
                emit({"type": "error", "message": str(exc), "kind": "backend"})
                session.add_message("assistant", f"[backend error] {exc}")
                result.reasoning = "".join(reasoning_text)[:20000]
                self._finish(session, result, final_text, gen_time, total_tokens, eval_count, eval_ns,
                             turn_ttft, reasoning_chars, args_chars)
                return result
            ended = time.perf_counter()
            if first_at is not None:
                # decode-only rate: exclude model load, queue and prompt prefill
                gen_time += ended - first_at
                if turn_ttft is None:
                    turn_ttft = first_at - started

            parsed: list[ParsedCall] = []
            if native and native_calls:
                parsed = self.parser.from_native(native_calls)
            elif native is False:
                report = self.parser.from_text(assistant_text)
                parsed = report.calls
                if report.had_malformed and not parsed and retries < self.config.agent.max_parse_retries:
                    retries += 1
                    session.add_message("assistant", assistant_text)
                    session.add_message("user", CORRECTION_MSG.format(reason=report.malformed_reason))
                    emit({"type": "retry", "reason": report.malformed_reason, "attempt": retries})
                    continue

            if not parsed and not assistant_text.strip() and reasoning_text:
                # thinking-only turns (strict reasoning models): the thoughts ARE the answer
                assistant_text = "".join(reasoning_text)
                emit({"type": "token", "text": assistant_text})

            if not parsed and not assistant_text.strip() and retries < self.config.agent.max_parse_retries:
                # small models sometimes answer with silence — nudge once
                retries += 1
                why = ("your reply was empty; summarize the tool results for the user"
                       if result.calls else "your reply was empty; answer the user's request, using a tool if needed")
                session.add_message("assistant", "")
                session.add_message("user", CORRECTION_MSG.format(reason=why))
                emit({"type": "retry", "reason": "empty model reply", "attempt": retries})
                continue

            if not parsed:
                final_text = self._clean_final(assistant_text)
                session.add_message("assistant", final_text)
                result.reasoning = "".join(reasoning_text)[:20000]
                self._finish(session, result, final_text, gen_time, total_tokens, eval_count, eval_ns,
                             turn_ttft, reasoning_chars, args_chars)
                emit({"type": "turn_end", **self._result_payload(result, final_text)})
                return result

            if assistant_text.strip():
                emit({"type": "assistant_note", "text": assistant_text.strip()})
            extra: dict[str, Any] = {}
            if native and parsed:
                # strict chat templates (gpt-oss) require the assistant turn to
                # declare its tool calls before the tool results follow
                for i, c in enumerate(parsed):
                    if not c.id:
                        c.id = f"call_{i}"
                extra["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                    for c in parsed
                ]
            session.add_message("assistant", assistant_text.strip(), **extra)

            for call in parsed:
                self._check_cancel(cancel)
                await self._handle_call(call, session, ctx, mode, prompt_fn, emit, result, model,
                                      truncated=finish_reason == "length")

            if step == self.config.agent.max_steps:
                break
        if not final_text.strip():
            # never end a tool-heavy turn in silence: one last tool-free summary pass
            final_text = await self._force_final(session, model, params, emit, cancel)
        result.reasoning = "".join(reasoning_text)[:20000]
        self._finish(session, result, final_text, gen_time, total_tokens, eval_count, eval_ns,
                     turn_ttft, reasoning_chars, args_chars)
        emit({"type": "turn_end", **self._result_payload(result, final_text)})
        return result

    async def _maybe_compact(
        self, session: Session, model: str, params: Any, emit: Callable[[dict[str, Any]], None],
    ) -> None:
        """Summarize old turns with the model itself when the context fills up."""
        if not self.config.agent.compact or len(session.messages) < 14:
            return
        est = sum(len(str(m.get("content", ""))) for m in session.messages) // 4
        budget = max(2048, int(params.num_ctx))
        if est < budget * self.config.agent.compact_ratio:
            return
        keep = 8
        head, tail = session.messages[:-keep], session.messages[-keep:]
        transcript = "\n".join(
            f"{m.get('role')}: {str(m.get('content', ''))[:400]}"
            for m in head if str(m.get("content", "")).strip()
        )
        try:
            summary, _ = await self.backend.generate(model, [
                {"role": "system", "content": "You compress conversations. Reply with a dense bullet summary only."},
                {"role": "user", "content":
                    "Summarize this conversation, preserving: user facts & preferences, decisions, "
                    "file paths touched, tool outcomes, and open threads. Be dense; no preamble.\n\n"
                    + transcript[:12000]},
            ], params)
        except Exception:  # noqa: BLE001 — compaction is best-effort, never fatal
            return
        if not summary or not summary.strip():
            return
        before = len(session.messages)
        session.messages[:] = [
            {"role": "system", "compacted": True, "ts": head[0].get("ts", time.time()),
             "content": "## Earlier conversation (compacted by the model)\n" + summary.strip()[:4000]},
            *tail,
        ]
        emit({"type": "compaction", "before": before, "after": len(session.messages),
              "est_tokens": est})

    async def _force_final(
        self, session: Session, model: str, params: Any,
        emit: Callable[[dict[str, Any]], None], cancel: asyncio.Event | None,
    ) -> str:
        session.add_message(
            "user",
            "STEP LIMIT reached — do NOT call any more tools. Reply to the user now in plain "
            "text: summarize what you already found and what (if anything) is still missing.",
        )
        system = self.prompt_builder.system_prompt(mode=self.policy.mode, native=False, ultrathink=self.config.generation.ultrathink)
        history = trim_messages(self._outgoing(session), max(4000, params.num_ctx * 3))
        messages = self.prompt_builder.assemble(system, history)
        text = ""
        try:
            async for chunk in self.backend.stream_chat(
                model, messages, params, tools=None, native_tools=False
            ):
                self._check_cancel(cancel)
                if chunk.content:
                    text += chunk.content
                    emit({"type": "token", "text": chunk.content})
                if chunk.done:
                    break
        except BackendError as exc:
            emit({"type": "error", "message": str(exc), "kind": "backend"})
            return ""
        text = self._clean_final(text)
        if text:
            session.add_message("assistant", text)
        return text

    # ------------------------------------------------------------- one call
    async def _handle_call(
        self,
        call: ParsedCall,
        session: Session,
        ctx: ToolContext,
        mode: PermissionMode,
        prompt_fn: PromptFn | None,
        emit: Callable[[dict[str, Any]], None],
        result: TurnResult,
        model: str,
        truncated: bool = False,
    ) -> None:
        started = time.perf_counter()
        vres = self.validator.validate(call)
        decision: Decision
        if not vres.ok:
            if truncated:
                note = ("your reply hit the max_tokens limit and the tool call was CUT OFF mid-JSON "
                        "(required parameters never arrived). Do NOT emit the same huge call again: "
                        "write the file in pieces — first write_file with roughly the first half, "
                        "then a second write_file with append=true for the rest — or answer in text.")
            else:
                note = f"invalid tool call: {vres.error_text()}"
            key = (call.name, note)
            cnt = self._invalid_counts.get(key, 0) + 1
            self._invalid_counts[key] = cnt
            if cnt >= 2:
                note += (f" — you have made THIS EXACT mistake {cnt} times. Do NOT emit this tool "
                         "call again unchanged. Fix the parameter names per the schema above, or "
                         "stop using tools and answer the user in plain text.")
            self._audit(call.name, call.args, "invalid", False, started, mode, model, error=note)
            session.add_message("assistant", call.raw or f"[{call.name}]")
            session.add_message("user", CORRECTION_MSG.format(reason=note))
            emit({"type": "retry", "reason": note, "attempt": 0})
            return

        name, args = vres.name, vres.args
        cache_key = f"{name}:{json.dumps(args, sort_keys=True, default=str)[:200]}"
        cache = getattr(self, "_turn_cache", None)
        if cache is not None and cache_key in cache:
            note = (cache[cache_key][:2500]
                    + "\n(duplicate of an earlier identical call — result cached; "
                      "stop repeating tool calls and answer the user now)")
            session.add_message("tool", note, name=name, tool_call_id=call.id)
            result.calls.append({"name": name, "status": "cached", "approval": "n/a",
                                 "args": args, "risk": 0})
            emit({"type": "tool_result", "name": name, "ok": True, "cached": True,
                  "output": "(cached — identical call already ran this turn)", "call_id": call.id})
            return
        decision = self.policy.evaluate(name, args, user_requested_memory=ctx.user_requested_memory)
        gcfg = self.config.guard
        if (gcfg.enabled and decision.allowed and not decision.requires_approval
                and mode in (PermissionMode.DANGEROUS, PermissionMode.FULL_ACCESS)
                and guard_mod.needs_assessment(name, args)):
            verdict = await guard_mod.assess(gcfg, name, args)
            if verdict.flagged:
                decision = Decision(True, True, max(decision.risk, 6),
                                    (*decision.reasons, verdict.reason(name)))
            elif verdict.error:
                decision = Decision(decision.allowed, decision.requires_approval,
                                    decision.risk, (*decision.reasons, verdict.reason(name)))
        preview = self.registry.preview(name, args) if (decision.requires_approval or decision.allowed) else ""
        plan = {
            "type": "tool_plan", "name": name, "args": args, "risk": decision.risk,
            "level": decision.level, "mode": mode.value, "approval": decision.requires_approval,
            "allowed": decision.allowed, "preview": preview, "call_id": call.id,
        }
        emit(plan)
        if not decision.allowed:
            reason = "; ".join(decision.reasons)
            body = f"BLOCKED BY PERMISSION ENGINE: {reason}"
            self._audit(name, args, "blocked", False, started, mode, model, error=reason)
            result.calls.append({"name": name, "status": "blocked", "approval": "blocked", "args": args, "risk": decision.risk})
            session.add_message("assistant", f"[attempted {name}]")
            session.add_message("tool", body, name=name, tool_call_id=call.id)
            emit({"type": "tool_result", "name": name, "ok": False, "blocked": True, "output": body, "call_id": call.id})
            return

        outcome = ApprovalOutcome.APPROVED
        if decision.requires_approval:
            outcome = await self.approvals.decide(name, args, decision, prompt_fn, preview=preview)
        if not outcome.approved:
            self._audit(name, args, "denied", False, started, mode, model, error="user denied")
            result.calls.append({"name": name, "status": "denied", "approval": "denied", "args": args, "risk": decision.risk})
            session.add_message("assistant", f"[asked to run {name}]")
            session.add_message("tool", "The user DENIED this tool call. Do not retry it; adapt your answer.", name=name, tool_call_id=call.id)
            emit({"type": "tool_result", "name": name, "ok": False, "denied": True, "output": "denied", "call_id": call.id})
            return

        try:
            tool_result: ToolResult = await asyncio.wait_for(
                self.registry.execute(name, args, ctx), timeout=self.config.agent.tool_timeout
            )
        except TimeoutError:
            tool_result = ToolResult(False, "", f"tool timed out after {self.config.agent.tool_timeout:.0f}s")
        except AgentCancelled:
            raise
        duration = (time.perf_counter() - started) * 1000
        out_text = tool_result.as_message()
        if self.soul is not None and name.startswith("soul_"):
            emit({"type": "memory_activity", "tool": name, "ok": tool_result.ok})
        if cache is not None:
            cache[cache_key] = out_text[:2500]
        session.add_message("tool", out_text[:3000], name=name, tool_call_id=call.id)
        session.record_tool(name, args, "ok" if tool_result.ok else "error", outcome.value, duration, tool_result.error)
        result.calls.append({"name": name, "status": "ok" if tool_result.ok else "error", "approval": outcome.value, "args": args, "risk": decision.risk, "duration_ms": duration})
        self._audit(name, args, outcome.value, tool_result.ok, started, mode, model, error=tool_result.error)
        emit({"type": "tool_result", "name": name, "ok": tool_result.ok, "approval": outcome.value, "output": out_text[:4000], "duration_ms": duration, "call_id": call.id, "file": tool_result.meta or None})

    # ------------------------------------------------------------- plumbing

    @staticmethod
    def _ultrathink_note(messages: list[dict[str, Any]], params: Any) -> None:
        """Recency beats distance: the deliberation demand lands on the wire as
        a suffix of the last user message, never in the stored session."""
        if not getattr(params, "ultrathink", False):
            return
        from .prompts import ULTRATHINK_NOTE
        for m in reversed(messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                m["content"] = str(m["content"]) + "\n\n" + ULTRATHINK_NOTE
                return

    def _outgoing(self, session: Session) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        openai_shape = self.backend.name.value in ("lmstudio", "mock")
        for m in session.messages:
            role = m.get("role")
            content = str(m.get("content", ""))
            if role == "tool":
                if openai_shape:
                    anchor = m.get("tool_call_id") or "call_0"
                    out.append({"role": "tool", "content": content,
                                "tool_call_id": anchor, "name": m.get("name") or "tool"})
                    # repair history written before tool_calls were persisted:
                    # synthesize the assistant anchor the template demands
                    prev = out[-2] if len(out) >= 2 else None
                    declared = prev is not None and prev.get("role") == "assistant" and any(
                        tc.get("id") == anchor for tc in prev.get("tool_calls") or [])
                    if not declared:
                        if prev is not None and prev.get("role") == "assistant":
                            prev.setdefault("tool_calls", []).append(
                                {"id": anchor, "type": "function",
                                 "function": {"name": m.get("name") or "tool", "arguments": "{}"}})
                        else:
                            out.insert(len(out) - 1, {
                                "role": "assistant", "content": "",
                                "tool_calls": [{"id": anchor, "type": "function",
                                                "function": {"name": m.get("name") or "tool",
                                                             "arguments": "{}"}}]})
                else:
                    out.append({"role": "tool", "content": content, "tool_name": m.get("name")})
            elif role in ("user", "assistant", "system"):
                if role == "assistant" and not content.strip() and not m.get("tool_calls"):
                    continue  # empty assistant turns break some chat templates
                entry = {"role": role, "content": content}
                if role == "assistant" and m.get("tool_calls"):
                    entry["tool_calls"] = m["tool_calls"]
                out.append(entry)
        return out[-80:]

    def _status_provider(self, model: str):
        def provider() -> dict[str, Any]:
            info: dict[str, Any] = {"model": model, "protocol_backend": self.backend.display_name}
            if self.last_usage:
                info["last_prompt_tokens_est"] = self.last_usage.get("prompt_eval_count")
            return info

        return provider

    def _default_model(self) -> str:
        section = getattr(self.config, self.backend.name.value, None)
        dm = getattr(section, "default_model", None) if section is not None else None
        if dm:
            return dm
        return "mock-tool-7b" if self.backend.name.value == "mock" else ""

    async def _auto_model(self) -> str:
        """No default configured: pick the best model actually present on the server."""
        from ..backends.base import BackendError

        try:
            models = sorted(await self.backend.list_models(), key=lambda m: m.name)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(
                f"{self.backend.name.value}: no models available and none configured ({exc})"
            ) from exc
        if not models:
            raise BackendError(
                f"{self.backend.name.value}: no models installed — pull one first "
                "(Ctrl+B in the TUI, or `ollama pull …`)"
            )
        best = next((m for m in models if m.tool_capable), models[0])
        section = getattr(self.config, self.backend.name.value, None)
        if section is not None:
            section.default_model = best.name  # cache for the rest of this process
        return best.name

    def _check_cancel(self, cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise AgentCancelled()

    def _clean_final(self, text: str) -> str:
        if self.config.agent.native_tools == "auto":
            report = self.parser.from_text(text)
            if report.leftover_text and not report.calls:
                return report.leftover_text.strip()
        return text.strip()

    def _finish(
        self, session: Session, result: TurnResult, text: str, gen_time: float, tokens: int,
        eval_count: int = 0, eval_ns: int = 0, ttft: float | None = None, reasoning_chars: int = 0,
        args_chars: int = 0,
    ) -> None:
        result.text = text
        est_all = max(tokens, (len(text) + reasoning_chars + args_chars) // 4)  # reasoning+args ARE generation
        result.tokens = est_all
        if eval_ns > 0 and eval_count > 0:
            # backend-reported decode time is exact (ollama; its eval_count
            # already includes thinking tokens)
            result.tokens = max(eval_count, est_all)
            result.tokens_per_s = round(result.tokens / (eval_ns / 1e9), 1)
        elif gen_time > 0:
            result.tokens_per_s = round(max(1, est_all) / gen_time, 1)
        if ttft is not None:
            result.ttft_s = round(ttft, 2)
        self.last_usage["tokens_per_s"] = result.tokens_per_s

    def _result_payload(self, result: TurnResult, text: str) -> dict[str, Any]:
        return {"text": text, "steps": result.steps, "calls": result.calls,
                "tokens_per_s": result.tokens_per_s, "ttft_s": result.ttft_s,
                "reasoning": result.reasoning[:6000], "tokens": result.tokens,
                "usage": result.usage}

    def _audit(
        self, tool: str, args: dict, approval: str, ok: bool, started: float,
        mode: PermissionMode, model: str, error: str | None = None,
    ) -> None:
        self.audit.log(
            mode=mode.value, tool=tool, args=args, approval=approval, success=ok,
            duration_ms=(time.perf_counter() - started) * 1000,
            backend=self.backend.name.value, model=model, error=error,
        )
