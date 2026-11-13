"""Load/save of `~/.ohmyllm/config.toml`."""

from __future__ import annotations

import contextlib
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .. import paths
from ..secureio import has_symlink_component
from . import tomlio
from .models import Config, PermissionMode

_SECURITY_SECTIONS = {
    "permission_mode", "policy_generation", "workspace", "safety", "memory", "guard", "logging",
}


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge a validated config patch without dropping sibling controls."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _fail_closed_config() -> Config:
    cfg = Config(permission_mode=PermissionMode.STRICT)
    cfg.memory.agent_writes = False
    cfg.safety.http_fetch_enabled = False
    cfg.safety.allow_remote_backends = False
    cfg.logging.audit_enabled = True
    return cfg


class ConfigStore:
    """Reads and writes the TOML config; never crashes on a corrupt file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.config_path()
        self.last_problems: list[str] = []

    def exists(self) -> bool:
        try:
            st = self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)

    def load(self) -> Config:
        """Return config (defaults when missing/invalid); problems recorded in
        ``last_problems`` for the doctor screen to surface."""
        self.last_problems = []
        try:
            st = self.path.lstat()
        except FileNotFoundError:
            return Config()
        except OSError as exc:
            self.last_problems.append(f"config.toml cannot be inspected; using fail-closed defaults: {exc}")
            return _fail_closed_config()
        if (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)
                or has_symlink_component(self.path)):
            self.last_problems.append(
                "config.toml path is symlinked or nonregular; using fail-closed security defaults"
            )
            return _fail_closed_config()
        try:
            raw = tomlio.load_file(self.path)
        except Exception as exc:  # noqa: BLE001 - surfaced as a problem, not a crash
            self.last_problems.append(f"config.toml is not valid TOML: {exc}")
            return _fail_closed_config()
        try:
            return Config.model_validate(raw)
        except ValidationError as exc:
            bad_security = any(
                error.get("loc") and str(error["loc"][0]) in _SECURITY_SECTIONS
                for error in exc.errors()
            )
            self.last_problems.append(
                f"config.toml has invalid values ({exc.error_count()}); "
                + ("security settings failed closed" if bad_security else "using defaults for affected fields")
            )
            clean = _fail_closed_config() if bad_security else Config()
            for key, value in raw.items():
                if key not in Config.model_fields or (bad_security and key in _SECURITY_SECTIONS):
                    continue
                try:
                    field = Config.model_fields[key]
                    ann = field.annotation
                    validated = ann.model_validate(value) if ann is not None and hasattr(ann, "model_validate") else value
                    setattr(clean, key, validated)
                except Exception as inner:  # noqa: BLE001
                    self.last_problems.append(f"ignored bad section {key!r}: {inner}")
            return clean

    def load_or_create(self) -> Config:
        existed = self.exists()
        cfg = self.load()
        if not existed:
            self.save(cfg)
        return cfg

    def save(self, config: Config) -> Path:
        if has_symlink_component(self.path):
            raise ValueError("refusing to write config through a symlinked path")
        data = config.model_dump(mode="json")
        tomlio.dump_file(self.path, data)
        # the file can contain provider API keys ([cloud] keys) — owner-only
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
        return self.path
