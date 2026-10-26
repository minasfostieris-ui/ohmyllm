"""Load/save of `~/.ohmyllm/config.toml`."""

from __future__ import annotations

import contextlib
from pathlib import Path

from pydantic import ValidationError

from .. import paths
from . import tomlio
from .models import Config


class ConfigStore:
    """Reads and writes the TOML config; never crashes on a corrupt file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.config_path()
        self.last_problems: list[str] = []

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Config:
        """Return config (defaults when missing/invalid); problems recorded in
        ``last_problems`` for the doctor screen to surface."""
        self.last_problems = []
        if not self.path.is_file():
            return Config()
        try:
            raw = tomlio.load_file(self.path)
        except Exception as exc:  # noqa: BLE001 - surfaced as a problem, not a crash
            self.last_problems.append(f"config.toml is not valid TOML: {exc}")
            return Config()
        try:
            return Config.model_validate(raw)
        except ValidationError as exc:
            self.last_problems.append(
                f"config.toml has invalid values ({exc.error_count()}); "
                "using defaults for the affected fields"
            )
            clean = Config()
            for key, value in raw.items():
                if key in Config.model_fields:
                    try:
                        field = Config.model_fields[key]
                        ann = field.annotation
                        validated = ann.model_validate(value) if ann is not None and hasattr(ann, "model_validate") else value
                        setattr(clean, key, validated)
                    except Exception as inner:  # noqa: BLE001
                        self.last_problems.append(f"ignored bad section {key!r}: {inner}")
            return clean

    def load_or_create(self) -> Config:
        existed = self.path.is_file()
        cfg = self.load()
        if not existed:
            self.save(cfg)
        return cfg

    def save(self, config: Config) -> Path:
        data = config.model_dump(mode="json")
        tomlio.dump_file(self.path, data)
        # the file can contain provider API keys ([cloud] keys) — owner-only
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
        return self.path
