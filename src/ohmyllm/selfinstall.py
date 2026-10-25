"""Global command aliases: ohmyllm / omllm / oml / OMLLM / OML on any OS.

Strategy
--------
posix: find the installed console script (pipx/venv ~/.local/bin etc). Symlink the
other spellings next to it; if no script exists, emit `#!/bin/sh` shims that run
`python -m ohmyllm`. Windows: copy the exe aliases and emit .bat/.ps1 shims.
PATH fixes are printed as exact copy-pasteable lines (never silently edited —
unless --write-profile is passed).
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MARKER = "# >>> ohmyllm PATH >>>"
END_MARKER = "# <<< ohmyllm PATH <<<"
NAMES = ("ohmyllm", "omllm", "oml", "OMLLM", "OML")


@dataclass
class AliasResult:
    name: str
    ok: bool
    location: str
    note: str = ""


def _is_windows() -> bool:
    return platform.system() == "Windows"


def find_existing_entry(name: str = "ohmyllm") -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    return None


def user_bin_dir() -> Path:
    if _is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Programs" / "ohmyllm"
    return Path.home() / ".local" / "bin"


def _make_exec(p: Path) -> None:
    if _is_windows():
        return
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ensure_aliases(interpreter: Path | None = None) -> list[AliasResult]:
    results: list[AliasResult] = []
    src = find_existing_entry("ohmyllm")
    if src is None:
        # create shims in user bin dir
        target_dir = user_bin_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        py = str(interpreter or Path(sys.executable))
        for name in NAMES:
            dest = target_dir / (name + (".bat" if _is_windows() else ""))
            if _is_windows():
                dest.write_text(f'@echo off\r\n"{py}" -m ohmyllm %*\r\n', encoding="ascii")
                ps = target_dir / (name + ".ps1")
                ps.write_text(f'& "{py}" -m ohmyllm @args\n', encoding="ascii")
            else:
                dest.write_text(f'#!/bin/sh\nexec "{py}" -m ohmyllm "$@"\n', encoding="utf-8")
                _make_exec(dest)
            results.append(AliasResult(name, True, str(dest), "created shim"))
        return results

    directory = src.parent
    for name in NAMES:
        target = directory / name
        if _is_windows():
            target = target.with_suffix(src.suffix or ".exe")
        if target.exists() or (name.lower() == src.name.lower() and target.name.lower() == name.lower()):
            results.append(AliasResult(name, True, str(target if target.exists() else src), "already present"))
            continue
        try:
            if src.name.lower() == name.lower():
                results.append(AliasResult(name, True, str(src), "already present (case-insensitive FS)"))
                continue
            if _is_windows():
                if src.suffix.lower() == ".exe":
                    shutil.copy2(src, target)
                    results.append(AliasResult(name, True, str(target), "copied executable"))
                    continue
                shutil.copy2(src, target)
                _make_exec(target)
                results.append(AliasResult(name, True, str(target), "copied script"))
            else:
                target.symlink_to(src)
                results.append(AliasResult(name, True, str(target), "symlinked"))
        except (OSError, shutil.SameFileError) as exc:
            # fallback: write a shim
            try:
                shim = directory / (name + (".bat" if _is_windows() else ""))
                if _is_windows():
                    shim.write_text(f'@echo off\r\ncall "{src}" %*\r\n', encoding="ascii")
                else:
                    shim.write_text(f'#!/bin/sh\nexec "{src}" "$@"\n', encoding="utf-8")
                    _make_exec(shim)
                results.append(AliasResult(name, True, str(shim), f"symlink failed ({exc}); shim created"))
            except OSError as exc2:
                results.append(AliasResult(name, False, str(directory), f"could not create alias: {exc2}"))
    return results


def path_dirs_missing_from_env() -> list[Path]:
    dirs = []
    src = find_existing_entry("ohmyllm")
    candidates = [d for d in [src.parent if src else None, user_bin_dir()] if d]
    seen: set[str] = set()
    for d in candidates:
        sp = str(d)
        if sp in seen:
            continue
        seen.add(sp)
        if sp not in os.environ.get("PATH", "").split(os.pathsep):
            dirs.append(d)
    return dirs


def path_instructions(extra_dirs: list[Path] | None = None) -> dict[str, list[str]]:
    dirs = [str(d) for d in (extra_dirs or path_dirs_missing_from_env())]
    out: dict[str, list[str]] = {}
    if not dirs:
        return out
    bash = [f'export PATH="{d}":"$PATH"' for d in dirs]
    fish = [f"fish_add_path {d}" for d in dirs]
    win_machine = [f'setx PATH "%PATH%;{d}"' for d in dirs]
    ps = [f'Set-Item -Path User:Env:PATH -Value "{d};$env:PATH"' for d in dirs]
    out["bash/zsh (add to ~/.bashrc or ~/.zshrc)"] = bash + ["source ~/.bashrc"]
    out["fish"] = fish
    out["powershell (current user)"] = ps
    out["windows cmd (persistent)"] = win_machine + ["restart the terminal"]
    return out


def append_to_profile(directories: list[Path] | None = None) -> list[str]:
    """Opt-in (--write-profile): add PATH lines to the active shell's profile."""
    dirs = [str(d) for d in (directories or path_dirs_missing_from_env())]
    if not dirs:
        return []
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    written: list[str] = []
    if shell.endswith("fish") or (home / ".config" / "fish").is_dir():
        conf = home / ".config" / "fish" / "config.fish"
        lines = [f"fish_add_path {d}" for d in dirs]
        _append_once(conf, lines)
        written.append(str(conf))
    elif _is_windows():
        for d in dirs:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"$p=[Environment]::GetEnvironmentVariable('Path','User'); "
                     f"if ($p -notlike '*{d}*') [Environment]::SetEnvironmentVariable('Path', '{d};' + $p, 'User')"],
                    check=False, timeout=20,
                )
                written.append(f"user PATH += {d}")
            except OSError:
                written.append(f"MANUAL: add {d} to PATH")
    else:
        target = home / (".zshrc" if shell.endswith("zsh") else ".bashrc")
        lines = [f'export PATH="{d}":"$PATH"' for d in dirs]
        _append_once(target, lines)
        written.append(str(target))
    return written


def _append_once(path: Path, lines: list[str]) -> None:
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if MARKER in existing:
        body = "\n".join(existing.splitlines())
        if all(ln in body for ln in lines):
            return
    block = "\n" + MARKER + "\n" + "\n".join(lines) + "\n" + END_MARKER + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(block)


def is_command_available() -> dict[str, bool]:
    return {n: bool(shutil.which(n)) for n in NAMES}
