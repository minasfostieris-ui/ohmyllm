"""End-to-end tests against the real GUI bridge (HTTP + SSE, ephemeral port)."""

from __future__ import annotations

import asyncio
import http.client
import json
import pathlib
import socket
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from ohmyllm import bridge as B


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def req(self, method: str, path: str, body: dict | bytes | None = None,
            auth: bool = True, headers: dict[str, str] | None = None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if auth:
            r.add_header("X-OMLLM-Token", self.token)
        for key, value in (headers or {}).items():
            r.add_header(key, value)
        try:
            resp = urllib.request.urlopen(r, timeout=60)
            if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
                return resp  # live stream
            with resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return {"__status__": e.code, **json.loads(e.read() or b"{}")}

    def get(self, p, auth=True):
        return self.req("GET", p, auth=auth)

    def post(self, p, body=None, auth=True):
        return self.req("POST", p, body or {}, auth=auth)

    def patch(self, p, body=None):
        return self.req("PATCH", p, body or {})

    def delete(self, p):
        return self.req("DELETE", p)


def _socket_was_closed(conn: socket.socket) -> bool:
    """Return true only for EOF/reset; an idle read timeout is not closure."""
    try:
        return conn.recv(1) == b""
    except TimeoutError:
        return False
    except ConnectionError:
        return True


def _recv_http_response(conn: socket.socket, timeout: float) -> bytes:
    """Accumulate a complete Content-Length response across fragmented reads."""
    deadline = time.monotonic() + timeout
    response = bytearray()
    expected_size: int | None = None
    while expected_size is None or len(response) < expected_size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("timed out before the complete HTTP response arrived")
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(4096)
        except TimeoutError as exc:
            raise AssertionError("timed out before the complete HTTP response arrived") from exc
        if not chunk:
            break
        response.extend(chunk)
        if expected_size is None and b"\r\n\r\n" in response:
            headers, body = bytes(response).split(b"\r\n\r\n", 1)
            content_length = next(
                int(line.split(b":", 1)[1].strip())
                for line in headers.split(b"\r\n")[1:]
                if line.lower().startswith(b"content-length:")
            )
            expected_size = len(response) - len(body) + content_length
    if expected_size is not None and len(response) < expected_size:
        raise AssertionError("connection closed before the complete HTTP response arrived")
    return bytes(response)


@pytest.fixture()
def bridge(oml_env, svc, monkeypatch):
    """Boot the bridge against the isolated home; default backend is mock."""
    holder: dict = {}
    stop = threading.Event()
    captured: dict = {}
    real_state = B.BridgeState

    class _CapturingState(real_state):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["state"] = self

    # Task #14: expose the live BridgeState to tests (loop-thread races,
    # live snapshots) without touching production behavior.
    monkeypatch.setattr(B, "BridgeState", _CapturingState)

    def run() -> None:
        # Ephemeral port; discovery stays inside this test's owner-private home.
        B.serve(workspace=str(oml_env["ws"]), port=0,
                write_port_file=str(oml_env["home"] / "p.port"), stop=stop)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    port_file = oml_env["home"] / "p.port"
    deadline = time.time() + 60  # macOS CI runners can take ~30s cold
    while time.time() < deadline:
        if port_file.exists():
            try:
                holder["info"] = json.loads(port_file.read_text())
                break
            except json.JSONDecodeError:
                pass
        time.sleep(0.02)
    assert "info" in holder, "bridge never wrote port file"
    info = holder["info"]
    assert set(info) == {"version", "port", "token", "pid"} and info["version"] == 1
    assert stat.S_IMODE(port_file.stat().st_mode) == 0o600
    c = Client(info["port"], info["token"])
    assert "state" in captured, "bridge state was never constructed"
    c.state = captured["state"]
    # wait until the app answers
    for _ in range(100):
        try:
            if c.get("/api/health", auth=False)["ok"]:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    else:
        pytest.fail("bridge health never came up")
    yield c
    stop.set()
    th.join(timeout=10)
    assert not th.is_alive(), "bridge thread refused to stop"
    assert not port_file.exists(), "bridge discovery survived shutdown"


def sse_events(resp, until: str = "done", hook=None, limit: int = 80) -> list[tuple[str, dict]]:
    """Parse an SSE stream; optional hook(event, data) may issue replies."""
    events: list[tuple[str, dict]] = []
    buf = ""
    raw = resp.read1(65536)
    while raw:
        buf += raw.decode(errors="replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            ev = ""
            dat = "{}"
            for line in frame.splitlines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    dat = line[6:]
            if not ev:
                continue
            events.append((ev, json.loads(dat)))
            if hook:
                hook(ev, json.loads(dat))
            if ev == until:
                resp.close()
                return events
        if len(events) > limit:
            resp.close()
            return events
        raw = resp.read1(65536)
    resp.close()
    return events


# ------------------------------------------------------------------ basics
def test_post_routes_not_misrouted_to_patch(bridge):
    # a batch once landed these in do_PATCH; POST must reach them (404
    # "no route" is the regression signal, any other answer is fine)
    c = bridge
    r = c.post("/api/market/download", {"mid": "does-not-exist"})
    assert r.get("error") != "no route", "market/download must be POST-routed"
    assert r.get("__status__") == 404
    r = c.post("/api/skills/cast", {"name": "x", "session": "y"})
    assert r.get("error") != "no route", "skills/cast must be POST-routed"


def test_bridge_voice_routes(bridge):
    from ohmyllm import voice

    r = bridge.get("/api/voice")
    assert "line" in r and "🎤" in r["line"]
    # TTS is gone: the routes must 404, not half-work
    assert "error" in bridge.post("/api/voice/tts", {"on": True})
    assert "error" in bridge.post("/api/voice/say", {})
    r = bridge.get("/api/market")
    secs = {e.get("section") for e in r["entries"]}
    assert secs == {"llm", "stt"}
    assert voice.status_line().startswith("🎤")


def test_bridge_providers_masked(bridge):
    secret = "sk-bridge-super-secret-key"
    r = bridge.post("/api/providers/key", {"pid": "openrouter", "key": secret})
    assert "error" not in r
    body = json.dumps(r)
    assert secret not in body  # never returned raw
    row = next(p for p in r["providers"] if p["pid"] == "openrouter")
    assert row["has_key"] and secret not in row["masked"]
    r = bridge.post("/api/providers/use", {"pid": "openrouter"})
    assert r["active"] == "openrouter" and r["default_backend"] == "cloud"
    r = bridge.post("/api/providers/custom", {"name": "Home vLLM", "base_url": "http://192.168.1.5:8000"})
    cpid = next(p["pid"] for p in r["providers"] if p["name"] == "Home vLLM")
    assert r["providers"][next(i for i, p in enumerate(r["providers"]) if p["pid"] == cpid)]["base_url"].endswith("/v1")
    r = bridge.post("/api/providers/forget", {"pid": "openrouter"})
    row = next(p for p in r["providers"] if p["pid"] == "openrouter")
    assert not row["has_key"] and not row["active"]  # removing the key deselects it
    assert r["active"] == ""
    r = bridge.get("/api/providers")
    assert all(not p["active"] or p["has_key"] for p in r["providers"])


def test_market_get_shape(bridge):
    r = bridge.get("/api/market")
    assert "entries" in r and len(r["entries"]) == 15  # 11 llm + 4 stt
    for e in r["entries"]:
        assert {"mid", "title", "size_gb", "fit", "fit_label", "blurb", "repo"} <= set(e)
    p = bridge.get("/api/market/progress")
    assert p == {"active": False} or p.get("title")


def test_health_is_public_but_api_needs_header_token(bridge):
    assert bridge.get("/api/health", auth=False)["ok"] is True
    denied = bridge.get("/api/status", auth=False)
    assert denied["__status__"] == 401
    query_denied = bridge.get(f"/api/status?t={bridge.token}", auth=False)
    assert query_denied["__status__"] == 401
    assert bridge.get("/api/status")["backend"] == "mock"


def test_static_console_uses_transient_fragment_bootstrap(bridge):
    with urllib.request.urlopen(f"{bridge.base}/?t={bridge.token}", timeout=10) as response:
        html = response.read().decode()
    assert "OhMyLLM" in html and bridge.token not in html
    with urllib.request.urlopen(f"{bridge.base}/app.js", timeout=10) as response:
        app = response.read().decode()
    assert "window.location.hash" in app
    assert "history.replaceState" in app
    assert "X-OMLLM-Token" in app
    assert bridge.token not in app


def test_gui_uses_styled_modals_not_native_dialogs():
    """Native prompt()/confirm() pop browser-chrome dialogs: they break the
    design system and block automation. The app must use its own modals."""
    import re
    from pathlib import Path
    js = (Path(__file__).parent.parent / "src/ohmyllm/gui_static/app.js").read_text()
    offenders = re.findall(r"(?<!\w)(?:window\.)?(?<!Modal)(prompt|confirm)\s*\(", js)
    assert not offenders, f"native dialogs in app.js: {offenders}"
    assert "function promptModal" in js and "function confirmModal" in js
    html = (Path(__file__).parent.parent / "src/ohmyllm/gui_static/index.html").read_text()
    assert 'id="confirm-modal"' in html


def test_gui_scrollbars_are_themed():
    """The white native scrollbar clashes with the dark console; CSS must
    theme both Firefox and WebKit scrollbar rendering."""
    from pathlib import Path
    css = (Path(__file__).parent.parent / "src/ohmyllm/gui_static/styles.css").read_text()
    assert "scrollbar-color" in css and "scrollbar-width" in css
    assert "::-webkit-scrollbar" in css


def test_mode_endpoints_and_phrase_gate(bridge):
    assert bridge.post("/api/mode", {"mode": "strict"})["ok"] is True
    st = bridge.get("/api/status")
    assert st["mode"] == "strict"
    denied = bridge.post("/api/mode", {"mode": "full_access", "phrase": "sorry"})
    assert denied["__status__"] == 403
    ok = bridge.post("/api/mode", {"mode": "full_access", "phrase": B.Services.full_access_phrase()})
    assert ok["ok"] is True
    assert bridge.get("/api/status")["mode"] == "full_access"
    bridge.post("/api/mode", {"mode": "safe"})


def test_config_patch_never_grants_full_access_and_deep_merges(bridge):
    denied = bridge.patch("/api/config", {"permission_mode": "full_access"})
    assert denied["__status__"] == 403
    res = bridge.patch("/api/config", {"generation": {"temperature": 0.42}})
    assert res["ok"] is True
    assert res["config"]["permission_mode"] != "full_access"
    assert res["config"]["generation"]["temperature"] == 0.42
    assert res["config"]["generation"]["top_p"] == 1.0


def test_bridge_rejects_foreign_host_and_origin_without_reflection(bridge):
    conn = http.client.HTTPConnection("127.0.0.1", int(bridge.base.rsplit(":", 1)[1]), timeout=10)
    conn.request("GET", "/api/status", headers={"Host": "evil.example", "X-OMLLM-Token": bridge.token})
    response = conn.getresponse()
    assert response.status == 403
    assert response.getheader("Access-Control-Allow-Origin") is None
    response.read()
    conn.close()

    denied = bridge.req("GET", "/api/status", headers={"Origin": "https://evil.example"})
    assert denied["__status__"] == 403


def test_bridge_rejects_malformed_duplicate_nonfinite_and_unknown_json(bridge):
    cases = [
        (b"[]", "/api/cancel"),
        (b'{"decision":"approve","decision":"deny"}', "/api/approval/nope"),
        (b'{"mode":NaN}', "/api/mode"),
        (b'{"unexpected":true}', "/api/cancel"),
        (b'{"new":"yes","text":"hello"}', "/api/message"),
    ]
    for raw, path in cases:
        result = bridge.req("POST", path, raw)
        assert result["__status__"] == 400, (path, result)


def test_bridge_rejects_invalid_and_oversized_content_lengths(bridge):
    port = int(bridge.base.rsplit(":", 1)[1])
    for value in ("-1", str(B._MAX_BODY_BYTES + 1)):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", "/api/cancel", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("X-OMLLM-Token", bridge.token)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", value)
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 400
        response.read()
        conn.close()


def test_bridge_enforces_absolute_header_and_body_deadlines(bridge, monkeypatch):
    port = int(bridge.base.rsplit(":", 1)[1])
    monkeypatch.setattr(B, "_HEADER_READ_TIMEOUT", 0.15)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.settimeout(2)
        conn.sendall(b"GET /api/health HTTP/1.1\r\nX-Slow: ")
        for _ in range(8):
            try:
                conn.sendall(b"a")
            except OSError:
                break
            time.sleep(0.04)
        conn.settimeout(0.5)
        assert _socket_was_closed(conn), "slow-trickled headers survived the absolute deadline"

    monkeypatch.setattr(B, "_HEADER_READ_TIMEOUT", 1.0)
    monkeypatch.setattr(B, "_BODY_READ_TIMEOUT", 0.1)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.settimeout(2)
        conn.sendall(
            f"POST /api/cancel HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"X-OMLLM-Token: {bridge.token}\r\nContent-Type: application/json\r\n"
            "Content-Length: 10\r\n\r\n{".encode()
        )
        response = _recv_http_response(conn, timeout=1.0)
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert b"deadline" in response


def test_bridge_rejects_continuous_body_trickle_by_absolute_deadline(bridge, monkeypatch):
    port = int(bridge.base.rsplit(":", 1)[1])
    body_deadline = 0.15
    monkeypatch.setattr(B, "_HEADER_READ_TIMEOUT", 1.0)
    monkeypatch.setattr(B, "_BODY_READ_TIMEOUT", body_deadline)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.sendall(
            f"POST /api/cancel HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"X-OMLLM-Token: {bridge.token}\r\nContent-Type: application/json\r\n"
            "Content-Length: 1024\r\n\r\n".encode()
        )
        stop = threading.Event()

        def trickle() -> None:
            while not stop.wait(0.02):
                try:
                    conn.sendall(b" ")
                except OSError:
                    return

        sender = threading.Thread(target=trickle, daemon=True)
        started = time.monotonic()
        sender.start()
        try:
            response = _recv_http_response(conn, timeout=1.0)
        finally:
            stop.set()
            sender.join(timeout=1)
        elapsed = time.monotonic() - started

    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert b"deadline" in response
    assert elapsed < body_deadline + 0.5


def test_discovery_rejects_stale_and_symlink_targets(tmp_path):
    launch = tmp_path / "launch"
    launch.mkdir(mode=0o700)
    stale = launch / "bridge.json"
    stale.write_text("stale")
    with pytest.raises(ValueError):
        B._write_discovery(str(stale), {"version": 1, "port": 1, "token": "0" * 32, "pid": 1})
    assert stale.read_text() == "stale"
    stale.unlink()
    target = launch / "elsewhere"
    target.write_text("untouched")
    stale.symlink_to(target)
    with pytest.raises(ValueError):
        B._write_discovery(str(stale), {"version": 1, "port": 1, "token": "0" * 32, "pid": 1})
    assert target.read_text() == "untouched"


def test_discovery_partial_failure_unlinks_written_files(tmp_path):
    """If first _write_discovery succeeds but second fails, the first is unlinked."""
    launch = tmp_path / "launch"
    launch.mkdir(mode=0o700)
    first = launch / "first.json"
    second = launch / "second.json"
    # Make the second path a pre-existing file so os.link will fail
    second.write_text("blocker")
    info = {"version": 1, "port": 9999, "token": "ab" * 16, "pid": 1}

    # Call serve's discovery logic inline: first succeeds, second raises
    with pytest.raises(ValueError):
        discovery_files: list[pathlib.Path] = []
        try:
            discovery_files.append(B._write_discovery(str(first), info))
            discovery_files.append(B._write_discovery(str(second), info))
        except BaseException:
            for written in discovery_files:
                written.unlink(missing_ok=True)
            raise

    assert not first.exists(), "first discovery file must be unlinked after second fails"
    assert second.read_text() == "blocker", "pre-existing second file must be untouched"


# ------------------------------------------------------------------ chats
def test_chats_crud_via_bridge(bridge):
    created = bridge.post("/api/chats", {})
    sid = created["id"]
    rows = bridge.get("/api/chats?q=")["chats"]
    assert any(r["id"] == sid for r in rows)
    renamed = bridge.patch(f"/api/chats/{sid}", {"title": "Bridgey chat", "pinned": True, "tag": "gui"})
    assert renamed["title"] == "Bridgey chat" and renamed["pinned"] is True and "gui" in renamed["tags"]
    exp = bridge.post(f"/api/chats/{sid}/export", {"format": "markdown"})
    assert exp["path"].endswith(".md")
    assert bridge.delete(f"/api/chats/{sid}")["trashed"]


# ------------------------------------------------------------------ soul
def test_soul_read_write_and_backup(bridge):
    d = bridge.get("/api/soul")
    assert "# Soul" in d["text"]  # seeded skeleton
    bridge.post("/api/soul", {"action": "write", "text": "# OhMyLLM\n\n## Identity\n\n- bridge wrote this\n"})
    assert "bridge wrote this" in bridge.get("/api/soul")["text"]
    bridge.post("/api/soul", {"action": "revert"})
    assert "bridge wrote this" not in bridge.get("/api/soul")["text"]
    assert bridge.get("/api/soul")["backups"], "a backup should exist"


# ------------------------------------------------------------------ turns
def test_streaming_turn_with_memory_flow(bridge):
    resp = bridge.req("POST", "/api/message", {"text": "Hi! my name is Bridgey and I have a rabbit named Tupac"})
    events = sse_events(resp)
    kinds = [e for e, _ in events]
    assert "tool_plan" in kinds and "tool_result" in kinds and "memory_activity" in kinds
    assert events[-1][0] == "done"
    assert "Bridgey" in bridge.get("/api/soul")["text"]
    status = bridge.get("/api/status")
    assert status["session"] and status["busy"] is False


def test_cancel_persists_turn_and_unlocks_stream(bridge):
    # regression (audit P0): Stop raised AgentCancelled — a BaseException —
    # which slipped past work()'s `except Exception`: no done event ever
    # queued, the SSE writer pinged forever, the GUI composer stayed locked,
    # and the whole turn (user msg + partials) was never saved.
    from ohmyllm.backends.mock import MockBackend

    MockBackend.delay = 0.4  # class-level: in-process servers pick it up
    try:
        resp = bridge.req("POST", "/api/message", {"text": "count slowly please"})
        cancelled = []

        def hook(ev, data):
            if ev in ("chunk", "token") and not cancelled:
                cancelled.append(True)
                bridge.post("/api/cancel")

        events = sse_events(resp, hook=hook)
        assert cancelled, "stream produced no chunks to cancel mid-way"
    finally:
        MockBackend.delay = 0.0
    assert events, "stream must yield events"
    assert events[-1][0] == "done", "cancel must terminate the stream with done"
    assert events[-1][1].get("error") == "cancelled"
    # busy clears in work()'s finally — a beat after the done event lands
    status = bridge.get("/api/status")
    for _ in range(50):
        if status["busy"] is False:
            break
        time.sleep(0.1)
        status = bridge.get("/api/status")
    assert status["busy"] is False, "composer must unlock after cancel"
    # the session save lands in work()'s finally, just after busy clears —
    # poll the disk view instead of racing it (Windows runners lost this)
    active: list = []
    for _ in range(50):
        chats = bridge.get("/api/chats")["chats"]
        active = [c for c in chats if c["id"] == status["session"]]
        if active and active[0]["messages"] >= 1:
            break
        time.sleep(0.1)
    assert active and active[0]["messages"] >= 1, "cancelled turn must be on disk"


def test_approval_roundtrip_over_sse(bridge):
    bridge.post("/api/mode", {"mode": "sandboxed"})  # workspace writes need confirmation
    got = {}

    def hook(ev, data):
        if ev == "approval_request":
            got["id"] = data["id"]
            got["tool"] = data["tool"]
            bridge.post(f"/api/approval/{data['id']}", {"decision": "approve"})

    resp = bridge.req("POST", "/api/message", {"text": 'create file bridge_ok.txt with content "hello from approval"'})
    events = sse_events(resp, hook=hook)
    assert got.get("tool") == "write_file"
    results = [d for e, d in events if e == "tool_result"]
    assert results and results[0]["ok"] is True
    assert (pathlib.Path(bridge.get("/api/status")["workspace"]) / "bridge_ok.txt").read_text().startswith("hello")
    bridge.post("/api/mode", {"mode": "safe"})


def test_approval_deny_blocks_write(bridge):
    bridge.post("/api/mode", {"mode": "sandboxed"})

    def hook(ev, data):
        if ev == "approval_request":
            bridge.post(f"/api/approval/{data['id']}", {"decision": "deny"})

    resp = bridge.req("POST", "/api/message", {"text": 'create file bridge_no.txt with content "denied"'})
    events = sse_events(resp, hook=hook)
    results = [d for e, d in events if e == "tool_result"]
    assert results and (results[0].get("denied") or results[0]["ok"] is False)
    assert not (pathlib.Path(bridge.get("/api/status")["workspace"]) / "bridge_no.txt").exists()
    bridge.post("/api/mode", {"mode": "safe"})


@pytest.mark.asyncio
async def test_bridge_cancel_active_denies_prompts_and_cancels_turn():
    state = B.BridgeState.__new__(B.BridgeState)
    state.turn_lock = threading.Lock()
    state.cancel = asyncio.Event()
    state.loop = asyncio.get_running_loop()
    pending = state.loop.create_future()
    active = state.loop.create_future()
    state.approvals = {"approval": pending}
    state.active_turn = active

    state.cancel_active()
    await asyncio.sleep(0)

    assert state.cancel.is_set()
    assert pending.result() is B.ApprovalOutcome.DENIED
    assert active.cancelled()


def test_cancel_endpoint_denies_pending_approval(bridge):
    bridge.post("/api/mode", {"mode": "sandboxed"})
    workspace = pathlib.Path(bridge.get("/api/status")["workspace"])
    approval_id = None

    def hook(ev, data):
        nonlocal approval_id
        if ev == "approval_request":
            approval_id = data["id"]
            assert bridge.post("/api/cancel")["ok"] is True

    resp = bridge.req(
        "POST", "/api/message", {"text": 'create file bridge_cancelled.txt with content "no"'}
    )
    sse_events(resp, hook=hook)
    deadline = time.time() + 3
    while time.time() < deadline and bridge.get("/api/status")["busy"]:
        time.sleep(0.02)

    assert approval_id is not None
    assert bridge.get("/api/status")["busy"] is False
    assert not (workspace / "bridge_cancelled.txt").exists()
    stale = bridge.post(f"/api/approval/{approval_id}", {"decision": "approve"})
    assert stale["__status__"] == 404
    bridge.post("/api/mode", {"mode": "safe"})


def test_bridge_disconnect_cancels_pending_approval(bridge, monkeypatch):
    monkeypatch.setattr(B, "_SSE_HEARTBEAT_SECONDS", 0.05)
    bridge.post("/api/mode", {"mode": "sandboxed"})
    workspace = pathlib.Path(bridge.get("/api/status")["workspace"])

    resp = bridge.req(
        "POST", "/api/message", {"text": 'create file bridge_disconnected.txt with content "no"'}
    )
    events = sse_events(resp, until="approval_request")
    assert any(event == "approval_request" for event, _ in events)

    deadline = time.time() + 5
    while time.time() < deadline and bridge.get("/api/status")["busy"]:
        time.sleep(0.05)
    assert bridge.get("/api/status")["busy"] is False
    assert not (workspace / "bridge_disconnected.txt").exists()
    bridge.post("/api/mode", {"mode": "safe"})


def test_models_backend_doctor_audit(bridge):
    models = bridge.get("/api/models")["models"]
    assert models and all(m["fit"] for m in models)
    be = bridge.get("/api/backends")
    assert be["mock"]["running"] is True
    doc = bridge.get("/api/doctor")
    assert doc["checks"] and any(c["status"] in ("pass", "warn") for c in doc["checks"])
    audit = bridge.get("/api/audit?n=10")["events"]
    assert isinstance(audit, list)


def test_message_while_busy_has_bounded_interjection_queue(bridge, monkeypatch):
    import json as _json

    from ohmyllm.backends.mock import MockBackend

    monkeypatch.setattr(MockBackend, "delay", 0.2)  # hold the turn open long enough to fill the queue

    done: dict = {}
    # The runtime drains interjections at the TOP of each model-loop iteration,
    # before the first model token is emitted. Synchronize the burst on that
    # first token so it lands AFTER the iteration-1 drain and the bounded queue
    # fills deterministically. Test-side only: no drain/lifecycle change.
    first_token = threading.Event()

    def slow_turn() -> None:
        try:
            resp = bridge.req("POST", "/api/message", {"text": "hello there friend"})

            def hook(ev: str, _data: dict) -> None:
                if ev in ("token", "thinking", "thinking_text"):
                    first_token.set()

            done["sse"] = sse_events(resp, until="done", hook=hook)
        except Exception as exc:  # noqa: BLE001
            done["err"] = str(exc)

    th = threading.Thread(target=slow_turn, daemon=True)
    th.start()
    queued: list[dict] = []
    overflow = None
    for _ in range(200):
        st = bridge.req("GET", "/api/status")
        st = _json.loads(st) if not isinstance(st, dict) else st
        if st.get("busy"):
            assert first_token.wait(timeout=30), "turn never reached its first model token"
            for index in range(B._MAX_INTERJECTIONS):
                queued.append(bridge.post("/api/message", {"text": f"interjection {index}"}))
            overflow = bridge.post("/api/message", {"text": "one too many"})
            break
        time.sleep(0.02)
    th.join(timeout=60)
    assert queued, "never observed busy"
    assert all(item.get("queued") is True for item in queued)
    assert overflow and overflow.get("__status__") == 429
    assert not th.is_alive() and "err" not in done


def test_bridge_request_threads_are_hard_bounded():
    server = B.BoundedThreadingHTTPServer.__new__(B.BoundedThreadingHTTPServer)
    server._request_slots = threading.BoundedSemaphore(B._MAX_REQUEST_THREADS)
    for _ in range(B._MAX_REQUEST_THREADS):
        assert server._request_slots.acquire(blocking=False)

    class FakeSocket:
        def __init__(self):
            self.sent = b""
            self.closed = False

        def sendall(self, data):
            self.sent += data

        def close(self):
            self.closed = True

    request = FakeSocket()
    server.process_request(request, ("127.0.0.1", 1))
    assert b"503 Service Unavailable" in request.sent
    assert request.closed is True


def test_voice_record_roundtrip(bridge, monkeypatch, tmp_path):
    # server-side hold-to-talk: fake recorder + engine, real route wiring
    from ohmyllm import voice as V

    class FakeRec:
        def __init__(self, out):
            self.out, self._live = out, False

        @property
        def running(self):
            return self._live

        def start(self):
            self.out.write_bytes(b"RIFF" + b"\0" * 20000)
            self._live = True
            return None

        def stop(self):
            self._live = False

    async def fake_transcribe(wav, language=None):
        return "hello world", ""

    monkeypatch.setattr(V, "Recorder", FakeRec)
    monkeypatch.setattr(V, "voice_dir", lambda: tmp_path)
    monkeypatch.setattr(V, "transcribe", fake_transcribe)

    assert bridge.post("/api/voice/record", {"action": "start"}).get("ok") is True
    r = bridge.post("/api/voice/record", {"action": "stop"})
    assert r.get("text") == "hello world"
    assert bridge.post("/api/voice/record", {"action": "stop"}).get("text") == ""
    assert "error" in bridge.post("/api/voice/record", {"action": "bogus"})


def test_bridge_syncs_disk_config(bridge, oml_env):
    # the TUI is a separate process; its saves must show up in the GUI
    import time

    from ohmyllm.config import ConfigStore

    st = ConfigStore()
    assert bridge.get("/api/status")["backend"] == "mock"  # fixture default
    cfg = st.load()
    cfg.default_backend = "ollama"
    time.sleep(0.02)
    st.save(cfg)
    assert bridge.get("/api/status")["backend"] == "ollama"


# ------------------------------- Task #14 review regressions (F4 / F6 / F7)
def test_voice_transcribe_per_route_body_cap(bridge):
    """F4: >1MiB bodies reach the transcribe handler (its own 40MB b64 limit
    applies); other routes keep the global 1MiB cap. Invalid padding makes
    b64decode fail inside the handler — no ffmpeg is ever spawned."""
    r = bridge.post("/api/voice/transcribe", {"audio": "A" * 2_000_001})
    assert r.get("error") == "audio is not valid base64"

    def cl_only(path: str, length: int) -> tuple[int, bytes]:
        port = int(bridge.base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", path, skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("X-OMLLM-Token", bridge.token)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(length))
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    voice_cap = B._ROUTE_BODY_CAPS["/api/voice/transcribe"]
    status, body = cl_only("/api/voice/transcribe", voice_cap + 1)
    assert status == 400 and b"too large" in body
    status, body = cl_only("/api/message", B._MAX_BODY_BYTES + 1)
    assert status == 400 and b"too large" in body  # other routes keep 1MiB


def test_bridge_state_live_snapshot_not_stale(bridge):
    """F6: a live turn is fresh by definition, regardless of its ts."""
    class _LiveRT:
        turn_active = True

        def machine_state(self):
            return {"state": "working", "session_id": "s9", "turn": 3, "backend": "mock",
                    "model": "m", "pending_approval_id": None, "seq": 9, "ts": 1.0}

    bridge.state.active_runtime = _LiveRT()
    try:
        out = bridge.get("/api/state")
        assert out["stale"] is False
        assert out["state"] == "working"
    finally:
        bridge.state.active_runtime = None


def _block_loop_with_error_capture(state):
    """Pause the bridge loop thread and capture exceptions raised on it.

    Blocking the loop makes approval races DETERMINISTIC: every HTTP-thread
    done() check passes before any resolution callback can run.
    """
    loop = state.loop
    errors: list = []
    release = threading.Event()
    started = threading.Event()

    async def _install():
        loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))

    async def _block():
        started.set()
        release.wait()

    asyncio.run_coroutine_threadsafe(_install(), loop).result(timeout=10)
    blocker = asyncio.run_coroutine_threadsafe(_block(), loop)
    assert started.wait(timeout=10), "loop blocker never started"
    return loop, errors, release, blocker


def _make_loop_future(loop):
    async def _make():
        return asyncio.get_running_loop().create_future()

    return asyncio.run_coroutine_threadsafe(_make(), loop).result(timeout=10)


def _drain_loop(loop):
    async def _noop():
        return True

    assert asyncio.run_coroutine_threadsafe(_noop(), loop).result(timeout=10)


def _no_invalid_state(errors):
    return not [e for e in errors if isinstance(e.get("exception"), asyncio.InvalidStateError)]


def test_concurrent_approval_decisions_first_wins_no_loop_error(bridge):
    """F7: two concurrent decisions must not double-resolve the future."""
    state = bridge.state
    loop = state.loop
    assert loop is not None
    fut = _make_loop_future(loop)  # create BEFORE pausing the loop thread
    loop, errors, release, blocker = _block_loop_with_error_capture(state)
    aid = "race-approve-deny"
    state.approvals[aid] = fut
    try:
        # Both POSTs pass the HTTP-thread done() check while the loop is
        # paused, so BOTH resolution callbacks queue — the old code raised
        # InvalidStateError on the second set_result.
        r1 = bridge.post(f"/api/approval/{aid}", {"decision": "approve"})
        r2 = bridge.post(f"/api/approval/{aid}", {"decision": "deny"})
        assert r1.get("ok") is True and r2.get("ok") is True
        release.set()
        blocker.result(timeout=10)
        _drain_loop(loop)
        assert fut.done() and fut.result() == B.ApprovalOutcome.APPROVED  # first wins
        assert _no_invalid_state(errors), errors
    finally:
        release.set()
        state.approvals.pop(aid, None)


def test_approval_race_with_cancel_sweep_no_loop_error(bridge):
    """F7: the cancel deny-sweep and a decision POST must not double-resolve."""
    state = bridge.state
    loop = state.loop
    assert loop is not None
    fut = _make_loop_future(loop)  # create BEFORE pausing the loop thread
    loop, errors, release, blocker = _block_loop_with_error_capture(state)
    aid = "race-cancel-sweep"
    state.approvals[aid] = fut
    try:
        state.cancel_active()  # deny_pending queues first, loop still paused
        r = bridge.post(f"/api/approval/{aid}", {"decision": "approve"})
        assert r.get("ok") is True  # HTTP check still saw an unresolved future
        release.set()
        blocker.result(timeout=10)
        _drain_loop(loop)
        assert fut.done() and fut.result() == B.ApprovalOutcome.DENIED  # sweep ran first
        assert _no_invalid_state(errors), errors
    finally:
        release.set()
        state.approvals.pop(aid, None)
