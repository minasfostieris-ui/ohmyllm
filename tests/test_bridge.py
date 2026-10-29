"""End-to-end tests against the real GUI bridge (HTTP + SSE, ephemeral port)."""

from __future__ import annotations

import json
import pathlib
import threading
import time

from ohmyllm import bridge as B


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


def test_health_is_public_but_api_needs_token(bridge):
    assert bridge.get("/api/health", auth=False)["ok"] is True
    denied = bridge.get("/api/status", auth=False)
    assert denied["__status__"] == 401
    assert bridge.get("/api/status")["backend"] == "mock"


def test_static_console_served_with_injected_token(bridge):
    import urllib.request as u

    with u.urlopen(f"{bridge.base}/?t={bridge.token}", timeout=10) as r:
        html = r.read().decode()
    assert "OhMyLLM" in html and bridge.token in html
    for asset in ("/styles.css", "/app.js"):
        with u.urlopen(f"{bridge.base}{asset}", timeout=10) as r:
            assert r.status == 200 and len(r.read()) > 500


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


def test_config_patch_never_grants_full_access(bridge):
    cfg = bridge.get("/api/config")
    cfg["permission_mode"] = "full_access"
    cfg["generation"]["temperature"] = 0.42
    res = bridge.patch("/api/config", cfg)
    assert res["ok"] is True
    assert res["config"]["permission_mode"] != "full_access"
    assert res["config"]["generation"]["temperature"] == 0.42


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


def test_models_backend_doctor_audit(bridge):
    models = bridge.get("/api/models")["models"]
    assert models and all(m["fit"] for m in models)
    be = bridge.get("/api/backends")
    assert be["mock"]["running"] is True
    doc = bridge.get("/api/doctor")
    assert doc["checks"] and any(c["status"] in ("pass", "warn") for c in doc["checks"])
    audit = bridge.get("/api/audit?n=10")["events"]
    assert isinstance(audit, list)


def test_message_while_busy_is_queued(bridge, monkeypatch):
    import json as _json

    from ohmyllm.backends.mock import MockBackend

    monkeypatch.setattr(MockBackend, "delay", 0.03)  # hold the turn open long enough to race

    done: dict = {}

    def slow_turn() -> None:
        try:
            resp = bridge.req("POST", "/api/message", {"text": "hello there friend"})
            done["sse"] = resp.read()
        except Exception as exc:  # noqa: BLE001
            done["err"] = str(exc)

    th = threading.Thread(target=slow_turn, daemon=True)
    th.start()
    queued = None
    for _ in range(200):
        st = bridge.req("GET", "/api/status")
        st = _json.loads(st) if not isinstance(st, dict) else st
        if st.get("busy"):
            queued = bridge.req("POST", "/api/message", {"text": "interjection from gui"})
            break
        time.sleep(0.02)
    th.join(timeout=60)
    assert queued is not None, "never observed busy"
    body = queued if isinstance(queued, dict) else _json.loads(queued.read())
    assert body.get("queued") is True and body.get("count", 0) >= 1


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
