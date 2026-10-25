"""Throwaway E2E loop-check: config → soul → runtime turn → memory → audit."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

tmp = tempfile.mkdtemp(prefix="omllm-e2e-")
os.environ["OMLLM_HOME"] = tmp
ws = Path(tmp) / "proj"
ws.mkdir()
os.environ["OMLLM_WORKSPACE"] = str(ws)

from ohmyllm import paths  # noqa: E402
from ohmyllm.agents import AgentRuntime  # noqa: E402
from ohmyllm.backends import make_backend  # noqa: E402
from ohmyllm.config import Config, ConfigStore, PermissionMode  # noqa: E402
from ohmyllm.memory import SoulManager  # noqa: E402
from ohmyllm.safety import ApprovalManager, AuditLogger, PolicyEngine  # noqa: E402
from ohmyllm.sessions import SessionStore  # noqa: E402
from ohmyllm.tools import build_default_registry  # noqa: E402


async def main() -> None:
    paths.ensure_global_dirs()
    cfg = Config()
    cfg.default_backend = "mock"
    cfg.permission_mode = PermissionMode.SANDBOXED
    cfg.first_run_completed = True
    store = ConfigStore()
    store.save(cfg)

    soul = SoulManager(paths.soul_path(), paths.soul_backups_dir())
    soul.ensure()
    backend = make_backend("mock", cfg)
    policy = PolicyEngine(cfg, ws)
    registry = build_default_registry()
    audit = AuditLogger()
    approvals = ApprovalManager(rules_path=Path(tmp) / "rules.json")
    rt = AgentRuntime(backend=backend, registry=registry, policy=policy, approvals=approvals, audit=audit, config=cfg, soul=soul)
    sessions = SessionStore()
    sess = sessions.create(backend="mock", model="mock-tool-7b", permission_mode="sandboxed", workspace=str(ws))

    events = []
    res = await rt.run_turn(sess, "hello my name is Minas", on_event=events.append)
    assert res.error is None, res.error
    assert res.text, "no reply"
    soul_text = soul.read()
    assert "User's name is Minas." in soul_text, soul_text
    assert any(c["name"] == "soul_append" and c["status"] == "ok" for c in res.calls), res.calls

    await rt.run_turn(sess, "I have a rabbit named Bnuy", on_event=events.append)
    assert "Bnuy" in soul.read(), soul.read()

    await rt.run_turn(sess, "I'm working on a CLI project called OhMyLLM", on_event=events.append)
    assert "OhMyLLM" in soul.read()

    audit_rows = audit.recent(20)
    assert any(r["tool"].startswith("soul_") and r["success"] for r in audit_rows), audit_rows

    # permission enforcement: sandbox must block network + outside writes
    d_net = policy.evaluate("http_fetch", {"url": "http://example.com"})
    assert not d_net.allowed
    d_out = policy.evaluate("write_file", {"path": "/etc/passwd", "content": "x"})
    assert not d_out.allowed
    d_cmd = policy.evaluate("run_command", {"command": "rm -rf /"})
    assert not d_cmd.allowed
    d_read_out = policy.evaluate("read_file", {"path": "/etc/hosts"})
    assert d_read_out.allowed and d_read_out.requires_approval, "sandboxed asks, no longer forbids"

    # session persisted & reloadable
    sessions.save(sess)
    again = sessions.get(sess.id)
    assert again and again.message_count >= 6

    print("E2E-OK soul entries:", len(soul.entries()))
    print("E2E-OK audit rows:", len(audit_rows))
    print("E2E-OK reply:", res.text[:60])
    print("tmp:", tmp)


asyncio.run(main())
