import json

from typer.testing import CliRunner

from ohmyllm.cli import app

runner = CliRunner()


def test_version_and_init(oml_env):
    import re
    from pathlib import Path

    import ohmyllm

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)
    assert ohmyllm.__version__ == expected
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and expected in r.output
    r2 = runner.invoke(app, ["init", "--backend", "mock"])
    assert r2.exit_code == 0
    from ohmyllm import paths

    assert paths.config_path().is_file()
    assert paths.soul_path().is_file()
    r3 = runner.invoke(app, ["init"])
    assert "already present" in r3.output  # idempotent without --force


def test_models_and_backend(oml_env, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["models"])
    assert r.exit_code == 0 and "mock-tool-7b" in r.output
    r2 = runner.invoke(app, ["backend", "ollama"])
    assert r2.exit_code == 0
    assert "ollama" in r2.output.lower()


def test_run_one_shot_writes_memory(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["run", "hello my name is CliTesta"])
    assert r.exit_code == 0, r.output
    from ohmyllm import paths

    soul = paths.soul_path().read_text()
    assert "CliTesta" in soul
    audit = paths.audit_log_path()
    assert audit.is_file() and "soul_append" in audit.read_text()


def test_run_blocks_when_mode_strict(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["run", "--mode", "strict", "hello my name is Nope"])
    assert r.exit_code == 0
    from ohmyllm import paths

    assert "Nope" not in paths.soul_path().read_text()  # blocked by policy


def test_run_rejects_full_access_from_cli(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["run", "--mode", "full_access", "x"])
    assert r.exit_code == 1
    assert "confirmation" in r.output or "TUI" in r.output


def test_run_mode_transition_invalidates_persisted_grants(oml_env):
    """Task #14 F5: run --mode must route through Services.set_mode so the
    policy_generation bump invalidates previously persisted session grants."""
    from ohmyllm.core import Services

    runner.invoke(app, ["init", "--backend", "mock"])
    svc = Services.boot()
    gen0 = svc.config.policy_generation
    args = {"path": str(svc.workspace / "grant.txt"), "content": "x"}
    scope = {"workspace": str(svc.workspace), "session_id": ""}
    svc.approvals.add_permanent("write_file", args, policy_generation=gen0, **scope)
    assert svc.approvals.preapproved("write_file", args, policy_generation=gen0, **scope)

    r = runner.invoke(app, ["run", "--mode", "strict", "hello mode transition"])
    assert r.exit_code == 0, r.output

    after = Services.boot()
    assert after.config.permission_mode.value == "strict"
    assert after.config.policy_generation > gen0  # bump persisted
    # set_mode -> invalidate_all wipes persisted grants AND bumps the
    # generation, so the old grant must be gone from disk entirely.
    assert after.approvals.permanent_rules == ()
    assert not after.approvals.preapproved(
        "write_file", args, policy_generation=after.config.policy_generation, **scope)


def test_chats_lifecycle(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    runner.invoke(app, ["run", "chat one for lifecycle"])
    r = runner.invoke(app, ["chats", "list"])
    assert "chat one for lifecycle" in r.output or r.output.strip()
    from ohmyllm import paths
    from ohmyllm.sessions import SessionStore

    store = SessionStore(paths.history_dir())
    rows = store.list()
    sid = rows[0].id
    r2 = runner.invoke(app, ["chats", "export", sid, "--format", "json"])
    assert r2.exit_code == 0 and "exported" in r2.output
    r3 = runner.invoke(app, ["chats", "delete", sid, "--yes"])
    assert r3.exit_code == 0
    assert store.get(sid) is None
    assert any(store.trash.glob(f"{sid}-*.json"))


def test_doctor_json(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output[r.output.index("["):])
    assert any(d["id"] == "python" for d in data)


def test_soul_and_logs_and_audit_cmds(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    runner.invoke(app, ["run", "remember that the sky is green"])
    r = runner.invoke(app, ["soul", "--path"])
    assert "soul.md" in r.output
    r2 = runner.invoke(app, ["audit", "-n", "5"])
    assert r2.exit_code == 0
    r3 = runner.invoke(app, ["logs", "-n", "5"])
    assert r3.exit_code == 0


def test_run_stats_footer_and_yes(oml_env):
    r = runner.invoke(app, ["run", "-b", "mock", "-y", "say hi"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "·" in out and "step" in out, f"stats footer missing: {out[-300:]}"


# ----------------------------------------------------- release regressions
def _out(r):
    try:
        return r.stdout
    except (AttributeError, ValueError):
        return r.output


def _err(r):
    try:
        return r.stderr
    except (AttributeError, ValueError):
        return r.output


def test_run_json_single_stdout_object(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["run", "--json", "hello json mode"])
    assert r.exit_code == 0, r.output
    data = json.loads(_out(r))  # exactly one JSON object on stdout
    assert data["ok"] is True
    assert isinstance(data["text"], str) and data["text"]
    assert "session" in data and "model" in data
    assert "·" not in _out(r)  # stats footer is a stderr diagnostic


def test_status_json(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0
    data = json.loads(_out(r))
    assert data["backend"] == "mock"
    assert {"version", "workspace", "mode", "operation_mode"} <= set(data)


def test_run_stdin_dash_cap_and_ok(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    r = runner.invoke(app, ["run", "-"], input="x" * 65537)
    assert r.exit_code == 1
    r2 = runner.invoke(app, ["run", "-"], input="hello from stdin")
    assert r2.exit_code == 0, r2.output


def test_run_stdin_dash_tty_refused(oml_env, monkeypatch):
    import types

    import ohmyllm.cli as cli_mod

    runner.invoke(app, ["init", "--backend", "mock"])
    fake = types.SimpleNamespace(
        stdin=types.SimpleNamespace(isatty=lambda: True, read=lambda n=-1: "x"))
    monkeypatch.setattr(cli_mod, "sys", fake)
    r = runner.invoke(app, ["run", "-"])
    assert r.exit_code == 1
    assert "Pipe" in _err(r)


def test_workspace_precedence(oml_env, tmp_path):
    runner.invoke(app, ["init", "--backend", "mock"])
    ws2 = tmp_path / "other-ws"
    ws2.mkdir()
    ws3 = tmp_path / "third-ws"
    ws3.mkdir()
    r = runner.invoke(app, ["-w", str(ws2), "status", "--json"])
    assert json.loads(_out(r))["workspace"] == str(ws2.resolve())
    r2 = runner.invoke(app, ["-w", str(ws2), "status", "-w", str(ws3), "--json"])
    assert json.loads(_out(r2))["workspace"] == str(ws3.resolve())


def test_resume_precedence(oml_env):
    from ohmyllm import paths
    from ohmyllm.sessions import SessionStore

    runner.invoke(app, ["init", "--backend", "mock"])
    assert runner.invoke(app, ["run", "first message for resume"]).exit_code == 0
    store = SessionStore(paths.history_dir())
    sid = store.list()[0].id
    r2 = runner.invoke(app, ["--resume", sid, "run", "second message"])
    assert r2.exit_code == 0, r2.output
    assert store.get(sid).message_count >= 2
    r3 = runner.invoke(app, ["run", "--resume", "nope-nope", "x"])
    assert r3.exit_code == 1


def _shell_plan(self, text, has_tools):
    return [{"id": "c1", "type": "function",
             "function": {"name": "run_command", "arguments": {"command": "echo hi"}}}]


def test_unattended_approval_fail_closed(oml_env, monkeypatch):
    from ohmyllm import paths
    from ohmyllm.backends.mock import MockBackend
    from ohmyllm.sessions import SessionStore

    runner.invoke(app, ["init", "--backend", "mock"])
    monkeypatch.setattr(MockBackend, "_plan_calls", _shell_plan)
    r = runner.invoke(app, ["run", "--mode", "sandboxed", "do it"])
    assert r.exit_code == 0, r.output
    sess = SessionStore(paths.history_dir()).list()[0]
    # fail-closed: the denial is recorded in the transcript; nothing ran ok
    assert "denied" in json.dumps(sess.messages).lower()
    assert not any(c.get("name") == "run_command" and c.get("status") == "ok"
                   for c in sess.tool_calls)


def test_yes_never_auto_approves_run_command(oml_env, monkeypatch):
    from ohmyllm import paths
    from ohmyllm.backends.mock import MockBackend
    from ohmyllm.sessions import SessionStore

    runner.invoke(app, ["init", "--backend", "mock"])
    monkeypatch.setattr(MockBackend, "_plan_calls", _shell_plan)
    r = runner.invoke(app, ["run", "--mode", "sandboxed", "-y", "do it"])
    assert r.exit_code == 0, r.output
    sess = SessionStore(paths.history_dir()).list()[0]
    # -y never auto-approves shell: denial recorded, nothing ran ok
    assert "denied" in json.dumps(sess.messages).lower()
    assert not any(c.get("name") == "run_command" and c.get("status") == "ok"
                   for c in sess.tool_calls)


def test_chats_search_command(oml_env):
    runner.invoke(app, ["init", "--backend", "mock"])
    runner.invoke(app, ["run", "zebraunique deployment notes"])
    r = runner.invoke(app, ["chats", "search", "zebraunique", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(_out(r))
    assert data["results"] and data["results"][0]["snippet"]
    assert set(data["results"][0]) == {"session_id", "title", "updated_iso", "snippet", "rank"}
    r2 = runner.invoke(app, ["chats", "search", "zebraunique"])
    assert r2.exit_code == 0 and "zebraunique" in r2.output
    r3 = runner.invoke(app, ["chats", "search", "nomatchxyz", "--json"])
    assert json.loads(_out(r3))["results"] == []
