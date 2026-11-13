"""App-first GUI launch contract (task #28, guard-compatible since task #44).

Default `oml gui` must select the native shell and never open a browser;
`--web` is the only browser opt-in; without a native binary the loopback
URL is printed and no browser opens. Every subprocess.Popen is faked and
recorded (argv/env/cwd) — no real process ever spawns, so the suite runs
cleanly under scripts/verify_isolated.py's subprocess audit hook. No real
bridge, no native window, no user-desktop side effects.
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
        self.pid = os.getpid()  # alive, so the --web reuse probe succeeds
        self.returncode = None
        port_file = Path(argv[argv.index("--port-file") + 1])
        atomic_write(
            port_file,
            json.dumps({"version": 1, "port": PORT, "token": TOKEN, "pid": self.pid},
                       separators=(",", ":")),
            mode=0o600,
        )

    def poll(self):
        # Exited: discovery is already published, and the foreground --web
        # path waits on poll() — a live fake would spin there forever.
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
    """Fake every launch: record argv/env/cwd, return fake proc objects.

    `calls` is a list of dicts: {"argv": [...], "env": {...}, "cwd": ...}.
    os.kill is faked too (signal-0 liveness probes report the fake bridge
    alive; real signals are only recorded) — the isolation guard audits
    os.kill inside the C implementation, so the Python-level fake keeps the
    suite guard-compatible. Nothing real is ever spawned or signalled.
    """
    opened, calls, kills = [], [], []

    def fake_open(url, *args, **kwargs):
        opened.append(url)
        return True

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "env": kwargs.get("env"), "cwd": kwargs.get("cwd")})
        if argv[0] == sys.executable:
            return FakeBridgeChild(argv, **kwargs)
        return FakeNativeChild(argv, **kwargs)

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        if sig == 0:
            return  # liveness probe: the fake bridge is always alive
        raise AssertionError(f"test must never send real signal {sig} to pid {pid}")

    monkeypatch.setattr(webbrowser, "open", fake_open)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "kill", fake_kill)
    yield opened, calls


def _native_marker(home: Path) -> Path:
    """Executable marker file for OMLLM_GUI_BIN resolution.

    Resolution (trusted_executable / _resolve_native_binary) requires a
    real executable file on disk; execution itself is always faked.
    """
    stub = home / "ohmyllm-gui-stub"
    stub.write_bytes(b"")
    stub.chmod(0o755)
    return stub


def test_default_launches_native_and_never_opens_browser(home, monkeypatch, spies):
    opened, calls = spies
    stub = _native_marker(home)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))

    result = runner.invoke(cli.app, ["gui"])
    assert result.exit_code == 0, result.output
    assert [c["argv"] for c in calls] == [[str(stub)]], "default must launch only the native shell"
    assert calls[0]["env"] is not None, "native launch must pass the sanitized environment"
    assert opened == [], "default must never open a browser"
    assert "native console launching" in result.output


def test_web_opt_in_is_the_only_browser_path(home, monkeypatch, spies):
    opened, calls = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)

    result = runner.invoke(cli.app, ["gui", "--web"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["argv"][:3] == [sys.executable, "-m", "ohmyllm.bridge"]
    assert opened == [f"http://127.0.0.1:{PORT}/#token={TOKEN}"]


def test_browser_flag_is_deprecated_alias_for_web(home, monkeypatch, spies):
    opened, calls = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)

    result = runner.invoke(cli.app, ["gui", "--browser"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert opened == [f"http://127.0.0.1:{PORT}/#token={TOKEN}"]


def test_no_native_prints_url_and_hint_without_browser(home, monkeypatch, spies):
    opened, calls = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)

    result = runner.invoke(cli.app, ["gui"])
    assert result.exit_code == 0, result.output
    assert opened == [], "no-native fallback must not open a browser"
    assert len(calls) == 1 and calls[0]["argv"][:3] == [sys.executable, "-m", "ohmyllm.bridge"]
    assert f"http://127.0.0.1:{PORT}/" in result.output
    assert "--web" in result.output
    assert (home / "gui-web.pid").read_text().strip() == str(os.getpid())
    assert (home / "gui-web.discovery").is_file()


def test_web_reuses_running_background_console(home, monkeypatch, spies):
    opened, calls = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)

    first = runner.invoke(cli.app, ["gui"])
    assert first.exit_code == 0, first.output
    calls.clear()
    opened.clear()

    second = runner.invoke(cli.app, ["gui", "--web"])
    assert second.exit_code == 0, second.output
    assert calls == [], "--web must attach to the running bridge, not stack one"
    assert opened == [f"http://127.0.0.1:{PORT}/#token={TOKEN}"]
    assert "already running" in second.output


def test_no_open_suppresses_browser_even_with_web(home, monkeypatch, spies):
    opened, calls = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)

    result = runner.invoke(cli.app, ["gui", "--web", "--no-open"])
    assert result.exit_code == 0, result.output
    assert opened == []
    assert len(calls) == 1
    assert f"http://127.0.0.1:{PORT}/" in result.output


def test_default_attaches_native_to_running_console(home, monkeypatch, spies):
    opened, calls = spies
    original_resolve = cli._resolve_native_binary
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)
    first = runner.invoke(cli.app, ["gui"])
    assert first.exit_code == 0, first.output
    discovery = (home / "gui-web.discovery").read_text().strip()
    calls.clear()
    opened.clear()

    stub = _native_marker(home)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))
    monkeypatch.setattr(cli, "_resolve_native_binary", original_resolve)

    second = runner.invoke(cli.app, ["gui"])
    assert second.exit_code == 0, second.output
    assert [c["argv"] for c in calls] == [[str(stub)]], "attach must not spawn a second bridge"
    assert calls[0]["env"]["OMLLM_BRIDGE_DISCOVERY"] == discovery, \
        "shell must receive the attach discovery"
    assert opened == []
