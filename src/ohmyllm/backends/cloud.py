"""Cloud backend: hosted OpenAI-compatible + Anthropic chat endpoints.

One backend serves every provider in the registry (config.cloud.provider
picks the active one). Messages travel in the OpenAI shape the runtime
already builds; for Anthropic we translate to/from the messages API inside
this class so the rest of the app never learns about wire formats.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config.models import BackendName, GenerationSettings
from ..providers import key_for, resolve
from .base import BackendError, BaseBackend, ChatChunk, ModelInfo, StartResult
from .thinksplit import ThinkSplitter

ANTHROPIC_VERSION = "2023-06-01"


class CloudBackend(BaseBackend):
    name = BackendName.CLOUD
    display_name = "Cloud"

    # ------------------------------------------------------------- wiring
    def _active(self) -> tuple[str, str, str, str, str]:
        """(pid, base_url, kind, key, display_name) for the chosen provider."""
        cl = self.config.cloud
        pid = cl.provider
        if not pid:
            raise BackendError("no provider connected — open /providers (TUI) or the Providers tab (GUI)")
        r = resolve(pid, cl.bases, cl.customs)
        if r is None:
            raise BackendError(f"unknown provider '{pid}' — connect one in /providers")
        base, kind, label = r
        key = key_for(pid, cl.keys)
        if not base:
            raise BackendError(f"{label}: no base URL — set it in /providers (custom provider)")
        return pid, base.rstrip("/"), kind, key, label

    def _headers(self, kind: str, key: str) -> dict[str, str]:
        if kind == "anthropic":
            return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        return {"Authorization": f"Bearer {key}"}

    def is_installed(self) -> bool:
        cl = self.config.cloud
        try:
            _, _, _, key, _ = self._active()
        except BackendError:
            return any(key_for(p, cl.keys) for p in list(cl.keys) + [f"custom-{i}" for i in cl.customs])
        return bool(key)

    async def is_running(self) -> bool:
        ok, _ = await self.health()
        return ok

    async def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            pid, base, kind, key, label = self._active()
        except BackendError as exc:
            return False, {"error": str(exc)}
        if not key:
            return False, {"error": f"{label}: no API key — /providers or OMLLM_KEY_{pid.upper()}"}
        url = f"{base}/models"
        try:
            async with httpx.AsyncClient(timeout=8.0, transport=self._transport) as c:
                r = await c.get(url, headers=self._headers(kind, key))
                if r.status_code == 200:
                    try:
                        n = len(r.json().get("data", []))
                    except Exception:  # noqa: BLE001
                        n = -1
                    return True, {"provider": label, "models": n}
                static = self._static_models(pid, label)
                if static:
                    return True, {"provider": label, "models": len(static), "note": "static model list"}
                return False, {"error": f"HTTP {r.status_code}: {r.text[:160]}"}
        except Exception as exc:  # noqa: BLE001
            static = self._static_models(pid, label)
            if static:
                return True, {"provider": pid, "models": len(static), "note": "static model list"}
            return False, {"error": str(exc)}

    async def ensure_running(self) -> StartResult:
        try:
            _, _, _, key, label = self._active()
        except BackendError as exc:
            return StartResult(ok=False, message=str(exc))
        if not key:
            return StartResult(
                ok=False,
                message=f"{label}: no API key configured.",
                instructions=[f"Open /providers (TUI) or the Providers tab (GUI) and paste your {label} key,",
                              f"or export OMLLM_KEY_{self.config.cloud.provider.upper()}."],
            )
        return StartResult(ok=True, was_already_running=True, message=f"{label} key present")

    # --------------------------------------------------------------- models
    def _static_models(self, pid: str, label: str) -> list[ModelInfo]:
        raw = self.config.cloud.model_lists.get(pid, "")
        out: list[ModelInfo] = []
        for mid in (x.strip() for x in raw.split(",")):
            if not mid:
                continue
            m = ModelInfo(name=mid, backend=BackendName.CLOUD, tool_capable=True)
            m.apply_name_heuristics()
            m.notes.append(label)
            out.append(m)
        return out

    async def list_models(self) -> list[ModelInfo]:
        pid, base, kind, key, label = self._active()
        if not key:
            raise BackendError(f"{label}: no API key — connect it in /providers")
        static = self._static_models(pid, label)
        if static:
            return static
        async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as c:
            r = await c.get(f"{base}/models", headers=self._headers(kind, key))
            if r.status_code != 200:
                raise BackendError(f"{label}: model list failed ({r.status_code}): {r.text[:200]}")
            data = r.json().get("data") or r.json().get("models") or []
        out: list[ModelInfo] = []
        for row in data:
            mid = str(row.get("id") or row.get("name") or "")
            if not mid:
                continue
            m = ModelInfo(name=mid, backend=BackendName.CLOUD, tool_capable=True)
            m.apply_name_heuristics()
            m.notes.append(label)
            out.append(m)
        return sorted(out, key=lambda m: m.name)

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
        return self._stream(model, messages, params, tools, native_tools)

    async def _stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: GenerationSettings,
        tools: list[dict[str, Any]] | None,
        native_tools: bool,
    ) -> AsyncIterator[ChatChunk]:
        pid, base, kind, key, label = self._active()
        if not key:
            raise BackendError(f"{label}: no API key — connect it in /providers")
        gen = self._gen(model)
        if kind == "anthropic":
            async for ch in self._stream_anthropic(base, key, model, messages, gen, tools, native_tools):
                yield ch
        else:
            async for ch in self._stream_openai(base, key, model, messages, gen, tools, native_tools):
                yield ch

    # ------------------------------------------------------ openai shape
    async def _stream_openai(self, base: str, key: str, model: str,
                             messages: list[dict[str, Any]], gen: GenerationSettings,
                             tools: list[dict[str, Any]] | None,
                             native_tools: bool) -> AsyncIterator[ChatChunk]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": gen.temperature,
            "top_p": gen.top_p,
            "max_tokens": max(gen.max_tokens, 16384) if gen.ultrathink else gen.max_tokens,
        }
        if gen.effort != "off" or gen.ultrathink:
            lvl = "xhigh" if gen.ultrathink else gen.effort
            body["reasoning_effort"] = {"low": "low", "medium": "medium",
                                        "high": "high", "xhigh": "high",
                                        "max": "high"}.get(lvl, "medium")
        if tools and native_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        sp = ThinkSplitter()
        pending: dict[int, dict[str, Any]] = {}
        timeout = httpx.Timeout(gen.timeout, read=None)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as c:
            async with c.stream("POST", f"{base}/chat/completions",
                                headers=self._headers("openai", key), json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise BackendError(f"{model} failed ({resp.status_code}): {detail}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise BackendError(f"{model}: {str(obj['error'])[:200]}")
                    usage = obj.get("usage")
                    for ch0 in obj.get("choices") or []:
                        d = ch0.get("delta") or {}
                        text = d.get("content") or ""
                        rc = d.get("reasoning_content") or d.get("reasoning") or ""
                        if rc:
                            yield ChatChunk(reasoning=rc)
                        if text:
                            for kind0, piece in sp.feed(text):
                                yield ChatChunk(content=piece if kind0 == "content" else "",
                                                reasoning=piece if kind0 == "reasoning" else "")
                        for tc in d.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        fin = ch0.get("finish_reason")
                        if fin:
                            for kind0, piece in sp.flush():
                                yield ChatChunk(content=piece if kind0 == "content" else "",
                                                reasoning=piece if kind0 == "reasoning" else "")
                            calls = [
                                {"id": s["id"] or f"call_{i}", "type": "function",
                                 "function": {"name": s["name"], "arguments": s["args"]}}
                                for i, s in sorted(pending.items()) if s["name"]
                            ]
                            yield ChatChunk(tool_calls=calls or None, done=True,
                                            finish=fin, usage=usage)

    # --------------------------------------------------- anthropic shape
    def _to_anthropic(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system = (system + "\n" + str(m.get("content", ""))).strip()
                continue
            if role == "tool":
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "call_0",
                    "content": str(m.get("content", "")),
                }]})
                continue
            blocks: list[dict[str, Any]] = []
            content = str(m.get("content", ""))
            if content.strip():
                blocks.append({"type": "text", "text": content})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "call_0",
                               "name": fn.get("name") or "tool", "input": args})
            if blocks:
                out.append({"role": "assistant" if role == "assistant" else "user",
                            "content": blocks})
        return system, out

    async def _stream_anthropic(self, base: str, key: str, model: str,
                                messages: list[dict[str, Any]], gen: GenerationSettings,
                                tools: list[dict[str, Any]] | None,
                                native_tools: bool) -> AsyncIterator[ChatChunk]:
        system, msgs = self._to_anthropic(messages)
        body: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "stream": True,
            "max_tokens": max(gen.max_tokens, 4096),
            "temperature": gen.temperature,
        }
        if system:
            body["system"] = system
        if gen.thinking or gen.ultrathink:
            budget = min(max(int(gen.thinking_budget or 4096), 1024), max(gen.max_tokens, 4096) - 1024)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if tools and native_tools:
            body["tools"] = [
                {"name": t.get("function", {}).get("name", "tool"),
                 "description": t.get("function", {}).get("description", ""),
                 "input_schema": t.get("function", {}).get("parameters")
                 or {"type": "object", "properties": {}}}
                for t in tools
            ]
        sp = ThinkSplitter()
        blocks: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        timeout = httpx.Timeout(gen.timeout, read=None)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as c:
            async with c.stream("POST", f"{base}/messages",
                                headers=self._headers("anthropic", key), json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise BackendError(f"{model} failed ({resp.status_code}): {detail}")
                event = ""
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if event == "error":
                        raise BackendError(f"{model}: {str(obj.get('error'))[:200]}")
                    if event == "message_start":
                        usage = dict((obj.get("message") or {}).get("usage") or {})
                    elif event == "content_block_start":
                        blk = obj.get("content_block") or {}
                        blocks[int(obj.get("index", 0))] = {
                            "type": blk.get("type"), "id": blk.get("id", ""),
                            "name": blk.get("name", ""), "text": "", "json": ""}
                    elif event == "content_block_delta":
                        d = obj.get("delta") or {}
                        slot = blocks.setdefault(int(obj.get("index", 0)),
                                                 {"type": "text", "id": "", "name": "", "text": "", "json": ""})
                        dt = d.get("type")
                        if dt == "text_delta" and d.get("text"):
                            for kind0, piece in sp.feed(d["text"]):
                                yield ChatChunk(content=piece if kind0 == "content" else "",
                                                reasoning=piece if kind0 == "reasoning" else "")
                        elif dt == "thinking_delta" and d.get("thinking"):
                            yield ChatChunk(reasoning=d["thinking"])
                        elif dt == "input_json_delta":
                            slot["json"] += d.get("partial_json", "")
                    elif event == "message_delta":
                        u = obj.get("usage") or {}
                        usage = {**(usage or {}), **u}
                        stop = (obj.get("delta") or {}).get("stop_reason")
                        if stop:
                            for kind0, piece in sp.flush():
                                yield ChatChunk(content=piece if kind0 == "content" else "",
                                                reasoning=piece if kind0 == "reasoning" else "")
                            calls = [
                                {"id": s["id"] or f"call_{i}", "type": "function",
                                 "function": {"name": s["name"], "arguments": s["json"] or "{}"}}
                                for i, s in sorted(blocks.items()) if s.get("type") == "tool_use" and s.get("name")
                            ]
                            finish = {"tool_use": "tool_calls", "max_tokens": "length",
                                      "stop_sequence": "stop", "end_turn": "stop"}.get(stop, stop)
                            yield ChatChunk(
                                tool_calls=calls or None, done=True, finish=finish,
                                usage={"prompt_tokens": (usage or {}).get("input_tokens", 0),
                                       "completion_tokens": (usage or {}).get("output_tokens", 0),
                                       "total_tokens": (usage or {}).get("input_tokens", 0)
                                       + (usage or {}).get("output_tokens", 0)})

    # ---------------------------------------------------------------- pull
    async def pull(self, name: str, progress: Any = None) -> None:
        raise BackendError("cloud models are hosted — nothing to pull; "
                           "pick one with Ctrl+M (models list comes from the provider)")
