"""Chat/plan/build operation modes, /yolo one-prompt bypass, tone_create."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from ohmyllm.agents.prompts import PromptBuilder
from ohmyllm.cli import app as cli_app
from ohmyllm.tools import build_default_registry
from ohmyllm.tools.spec import ToolContext


def _ctx(svc, saver=None) -> ToolContext:
    return ToolContext(workspace=svc.workspace, mode=svc.config.permission_mode.value,
                       config=svc.config, soul=svc.soul,
                       config_saver=saver or (lambda: None))


def test_chat_mode_blocks_shell_writes_allows_reads(svc):
    svc.config.agent.op_mode = "chat"
    eng = svc.policy
    for tool, args in [("run_command", {"command": "ls"}),
                       ("write_file", {"path": "x.txt", "content": "hi"}),
                       ("email_send", {"to": "a@b.c", "subject": "s", "body": "x"})]:
        d = eng.evaluate(tool, args)
        assert not d.allowed, tool
        assert "/build" in " ".join(d.reasons)
    assert eng.evaluate("read_file", {"path": "."}).allowed
    assert eng.evaluate("list_dir", {}).allowed


def test_plan_mode_blocks_mutations_keeps_exploration(svc):
    svc.config.agent.op_mode = "plan"
    eng = svc.policy
    assert not eng.evaluate("run_command", {"command": "echo hi"}).allowed
    assert not eng.evaluate("patch_file", {"path": "a.py", "patch": "@"}).allowed
    assert eng.evaluate("search_files", {"query": "def"}).allowed


def test_build_mode_changes_nothing(svc):
    svc.config.agent.op_mode = "build"
    d = svc.policy.evaluate("run_command", {"command": "ls -la"})
    assert "disabled in" not in " ".join(d.reasons)


def test_yolo_silences_approvals(svc):
    svc.config.agent.op_mode = "build"
    base = svc.policy.evaluate("write_file", {"path": "/tmp/zz.txt", "content": "hi"})
    assert base.allowed and base.requires_approval, "outside-workspace write must ask first"
    svc.config.agent.yolo = True
    d = svc.policy.evaluate("write_file", {"path": "/tmp/zz.txt", "content": "hi"})
    assert d.allowed and not d.requires_approval
    assert "YOLO" in " ".join(d.reasons)


def test_yolo_never_runs_catastrophic(svc):
    svc.config.permission_mode = "full_access"
    svc.config.agent.yolo = True
    eng = svc.policy
    for cmd in ["rm -rf / --no-preserve-root", ":(){ :|:& };:", "sudo rm -rf /"]:
        d = eng.evaluate("run_command", {"command": cmd})
        assert not d.allowed, f"{cmd} must stay blocked under yolo+full_access"
    assert not eng.evaluate("write_file",
                            {"path": "/etc/cron.d/evil", "content": "x"}).allowed


def test_yolo_resets_after_turn(svc):
    from ohmyllm.agents.runtime import TurnResult

    svc.config.agent.yolo = True
    rt = svc.runtime()
    rt._finish(svc.new_session(), TurnResult(), "done", 1.0, 5)
    assert svc.config.agent.yolo is False


def test_status_snapshot_exposes_mode_and_yolo(svc):
    st = svc.status_snapshot()
    assert st["op_mode"] == "build"
    assert st["yolo"] is False
    svc.config.agent.op_mode = "plan"
    svc.config.agent.yolo = True
    st = svc.status_snapshot()
    assert st["op_mode"] == "plan" and st["yolo"] is True


def test_yolo_never_persisted(svc):
    svc.config.agent.yolo = True
    dump = json.loads(svc.config.model_dump_json())
    assert "yolo" not in dump["agent"]
    svc.config_store.save(svc.config)
    assert "yolo" not in svc.config_store.path.read_text()


@pytest.mark.asyncio()
async def test_tone_create_tool_roundtrip(svc):
    saved: list[int] = []
    reg = build_default_registry()
    res = await reg.execute("tone_create", {
        "name": "pirate",
        "instructions": "Reply as a 18th-century pirate: nautical metaphors and arr, "
                        "but technically precise.",
    }, _ctx(svc, saver=lambda: saved.append(1)))
    assert res.ok and saved == [1]
    assert svc.config.agent.tone == "pirate"
    assert "pirate" in svc.config.agent.custom_tones
    d = svc.policy.evaluate("tone_create", {"name": "x", "instructions": "y"})
    assert d.allowed and not d.requires_approval


@pytest.mark.asyncio()
async def test_tone_create_rejects_bad_input(svc):
    reg = build_default_registry()
    ctx = _ctx(svc)
    assert not (await reg.execute("tone_create", {"name": "Default", "instructions": "x" * 50}, ctx)).ok
    assert not (await reg.execute("tone_create", {"name": "chill", "instructions": "x" * 50}, ctx)).ok
    assert not (await reg.execute("tone_create", {"name": "cool", "instructions": "too short"}, ctx)).ok
    assert svc.config.agent.tone == "default"


@pytest.mark.asyncio()
async def test_tone_create_respects_op_mode(svc):
    svc.config.agent.op_mode = "plan"
    reg = build_default_registry()
    res = await reg.execute("tone_create", {
        "name": "robot", "instructions": "Answer like a monotone robot. Beep boop. Always. "}, _ctx(svc))
    assert not res.ok and "/build" in res.error


def test_custom_tone_reaches_system_prompt(svc):
    svc.config.agent.custom_tones = {"robot": "Answer like a monotone robot. Beep boop."}
    svc.config.agent.tone = "robot"
    pb = PromptBuilder(svc.config, svc.registry, svc.soul, workspace=svc.workspace)
    assert "monotone robot" in pb.system_prompt(mode=svc.config.permission_mode, native=False)


def test_plan_mode_fragment_in_prompt(svc):
    svc.config.agent.op_mode = "plan"
    pb = PromptBuilder(svc.config, svc.registry, svc.soul, workspace=svc.workspace)
    assert "OPERATION MODE: plan" in pb.system_prompt(mode=svc.config.permission_mode, native=False)


def test_tone_create_in_native_schemas(svc):
    names = [d["function"]["name"] for d in svc.registry.native_schemas()]
    assert "tone_create" in names


def test_cli_entry_commands():
    runner = CliRunner()
    res = runner.invoke(cli_app, ["--help"])
    assert res.exit_code == 0
    for name in ("tui", "cli", "update", "gui"):
        assert name in res.output
