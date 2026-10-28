"""Cloud backend: hosted OpenAI-compatible + Anthropic chat endpoints.

One backend serves every provider in the registry (config.cloud.provider
picks the active one). Messages travel in the OpenAI shape the runtime
already builds; for Anthropic we translate to/from the messages API inside
this class so the rest of the app never learns about wire formats.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config.models import BackendName, GenerationSettings
from ..providers import key_for, resolve
from .base import BackendError, BaseBackend, ChatChunk, ModelInfo, StartResult
from .thinksplit import ThinkSplitter

ANTHROPIC_VERSION = "2023-06-01"


def _ctx_from_row(row: dict) -> int | None:
    """Context window from a provider's /models entry: OpenRouter says
    context_length, Anthropic max_input_tokens, others context."""
    for field in ("context_length", "max_input_tokens", "max_context_length", "context"):
        v = row.get(field)
        if isinstance(v, (int, float)) and 512 <= v <= 200_000_000:
            return int(v)
    return None


def _anth_usage(usage: dict | None) -> dict | None:
    """Map anthropic token counts; None when the provider sent nothing —
    synthesized zeros would clobber the runtime's last_usage."""
    if not usage:
        return None
    try:
        pin = int(usage.get("input_tokens") or 0)
        pout = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if not (pin or pout):
        return None
    return {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout}


class CloudBackend(BaseBackend):
    name = BackendName.CLOUD
    display_name = "Cloud"

    # ------------------------------------------------------------- wiring
    def _active(self) -> tuple[str, str, str, str, str]:
        """(pid, base_url, kind, key, display_name) for the chosen provider."""
        cl = self.config.cloud
        pid = cl.provider
        if not pid:
            # a key was pasted but the provider never explicitly activated:
            # with exactly one connected provider there is no ambiguity
            connected = [k for k, v in cl.keys.items() if v and resolve(k, cl.bases, cl.customs)]
            if len(connected) == 1:
                pid = connected[0]
            else:
                raise BackendError("no provider connected — open /providers (TUI) or the Providers tab (GUI)")
        r = resolve(pid, cl.bases, cl.customs)
        if r is None:
            raise BackendError(f"unknown provider '{pid}' — connect one in /providers")
        base, kind, label = r
        key = key_for(pid, cl.keys)
        if not base:
            raise BackendError(f"{label}: no base URL — set it in /providers (custom provider)")
        from urllib.parse import urlparse

        if urlparse(base).scheme not in ("http", "https"):
            raise BackendError(f"{label}: base URL must be http(s) — got {base!r}")
        return pid, base.rstrip("/"), kind, key, label

    def _headers(self, kind: str, key: str) -> dict[str, str]:
        if kind == "anthropic":
            return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        return {"Authorization": f"Bearer {key}"}

    def is_installed(self) -> bool:
        from ..providers import PROVIDERS

        cl = self.config.cloud
        pids = {p.pid for p in PROVIDERS} | set(cl.keys) | set(cl.customs)
        return any(key_for(pid, cl.keys) for pid in pids)

    async def is_running(self) -> bool:
        ok, _ = await self.health()
        return ok

    async def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            pid, base, kind, key, label = self._active()
        except BackendError as exc:
            return False, {"error": str(exc)}
        if not key:
            return False, {"error": f"{label}: no API key — /providers or "
                                   f"OMLLM_KEY_{pid.upper().replace('-', '_')}"}
        static = self._static_models(pid, label)
        if static:  # user declared the catalog — health must agree with the picker
            return True, {"provider": label, "models": len(static), "note": "static model list"}
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
                              f"or export OMLLM_KEY_{self.config.cloud.provider.upper().replace('-', '_')}."],
            )
        return StartResult(ok=True, was_already_running=True, message=f"{label} key present")

    # --------------------------------------------------------------- models
    def _static_models(self, pid: str, label: str) -> list[ModelInfo]:
        raw = self.config.cloud.model_lists.get(pid, "")
        out: list[ModelInfo] = []
        for mid in dict.fromkeys(x.strip() for x in raw.split(",")):
            if not mid:
                continue
            ctx = None
            # gateways with no /models listing can still declare the window:
            # "qwen3.8-flash:256k" or "long-context-model:1m"
            cm = re.search(r":(\d+(?:\.\d+)?)(k|m)$", mid.lower())
            if cm:
                mid = mid[: mid.rfind(":")]
                ctx = int(float(cm.group(1)) * (1024 if cm.group(2) == "k" else 1048576))
            m = ModelInfo(name=mid, backend=BackendName.CLOUD, tool_capable=True)
            m.ctx_len = ctx
            m.apply_name_heuristics()
            m.notes.append(label)
            out.append(m)
        self._note_ctx(out)
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
            m.ctx_len = _ctx_from_row(row)
            m.apply_name_heuristics()
            m.notes.append(label)
            out.append(m)
        self._note_ctx(out)
        return sorted(out, key=lambda m: m.name)

    def _ctx_map(self) -> dict[str, int]:
        cached: dict[str, int] = self.__dict__.setdefault("_ctx_map_", {})
        return cached

    def _note_ctx(self, models: list[ModelInfo]) -> None:
        """Remember each model's real context window (provider metadata or
        name heuristic) so the ctx% bar and auto-compaction stop using the
        local llama num_ctx setting as the cloud denominator."""
        for m in models:
            if m.ctx_len:
                self._ctx_map()[m.name] = int(m.ctx_len)

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
        # caller params ARE the effective settings (runtime applies
        # settings_for; vision.py sends a bespoke deterministic probe)
        gen = params
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
        by_id: dict[str, int] = {}
        last_key = -1
        usage: dict[str, Any] | None = None
        ended = False
        end_reason: str | None = None
        timeout = httpx.Timeout(gen.timeout, read=None)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as c:
            async with c.stream("POST", f"{base}/chat/completions",
                                headers=self._headers("openai", key), json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise BackendError(f"{model} failed ({resp.status_code}): {detail}")
                if "text/event-stream" not in (resp.headers.get("content-type") or ""):
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise BackendError(f"{model}: expected SSE, got {detail}")
                try:
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
                        if not isinstance(obj, dict):
                            continue
                        if obj.get("error"):
                            raise BackendError(f"{model}: {str(obj['error'])[:200]}")
                        if isinstance(obj.get("usage"), dict) and obj["usage"]:
                            usage = obj["usage"]
                        choices = obj.get("choices")
                        if not isinstance(choices, list):
                            continue
                        for ch0 in choices:
                            if not isinstance(ch0, dict):
                                continue
                            d0 = ch0.get("delta")
                            d = d0 if isinstance(d0, dict) else {}
                            rc = d.get("reasoning_content") or d.get("reasoning") or ""
                            if isinstance(rc, str) and rc:
                                yield ChatChunk(reasoning=rc)
                            text = d.get("content") or ""
                            if isinstance(text, (int, float)):
                                text = str(text)
                            if isinstance(text, str) and text:
                                for kind0, piece in sp.feed(text):
                                    yield ChatChunk(content=piece if kind0 == "content" else "",
                                                    reasoning=piece if kind0 == "reasoning" else "")
                            tcs = d.get("tool_calls")
                            for tc in tcs if isinstance(tcs, list) else []:
                                if not isinstance(tc, dict):
                                    continue
                                tid = str(tc.get("id") or "")
                                if tid and tid in by_id:
                                    slot = pending[by_id[tid]]
                                elif tid:
                                    last_key = len(pending)
                                    by_id[tid] = last_key
                                    slot = pending[last_key] = {"id": tid, "name": "", "args": ""}
                                elif last_key >= 0:
                                    slot = pending[last_key]  # continuation without id/index
                                else:
                                    try:
                                        idx = int(tc.get("index", 0))
                                    except (TypeError, ValueError):
                                        idx = 0
                                    last_key = idx
                                    slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                                fn0 = tc.get("function")
                                fn = fn0 if isinstance(fn0, dict) else {}
                                if fn.get("name"):
                                    slot["name"] = str(fn["name"])
                                args = fn.get("arguments")
                                if isinstance(args, str):
                                    slot["args"] += args
                            fin = ch0.get("finish_reason")
                            if fin:
                                ended = True
                                end_reason = str(fin)
                except BackendError:
                    raise
                except (httpx.HTTPError, UnicodeDecodeError) as exc:
                    raise BackendError(f"{model}: stream broke: {exc}") from exc
        # terminal chunk on finish_reason OR early stream end: pending tool args
        # and the splitter tail must never vanish silently
        for kind0, piece in sp.flush():
            yield ChatChunk(content=piece if kind0 == "content" else "",
                            reasoning=piece if kind0 == "reasoning" else "")
        calls = [
            {"id": slot["id"] or f"call_{i}", "type": "function",
             "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}}
            for i, slot in sorted(pending.items()) if slot["name"]
        ]
        yield ChatChunk(tool_calls=calls or None, done=True,
                        finish=end_reason or ("stream_ended" if not ended else None),
                        usage=usage)

    # --------------------------------------------------- anthropic shape
    def _to_anthropic(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        out: list[dict[str, Any]] = []
        for n, m in enumerate(messages):
            role = m.get("role")
            raw0 = m.get("content")
            if role == "system":
                if isinstance(raw0, list):  # parts-list system: join the texts
                    raw0 = "\n".join(str(pt.get("text", "")) for pt in raw0
                                      if isinstance(pt, dict) and pt.get("type") == "text")
                system = (system + "\n" + str(raw0 or "")).strip()
                continue
            if role == "tool":
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or f"call_{n}",
                    "content": str(raw0 or ""),
                }]})
                continue
            blocks: list[dict[str, Any]] = []
            raw = m.get("content", "")
            had_parts = False
            if isinstance(raw, list):
                # OpenAI parts list (ctrl+V vision paste): fold into anthropic
                # text/image blocks — base64 data URLs become source blocks
                import base64 as _b64

                for part in raw:
                    if not isinstance(part, dict):
                        continue
                    had_parts = True
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": str(part.get("text") or "")})
                    elif part.get("type") == "image_url":
                        url = str((part.get("image_url") or {}).get("url", ""))
                        from urllib.parse import urlparse as _up

                        if url.startswith("data:"):
                            try:
                                head, _, b64 = url.partition(",")
                                media = head[5:].split(";")[0] or "image/png"
                                _b64.b64decode(b64, validate=True)  # reject junk outright
                            except Exception:  # noqa: BLE001
                                blocks.append({"type": "text", "text": "[undecodable image]"})
                                continue
                            if media not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                                media = "image/png"
                            blocks.append({"type": "image", "source": {
                                "type": "base64", "media_type": media, "data": b64}})
                        elif _up(url).scheme in ("http", "https"):
                            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                content = ""
            else:
                content = str(raw or "")
            if content.strip():
                blocks.append({"type": "text", "text": content})
            if not blocks and had_parts:
                blocks.append({"type": "text", "text": "[unsupported content]"})
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
            body["temperature"] = 1  # the API rejects thinking requests at any other temperature
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
        saw_stop = False
        timeout = httpx.Timeout(gen.timeout, read=None)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as c:
            async with c.stream("POST", f"{base}/messages",
                                headers=self._headers("anthropic", key), json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise BackendError(f"{model} failed ({resp.status_code}): {detail}")
                event = ""
                try:
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
                        if not isinstance(obj, dict):
                            obj = {"_raw": obj}
                        if event == "error":
                            err = obj.get("error", obj.get("_raw"))
                            raise BackendError(f"{model}: {str(err)[:200]}")
                        if event == "message_start":
                            msg0 = obj.get("message")
                            u0 = msg0.get("usage") if isinstance(msg0, dict) else None
                            if isinstance(u0, dict):
                                usage = dict(u0)
                        elif event == "content_block_start":
                            try:
                                bidx = int(obj.get("index", 0))
                            except (TypeError, ValueError):
                                bidx = len(blocks)
                            blk = obj.get("content_block")
                            blk = blk if isinstance(blk, dict) else {}
                            prev = blocks.get(bidx)
                            if prev and prev.get("json"):
                                blk_id = blk.get("id") or prev["id"]
                                prev.update({"type": blk.get("type") or prev["type"],
                                             "id": blk_id, "name": blk.get("name") or prev["name"]})
                            else:
                                blocks[bidx] = {"type": blk.get("type"), "id": str(blk.get("id", "")),
                                                "name": str(blk.get("name", "")), "text": "", "json": ""}
                        elif event == "content_block_delta":
                            try:
                                didx = int(obj.get("index", 0))
                            except (TypeError, ValueError):
                                didx = 0
                            slot = blocks.setdefault(didx, {"type": "text", "id": "", "name": "",
                                                            "text": "", "json": ""})
                            d0 = obj.get("delta")
                            d = d0 if isinstance(d0, dict) else {}
                            dt = d.get("type")
                            if dt == "text_delta" and isinstance(d.get("text"), str) and d["text"]:
                                for kind0, piece in sp.feed(d["text"]):
                                    yield ChatChunk(content=piece if kind0 == "content" else "",
                                                    reasoning=piece if kind0 == "reasoning" else "")
                            elif dt == "thinking_delta" and isinstance(d.get("thinking"), str) and d["thinking"]:
                                yield ChatChunk(reasoning=d["thinking"])
                            elif dt == "input_json_delta":
                                pj = d.get("partial_json")
                                slot["json"] += pj if isinstance(pj, str) else json.dumps(pj)
                        elif event == "message_delta":
                            u = obj.get("usage")
                            if isinstance(u, dict) and u:
                                usage = {**(usage or {}), **u}
                            dlt = obj.get("delta")
                            stop = dlt.get("stop_reason") if isinstance(dlt, dict) else None
                            if stop:
                                saw_stop = True
                                for kind0, piece in sp.flush():
                                    yield ChatChunk(content=piece if kind0 == "content" else "",
                                                    reasoning=piece if kind0 == "reasoning" else "")
                                calls = [
                                    {"id": slot["id"] or f"call_{i}", "type": "function",
                                     "function": {"name": slot["name"], "arguments": slot["json"] or "{}"}}
                                    for i, slot in sorted(blocks.items())
                                    if slot.get("type") == "tool_use" and slot.get("name")
                                ]
                                finish = {"tool_use": "tool_calls", "max_tokens": "length",
                                          "stop_sequence": "stop", "end_turn": "stop"}.get(stop, stop)
                                yield ChatChunk(
                                    tool_calls=calls or None, done=True, finish=finish,
                                    usage=_anth_usage(usage))
                except BackendError:
                    raise
                except (httpx.HTTPError, UnicodeDecodeError) as exc:
                    raise BackendError(f"{model}: stream broke: {exc}") from exc
        if not saw_stop:
            # early EOF: never lose accumulated tool JSON or the splitter tail silently
            for kind0, piece in sp.flush():
                yield ChatChunk(content=piece if kind0 == "content" else "",
                                reasoning=piece if kind0 == "reasoning" else "")
            calls = [
                {"id": slot["id"] or f"call_{i}", "type": "function",
                 "function": {"name": slot["name"], "arguments": slot["json"] or "{}"}}
                for i, slot in sorted(blocks.items())
                if slot.get("type") == "tool_use" and slot.get("name")
            ]
            yield ChatChunk(tool_calls=calls or None, done=True,
                            finish="stream_ended", usage=_anth_usage(usage))

    # ---------------------------------------------------------------- pull
    async def pull(self, name: str, progress: Any = None) -> None:
        raise BackendError("cloud models are hosted — nothing to pull; "
                           "pick one with Ctrl+M (models list comes from the provider)")
