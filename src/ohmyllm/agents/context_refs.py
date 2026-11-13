"""Workspace-bounded @-references for user messages (D1).

Syntax: ``@file:path``, ``@folder:path``, ``@diff``, ``@staged``, ``@git:REF``.
Expansion happens in AgentRuntime.run_turn BEFORE the PromptBuilder sees the
text, so run/plain/TUI/GUI all get identical plain-text behavior.

Security contract (fail-closed):
- every path is resolved through secureio inside the workspace root;
- symlinks are refused outright;
- secret material is blocked with the SAME predicate the policy engine uses
  (``is_sensitive_path``): dotenv variants, keys, credential stores, etc.,
  checked on every folder path component and on ``@git:REV:PATH`` paths;
- per-reference and total byte caps with an explicit ``[truncated]`` marker;
- git is reached ONLY through allowlisted read-only subcommands
  (``diff``, ``diff --staged``, ``show``) via an INJECTABLE runner — tests
  pass a fake, production uses a bounded subprocess;
- a denied reference is reported inline in the expanded text, never
  silently dropped.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..safety.policy import is_sensitive_path
from ..secureio import (
    UnsafePathError,
    is_within,
    resolve_path,
    sanitized_environment,
)

GitRunner = Callable[[list[str]], str]

PER_REF_CAP = 32_768
TOTAL_CAP = 131_072
_FOLDER_FILE_CAP = 8_192
_FOLDER_MAX_FILES = 400
_GIT_OUTPUT_CAP = 1_048_576
_GIT_STDERR_CAP = 65_536
_GIT_TIMEOUT = 20.0
# Repo/global/system git config must never be able to execute code through
# diff.external or textconv filters: command-line -c overrides repo config,
# and the diff-family flags disable external helpers outright.
_GIT_CONFIG_FLAGS = ("-c", "diff.external=", "-c", "core.attributesFile=/dev/null")
_GIT_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")

_REF_RX = re.compile(r"@(file|folder|git):([^\s@]+)|@(diff|staged)(?!\w)")
_GIT_REF_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/^~:-]*$")


class _RefDenied(Exception):
    """A reference was refused; the reason is surfaced to the user/model."""


def _blocklisted_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith("credentials") or lowered.startswith("secrets")


def _blocklisted(path: Path, workspace: Path) -> bool:
    # Parity with PolicyEngine.is_sensitive (shared predicate, no allowlist:
    # fail-closed) plus the historical credentials*/secrets* name block.
    return _blocklisted_name(path.name) or is_sensitive_path(path, workspace=workspace)


def _sensitive_component(root: Path, rel: str, workspace: Path) -> bool:
    """Check EVERY path component, so e.g. .env.d/prod is caught by the
    directory name even when the leaf file name looks innocent."""
    parts = Path(rel).parts
    return any(_blocklisted(root.joinpath(*parts[:i]), workspace) for i in range(1, len(parts) + 1))


def _resolve(raw: str, workspace: Path) -> Path:
    if not raw.strip():
        raise _RefDenied("empty path")
    try:
        resolved = resolve_path(raw, base=workspace, root=workspace, reject_symlinks=True)
    except (UnsafePathError, OSError, ValueError) as exc:
        raise _RefDenied(f"unsafe path ({exc})") from exc
    if not is_within(resolved, workspace):
        raise _RefDenied("outside the workspace")
    return resolved


def _read_file(raw: str, workspace: Path, cap: int) -> str:
    path = _resolve(raw, workspace)
    rel = path.relative_to(workspace).as_posix()
    if _sensitive_component(workspace, rel, workspace):
        raise _RefDenied("blocklisted file — secrets are never injected")
    if not path.is_file():
        raise _RefDenied("not a regular file")
    with path.open("rb") as handle:
        data = handle.read(cap + 1)
    text = data[:cap].decode("utf-8", errors="replace")
    if len(data) > cap:
        text += "\n[truncated]"
    return text


def _read_folder(raw: str, workspace: Path, cap: int) -> str:
    root = _resolve(raw, workspace)
    root_rel = root.relative_to(workspace).as_posix()
    if _sensitive_component(workspace, root_rel, workspace):
        raise _RefDenied("blocklisted location — secrets are never injected")
    if not root.is_dir():
        raise _RefDenied("not a directory")
    chunks: list[str] = []
    used = 0
    for path in sorted(root.rglob("*"))[:_FOLDER_MAX_FILES]:
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _sensitive_component(root, rel, workspace):
            chunks.append(f"[skipped blocklisted file: {rel}]")
            continue
        room = cap - used
        if room <= 0:
            chunks.append("[truncated]")
            break
        with path.open("rb") as handle:
            data = handle.read(min(room, _FOLDER_FILE_CAP) + 1)
        piece = f"--- {rel} ---\n" + data[: min(room, _FOLDER_FILE_CAP)].decode("utf-8", errors="replace")
        if len(data) > min(room, _FOLDER_FILE_CAP):
            piece += "\n[truncated]"
        chunks.append(piece)
        used += len(piece.encode("utf-8", errors="replace"))
    return "\n".join(chunks) if chunks else "(empty folder)"


def _git_env() -> dict[str, str]:
    """Sanitized child env: no inherited credentials, no git config overrides."""
    return sanitized_environment({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})


def _drain_bounded(stream, cap: int) -> tuple[str, bool]:
    """Incrementally read at most *cap* characters; report overflow instead
    of buffering an unbounded child output into memory."""
    chunks: list[str] = []
    total = 0
    while True:
        piece = stream.read(65_536)
        if not piece:
            break
        total += len(piece)
        if total > cap:
            keep = cap - (total - len(piece))
            if keep > 0:
                chunks.append(piece[:keep])
            return "".join(chunks), True
        chunks.append(piece)
    return "".join(chunks), False


def _subprocess_runner(workspace: Path) -> GitRunner:
    """Production git seam. Never used under the offline test guard — tests
    inject a fake runner (or a fake Popen) instead. Hardened three ways:
    sanitized env, config-neutralized argv, and bounded incremental capture
    that kills the child on overflow or timeout."""

    def run(argv: list[str]) -> str:
        proc = subprocess.Popen(  # noqa: S603 - fixed allowlisted argv, no shell
            argv, cwd=str(workspace), env=_git_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        watchdog = threading.Timer(_GIT_TIMEOUT, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            out, over = _drain_bounded(proc.stdout, _GIT_OUTPUT_CAP)
            if over:
                proc.kill()
            err, err_over = _drain_bounded(proc.stderr, _GIT_STDERR_CAP)
            if err_over:
                proc.kill()
            try:
                code = proc.wait(timeout=_GIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise RuntimeError("git timed out") from None
            if over:
                return out + "\n[truncated]"
            if code != 0:
                raise RuntimeError((err or out or "git failed").strip()[:200])
            return out
        finally:
            watchdog.cancel()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()

    return run


def _harden_argv(argv: list[str]) -> list[str]:
    return [argv[0], *_GIT_CONFIG_FLAGS, *argv[1:], *_GIT_DIFF_FLAGS]


def _run_git(argv: list[str], workspace: Path, runner: GitRunner | None) -> str:
    # argv is ALWAYS one of the three allowlisted read-only shapes below,
    # wrapped in config-neutralizing hardening flags; user input can only
    # ever occupy the validated REF slot of `git show`.
    try:
        return (runner or _subprocess_runner(workspace))(_harden_argv(argv))
    except _RefDenied:
        raise
    except Exception as exc:  # noqa: BLE001 - git failure becomes a reported denial
        raise _RefDenied(f"git unavailable ({exc})") from exc


def _git_show(ref: str, workspace: Path, runner: GitRunner | None) -> str:
    if not _GIT_REF_RX.fullmatch(ref) or ".." in ref:
        raise _RefDenied("invalid git ref (plain commit/tag/branch names only)")
    if ":" in ref:
        # REV:PATH — the path portion gets the same sensitive checks as @file.
        path_part = ref.split(":", 1)[1]
        if path_part and _sensitive_component(workspace, path_part, workspace):
            raise _RefDenied("blocklisted git path — secrets are never injected")
    return _run_git(["git", "show", ref], workspace, runner)


def expand_context_refs(
    text: str,
    *,
    workspace: Path,
    git_runner: GitRunner | None = None,
    per_ref_cap: int = PER_REF_CAP,
    total_cap: int = TOTAL_CAP,
) -> str:
    """Expand @-references in *text* into bounded, reported context blocks."""
    if "@" not in text:
        return text
    root = Path(workspace).resolve(strict=False)
    budget = max(0, int(total_cap))
    per_ref = max(1, int(per_ref_cap))

    def replace(match: re.Match[str]) -> str:
        nonlocal budget
        kind = match.group(1) or match.group(3)
        arg = match.group(2) or ""
        label = match.group(0)
        try:
            if kind == "file":
                body = _read_file(arg, root, per_ref)
            elif kind == "folder":
                body = _read_folder(arg, root, per_ref)
            elif kind == "diff":
                body = _run_git(["git", "diff"], root, git_runner)
            elif kind == "staged":
                body = _run_git(["git", "diff", "--staged"], root, git_runner)
            else:  # git:REF
                body = _git_show(arg, root, git_runner)
        except _RefDenied as exc:
            return f"[context {label} denied: {exc}]"
        if budget <= 0:
            return f"[context {label} denied: total context budget exhausted]"
        raw_bytes = body.encode("utf-8", errors="replace")
        allowance = min(per_ref, budget)
        if len(raw_bytes) > allowance:
            body = raw_bytes[:allowance].decode("utf-8", errors="replace") + "\n[truncated]"
            budget -= allowance
        else:
            budget -= len(raw_bytes)
        return f"[context {label}]\n{body}\n[/context]"

    return _REF_RX.sub(replace, text)
