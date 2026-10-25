"""Filesystem locations for OhMyLLM: global home, project dirs, history, soul, backups.

Layout
------
Global:    ~/.ohmyllm/{config.toml, soul.md, logs/, backups/{soul,files}/, history/sessions/}
Project:   <root>/.ohmyllm/{soul.md, history/sessions/}   (preferred when a project is active)

Environment overrides (used by tests and power users):
    OMLLM_HOME      replaces ~/.ohmyllm
    OMLLM_WORKSPACE forces the workspace/project root
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "OMLLM_HOME"
ENV_WORKSPACE = "OMLLM_WORKSPACE"

#: Markers that identify a project root when walking up from a directory.
PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "pom.xml",
    ".ohmyllm",
)

CONFIG_NAME = "config.toml"
SOUL_NAME = "soul.md"


def env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve(strict=False) if raw else None


def global_home() -> Path:
    """Root of global state (config, logs, global history, backups)."""
    return env_path(ENV_HOME) or Path.home() / ".ohmyllm"


def skills_dir() -> Path:
    """Imported skill guides: <home>/skills/<name>/SKILL.md."""
    return env_path("OMLLM_SKILLS") or global_home() / "skills"


def detect_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default cwd) looking for a project marker."""
    cur = Path(start or Path.cwd()).expanduser()
    try:
        cur = cur.resolve(strict=False)
    except OSError:
        return None
    seen = set()
    while str(cur) not in seen:
        seen.add(str(cur))
        for marker in PROJECT_MARKERS:
            try:
                if (cur / marker).exists():
                    return cur
            except OSError:
                pass
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def workspace_root(start: Path | None = None) -> Path:
    """The directory the agent is allowed to touch in sandboxed mode."""
    forced = env_path(ENV_WORKSPACE)
    if forced:
        return forced
    root = detect_project_root(start)
    return root or Path(start or Path.cwd()).expanduser()


def project_dir(start: Path | None = None) -> Path | None:
    """`<root>/.ohmyllm` when a project is active, else None."""
    forced = env_path(ENV_WORKSPACE)
    if forced:
        return forced / ".ohmyllm"
    root = detect_project_root(start)
    return (root / ".ohmyllm") if root else None


def ensure_project_dir(start: Path | None = None) -> Path:
    pd = project_dir(start) or (workspace_root(start) / ".ohmyllm")
    pd.mkdir(parents=True, exist_ok=True)
    return pd


def config_path() -> Path:
    return global_home() / CONFIG_NAME


def logs_dir() -> Path:
    return global_home() / "logs"


def audit_log_path() -> Path:
    return logs_dir() / "audit.jsonl"


def app_log_path() -> Path:
    return logs_dir() / "ohmyllm.log"


def backups_dir() -> Path:
    return global_home() / "backups"


def soul_backups_dir() -> Path:
    return backups_dir() / "soul"


def file_backups_dir() -> Path:
    return backups_dir() / "files"


def global_history_dir() -> Path:
    return global_home() / "history"


def history_dir(prefer_project: bool = True) -> Path:
    """Sessions dir: project-local when a project is active, else global."""
    if prefer_project:
        pd = project_dir()
        if pd is not None:
            return pd / "history"
    return global_history_dir()


def sessions_dir() -> Path:
    return history_dir() / "sessions"


def trash_dir() -> Path:
    return history_dir() / "trash"


def soul_path() -> Path:
    """Active soul.md location: project-local wins if it exists, else global."""
    pd = project_dir()
    if pd is not None and (pd / SOUL_NAME).exists():
        return pd / SOUL_NAME
    return global_home() / SOUL_NAME


def ensure_global_dirs() -> None:
    """Create the global directory tree (idempotent)."""
    for d in (
        global_home(),
        logs_dir(),
        backups_dir(),
        soul_backups_dir(),
        file_backups_dir(),
        global_history_dir(),
        global_history_dir() / "sessions",
        global_history_dir() / "trash",
    ):
        d.mkdir(parents=True, exist_ok=True)


def ensure_project_dirs() -> None:
    """Create the project-local directory tree when a project is active."""
    pd = project_dir()
    if pd is None:
        return
    for d in (pd, pd / "history", pd / "history" / "sessions"):
        d.mkdir(parents=True, exist_ok=True)
