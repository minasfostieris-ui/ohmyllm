"""R2 UX CLI/doc regressions (task #56, guard-compatible).

Proves the launch-contract fixes the headless GUI checker cannot reach:
U1 (--no-open launches nothing and prints a working tokenized URL), U3
(--stop closes the native shell's process group AND the web bridge), U18
(native + --port is rejected with guidance), U19 (backend help lists mock),
plus U2/U20 doc-truthfulness guards. Every Popen / webbrowser.open / os.kill
/ os.killpg is faked and recorded — nothing real spawns or signals, so the
suite runs cleanly under scripts/verify_isolated.py's subprocess audit hook
(the Python-level fakes keep the C-level audit from ever seeing a real call).
"""

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ohmyllm import cli
from ohmyllm.secureio import atomic_write

runner = CliRunner()

TOKEN = "ab" * 16
PORT = 43210


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.paths, "global_home", lambda: tmp_path)
    return tmp_path


class FakeBridgeChild:
    """Stands in for `python -m ohmyllm.bridge`: publishes discovery at once."""

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = os.getpid()  # alive, so the reuse liveness probe succeeds
        self.returncode = None
        port_file = Path(argv[argv.index("--port-file") + 1])
        atomic_write(
            port_file,
            json.dumps({"version": 1, "port": PORT, "token": TOKEN, "pid": self.pid},
                       separators=(",", ":")),
            mode=0o600,
        )

    def poll(self):
        return 0

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def send_signal(self, sig):
        pass


class FakeNativeChild:
    """Stands in for the native shell: records, never executes."""

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = os.getpid()
        self.returncode = None

    def poll(self):
        return None

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def send_signal(self, sig):
        pass


@pytest.fixture
def spies(monkeypatch):
    """Fake every launch and signal; record argv/env/cwd and kill targets.

    os.kill and os.killpg are replaced with Python-level recorders, so the
    isolation guard's C-level audit never observes a real signal. Nothing
    real is ever spawned or signalled.
    """
    opened, calls, kills, killpgs = [], [], [], []

    def fake_open(url, *args, **kwargs):
        opened.append(url)
        return True

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "env": kwargs.get("env"), "cwd": kwargs.get("cwd"),
                      "start_new_session": kwargs.get("start_new_session")})
        if argv[0] == sys.executable:
            return FakeBridgeChild(argv, **kwargs)
        return FakeNativeChild(argv, **kwargs)

    def fake_kill(pid, sig):
        kills.append((pid, sig))

    def fake_killpg(pid, sig):
        killpgs.append((pid, sig))

    monkeypatch.setattr(webbrowser, "open", fake_open)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    yield opened, calls, kills, killpgs


def _native_marker(home: Path) -> Path:
    """Executable marker for OMLLM_GUI_BIN resolution (execution is faked)."""
    stub = home / "ohmyllm-gui-stub"
    stub.write_bytes(b"")
    stub.chmod(0o755)
    return stub


# ---- U1: --no-open launches nothing and prints a working tokenized URL ----
def test_no_open_with_native_present_launches_nothing(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    stub = _native_marker(home)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(cli.app, ["gui", "--no-open"])
    assert result.exit_code == 0, result.output
    assert opened == [], "--no-open must never open a browser"
    assert len(calls) == 1 and calls[0]["argv"][:3] == [sys.executable, "-m", "ohmyllm.bridge"]
    assert [str(stub)] not in [c["argv"] for c in calls], "--no-open must not launch the native shell"
    assert f"http://127.0.0.1:{PORT}/#token={TOKEN}" in result.output, "must print the full tokenized URL"
    assert (home / "gui-web.pid").is_file()


def test_no_open_without_native_prints_tokenized_url(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(cli.app, ["gui", "--no-open"])
    assert result.exit_code == 0, result.output
    assert opened == []
    assert len(calls) == 1 and calls[0]["argv"][:3] == [sys.executable, "-m", "ohmyllm.bridge"]
    assert f"http://127.0.0.1:{PORT}/#token={TOKEN}" in result.output
    assert "nothing was launched" in result.output


def test_no_open_reuses_running_console(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)
    monkeypatch.setenv("COLUMNS", "200")
    first = runner.invoke(cli.app, ["gui"])
    assert first.exit_code == 0, first.output
    calls.clear()
    opened.clear()
    second = runner.invoke(cli.app, ["gui", "--no-open"])
    assert second.exit_code == 0, second.output
    assert calls == [], "--no-open must reuse the running bridge, not stack one"
    assert opened == [], "--no-open must not open a browser"
    assert f"http://127.0.0.1:{PORT}/#token={TOKEN}" in second.output
    assert "already running" in second.output


# ---- U3: --stop closes what we launched (native group + web bridge) ----
def test_stop_closes_native_shell_and_bridge_group(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    stub = _native_marker(home)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))
    r1 = runner.invoke(cli.app, ["gui"])
    assert r1.exit_code == 0, r1.output
    assert (home / "gui-native.pid").is_file()
    native_pid = int((home / "gui-native.pid").read_text().strip())
    assert calls[0]["start_new_session"] is True, "native shell must lead its own session"
    calls.clear()
    kills.clear()
    killpgs.clear()
    r2 = runner.invoke(cli.app, ["gui", "--stop"])
    assert r2.exit_code == 0, r2.output
    assert killpgs == [(native_pid, 15)], "--stop must signal the native shell's process group"
    assert not (home / "gui-native.pid").exists()
    assert "native console closed" in r2.output
    assert opened == []


def test_stop_closes_background_web_console(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)
    r1 = runner.invoke(cli.app, ["gui"])
    assert r1.exit_code == 0, r1.output
    web_pid = int((home / "gui-web.pid").read_text().strip())
    calls.clear()
    kills.clear()
    killpgs.clear()
    r2 = runner.invoke(cli.app, ["gui", "--stop"])
    assert r2.exit_code == 0, r2.output
    assert kills == [(web_pid, 15)], "--stop must signal the web bridge pid"
    assert not (home / "gui-web.pid").exists()
    assert not (home / "gui-web.discovery").exists()
    assert "web console closed" in r2.output


def test_stop_closes_both_consoles(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)
    runner.invoke(cli.app, ["gui"])  # background web console
    web_pid = int((home / "gui-web.pid").read_text().strip())
    atomic_write(home / "gui-native.pid", str(os.getpid()))  # a native shell alongside
    calls.clear()
    kills.clear()
    killpgs.clear()
    r = runner.invoke(cli.app, ["gui", "--stop"])
    assert r.exit_code == 0, r.output
    assert kills == [(web_pid, 15)]
    assert killpgs == [(os.getpid(), 15)]
    assert "web console closed" in r.output and "native console closed" in r.output
    assert not (home / "gui-web.pid").exists() and not (home / "gui-native.pid").exists()


def test_stop_with_nothing_running_is_honest(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    r = runner.invoke(cli.app, ["gui", "--stop"])
    assert r.exit_code == 0, r.output
    assert "no console running" in r.output
    assert kills == [] and killpgs == []


# ---- U18: native + --port is rejected with actionable guidance ----
def test_native_rejects_port_with_guidance(home, monkeypatch, spies):
    opened, calls, kills, killpgs = spies
    stub = _native_marker(home)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))
    r = runner.invoke(cli.app, ["gui", "--port", str(PORT)])
    assert r.exit_code == 1, r.output
    assert calls == [], "must not launch the native shell when --port is rejected"
    assert "--port only applies to the web console" in r.output
    assert "--web --port" in r.output
    assert opened == []


# ---- U19: backend help lists the mock backend ----
def test_backend_help_lists_mock(home, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    r = runner.invoke(cli.app, ["models", "--help"])
    assert r.exit_code == 0, r.output
    assert "ollama | lmstudio | cloud | mock" in r.output
    r2 = runner.invoke(cli.app, ["backend", "--help"])
    assert r2.exit_code == 0, r2.output
    assert "ollama | lmstudio | cloud | mock" in r2.output


# ---- U2/U20: doc truthfulness guards ----
def test_help_screen_gui_rows_are_truthful():
    from ohmyllm.tui.screens.help import HELP
    assert "launch nothing" in HELP, "U1: --no-open must be documented as launching nothing"
    assert "native window + its bridge" in HELP, "U3: --stop must close the shell and its bridge"
    assert "whose URL is printed" in HELP, "U2: fallback is a printed URL, not a browser"
    assert "automatic browser fallback" not in HELP.lower()


def test_readme_no_false_browser_fallback_claim():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "automatic browser fallback" not in readme.lower(), "U2: remove the false fallback claim"
    assert "--no-open" in readme, "U1: --no-open must be documented"
    assert "OMLLM_GUI_BIN" in readme, "U2: the cargo note must point at the shipped binary + override"
