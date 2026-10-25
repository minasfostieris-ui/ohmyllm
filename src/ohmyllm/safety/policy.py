"""PolicyEngine: the single gate every tool call must pass before execution.

The permission mode is owned by the platform (config/TUI/CLI); model output can
never change it. This module only decides; execution lives in tools/runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..config.models import Config, PermissionMode
from .modes import SAFE_COMMANDS, SUBCOMMAND_ALLOWLIST
from .risk import RiskScorer, _basename, level_for


class ToolClass(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NET = "net"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    STATUS = "status"
    OTHER = "other"


TOOL_CLASSES: dict[str, ToolClass] = {
    "read_file": ToolClass.READ,
    "list_dir": ToolClass.READ,
    "search_files": ToolClass.READ,
    "write_file": ToolClass.WRITE,
    "patch_file": ToolClass.WRITE,
    "run_command": ToolClass.SHELL,
    "http_fetch": ToolClass.NET,
    "skills_list": ToolClass.STATUS,
    "skills_read": ToolClass.STATUS,
    "soul_read": ToolClass.MEMORY_READ,
    "soul_search": ToolClass.MEMORY_READ,
    "soul_append": ToolClass.MEMORY_WRITE,
    "soul_upsert": ToolClass.MEMORY_WRITE,
    "soul_forget": ToolClass.MEMORY_WRITE,
    "get_system_status": ToolClass.STATUS,
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    requires_approval: bool = False
    risk: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def level(self) -> str:
        return level_for(self.risk)

    def summary(self) -> str:
        bits = [f"risk={self.level}({self.risk})"]
        if self.requires_approval:
            bits.append("approval required")
        bits.extend(self.reasons)
        return "; ".join(bits)


def classify(tool: str) -> ToolClass:
    return TOOL_CLASSES.get(tool, ToolClass.OTHER)


@dataclass
class PolicyEngine:
    """Workspace-confined sandbox + mode gates + risk scoring."""

    config: Config
    workspace: Path
    scorer: RiskScorer = field(default_factory=RiskScorer)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve(strict=False)

    @property
    def mode(self) -> PermissionMode:
        return self.config.permission_mode

    # ------------------------------------------------------------------ paths
    def within_workspace(self, raw: str | Path) -> bool:
        p = self._abs(raw)
        try:
            return p == self.workspace or p.is_relative_to(self.workspace)
        except (ValueError, OSError):
            return False

    def is_system(self, raw: str | Path) -> bool:
        """OS-owned locations: never writable by the agent, in any mode."""
        p = str(self._abs(raw)).lower()
        rx = (
            r"^/etc(/|$)", r"^/usr(/|$)", r"^/bin(/|$)", r"^/sbin(/|$)", r"^/boot(/|$)",
            r"^/sys(/|$)", r"^/proc(/|$)", r"^/dev(/|$)", r"^/lib(/|$)", r"^/lib64(/|$)",
            r"^/var(/|$)", r"^/run(/|$)", r"^/root(/|$)", r"[\\:]\\windows(\\|$)",
            r"[\\:]\\program files(\\|$)", r"[\\:]\\programdata(\\|$)",
        )
        return any(re.search(r, p) for r in rx)

    def is_sensitive(self, raw: str | Path) -> bool:
        p = self._abs(raw)
        lowered = str(p).lower()
        if any(g and lowered.startswith(g.lower()) for g in self.config.safety.allow_sensitive_paths):
            return False  # explicitly re-allowed
        rx = (
            r"[/\\]\.ssh([/\\]|$)", r"\.gnupg([/\\]|$)", r"[/\\]\.aws([/\\]|$)",
            r"[/\\]\.azure([/\\]|$)", r"[/\\]\.gcloud([/\\]|$)", r"[/\\]\.config[/\\]gh([/\\]|$)",
            r"keyring", r"keychain", r"\.netrc($|[^-\w])", r"id_rsa", r"\.pem($|\b)", r"\.key($|\b)",
            r"[/\\]\.(bash|zsh|sh|python)_history($|[^-\w])",
            r"\.mozilla([/\\]|$)", r"google-chrome([/\\]|$)", r"[/\\]chromium([/\\]|$)",
            r"firefox([/\\]|$)", r"microsoft[/\\]credentials", r"windows[/\\]system32",
        )
        return any(re.search(r, lowered) for r in rx)

    def _abs(self, raw: str | Path) -> Path:
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve(strict=False)

    # ------------------------------------------------------------- evaluation
    def evaluate(
        self,
        tool: str,
        args: dict,
        *,
        user_requested_memory: bool = False,
    ) -> Decision:
        mode = self.mode
        cls = classify(tool)
        args = args or {}
        reasons: list[str] = [f"mode={mode.value}"]
        risk = 0

        if cls is ToolClass.OTHER:
            return Decision(False, False, 5, (*reasons, f"unknown tool {tool!r} is not permitted"))

        if cls is ToolClass.STATUS:
            return Decision(True, False, 0, tuple(reasons))

        if cls in (ToolClass.MEMORY_READ, ToolClass.MEMORY_WRITE):
            if not self.config.memory.enabled:
                return Decision(False, False, 1, (*reasons, "memory disabled in settings"))
            if cls is ToolClass.MEMORY_WRITE and not self.config.memory.agent_writes:
                return Decision(False, False, 1, (*reasons, "agent memory writes disabled in settings"))
            if mode is PermissionMode.STRICT and not user_requested_memory:
                return Decision(
                    False, False, 1,
                    (*reasons, "strict mode: memory writes only when the user explicitly asks (use /remember)"),
                )
            risk = 2 if cls is ToolClass.MEMORY_WRITE else 0
            return Decision(True, risk >= 9, risk, tuple(reasons))

        if cls is ToolClass.READ:
            target = args.get("path") or args.get("directory") or args.get("dir") or "."
            risk = max(risk, self.scorer.score_path(str(target), write=False))
            if self.is_sensitive(target) and not self._explicitly_allows(target):
                return Decision(False, False, risk, (*reasons, f"{target!r} is a sensitive location"))
            if mode in (PermissionMode.STRICT, PermissionMode.SAFE):
                if not self.within_workspace(target):
                    return Decision(False, False, risk, (*reasons, f"{target!r} is outside workspace"))
            elif mode is PermissionMode.SANDBOXED:
                # sandboxed asks; it does not silently forbid the user's own disk
                if not self.within_workspace(target):
                    return Decision(True, True, max(risk, 5), (*reasons, "read outside workspace"))
            elif mode is PermissionMode.DANGEROUS:
                if not self.within_workspace(target):
                    if self.is_sensitive(target) and not self._explicitly_allows(target):
                        return Decision(False, False, risk, (*reasons, f"{target!r} is a sensitive location"))
                    return Decision(True, True, max(risk, 5), (*reasons, "read outside workspace"))
            elif mode is PermissionMode.FULL_ACCESS and self.is_sensitive(target) and not self._explicitly_allows(target):
                return Decision(False, False, risk, (*reasons, f"{target!r} is a sensitive location"))
            return Decision(True, False, risk, tuple(reasons))

        if cls is ToolClass.WRITE:
            if mode in (PermissionMode.STRICT, PermissionMode.SAFE):
                return Decision(False, False, 3, (*reasons, f"{mode.value} mode forbids file writes"))
            target = args.get("path") or args.get("file") or ""
            risk = max(risk, self.scorer.score_path(str(target), write=True))
            if mode is PermissionMode.SANDBOXED:
                if self.is_sensitive(target) and not self._explicitly_allows(target):
                    return Decision(False, False, risk, (*reasons, f"{target!r} is a sensitive location"))
                if self.is_system(target) and not self._explicitly_allows(target):
                    return Decision(False, False, max(risk, 9), (*reasons, f"{target!r} is a system location"))
                if not self.within_workspace(target):
                    return Decision(True, True, max(risk, 6), (*reasons, "write outside workspace"))
                needs = self.config.safety.workspace_write_requires_confirmation
                return Decision(True, needs, risk, (*reasons, "workspace write"))
            if mode is PermissionMode.DANGEROUS:
                if self.is_sensitive(target) and not self._explicitly_allows(target):
                    return Decision(False, False, risk, (*reasons, f"{target!r} is a sensitive location"))
                if self.is_system(target) and not self._explicitly_allows(target):
                    return Decision(False, False, max(risk, 9), (*reasons, f"{target!r} is a system location"))
                if not self.within_workspace(target):
                    return Decision(True, True, max(risk, 6), (*reasons, "write outside workspace"))
                return Decision(True, risk >= 4, risk, tuple(reasons))
            # full_access
            if self.is_sensitive(target) and not self._explicitly_allows(target):
                return Decision(False, False, risk, (*reasons, f"{target!r} is a sensitive location"))
            if self.is_system(target) and not self._explicitly_allows(target):
                return Decision(True, True, max(risk, 9), (*reasons, f"system location {target!r}"))
            return Decision(True, False, risk, tuple(reasons))

        if cls is ToolClass.SHELL:
            if mode in (PermissionMode.STRICT, PermissionMode.SAFE):
                return Decision(False, False, 4, (*reasons, f"{mode.value} mode forbids shell"))
            cmd = str(args.get("command", ""))
            for bad in self.config.safety.command_denylist:
                if bad and bad.lower() in cmd.lower():
                    return Decision(False, False, 7, (*reasons, f"command blocklist rule: {bad!r}"))
            cwd = str(args.get("cwd") or self.workspace)
            crisk = self.scorer.score_command(cmd)
            risk = max(risk, crisk.score)
            reasons.extend(crisk.reasons)
            if mode is PermissionMode.SANDBOXED:
                allowed = SAFE_COMMANDS | {c.lower() for c in self.config.safety.command_allowlist_extra}
                base = _basename(cmd.strip().split()[0] if cmd.strip() else "")
                if base not in allowed:
                    return Decision(False, False, risk, (*reasons, f"{base!r} is not on the sandbox allowlist"))
                subs = SUBCOMMAND_ALLOWLIST.get(base)
                second = cmd.lower().split()[1] if len(cmd.split()) > 1 else ""
                if subs is not None and second and second not in subs:
                    return Decision(False, False, risk, (*reasons, f"{base} {second} is not on the sandbox allowlist"))
                if crisk.catastrophic or crisk.score >= 6:
                    return Decision(False, False, risk, (*reasons, "too dangerous for the sandbox allowlist"))
                if self._redirect_system(cmd):
                    return Decision(False, False, max(risk, 9), (*reasons, "output redirect into a system location"))
                if self._redirect_outside(cmd):
                    return Decision(True, True, max(risk, 6), (*reasons, "output redirect outside the workspace"))
                if not self.within_workspace(cwd):
                    return Decision(False, False, risk, (*reasons, "cwd outside workspace"))
                return Decision(True, crisk.score >= 4, risk, tuple(reasons))
            if mode is PermissionMode.DANGEROUS:
                if crisk.catastrophic:
                    return Decision(True, True, risk, (*reasons, "catastrophic: explicit approval required"))
                if not self.within_workspace(cwd):
                    return Decision(True, True, risk, (*reasons, "cwd outside workspace"))
                return Decision(True, crisk.score >= 3, risk, tuple(reasons))
            # full_access
            if crisk.catastrophic and not self.config.safety.catastrophic_override:
                return Decision(False, False, risk, (*reasons, "catastrophic command blocked (settings override)"))
            return Decision(True, False, risk, tuple(reasons))

        if cls is ToolClass.NET:
            risk = 6
            enabled = self.config.safety.http_fetch_enabled
            if mode in (PermissionMode.STRICT, PermissionMode.SAFE):
                return Decision(False, False, risk, (*reasons, f"{mode.value} mode forbids network"))
            if mode is PermissionMode.SANDBOXED:
                if not enabled:
                    return Decision(False, False, risk, (*reasons, "network disabled by default in sandboxed mode"))
                return Decision(True, True, risk, (*reasons, "http_fetch enabled: approval required"))
            if mode is PermissionMode.DANGEROUS:
                return Decision(True, True, risk, (*reasons, "network access needs explicit approval"))
            # full_access
            if not enabled and not self.config.safety.catastrophic_override:
                # full access still honors the http_fetch kill switch
                return Decision(True, True, risk, (*reasons, "http_fetch disabled: approval required"))
            return Decision(True, False, risk, tuple(reasons))

        return Decision(False, False, risk, tuple(reasons))

    # --------------------------------------------------------------- helpers
    def _explicitly_allows(self, target: str | Path) -> bool:
        low = str(self._abs(target)).lower()
        return any(g and low.startswith(g.lower()) for g in self.config.safety.allow_sensitive_paths)

    def _redirect_outside(self, cmd: str) -> bool:
        for m in re.finditer(r">>?\s*['\"]?([^\s'\"|&;]+)", cmd):
            tgt = m.group(1)
            if tgt.lower() == "/dev/null":
                continue
            if not self.within_workspace(tgt):
                return True
        return False

    def _redirect_system(self, cmd: str) -> bool:
        for m in re.finditer(r">>?\s*['\"]?([^\s'\"|&;]+)", cmd):
            tgt = m.group(1)
            if tgt.lower() in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                continue
            if self.is_system(tgt):
                return True
        return False


def describe(tool: str, args: dict, decision: Decision) -> str:
    """Human summary for approval modals / audit rows."""
    key = args.get("command") or args.get("path") or args.get("url") or args.get("text") or ""
    key = str(key)[:160]
    return f"{tool}({key}) — {decision.summary()}"
