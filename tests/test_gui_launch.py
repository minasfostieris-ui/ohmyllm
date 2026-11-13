"""App-first GUI launch contract (task #28).

Default `oml gui` must select the native shell and never open a browser;
`--web` is the only browser opt-in; without a native binary the loopback
URL is printed and no browser opens. All launches are stubbed — no real
bridge, no native window, no user-desktop side effects.
"""

import json
import os
import subprocess
import sys
import time
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
        pass

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def spies(monkeypatch):
    opened, calls, children = [], [], []
    real_popen = subprocess.Popen

    def fake_open(url, *args, **kwargs):
        opened.append(url)
        return True

    def fake_popen(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == sys.executable:
            return FakeBridgeChild(argv, **kwargs)
        child = real_popen(argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(webbrowser, "open", fake_open)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    yield opened, calls
    for child in children:
        child.wait(timeout=3)


def test_default_launches_native_and_never_opens_browser(home, monkeypatch, spies):
    opened, calls = spies
    log = home / "stub-argv.log"
    stub = home / "ohmyllm-gui-stub"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))

    result = runner.invoke(cli.app, ["gui"])
    assert result.exit_code == 0, result.output
    deadline = time.time() + 3
    while not log.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert log.exists(), "native stub was never executed"
    assert log.read_text().strip() == ""  # stub logs argv: none expected
    assert calls == [[str(stub)]], "default must launch only the native shell"
    assert opened == [], "default must never open a browser"
    assert "native console launching" in result.output


def test_web_opt_in_is_the_only_browser_path(home, monkeypatch, spies):
    opened, calls = spies
    monkeypatch.setattr(cli, "_resolve_native_binary", lambda env: None)

    result = runner.invoke(cli.app, ["gui", "--web"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][:3] == [sys.executable, "-m", "ohmyllm.bridge"]
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
    assert len(calls) == 1 and calls[0][:3] == [sys.executable, "-m", "ohmyllm.bridge"]
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

    log = home / "stub-env.log"
    stub = home / "ohmyllm-gui-stub"
    stub.write_text(f"#!/bin/sh\necho \"$OMLLM_BRIDGE_DISCOVERY\" >> {log}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("OMLLM_GUI_BIN", str(stub))
    monkeypatch.setattr(cli, "_resolve_native_binary", original_resolve)

    second = runner.invoke(cli.app, ["gui"])
    assert second.exit_code == 0, second.output
    deadline = time.time() + 3
    while not log.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert log.exists(), "native stub was never executed"
    assert log.read_text().strip() == discovery, "shell must receive the attach discovery"
    assert calls == [[str(stub)]], "attach must not spawn a second bridge"
    assert opened == []
