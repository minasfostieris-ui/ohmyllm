"""Shell tool — execution is only ever reached after PolicyEngine + approval."""

from __future__ import annotations

import os
import subprocess
import time

from .spec import ToolContext, ToolResult

OUTPUT_CAP = 6000


def run_command(ctx: ToolContext, args: dict) -> ToolResult:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return ToolResult(False, "", "empty command")
    cwd_arg = args.get("cwd")
    cwd = os.path.expanduser(str(cwd_arg)) if cwd_arg else str(ctx.workspace)
    timeout = float(args.get("timeout") or ctx.config.agent.tool_timeout)
    timeout = max(1.0, min(timeout, 600.0))
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env["OMLLM_AGENT"] = "1"
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, "", f"command timed out after {timeout:.0f}s: {cmd}")
    except OSError as exc:
        return ToolResult(False, "", f"spawn failed: {exc}")
    dur = (time.perf_counter() - start) * 1000
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"$ {cmd}", f"[cwd: {cwd}] [exit {proc.returncode}] [{dur:.0f}ms]"]
    if out:
        parts.append("--- stdout ---\n" + _cap(out))
    if err:
        parts.append("--- stderr ---\n" + _cap(err))
    if not out and not err:
        parts.append("(no output)")
    body = "\n".join(parts)
    ok = proc.returncode == 0
    return ToolResult(ok, body, None if ok else f"exit code {proc.returncode}")


def preview_shell(args: dict) -> str:
    cmd = str(args.get("command") or "")
    cwd = str(args.get("cwd") or "(workspace)")
    return f"command: {cmd}\nworking dir: {cwd}\ntimeout: {args.get('timeout', 'default')}"


def _cap(text: str) -> str:
    if len(text) > OUTPUT_CAP:
        return text[:OUTPUT_CAP] + f"\n… [{len(text) - OUTPUT_CAP} chars truncated]"
    return text
