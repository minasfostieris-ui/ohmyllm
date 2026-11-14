"""Command risk scoring: destructive-pattern detection and risk levels."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import (
    _ENV_REQ_LONG,
    _ENV_REQ_SHORT,
    _PRIV_REQ_LONG,
    _PRIV_REQ_SHORT,
    _PRIV_WRAPPERS,
    _SHELL_WRAPPERS,
    _XARGS_OPT_SHORT,
    _XARGS_REQ_LONG,
    _XARGS_REQ_SHORT,
    _skip_wrapper_flags,
    normalize_command,
)

#: (pattern, score, reason, verbs) — catastrophic is score>=9 anyway.
#: *verbs* is the command-position requirement (R2 W5): the rule only fires
#: when one of these executables appears as a segment's executable (after
#: assignment/wrapper stripping), so quoted literals like
#: ``echo "rm -rf /"`` are not scored as destruction. None = anywhere.
_RULES: list[tuple[str, int, str, frozenset[str] | None]] = [
    # filesystem destruction
    (r"\brm\s+(-[a-zA-Z]+\s+)*(-[rRf]\S*\s+)*(-[fRr]\S*\s+)*['\"]?/(?:\.{1,2}/)*\.{0,2}(?=[\s'\"*]|$)", 10, "rm of root filesystem", frozenset({"rm"})),
    (r"\brm\s+-[rRf]{1,3}\b", 6, "recursive/forced rm", frozenset({"rm"})),
    (r"\brm\s+(-[a-zA-Z]+\s+)*['\"]?(~/?(?=\s|$)|\$HOME/?(?=\s|$))", 10, "rm of home directory", frozenset({"rm"})),
    (r"\bshred\b|\bwipefs\b", 8, "secure erase", frozenset({"shred", "wipefs"})),
    (r"\btruncate\s+-s\s*0\b", 7, "file truncation to zero", frozenset({"truncate"})),
    # raw devices / disks
    (r"\bmkfs(\.\w+)?\b", 10, "format filesystem (mkfs)", frozenset({"mkfs"})),
    (r"\bdd\b[^|;]*\bof=/dev/", 10, "dd write to raw device", frozenset({"dd"})),
    (r"\bfind\b[^|;]*\s-delete(\s|$)", 10, "find -delete mass removal", frozenset({"find"})),
    (r"\bfind\b[^|;]*\s-exec(dir)?\s+rm\b", 10, "find -exec rm mass removal", frozenset({"find"})),
    (r">\s*/dev/(sd|nvme|hd|disk|mapper)", 10, "redirect onto raw device", None),
    (r"\bfdisk\b|\bparted\b|\bmkswap\b|\blvm\b", 9, "disk partitioning/volumes", frozenset({"fdisk", "parted", "mkswap", "lvm"})),
    (r"\bmount\b|\bumount\b", 6, "filesystem mount change", frozenset({"mount", "umount"})),
    # power / system
    (r"\b(shutdown|reboot|halt|poweroff)\b", 9, "power state change", frozenset({"shutdown", "reboot", "halt", "poweroff"})),
    (r"\bsystemctl\s+(poweroff|reboot|halt)\b", 9, "power state change via systemctl", frozenset({"systemctl"})),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", 9, "fork bomb", frozenset({":"})),
    # permissions
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(?:\s|\*|$)", 9, "chmod 777 on /", frozenset({"chmod"})),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*[-+rRwxa]*\s*777\s+~", 9, "chmod 777 on home", frozenset({"chmod"})),
    (r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\b|\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\b", 5, "recursive permission change", frozenset({"chmod", "chown"})),
    (r"\bsudo\b|\bdoas\b", 6, "privilege escalation", frozenset({"sudo", "doas"})),
    # remote code execution
    (r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba)?sh\b", 10, "piped remote script to shell", frozenset({"curl", "wget"})),
    (r"\b(curl|wget)\b[^|;]*\|\s*python3?\b", 10, "piped remote script to python", frozenset({"curl", "wget"})),
    (r"\beval\s*\$\(\s*(curl|wget)\b", 8, "eval of remote content", frozenset({"eval"})),
    # version control damage
    (r"\bgit\s+push\b[^|;]*(--force|-f)\b", 6, "force push", frozenset({"git"})),
    (r"\bgit\s+config\b[^|;]*\b(core\.fsmonitor|core\.hookspath|core\.sshcommand|core\.editor|core\.pager|alias\.)", 8, "git config hook/exec injection", frozenset({"git"})),
    (r"\bgit\s+reset\s+--hard\b", 5, "hard reset discards work", frozenset({"git"})),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", 5, "git clean removes untracked files", frozenset({"git"})),
    (r"\bgit\s+(filter-branch|filter-repo)\b", 7, "history rewrite", frozenset({"git"})),
    # package/system mutation
    (r"\b(apt|apt-get|dnf|yum|pacman|zypper|brew|choco|winget)\s+install\b", 6, "system package install", frozenset({"apt", "apt-get", "dnf", "yum", "pacman", "zypper", "brew", "choco", "winget"})),
    (r"\b(pip|pip3|uv)\s+install\b", 5, "package install", frozenset({"pip", "pip3", "uv"})),
    (r"\bnpm\s+(install|i)\b", 4, "node package install", frozenset({"npm"})),
    # in-place editors / arbitrary interpreter payloads (M1)
    (r"\bsed\b[^|;]*\s-i\b", 5, "sed in-place file edit", frozenset({"sed"})),
    (r"shutil\.rmtree|os\.system\s*\(|subprocess\.(call|run|Popen)\s*\(|os\.exec\w*\s*\(|child_process", 9, "destructive interpreter payload", frozenset({"python", "python3", "node", "perl", "ruby", "sh", "bash", "zsh", "dash", "ksh", "eval"})),
    # process control
    (r"\b(killall|pkill)\b|\bkill\s+-9\b", 5, "process kill", frozenset({"killall", "pkill", "kill"})),
    # credential / privacy access
    (r"\.ssh\b|id_rsa|\.gnupg|\.aws\b|keyring|\.netrc", 8, "credential store access", None),
    (r"\.(bash|zsh|sh|python)_history\b", 7, "shell history access", None),
    (r"\.mozilla|google-chrome|chromium|firefox", 7, "browser profile access", None),
    (r"(^|\s)\.env(\.\w+)?(\s|$)", 6, "dotenv file access", None),
    # output redirection onto absolute paths
    (r">>?\s*['\"]?/(?!dev/null)(?:[\w./-]+)", 5, "redirect to absolute path", None),
    (r">>?\s*['\"]?(~|\$HOME)/", 6, "redirect into home", None),
    # windows equivalents
    (r"\bformat\s+[a-z]:", 10, "windows format", frozenset({"format"})),
    (r"\bdiskpart\b", 9, "windows disk partitioning", frozenset({"diskpart"})),
    (r"\bdel\s+/[sq]\b|\brd\s+/s\b", 8, "windows recursive delete", frozenset({"del", "rd"})),
    (r"\bRemove-Item\b[^;]*-Re(curse)?\b[^;]*-Force|\bRemove-Item\b[^;]*-Force[^;]*-Re(curse)?", 8, "powershell recursive forced delete", None),
    (r"\bSet-ExecutionPolicy\b", 7, "powershell execution policy change", None),
    (r"\b(Stop-Computer|Restart-Computer)\b", 9, "windows power state", None),
    (r"\breg\s+(delete|add)\b", 6, "registry change", frozenset({"reg"})),
    (r"\bschtasks\b", 6, "scheduled task change", frozenset({"schtasks"})),
]

_COMPILED: list[tuple[re.Pattern[str], int, str, frozenset[str] | None]] = [
    (re.compile(p, re.IGNORECASE), s, r, v) for p, s, r, v in _RULES
]

_WRITEISH_SUBCOMMANDS: dict[str, set[str]] = {
    "git": {"add", "commit", "checkout", "switch", "stash", "reset", "merge", "rebase", "push", "clean", "config"},
    "pip": {"install", "uninstall"}, "pip3": {"install", "uninstall"},
    "uv": {"add", "remove", "sync", "pip"},
    "npm": {"install", "i", "uninstall", "link"},
    "make": set(), "cargo": {"add", "rm", "build", "clean", "update", "fix"},
}


@dataclass(frozen=True)
class CommandRisk:
    score: int
    reasons: tuple[str, ...] = ()
    catastrophic: bool = False

    @property
    def level(self) -> str:
        return level_for(self.score)


@dataclass
class RiskScorer:
    """Scores shell commands and paths. Pure logic; no I/O."""

    def score_command(self, command: str) -> CommandRisk:
        reasons: list[str] = []
        score = 0
        # Deobfuscate first (quotes/escapes/ANSI-C/${VAR}/semantic flags) so
        # spelling tricks cannot dodge the rules (C1/C2/H1). One canonical
        # string — NOT command_variants — so stray tokens cannot inflate it.
        command = normalize_command(command)
        if command.strip():
            lowered = command.lower()
            first = lowered.split()
            base = _basename(first[0]) if first else ""
            score, reasons = self._base_score(base, lowered, first), []
            # R2 W5: destructive-verb rules only fire when the verb sits at
            # a segment's command position — argument literals (echo/grep/
            # git-commit mentioning "rm -rf /") must not score as destruction.
            verbs = self._positional_verbs(command)
            matched: list[tuple[int, str]] = []
            for pattern, pat_score, reason, need in _COMPILED:
                if need is not None and not (need & verbs):
                    continue
                if pattern.search(command):
                    matched.append((pat_score, reason))
            for m_score, m_reason in sorted(matched, reverse=True):
                if m_score > score:
                    score = m_score
                if m_reason not in reasons:
                    reasons.append(m_reason)
            # interpreter -c/-e payloads are scored recursively (M1): the
            # inline code is the real command, not the interpreter wrapper.
            if base in _INTERPRETERS:
                payload = _inline_code_payload(first)
                if payload:
                    sub = self.score_command(payload)
                    if sub.score > score:
                        score = sub.score
                    reasons.extend(r for r in sub.reasons if r not in reasons)
            segments = re.split(r"[;&|]+|\$\(", command)
            for seg in segments[1:]:
                sub = self.score_command(seg.strip())
                if sub.score > score:
                    score = sub.score
                    reasons.extend(r for r in sub.reasons if r not in reasons)
        return CommandRisk(score=score, reasons=tuple(reasons), catastrophic=score >= 9)

    def _positional_verbs(self, command: str, depth: int = 0) -> set[str]:
        """Executables appearing at command position across the normalized
        command's segments, recursing through shell/eval/env/xargs wrappers
        (bounded). Assignments are skipped; ``mkfs.ext4`` also yields the
        ``mkfs`` root; ``:(){`` yields ``:`` for the fork-bomb rule."""
        verbs: set[str] = set()
        if depth > 3:
            return verbs
        for seg in re.split(r"[;&|]+|\$\(", command):
            tokens = seg.split()
            i = 0
            while i < len(tokens) and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
                i += 1
            if i >= len(tokens):
                continue
            base = _basename(tokens[i])
            if base.startswith(":("):
                verbs.add(":")
            verbs.add(base)
            if "." in base:
                verbs.add(base.split(".", 1)[0])
            rest = tokens[i + 1:]
            if base in _PRIV_WRAPPERS:
                payload = _skip_wrapper_flags(
                    " ".join(rest), req_short=_PRIV_REQ_SHORT,
                    req_long=_PRIV_REQ_LONG,
                )
                verbs |= self._positional_verbs(payload, depth + 1)
            elif base in _SHELL_WRAPPERS and "-c" in rest[:2]:
                m = re.search(r"(?:^|\s)-c\s+", seg)
                if m:
                    verbs |= self._positional_verbs(seg[m.end():], depth + 1)
            elif base == "eval":
                verbs |= self._positional_verbs(" ".join(rest), depth + 1)
            elif base == "env":
                payload = _skip_wrapper_flags(
                    " ".join(rest), req_short=_ENV_REQ_SHORT,
                    req_long=_ENV_REQ_LONG, assignments=True,
                )
                verbs |= self._positional_verbs(payload, depth + 1)
            elif base == "xargs":
                payload = _skip_wrapper_flags(
                    " ".join(rest), req_short=_XARGS_REQ_SHORT,
                    opt_short=_XARGS_OPT_SHORT, req_long=_XARGS_REQ_LONG,
                )
                verbs |= self._positional_verbs(payload, depth + 1)
        return verbs

    def _base_score(self, base: str, lowered: str, tokens: list[str]) -> int:
        if not base:
            return 0
        if base in {"rm", "mv", "cp", "touch", "mkdir", "tee"}:
            return 3
        subs = _WRITEISH_SUBCOMMANDS.get(base)
        if subs is not None:
            rest = set(tokens[1:2])
            if rest & subs or ("pip" in base or base == "uv"):
                return 4
            return 1
        if base in {"cat", "ls", "pwd", "echo", "head", "tail", "wc", "find", "grep", "rg",
                    "tree", "du", "df", "free", "nproc", "uname", "date", "whoami", "id",
                    "which", "file", "stat", "env", "printenv", "jq", "yq", "diff", "sort",
                    "uniq", "cut", "tr", "seq", "clear", "tput"}:
            return 0
        if base in {"python", "python3", "node", "perl", "ruby", "bash", "sh", "zsh", "eval", "xargs"}:
            return 4  # arbitrary code runner
        if base in {"curl", "wget", "nc", "ssh", "scp", "rsync", "ftp"}:
            return 6
        return 2

    def score_path(self, path: str, *, write: bool) -> int:
        low = path.lower()
        sensitive = any(
            re.search(rx, low)
            for rx in (r"/\.ssh(/|$)", r"\\\.ssh(\\|$)", r"\.gnupg", r"\.aws[/\\]", r"keyring",
                       r"\.netrc", r"id_rsa", r"\.pem$", r"\.key$", r"_history$", r"\.mozilla",
                       r"google-chrome", r"chromium", r"firefox", r"microsoft\\credentials",
                       r"keychains", r"\.env$", r"\.env\.",
                       r"\.npmrc($|[^-\w])", r"\.pypirc($|[^-\w])",
                       r"\.docker[/\\]config\.json", r"\.git[/\\]config($|[^-\w])")
        )
        system = any(
            re.search(rx, low)
            for rx in (r"^/etc", r"^/usr", r"^/bin", r"^/sbin", r"^/lib", r"^/boot", r"^/sys",
                       r"^/proc", r"^/var/lib", r"windows\\system32", r"^c:\\windows")
        )
        if sensitive:
            return 8 if write else 7
        if system:
            return 9 if write else 5
        return 4 if write else 2


def level_for(score: int) -> str:
    if score >= 9:
        return "critical"
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def _basename(tok: str) -> str:
    tok = tok.strip("\"'`")
    tok = tok.replace("\\", "/")
    return tok.rsplit("/", 1)[-1].lower()


_INTERPRETERS = frozenset({"python", "python3", "node", "perl", "ruby"})


def _inline_code_payload(tokens: list[str]) -> str:
    """Code following an interpreter's ``-c``/``-e`` flag (separated or
    attached). Returns the raw remainder; quotes were already stripped by
    normalization, so this is the payload the interpreter would execute."""
    for i, tok in enumerate(tokens[1:], start=1):
        if tok in ("-c", "-e") and i + 1 < len(tokens):
            return " ".join(tokens[i + 1:])
        if tok.startswith(("-c", "-e")) and len(tok) > 2 and not tok.startswith("--"):
            return tok[2:] + (" " + " ".join(tokens[i + 1:]) if i + 1 < len(tokens) else "")
    return ""
