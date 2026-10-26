"""Services: one place that wires config, memory, sessions, safety and runtimes.

TUI, plain mode and CLI all build on this so behavior is identical everywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .agents import AgentRuntime
from .backends import BaseBackend, make_backend
from .config import Config, ConfigStore, PermissionMode
from .memory import SoulManager
from .safety import ApprovalManager, AuditLogger, PolicyEngine
from .sessions import Session, SessionStore
from .tools import ToolRegistry, build_default_registry


@dataclass
class Services:
    config_store: ConfigStore
    config: Config
    registry: ToolRegistry = field(default_factory=build_default_registry)
    _backend_cache: dict[str, BaseBackend] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------- factory
    @classmethod
    def boot(cls, workspace: str | Path | None = None) -> Services:
        paths.ensure_global_dirs()
        paths.ensure_project_dirs()
        store = ConfigStore()
        cfg = store.load()
        if workspace:
            cfg.workspace = str(Path(workspace).expanduser().resolve(strict=False))
        if cfg.workspace:
            Path(cfg.workspace).mkdir(parents=True, exist_ok=True)
        svc = cls(config_store=store, config=cfg)
        svc.soul.ensure()
        return svc

    # ------------------------------------------------------------- members
    @property
    def workspace(self) -> Path:
        if self.config.workspace:
            return Path(self.config.workspace).expanduser().resolve(strict=False)
        return paths.workspace_root()

    @property
    def soul(self) -> SoulManager:
        if not hasattr(self, "_soul"):
            self._soul = SoulManager(
                paths.soul_path(),
                paths.soul_backups_dir(),
                keep_backups=self.config.memory.backups_keep,
            )
        return self._soul

    @property
    def skills(self) -> Any:
        if not hasattr(self, "_skills"):
            from .skills import SkillManager

            self._skills = SkillManager(paths.skills_dir())
        return self._skills

    @property
    def sessions(self) -> SessionStore:
        if not hasattr(self, "_sessions"):
            self._sessions = SessionStore(paths.history_dir())
        return self._sessions

    @property
    def policy(self) -> PolicyEngine:
        if (not hasattr(self, "_policy") or getattr(self, "_policy_cfg", None) is not self.config
                or getattr(self, "_policy_ws", None) != self.workspace):
            self._policy = PolicyEngine(self.config, self.workspace)
            self._policy_cfg = self.config
            self._policy_ws = self.workspace
        return self._policy

    @property
    def approvals(self) -> ApprovalManager:
        if not hasattr(self, "_approvals"):
            self._approvals = ApprovalManager(rules_path=paths.global_home() / "rules.json")
        return self._approvals

    @property
    def audit(self) -> AuditLogger:
        if not hasattr(self, "_audit"):
            self._audit = AuditLogger(
                enabled=self.config.logging.audit_enabled,
                max_bytes=self.config.logging.max_log_bytes,
            )
        return self._audit

    # ------------------------------------------------------------- backend
    def backend_name_str(self) -> str:
        return self.config.default_backend.value

    def backend(self, name: str | None = None) -> BaseBackend:
        key = name or self.config.default_backend.value
        if key not in self._backend_cache:
            self._backend_cache[key] = make_backend(key, self.config)
        return self._backend_cache[key]

    def runtime(self, name: str | None = None) -> AgentRuntime:
        be = self.backend(name)
        return AgentRuntime(
            backend=be,
            registry=self.registry,
            policy=self.policy,
            approvals=self.approvals,
            audit=self.audit,
            config=self.config,
            soul=self.soul,
            skills=self.skills,
        )

    # ------------------------------------------------------------- actions
    def save_config(self) -> None:
        self.config_store.save(self.config)

    def apply_config(self, cfg: Config) -> None:
        """Swap the live config and rewire everything that captured the old one."""
        self.config = cfg
        self._backend_cache.clear()  # backends hold config refs; rebuild lazily
        self.save_config()

    def set_mode(self, mode: PermissionMode, *, phrase: str | None = None) -> bool:
        """Full access requires the exact phrase; every change is audited."""
        if mode is PermissionMode.FULL_ACCESS:
            if (phrase or "").strip() != self.full_access_phrase():
                return False
        self.config.permission_mode = mode
        self.save_config()
        self.audit.log(
            mode=mode.value, tool="__permission_mode__", args={"new_mode": mode.value},
            approval="user", success=True, duration_ms=0, event="permission_mode_changed",
        )
        return True

    @staticmethod
    def full_access_phrase() -> str:
        from .config.models import FULL_ACCESS_PHRASE

        return FULL_ACCESS_PHRASE

    def new_session(self, first_message: str = "") -> Session:
        be = self.config.default_backend.value
        model = self.current_model()
        return self.sessions.create(
            first_message=first_message,
            backend=be,
            model=model,
            permission_mode=self.config.permission_mode.value,
            workspace=str(self.workspace),
        )

    def current_model(self, name: str | None = None) -> str:
        key = name or self.config.default_backend.value
        section = getattr(self.config, key, None)
        dm = getattr(section, "default_model", None) if section else None
        if dm:
            return dm
        return "mock-tool-7b" if key == "mock" else ""

    async def release_model(self) -> None:
        """Eject the current model from the backend so closing the app frees
        VRAM. Best-effort: never raises (quit paths must not hang)."""
        import contextlib

        be_name = self.config.default_backend.value
        model = self.current_model()
        if not model or be_name == "mock":
            return
        with contextlib.suppress(Exception):
            be = self.backend(be_name)
            if be_name == "lmstudio":
                unload = getattr(be, "unload", None)
                if unload is not None:
                    await unload(model)
            elif be_name == "ollama":
                import httpx

                url = self.config.ollama.host.rstrip("/")
                async with httpx.AsyncClient(timeout=10.0) as c:
                    await c.post(url + "/api/generate", json={"model": model, "keep_alive": 0})

    def status_snapshot(self, tok_s: float = 0.0, ctx_used: int = 0) -> dict[str, Any]:
        gen = self.config.settings_for(self.config.default_backend, self.current_model())
        return {
            "backend": self.config.default_backend.value,
            "model": self.current_model() or "—",
            "mode": self.config.permission_mode.value,
            "ultrathink": gen.ultrathink,
            "effort": gen.effort,
            "num_ctx": gen.num_ctx,
            "ctx_used": ctx_used,
            "ctx_pct": round(100 * ctx_used / max(1, gen.num_ctx)),
            "tokens_s": tok_s,
            "updated": time.time(),
        }
