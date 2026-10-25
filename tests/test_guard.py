"""Danger-guard (Clef-Flash /v1/systemone) client + runtime hook tests."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from ohmyllm.agents import AgentRuntime
from ohmyllm.backends.base import ChatChunk
from ohmyllm.config import GuardSettings, PermissionMode
from ohmyllm.guard import assess, build_state, needs_assessment
from ohmyllm.safety import ApprovalOutcome


class _Stub(BaseHTTPRequestHandler):
    prob = 0.9
    fail = False

    def do_POST(self):  # noqa: N802
        json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if _Stub.fail:
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({"answers": {"dangerous": {"type": "noul", "noul": _Stub.prob}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_needs_assessment_skips_readers():
    assert not needs_assessment("read_file", {})
    assert not needs_assessment("list_dir", {})
    assert needs_assessment("run_command", {"command": "rm -rf /"})
    assert needs_assessment("write_file", {"path": "a"})


def test_build_state_truncates_big_content():
    s = build_state("write_file", {"path": "a", "content": "x" * 5000})
    assert "5000 chars total" in s and len(s) < 5000


def test_assess_flags_above_threshold():
    srv, port = _serve()
    try:
        cfg = GuardSettings(enabled=True, base_url=f"http://127.0.0.1:{port}")
        v = asyncio.run(assess(cfg, "run_command", {"command": "rm -rf ~"}))
        assert v.flagged and v.prob == 0.9 and v.error is None
    finally:
        srv.shutdown()


def test_assess_below_threshold_not_flagged():
    _Stub.prob = 0.1
    srv, port = _serve()
    try:
        cfg = GuardSettings(enabled=True, base_url=f"http://127.0.0.1:{port}")
        v = asyncio.run(assess(cfg, "run_command", {"command": "ls"}))
        assert not v.flagged and v.error is None
    finally:
        srv.shutdown()
        _Stub.prob = 0.9


def test_assess_fail_open_on_http_error():
    _Stub.fail = True
    srv, port = _serve()
    try:
        cfg = GuardSettings(enabled=True, base_url=f"http://127.0.0.1:{port}")
        v = asyncio.run(assess(cfg, "run_command", {"command": "x"}))
        assert not v.flagged and v.error and "500" in v.error
    finally:
        srv.shutdown()
        _Stub.fail = False


def test_assess_fail_open_server_down():
    cfg = GuardSettings(enabled=True, base_url="http://127.0.0.1:1", timeout_s=2)
    v = asyncio.run(assess(cfg, "run_command", {"command": "x"}))
    assert not v.flagged and v.error


def _make_rt(svc):
    be = svc.backend()
    rt = AgentRuntime(
        backend=be, registry=svc.registry, policy=svc.policy,
        approvals=svc.approvals, audit=svc.audit, config=svc.config, soul=svc.soul,
    )
    rt._probe = be
    return rt


def test_guard_forces_approval_in_dangerous_mode(oml_env, svc):
    srv, port = _serve()
    try:
        _Stub.prob = 0.95
        svc.config.permission_mode = PermissionMode.DANGEROUS
        svc.config.guard = GuardSettings(enabled=True, base_url=f"http://127.0.0.1:{port}")
        be = svc.backend()
        be.script = [
            [ChatChunk(content="", tool_calls=[{"id": "t1", "function": {
                "name": "run_command", "arguments": {"command": "echo hi"}}}], done=True)],
            [ChatChunk(content="done"), ChatChunk(done=True)],
        ]
        asked = []

        async def prompt_fn(req):
            asked.append(req)
            return ApprovalOutcome.DENIED

        rt = _make_rt(svc)
        sess = svc.new_session()
        res = asyncio.run(rt.run_turn(sess, "run it", prompt_fn=prompt_fn, model="mock-tool-7b"))
        assert asked, "guard must force an approval prompt even in dangerous mode"
        assert "GUARD 95%" in " ".join(asked[0].decision.reasons)
        assert any(c["name"] == "run_command" and c["status"] == "denied" for c in res.calls)
    finally:
        srv.shutdown()


def test_guard_offline_fails_open_in_dangerous_mode(oml_env, svc):
    svc.config.permission_mode = PermissionMode.DANGEROUS
    svc.config.guard = GuardSettings(enabled=True, base_url="http://127.0.0.1:1", timeout_s=1)
    be = svc.backend()
    be.script = [
        [ChatChunk(content="", tool_calls=[{"id": "t1", "function": {
            "name": "run_command", "arguments": {"command": "echo hi"}}}], done=True)],
        [ChatChunk(content="done"), ChatChunk(done=True)],
    ]
    asked = []

    async def prompt_fn(req):
        asked.append(req)
        return ApprovalOutcome.DENIED

    rt = _make_rt(svc)
    sess = svc.new_session()
    res = asyncio.run(rt.run_turn(sess, "run it", prompt_fn=prompt_fn, model="mock-tool-7b"))
    assert not asked, "dead guard must not block dangerous-mode work"
    assert any(c["name"] == "run_command" and c["status"] == "ok" for c in res.calls)
