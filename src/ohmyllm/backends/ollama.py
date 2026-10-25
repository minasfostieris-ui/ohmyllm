"""Ollama backend: native API, auto-start, streaming chat, pulls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import paths
from ..config.models import BackendName, GenerationSettings
from .base import BackendError, BaseBackend, ChatChunk, ModelInfo, StartResult

INSTALL_INSTRUCTIONS = [
    "Install Ollama: https://ollama.com/download (macOS/Windows installer)",
    "Linux: curl -fsSL https://ollama.com/install.sh | sh",
    "Then run:  ollama serve   (OhMyLLM will also start it automatically)",
    "And pull a model, e.g.:  ollama pull qwen2.5:7b-instruct",
]


class OllamaBackend(BaseBackend):
    name = BackendName.OLLAMA
    display_name = "Ollama"

    # ----------------------------------------------------------- presence
    def binary(self) -> str | None:
        return shutil.which(self.config.ollama.binary)

    def is_installed(self) -> bool:
        return self.binary() is not None

    async def is_running(self) -> bool:
        ok, _ = await self.health()
        return ok

    async def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            async with self._client(self.config.ollama.host, timeout=3.0) as c:
                r = await c.get("/api/version")
                if r.status_code == 200:
                    return True, {"version": r.json().get("version", "?"), "host": self.config.ollama.host}
                return False, {"error": f"HTTP {r.status_code}"}
        except Exception as exc:  # noqa: BLE001 - any failure == unhealthy
            return False, {"error": str(exc)}

    async def ensure_running(self) -> StartResult:
        ok, info = await self.health()
        if ok:
            return StartResult(ok=True, was_already_running=True, message=f"Ollama {info.get('version', '')} is live at {self.config.ollama.host}")
        exe = self.binary()
        if not exe:
            return StartResult(
                ok=False,
                message="Ollama binary not found on PATH.",
                instructions=INSTALL_INSTRUCTIONS,
            )
        if not self.config.ollama.auto_start:
            return StartResult(
                ok=False,
                message="Auto-start is disabled in settings; run 'ollama serve' manually.",
                instructions=["Run:  ollama serve", "Keep the terminal open, then retry."],
            )
        logf = paths.logs_dir() / "ollama-serve.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(logf, "ab")
            popen_kw: dict[str, Any] = {"stdout": fh, "stderr": fh, "stdin": subprocess.DEVNULL}
            if os.name == "posix":
                popen_kw["start_new_session"] = True
            else:
                popen_kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
            env = dict(os.environ)
            host = self.config.ollama.host
            m = httpx.URL(host)
            env.setdefault("OLLAMA_HOST", f"{m.host}:{m.port or 11434}")
            proc = subprocess.Popen([exe, "serve"], env=env, **popen_kw)  # noqa: S603
        except OSError as exc:
            return StartResult(ok=False, message=f"Failed to launch 'ollama serve': {exc}", instructions=INSTALL_INSTRUCTIONS)
        deadline = time.monotonic() + self.config.ollama.startup_timeout
        while time.monotonic() < deadline:
            ok, info = await self.health()
            if ok:
                return StartResult(
                    ok=True, started_process=True,
                    message=f"Started Ollama (pid {proc.pid}) at {host}",
                )
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.4)
        tail: list[str] = []
        with contextlib.suppress(OSError):
            tail = logf.read_text(errors="replace").splitlines()[-3:]
        return StartResult(
            ok=False,
            started_process=True,
            message="Ollama process started but the API never became healthy"
            + (f": {' | '.join(tail)}" if tail else ""),
            instructions=["Check " + str(logf), "Try running manually:  ollama serve"],
        )

    # ------------------------------------------------------------- models
    async def list_models(self) -> list[ModelInfo]:
        try:
            async with self._client(self.config.ollama.host, timeout=10.0) as c:
                r = await c.get("/api/tags")
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            raise BackendError(f"Cannot list Ollama models: {exc}") from exc
        out: list[ModelInfo] = []
        for m in data.get("models", []):
            details = m.get("details", {}) or {}
            params = _parse_params(details.get("parameter_size"))
            info = ModelInfo(
                name=m.get("model", "?"),
                backend=self.name,
                size_bytes=int(m.get("size") or 0) or None,
                quant=details.get("quantization_level"),
                params_b=params,
                modified=_parse_ts(m.get("modified_at")),
                path=m.get("digest"),
            )
            out.append(info.apply_name_heuristics())
        for info in out:
            try:
                show = await self.inspect(info.name)
            except Exception:  # noqa: BLE001 — listing must not fail on metadata
                show = {}
            if show:
                _apply_show(info, show)
        return out

    async def inspect(self, model: str) -> dict[str, Any]:
        try:
            async with self._client(self.config.ollama.host, timeout=10.0) as c:
                r = await c.post("/api/show", json={"name": model})
                r.raise_for_status()
                return r.json()
        except httpx.HTTPError:
            return {}

    async def get_model(self, name: str) -> ModelInfo | None:
        base = await super().get_model(name)
        show = await self.inspect(name)
        if base is None and show:
            base = ModelInfo(name=name, backend=self.name)
        if base is None:
            return None
        _apply_show(base, show)
        base.apply_name_heuristics()
        return base

    # --------------------------------------------------------------- chat
    def _options(self, params: GenerationSettings) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "temperature": params.temperature,
            "top_p": params.top_p,
            "top_k": params.top_k,
            "repeat_penalty": params.repeat_penalty,
            "num_ctx": params.num_ctx,
            "num_batch": params.num_batch,
            "num_predict": params.max_tokens,
            "seed": params.seed or -1,
        }
        if params.num_gpu >= 0:
            opts["num_gpu"] = params.num_gpu
        if params.threads:
            opts["num_thread"] = params.threads
        return opts

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: GenerationSettings,
        *,
        tools: list[dict[str, Any]] | None = None,
        native_tools: bool = False,
    ) -> AsyncIterator[ChatChunk]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": self._options(params),
        }
        if tools and native_tools:
            body["tools"] = tools
        body["keep_alive"] = "30m"  # don't unload between turns
        if params.thinking:
            body["thinking"] = True
        prev = getattr(self, "_last_model", None)
        if prev and prev != model:
            # eject the previously used model so it stops squatting on VRAM
            try:
                async with self._client(self.config.ollama.host, timeout=30.0) as uc:
                    await uc.post("/api/generate", json={"model": prev, "keep_alive": 0})
            except httpx.HTTPError:
                pass  # best effort
        self._last_model = model
        content_seen = False
        thinking_parts: list[str] = []
        try:
            async with self._client(self.config.ollama.host, timeout=httpx.Timeout(params.timeout, read=None)) as c:
                async with c.stream("POST", "/api/chat", json=body) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread()).decode(errors="replace")[:300]
                        raise BackendError(f"Ollama chat failed ({resp.status_code}): {detail}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = data.get("message") or {}
                        if msg.get("content"):
                            content_seen = True
                        if msg.get("thinking"):
                            thinking_parts.append(str(msg["thinking"]))
                        chunk = ChatChunk(
                            content=msg.get("content") or "",
                            reasoning=str(msg.get("thinking") or ""),
                            tool_calls=msg.get("tool_calls"),
                            done=bool(data.get("done")),
                            finish=data.get("done_reason"),
                        )
                        if data.get("done"):
                            chunk.usage = {
                                "eval_count": data.get("eval_count"),
                                "prompt_eval_count": data.get("prompt_eval_count"),
                                "eval_duration_ns": data.get("eval_duration"),
                            }
                            # thinking-only replies (qwen3, r1) would look empty
                            if not content_seen and thinking_parts and not chunk.tool_calls:
                                chunk.content = "".join(thinking_parts)
                        yield chunk
        except httpx.HTTPError as exc:
            raise BackendError(f"Ollama request error: {exc}") from exc

    # --------------------------------------------------------------- pull
    async def pull(self, name: str, progress: Any = None) -> None:
        try:
            async with self._client(self.config.ollama.host, timeout=httpx.Timeout(30.0, read=None)) as c:
                async with c.stream("POST", "/api/pull", json={"name": name, "stream": True}) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread()).decode(errors="replace")[:300]
                        raise BackendError(f"Ollama pull failed ({resp.status_code}): {detail}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        status = data.get("status", "")
                        total, completed = data.get("total"), data.get("completed")
                        pct = (completed / total) if total and completed is not None else None
                        if progress:
                            progress(status, pct)
                        if "error" in data:
                            raise BackendError(f"Ollama pull error: {data['error']}")
        except httpx.HTTPError as exc:
            raise BackendError(f"Ollama pull failed: {exc}") from exc


def _parse_params(s: str | None) -> float | None:
    if not s:
        return None
    import re

    m = re.search(r"(\d+(?:\.\d+)?)\s*B", s, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _apply_show(info: ModelInfo, show: dict[str, Any]) -> None:
    """Merge /api/show metadata into a ModelInfo (used by list + get paths)."""
    meta = show.get("model_info") or {}
    for k, v in meta.items():
        lk = k.lower()
        if lk.endswith(".size"):
            info.size_bytes = info.size_bytes or int(v or 0) or None
        elif lk.endswith(".quantization") and isinstance(v, str) and "Q" in v.upper():
            info.quant = info.quant or v
        elif lk.endswith(".context_length") and isinstance(v, (int, float)):
            info.ctx_len = info.ctx_len or int(v)
        elif lk.endswith(".block_count") and isinstance(v, (int, float)):
            info.layers = info.layers or int(v)
        elif lk == "general.parameter_count" and isinstance(v, (int, float)):
            info.params_b = info.params_b or round(v / 1e9, 2) or None
    params = show.get("parameters") or ""
    if isinstance(params, str) and info.quant is None:
        for tok in params.split():
            if tok.upper().startswith("Q"):
                info.quant = tok
                break
    tmpl = show.get("template") or ""
    msgs = json.dumps(show.get("messages", []))[:4000]
    if "Tool" in tmpl or "tool_calls" in tmpl or '"tool"' in msgs:
        info.tool_capable = True
