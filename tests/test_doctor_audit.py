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
    a.log(mode="safe", tool="write_file", args={"path": ".env", "content": "API_KEY=sk-supersecret123"},
          approval="approved", success=True, duration_ms=1)
    entry = a.recent(1)[0]
    assert "sk-supersecret123" not in entry["arguments"]
    assert "REDACTED" in entry["arguments"]


def test_approval_session_and_permanent_rules(tmp_path):
    rules = tmp_path / "rules.json"
    am = ApprovalManager(rules_path=rules)
    from ohmyllm.safety import Decision

    d = Decision(allowed=True, requires_approval=True, risk=4, reasons=("test",))

    async def approve_always(req):
        return ApprovalOutcome.APPROVED_ALWAYS

    import asyncio

    out = asyncio.run(am.decide("run_command", {"command": "ls"}, d, approve_always))
    assert out.approved
    assert am.preapproved("run_command", {"command": "ls"})
    data = json.loads(rules.read_text())
    assert any(r["key"].startswith("run_command:") for r in data["rules"])

    # reload persists
    am2 = ApprovalManager(rules_path=rules)
    assert am2.preapproved("run_command", {"command": "ls"})

    async def deny(req):
        return ApprovalOutcome.DENIED

    out2 = asyncio.run(am2.decide("run_command", {"command": "other"}, d, deny))
    assert not out2.approved
    # prompt None (non-interactive) fails closed
    out3 = asyncio.run(am2.decide("run_command", {"command": "third"}, d, None))
    assert out3 is ApprovalOutcome.DENIED


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
