"""get_system_status — safe introspection tool for the agent."""

from __future__ import annotations

from .. import paths
from ..vram.detect import cpu_ram_bytes, detect_gpus, human_bytes
from .spec import ToolContext, ToolResult


def get_system_status(ctx: ToolContext, args: dict) -> ToolResult:
    cfg = ctx.config
    lines = [
        f"permission mode : {cfg.permission_mode.value}",
        f"default backend : {cfg.default_backend.value}",
        f"workspace       : {ctx.workspace}",
        f"soul.md         : {ctx.soul.path if ctx.soul else 'n/a'} ({'exists' if ctx.soul and ctx.soul.exists() else 'missing'})",
        f"memory writes   : {'allowed' if cfg.memory.agent_writes else 'disabled'}",
        f"global home     : {paths.global_home()}",
        f"audit log       : {paths.audit_log_path()}",
    ]
    provider = ctx.status_provider
    if provider is not None:
        try:
            extra = provider()
            for k, v in extra.items():
                if v is not None:
                    lines.append(f"{k:<15}: {v}")
        except Exception:  # noqa: BLE001 - status must never fail the turn
            pass
    gpus = detect_gpus()
    if gpus:
        for g in gpus:
            free = human_bytes(g.free_bytes) if g.free_bytes is not None else "?"
            lines.append(f"gpu             : {g.model} — {human_bytes(g.total_bytes)} total, {free} free ({g.source})")
    else:
        lines.append("gpu             : not detected")
    ram = cpu_ram_bytes()
    if ram:
        lines.append(f"system ram      : {human_bytes(ram)}")
    return ToolResult(True, "\n".join(lines))
