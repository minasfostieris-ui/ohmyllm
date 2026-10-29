"""LM Studio backend: OpenAI-compatible server on localhost (default :1234).

LM Studio's GUI hosts the server; OhMyLLM can drive `lms server start` when the
CLI is available, otherwise it shows exact instructions instead of failing
silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from ..config.models import BackendName, GenerationSettings
from .base import BackendError, BaseBackend, ChatChunk, ModelInfo, StartResult

SERVER_INSTRUCTIONS = [
    "Open LM Studio.",
    "Load a model (top bar, or Ctrl/Cmd+K).",
    "Go to the Developer / Server tab (left sidebar, server icon).",
    "Set the port (default 1234) and click 'Start Server'.",
    "Enable 'Accept connections from local apps' if the port stays unreachable.",
    "Newer versions also offer the CLI: run `lms server start` in a terminal.",
]


_CONTENT_START_RX = re.compile(r'"content"\s*:\s*"')


from ..market import has_vision, scan_all_models  # noqa: E402


class LMStudioBackend(BaseBackend):
    name = BackendName.LMSTUDIO
    display_name = "LM Studio"

    # ----------------------------------------------------------- presence
    def _app_candidate(self) -> Path | None:
        cfg = self.config.lmstudio.binary_path
        if cfg:
            p = Path(cfg).expanduser()
            return p if p.exists() else None
        sysname = platform.system()
        if sysname == "Darwin":
            for cand in ("/Applications/LM Studio.app", str(Path.home() / "Applications/LM Studio.app")):
                if Path(cand).exists():
                    return Path(cand)
        elif sysname == "Windows":
            la = os.environ.get("LOCALAPPDATA", "")
            if la:
                p = Path(la) / "Programs" / "LM Studio" / "LM Studio.exe"
                if p.exists():
                    return p
        else:
            which = shutil.which("lm-studio") or shutil.which("lmstudio")
            if which:
                return Path(which)
            for home in (Path.home() / "Applications", Path.home()):
                hits = sorted(home.glob("*[Ll][Mm] *[Ss]tudio*.AppImage")) or sorted(home.glob("LM_Studio*.AppImage"))
                if hits:
                    return hits[0]
        return None

    def lms_cli(self) -> str | None:
        found = shutil.which("lms")
        if found:
            return found
        cand = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
        return str(cand) if cand.exists() else None

    def is_installed(self) -> bool:
        return self._app_candidate() is not None or self.lms_cli() is not None

    async def is_running(self) -> bool:
        ok, _ = await self.health()
        return ok

    async def health(self) -> tuple[bool, dict[str, Any]]:
        base = self.config.lmstudio.base_url
        try:
            async with self._client(base, timeout=3.0) as c:
                r = await c.get("/v1/models")
                if r.status_code == 200:
                    try:
                        n = len(r.json().get("data", []))
                    except Exception:  # noqa: BLE001
                        n = -1
                    return True, {"base_url": base, "models": n}
                return False, {"error": f"HTTP {r.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    async def ensure_running(self) -> StartResult:
        ok, info = await self.health()
        if ok:
            return StartResult(ok=True, was_already_running=True, message=f"LM Studio server live at {info.get('base_url')}")
        cli = self.lms_cli()
        app = self._app_candidate()
        if not self.config.lmstudio.auto_start:
            return StartResult(ok=False, message="Auto-start disabled in settings.", instructions=SERVER_INSTRUCTIONS)
        if not cli and not app:
            return StartResult(
                ok=False,
                message="LM Studio does not appear to be installed on this machine.",
                instructions=["Install it from https://lmstudio.ai (macOS/Windows/Linux)", *SERVER_INSTRUCTIONS],
            )
        launched = False
        popen_kw: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if os.name == "posix":
            popen_kw["start_new_session"] = True
        else:
            popen_kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            if cli:
                subprocess.Popen([cli, "server", "start"], **popen_kw)  # noqa: S603
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(app)], **popen_kw)  # noqa: S603
            else:
                subprocess.Popen([str(app)], **popen_kw)  # noqa: S603
            launched = True
        except OSError as exc:
            return StartResult(ok=False, message=f"Could not launch LM Studio: {exc}", instructions=SERVER_INSTRUCTIONS)
        deadline = time.monotonic() + self.config.lmstudio.startup_timeout
        while time.monotonic() < deadline:
            ok, info = await self.health()
            if ok:
                return StartResult(ok=True, started_process=launched, message="LM Studio server is live.")
            await asyncio.sleep(0.7)
        return StartResult(
            ok=False,
            started_process=launched,
            message="LM Studio was launched but its local server never answered.",
            instructions=[*SERVER_INSTRUCTIONS, f"Verify the base URL in settings (currently {self.config.lmstudio.base_url})."],
        )

    # ------------------------------------------------------------- models
    def _vis(self, name: str, vis: set[str], allm: set[str]) -> bool | None:
        v = has_vision(name, vis)
        if v is not None:
            return v
        low = name.lower()
        return False if any(s2 in low or low in s2 for s2 in allm) else None

    def vision_scan(self) -> set[str]:
        from ..market import scan_vision_models

        return scan_vision_models()

    async def list_models(self) -> list[ModelInfo]:
        base = self.config.lmstudio.base_url
        try:
            async with self._client(base, timeout=10.0) as c:
                r = await c.get("/v1/models")
                r.raise_for_status()
                data = r.json().get("data", [])
        except httpx.HTTPError as exc:
            raise BackendError(f"Cannot list LM Studio models: {exc}") from exc
        rest = await self._rest_model_details()
        self._vision_scan_cache = getattr(self, "_vision_scan_cache", None) or self.vision_scan()
        vis: set[str] = self._vision_scan_cache
        try:
            self._all_scan_cache = getattr(self, "_all_scan_cache", None) or scan_all_models()
            allm: set[str] = self._all_scan_cache
        except Exception:  # noqa: BLE001
            allm = set()
        for name, sizes in (await self._lms_cli_sizes()).items():
            target = rest.setdefault(name, {"id": name})
            for k, v in sizes.items():
                target.setdefault(k, v)
        out: list[ModelInfo] = []
        for m in data:
            name = m.get("id") or m.get("model") or "?"
            extra = rest.get(name, {})
            info = ModelInfo(
                name=name,
                backend=self.name,
                size_bytes=_as_int(extra.get("vram_required") or extra.get("size_vram") or extra.get("size_bytes")),
                quant=extra.get("quantization"),
                params_b=None,
                ctx_len=_as_int(m.get("context_length") or extra.get("ctx_length") or extra.get("loaded_context_length")),
                modified=_as_float(m.get("created")),
                tool_capable=_tool_flag(extra),
                vision=self._vis(name, vis, allm),
                loaded=bool(extra.get("loaded")) if "loaded" in extra else None,
            )
            out.append(info.apply_name_heuristics())
        seen = {i.name for i in out}
        for name, extra in rest.items():
            if name in seen:
                continue
            info = ModelInfo(
                name=name,
                backend=self.name,
                size_bytes=_as_int(extra.get("size_bytes") or extra.get("vram_required")),
                quant=extra.get("quantization"),
                params_b=None,
                ctx_len=_as_int(extra.get("max_context_length") or extra.get("ctx_length")),
                tool_capable=_tool_flag(extra),
                vision=self._vis(name, vis, allm),
                loaded=str(extra.get("state") or "").startswith("loaded"),
            )
            out.append(info.apply_name_heuristics())
        return out

    async def _lms_cli_sizes(self) -> dict[str, dict[str, Any]]:
        """`lms ls --json` carries on-disk sizes the HTTP APIs omit. Best effort."""
        import asyncio
        import json as _json
        import subprocess

        cli = self.lms_cli()
        if not cli:
            return {}
        try:
            proc = await asyncio.to_thread(
                subprocess.run, [cli, "ls", "--json"],
                capture_output=True, text=True, timeout=6,
            )
            rows = _json.loads(proc.stdout or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for r in rows if isinstance(rows, list) else []:
            key = r.get("modelKey") or r.get("identifier")
            if not key:
                continue
            entry: dict[str, Any] = {}
            if r.get("sizeBytes"):
                entry["size_bytes"] = r["sizeBytes"]
            if r.get("loadedContextLength"):
                entry["ctx_length"] = r["loadedContextLength"]
            out[key] = entry
        return out

    async def _rest_model_details(self) -> dict[str, dict[str, Any]]:
        """Newer LM Studio builds expose /api/v0/models with rich metadata.
        Best effort: older builds 404 and we degrade to /v1 names only."""
        try:
            async with self._client(self.config.lmstudio.base_url, timeout=4.0) as c:
                r = await c.get("/api/v0/models")
                if r.status_code != 200:
                    return {}
                payload = r.json()
        except httpx.HTTPError:
            return {}
        items = payload.get("data", payload if isinstance(payload, list) else [])
        out: dict[str, dict[str, Any]] = {}
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("id"):
                out[it["id"]] = it
        return out

    # --------------------------------------------------------------- chat
    async def _v0_state(self, model: str) -> tuple[str | None, str]:
        """Return (key, state) for a model via the v0 admin API."""
        async with self._client(self.config.lmstudio.base_url, timeout=10.0) as c:
            resp = await c.get("/api/v0/models")
            resp.raise_for_status()
            for m in resp.json().get("data") or []:
                if m.get("id") == model or m.get("key") == model:
                    return str(m.get("key") or m.get("id")), str(m.get("state") or "")
        return None, ""

    async def unload(self, model: str) -> bool:
        """Best-effort eject of a loaded model (frees VRAM on model switch).

        Tries the v1 REST endpoints, then the ``lms`` CLI. Never raises:
        an eject failure must not break the turn that triggered the switch.
        """
        import asyncio
        import subprocess

        for path in ("/api/v1/models/eject", "/api/v1/models/unload"):
            try:
                async with self._client(self.config.lmstudio.base_url, timeout=15.0) as c:
                    resp = await c.post(path, json={"model": model})
                    if resp.status_code == 200:
                        return True
            except (httpx.HTTPError, BackendError):
                pass
        cli = self.lms_cli()
        if cli:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, [cli, "unload", model],
                    capture_output=True, text=True, timeout=60,
                )
                return proc.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        return False

    async def ensure_loaded(self, model: str) -> None:
        """LM Studio may refuse requests to unloaded models ('Model is unloaded');
        load it explicitly and wait. Ejects the model OhMyLLM loaded previously
        so switching models frees VRAM instead of stacking two loaded models."""
        import asyncio
        import time

        prev = getattr(self, "_last_loaded", None)
        if prev and prev != model:
            _, pstate = await self._v0_state(prev)
            if pstate == "loaded":
                await self.unload(prev)
        self._last_loaded = model
        _, state = await self._v0_state(model)
        if state == "loaded":
            return
        # GPU offload is only controllable through the lms CLI — the REST load
        # API uses LM Studio's own defaults, which is how a 9B Q4 ends up at
        # ~5 t/s on CPU. Prefer the CLI with explicit flags; fall back to REST.
        gen = self.config.settings_for(BackendName.LMSTUDIO, model)
        cli = self.lms_cli()
        loaded_via_cli = False
        if cli:
            import subprocess
            args = [cli, "load", model, "-y"]
            if gen.num_gpu == -1:
                args += ["--gpu", "max"]
            elif gen.num_gpu == 0:
                args += ["--gpu", "off"]
            if gen.num_ctx:
                args += ["--context-length", str(gen.num_ctx)]
            proc = await asyncio.to_thread(
                subprocess.run, args, capture_output=True, text=True, timeout=600,
            )
            if proc.returncode == 0:
                loaded_via_cli = True
            elif "unknown option" not in (proc.stderr or proc.stdout).lower():
                raise BackendError(
                    f"LM Studio failed to load {model!r}: "
                    f"{(proc.stderr or proc.stdout).strip()[:300]} — likely not enough VRAM"
                )
        loaded_via_api = False
        if not loaded_via_cli:
            # v1 REST (LM Studio ≥ 0.4): POST /api/v1/models/load {"model": id}
            try:
                async with self._client(self.config.lmstudio.base_url, timeout=60.0) as c:
                    resp = await c.post("/api/v1/models/load", json={"model": model})
                    if resp.status_code == 200:
                        loaded_via_api = True
                    elif resp.status_code == 404:
                        pass  # older server: fall through to the lms CLI
                    else:
                        raise BackendError(
                            f"LM Studio could not load {model!r} (HTTP {resp.status_code}): "
                            f"{resp.text[:200]}"
                        )
            except httpx.HTTPError as exc:
                raise BackendError(f"LM Studio load request failed for {model!r}: {exc}") from exc
            if not loaded_via_api:
                if not cli:
                    raise BackendError(
                        f"LM Studio model {model!r} is not loaded and this server version "
                        "has no load API — load it in the LM Studio app or run `lms load "
                        f"{model}`"
                    )
                import subprocess
                proc = await asyncio.to_thread(
                    subprocess.run, [cli, "load", model],
                    capture_output=True, text=True, timeout=600,
                )
                if proc.returncode != 0:
                    raise BackendError(
                        f"LM Studio failed to load {model!r}: "
                        f"{(proc.stderr or proc.stdout).strip()[:300]} — likely not enough VRAM"
                    )
        deadline = time.monotonic() + max(self.config.lmstudio.startup_timeout, 180.0)
        while time.monotonic() < deadline:
            _, state = await self._v0_state(model)
            if state == "loaded":
                return
            await asyncio.sleep(1.0)
        raise BackendError(f"LM Studio is still loading {model!r} after the timeout — try again")

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: GenerationSettings,
        *,
        tools: list[dict[str, Any]] | None = None,
        native_tools: bool = False,
    ) -> AsyncIterator[ChatChunk]:
        if self._transport is None:  # live server: make sure the model is resident
            await self.ensure_loaded(model)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": params.temperature,
            "top_p": params.top_p,
            "max_tokens": max(params.max_tokens, 16384) if params.ultrathink else params.max_tokens,
            # keep the model resident between requests (LM Studio unloads by
            # default after each completion; unknown keys are ignored elsewhere)
            "unload_model": False,
        }
        if params.seed:
            body["seed"] = params.seed
        # effort ladder: /think low..max; /ultrathink rides the ceiling (xhigh)
        # and exceeds every level via the critique-and-revise pass in runtime.
        # xhigh is the highest level LM Studio/llama.cpp accept; "max" maps to
        # it plus the explicit thinking_budget when the user set one.
        if params.kv_cache not in ("f16", "auto"):
            # llama.cpp-standard keys; LM Studio applies them at model load
            # and ignores them otherwise (verified: unknown keys never 400)
            body["cache_type_k"] = params.kv_cache
            body["cache_type_v"] = params.kv_cache
        lvl = "xhigh" if params.ultrathink else params.effort
        if lvl != "off":
            wire = {"low": "low", "medium": "medium", "high": "high",
                    "xhigh": "xhigh", "max": "xhigh"}.get(lvl, "medium")
            body["reasoning_effort"] = wire
            body["reasoning"] = {"enabled": True, "effort": wire}
            if params.thinking_budget:
                body["thinking_budget"] = params.thinking_budget
        if tools and native_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        pending_tools: dict[int, dict[str, Any]] = {}
        # reasoning models (gpt-oss family) stream their text into delta.reasoning
        # and may leave delta.content empty — buffer it as a fallback answer.
        # Models whose servers do NOT parse inline tags (raw llama.cpp, vLLM
        # without --reasoning-parser, Qwen3/QwQ/R1/Kimi/GLM-Z1 templates) leak
        #  into content: the splitter routes it back to the
        # reasoning channel so tags never reach the UI or the session.
        from .thinksplit import ThinkSplitter
        sp = ThinkSplitter()
        reasoning_parts: list[str] = []
        content_seen = False
        prog_seen: set[int] = set()
        # servers that validate reasoning params strictly (some LM Studio
        # builds reject xhigh for template-controlled models) get ONE graceful
        # retry with the effort keys stripped instead of a dead turn
        for _attempt in range(2):
            try:
                # read=None would hang forever if the server accepts the
                # request but never emits (wedged model slot); cap the gap
                # between SSE lines instead — a streaming turn can take as
                # long as it likes as long as tokens keep arriving
                _to = httpx.Timeout(params.timeout, read=min(max(params.timeout, 300.0), 1800.0))
                async with self._client(self.config.lmstudio.base_url, timeout=_to) as c:
                    async with c.stream("POST", "/v1/chat/completions", json=body) as resp:
                        if resp.status_code != 200:
                            detail = (await resp.aread()).decode(errors="replace")[:300]
                            raise BackendError(f"LM Studio chat failed ({resp.status_code}): {detail}")
                        if "text/event-stream" not in (resp.headers.get("content-type") or ""):
                            # LM Studio answers some failures (unloaded model, disk
                            # not mounted) as 200 + plain JSON — surface it instead
                            # of streaming silence
                            detail = (await resp.aread()).decode(errors="replace")[:300]
                            raise BackendError(f"LM Studio chat: expected SSE, got {detail}")
                        event_name = ""
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if line.startswith("event:"):
                                event_name = line[6:].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if event_name == "error":
                                try:
                                    msg = (json.loads(payload).get("error") or {}).get("message") or payload
                                except json.JSONDecodeError:
                                    msg = payload
                                raise BackendError(f"LM Studio: {str(msg)[:200]}")
                            if payload == "[DONE]":
                                for kind, text in sp.flush():
                                    if kind == "reasoning":
                                        reasoning_parts.append(text)
                                    else:
                                        content_seen = True
                                        yield ChatChunk(content=text)
                                if not content_seen and reasoning_parts and not pending_tools:
                                    yield ChatChunk(content="".join(reasoning_parts))
                                yield ChatChunk(done=True, finish="stop", tool_calls=_finish_tools(pending_tools))
                                return
                            try:
                                data = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            choice = (data.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            finish = choice.get("finish_reason")
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = pending_tools.setdefault(idx, {"name": "", "args": ""})
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
                                    n = len(slot["args"])
                                    crossed = idx not in prog_seen or n // 64 > (n - len(fn["arguments"])) // 64
                                    if slot["name"] and crossed:
                                        prog_seen.add(idx)
                                        ct = ""
                                        cm = _CONTENT_START_RX.search(slot["args"])
                                        if cm:
                                            ct = slot["args"][cm.end():][-1600:]
                                        yield ChatChunk(tool_progress={
                                            "name": slot["name"], "chars": n,
                                            "tail": slot["args"][-900:],
                                            "content_tail": ct,
                                        })
                            for key in ("reasoning", "reasoning_content"):
                                if delta.get(key):
                                    reasoning_parts.append(str(delta[key]))
                            ctext, itext = "", ""
                            if delta.get("content"):
                                for kind, text in sp.feed(str(delta["content"])):
                                    if kind == "reasoning":
                                        itext += text
                                        reasoning_parts.append(text)
                                    else:
                                        ctext += text
                            if ctext:
                                content_seen = True
                            usage = data.get("usage")
                            chunk = ChatChunk(
                                content=ctext,
                                reasoning=str(delta.get("reasoning") or delta.get("reasoning_content") or "") + itext,
                                done=bool(finish), finish=finish, usage=usage,
                            )
                            if finish:
                                for kind, text in sp.flush():
                                    if kind == "reasoning":
                                        chunk.reasoning += text
                                        reasoning_parts.append(text)
                                    else:
                                        chunk.content += text
                                        content_seen = True
                            if finish:
                                for fslot in pending_tools.values():
                                    if fslot["name"] and fslot["args"]:
                                        fm = _CONTENT_START_RX.search(fslot["args"])
                                        yield ChatChunk(tool_progress={
                                            "name": fslot["name"], "chars": len(fslot["args"]),
                                            "tail": fslot["args"][-900:],
                                            "content_tail": fslot["args"][fm.end():][-1600:] if fm else "",
                                        })
                                if pending_tools:
                                    chunk.tool_calls = _finish_tools(pending_tools)
                                elif not content_seen and reasoning_parts:
                                    chunk.content = "".join(reasoning_parts)
                            yield chunk
                            if finish:
                                return
            except httpx.HTTPError as exc:
                raise BackendError(f"LM Studio request error: {exc}") from exc
            except BackendError as exc:
                if _attempt == 0 and ("reasoning" in str(exc).lower() or "effort" in str(exc).lower()) \
                        and ("reasoning_effort" in body or "reasoning" in body):
                    body.pop("reasoning_effort", None)
                    body.pop("reasoning", None)
                    continue
                if _attempt == 0 and any(k in str(exc).lower() for k in ("image", "vision", "multimodal")) \
                        and any(not isinstance(m.get("content"), str) for m in body.get("messages", [])):
                    # model/server has no vision path — resend as text-only
                    body["messages"] = [
                        {**m, "content": " ".join(str(x.get("text") or "") for x in m["content"]
                                                  if isinstance(x, dict) and x.get("type") == "text").strip() or "[image]"}
                        if not isinstance(m.get("content"), str) else m
                        for m in body["messages"]]
                    continue
                raise


    async def pull(self, name: str, progress: Any = None) -> None:
        raise BackendError(
            "LM Studio downloads are managed inside its app (search in the 'Discover' tab, "
            "or import GGUFs into ~/.cache/lm-studio/models)."
        )


def _finish_tools(pending: dict[int, dict[str, Any]]) -> list[dict[str, Any]] | None:
    if not pending:
        return None
    calls: list[dict[str, Any]] = []
    for idx in sorted(pending):
        raw = pending[idx]
        try:
            args = json.loads(raw["args"] or "{}") if raw["args"] else {}
        except json.JSONDecodeError:
            args = {"_raw": raw["args"]}
        calls.append({"id": f"call_{idx}", "type": "function", "function": {"name": raw["name"], "arguments": args}})
    return calls


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _tool_flag(extra: dict[str, Any]) -> bool | None:
    caps = extra.get("capabilities")
    if isinstance(caps, list):
        return any(c in caps for c in ("tool_use", "tools", "function_calling"))
    return None
