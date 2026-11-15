"""R6 correctness regressions — Task #101 (reproduce-first).

Confirmed findings from .validation/cindy-r6cor-report.md (HEAD 2dbd215):
  C1 (Med)  voice: a re-press inside stop's 3s grace window is told
     already=True, but the looping stop still tears the recorder down
     when the first start lands → GUI believes it is recording, mic off.
  C2 (Med)  lmstudio R5-F3 trailer wait counts only data: lines — comment
     keepalives bypass the 64-line cap AND ride the 300s read floor →
     indefinite, TUI-uncancellable hang after the answer is complete.
     cloud.py _stream_openai has the same uncapped post-finish read.
  C2b (Low) lmstudio: a late event:error AFTER the finish chunk raises and
     the runtime discards the completed answer (pre-R5 it survived).
  C3 (Med)  bridge defer-yolo TOCTOU: busy is read under turn_lock, the
     lock is released, then request_yolo stores the pending arm — a turn
     settling in the gap orphans the arm (lands one prompt late, UI lies).
  C4 (Low)  bridge: active_turn is assigned AFTER scheduling; a preempted
     request thread can overwrite a newer turn's live handle with a DONE
     future → GUI Stop no-ops (compounds C2's hang).
  E9 (Low)  bridge _authed: a non-ASCII token header makes
     hmac.compare_digest raise TypeError → connection dropped, no 401.

Every defect test FAILS on pristine 2dbd215; the two controls pin the R5-F3
spec-order usage behavior that must NOT change. Guard-compatible: no
subprocess, loopback bridges on ephemeral ports, fake transports/recorders,
temp OMLLM_HOME via the shared fixtures.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx

from ohmyllm import voice as V
from ohmyllm.backends.base import ChatChunk
from ohmyllm.backends.cloud import CloudBackend
from ohmyllm.backends.lmstudio import LMStudioBackend
from ohmyllm.backends.mock import MockBackend
from ohmyllm.config import Config
from ohmyllm.config.models import GenerationSettings
from ohmyllm.core import Services


# ------------------------------------------------------------------ helpers
def _sse(resp, until="done", hook=None, limit: int = 400) -> list[tuple[str, dict]]:
    """Minimal SSE reader (same framing as test_r5cor_regressions); `until`
    may be a tuple of terminal event names."""
    terminals = (until,) if isinstance(until, str) else tuple(until)
    events: list[tuple[str, dict]] = []
    buf = ""
    raw = resp.read1(65536)
    while raw:
        buf += raw.decode(errors="replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            ev, dat = "", "{}"
            for line in frame.splitlines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    dat = line[6:]
            if not ev:
                continue
            data = json.loads(dat)
            events.append((ev, data))
            if hook:
                hook(ev, data)
            if ev in terminals:
                resp.close()
                return events
        if len(events) > limit:
            resp.close()
            return events
        raw = resp.read1(65536)
    resp.close()
    return events


def _wait_idle(bridge, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and bridge.get("/api/status")["busy"]:
        time.sleep(0.02)
    assert bridge.get("/api/status")["busy"] is False, "turn never went idle"


_P_CONTENT = json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]})
_P_FINISH = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})


class _DripStream(httpx.AsyncByteStream):
    """SSE payloads, then a post-finish tail that never sends [DONE]:
    'keepalive' = comment lines every 20ms; 'silent' = no bytes at all."""

    def __init__(self, payloads: list[str], tail: str) -> None:
        self._payloads = payloads
        self._tail = tail

    async def __aiter__(self):
        for p in self._payloads:
            yield f"data: {p}\n\n".encode()
        if self._tail == "keepalive":
            while True:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.02)
        else:  # silent: stream stays open, nothing arrives
            await asyncio.sleep(30)


class _DripTransport(httpx.AsyncBaseTransport):
    def __init__(self, payloads: list[str], tail: str) -> None:
        self._payloads = payloads
        self._tail = tail

    async def handle_async_request(self, request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_DripStream(self._payloads, self._tail))


async def _consume(be) -> list[ChatChunk]:
    return [ch async for ch in be.stream_chat(
        "m", [{"role": "user", "content": "hi"}], GenerationSettings())]


def _cloud_cfg() -> Config:
    cfg = Config()
    cfg.cloud.provider = "openai"
    cfg.cloud.keys = {"openai": "sk-r6-test"}
    return cfg


# --------------------------------------------- C1: grace-window re-press
def test_r6_c1_voice_repress_inside_grace_window(bridge, monkeypatch, tmp_path):
    """start1 spawning (1s) → stop1 grace-looping → start2 told already=True
    → start1 lands. stop1 must NOT tear the recorder down: the re-press
    superseded it. The mic stays live until the NEXT release."""
    instances = []

    class GraceRec:
        def __init__(self, out):
            self.out = out
            self.running = False
            instances.append(self)

        def start(self):
            time.sleep(1.0)  # lands inside stop's 3s grace cap
            self.running = True
            return None

        def stop(self):
            self.running = False
            self.out.write_bytes(b"x" * 20_000)

    async def fake_transcribe(wav, *, language=None, timeout=120.0):
        return "ok", None

    monkeypatch.setattr(V, "Recorder", GraceRec)
    monkeypatch.setattr(V, "voice_dir", lambda: tmp_path)
    monkeypatch.setattr(V, "transcribe", fake_transcribe)

    results: dict = {}

    def do(action, key):
        results[key] = bridge.post("/api/voice/record", {"action": action})

    t1 = threading.Thread(target=do, args=("start", "start1"))
    t1.start()
    time.sleep(0.3)  # start1 pending, recorder still spawning
    t2 = threading.Thread(target=do, args=("stop", "stop1"))
    t2.start()
    time.sleep(0.2)  # stop1 now grace-looping
    r2 = bridge.post("/api/voice/record", {"action": "start"})
    assert r2.get("already") is True, r2
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive() and not t2.is_alive()
    assert len(instances) == 1
    # pre-fix: stop1 tore it down (running False) while the UI was told already
    assert instances[0].running is True, "R6-C1: grace-window stop killed the re-pressed recorder"
    r3 = bridge.post("/api/voice/record", {"action": "stop"})
    assert r3.get("text") == "ok", r3
    assert instances[0].running is False


# --------------------------------------------- C2: bounded post-finish read
async def test_r6_c2_lmstudio_keepalive_bounded():
    """Comment keepalives after finish must count toward the trailer cap —
    pre-fix they are skipped uncounted and the 300s read floor hangs the
    turn forever (TUI cancel never fires: no chunk is ever yielded)."""
    be = LMStudioBackend(Config(), _DripTransport([_P_CONTENT, _P_FINISH], "keepalive"))
    t0 = time.monotonic()
    chunks = await asyncio.wait_for(_consume(be), 6.0)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert "".join(ch.content for ch in chunks) == "hi"
    assert time.monotonic() - t0 < 5.0


async def test_r6_c2_lmstudio_post_finish_silent_bounded():
    """A server that goes SILENT after finish (stream open, no lines) must
    be bounded by the 2s post-finish wall clock, not the 300s read floor."""
    be = LMStudioBackend(Config(), _DripTransport([_P_CONTENT, _P_FINISH], "silent"))
    t0 = time.monotonic()
    chunks = await asyncio.wait_for(_consume(be), 6.0)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert "".join(ch.content for ch in chunks) == "hi"
    assert time.monotonic() - t0 < 5.0


async def test_r6_c2b_lmstudio_late_error_nonfatal():
    """event:error AFTER the finish chunk must not discard the completed
    answer — deliver the stashed terminal (pre-R5 answer survival)."""
    text = (f"data: {_P_CONTENT}\n\n"
            f"data: {_P_FINISH}\n\n"
            'event: error\ndata: {"error": {"message": "late boom"}}\n\n'
            "data: [DONE]\n\n")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = await _consume(be)  # pre-fix: BackendError("late boom")
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert "".join(ch.content for ch in chunks) == "hi"


async def test_r6_c2_cloud_keepalive_bounded():
    """cloud.py _stream_openai fold-in: same uncapped post-finish read —
    keepalives after finish_reason must not hang the turn."""
    be = CloudBackend(_cloud_cfg(), _DripTransport([_P_CONTENT, _P_FINISH], "keepalive"))
    t0 = time.monotonic()
    chunks = await asyncio.wait_for(_consume(be), 6.0)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert "".join(ch.content for ch in chunks) == "hi"
    assert time.monotonic() - t0 < 5.0


# --------------------------------------------- C3: atomic defer (no orphan)
def test_r6_c3_yolo_defer_atomic_no_orphan(bridge, svc, oml_env, monkeypatch):
    """The busy-check and request_yolo must happen under ONE turn_lock
    acquisition. Pre-fix the lock is released between them: a turn settling
    in the gap runs settle_yolo BEFORE the arm is stored → the pending arm
    is orphaned and prompt2 still prompts while the UI shows armed."""
    monkeypatch.setattr(MockBackend, "delay", 0.05)
    orig = Services.request_yolo

    def slow_request(self, want, *, busy):
        if busy:
            # widen the TOCTOU gap: store the arm only once the turn looks
            # idle (bounded — post-fix this runs UNDER turn_lock, so the
            # turn's finally cannot settle until the 3s cap expires).
            deadline = time.time() + 3.0
            while time.time() < deadline and bridge.get("/api/status")["busy"]:
                time.sleep(0.02)
        return orig(self, want, busy=busy)

    monkeypatch.setattr(Services, "request_yolo", slow_request)

    resp1 = bridge.post("/api/message", {"text": "hello"})
    arm = bridge.post("/api/config/agent", {"yolo": True, "confirm": True})
    assert arm.get("ok") is True, arm
    assert arm.get("yolo_deferred") is True
    assert bridge.get("/api/status")["yolo"] is True  # truthful effective UI
    _sse(resp1)
    _wait_idle(bridge)

    resp2 = bridge.post("/api/message", {"text": "create file r6c3.txt"})
    events2 = _sse(resp2, until=("done", "approval_request"))
    approvals = [d for ev, d in events2 if ev == "approval_request"]
    if approvals:
        # let the pre-fix turn finish cleanly before failing
        bridge.post(f"/api/approval/{approvals[0]['id']}", {"decision": "deny"})
        _wait_idle(bridge)
    assert not approvals, "R6-C3: deferred arm orphaned — prompt2 still prompts"
    assert (oml_env["ws"] / "r6c3.txt").exists()
    _wait_idle(bridge)
    # F1 one-prompt contract: turn2 consumed the arm and finally-disarmed
    assert bridge.get("/api/status")["yolo"] is False


# --------------------------------------------- C4: no stale handle overwrite
def test_r6_c4_active_turn_no_stale_overwrite(bridge, monkeypatch):
    """A request thread preempted between scheduling and the active_turn
    assignment must not overwrite a NEWER turn's live handle with its DONE
    future — GUI Stop has to keep cancelling the live turn."""
    orig_rcts = asyncio.run_coroutine_threadsafe
    sched = threading.Event()
    release = threading.Event()
    holder: dict = {}
    held_once = {"v": False}

    def rcts(coro, loop):
        fut = orig_rcts(coro, loop)  # turn1 runs to completion while T1 waits
        holder["loop"] = loop
        if not held_once["v"]:
            held_once["v"] = True
            sched.set()
            release.wait(15)
        return fut

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", rcts)

    hang = asyncio.Event()
    out1: dict = {}

    def t1_run():
        out1["events"] = _sse(bridge.post("/api/message", {"text": "hello"}))

    t1 = threading.Thread(target=t1_run)
    t1.start()
    try:
        assert sched.wait(10), "turn1 never scheduled"
        _wait_idle(bridge)  # turn1 DONE; T1 still preempted pre-assignment

        async def hang_stream(self, model, messages, params, *, tools=None, native_tools=False):
            await hang.wait()  # no chunk ever arrives → per-chunk cancel blind
            yield ChatChunk(content="late", done=True, finish="stop")

        monkeypatch.setattr(MockBackend, "stream_chat", hang_stream)
        out2: dict = {}

        def t2_run():
            out2["events"] = _sse(bridge.post("/api/message", {"text": "hang turn"}))

        t2 = threading.Thread(target=t2_run)
        t2.start()
        deadline = time.time() + 10
        while time.time() < deadline and not bridge.get("/api/status")["busy"]:
            time.sleep(0.02)
        assert bridge.get("/api/status")["busy"] is True
        time.sleep(0.3)  # turn2 parked inside stream_chat

        release.set()  # T1 resumes: pre-fix it overwrites active_turn here
        t1.join(10)
        assert not t1.is_alive()
        time.sleep(0.2)
        assert bridge.post("/api/cancel").get("ok") is True
        # pre-fix: cancel hit the DONE future → turn2 hangs → this fails
        _wait_idle(bridge, timeout=5)
        t2.join(10)
        assert not t2.is_alive()
        done2 = [d for ev, d in out2["events"] if ev == "done"]
        assert done2 and done2[-1].get("cancelled") is True
    finally:
        release.set()
        loop = holder.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(hang.set)
        t1.join(10)


# --------------------------------------------- E9: non-ASCII token → 401
def test_r6_e9_non_ascii_token_clean_401(bridge):
    """A non-ASCII X-OMLLM-Token must fail closed with a clean 401 — pre-fix
    hmac.compare_digest raises TypeError and the connection is dropped."""
    port = int(bridge.base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        req = (f"GET /api/status HTTP/1.1\r\n"
               f"Host: 127.0.0.1:{port}\r\n"
               "X-OMLLM-Token: \u2603\r\n"
               "Connection: close\r\n\r\n")
        s.sendall(req.encode("utf-8"))
        s.settimeout(10)
        data = b""
        while True:
            try:
                part = s.recv(65536)
            except TimeoutError:
                break
            if not part:
                break
            data += part
    assert data.startswith(b"HTTP/"), "E9: connection dropped without any response"
    assert b"401" in data.split(b"\r\n", 1)[0]


# --------------------------------------------- controls (must stay green)
async def test_r6_ctl_lmstudio_comments_before_finish():
    """R5-F3 contract: comments BEFORE finish are ignored, the spec-order
    usage trailer (finish → trailer → [DONE]) still lands on the terminal."""
    usage = {"prompt_tokens": 3, "completion_tokens": 2}
    text = (": welcome\n\n"
            f"data: {_P_CONTENT}\n\n"
            ": mid-stream keepalive\n\n"
            f"data: {_P_FINISH}\n\n"
            "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
            "data: [DONE]\n\n")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = LMStudioBackend(Config(), httpx.MockTransport(handler))
    chunks = await _consume(be)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage
    assert "".join(ch.content for ch in chunks) == "hi"


async def test_r6_ctl_cloud_spec_order_usage():
    """cloud spec-order control: finish → usage trailer → [DONE] delivers
    the terminal with usage (unchanged by the bounded-window fold-in)."""
    usage = {"prompt_tokens": 5, "completion_tokens": 1}
    text = (f"data: {_P_CONTENT}\n\n"
            f"data: {_P_FINISH}\n\n"
            "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
            "data: [DONE]\n\n")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    be = CloudBackend(_cloud_cfg(), httpx.MockTransport(handler))
    chunks = await _consume(be)
    done = [ch for ch in chunks if ch.done]
    assert len(done) == 1
    assert done[0].usage == usage
    assert "".join(ch.content for ch in chunks) == "hi"
