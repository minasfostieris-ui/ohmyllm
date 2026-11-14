"""PolicyEngine: the single gate every tool call must pass before execution.

The permission mode is owned by the platform (config/TUI/CLI); model output can
never change it. This module only decides; execution lives in tools/runtime.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .. import paths
from ..config.models import Config, PermissionMode
from ..secureio import UnsafePathError, file_identity, is_environment_file, is_within, resolve_path
from .modes import SAFE_COMMANDS, SUBCOMMAND_ALLOWLIST
from .normalize import (
    _PRIV_REQ_LONG,
    _PRIV_REQ_SHORT,
    _PRIV_WRAPPERS,
    _skip_wrapper_flags,
    command_variants,
    normalize_command,
    unresolved_destructive_operands,
)
from .risk import RiskScorer, _basename, level_for

#: In-place write verbs whose destinations bypass redirect detection (S2/S2g).
_WRITE_VERBS = frozenset({"sed", "tee", "cp", "mv", "ln", "install", "dd"})


def _quote_segments(text: str) -> list[str] | None:
    """Split on ``; & |`` OUTSIDE quotes (shlex-style quote state: single
    quotes literal, double quotes honor backslash escapes, backslash escapes
    outside quotes). Returns None when a quote/escape is left unclosed —
    callers fail closed for write verbs (task #71)."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if quote == '"':
            buf.append(ch)
            if ch == '"':
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in ";&|":
            segs.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if quote is not None or escaped:
        return None
    segs.append("".join(buf))
    return segs


def _quote_words(text: str) -> list[tuple[str, int]] | None:
    """shlex-style words with end offsets (posix semantics: single quotes
    literal, double quotes honor backslash escapes, backslash escapes outside
    quotes). Offsets let callers recover the RAW remainder after a word, so
    sudo/doas payloads keep their quoting. None when unclosed (task #71)."""
    words: list[tuple[str, int]] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    in_word = False
    for i, ch in enumerate(text):
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                buf.append(ch)
            continue
        if ch == "\\":
            escaped = True
            in_word = True
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            else:
                buf.append(ch)
            continue
        if ch in "'\"":
            quote = ch
            in_word = True
            continue
        if ch.isspace():
            if in_word:
                words.append(("".join(buf), i))
                buf = []
                in_word = False
            continue
        buf.append(ch)
        in_word = True
    if quote is not None or escaped:
        return None
    if in_word:
        words.append(("".join(buf), len(text)))
    return words


class ToolClass(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NET = "net"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    STATUS = "status"
    MAIL = "mail"
    CONFIG = "config"
    OTHER = "other"


TOOL_CLASSES: dict[str, ToolClass] = {
    "read_file": ToolClass.READ,
    "list_dir": ToolClass.READ,
    "search_files": ToolClass.READ,
    "write_file": ToolClass.WRITE,
    "patch_file": ToolClass.WRITE,
    "run_command": ToolClass.SHELL,
    "http_fetch": ToolClass.NET,
    "email_search": ToolClass.MAIL,
    "email_read": ToolClass.MAIL,
    "email_draft": ToolClass.MAIL,
    "email_send": ToolClass.MAIL,
    "skills_list": ToolClass.STATUS,
    "skills_read": ToolClass.STATUS,
    "tone_create": ToolClass.CONFIG,
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


# Shared sensitive-path predicate: PolicyEngine.is_sensitive AND the @-reference
# expander (agents/context_refs) must agree on what counts as secret material.
_SENSITIVE_RX = (
    r"[/\\]\.ssh([/\\]|$)", r"\.gnupg([/\\]|$)", r"[/\\]\.aws([/\\]|$)",
    r"[/\\]\.azure([/\\]|$)", r"[/\\]\.gcloud([/\\]|$)", r"[/\\]\.config[/\\]gh([/\\]|$)",
    r"keyring", r"keychain", r"\.netrc($|[^-\w])", r"id_rsa", r"\.pem($|\b)", r"\.key($|\b)",
    r"\.git-credentials($|[^-\w])",
    r"[/\\]\.(bash|zsh|sh|python)_history($|[^-\w])",
    r"\.mozilla([/\\]|$)", r"google-chrome([/\\]|$)", r"[/\\]chromium([/\\]|$)",
    r"firefox([/\\]|$)", r"microsoft[/\\]credentials", r"windows[/\\]system32",
    # package-manager / registry credentials and git hook config (M2)
    r"[/\\]\.npmrc($|[^-\w])", r"[/\\]\.pypirc($|[^-\w])",
    r"[/\\]\.docker[/\\]config\.json($|[^-\w])", r"[/\\]\.git[/\\]config($|[^-\w])",
)


def is_sensitive_path(
    raw: str | Path,
    *,
    workspace: Path,
    allow_sensitive_paths: tuple[str, ...] | list[str] = (),
) -> bool:
    """Config-free sensitive predicate shared by policy and context refs.

    Callers without a config (e.g. @-reference expansion) pass no allowlist,
    which is fail-closed: nothing is re-allowed implicitly.
    """
    workspace = Path(workspace).expanduser().resolve(strict=False)
    p = resolve_path(raw, base=workspace, reject_symlinks=False)
    lowered = str(p).lower()
    if is_within(p, paths.global_home()) or is_within(p, workspace / ".ohmyllm"):
        return True
    for configured in allow_sensitive_paths:
        if not configured:
            continue
        try:
            boundary = resolve_path(configured, base=workspace, reject_symlinks=False)
        except (OSError, ValueError):
            continue
        if is_within(p, boundary):
            return False
    if is_environment_file(p):
        return True
    return any(re.search(r, lowered) for r in _SENSITIVE_RX)


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
        return is_within(self._abs(raw), self.workspace)

    def is_system(self, raw: str | Path) -> bool:
        """OS-owned locations: never writable by the agent, in any mode."""
        p = str(self._abs(raw)).lower()
        rx = (
            r"^/etc(/|$)", r"^/usr(/|$)", r"^/bin(/|$)", r"^/sbin(/|$)", r"^/boot(/|$)",
            r"^/sys(/|$)", r"^/proc(/|$)", r"^/dev(/|$)", r"^/lib(/|$)", r"^/lib64(/|$)",
            r"^/var(/|$)", r"^/run(/|$)", r"^/root(/|$)",
            r"^/private/etc(/|$)", r"^/private/var(?!/folders(/|$))(/|$)", r"[\\:]\\windows(\\|$)",
            r"[\\:]\\program files(\\|$)", r"[\\:]\\programdata(\\|$)",
        )
        return any(re.search(r, p) for r in rx)

    def is_sensitive(self, raw: str | Path) -> bool:
        return is_sensitive_path(
            raw, workspace=self.workspace,
            allow_sensitive_paths=self.config.safety.allow_sensitive_paths,
        )

    def _abs(self, raw: str | Path) -> Path:
        return resolve_path(raw, base=self.workspace, reject_symlinks=False)

    def prepare_args(self, tool: str, args: dict) -> dict:
        """Canonicalize path identities before policy, preview, and approval."""
        prepared = dict(args or {})
        path_key = next((k for k in ("path", "file", "directory", "dir") if prepared.get(k)), None)
        try:
            if path_key and classify(tool) in (ToolClass.READ, ToolClass.WRITE):
                prepared[path_key] = str(resolve_path(prepared[path_key], base=self.workspace))
                if classify(tool) is ToolClass.WRITE:
                    prepared["__target_identity"] = file_identity(Path(prepared[path_key]))
            if classify(tool) is ToolClass.SHELL:
                prepared["cwd"] = str(resolve_path(prepared.get("cwd") or self.workspace, base=self.workspace))
        except (UnsafePathError, OSError, ValueError) as exc:
            prepared["__path_error"] = str(exc)
        return prepared

    # ------------------------------------------------------------- evaluation
    def evaluate(
        self,
        tool: str,
        args: dict,
        *,
        user_requested_memory: bool = False,
    ) -> Decision:
        if tool == "run_command":
            floor = self._deny_floor(args or {})
            if floor is not None:
                return floor
        d = self._evaluate(tool, args, user_requested_memory=user_requested_memory)
        return self._apply_op_mode_and_yolo(tool, args or {}, d)

    def _deny_floor(self, args: dict) -> Decision | None:
        """``safety.deny`` globs are a hard floor: evaluated BEFORE the
        SAFE_COMMANDS allowlist and BEFORE any yolo/full_access shortcut, in
        every permission mode. Matching runs over deobfuscated command
        variants (chaining, quotes, escapes, $()/backticks, sh -c, xargs).
        An invalid rule entry (non-string or empty) fails closed and denies
        run_command entirely. The matched rule is named in the decision
        reason, which flows into the audit log."""
        rules = self.config.safety.deny
        if not rules:
            return None
        for rule in rules:
            if not isinstance(rule, str) or not rule.strip():
                return Decision(
                    False, False, 10,
                    ("safety.deny contains an invalid rule (non-string or empty) — "
                     "run_command denied entirely (fail-closed)",),
                )
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        variants = command_variants(command)
        for rule in rules:
            needle = rule.strip()
            lowered = needle.casefold()
            for variant in variants:
                if fnmatch.fnmatchcase(variant, needle) or fnmatch.fnmatchcase(variant.casefold(), lowered):
                    return Decision(
                        False, False, 10,
                        (f"safety.deny rule {needle!r} matched — denied in every mode",),
                    )
        return None

    def _apply_op_mode_and_yolo(self, tool: str, args: dict, d: Decision) -> Decision:
        """Operation modes (chat/plan) strip mutation tools entirely; YOLO
        (one prompt, never persisted) skips approvals — but NOTHING ever
        runs a catastrophic command, not even in full_access."""
        if self._is_catastrophic(tool, args) and not self.config.safety.catastrophic_override:
            return Decision(False, False, 10, (*d.reasons, "catastrophic command — blocked (flip the settings override to allow)"))
        omode = getattr(self.config.agent, "op_mode", "build")
        if omode in ("chat", "plan") and d.allowed:
            cls = classify(tool)
            if cls in (ToolClass.WRITE, ToolClass.SHELL, ToolClass.MAIL):
                hint = "/build" if omode == "plan" else "/build or /plan"
                return Decision(False, False, d.risk,
                                (*d.reasons, f"{tool} is disabled in {omode} mode — switch with {hint}"))
        if (d.allowed and d.requires_approval and getattr(self.config.agent, "yolo", False)
                and classify(tool) not in (ToolClass.SHELL, ToolClass.NET, ToolClass.MAIL, ToolClass.CONFIG)):
            return Decision(True, False, d.risk, (*d.reasons, "YOLO: one-prompt approval bypass"))
        return d

    # R2 C1/W5: (required executables, pattern). A rule only fires when the
    # variant's command-position executable (after assignment stripping) is
    # one of the required verbs — quoted argument literals such as
    # ``echo "rm -rf /"`` are NOT catastrophic. The root operand consumes
    # dot components: /, /., /./, /.. (but NOT /home).
    _CATASTROPHIC_RULES: tuple[tuple[frozenset[str], str], ...] = (
        (frozenset({"rm"}),
         r"\brm\s+(-[a-z]*\s+)*-?[a-z]*[rf][a-z]*\s+"
         r"(/(?:\.{1,2}/)*\.{0,2}(?=[\s'\"*]|$)"
         r"|/(?:home|usr|etc|var|boot|opt|srv)/?(?=[\s'\"*]|$)"
         r"|\$HOME/?(?=[\s'\"*]|$)|\*(?=[\s'\"*]|$))"),
        (frozenset({"rm"}), r"--no-preserve-root"),
        (frozenset({":"}), r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"),
        (frozenset({"mkfs"}), r"\bmkfs(\.\w+)?\b"),
        (frozenset({"dd"}), r"\bdd\b[^|;]*\bof=/dev/"),
        (frozenset({"shred", "wipefs"}), r"\b(shred|wipefs)\b"),
        (frozenset({"shutdown", "reboot", "halt", "poweroff"}),
         r"\b(shutdown|reboot|halt|poweroff)\b"),
        # R3 S3: mode/flag order and 0777 spelling must not dodge the gate.
        (frozenset({"chmod"}),
         r"\bchmod\s+(?:-[a-z]+\s+)*0?777\s+/(\s|$)"
         r"|\bchmod\s+0?777\s+(?:-[a-z]+\s+)*/(\s|$)"),
        (frozenset({"chown"}), r"\bchown\s+-R\s+[^ ]+\s+/(\s|$)"),
        # R3 S6: only absolute/system-root find -delete is catastrophic;
        # workspace-scoped (relative) cleanup stays approvable.
        (frozenset({"find"}),
         r"\bfind\s+(?:-{1,2}\S+\s+)*['\"]?(?:/|~|\$HOME\b)[^|;]*\s-delete(\s|$)"),
        (frozenset({"find"}), r"\bfind\b[^|;]*\s-exec\s+rm\b"),
        (frozenset({"find"}), r"\bfind\b[^|;]*\s-execdir\s+rm\b"),
    )

    def _is_catastrophic(self, tool: str, args: dict) -> bool:
        if tool == "run_command":
            # Match over deobfuscated variants (semantic flags, ${VAR},
            # ANSI-C quoting, chains, wrappers, resolved dynamic targets) —
            # spelling must not dodge the catastrophic gate (C1/C2/H1/C3).
            cmd = str(args.get("command") or "")
            # R2 C3 fail-closed: a destructive executable whose operands the
            # normalizer cannot resolve is catastrophic, never silent-allow.
            if unresolved_destructive_operands(cmd):
                return True
            for variant in command_variants(cmd):
                low = variant.lower()
                tokens = low.split()
                # fork bomb is structural: its "executable" is `:(){`
                if tokens and tokens[0].startswith(":(") and re.search(
                        r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", low):
                    return True
                while tokens and re.match(r"[a-z_][a-z0-9_]*=", tokens[0]):
                    tokens = tokens[1:]
                if not tokens:
                    continue
                exe = _basename(tokens[0])
                # sudo/doas are transparent: position-match the real verb
                if exe in _PRIV_WRAPPERS:
                    payload = _skip_wrapper_flags(
                        " ".join(tokens[1:]), req_short=_PRIV_REQ_SHORT,
                        req_long=_PRIV_REQ_LONG,
                    )
                    tokens = payload.split()
                    if not tokens:
                        continue
                    exe = _basename(tokens[0])
                for verbs, rx in self._CATASTROPHIC_RULES:
                    if (exe in verbs or any(exe.startswith(v + ".") for v in verbs)) \
                            and re.search(rx, low, re.IGNORECASE):
                        return True
            return False
        if tool in ("write_file", "patch_file"):
            target = str(args.get("path") or "")
            return bool(re.match(r"^(/etc|/boot|/dev|/sys|/proc|/usr/lib/systemd)(/|$)", target))
        return False

    def _evaluate(
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
        if args.get("__path_error"):
            return Decision(False, False, 8, (*reasons, str(args["__path_error"])))
        risk = 0

        if cls is ToolClass.OTHER:
            return Decision(False, False, 5, (*reasons, f"unknown tool {tool!r} is not permitted"))

        if cls is ToolClass.STATUS:
            return Decision(True, False, 0, tuple(reasons))

        if cls is ToolClass.CONFIG:
            # tone_create persists a system-prompt fragment: strict forbids
            # it outright; every other mode requires explicit approval and
            # YOLO can never bypass it (H3).
            if mode is PermissionMode.STRICT:
                return Decision(False, False, 5,
                                (*reasons, "strict mode forbids tone_create (persisted prompt change)"))
            return Decision(True, True, 5,
                            (*reasons, "tone_create persists a system-prompt change — approval required"))

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
                    return Decision(False, False, max(risk, 6), (*reasons, "write outside workspace"))
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
                stripped = cmd.strip()
                if stripped.startswith("$"):
                    return Decision(False, False, max(risk, 6),
                                    (*reasons, "variable-indirect executable cannot be verified in the sandbox"))
                base = _basename(stripped.split()[0] if stripped else "")
                if base not in allowed:
                    return Decision(False, False, risk, (*reasons, f"{base!r} is not on the sandbox allowlist"))
                subs = SUBCOMMAND_ALLOWLIST.get(base)
                second = cmd.lower().split()[1] if len(cmd.split()) > 1 else ""
                if subs is not None and second and second not in subs:
                    return Decision(False, False, risk, (*reasons, f"{base} {second} is not on the sandbox allowlist"))
                if crisk.catastrophic or crisk.score >= 6:
                    return Decision(False, False, risk, (*reasons, "too dangerous for the sandbox allowlist"))
                norm = normalize_command(cmd)
                if base == "git" and re.search(r"\bgit\s+config\b", norm) and re.search(
                        r"\b(core\.fsmonitor|core\.hookspath|core\.sshcommand|core\.editor|core\.pager|alias\.)", norm):
                    return Decision(False, False, max(risk, 8),
                                    (*reasons, "git config hook/exec injection is not allowed in the sandbox"))
                if self._redirect_system(cmd):
                    return Decision(False, False, max(risk, 9), (*reasons, "output redirect into a system location"))
                if self._redirect_outside(cmd):
                    return Decision(True, True, max(risk, 6), (*reasons, "output redirect outside the workspace"))
                inline = self._inline_write_targets(cmd)
                if inline is None:
                    return Decision(False, False, max(risk, 9),
                                    (*reasons, "unresolved quoted write target (fail-closed)"))
                for tgt in inline:
                    if self.is_system(tgt) and not self._explicitly_allows(tgt):
                        return Decision(False, False, max(risk, 9),
                                        (*reasons, f"in-place write to system location {tgt!r}"))
                    if not self.within_workspace(tgt):
                        return Decision(True, True, max(risk, 6),
                                        (*reasons, "in-place write outside the workspace"))
                if not self.within_workspace(cwd):
                    return Decision(False, False, risk, (*reasons, "cwd outside workspace"))
                return Decision(True, True, risk, (*reasons, "fresh one-shot shell approval required"))
            if mode is PermissionMode.DANGEROUS:
                if crisk.catastrophic:
                    return Decision(True, True, risk, (*reasons, "catastrophic: explicit approval required"))
                if not self.within_workspace(cwd):
                    return Decision(True, True, risk, (*reasons, "cwd outside workspace"))
                return Decision(True, True, risk, (*reasons, "fresh one-shot shell approval required"))
            # full_access
            if crisk.catastrophic and not self.config.safety.catastrophic_override:
                return Decision(False, False, risk, (*reasons, "catastrophic command blocked (settings override)"))
            norm = normalize_command(cmd)
            if re.search(r"\bgit\s+config\b", norm) and re.search(
                    r"\b(core\.fsmonitor|core\.hookspath|core\.sshcommand|core\.editor|core\.pager|alias\.)", norm):
                # M1: hook/exec injection persists arbitrary code execution —
                # never silent, even in full_access.
                return Decision(True, True, max(risk, 8),
                                (*reasons, "git config hook/exec injection requires approval"))
            # R2 C4: the sensitive-path invariant holds for SHELL too —
            # operand paths and redirect targets go through the same
            # predicate as read_file/write_file (docs/PERMISSIONS.md).
            # R3 S1: relative operands are expanded against the resolved cwd.
            sensitive_targets = self._shell_sensitive_targets(cmd, cwd)
            if sensitive_targets is None:
                return Decision(False, False, max(risk, 9),
                                (*reasons, "unresolved quoted write target (fail-closed)"))
            for tgt in sensitive_targets:
                try:
                    sensitive = self.is_sensitive(tgt)
                except (OSError, ValueError, UnsafePathError):
                    continue
                if sensitive:
                    return Decision(False, False, max(risk, 8),
                                    (*reasons, f"sensitive path {tgt!r} in shell command — denied in every mode"))
            # R3 S2: system writes via shell mirror the WRITE gate — redirects
            # and piped/sudo tee / sed -i are never silent in full_access.
            writes = [m.group(1) for m in re.finditer(r">>?\s*['\"]?([^\s'\"|&;]+)", cmd)]
            inline = self._inline_write_targets(cmd)
            if inline is None:
                return Decision(False, False, max(risk, 9),
                                (*reasons, "unresolved quoted write target (fail-closed)"))
            writes.extend(inline)
            for tgt in writes:
                if tgt.lower() in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                    continue
                try:
                    resolved = str(resolve_path(tgt, base=Path(cwd), reject_symlinks=False))
                    system = self.is_system(resolved)
                except (OSError, ValueError, UnsafePathError):
                    continue
                if system and not self._explicitly_allows(resolved):
                    return Decision(True, True, max(risk, 9),
                                    (*reasons, f"system location {tgt!r}"))
            return Decision(True, False, risk, tuple(reasons))

        if cls is ToolClass.NET:
            risk = 6
            if not self.config.safety.http_fetch_enabled:
                return Decision(False, False, risk, (*reasons, "http_fetch is disabled"))
            if not self.config.safety.http_fetch_allowlist:
                return Decision(False, False, risk, (*reasons, "http_fetch requires an explicit host allowlist"))
            if mode in (PermissionMode.STRICT, PermissionMode.SAFE):
                return Decision(False, False, risk, (*reasons, f"{mode.value} mode forbids network"))
            if mode is PermissionMode.FULL_ACCESS:
                return Decision(True, False, risk, tuple(reasons))
            return Decision(True, True, risk, (*reasons, "network access needs explicit approval"))
        if cls is ToolClass.MAIL:
            return self._evaluate_mail(mode, tool, args, reasons)

        return Decision(False, False, risk, tuple(reasons))

    def _evaluate_mail(self, mode: PermissionMode, tool: str,
                       args: dict, reasons: list[str]) -> Decision:
        """Mail gating. Draft is local and harmless; read/search are network
        reads behind the [mail] enabled switch; SEND is irreversible and an
        exfiltration channel — it stays approval-gated everywhere except
        full_access with an explicit opt-in (and never past the recipient
        allowlist)."""
        m = self.config.mail
        if tool == "email_draft":
            return Decision(True, False, 1, (*reasons, "draft is local-only"))
        if mode in (PermissionMode.STRICT, PermissionMode.SAFE):
            return Decision(False, False, 6, (*reasons, f"{mode.value} mode forbids email"))
        if tool in ("email_search", "email_read"):
            if not m.enabled:
                return Decision(False, False, 6, (*reasons, "email not configured (oml mail)"))
            return Decision(True, False, 5, (*reasons, "mail read: account configured by the user"))
        # email_send
        risk = 7
        if mode in (PermissionMode.SANDBOXED, PermissionMode.DANGEROUS):
            return Decision(True, True, risk, (*reasons, "sending email is irreversible"))
        # full_access: silent only with explicit mail opt-in…
        if not m.enabled:
            return Decision(True, True, risk, (*reasons, "email not configured: approval required"))
        # …and never outside the recipient allowlist when one is set
        allow = [a.strip().lower() for a in m.recipient_allowlist.split(",") if a.strip()]
        to = str(args.get("to") or "").strip().lower()
        if not allow:
            return Decision(True, True, risk,
                            (*reasons, "no mail recipient allowlist configured: approval required"))
        if to not in allow:
            return Decision(True, True, 8, (*reasons, f"{to!r} is not on the mail recipient allowlist"))
        return Decision(True, False, risk, (*reasons, "full_access + mail enabled"))

    # --------------------------------------------------------------- helpers
    def _explicitly_allows(self, target: str | Path) -> bool:
        path = self._abs(target)
        for configured in self.config.safety.allow_sensitive_paths:
            if not configured:
                continue
            try:
                boundary = resolve_path(configured, base=self.workspace, reject_symlinks=False)
            except (OSError, ValueError):
                continue
            if is_within(path, boundary):
                return True
        return False

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

    def _inline_write_targets(self, cmd: str) -> list[str] | None:
        """File operands of in-place writers that bypass redirect detection:
        ``sed -i SCRIPT FILE…`` and ``tee [-a] FILE…`` (M1). R3 S2: scanned at
        EVERY segment position (piped ``… | tee``) and through transparent
        sudo/doas prefixes, not just the first token of the whole command.
        R3 S2g (task #70): the same treatment covers the rest of the
        write-vector family — cp/mv/ln/install destinations (last operand or
        ``-t DIR``) and ``dd of=`` — keyed on the destination only.
        Task #71: a quote-aware pass on the RAW command is unioned in (the
        normalized form strips quotes, so ``cp x "/etc/my dir"`` would split
        there). Returns None — fail closed — when a write-verb segment has
        an unclosed quote/escape; non-write segments keep legacy behavior."""
        targets: list[str] = []
        for seg in re.split(r"[;&|]+", normalize_command(cmd)):
            tokens = seg.split()
            while tokens and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
                tokens = tokens[1:]
            if not tokens:
                continue
            if _basename(tokens[0]) in _PRIV_WRAPPERS:
                payload = _skip_wrapper_flags(
                    " ".join(tokens[1:]), req_short=_PRIV_REQ_SHORT,
                    req_long=_PRIV_REQ_LONG,
                )
                tokens = payload.split()
                if not tokens:
                    continue
            targets.extend(self._dests_for_verb(tokens))
        raw = self._raw_write_targets(cmd)
        if raw is None:
            return None
        targets.extend(raw)
        return targets

    @staticmethod
    def _dests_for_verb(tokens: list[str]) -> list[str]:
        """Destination operands of one segment whose first token is already
        the executable (assignment/sudo stripping happened upstream)."""
        if not tokens:
            return []
        base = _basename(tokens[0])
        args = [t for t in tokens[1:] if not t.startswith("-")]
        if base == "sed" and any(t == "-i" or t.startswith("-i") for t in tokens[1:]):
            # sed needs a script operand; everything after it is a file
            return args[1:] if len(args) >= 2 else []
        if base == "tee":
            return args
        if base in ("cp", "mv", "ln", "install"):
            return PolicyEngine._copy_dest_args(tokens[1:], dirs_only=base == "install")
        if base == "dd":
            return [t[3:] for t in tokens[1:] if t.startswith("of=") and len(t) > 3]
        return []

    def _raw_write_targets(self, cmd: str) -> list[str] | None:
        """Quote-aware destination pass on the RAW command (task #71): honors
        single/double quotes, escaped quotes and backslash escapes, and keeps
        quoted ``; & |`` inside destinations. None = a write-verb segment is
        unparseable (unclosed quote/escape) -> fail closed."""
        segments = _quote_segments(cmd)
        suspect = segments is None
        if segments is None:
            segments = re.split(r"[;&|]+", cmd)
        targets: list[str] = []
        for seg in segments:
            words = None if suspect else _quote_words(seg)
            if words is None:
                # Unparseable/unclosed: fail closed only when a write verb
                # leads the segment; anything else keeps legacy behavior.
                tokens = seg.split()
                while tokens and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
                    tokens = tokens[1:]
                if tokens and _basename(tokens[0]) in _PRIV_WRAPPERS:
                    payload = _skip_wrapper_flags(
                        " ".join(tokens[1:]), req_short=_PRIV_REQ_SHORT,
                        req_long=_PRIV_REQ_LONG,
                    )
                    tokens = payload.split()
                if tokens and _basename(normalize_command(tokens[0])) in _WRITE_VERBS:
                    return None
                continue
            texts = [w for w, _ in words]
            ends = [e for _, e in words]
            k = 0
            while k < len(texts) and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", texts[k]):
                k += 1
            if k >= len(texts):
                continue
            if _basename(normalize_command(texts[k])) in _PRIV_WRAPPERS:
                # Strip the wrapper from the RAW remainder so the payload's
                # quoting survives (sudo cp x "/etc/my dir").
                payload = _skip_wrapper_flags(
                    seg[ends[k]:].strip(), req_short=_PRIV_REQ_SHORT,
                    req_long=_PRIV_REQ_LONG,
                )
                sub = _quote_words(payload)
                if sub is None:
                    tokens = payload.split()
                    if tokens and _basename(normalize_command(tokens[0])) in _WRITE_VERBS:
                        return None
                    continue
                texts = [w for w, _ in sub]
                k = 0
                while k < len(texts) and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", texts[k]):
                    k += 1
                if k >= len(texts):
                    continue
            if _basename(normalize_command(texts[k])) not in _WRITE_VERBS:
                continue
            targets.extend(self._dests_for_verb(texts[k:]))
        return targets

    @staticmethod
    def _copy_dest_args(flags: list[str], *, dirs_only: bool = False) -> list[str]:
        """Destination operands of cp/mv/ln/install: ``-t DIR`` wins, else the
        last operand (``install -d`` creates every operand directory). Flags
        that consume a separated argument (-t/-m/-o/-g/-S/-D…) are skipped so
        their values are never mistaken for destinations."""
        operands: list[str] = []
        target_dir: str | None = None
        make_dirs = False
        skip = 0
        for tok in flags:
            if skip:
                skip -= 1
                continue
            if tok in ("-t", "--target-directory"):
                target_dir = ""  # completed by the next token, if any
                continue
            if target_dir == "":
                target_dir = tok
                continue
            if tok.startswith("-"):
                if tok in ("-m", "-o", "-g", "-S", "-D", "--mode", "--owner",
                           "--group", "--suffix", "--directory"):
                    skip = 1
                elif dirs_only and tok == "-d":
                    make_dirs = True
                continue
            operands.append(tok)
        if target_dir:
            return [target_dir]
        if make_dirs:
            return operands
        return operands[-1:] if operands else []

    def _shell_sensitive_targets(self, cmd: str, cwd: str | Path | None = None) -> list[str] | None:
        """Operand paths + redirect targets of a shell command (R2 C4).
        Reuses the redirect/inline-write extraction. R3 S1: relative operands
        are expanded against the resolved cwd (the file-tool resolver), so
        ``cat .ssh/id_rsa`` with cwd=$HOME is gated exactly like
        ``cat ~/.ssh/id_rsa``; ``~``/``$HOME``/absolute tokens keep working."""
        base = Path(cwd) if cwd else self.workspace
        home = str(Path.home())

        def absolutize(tok: str) -> str | None:
            tok = tok.replace("${HOME}", home).replace("$HOME", home)
            if tok.startswith(("~", "/")):
                return tok
            try:
                return str(resolve_path(tok, base=base, reject_symlinks=False))
            except (OSError, ValueError, UnsafePathError):
                return None

        targets: list[str] = []
        for m in re.finditer(r">>?\s*['\"]?([^\s'\"|&;]+)", cmd):
            targets.append(m.group(1))
        inline = self._inline_write_targets(cmd)
        if inline is None:
            return None
        targets.extend(inline)
        norm = normalize_command(cmd)
        for seg in re.split(r"[;&|]+", norm):
            for tok in seg.split()[1:]:
                if not tok.startswith("-"):
                    targets.append(tok)
        out: list[str] = []
        for t in targets:
            resolved = absolutize(t)
            if resolved is not None:
                out.append(resolved)
        return out


def describe(tool: str, args: dict, decision: Decision) -> str:
    """Human summary for approval modals / audit rows."""
    key = args.get("command") or args.get("path") or args.get("url") or args.get("text") or ""
    key = str(key)[:160]
    return f"{tool}({key}) — {decision.summary()}"
