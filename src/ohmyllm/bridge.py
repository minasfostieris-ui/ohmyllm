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
from .config import BackendName, Config, PermissionMode
from .core import Services
from .doctor import run_checks, summarize
from .safety import ApprovalOutcome
from .sessions.export import export
from .sessions.store import make_title
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
        self.interjections: list[str] = []
        self.tok_s = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.active_backend_ready: bool | None = None
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
            ctx_window=self.svc.config.settings_for(
                self.svc.config.default_backend, self.svc.current_model()).num_ctx,
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
            paths.app_log_path().parent.mkdir(parents=True, exist_ok=True)
            with open(paths.app_log_path(), "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} gui {fmt % args}\n")

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
                return self._json(200, self.state.status())
            if path == "/api/skills":
                return self._json(200, {"skills": [
                    {"name": sk.name, "description": sk.description} for sk in self.state.svc.skills.list()]})
            if path == "/api/config":
                return self._json(200, json.loads(self.state.svc.config.model_dump_json()))
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
                if self.state.cancel:
                    self.state.cancel.set()
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
        if path == "/api/skills/cast":
            return self._skill_cast(body)
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
        for name in ("ollama", "lmstudio", "mock"):
            be = self.state.svc.backend(name)
            try:
                ok, info = await be.health()
            except Exception as exc:  # noqa: BLE001
                ok, info = False, {"error": str(exc)}
            out[name] = {"installed": be.is_installed(), "running": ok,
                         "health": str(info.get("version") or info.get("message") or "")}
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
        merged = {**json.loads(cfg.model_dump_json()), **body}
        # never allow flipping to full_access over the plain config API
        if merged.get("permission_mode") == PermissionMode.FULL_ACCESS.value:
            merged["permission_mode"] = cfg.permission_mode.value
        try:
            new = Config.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": str(exc)[:400]})
        self.state.svc.apply_config(new)
        self._json(200, {"ok": True, "config": json.loads(new.model_dump_json())})

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
        if self.state.busy:
            # queue it into the running turn instead of refusing
            self.state.interjections.append(text)
            return self._json(202, {"queued": True, "count": len(self.state.interjections)})
        if body.get("new") or not self.state.session_id:
            sess = self.state.svc.new_session()
            self.state.session_id = sess.id
        sess = self.state.svc.sessions.get(self.state.session_id) or self.state.ensure_session()
        if body.get("session"):
            loaded = self.state.svc.sessions.get(str(body["session"]))
            if loaded:
                sess = loaded
                self.state.session_id = sess.id

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
            self.state.busy = True
            self.state.cancel = asyncio.Event()
            try:
                res = await rt.run_turn(
                    sess, text, prompt_fn=prompt_fn, on_event=on_event,
                    cancel=self.state.cancel, user_requested_memory=bool(body.get("memory_directive")),
                    interjections=self.state.interjections,
                )
                self.state.svc.sessions.save(sess)
                if sess.title in ("New chat", "", None):
                    new_title = make_title(text)
                    self.state.svc.sessions.rename(sess.id, new_title)
                    sess.title = new_title
                self.state.tok_s = res.tokens_per_s or self.state.tok_s
                q.put(("done", {"text": res.text, "error": res.error, "steps": res.steps,
                                "tokens_per_s": res.tokens_per_s, "calls": res.calls,
                                "usage": res.usage, "model": sess.model or "",
                                "reasoning": res.reasoning[:6000],
                                "session": sess.id, "title": sess.title}))
            except Exception as exc:  # noqa: BLE001
                tb = traceback.extract_tb(exc.__traceback__)
                where = f" (at {tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno})" if tb else ""
                q.put(("error", {"message": f"{type(exc).__name__}: {exc}{where}"}))
            finally:
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
    if write_port_file:
        Path(write_port_file).write_text(info, encoding="utf-8")
    if token_file:
        Path(token_file).write_text(info, encoding="utf-8")
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
