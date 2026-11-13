import json

from ohmyllm.doctor import run_checks, summarize
from ohmyllm.safety import ApprovalManager, ApprovalOutcome, AuditLogger


def test_audit_writes_and_rotates(oml_env, tmp_path):
    log = tmp_path / "audit.jsonl"
    a = AuditLogger(path=log, max_bytes=500, keep=2)
    for i in range(30):
        a.log(mode="sandboxed", tool="run_command", args={"command": f"echo {i}"},
              approval="approved", success=True, duration_ms=1.5, backend="mock", model="m")
    assert log.is_file()
    rows = a.recent(5)
    assert rows and rows[-1]["tool"] == "run_command"
    rotated = list(tmp_path.glob("audit.jsonl.*"))
    assert rotated, "rotation should keep numbered files"


def test_audit_redacts_secrets(oml_env, tmp_path):
    log = tmp_path / "audit.jsonl"
    a = AuditLogger(path=log)
    secret = "github_pat_11AA22BB33CC44DD55"
    content = f'API_KEY=sk-supersecret123 {{"password":"hunter1234","token":"{secret}"}}'
    a.log(mode="safe", tool="write_file", args={"path": ".env", "content": content},
          approval="approved", success=True, duration_ms=1)
    serialized = json.dumps(a.recent(1)[0]["arguments"])
    assert "sk-supersecret123" not in serialized
    assert "hunter1234" not in serialized
    assert secret not in serialized
    assert "REDACTED" in serialized


def test_audit_redacts_nested_values_caps_records_and_zero_queries(tmp_path):
    log = tmp_path / "audit.jsonl"
    audit = AuditLogger(path=log)
    args = {
        "headers": {"Authorization": "Bearer token-value-123456"},
        "items": [{"api_key": "sk-abcdefghijklmnop"}, "https://user:pass@example.com/path"],
        "payload": "x" * 100_000,
    }
    audit.log(
        mode="sandboxed", tool="write_file", args=args, approval="approved",
        success=True, duration_ms=1,
    )
    entry = audit.recent(1)[0]
    serialized = json.dumps(entry)
    assert "token-value" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "user:pass" not in serialized
    assert entry["arguments"]["omitted"] is True
    assert log.stat().st_size < 32_768
    assert audit.recent(0) == []


def test_audit_private_mode_and_health_surface_write_failures(tmp_path, monkeypatch):
    import os
    import stat

    audit = AuditLogger(path=tmp_path / "private" / "audit.jsonl")
    audit.log(
        mode="safe", tool="read_file", args={"path": "notes.txt"},
        approval="approved", success=True, duration_ms=1,
    )
    if os.name == "posix":
        assert stat.S_IMODE(audit.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(audit.path.stat().st_mode) == 0o600

    def fail_append(*args, **kwargs):
        raise OSError("synthetic unavailable storage")

    monkeypatch.setattr("ohmyllm.safety.audit.append_private", fail_append)
    audit.log(
        mode="safe", tool="read_file", args={}, approval="approved",
        success=False, duration_ms=1,
    )
    assert audit.health()["healthy"] is False
    assert "synthetic unavailable storage" in audit.health()["error"]


def test_approval_identity_is_complete_and_scoped(tmp_path):
    am = ApprovalManager(rules_path=tmp_path / "rules.json")
    common = "x" * 1000
    args = {"path": "note.txt", "content": common + "A"}
    scope = {"workspace": "/work/a", "session_id": "abc123", "policy_generation": 7}
    am.remember_session("write_file", args, **scope)

    assert am.preapproved("write_file", args, **scope)
    assert not am.preapproved(
        "write_file", {"path": "note.txt", "content": common + "B"}, **scope
    )
    assert not am.preapproved(
        "write_file", {"path": "note.txt", "content": common + " A"}, **scope
    )
    assert not am.preapproved("write_file", args, **{**scope, "workspace": "/work/ab"})
    assert not am.preapproved("write_file", args, **{**scope, "session_id": "other"})
    assert not am.preapproved("write_file", args, **{**scope, "policy_generation": 8})


def test_permanent_rules_roundtrip_and_legacy_invalidation(tmp_path):
    import asyncio

    from ohmyllm.safety import Decision

    rules = tmp_path / "rules.json"
    am = ApprovalManager(rules_path=rules)
    decision = Decision(allowed=True, requires_approval=True, risk=4, reasons=("test",))
    scope = {"workspace": "/work", "session_id": "abc123", "policy_generation": 2}

    async def approve_always(req):
        return ApprovalOutcome.APPROVED_ALWAYS

    args = {"path": "note.txt", "content": "exact text"}
    assert asyncio.run(am.decide("write_file", args, decision, approve_always, **scope)).approved
    data = json.loads(rules.read_text())
    assert data["version"] == 2
    assert all(r["key"].startswith("v2:sha256:") for r in data["rules"])
    assert ApprovalManager(rules_path=rules).preapproved("write_file", args, **scope)

    rules.write_text(json.dumps({"rules": [{"key": "write_file:legacy", "decision": "allow"}]}))
    rules.chmod(0o600)
    assert not ApprovalManager(rules_path=rules).preapproved("write_file", args, **scope)


def test_invalidation_cancels_pending_approval_and_denies_it(tmp_path):
    import asyncio

    from ohmyllm.safety import Decision

    async def scenario():
        manager = ApprovalManager(rules_path=tmp_path / "rules.json")
        decision = Decision(allowed=True, requires_approval=True, risk=4, reasons=("test",))
        started = asyncio.Event()
        never = asyncio.Event()

        async def prompt(_request):
            started.set()
            await never.wait()
            return ApprovalOutcome.APPROVED

        pending = asyncio.create_task(manager.decide("write_file", {}, decision, prompt))
        await started.wait()
        manager.invalidate_all()
        return await asyncio.wait_for(pending, timeout=1)

    assert asyncio.run(scenario()) is ApprovalOutcome.DENIED


def test_shell_approval_is_always_one_shot(tmp_path):
    import asyncio

    from ohmyllm.safety import Decision

    am = ApprovalManager(rules_path=tmp_path / "rules.json")
    decision = Decision(allowed=True, requires_approval=True, risk=1, reasons=("shell",))
    prompts = 0

    async def approve_always(req):
        nonlocal prompts
        prompts += 1
        return ApprovalOutcome.APPROVED_ALWAYS

    args = {"command": "printf safe", "cwd": "/work"}
    first = asyncio.run(am.decide("run_command", args, decision, approve_always))
    second = asyncio.run(am.decide("run_command", args, decision, approve_always))
    assert first is ApprovalOutcome.APPROVED
    assert second is ApprovalOutcome.APPROVED
    assert prompts == 2
    assert not am.permanent_rules


def test_session_approval_does_not_cover_another_file(tmp_path):
    # Session consent is scoped to the authorized payload and file identity.
    from ohmyllm.safety import Decision

    am = ApprovalManager(rules_path=tmp_path / "rules.json")
    d = Decision(allowed=True, requires_approval=True, risk=4, reasons=("test",))
    prompts: list = []

    async def approve_session(req):
        prompts.append(req.tool)
        return ApprovalOutcome.APPROVED_SESSION

    import asyncio

    out = asyncio.run(am.decide("write_file", {"path": "a.html", "content": "x"}, d, approve_session))
    assert out.approved and len(prompts) == 1
    out2 = asyncio.run(am.decide("write_file", {"path": "b.css", "content": "y"}, d, approve_session))
    assert out2.approved and len(prompts) == 2
    # a DIFFERENT tool still asks; session consent covered two distinct
    # write payloads (a.html, b.css) plus this shell call
    out3 = asyncio.run(am.decide("run_command", {"command": "ls"}, d, approve_session))
    assert out3.approved and prompts == ["write_file", "write_file", "run_command"]
    # permanent rules stay exact-arg (reload simulates a fresh process);
    # shell consent is one-shot, so the fresh manager must prompt again
    am2 = ApprovalManager(rules_path=tmp_path / "rules.json")
    out4 = asyncio.run(am2.decide("run_command", {"command": "ls other"}, d, approve_session))
    assert out4.approved and len(prompts) == 4


def test_doctor_statuses(oml_env, monkeypatch):
    checks = run_checks()
    ids = {c.id for c in checks}
    assert {"python", "config", "soul", "mode", "backend", "gpu", "workspace", "audit"} <= ids
    s = summarize(checks)
    assert s.get("fail", 0) == 0  # fresh env should have no hard failures
    soul = next(c for c in checks if c.id == "soul")
    assert soul.status == "warn"  # not created yet by bare run_checks
    ollama = next(c for c in checks if c.id == "ollama-binary")
    assert ollama.status in {"pass", "info"}


def test_doctor_backend_probes_disable_proxy_and_redirect_inheritance(oml_env, monkeypatch):
    import ohmyllm.doctor.checks as doctor_checks
    from ohmyllm.config import Config

    captured: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code=503, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _path):
            return FakeResponse()

        def post(self, _path, **_kwargs):
            return FakeResponse(200, {"answers": {"dangerous": {"noul": 0.0}}})

    cfg = Config()
    cfg.guard.enabled = True
    monkeypatch.setattr(doctor_checks.httpx, "Client", FakeClient)
    monkeypatch.setattr(doctor_checks.OllamaBackend, "is_installed", lambda self: True)
    monkeypatch.setattr(doctor_checks.LMStudioBackend, "is_installed", lambda self: False)
    run_checks(config=cfg)

    assert len(captured) == 3
    assert all(item["trust_env"] is False for item in captured)
    assert all(item["follow_redirects"] is False for item in captured)


def test_windows_profile_update_passes_path_as_sanitized_data(tmp_path, monkeypatch):
    from pathlib import Path

    from ohmyllm import selfinstall

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)

    dangerous = tmp_path / "bin'; Write-Output injected; '"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(selfinstall, "_is_windows", lambda: True)
    monkeypatch.setattr(selfinstall.subprocess, "run", fake_run)
    monkeypatch.setenv("SELFINSTALL_API_TOKEN", "must-not-leak")
    monkeypatch.setenv("SHELL", "")

    selfinstall.append_to_profile([dangerous])
    assert str(dangerous) not in captured["argv"][-1]
    assert captured["env"]["OMLLM_ADD_PATH"] == str(dangerous)
    assert "SELFINSTALL_API_TOKEN" not in captured["env"]


def test_selfinstall_shims_and_path_text(oml_env, tmp_path, monkeypatch):
    from ohmyllm import selfinstall

    monkeypatch.setattr(selfinstall, "user_bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(selfinstall, "find_existing_entry", lambda name="ohmyllm": None)
    import sys

    res = selfinstall.ensure_aliases(interpreter=sys.executable)
    assert all(r.ok for r in res)
    import os
    shim = tmp_path / "bin" / ("ohmyllm" + (".bat" if os.name == "nt" else ""))
    assert shim.is_file() and "-m ohmyllm" in shim.read_text()
    instr = selfinstall.path_instructions([tmp_path / "bin"])
    joined = "\n".join(sum(instr.values(), []))
    assert str(tmp_path / "bin") in joined
    # profile append is idempotent
    prof = tmp_path / ".bashrc"
    selfinstall._append_once(prof, [f'export PATH="{tmp_path / "bin"}":"$PATH"'])
    selfinstall._append_once(prof, [f'export PATH="{tmp_path / "bin"}":"$PATH"'])
    assert prof.read_text().count(selfinstall.MARKER) == 1
