"""Bounded local validation: synthetic homes, no external services or child processes.

Run with the project venv. Evidence stays in the ignored .validation directory.
The audit hook is defense in depth, not an OS sandbox for untrusted code.
"""
from __future__ import annotations

import json
import math
import os
import runpy
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# R3 (Task 139): the per-step budget is a finite parameter, not a hardcoded
# 600s. The guarded census grew past 600s (R9: ~617s), which forced an
# identical second full run every gate. Default 1500s keeps a genuine hang
# caught while letting the census complete ONCE in-process.
DEFAULT_STEP_TIMEOUT = 1500.0
EXCLUDED = (
    "test_run_command_output_and_timeout",
    "test_shell_uses_resolved_cwd_sanitized_environment_and_output_cap",
    "test_shell_timeout_terminates_descendants",
    "test_shell_cleans_descendants_after_leader_exits",
    "test_recorder_stop_is_fast",
)


def guarded(kind: str, args: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="isolated-", dir=ROOT / ".validation") as temp:
        home = Path(temp)
        ws = home / "workspace"
        ws.mkdir()
        os.environ.update(HOME=temp, USERPROFILE=temp, XDG_CONFIG_HOME=str(home / "config"),
                          XDG_CACHE_HOME=str(home / "cache"), OMLLM_HOME=str(home / "state"),
                          OMLLM_WORKSPACE=str(ws), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        for key in list(os.environ):
            if key.startswith("OMLLM_KEY_") or key.lower().endswith("_proxy"):
                os.environ.pop(key, None)
        listeners: list[socket.socket] = []

        def guard(event, values):
            if event in {"subprocess.Popen", "os.system", "os.kill", "os.killpg", "os.posix_spawn", "os.fork", "os.forkpty"}:
                raise PermissionError(f"isolated validation blocks {event}")
            if event == "socket.bind":
                sock, addr = values
                if not isinstance(addr, tuple) or addr[0] not in {"127.0.0.1", "::1"} or addr[1] != 0:
                    raise PermissionError("only ephemeral loopback listeners allowed")
                listeners.append(sock)
            if event == "socket.connect":
                _, addr = values
                ports = set()
                for listener in listeners:
                    try:
                        ports.add(listener.getsockname()[1])
                    except OSError:
                        pass
                if not isinstance(addr, tuple) or addr[0] not in {"127.0.0.1", "::1", "localhost"} or addr[1] not in ports:
                    raise PermissionError("external/live-service connection blocked")

        sys.addaudithook(guard)
        if kind == "pytest":
            import pytest
            return pytest.main(["-p", "pytest_asyncio.plugin", "-o", "addopts=", *args])
        sys.argv = [kind, *args]
        runpy.run_path(str(ROOT / kind), run_name="__main__")
        return 0


def parse_step_timeout(argv: list[str]) -> tuple[float, list[str]]:
    """Extract ``--step-timeout SECONDS`` (or ``--step-timeout=SECONDS``).

    Returns (budget, remaining argv). The budget must be finite and positive
    so a genuine hang is still caught; default DEFAULT_STEP_TIMEOUT (1500s).
    """
    budget = DEFAULT_STEP_TIMEOUT
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        value: str | None = None
        if arg == "--step-timeout":
            if i + 1 >= len(argv):
                raise SystemExit("--step-timeout requires a value in seconds")
            value = argv[i + 1]
            i += 2
        elif arg.startswith("--step-timeout="):
            value = arg.split("=", 1)[1]
            i += 1
        else:
            rest.append(arg)
            i += 1
            continue
        try:
            budget = float(value)
        except ValueError:
            raise SystemExit(f"--step-timeout: not a number: {value!r}") from None
    if not math.isfinite(budget) or budget <= 0:
        raise SystemExit("--step-timeout must be a finite positive number of seconds")
    return budget, rest


def main() -> int:
    os.chdir(ROOT)
    evidence = ROOT / ".validation"
    evidence.mkdir(exist_ok=True)
    budget, argv = parse_step_timeout(sys.argv[1:])
    if argv and argv[0] == "--guard":
        return guarded(argv[1], argv[2:])
    phase = argv[0] if argv else "final"
    python = str(ROOT / ".venv/bin/python")
    steps = [
        ([python, "-m", "ruff", "check", "src", "tests", "scripts"], ROOT),
        ([python, "-m", "compileall", "-q", "src", "tests", "scripts"], ROOT),
        ([python, "-m", "mypy", "src/ohmyllm"], ROOT),
        ([python, __file__, "--guard", "pytest", "tests/", "-q", "-k", " and ".join(f"not {n}" for n in EXCLUDED)], ROOT),
    ]
    if phase not in {"baseline", "integration"}:
        steps += [([python, __file__, "--guard", script], ROOT) for script in ("scripts/e2e_check.py", "scripts/tui_smoke.py")]
        steps += [(["cargo", *args], ROOT / "gui/src-tauri") for args in (("fmt", "--check"), ("check", "--offline"), ("test", "--offline"))]
    results = []
    for i, (cmd, cwd) in enumerate(steps):
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=budget)
            code, output = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired as exc:
            code, output = 124, str(exc)
        log = evidence / f"{phase}-{i}.log"
        log.write_text(output)
        results.append({"command": cmd, "cwd": str(cwd), "exit": code,
                        "step_timeout_s": budget, "log": str(log)})
        print(json.dumps(results[-1]), flush=True)
        print(output[-2500:], flush=True)
    (evidence / f"{phase}.json").write_text(json.dumps(results, indent=2))
    return int(any(r["exit"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
