"""Permission mode constants and command allowlists."""

from __future__ import annotations

from ..config.models import FULL_ACCESS_PHRASE, PermissionMode

#: Commands whose first token is accepted in sandboxed mode (further checks
#: still scan the full string for destructive payloads / redirects).
SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "ls", "pwd", "echo", "cat", "head", "tail", "wc", "sort", "uniq", "cut",
        "find", "grep", "rg", "tree", "du", "df", "free", "nproc", "uname",
        "date", "whoami", "id", "which", "whereis", "file", "stat", "env",
        "printenv", "jq", "yq", "python", "python3", "pytest", "ruff", "mypy",
        "git", "cargo", "go", "node", "npm", "npx", "make", "cmake", "gcc",
        "g++", "pip", "pip3", "uv", "diff", "comm", "tr", "base64", "md5sum",
        "sha256sum", "sed", "awk", "xargs", "yes", "seq", "tput", "clear",
    }
)

#: Restricted subcommand sets for tools that can otherwise do damage.
SUBCOMMAND_ALLOWLIST: dict[str, frozenset[str]] = {
    "git": frozenset(
        {"status", "log", "diff", "show", "branch", "remote", "blame", "rev-parse",
         "ls-files", "config", "describe", "tag", "stash", "checkout", "switch",
         "add", "commit"}
    ),
    "uv": frozenset({"pip", "add", "run", "list", "tree", "sync", "lock"}),
    "npm": frozenset({"list", "ls", "view", "outdated", "install", "i", "run", "test"}),
    "pip": frozenset({"install", "list", "show", "freeze", "uninstall"}),
}

MODE_ORDER: tuple[PermissionMode, ...] = (
    PermissionMode.STRICT,
    PermissionMode.SAFE,
    PermissionMode.SANDBOXED,
    PermissionMode.DANGEROUS,
    PermissionMode.FULL_ACCESS,
)

__all__ = [
    "FULL_ACCESS_PHRASE",
    "MODE_ORDER",
    "PermissionMode",
    "SAFE_COMMANDS",
    "SUBCOMMAND_ALLOWLIST",
]
