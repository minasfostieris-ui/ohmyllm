"""Command risk scoring: destructive-pattern detection and risk levels."""

from __future__ import annotations

import re
from dataclasses import dataclass

#: (pattern, score, reason, catastrophic) — catastrophic is score>=9 anyway;
#: the flag marks "never prompt-able without override" patterns.
_RULES: list[tuple[str, int, str]] = [
    # filesystem destruction
    (r"\brm\s+(-[a-zA-Z]+\s+)*(-[rRf]\S*\s+)*(-[fRr]\S*\s+)*['\"]?/(?:\s|['\"]|\*|$)", 10, "rm of root filesystem"),
    (r"\brm\s+-[rRf]{1,3}\b", 6, "recursive/forced rm"),
    (r"\brm\s+(-[a-zA-Z]+\s+)*['\"]?(~(?=\s|$|/)|\$HOME\b)", 10, "rm of home directory"),
    (r"\bshred\b|\bwipefs\b", 8, "secure erase"),
    (r"\btruncate\s+-s\s*0\b", 7, "file truncation to zero"),
    # raw devices / disks
    (r"\bmkfs(\.\w+)?\b", 10, "format filesystem (mkfs)"),
    (r"\bdd\b[^|;]*\bof=/dev/", 10, "dd write to raw device"),
    (r">\s*/dev/(sd|nvme|hd|disk|mapper)", 10, "redirect onto raw device"),
    (r"\bfdisk\b|\bparted\b|\bmkswap\b|\blvm\b", 9, "disk partitioning/volumes"),
    (r"\bmount\b|\bumount\b", 6, "filesystem mount change"),
    # power / system
    (r"\b(shutdown|reboot|halt|poweroff)\b", 9, "power state change"),
    (r"\bsystemctl\s+(poweroff|reboot|halt)\b", 9, "power state change via systemctl"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", 9, "fork bomb"),
    # permissions
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(?:\s|\*|$)", 9, "chmod 777 on /"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*[-+rRwxa]*\s*777\s+~", 9, "chmod 777 on home"),
    (r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\b|\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\b", 5, "recursive permission change"),
    (r"\bsudo\b|\bdoas\b", 6, "privilege escalation"),
    # remote code execution
    (r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba)?sh\b", 10, "piped remote script to shell"),
    (r"\b(curl|wget)\b[^|;]*\|\s*python3?\b", 10, "piped remote script to python"),
    (r"\beval\s*\$\(\s*(curl|wget)\b", 8, "eval of remote content"),
    # version control damage
    (r"\bgit\s+push\b[^|;]*(--force|-f)\b", 6, "force push"),
    (r"\bgit\s+reset\s+--hard\b", 5, "hard reset discards work"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", 5, "git clean removes untracked files"),
    (r"\bgit\s+(filter-branch|filter-repo)\b", 7, "history rewrite"),
    # package/system mutation
    (r"\b(apt|apt-get|dnf|yum|pacman|zypper|brew|choco|winget)\s+install\b", 6, "system package install"),
    (r"\b(pip|pip3|uv)\s+install\b", 5, "package install"),
    (r"\bnpm\s+(install|i)\b", 4, "node package install"),
    # process control
    (r"\b(killall|pkill)\b|\bkill\s+-9\b", 5, "process kill"),
    # credential / privacy access
    (r"\.ssh\b|id_rsa|\.gnupg|\.aws\b|keyring|\.netrc", 8, "credential store access"),
    (r"\.(bash|zsh|sh|python)_history\b", 7, "shell history access"),
    (r"\.mozilla|google-chrome|chromium|firefox", 7, "browser profile access"),
    (r"(^|\s)\.env(\.\w+)?(\s|$)", 6, "dotenv file access"),
    # output redirection onto absolute paths
    (r">>?\s*['\"]?/(?!dev/null)(?:[\w./-]+)", 5, "redirect to absolute path"),
    (r">>?\s*['\"]?(~|\$HOME)/", 6, "redirect into home"),
    # windows equivalents
    (r"\bformat\s+[a-z]:", 10, "windows format"),
    (r"\bdiskpart\b", 9, "windows disk partitioning"),
    (r"\bdel\s+/[sq]\b|\brd\s+/s\b", 8, "windows recursive delete"),
    (r"\bRemove-Item\b[^;]*-Re(curse)?\b[^;]*-Force|\bRemove-Item\b[^;]*-Force[^;]*-Re(curse)?", 8, "powershell recursive forced delete"),
    (r"\bSet-ExecutionPolicy\b", 7, "powershell execution policy change"),
    (r"\b(Stop-Computer|Restart-Computer)\b", 9, "windows power state"),
    (r"\breg\s+(delete|add)\b", 6, "registry change"),
    (r"\bschtasks\b", 6, "scheduled task change"),
]

_COMPILED: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(p, re.IGNORECASE), s, r) for p, s, r in _RULES
]

_WRITEISH_SUBCOMMANDS: dict[str, set[str]] = {
    "git": {"add", "commit", "checkout", "switch", "stash", "reset", "merge", "rebase", "push", "clean"},
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
        if command.strip():
            lowered = command.lower()
            first = lowered.split()
            base = _basename(first[0]) if first else ""
            score, reasons = self._base_score(base, lowered, first), []
            matched: list[tuple[int, str]] = []
            for pattern, pat_score, reason in _COMPILED:
                if pattern.search(command):
                    matched.append((pat_score, reason))
            for m_score, m_reason in sorted(matched, reverse=True):
                if m_score > score:
                    score = m_score
                if m_reason not in reasons:
                    reasons.append(m_reason)
            segments = re.split(r"[;&|]+|\$\(", command)
            for seg in segments[1:]:
                sub = self.score_command(seg.strip())
                if sub.score > score:
                    score = sub.score
                    reasons.extend(r for r in sub.reasons if r not in reasons)
        return CommandRisk(score=score, reasons=tuple(reasons), catastrophic=score >= 9)

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
                       r"keychains", r"\.env$", r"\.env\.")
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
