"""Explicitly gated shell execution with bounded output and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time

from ..secureio import UnsafePathError, resolve_path, sanitized_environment
from .spec import ToolContext, ToolResult

OUTPUT_CAP = 6000
CHUNK = 16_384
DRAIN_TIMEOUT = 1.5


async def _drain(reader: asyncio.StreamReader | None) -> str:
    if reader is None:
        return ""
    kept = bytearray()
    total = 0
    while True:
        chunk = await reader.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if len(kept) < OUTPUT_CAP:
            kept.extend(chunk[:OUTPUT_CAP - len(kept)])
    text = kept.decode("utf-8", errors="replace").strip()
    if total > len(kept):
        text += f"\n… [{total - len(kept)} bytes truncated]"
    return text


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Terminate the complete process group even when its leader already exited."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except TimeoutError:
                pass
        await asyncio.sleep(0.1)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1.5)
        return
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.5)
    except (ProcessLookupError, TimeoutError):
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _collect_drains(*tasks: asyncio.Task[str]) -> tuple[str, ...]:
    done, pending = await asyncio.wait(tasks, timeout=DRAIN_TIMEOUT)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return tuple(task.result() if task in done and not task.cancelled() else "" for task in tasks)


async def run_command(ctx: ToolContext, args: dict) -> ToolResult:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return ToolResult(False, "", "empty command")
    try:
        cwd = resolve_path(args.get("cwd") or ctx.workspace, base=ctx.workspace)
    except (UnsafePathError, OSError, ValueError) as exc:
        return ToolResult(False, "", f"unsafe working directory: {exc}")
    if not cwd.is_dir():
        return ToolResult(False, "", f"working directory does not exist: {cwd}")
    timeout = max(1.0, min(float(args.get("timeout") or ctx.config.agent.tool_timeout), 600.0))
    env = sanitized_environment({"OMLLM_AGENT": "1"})
    start = time.perf_counter()
    shell = "/bin/sh" if os.name == "posix" else None
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            executable=shell,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=os.name == "posix",
        )
        stdout_task = asyncio.create_task(_drain(proc.stdout))
        stderr_task = asyncio.create_task(_drain(proc.stderr))
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            await _terminate(proc)
            await _collect_drains(stdout_task, stderr_task)
            return ToolResult(False, "", f"command timed out after {timeout:.0f}s: {cmd}")
        # A shell can exit while background descendants retain its pipes.
        await _terminate(proc)
        out, err = await _collect_drains(stdout_task, stderr_task)
    except asyncio.CancelledError:
        if "proc" in locals():
            await _terminate(proc)
        if "stdout_task" in locals() and "stderr_task" in locals():
            await _collect_drains(stdout_task, stderr_task)
        raise
    except OSError as exc:
        return ToolResult(False, "", f"spawn failed: {exc}")
    duration = (time.perf_counter() - start) * 1000
    parts = [f"$ {cmd}", f"[cwd: {cwd}] [exit {proc.returncode}] [{duration:.0f}ms]"]
    if out:
        parts.append("--- stdout ---\n" + out)
    if err:
        parts.append("--- stderr ---\n" + err)
    if not out and not err:
        parts.append("(no output)")
    body = "\n".join(parts)
    ok = proc.returncode == 0
    return ToolResult(ok, body, None if ok else f"exit code {proc.returncode}")


def preview_shell(args: dict, ctx: ToolContext | None = None) -> str:
    cmd = str(args.get("command") or "")
    cwd = str(args.get("cwd") or (ctx.workspace if ctx else "(workspace)"))
    return f"command: {cmd}\nworking dir: {cwd}\ntimeout: {args.get('timeout', 'default')}"


def _cap(text: str) -> str:
    if len(text) > OUTPUT_CAP:
        tail = 1500
        # keep the end too: compilers, pytest and tracebacks put the actual
        # error LAST — head-only clipping forces a re-run and wastes tokens
        return (text[: OUTPUT_CAP - tail]
                + f"\n… [{len(text) - OUTPUT_CAP} chars skipped; tail follows]\n"
                + text[-tail:])
    return text
