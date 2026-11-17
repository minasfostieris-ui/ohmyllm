"""Command risk scoring: destructive-pattern detection and risk levels."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import (
    _ROOT_BARE_RX,
    _ROOT_DIRS_RX,
    _ROOT_GLOB_RX,
    _ROOT_HOME_RX,
    _SHELL_WRAPPERS,
    _XARGS_OPT_SHORT,
    _XARGS_REQ_LONG,
    _XARGS_REQ_SHORT,
    _rm_root_order_free,
    _skip_wrapper_flags,
    _strip_transparent_prefixes,
    _strip_tricks,
    nesting_exceeds_budget,
    normalize_command,
)

#: (pattern, score, reason, verbs) — catastrophic is score>=9 anyway.
#: *verbs* is the command-position requirement (R2 W5): the rule only fires
#: when one of these executables appears as a segment's executable (after
#: assignment/wrapper stripping), so quoted literals like
#: ``echo "rm -rf /"`` are not scored as destruction. None = anywhere.
_RULES: list[tuple[str, int, str, frozenset[str] | None]] = [
    # filesystem destruction
    (r"\brm\s+(-[a-zA-Z]+\s+)*(-[rRf]\S*\s+)*(-[fRr]\S*\s+)*['\"]?" + _ROOT_BARE_RX + r"(?=[\s'\"*]|$)", 10, "rm of root filesystem", frozenset({"rm"})),
    # R3 S3: top-level system directories are root-equivalent operands
    # (/home itself, NOT /home/user — the boundary lookahead keeps subpaths).
    # R6 E1: dot-suffix spellings (/home/., /etc/./) share the same grammar.
    (r"\brm\s+(-[a-zA-Z]+\s+)*(-[rRf]\S*\s+)*(-[fRr]\S*\s+)*['\"]?" + _ROOT_DIRS_RX + r"(?=[\s'\"*]|$)", 10, "rm of top-level system directory", frozenset({"rm"})),
    # R7 S2: a first-component absolute glob (/hom* /?ome /[h]ome /us*) can
    # expand to top-level system dirs — same catastrophe, glob spelling.
    (r"\brm\s+(-[a-zA-Z]+\s+)*(-[rRf]\S*\s+)*(-[fRr]\S*\s+)*['\"]?" + _ROOT_GLOB_RX, 10, "rm of root-equivalent glob", frozenset({"rm"})),
    (r"\brm\s+-[rRf]{1,3}\b", 6, "recursive/forced rm", frozenset({"rm"})),
    (r"\brm\s+(-[a-zA-Z]+\s+)*['\"]?" + _ROOT_HOME_RX + r"(?=[\s'\"*]|$)", 10, "rm of home directory", frozenset({"rm"})),
    (r"\bshred\b|\bwipefs\b", 9, "secure erase", frozenset({"shred", "wipefs"})),
    (r"\btruncate\s+-s\s*0\b", 7, "file truncation to zero", frozenset({"truncate"})),
    # raw devices / disks
    (r"\bmkfs(\.\w+)?\b", 10, "format filesystem (mkfs)", frozenset({"mkfs"})),
    (r"\bdd\b[^|;]*\bof=/dev/", 10, "dd write to raw device", frozenset({"dd"})),
    # R3 S6: find -delete is catastrophic for absolute/system roots; a
    # relative (workspace-scoped) target stays high-risk but approvable.
    (r"\bfind\s+(?:-{1,2}\S+\s+)*['\"]?(?:/|~|\$HOME\b)[^|;]*\s-delete(\s|$)", 10, "find -delete mass removal", frozenset({"find"})),
    (r"\bfind\b[^|;]*\s-delete(\s|$)", 6, "find -delete removal (scoped)", frozenset({"find"})),
    (r"\bfind\b[^|;]*\s-exec(dir)?\s+rm\b", 10, "find -exec rm mass removal", frozenset({"find"})),
    (r">\s*/dev/(sd|nvme|hd|disk|mapper)", 10, "redirect onto raw device", None),
    (r"\bfdisk\b|\bparted\b|\bmkswap\b|\blvm\b", 9, "disk partitioning/volumes", frozenset({"fdisk", "parted", "mkswap", "lvm"})),
    (r"\bmount\b|\bumount\b", 6, "filesystem mount change", frozenset({"mount", "umount"})),
    # power / system
    (r"\b(shutdown|reboot|halt|poweroff)\b", 9, "power state change", frozenset({"shutdown", "reboot", "halt", "poweroff"})),
    (r"\bsystemctl\s+(poweroff|reboot|halt)\b", 9, "power state change via systemctl", frozenset({"systemctl"})),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", 9, "fork bomb", frozenset({":"})),
    # permissions
    # permissions — R3 S3: mode/flag order and 0777 spelling must not dodge
    # the rule ("chmod 777 -R /", "chmod -R 0777 /").
    (r"\bchmod\s+(?:-[a-zA-Z]+\s+)*0?777\s+/(?:\s|\*|$)|\bchmod\s+0?777\s+(?:-[a-zA-Z]+\s+)*/(?:\s|\*|$)", 9, "chmod 777 on /", frozenset({"chmod"})),
    (r"\bchmod\s+(?:-[a-zA-Z]+\s+)*0?777\s+~|\bchmod\s+0?777\s+(?:-[a-zA-Z]+\s+)*~", 9, "chmod 777 on home", frozenset({"chmod"})),
    (r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\b|\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\b", 5, "recursive permission change", frozenset({"chmod", "chown"})),
    (r"\bsudo\b|\bdoas\b", 6, "privilege escalation", frozenset({"sudo", "doas"})),
    # remote code execution
    (r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", 10, "piped remote script to shell", frozenset({"curl", "wget"})),
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
    # R5 A26: --in-place[=SUFFIX] is the long spelling of -i.
    # R6 E5: clustered -i (-ni/-Ei) is in-place too; the prefix class excludes
    # e/f (script-taking) so -e's/i/x/' is NOT scored as an in-place edit.
    (r"\bsed\b[^|;]*(\s-[a-dg-zA-Z]*i[a-zA-Z]*\b|--in-place(=[^\s]*)?)", 5, "sed in-place file edit", frozenset({"sed"})),
    # R3 S4: __import__ indirection, node fs.rmSync/unlink and perl unlink
    # glob are the same destructive payloads in a different spelling.
    (r"shutil\.rmtree|os\.system\s*\(|subprocess\.(call|run|Popen)\s*\(|os\.exec\w*\s*\(|child_process"
     r"|__import__\s*\(.*\)\s*\.\s*(rmtree|system|popen|remove|unlink|exec\w*|spawn\w*)"
     r"|\brmSync\b|\bfs\s*\.\s*(rm|unlink|rmdir)(Sync)?\b|promises\s*\.\s*(unlink|rm|rmdir)\b"
     r"|\bunlink\s+glob\b", 9, "destructive interpreter payload", frozenset({"python", "python3", "node", "perl", "ruby", "sh", "bash", "zsh", "dash", "ksh", "eval"})),
    # R4-S4: perl system/exec WITHOUT parens (and backticks) are the same
    # destructive payloads as the parenthesized form — the subshell extractor
    # only sees parens/$()/backtick interiors, so score the bare spellings.
    (r"\b(system|exec)\s*\(?\s*['\"]?\s*(rm|shred|wipefs|mkfs(\.\w+)?|dd|chmod|chown)\b"
     r"|`[^`]*\b(rm|shred|wipefs|mkfs|dd)\b", 9, "destructive interpreter payload", frozenset({"perl"})),
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
        raw_command = command
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
            # R5 A44 (REFUTED, must stay inert): a control char spliced INTO
            # the executable word is one nonexistent command name — bash
            # never word-splits quoted expansions, so destructive-verb rules
            # must not fire on it (wrapper payloads are still walked below).
            # Detect on the _strip_tricks form: normalize_command folds LF
            # into spaces and would erase the splice evidence.
            first_word = re.split(r"[ \t]+", _strip_tricks(raw_command).strip(), maxsplit=1)[0]
            exe_spliced = any(ord(ch) < 0x20 for ch in first_word)
            matched: list[tuple[int, str]] = []
            for pattern, pat_score, reason, need in _COMPILED:
                if need is not None and (exe_spliced or not (need & verbs)):
                    continue
                if pattern.search(command):
                    matched.append((pat_score, reason))
            for m_score, m_reason in sorted(matched, reverse=True):
                if m_score > score:
                    score = m_score
                if m_reason not in reasons:
                    reasons.append(m_reason)
            # R5 A01-A03: rf flags + root operand in ANY order is the same
            # catastrophe as `rm -rf /` (structural, order-free). Transparent
            # prefixes (busybox/nice/…) must not hide the rm verb.
            first_seg = _strip_transparent_prefixes(re.split(r"[;&|\n]+", command)[0])
            if not exe_spliced and _rm_root_order_free(first_seg.split()):
                score = max(score, 10)
                if "rm of root filesystem" not in reasons:
                    reasons.append("rm of root filesystem")
            # R5 A18-A19: nesting beyond the analysis budget fails closed.
            if nesting_exceeds_budget(command):
                score = max(score, 10)
                if "wrapper nesting beyond analysis budget (fail-closed)" not in reasons:
                    reasons.append("wrapper nesting beyond analysis budget (fail-closed)")
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
            # R5 A20-A21: process-substitution interiors are real command
            # boundaries — score them, and when a shell consumes the procsub
            # output as code (bash <(echo 'rm -rf /')), score that literal
            # output as the executed command too.
            # R10 F1: the exec-consumer lane covers source/dot/ash too, and
            # the interior output is read through the ONE pipe-aware static
            # reader (herestring-fed cat/head, printf %b, multi-hop chains) —
            # not just a bare echo/printf. ONE procsub grammar (normalize).
            from .normalize import _raw_segments, _static_output, procsub_interiors
            for inner in procsub_interiors(command):
                sub = self.score_command(inner)
                if sub.score > score:
                    score = sub.score
                reasons.extend(r for r in sub.reasons if r not in reasons)
                if base in _SHELL_WRAPPERS or base in ("eval", "source", ".", "ash"):
                    isegs = _raw_segments(inner)
                    lit = _static_output(isegs, len(isegs) - 1) if isegs else None
                    if lit:
                        sub2 = self.score_command(lit)
                        if sub2.score > score:
                            score = sub2.score
                        reasons.extend(r for r in sub2.reasons if r not in reasons)
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
            # R5 A04-A13: sudo/doas/env/busybox/toybox/exec/command/nice/
            # nohup/time/timeout/stdbuf/ionice are all transparent — the
            # payload verb must reach the destructive rules.
            stripped = _strip_transparent_prefixes(" ".join(tokens[i:]))
            if stripped != " ".join(tokens[i:]):
                verbs |= self._positional_verbs(stripped, depth + 1)
            if base in _SHELL_WRAPPERS and "-c" in rest[:2]:
                m = re.search(r"(?:^|\s)-c\s+", seg)
                if m:
                    verbs |= self._positional_verbs(seg[m.end():], depth + 1)
            elif base == "eval":
                verbs |= self._positional_verbs(" ".join(rest), depth + 1)
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
