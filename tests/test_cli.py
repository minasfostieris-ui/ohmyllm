import json

from typer.testing import CliRunner

from ohmyllm.cli import app

runner = CliRunner()


def test_version_and_init(oml_env):
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "0.1.0" in r.output
    r2 = runner.invoke(app, ["init", "--backend", "mock"])
    assert r2.exit_code == 0
    from ohmyllm import paths

    assert paths.config_path().is_file()
    assert paths.soul_path().is_file()
    r3 = runner.invoke(app, ["init"])
    assert "already present" in r3.output  # idempotent without --force


def test_models_and_backend(oml_env):
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
