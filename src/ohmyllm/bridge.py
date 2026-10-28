"""Local GUI bridge: serves the OhMyLLM engine over loopback HTTP + SSE.

This is the backend the Tauri window (or a browser via `ohmyllm gui --browser`)
talks to. Design constraints:

- 127.0.0.1 only, random port by default, per-process bearer token.
- Zero extra dependencies: stdlib ThreadingHTTPServer + an asyncio loop in the
  main thread; tool execution still goes through the same AgentRuntime,
  PolicyEngine and ApprovalManager as the TUI.
- Approvals are SSE-pushed to the UI and resolved by an HTTP POST, so the
  permission engine never runs without a human (unless full_access).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, paths
from .agents.runtime import AgentCancelled, TurnResult
from .capabilities import thinking_support as _thinking_support
from .config import BackendName, Config, PermissionMode
from .core import Services
from .doctor import run_checks, summarize
from .safety import ApprovalOutcome
from .sessions.export import export
from .sessions.store import make_title
from .vision import cached_verdict as _vision_cached
from .vram.detect import detect_gpus, human_bytes

STATIC_DIR = Path(__file__).resolve().parent / "gui_static"
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",  # loopback dev tool; token still required for writes
    "Access-Control-Allow-Headers": "Content-Type, X-OMLLM-Token",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
}


class BridgeState:
    def __init__(self, workspace: str | None = None) -> None:
        self.svc = Services.boot(workspace)
        self.token = secrets.token_hex(16)
        self.session_id: str | None = None
        self.approvals: dict[str, asyncio.Future] = {}
        self.cancel: asyncio.Event | None = None
        self.busy = False
        self.turn_lock = threading.Lock()  # atomic busy-check-and-claim
        self.interjections: list[str] = []
        self.tok_s = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.active_backend_ready: bool | None = None
        self.market_state: Any = None
        self.market_cancel: bool = False
        self.market_thread: Any = None
        self.cfg_mtime = 0.0
        self.talk: Any = None  # server-side hold-to-talk recorder
        self.talk_pending = False  # start() still spawning the recorder
        self.shutdown_hook = lambda: None

    def ensure_session(self) -> Any:
        if self.session_id:
            sess = self.svc.sessions.get(self.session_id)
            if sess:
                return sess
        sess = self.svc.new_session()
        self.session_id = sess.id
        return sess

    def status(self) -> dict[str, Any]:
        sess = self.svc.sessions.get(self.session_id) if self.session_id else None
        ctx_used = sum(len(str(m.get("content", ""))) for m in sess.messages) // 4 if sess else 0
        snap = self.svc.status_snapshot(tok_s=self.tok_s, ctx_used=ctx_used)
        gpus = detect_gpus()
        snap.update(
            version=__version__,
            busy=self.busy,
            backend_ready=bool(self.active_backend_ready) if self.active_backend_ready is not None else None,
            tok_s=self.tok_s,
            ctx_window=snap["num_ctx"],  # cloud-aware window from the snapshot,
                                         # not the raw local num_ctx setting
            session=sess.id if sess else None,
            session_title=sess.title if sess else None,
            saved=True,
            gpu=[{"model": g.model, "total": human_bytes(g.total_bytes),
                  "free": human_bytes(g.free_bytes) if g.free_bytes is not None else None} for g in gpus],
            gpu_total_bytes=sum(g.total_bytes for g in gpus) or None,
            workspace=str(self.svc.workspace),
            soul=str(self.svc.soul.path),
            memory_writes=self.svc.config.memory.agent_writes,
        )
        return snap


class Handler(BaseHTTPRequestHandler):
    @property
    def state(self) -> BridgeState:  # per-server; testable isolation
        s = getattr(self.server, "state", None)
        if s is None:
            raise RuntimeError("bridge state not bound")
        return s
    server_version = f"OhMyLLM-Bridge/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr spam
        with contextlib.suppress(OSError, TypeError):
            line = f"{time.strftime('%H:%M:%S')} gui {fmt % args}"
            tok = getattr(self.state, "token", "")
            if tok:
                line = line.replace(tok, "…token…")  # never persist the auth token
            paths.app_log_path().parent.mkdir(parents=True, exist_ok=True)
            log = paths.app_log_path()
            is_new = not log.exists()
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if is_new:
                with contextlib.suppress(OSError):
                    log.chmod(0o600)

    def _cors_preflight(self) -> bool:
        if self.command == "OPTIONS":
            self.send_response(204)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            return True
        return False

    def _authed(self, path: str) -> bool:
        if path == "/api/health" or path in ("/styles.css", "/app.js"):
            return True
        tok = self.headers.get("X-OMLLM-Token", "") or (parse_qs(urlparse(self.path).query).get("t", [""])[0])
        return tok == self.state.token

    def _sync_disk_config(self) -> None:
        """The TUI is a separate process; its saves are invisible here until
        config.toml's mtime moves — then reload before serving any state."""
        import os

        try:
            mt = os.path.getmtime(self.state.svc.config_store.path)
        except OSError:
            return
        if self.state.cfg_mtime and mt != self.state.cfg_mtime:
            self.state.cfg_mtime = mt
            with contextlib.suppress(Exception):
                self.state.svc.reload_config()
        elif not self.state.cfg_mtime:
            self.state.cfg_mtime = mt

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _sse_init(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def _sse(self, event: str, data: Any) -> None:
        with contextlib.suppress(OSError):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

    # ------------------------------------------------------------- routing
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._cors_preflight()

    def do_GET(self) -> None:  # noqa: N802
        if self._cors_preflight():
            return
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if not self._authed(path):
            return self._json(401, {"error": "bad token"})
        try:
            if path == "/" or path == "/index.html":
                return self._static()
            if path in ("/app.css", "/styles.css"):
                return self._static(name="styles.css")
            if path == "/app.js":
                return self._static(name="app.js")
            if path == "/api/health":
                return self._json(200, {"ok": True, "version": __version__})
            if path == "/api/status":
                self._sync_disk_config()
                return self._json(200, self.state.status())
            if path == "/api/market":
                return self._market()
            if path == "/api/market/progress":
                return self._market_progress()
            if path == "/api/voice":
                from .voice import status_line

                return self._json(200, {"line": status_line()})
            if path == "/api/providers":
                self._sync_disk_config()
                return self._json(200, self._providers_state())
            if path == "/api/skills":
                return self._json(200, {"skills": [
                    {"name": sk.name, "description": sk.description} for sk in self.state.svc.skills.list()]})
            if path == "/api/config":
                self._sync_disk_config()
                return self._json(200, _safe_config(self.state.svc.config))
            if path == "/api/backends":
                return self._run(self._backends())
            if path == "/api/models":
                return self._run(self._models(qs.get("backend", [None])[0]))
            if path == "/api/doctor":
                checks = run_checks(self.state.svc.config_store, self.state.svc.config)
                return self._json(200, {"checks": [c.__dict__ for c in checks], "summary": summarize(checks)})
            if path == "/api/soul":
                return self._json(200, {"path": str(self.state.svc.soul.path), "text": self.state.svc.soul.read(),
                                        "recent": self.state.svc.soul.recent(12),
                                        "backups": [str(p.name) for p in self.state.svc.soul.backups()[:20]],
                                        "entries": [{"section": e.section, "index": e.index, "text": e.text} for e in self.state.svc.soul.entries()]})
            if path == "/api/chats":
                return self._chats(qs)
            if path.startswith("/api/chats/"):
                sid = path.rsplit("/", 1)[-1]
                sess = self.state.svc.sessions.get(sid)
                return self._json(200, sess.to_dict()) if sess else self._json(404, {"error": "not found"})
            if path == "/api/audit":
                return self._json(200, {"events": self.state.svc.audit.recent(int(qs.get("n", ["50"])[0]))})
            if path == "/api/approvals":
                return self._json(200, {"rules": self.state.svc.approvals.permanent_rules})
        except Exception as exc:  # noqa: BLE001 — API must never die mid-render
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        if self._cors_preflight():
            return
        u = urlparse(self.path)
        path = u.path
        if not self._authed(path):
            return self._json(401, {"error": "bad token"})
        body = self._body()
        try:
            if path == "/api/message":
                return self._message(body)
            if path == "/api/cancel":
                # Event.set() from this (HTTP) thread is not loop-safe —
                # hop onto the loop
                if self.state.cancel is not None and self.state.loop is not None:
                    self.state.loop.call_soon_threadsafe(self.state.cancel.set)
                return self._json(200, {"ok": True})
            if path.startswith("/api/approval/"):
                return self._approval(path.rsplit("/", 1)[-1], body)
            if path == "/api/mode":
                return self._mode(body)
            if path == "/api/backend":
                return self._run(self._backend(body))
            if path == "/api/model":
                return self._set_model(body)
            if path == "/api/soul":
                return self._soul(body)
            if path == "/api/chats":
                sess = self.state.svc.new_session(body.get("first_message", ""))
                self.state.svc.sessions.save(sess)
                self.state.session_id = sess.id
                return self._json(200, sess.to_dict())
            if path.startswith("/api/chats/") and path.endswith("/export"):
                sid = path.split("/")[3]
                found = self.state.svc.sessions.get(sid)
                if not found:
                    return self._json(404, {"error": "not found"})
                fmt = body.get("format", "markdown")
                p = export(self.state.svc.sessions, found, fmt)
                return self._json(200, {"path": str(p)})
            if path == "/api/shutdown":
                # graceful close: eject the model, stop serving, exit
                self.state.shutdown_hook()
                return self._json(200, {"ok": True})
            if path == "/api/pull":
                return self._pull(body)
            if path == "/api/config":
                return self._config(body)
            if path == "/api/skills/cast":
                return self._skill_cast(body)
            if path == "/api/market/download":
                return self._market_download(body)
            if path == "/api/vision/probe":
                return self._run(self._vision_probe(body))
            if path == "/api/voice/transcribe":
                return self._run(self._voice_transcribe(body))
            if path == "/api/voice/record":
                return self._run(self._voice_record(body))
            if path == "/api/providers/key":
                code, payload = self._providers_key(body)
                return self._json(code, payload)
            if path == "/api/providers/use":
                code, payload = self._providers_use(body)
                return self._json(code, payload)
            if path == "/api/providers/forget":
                code, payload = self._providers_forget(body)
                return self._json(code, payload)
            if path == "/api/providers/custom":
                code, payload = self._providers_custom(body)
                return self._json(code, payload)
            if path == "/api/market/cancel":
                self.state.market_cancel = True
                return self._json(200, {"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        self._json(404, {"error": "no route"})

    def do_PATCH(self) -> None:  # noqa: N802
        if self._cors_preflight():
            return
        path = urlparse(self.path).path
        if not self._authed(path):
            return self._json(401, {"error": "bad token"})
        body = self._body()
        if path.startswith("/api/chats/"):
            sid = path.rsplit("/", 1)[-1]
            store = self.state.svc.sessions
            sess = store.get(sid)
            if not sess:
                return self._json(404, {"error": "not found"})
            if "title" in body:
                store.rename(sid, body["title"])
            if "pinned" in body:
                if bool(body["pinned"]) != sess.pinned:
                    store.toggle_pin(sid)
            if "tag" in body:
                store.add_tag(sid, body["tag"])
            return self._json(200, (store.get(sid) or sess).to_dict())
        if path == "/api/config":
            return self._config(body)
        self._json(404, {"error": "no route"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self._cors_preflight():
            return
        path = urlparse(self.path).path
        if not self._authed(path):
            return self._json(401, {"error": "bad token"})
        if path.startswith("/api/chats/"):
            sid = path.rsplit("/", 1)[-1]
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("export", ["1"])[0] == "1":
                sess = self.state.svc.sessions.get(sid)
                if sess:
                    export(self.state.svc.sessions, sess, "markdown")
            trash = self.state.svc.sessions.delete(sid)
            if self.state.session_id == sid:
                self.state.session_id = None
            return self._json(200, {"trashed": str(trash) if trash else None})
        self._json(404, {"error": "no route"})

    # ------------------------------------------------------------ helpers
    def _run(self, coro: Any) -> None:
        if self.state.loop is None:  # pragma: no cover
            raise RuntimeError("loop missing")
        fut = asyncio.run_coroutine_threadsafe(coro, self.state.loop)
        result = fut.result(timeout=120)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            self._json(result[0], result[1])
        else:
            self._json(200, result)

    def _static(self, name: str = "index.html") -> None:
        f = STATIC_DIR / name
        if not f.is_file():
            return self._json(404, {"error": f"missing {name}"})
        raw = f.read_bytes()
        if name == "index.html":
            boot = json.dumps({"token": self.state.token, "port": self._port(),
                               "first_run": bool(self.state.svc.config.first_run_completed)})
            raw = raw.replace(b"/*BOOT*/null", boot.encode())
        self.send_response(200)
        ctype = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css; charset=utf-8",
                 "app.js": "text/javascript; charset=utf-8"}[name]
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _port(self) -> int:
        return getattr(self.server, "omllm_port", 0)

    # ------------------------------------------------------------ routes
    async def _backends(self) -> dict:
        out: dict[str, dict[str, Any]] = {}
        for name in ("ollama", "lmstudio", "mock", "cloud"):
            be = self.state.svc.backend(name)
            try:
                ok, info = await be.health()
            except Exception as exc:  # noqa: BLE001
                ok, info = False, {"error": str(exc)}
            out[name] = {"installed": be.is_installed(), "running": ok,
                         "health": str(info.get("version") or info.get("models")
                                     or info.get("message") or info.get("base_url") or "")}
        return out

    async def _models(self, backend: str | None) -> dict:
        be = self.state.svc.backend(backend)
        out = []
        try:
            models = await be.list_models()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "models": [], "running": False}
        cfg = self.state.svc.config
        gpus = detect_gpus()
        total = sum(g.total_bytes for g in gpus) or None
        frees = [g.free_bytes for g in gpus if g.free_bytes is not None]
        free = sum(frees) if frees else None
        for m in models:
            est = m.vram_estimate(cfg.settings_for(be.name, m.name), total, free)
            out.append({
                "name": m.name, "size_bytes": m.size_bytes, "size": human_bytes(m.size_bytes),
                "quant": m.quant, "params_b": m.params_b, "ctx_len": m.ctx_len,
                "tool_capable": m.tool_capable, "why_weak": m.why_tool_weak(),
                "loaded": m.loaded,
                "vision": m.vision if m.vision is not None else _vision_cached(be.name.value, m.name),
                "thinking": m.thinking or _thinking_support(m.name, m.params_b),
                "vram_min": est.min_bytes if est else None,
                "vram_comfortable": est.comfortable_bytes if est else None,
                "fit": est.status if est else "unknown",
                "warnings": est.warnings if est else [],
                "notes": est.notes if est else [],
            })
        return {"models": out, "backend": be.name.value, "running": True,
                "default": self.state.svc.current_model(be.name.value)}

    def _set_model(self, body: dict) -> None:
        backend = body.get("backend") or self.state.svc.config.default_backend.value
        name = str(body.get("model", ""))
        section = getattr(self.state.svc.config, backend, None)
        if section is None or not name:
            return self._json(400, {"error": "bad request"})
        section.default_model = name
        self.state.svc.save_config()
        self._json(200, {"ok": True, "backend": backend, "model": name})

    async def _backend(self, body: dict) -> tuple[int, dict]:
        name = body.get("name")
        if name:
            try:
                self.state.svc.config.default_backend = BackendName(name)
            except ValueError:
                return 400, {"error": f"unknown backend {name!r}"}
            self.state.svc.save_config()
        if body.get("start"):
            be = self.state.svc.backend(name or None)
            res = await be.ensure_running()
            self.state.active_backend_ready = res.ok
            return 200, {"ok": res.ok, "message": res.message, "instructions": res.instructions}
        be = self.state.svc.backend(name or None)
        ok, info = await be.health()
        return 200, {"ok": ok, "info": info, "installed": be.is_installed()}

    def _mode(self, body: dict) -> None:
        try:
            mode = PermissionMode(str(body.get("mode", "")))
        except ValueError:
            return self._json(400, {"error": "unknown mode"})
        ok = self.state.svc.set_mode(mode, phrase=body.get("phrase"))
        if ok:
            self._json(200, {"ok": True, "mode": mode.value})
        else:
            self._json(403, {"error": f"full_access requires the exact phrase: {self.state.svc.full_access_phrase()!r}"})

    # ------------------------------------------------------------- providers
    def _revert_cloud_default_if_orphaned(self) -> None:
        from .config import BackendName
        from .providers import any_key, best_local_backend

        cfg = self.state.svc.config
        if cfg.default_backend is BackendName.CLOUD and not any_key(cfg.cloud):
            cfg.default_backend = best_local_backend(cfg)

    def _providers_state(self) -> dict:
        from .providers import PROVIDERS, key_for, mask

        cl = self.state.svc.config.cloud
        rows = []
        for p in PROVIDERS:
            key = key_for(p.pid, cl.keys)
            rows.append({"pid": p.pid, "name": p.name, "blurb": p.blurb, "kind": p.kind,
                         "base_url": p.base_url, "popular": p.popular, "recommended": p.recommended,
                         "signup_url": p.signup_url, "has_key": bool(key),
                         "masked": mask(key), "active": cl.provider == p.pid and bool(key),
                         "models": cl.model_lists.get(p.pid, "")})
        for pid, name in sorted(cl.customs.items()):
            key = key_for(pid, cl.keys)
            rows.append({"pid": pid, "name": name, "blurb": "custom OpenAI-compatible endpoint",
                         "kind": "openai", "base_url": cl.bases.get(pid, ""), "popular": False,
                         "recommended": False, "signup_url": "", "has_key": bool(key),
                         "masked": mask(key), "active": cl.provider == pid and bool(key),
                         "models": cl.model_lists.get(pid, "")})
        return {"providers": rows, "active": cl.provider,
                "default_backend": self.state.svc.config.default_backend.value}

    def _probe_models(self, pid: str) -> bool:
        """True when the provider's /models listing answers with entries.
        Gateways without a listing (e.g. Alibaba Token Plan) return False —
        the GUI then asks for model ids instead of pretending."""
        import httpx

        from .providers import get as _pg
        from .providers import is_custom, key_for

        cl = self.state.svc.config.cloud
        key = key_for(pid, cl.keys)
        if not key:
            return False
        if is_custom(pid) and pid in cl.customs:
            base = (cl.bases.get(pid) or "").rstrip("/")
            kind = "openai"
        else:
            p = _pg(pid)
            if p is None:
                return False
            base, kind = p.base_url.rstrip("/"), p.kind
        headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
                   if kind == "anthropic" else {"Authorization": f"Bearer {key}"})
        try:
            r = httpx.get(f"{base}/models", headers=headers, timeout=10.0)
            if r.status_code != 200:
                return False
            j = r.json()
            data = j.get("data") or j.get("models") or []
            return bool(data)
        except Exception:  # noqa: BLE001 — a probe failure is never fatal
            return False

    def _providers_key(self, body: dict) -> tuple[int, dict]:
        pid = str(body.get("pid") or "").strip()
        raw_key = body.get("key", "")
        if not isinstance(raw_key, str):
            return 400, {"error": "key must be a string (empty to remove)"}
        if len(raw_key) > 4096:
            return 400, {"error": "key unreasonably long"}
        key = raw_key.strip()
        if not pid:
            return 400, {"error": "pid required"}
        from .providers import get as _pg
        from .providers import is_custom

        # arbitrary pids would land in the TOML as exotic table keys and — before
        # the serializer hardening — could corrupt the whole config file
        if _pg(pid) is None and not (is_custom(pid) and pid in self.state.svc.config.cloud.customs):
            return 400, {"error": f"unknown provider {pid!r} — connect customs first"}
        cl = self.state.svc.config.cloud
        models = str(body.get("models") or "").strip()
        if models:
            cl.model_lists[pid] = models
        if key:
            cl.keys[pid] = key
            if not cl.provider:
                cl.provider = pid
        else:
            cl.keys.pop(pid, None)
            if cl.provider == pid:
                cl.provider = ""
            self._revert_cloud_default_if_orphaned()
        self.state.svc.save_config()
        payload = self._providers_state()
        # the GUI asks for a live /models probe (probe: true) so it only
        # falls back to asking for model ids when the gateway has no
        # listing; tests and other callers never touch the network here
        payload["models_ok"] = self._probe_models(pid) if (key and body.get("probe")) else None
        return 200, payload

    def _providers_use(self, body: dict) -> tuple[int, dict]:
        pid = str(body.get("pid") or "").strip()
        cl = self.state.svc.config.cloud
        from .providers import get as _pg
        from .providers import is_custom, key_for

        if _pg(pid) is None and not (is_custom(pid) and pid in cl.customs):
            return 400, {"error": f"unknown provider {pid!r}"}
        if not key_for(pid, cl.keys):
            return 400, {"error": "connect a key for this provider first"}
        cl.provider = pid
        self.state.svc.config.default_backend = BackendName.CLOUD
        self.state.svc.save_config()
        return 200, self._providers_state()

    def _providers_forget(self, body: dict) -> tuple[int, dict]:
        pid = str(body.get("pid") or "").strip()
        cl = self.state.svc.config.cloud
        cl.keys.pop(pid, None)
        if cl.provider == pid:
            cl.provider = ""  # a provider without a key is not "active"
        self._revert_cloud_default_if_orphaned()
        self.state.svc.save_config()
        return 200, self._providers_state()

    def _providers_custom(self, body: dict) -> tuple[int, dict]:
        import re as _re
        import time as _t

        name = str(body.get("name") or "").strip()[:40] or "Custom"
        base = str(body.get("base_url") or "").strip()
        from urllib.parse import urlparse

        if urlparse(base).scheme not in ("http", "https") or not urlparse(base).netloc:
            return 400, {"error": "base_url must be a valid http(s) URL"}
        base = base.rstrip("/")
        from urllib.parse import urlparse as _up

        if _up(base).path in ("", "/"):  # only a bare host gets the /v1 default;
            base += "/v1"                # /v2 or /api/anthropic stay as typed
        slug = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "provider"
        pid = f"custom-{slug}-{int(_t.time()) % 100000}"
        n = 2
        while pid in self.state.svc.config.cloud.customs:
            pid = f"custom-{slug}-{int(_t.time()) % 100000}-{n}"
            n += 1
        cl = self.state.svc.config.cloud
        cl.customs[pid] = name
        cl.bases[pid] = base
        models = str(body.get("models") or "").strip()
        if models:
            cl.model_lists[pid] = models
        self.state.svc.save_config()
        return 200, self._providers_state()

    async def _voice_transcribe(self, body: dict) -> tuple[int, dict]:
        """Hold-to-talk from the browser: MediaRecorder webm/opus arrives as
        base64, ffmpeg decodes it to the 16 kHz mono WAV the STT engines
        want, then the normal engine routing answers."""
        import asyncio
        import base64
        import shutil
        import tempfile

        from . import voice as V

        b64 = str(body.get("audio") or "")
        if not b64:
            return 400, {"error": "audio (base64) required"}
        if len(b64) > 40_000_000:
            return 400, {"error": "audio too large"}
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return 400, {"error": "audio is not valid base64"}
        ff = shutil.which("ffmpeg")
        if ff is None:
            return 500, {"error": "no ffmpeg on this machine — cannot decode browser audio"}
        tmp = Path(tempfile.mkdtemp(prefix="oml-voice-"))
        try:
            src = tmp / "in.bin"
            src.write_bytes(raw)
            wav = tmp / "in.wav"
            cp = await asyncio.create_subprocess_exec(
                ff, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(cp.communicate(), timeout=60)
            if cp.returncode or not wav.exists():
                return 500, {"error": "audio decode failed: "
                                      + err.decode(errors="replace")[-160:]}
            lang = self.state.svc.config.voice.language
            text, terr = await V.transcribe(wav, language=None if lang == "auto" else lang)
            if terr:
                return 502, {"error": terr}
            return 200, {"text": text}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _voice_record(self, body: dict) -> tuple[int, dict]:
        """Server-side hold-to-talk: the Tauri webview (and any browser the
        user has denied mic access to) cannot getUserMedia — but the bridge
        runs on this machine, so pw-record/arecord can. Ctrl+E in the GUI
        starts the SAME recorder the TUI uses; release stops + transcribes."""
        import asyncio
        import time

        from . import voice as V

        action = str(body.get("action") or "")
        if action == "start":
            if self.state.talk is not None and self.state.talk["rec"].running:
                return 200, {"ok": True, "already": True}
            wav = V.voice_dir() / f"gui-hold-{int(time.time() * 1000)}.wav"
            rec = V.Recorder(wav)
            self.state.talk_pending = True
            try:
                err = await asyncio.to_thread(rec.start)
            finally:
                self.state.talk_pending = False
            if err:
                return 500, {"error": err}
            self.state.talk = {"rec": rec, "path": wav}
            return 200, {"ok": True}
        if action == "stop":
            # a quick tap can release before the recorder finished spawning —
            # give a pending start up to 3s to land, then stop what exists
            for _ in range(30):
                if self.state.talk is not None or not self.state.talk_pending:
                    break
                await asyncio.sleep(0.1)
            talk, self.state.talk = self.state.talk, None
            if talk is None:
                return 200, {"text": "", "note": "not recording"}
            await asyncio.to_thread(talk["rec"].stop)
            wav = talk["path"]
            if not wav.exists() or wav.stat().st_size < 10_000:
                with contextlib.suppress(OSError):
                    wav.unlink(missing_ok=True)
                return 500, {"error": "too short — hold Ctrl+E, speak, release when done"}
            lang = self.state.svc.config.voice.language
            text, terr = await V.transcribe(wav, language=None if lang == "auto" else lang)
            with contextlib.suppress(OSError):
                wav.unlink(missing_ok=True)
            if terr:
                return 502, {"error": terr}
            return 200, {"text": text}
        return 400, {"error": "action must be start or stop"}

    async def _vision_probe(self, body: dict) -> tuple[int, dict]:
        from .vision import probe_vision, save_verdict

        model = str(body.get("model") or "")
        if not model:
            return 400, {"error": "model required"}
        be = self.state.svc.backend(body.get("backend"))
        try:
            verdict = await probe_vision(be, model)
        except Exception as exc:  # noqa: BLE001
            return 502, {"error": f"probe failed: {str(exc)[:160]}"}
        save_verdict(f"{be.name.value}:{model}", verdict)
        return 200, {"model": model, "vision": verdict}

    def _market(self) -> None:
        from .market import CATALOG, detect_vram_bytes, fit_verdict, resolve
        from .vram import detect_gpus

        vram = detect_vram_bytes()
        gpu = ""
        try:
            gpus = detect_gpus()
            if gpus:
                g = max(gpus, key=lambda g: g.total_bytes)
                if g.model and not g.model.endswith("GPU 0"):
                    gpu = g.model
        except Exception:  # noqa: BLE001
            pass
        rows = []
        for e in CATALOG:
            r = resolve(e)
            key, label = fit_verdict(r.size_gb, vram)
            rows.append({"mid": e.mid, "title": r.title, "size_gb": r.size_gb, "section": e.section,
                         "fit": key, "fit_label": label, "short": r.short,
                         "blurb": r.blurb, "repo": r.repo, "kind": r.kind,
                         "tags": list(r.tags)})
        self._json(200, {"vram_bytes": vram, "gpu": gpu, "entries": rows})

    def _market_download(self, body: dict) -> None:
        from .market import CATALOG, DownloadState

        mid = str(body.get("mid") or "")
        entry = next((e for e in CATALOG if e.mid == mid), None)
        if entry is None:
            return self._json(404, {"error": f"no market entry {mid!r}"})
        st = self.state
        if getattr(st, "market_thread", None) and st.market_thread.is_alive():
            return self._json(409, {"error": "a download is already running"})
        state = DownloadState(entry=entry)
        st.market_state = state
        st.market_cancel = False

        def run() -> None:
            import asyncio as _a

            from .market import download as dl

            try:
                _a.run(dl(entry, state, cancel=lambda: st.market_cancel))
            except Exception as exc:  # noqa: BLE001 — never leave market_thread set
                state.error = state.error or f"{type(exc).__name__}: {exc}"
                state.done = True
            finally:
                st.market_thread = None

        t = threading.Thread(target=run, daemon=True)
        st.market_thread = t
        t.start()
        self._json(200, {"ok": True, "started": entry.title})

    def _market_progress(self) -> None:
        st = self.state
        state = getattr(st, "market_state", None)
        if state is None:
            return self._json(200, {"active": False})
        th = getattr(st, "market_thread", None)
        alive = bool(th and th.is_alive())
        self._json(200, {
            "active": alive and not state.done and not state.error,
            "title": state.entry.title, "received": state.received, "total": state.total,
            "mbps": round(state.mbps, 2), "eta_s": round(state.eta_s),
            "done": state.done, "error": state.error})

    def _skill_cast(self, body: dict) -> None:
        svc = self.state.svc
        name = str(body.get("name") or "")
        sid = str(body.get("session") or "")
        sess = svc.sessions.get(sid) if sid else None
        if sess is None:
            return self._json(400, {"error": "unknown session"})
        content = svc.skills.read(name)
        if content is None:
            return self._json(404, {"error": f"no skill {name!r}",
                                    "skills": [sk.name for sk in svc.skills.list()]})
        sess.add_message("user", f'<skill name="{name}">\n{content}\n</skill>\n'
                         "Apply this skill to my requests in this chat.")
        svc.sessions.save(sess)
        self._json(200, {"ok": True, "cast": name})

    def _config(self, body: dict) -> None:
        cfg = self.state.svc.config
        body = dict(body)
        # API keys are owned by /api/providers/key — a config PATCH must never
        # write them (the GET we hand the UI is masked, and echoing that back
        # would silently destroy the real values)
        if "cloud" in body and isinstance(body["cloud"], dict):
            body["cloud"] = {k: v for k, v in body["cloud"].items() if k != "keys"}
        merged = {**json.loads(cfg.model_dump_json()), **body}
        if isinstance(merged.get("cloud"), dict) and isinstance(body.get("cloud"), dict):
            # merge section-wise so a partial {"cloud": {...}} PATCH cannot
            # erase bases/customs/model_lists it never mentioned
            base_cloud = merged["cloud"]
            live = json.loads(cfg.model_dump_json()).get("cloud", {})
            for field in ("bases", "customs", "model_lists"):
                if field in body["cloud"]:
                    continue
                base_cloud[field] = live.get(field, {})
            if "keys" not in body["cloud"]:
                base_cloud["keys"] = dict(cfg.cloud.keys)
        # never allow flipping to full_access over the plain config API
        if merged.get("permission_mode") == PermissionMode.FULL_ACCESS.value:
            merged["permission_mode"] = cfg.permission_mode.value
        try:
            new = Config.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": str(exc)[:400]})
        self.state.svc.apply_config(new)
        self._json(200, {"ok": True, "config": _safe_config(new)})

    def _soul(self, body: dict) -> None:
        soul = self.state.svc.soul
        action = body.get("action", "write")
        if action == "write":
            soul.backup()
            soul.path.write_text(str(body.get("text", "")), encoding="utf-8")
            return self._json(200, {"ok": True})
        if action == "revert":
            soul.revert(body["backup"] if body.get("backup") else None)
            return self._json(200, {"ok": True, "text": soul.read()})
        if action == "clear":
            soul.clear()
            return self._json(200, {"ok": True, "text": soul.read()})
        if action in ("append", "upsert"):
            res = getattr(soul, action)(body.get("section", "Important Notes"), body.get("text", ""))
            return self._json(200, res)
        self._json(400, {"error": "bad soul action"})

    def _chats(self, qs: dict) -> None:
        q = (qs.get("q", [""])[0] or "").strip()
        rows = self.state.svc.sessions.search(q) if q else self.state.svc.sessions.list()
        out = []
        for s in rows:
            out.append({"id": s.id, "title": s.title, "updated": s.updated_at, "created": s.created_at,
                        "backend": s.backend, "model": s.model, "mode": s.permission_mode,
                        "workspace": s.workspace, "messages": s.message_count, "tools": len(s.tool_calls),
                        "pinned": s.pinned, "tags": s.tags, "preview": s.preview})
        self._json(200, {"chats": out, "active": self.state.session_id})

    # ----------------------------------------------------------- streaming
    def _message(self, body: dict) -> None:
        text = str(body.get("text", "")).strip()
        if not text:
            return self._json(400, {"error": "empty message"})
        with self.state.turn_lock:
            if self.state.busy:
                # queue it into the running turn instead of refusing
                self.state.interjections.append(text)
                return self._json(202, {"queued": True, "count": len(self.state.interjections)})
            # claim NOW, on this thread: two rapid POSTs (double Enter, two
            # tabs) used to both see busy=False and run two turns over one
            # Session; and /api/cancel arriving before work() started was a
            # silent no-op because the event did not exist yet
            self.state.busy = True
            self.state.cancel = asyncio.Event()
        if body.get("new") or not self.state.session_id:
            sess = self.state.svc.new_session()
            self.state.session_id = sess.id
        sess = self.state.svc.sessions.get(self.state.session_id) or self.state.ensure_session()
        if body.get("session"):
            loaded = self.state.svc.sessions.get(str(body["session"]))
            if loaded:
                sess = loaded
                self.state.session_id = sess.id
        if body.get("new") or body.get("session"):
            # interjections queued for the PREVIOUS chat must never leak into
            # this one (a cancelled/short turn never drains the list)
            self.state.interjections.clear()

        self._sse_init()
        q: queue.Queue = queue.Queue()

        def on_event(ev: dict) -> None:
            q.put(("evt", ev))

        async def prompt_fn(req: Any) -> ApprovalOutcome:
            aid = secrets.token_hex(6)
            fut = self.state.loop.create_future() if self.state.loop else None
            if fut is None:
                return ApprovalOutcome.DENIED
            self.state.approvals[aid] = fut
            q.put(("approval", {
                "id": aid, "tool": req.tool, "args": req.args,
                "risk": req.decision.risk, "level": req.decision.level,
                "mode": self.state.svc.config.permission_mode.value,
                "reasons": list(req.decision.reasons), "preview": req.preview,
            }))
            try:
                return await asyncio.wait_for(fut, timeout=600)
            except TimeoutError:
                return ApprovalOutcome.DENIED
            finally:
                self.state.approvals.pop(aid, None)

        async def work() -> None:
            rt = self.state.svc.runtime()

            def done_payload(res: Any, *, cancelled: bool = False) -> tuple:
                return ("done", {
                    "text": res.text, "error": res.error or ("cancelled" if cancelled else None),
                    "steps": res.steps, "tokens_per_s": res.tokens_per_s, "calls": res.calls,
                    "usage": dict(rt.last_usage or {}), "model": sess.model or "",
                    "reasoning": res.reasoning[:6000], "tokens": res.tokens,
                    "ttft_s": res.ttft_s, "session": sess.id, "title": sess.title,
                })

            try:
                res = await rt.run_turn(
                    sess, text, prompt_fn=prompt_fn, on_event=on_event,
                    cancel=self.state.cancel, user_requested_memory=bool(body.get("memory_directive")),
                    interjections=self.state.interjections,
                )
                if sess.title in ("New chat", "", None):
                    new_title = make_title(text)
                    self.state.svc.sessions.rename(sess.id, new_title)
                    sess.title = new_title
                self.state.tok_s = res.tokens_per_s or self.state.tok_s
                self.state.busy = False
                q.put(done_payload(res))
            except AgentCancelled:
                # Stop pressed or the turn unwound: partial work is REAL —
                # persist it, and always emit done so the SSE writer exits
                # and the GUI composer unlocks (was: infinite pings, lost
                # turn, frozen UI — the P0 the audit called out)
                self.state.busy = False
                q.put(done_payload(TurnResult(text="", error=None, cancelled=True),
                                   cancelled=True))
            except Exception as exc:  # noqa: BLE001
                tb = traceback.extract_tb(exc.__traceback__)
                where = f" (at {tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno})" if tb else ""
                self.state.busy = False
                q.put(("error", {"message": f"{type(exc).__name__}: {exc}{where}"}))
            finally:
                # save AFTER the done/error event may already be in flight:
                # it is synchronous on the loop, so a next turn (same loop)
                # still cannot read the disk before this write lands —
                # but busy must not stay True past the visible end of turn
                try:
                    self.state.svc.sessions.save(sess)
                except Exception as exc:  # noqa: BLE001
                    # a lost turn is data loss — make it visible (bridge
                    # stderr lands in the log) instead of swallowing it
                    print(f"OMLLM_BRIDGE session save FAILED {sess.id}: {exc!r}",
                          file=sys.stderr, flush=True)
                self.state.busy = False

        assert self.state.loop is not None
        asyncio.run_coroutine_threadsafe(work(), self.state.loop)
        try:
            while True:
                try:
                    kind, payload = q.get(timeout=25)
                except queue.Empty:
                    self._sse("ping", {"t": time.time()})
                    continue
                if kind == "done":
                    self._sse("done", payload)
                    return
                if kind == "approval":
                    self._sse("approval_request", payload)
                    continue
                self._sse(str(payload.get("type", kind)), payload)
                if payload.get("type") == "error":
                    # keep stream open; runtime continues its own loop
                    pass
        except (BrokenPipeError, OSError):
            if self.state.cancel:
                self.state.cancel.set()

    def _approval(self, aid: str, body: dict) -> None:
        fut = self.state.approvals.get(aid)
        if fut is None or fut.done():
            return self._json(404, {"error": "no pending approval"})
        mapping = {
            "approve": ApprovalOutcome.APPROVED,
            "approved": ApprovalOutcome.APPROVED,
            "deny": ApprovalOutcome.DENIED,
            "denied": ApprovalOutcome.DENIED,
            "session": ApprovalOutcome.APPROVED_SESSION,
            "always": ApprovalOutcome.APPROVED_ALWAYS,
        }
        outcome = mapping.get(str(body.get("decision", "")).lower())
        if outcome is None:
            return self._json(400, {"error": "bad decision"})
        assert self.state.loop is not None
        self.state.loop.call_soon_threadsafe(fut.set_result, outcome)
        self._json(200, {"ok": True})

    def _pull(self, body: dict) -> None:
        be = self.state.svc.backend(body.get("backend"))
        name = str(body.get("name", "")).strip()
        if not name:
            return self._json(400, {"error": "name required"})
        self._sse_init()
        q: queue.Queue = queue.Queue()

        def prog(status: str, pct: float | None) -> None:
            q.put(("progress", {"status": status, "pct": pct}))

        async def work() -> None:
            try:
                await be.pull(name, prog)
                q.put(("done", {"ok": True, "name": name}))
            except Exception as exc:  # noqa: BLE001
                q.put(("done", {"ok": False, "error": str(exc)}))

        assert self.state.loop is not None
        asyncio.run_coroutine_threadsafe(work(), self.state.loop)
        try:
            while True:
                kind, payload = q.get(timeout=600)
                self._sse(kind, payload)
                if kind == "done":
                    return
        except (BrokenPipeError, OSError, queue.Empty):
            return


def _safe_config(cfg: Config) -> dict:
    """Config as JSON for the UI — provider API keys replaced by their mask."""
    from .providers import mask

    d = json.loads(cfg.model_dump_json())
    cloud = d.get("cloud")
    if isinstance(cloud, dict) and isinstance(cloud.get("keys"), dict):
        cloud["keys"] = {k: mask(v) for k, v in cloud["keys"].items()}
    return d


def serve(workspace: str | None = None, port: int = 0, token_file: str | None = None,
          write_port_file: str | None = None, stop: threading.Event | None = None) -> None:
    """Run the bridge until killed. Used by `ohmyllm gui` and the Tauri shell."""
    state = BridgeState(workspace)
    loop = asyncio.new_event_loop()

    async def heartbeat() -> None:
        # keeps a loop thread alive & warms backend status periodically
        while True:
            try:
                be = state.svc.backend()
                ok, _ = await be.health()
                state.active_backend_ready = ok
            except Exception:  # noqa: BLE001
                state.active_backend_ready = False
            await asyncio.sleep(8)

    def loop_main() -> None:
        asyncio.set_event_loop(loop)
        state.loop = loop
        loop.create_task(heartbeat())
        loop.run_forever()

    t = threading.Thread(target=loop_main, daemon=True)
    t.start()
    while state.loop is None:
        time.sleep(0.01)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.state = state  # type: ignore[attr-defined]
    actual = httpd.server_address[1]
    httpd.omllm_port = actual  # type: ignore[attr-defined]
    info = f"{actual} {state.token}"

    def _secret_write(path: str) -> None:
        # the file holds the bridge token — owner-only. Written directly:
        # os.fchmod does not exist on Windows (AttributeError killed the serve
        # thread and the port file never appeared), and temp+rename raced AV
        # file-locks on the Windows CI runners.
        import os

        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(info)
        except OSError:
            Path(path).write_text(info, encoding="utf-8")  # exotic FS without modes

    if write_port_file:
        _secret_write(write_port_file)
    if token_file:
        _secret_write(token_file)
    print(f"OMLLM_BRIDGE port={actual} token={state.token}", flush=True)
    _done = False

    def _graceful() -> None:
        """Eject the model (frees VRAM), stop the loop, exit serve()."""
        nonlocal _done
        if _done:
            return
        _done = True

        def _work() -> None:
            try:
                fut = asyncio.run_coroutine_threadsafe(state.svc.release_model(), loop)
                fut.result(timeout=20)
            except Exception:  # noqa: BLE001 — a dead server must not block quit
                pass
            httpd.shutdown()
        threading.Thread(target=_work, daemon=True).start()

    state.shutdown_hook = _graceful
    import signal as _signal

    def _on_sig(_signum: int, _frame: object) -> None:
        _graceful()

    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):  # non-main thread / unsupported
            _signal.signal(_sig, _on_sig)
    if stop is not None:
        def _watch() -> None:
            stop.wait()
            _graceful()
        threading.Thread(target=_watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="ohmyllm-bridge")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OMLLM_GUI_PORT", "0")))
    ap.add_argument("--workspace", default=os.environ.get("OMLLM_WORKSPACE") or None)
    ap.add_argument("--port-file", default=os.environ.get("OMLLM_GUI_PORT_FILE") or None)
    ap.add_argument("--token-file", default=None)
    args = ap.parse_args(argv)
    serve(args.workspace, args.port, args.token_file, args.port_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
