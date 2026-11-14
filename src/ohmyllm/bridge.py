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
import hmac
import json
import math
import os
import queue
import secrets
import socket
import stat
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, paths
from .agents.runtime import AgentCancelled
from .capabilities import thinking_support as _thinking_support
from .config import BackendName, Config, PermissionMode, deep_merge
from .core import Services
from .doctor import run_checks, summarize
from .safety import ApprovalOutcome
from .secureio import append_private, atomic_write, read_private_json, rotate_private_log
from .sessions.export import export
from .sessions.store import make_title
from .vision import cached_verdict as _vision_cached
from .vram.detect import detect_gpus, human_bytes

STATIC_DIR = Path(__file__).resolve().parent / "gui_static"
_MAX_BODY_BYTES = 1_048_576
# /api/voice/transcribe accepts base64 audio up to 40 MB (handler-enforced);
# its body cap must leave room for that payload plus the JSON envelope, while
# every other route keeps the global 1 MiB limit.
_ROUTE_BODY_CAPS = {"/api/voice/transcribe": 41_000_000}
_MAX_REQUEST_THREADS = 16
_MAX_EVENT_QUEUE = 256
_MAX_INTERJECTIONS = 16
_MAX_MESSAGE_CHARS = 65_536
_SSE_HEARTBEAT_SECONDS = 1.0
_HEADER_READ_TIMEOUT = 5.0
_BODY_READ_TIMEOUT = 10.0
_CORS_HEADERS = {
    "Access-Control-Allow-Headers": "Content-Type, X-OMLLM-Token",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
}
_PUBLIC_PATHS = {"/", "/index.html", "/api/health", "/app.css", "/styles.css", "/app.js", "/boot.js"}
_BODY_FIELDS = {
    ("POST", "/api/message"): {"text", "new", "session", "memory_directive"},
    ("POST", "/api/cancel"): set(),
    ("POST", "/api/mode"): {"mode", "phrase"},
    ("POST", "/api/backend"): {"name", "start"},
    ("POST", "/api/model"): {"backend", "model"},
    ("POST", "/api/config/memory"): {"enabled", "agent_writes", "require_explicit_save",
                                       "max_inject_chars", "backups_keep"},
    ("POST", "/api/config/agent"): {"max_steps", "max_parse_retries", "tool_timeout", "native_tools",
                                      "react_fallback", "compact", "compact_ratio", "thinking_visible",
                                      "agent_loop", "tone", "custom_tones", "op_mode", "yolo", "confirm"},
    ("POST", "/api/soul"): {"action", "text", "backup", "section"},
    ("POST", "/api/chats"): {"first_message"},
    ("POST", "/api/shutdown"): set(),
    ("POST", "/api/pull"): {"backend", "name"},
    ("POST", "/api/steer"): {"text"},
}


class RequestBodyError(ValueError):
    """A malformed request body that must be rejected without coercion."""


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
        self.active_turn: Any = None
        self.active_runtime: Any = None  # live AgentRuntime while a turn runs (D4/D6)
        self.deleted_sessions: set[str] = set()  # D8: tombstones — a deleted chat must not resurrect
        self.heartbeat_task: Any = None  # D15: cancelled explicitly on shutdown

    def cancel_active(self) -> None:
        """Cancel the active turn and deny every bridge-owned pending prompt."""
        with self.turn_lock:
            if self.cancel is not None:
                self.cancel.set()
            active = self.active_turn
        if self.loop is not None:
            def deny_pending() -> None:
                for future in tuple(self.approvals.values()):
                    # Runs ON the loop thread; re-check done() so a concurrent
                    # approval decision can never be double-resolved.
                    if not future.done():
                        future.set_result(ApprovalOutcome.DENIED)
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(deny_pending)
        if active is not None:
            active.cancel()

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
    def setup(self) -> None:
        super().setup()
        self._headers_complete = threading.Event()
        self._header_timer = threading.Timer(_HEADER_READ_TIMEOUT, self._expire_header_read)
        self._header_timer.daemon = True
        self._header_timer.start()

    def _expire_header_read(self) -> None:
        if self._headers_complete.is_set():
            return
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.connection.close()

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            self._headers_complete.set()
            self._header_timer.cancel()

    def finish(self) -> None:
        self._headers_complete.set()
        self._header_timer.cancel()
        super().finish()

    @property
    def state(self) -> BridgeState:  # per-server; testable isolation
        s = getattr(self.server, "state", None)
        if s is None:
            raise RuntimeError("bridge state not bound")
        return s
    server_version = f"OhMyLLM-Bridge/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr spam
        # Never persist queries: legacy clients may still put credentials there.
        with contextlib.suppress(OSError, TypeError, ValueError):
            path = urlparse(self.path).path[:500]
            line = f"{time.strftime('%H:%M:%S')} gui {self.command} {path}\n"
            log_path = paths.app_log_path()
            rotate_private_log(log_path)
            append_private(log_path, line, max_bytes=2048)

    def _origin_ok(self) -> bool:
        """Accept an absent Origin or this bridge's exact loopback origin."""
        origin_raw = self.headers.get("Origin")
        if not origin_raw:
            return True
        try:
            origin = urlparse(origin_raw)
            origin_host = (origin.hostname or "").casefold().rstrip(".")
            return (
                origin.scheme == "http"
                and origin_host in {"127.0.0.1", "localhost", "::1"}
                and origin.port == self._port()
                and origin.username is None
                and origin.password is None
                and not origin.path
                and not origin.params
                and not origin.query
                and not origin.fragment
            )
        except ValueError:
            return False

    def _send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        origin = self.headers.get("Origin")
        # Never reflect an Origin that failed the loopback boundary check, even
        # on the error response explaining that failure.
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for key, value in _CORS_HEADERS.items():
            self.send_header(key, value)

    def _request_boundary_ok(self) -> bool:
        """Accept only this bridge's loopback Host and a matching browser Origin."""
        try:
            host = urlparse("//" + (self.headers.get("Host") or ""))
            host_name = (host.hostname or "").casefold().rstrip(".")
            host_ok = (
                host_name in {"127.0.0.1", "localhost", "::1"}
                and host.port == self._port()
                and host.username is None
                and host.password is None
                and not host.path
                and not host.query
                and not host.fragment
            )
            return host_ok and self._origin_ok()
        except ValueError:
            return False

    def _cors_preflight(self) -> bool:
        if self.command == "OPTIONS":
            self.send_response(204)
            self._send_security_headers()
            self.end_headers()
            return True
        return False

    def _authed(self, path: str) -> bool:
        if path in _PUBLIC_PATHS:
            return True
        token = self.headers.get("X-OMLLM-Token", "")
        return bool(token) and hmac.compare_digest(token, self.state.token)

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
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _body(self, path: str) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise RequestBodyError("transfer-encoded request bodies are not accepted")
        length = self.headers.get("Content-Length")
        if length is None:
            n = 0
        elif not length.isascii() or not length.isdigit():
            raise RequestBodyError("invalid Content-Length")
        else:
            n = int(length)
        if n > _ROUTE_BODY_CAPS.get(path, _MAX_BODY_BYTES):
            raise RequestBodyError("request body is too large")
        if n and (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            raise RequestBodyError("Content-Type must be application/json")
        if n:
            deadline = time.monotonic() + _BODY_READ_TIMEOUT
            chunks: list[bytes] = []
            remaining = n
            previous_timeout = self.connection.gettimeout()
            try:
                while remaining:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise RequestBodyError("request body read deadline exceeded")
                    self.connection.settimeout(min(_HEADER_READ_TIMEOUT, left))
                    # read1 performs at most one raw socket read. Buffered read(n)
                    # can wait to fill n while a peer trickles bytes indefinitely.
                    chunk = self.rfile.read1(min(remaining, 65_536))
                    if time.monotonic() >= deadline:
                        raise RequestBodyError("request body read deadline exceeded")
                    if not chunk:
                        raise RequestBodyError("incomplete request body")
                    chunks.append(chunk)
                    remaining -= len(chunk)
            except TimeoutError as exc:
                raise RequestBodyError("request body read deadline exceeded") from exc
            finally:
                with contextlib.suppress(OSError):
                    self.connection.settimeout(previous_timeout)
            raw = b"".join(chunks)
        else:
            raw = b"{}"

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            obj: dict[str, Any] = {}
            for key, value in pairs:
                if key in obj:
                    raise RequestBodyError(f"duplicate JSON field: {key}")
                obj[key] = value
            return obj

        try:
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(RequestBodyError(f"nonfinite number: {value}")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestBodyError("malformed JSON body") from exc
        if type(parsed) is not dict:
            raise RequestBodyError("JSON body must be an object")

        def reject_nonfinite(value: Any) -> None:
            if type(value) is float and not math.isfinite(value):
                raise RequestBodyError("nonfinite numbers are not accepted")
            if type(value) is dict:
                for nested in value.values():
                    reject_nonfinite(nested)
            elif type(value) is list:
                for nested in value:
                    reject_nonfinite(nested)

        reject_nonfinite(parsed)
        allowed = _BODY_FIELDS.get((self.command, path))
        if self.command == "POST" and path.startswith("/api/approval/"):
            allowed = {"decision"}
        elif self.command == "POST" and path.startswith("/api/chats/") and path.endswith("/export"):
            allowed = {"format"}
        elif self.command == "PATCH" and path.startswith("/api/chats/"):
            allowed = {"title", "pinned", "tag"}
        elif self.command == "PATCH" and path == "/api/config":
            allowed = set(Config.model_fields)
        if allowed is not None:
            unknown = set(parsed) - allowed
            if unknown:
                raise RequestBodyError("unknown JSON fields: " + ", ".join(sorted(unknown)))
        self._validate_body_types(path, parsed)
        return parsed

    def _validate_body_types(self, path: str, body: dict[str, Any]) -> None:
        """Reject JSON coercion and oversized route fields before side effects."""
        rules: dict[str, tuple[type, int | None]] = {}
        if path == "/api/message":
            rules = {"text": (str, _MAX_MESSAGE_CHARS), "new": (bool, None),
                     "session": (str, 128), "memory_directive": (bool, None)}
        elif path == "/api/mode":
            rules = {"mode": (str, 32), "phrase": (str, 128)}
        elif path == "/api/backend":
            rules = {"name": (str, 32), "start": (bool, None)}
        elif path == "/api/model":
            rules = {"backend": (str, 32), "model": (str, 512)}
        elif path == "/api/config/memory":
            rules = {"enabled": (bool, None), "agent_writes": (bool, None),
                     "require_explicit_save": (bool, None), "max_inject_chars": (int, None),
                     "backups_keep": (int, None)}
        elif path == "/api/config/agent":
            rules = {"max_steps": (int, None), "max_parse_retries": (int, None),
                     "tool_timeout": (float, None), "native_tools": (str, 16),
                     "react_fallback": (bool, None), "compact": (bool, None),
                     "compact_ratio": (float, None), "thinking_visible": (bool, None),
                     "agent_loop": (bool, None), "tone": (str, 64),
                     "op_mode": (str, 16), "yolo": (bool, None), "confirm": (bool, None)}
            tones = body.get("custom_tones")
            if tones is not None:
                if type(tones) is not dict or len(tones) > 64:
                    raise RequestBodyError("custom_tones must be an object (max 64 entries)")
                for tname, tfrag in tones.items():
                    if type(tname) is not str or type(tfrag) is not str or len(tfrag) > 1500:
                        raise RequestBodyError("custom_tones entries must be strings (fragment <= 1500 chars)")
        elif path == "/api/soul":
            rules = {"action": (str, 16), "text": (str, _MAX_BODY_BYTES),
                     "backup": (str, 255), "section": (str, 200)}
        elif path == "/api/chats":
            rules = {"first_message": (str, _MAX_MESSAGE_CHARS)}
        elif path == "/api/pull":
            rules = {"backend": (str, 32), "name": (str, 512)}
        elif path == "/api/steer":
            rules = {"text": (str, _MAX_MESSAGE_CHARS)}
        elif path.startswith("/api/approval/"):
            rules = {"decision": (str, 32)}
        elif path.startswith("/api/chats/") and path.endswith("/export"):
            rules = {"format": (str, 16)}
        elif self.command == "PATCH" and path.startswith("/api/chats/"):
            rules = {"title": (str, 500), "pinned": (bool, None), "tag": (str, 100)}
        for key, value in body.items():
            rule = rules.get(key)
            if rule is None:
                continue
            expected, max_chars = rule
            if type(value) is not expected:
                raise RequestBodyError(f"{key} must be a {expected.__name__}")
            if max_chars is not None and type(value) is str and len(value) > max_chars:
                raise RequestBodyError(f"{key} is too large")

    def _sse_init(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self._send_security_headers()
        self.end_headers()

    def _sse(self, event: str, data: Any) -> None:
        self.wfile.write(
            f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n".encode()
        )
        self.wfile.flush()

    # ------------------------------------------------------------- routing
    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._request_boundary_ok():
            return self._json(403, {"error": "invalid loopback Host or Origin"})
        self._cors_preflight()

    def do_GET(self) -> None:  # noqa: N802
        if not self._request_boundary_ok():
            return self._json(403, {"error": "invalid loopback Host or Origin"})
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
            if path == "/boot.js":
                return self._boot_js()
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
                raw_n = qs.get("n", ["50"])[0]
                if not raw_n.isascii() or not raw_n.isdigit() or int(raw_n) > 500:
                    return self._json(400, {"error": "n must be an integer from 0 to 500"})
                return self._json(200, {"events": self.state.svc.audit.recent(int(raw_n))})
            if path == "/api/approvals":
                return self._json(200, {"rules": self.state.svc.approvals.permanent_rules})
            if path == "/api/state":
                return self._json(200, self._machine_state())
            if path == "/api/search":
                return self._search(qs)
        except Exception as exc:  # noqa: BLE001 — API must never die mid-render
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_boundary_ok():
            return self._json(403, {"error": "invalid loopback Host or Origin"})
        if self._cors_preflight():
            return
        u = urlparse(self.path)
        path = u.path
        if not self._authed(path):
            return self._json(401, {"error": "bad token"})
        try:
            body = self._body(path)
        except RequestBodyError as exc:
            return self._json(400, {"error": str(exc)})
        try:
            if path == "/api/message":
                return self._message(body)
            if path == "/api/cancel":
                self.state.cancel_active()
                return self._json(200, {"ok": True})
            if path == "/api/steer":
                return self._steer(body)
            if path.startswith("/api/approval/"):
                return self._approval(path.rsplit("/", 1)[-1], body)
            if path == "/api/mode":
                return self._mode(body)
            if path == "/api/backend":
                return self._run(self._backend(body))
            if path == "/api/model":
                return self._set_model(body)
            if path == "/api/config/memory":
                return self._memory_config(body)
            if path == "/api/config/agent":
                return self._agent_config(body)
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
        if not self._request_boundary_ok():
            return self._json(403, {"error": "invalid loopback Host or Origin"})
        if self._cors_preflight():
            return
        path = urlparse(self.path).path
        if not self._authed(path):
            return self._json(401, {"error": "bad token"})
        try:
            body = self._body(path)
        except RequestBodyError as exc:
            return self._json(400, {"error": str(exc)})
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
        if not self._request_boundary_ok():
            return self._json(403, {"error": "invalid loopback Host or Origin"})
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
            if trash:
                # D8: tombstone the id so an in-flight turn's finally-save
                # cannot resurrect the chat; cancel it if it is still running.
                self.state.deleted_sessions.add(sid)
                if self.state.session_id == sid and self.state.busy:
                    self.state.cancel_active()
            if self.state.session_id == sid:
                self.state.session_id = None
            return self._json(200, {"trashed": str(trash) if trash else None})
        self._json(404, {"error": "no route"})

    # ------------------------------------------------------------ helpers
    def _run(self, coro: Any) -> None:
        if self.state.loop is None:  # pragma: no cover
            raise RuntimeError("loop missing")
        fut = asyncio.run_coroutine_threadsafe(coro, self.state.loop)
        try:
            result = fut.result(timeout=120)
        except TimeoutError:
            fut.cancel()  # D9: a timed-out request must not keep running orphaned
            raise
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            self._json(result[0], result[1])
        else:
            self._json(200, result)

    def _static(self, name: str = "index.html") -> None:
        f = STATIC_DIR / name
        if not f.is_file():
            return self._json(404, {"error": f"missing {name}"})
        raw = f.read_bytes()
        self.send_response(200)
        ctype = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css; charset=utf-8",
                 "app.js": "text/javascript; charset=utf-8"}[name]
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _boot_js(self) -> None:
        """External boot script (L3): keeps index.html free of inline JS so
        the CSP can drop 'unsafe-inline'. The token arrives only in the URL
        fragment and is never sent in a request."""
        boot = json.dumps({"port": self._port(),
                           "first_run": bool(self.state.svc.config.first_run_completed)})
        raw = f"window.__OMLLM__ = {boot};\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._send_security_headers()
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
        if backend is not None and backend not in {name.value for name in BackendName}:
            return {"error": "unknown backend", "models": [], "running": False}
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
        updated = self.state.svc.config.model_copy(deep=True)
        section = getattr(updated, backend, None)
        if section is None or not name:
            return self._json(400, {"error": "bad request"})
        section.default_model = name
        if backend != updated.default_backend.value:
            # mirror TUI/plain semantics: choosing a model on another backend
            # switches the default too, else the next turn silently runs on
            # the old backend's model
            try:
                updated.default_backend = BackendName(backend)
            except ValueError:
                return self._json(400, {"error": f"unknown backend {backend!r}"})
        self.state.svc.apply_config(updated)
        self._json(200, {"ok": True, "backend": backend, "model": name})

    async def _backend(self, body: dict) -> tuple[int, dict]:
        name = body.get("name")
        if name:
            updated = self.state.svc.config.model_copy(deep=True)
            try:
                updated.default_backend = BackendName(name)
            except ValueError:
                return 400, {"error": f"unknown backend {name!r}"}
            self.state.svc.apply_config(updated)
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
            key, label = fit_verdict(r.size_gb, vram, r.hybrid)
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
        # mail password is owned by `oml mail setup` — same echo-back trap
        if "mail" in body and isinstance(body["mail"], dict):
            body["mail"] = {k: v for k, v in body["mail"].items() if k != "password"}
        live = json.loads(cfg.model_dump_json())
        # Security transitions have dedicated, confirmation-aware controls; the
        # generic endpoint may echo these sections but must never alter them.
        # agent/cloud/mail/default_backend joined the protected set (H2): they
        # carry yolo/op_mode/tone injection, endpoint redirection and the mail
        # exfiltration channel. Comparison runs on the MERGED result so masked
        # echo-backs (stripped keys/passwords) still pass through untouched.
        protected = {
            "version", "policy_generation", "permission_mode", "workspace", "memory",
            "safety", "guard", "logging", "ollama", "lmstudio",
            "agent", "cloud", "mail", "default_backend",
        }
        merged = deep_merge(live, body)
        changed = sorted(key for key in protected if merged.get(key) != live.get(key))
        if changed:
            return self._json(403, {"error": "security config requires a dedicated control: " + ", ".join(changed)})
        try:
            new = Config.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": _validation_sentence(exc)})
        self.state.svc.apply_config(new)
        self._json(200, {"ok": True, "config": _safe_config(new)})

    def _agent_config(self, body: dict[str, Any]) -> None:
        """Dedicated agent-policy control (H2). yolo/op_mode/tone/custom_tones
        are security-sensitive: changing them requires an explicit
        ``confirm: true`` (the GUI sends it only from a user-driven save).
        The generic /api/config endpoint refuses these fields entirely."""
        svc = self.state.svc
        live = json.loads(svc.config.model_dump_json())
        live_agent = dict(live.get("agent") or {})
        live_agent["yolo"] = bool(svc.config.agent.yolo)  # never in dumps
        incoming = {k: v for k, v in body.items() if k != "confirm"}
        sensitive = {"yolo", "op_mode", "tone", "custom_tones"}
        changed = sorted(k for k in sensitive if k in incoming and incoming[k] != live_agent.get(k))
        if changed and body.get("confirm") is not True:
            return self._json(403, {"error": "agent policy change requires confirm=true: " + ", ".join(changed)})
        merged = deep_merge(live, {"agent": incoming})
        try:
            new = Config.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": _validation_sentence(exc)})
        svc.apply_config(new)
        svc.audit.log(
            mode=svc.config.permission_mode.value, tool="__agent_config__",
            args={"changed": sorted(incoming)}, approval="user", success=True,
            duration_ms=0, event="agent_config_changed",
        )
        self._json(200, {"ok": True, "config": _safe_config(new)})

    def _memory_config(self, body: dict[str, Any]) -> None:
        """Apply explicit memory-policy changes through a dedicated control."""
        live = json.loads(self.state.svc.config.model_dump_json())
        merged = deep_merge(live, {"memory": body})
        try:
            new = Config.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": _validation_sentence(exc)})
        self.state.svc.apply_config(new)
        self._json(200, {"ok": True, "config": _safe_config(new)})

    def _soul(self, body: dict) -> None:
        soul = self.state.svc.soul
        action = body.get("action", "write")
        if action == "write":
            text = body.get("text", "")
            if not isinstance(text, str):
                return self._json(400, {"error": "text must be a string"})
            soul.write(text)
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
        raw_query = qs.get("q", [""])[0]
        if not isinstance(raw_query, str) or len(raw_query) > 1000:
            return self._json(400, {"error": "chat query is too large"})
        q = raw_query.strip()
        rows = self.state.svc.sessions.search(q) if q else self.state.svc.sessions.list()
        out = []
        for s in rows:
            out.append({"id": s.id, "title": s.title, "updated": s.updated_at, "created": s.created_at,
                        "backend": s.backend, "model": s.model, "mode": s.permission_mode,
                        "workspace": s.workspace, "messages": s.message_count, "tools": len(s.tool_calls),
                        "pinned": s.pinned, "tags": s.tags, "preview": s.preview})
        self._json(200, {"chats": out, "active": self.state.session_id})

    def _machine_state(self) -> dict[str, Any]:
        """D4: live runtime snapshot mid-turn, durable state.json otherwise."""
        rt = self.state.active_runtime
        if rt is not None and getattr(rt, "turn_active", False):
            snap: dict[str, Any] = rt.machine_state()
            # Live id is the DECIDABLE SSE approval aid (answered via /api/approval)
            # so supervisors can correlate state with the approval stream; the
            # durable state.json (CLI/TUI) keeps the runtime's internal id.
            if snap.get("pending_approval_id") and self.state.approvals:
                snap["pending_approval_id"] = next(iter(self.state.approvals))
            snap["stale"] = False  # a live turn is fresh by definition
        else:
            snap = {"state": "idle", "session_id": None, "turn": 0, "backend": "",
                    "model": "", "pending_approval_id": None, "seq": 0, "ts": 0.0}
            disk: Any = None
            with contextlib.suppress(OSError, ValueError):
                disk = read_private_json(paths.global_home() / "state.json")
            if isinstance(disk, dict):
                for key in snap:
                    if key in disk:
                        snap[key] = disk[key]
            # Additive key: a crashed agent leaves "working" on disk forever.
            # Flag it with the SAME 300s threshold the CLI state command uses.
            ts = float(snap.get("ts") or 0.0)
            snap["stale"] = bool(
                snap.get("state") in ("working", "blocked")
                and ts > 0
                and (time.time() - ts) > 300
            )
        return snap

    def _search(self, qs: dict) -> None:
        """D2: full-text session search; bounded query and result count."""
        q = qs.get("q", [""])[0]
        if not isinstance(q, str) or len(q) > 512:
            return self._json(400, {"error": "q must be a string of at most 512 characters"})
        raw_limit = qs.get("limit", ["10"])[0]
        if not raw_limit.isascii() or not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 50:
            return self._json(400, {"error": "limit must be an integer from 1 to 50"})
        hits = self.state.svc.sessions.search(q.strip(), int(raw_limit))
        return self._json(200, {"results": hits})

    def _steer(self, body: dict) -> None:
        """D6: queue mid-turn guidance; never touches mode, tools or approvals."""
        rt = self.state.active_runtime
        if rt is None or not getattr(rt, "turn_active", False):
            return self._json(409, {"error": "no active turn"})
        try:
            position = rt.steer(body.get("text", ""))
        except (ValueError, RuntimeError) as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {"queued": True, "position": position})

    # ----------------------------------------------------------- streaming
    def _message(self, body: dict) -> None:
        raw_text = body.get("text", "")
        if not isinstance(raw_text, str):
            return self._json(400, {"error": "text must be a string"})
        text = raw_text.strip()
        if not text:
            return self._json(400, {"error": "empty message"})
        if len(text) > _MAX_MESSAGE_CHARS:
            return self._json(400, {"error": "message is too large"})
        with self.state.turn_lock:
            if self.state.busy:
                target = body.get("session")
                if target and target != self.state.session_id:
                    # a message aimed at another chat must never be injected
                    # into the running turn's session
                    return self._json(409, {"error": "turn running in another chat"})
                if len(self.state.interjections) >= _MAX_INTERJECTIONS:
                    return self._json(429, {"error": "interjection queue is full"})
                self.state.interjections.append(text)
                return self._json(202, {"queued": True, "count": len(self.state.interjections)})
            # Claim ownership before touching session state; concurrent requests
            # can only enqueue bounded interjections after this point.
            self.state.busy = True
            self.state.cancel = asyncio.Event()
            if body.get("new") or body.get("session"):
                self.state.interjections.clear()
        try:
            if body.get("new") or not self.state.session_id:
                sess = self.state.svc.new_session()
                self.state.session_id = sess.id
            sess = self.state.svc.sessions.get(self.state.session_id) or self.state.ensure_session()
            if body.get("session"):
                loaded = self.state.svc.sessions.get(body["session"])
                if loaded:
                    sess = loaded
                    self.state.session_id = sess.id
            q: queue.Queue = queue.Queue(maxsize=_MAX_EVENT_QUEUE)
        except BaseException:
            with self.state.turn_lock:
                self.state.busy = False
            raise

        def emit(kind: str, payload: dict[str, Any]) -> bool:
            try:
                q.put((kind, payload), timeout=1.0)
                return True
            except queue.Full:
                if self.state.cancel:
                    self.state.cancel.set()
                return False

        def on_event(ev: dict) -> None:
            # The runtime's approval_request is an informational echo whose id is
            # NOT registered with /api/approval. prompt_fn below emits the single
            # decidable approval_request; forwarding the echo would stack a phantom
            # modal (and break clients that key on the first event). Drop it.
            if ev.get("type") == "approval_request":
                return
            emit("evt", ev)

        async def prompt_fn(req: Any) -> ApprovalOutcome:
            aid = secrets.token_hex(12)
            fut = self.state.loop.create_future() if self.state.loop else None
            if fut is None:
                return ApprovalOutcome.DENIED
            self.state.approvals[aid] = fut
            emit("approval", {
                "id": aid, "tool": req.tool, "name": req.tool, "args": req.args,
                "risk": req.decision.risk, "level": req.decision.level,
                "mode": self.state.svc.config.permission_mode.value,
                "reasons": list(req.decision.reasons), "preview": req.preview,
            })
            try:
                return await asyncio.wait_for(fut, timeout=600)
            except TimeoutError:
                return ApprovalOutcome.DENIED
            finally:
                self.state.approvals.pop(aid, None)

        async def work() -> None:
            final_kind = "error"
            final_payload: dict[str, Any] = {"message": "turn did not complete"}
            try:
                rt = self.state.svc.runtime()
                self.state.active_runtime = rt
                res = await rt.run_turn(
                    sess, text, prompt_fn=prompt_fn, on_event=on_event,
                    cancel=self.state.cancel, user_requested_memory=bool(body.get("memory_directive")),
                    interjections=self.state.interjections,
                )
                if sess.id not in self.state.deleted_sessions and sess.title in ("New chat", "", None):
                    new_title = make_title(text)
                    self.state.svc.sessions.rename(sess.id, new_title)
                    sess.title = new_title
                self.state.tok_s = res.tokens_per_s or self.state.tok_s
                final_kind = "done"
                final_payload = {"text": res.text, "error": res.error, "steps": res.steps,
                                 "tokens_per_s": res.tokens_per_s, "calls": res.calls,
                                 "usage": res.usage, "model": sess.model or "",
                                 "reasoning": res.reasoning[:6000], "tokens": res.tokens,
                                 "ttft_s": res.ttft_s, "session": sess.id, "title": sess.title}
            except (AgentCancelled, asyncio.CancelledError):
                final_kind = "done"
                final_payload = {"error": "cancelled", "cancelled": True,
                                 "session": sess.id, "title": sess.title}
            except Exception as exc:  # noqa: BLE001
                tb = traceback.extract_tb(exc.__traceback__)
                where = f" (at {tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno})" if tb else ""
                final_payload = {"message": f"{type(exc).__name__}: {exc}{where}"}
            finally:
                if sess.id not in self.state.deleted_sessions:
                    # D8: a chat deleted mid-turn must never be written back.
                    try:
                        self.state.svc.sessions.save(sess)
                    except Exception as exc:
                        final_kind = "done"
                        final_payload["error"] = f"Session save failed: {exc}"
                        final_payload["saved"] = False
                with self.state.turn_lock:
                    self.state.busy = False
                    self.state.active_turn = None
                    self.state.active_runtime = None
                    self.state.interjections.clear()
                # Normal events may be dropped after bounded backpressure, but a
                # terminal event must reach the draining request thread.
                with contextlib.suppress(queue.Full):
                    q.put((final_kind, final_payload), timeout=5.0)

        try:
            assert self.state.loop is not None
            active = asyncio.run_coroutine_threadsafe(work(), self.state.loop)
            with self.state.turn_lock:
                self.state.active_turn = active
        except BaseException:
            with self.state.turn_lock:
                self.state.busy = False
            raise
        self._sse_init()
        try:
            while True:
                try:
                    kind, payload = q.get(timeout=_SSE_HEARTBEAT_SECONDS)
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
                if kind == "error":
                    return
        except (BrokenPipeError, OSError):
            self.state.cancel_active()

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

        def resolve() -> None:
            # Re-check ON the loop thread: a concurrent POST or the cancel
            # deny-sweep may have resolved the future between the HTTP-thread
            # check above and this callback; set_result twice would raise
            # InvalidStateError inside the loop. First decision wins.
            if not fut.done():
                fut.set_result(outcome)

        self.state.loop.call_soon_threadsafe(resolve)
        self._json(200, {"ok": True})

    def _pull(self, body: dict) -> None:
        be = self.state.svc.backend(body.get("backend"))
        name = str(body.get("name", "")).strip()
        if not name:
            return self._json(400, {"error": "name required"})
        self._sse_init()
        q: queue.Queue = queue.Queue(maxsize=64)

        def prog(status: str, pct: float | None) -> None:
            with contextlib.suppress(queue.Full):
                q.put_nowait(("progress", {"status": str(status)[:500], "pct": pct}))

        async def work() -> None:
            try:
                await be.pull(name, prog)
                q.put(("done", {"ok": True, "name": name}), timeout=2)
            except Exception as exc:  # noqa: BLE001
                with contextlib.suppress(queue.Full):
                    q.put(("done", {"ok": False, "error": str(exc)[:1000]}), timeout=2)

        assert self.state.loop is not None
        fut = asyncio.run_coroutine_threadsafe(work(), self.state.loop)
        try:
            while True:
                kind, payload = q.get(timeout=600)
                self._sse(kind, payload)
                if kind == "done":
                    return
        except (BrokenPipeError, OSError, queue.Empty):
            fut.cancel()  # D10: nobody is listening — stop the orphaned pull
            return


def _validation_sentence(exc: Exception) -> str:
    """K10: turn pydantic's wall of ValidationError text into one human
    sentence the GUI can show verbatim ("generation.temperature must be at
    most 2"), instead of a raw multi-line dump with a docs URL."""
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return str(exc)[:200] or "invalid configuration"
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc") or ()) or "config"
        etype = str(err.get("type") or "")
        ctx = err.get("ctx") or {}
        lo = ctx.get("ge", ctx.get("gt"))
        hi = ctx.get("le", ctx.get("lt"))
        if etype in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
            if lo is not None and hi is not None:
                parts.append(f"{loc} must be between {lo} and {hi}")
            elif lo is not None:
                parts.append(f"{loc} must be at least {lo}")
            elif hi is not None:
                parts.append(f"{loc} must be at most {hi}")
            else:
                parts.append(f"{loc}: {err.get('msg') or 'invalid value'}")
        elif etype in ("string_pattern_mismatch", "enum", "literal_error"):
            parts.append(f"{loc} is not an allowed value")
        else:
            parts.append(f"{loc}: {err.get('msg') or 'invalid value'}")
        if len(parts) >= 4:
            break
    return "; ".join(parts)[:300] or "invalid configuration"


def _safe_config(cfg: Config) -> dict:
    """Mask credentials at every configuration response boundary."""
    from .providers import mask

    data = json.loads(cfg.model_dump_json())
    data["cloud"]["keys"] = {key: mask(value) for key, value in cfg.cloud.keys.items()}
    if cfg.mail.password:
        data["mail"]["password"] = mask(cfg.mail.password)
    return data


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with a hard cap instead of an attacker-sized thread pool."""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self._request_slots = threading.BoundedSemaphore(_MAX_REQUEST_THREADS)
        super().__init__(server_address, handler)

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(_HEADER_READ_TIMEOUT)
        return request, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\nCache-Control: no-store\r\n\r\n"
                )
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _write_discovery(path: str, info: dict[str, Any]) -> Path:
    """Create one owner-private, non-symlink bridge discovery file."""
    target = Path(path)
    parent = target.parent
    try:
        pst = parent.lstat()
    except OSError as exc:
        raise ValueError("discovery parent must already exist") from exc
    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        raise ValueError("discovery parent must be a real directory")
    if os.name == "posix" and (pst.st_uid != os.getuid() or stat.S_IMODE(pst.st_mode) & 0o077):
        raise ValueError("discovery parent must be owner-only")
    payload = json.dumps(info, separators=(",", ":"))
    # Publish only a complete file. The hard link is atomic and fails rather
    # than replacing stale or attacker-substituted discovery state.
    staged = parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    atomic_write(staged, payload, mode=0o600)
    try:
        os.link(staged, target, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("discovery file must be a new regular path") from exc
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()
    return target


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
        state.heartbeat_task = loop.create_task(heartbeat())
        loop.run_forever()

    t = threading.Thread(target=loop_main, daemon=True)
    t.start()
    while state.loop is None:
        time.sleep(0.01)

    httpd = BoundedThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.state = state  # type: ignore[attr-defined]
    actual = httpd.server_address[1]
    httpd.omllm_port = actual  # type: ignore[attr-defined]
    info = {"version": 1, "port": actual, "token": state.token, "pid": os.getpid()}
    discovery_files: list[Path] = []
    try:
        if write_port_file:
            discovery_files.append(_write_discovery(write_port_file, info))
        if token_file:
            discovery_files.append(_write_discovery(token_file, info))
    except BaseException:
        for written in discovery_files:
            with contextlib.suppress(OSError):
                written.unlink()
        raise
    # The token is intentionally never printed; authorized launchers use the
    # private, short-lived discovery file above.
    print(f"OMLLM_BRIDGE port={actual}", flush=True)
    _done = False

    def _graceful() -> None:
        """Eject the model (frees VRAM), stop the loop, exit serve()."""
        nonlocal _done
        if _done:
            return
        _done = True
        state.cancel_active()

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
        state.cancel_active()
        httpd.server_close()
        for discovery in discovery_files:
            with contextlib.suppress(OSError):
                discovery.unlink()

        # D15: cancel every pending task (heartbeat included) and give the
        # loop a beat to process the cancellations BEFORE stopping it —
        # stop() in the same callback would leave tasks pending forever.
        def _drain() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.call_later(0.2, loop.stop)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_drain)
        t.join(timeout=5)
        if not t.is_alive():
            with contextlib.suppress(RuntimeError):
                loop.close()


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
