"""Services: one place that wires config, memory, sessions, safety and runtimes.

TUI, plain mode and CLI all build on this so behavior is identical everywhere.
"""

from __future__ import annotations

import contextlib
import copy
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .agents import AgentRuntime
from .backends import BaseBackend, make_backend
from .config import BackendName, Config, ConfigStore, PermissionMode
from .memory import SoulManager
from .safety import ApprovalManager, AuditLogger, PolicyEngine
from .secureio import url_is_loopback
from .sessions import Session, SessionStore
from .tools import ToolRegistry, build_default_registry


@dataclass
class Services:
    config_store: ConfigStore
    config: Config
    registry: ToolRegistry = field(default_factory=build_default_registry)
    _backend_cache: dict[str, BaseBackend] = field(default_factory=dict, repr=False)
    _runtimes: weakref.WeakSet[AgentRuntime] = field(default_factory=weakref.WeakSet, repr=False)

    # ------------------------------------------------------------- factory
    @classmethod
    def boot(cls, workspace: str | Path | None = None) -> Services:
        paths.ensure_global_dirs()
        paths.ensure_project_dirs()
        store = ConfigStore()
        cfg = store.load()
        # Persisted full access is never active without confirmation in this process.
        stale_full_access = cfg.permission_mode is PermissionMode.FULL_ACCESS
        if stale_full_access:
            cfg.permission_mode = PermissionMode.DANGEROUS
            cfg.policy_generation += 1
            store.last_problems.append("persisted full_access requires fresh process-local confirmation")
        if workspace:
            cfg.workspace = str(Path(workspace).expanduser().resolve(strict=False))
        if cfg.workspace:
            Path(cfg.workspace).mkdir(parents=True, exist_ok=True)
        if cfg.default_backend is BackendName.CLOUD:
            from .providers import any_key

            if not cfg.cloud.provider or not any_key(cfg.cloud):
                # a cloud default with no connected provider bricks the models
                # screen ("probing backend…" forever) — fall back at boot
                from .providers import best_local_backend

                cfg.default_backend = best_local_backend(cfg)
                store.save(cfg)
        svc = cls(config_store=store, config=cfg)
        if stale_full_access:
            svc.approvals.invalidate_all()
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
                or getattr(self, "_policy_ws", None) != self.workspace
                or getattr(self, "_policy_mode", None) is not self.config.permission_mode):
            self._policy = PolicyEngine(self.config, self.workspace)
            self._policy_cfg = self.config
            self._policy_ws = self.workspace
            self._policy_mode = self.config.permission_mode
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
        section = getattr(self.config, key, None)
        endpoint = getattr(section, "host", None) or getattr(section, "base_url", None)
        if endpoint and not self.config.safety.allow_remote_backends and not url_is_loopback(str(endpoint)):
            raise ValueError(f"remote backend endpoint requires safety.allow_remote_backends: {endpoint}")
        if key not in self._backend_cache:
            self._backend_cache[key] = make_backend(key, self.config)
        return self._backend_cache[key]

    def may_autostart(self, name: str | None = None) -> bool:
        """Whether the user allowed oml to spawn this local server process.
        Off by default: booting the app must never launch LM Studio/Ollama
        behind the user's back — explicit actions still call ensure_running
        directly."""
        key = name or self.config.default_backend.value
        section = getattr(self.config, key, None)
        return bool(getattr(section, "auto_start", False))

    def runtime(self, name: str | None = None) -> AgentRuntime:
        be = self.backend(name)
        runtime = AgentRuntime(
            backend=be,
            registry=self.registry,
            policy=self.policy,
            approvals=self.approvals,
            audit=self.audit,
            config=self.config,
            soul=self.soul,
            skills=self.skills,
            config_saver=self.save_config,
        )
        self._runtimes.add(runtime)
        return runtime

    def _retire_runtimes(self) -> None:
        for runtime in tuple(self._runtimes):
            runtime.retire()
        self._runtimes.clear()

    # ------------------------------------------------------------- actions
    def save_config(self) -> None:
        self.config_store.save(self.config)

    @staticmethod
    def _security_snapshot(cfg: Config) -> dict[str, Any]:
        return {
            "permission_mode": cfg.permission_mode.value,
            "workspace": cfg.workspace,
            "safety": cfg.safety.model_dump(mode="json"),
            "memory": cfg.memory.model_dump(mode="json"),
            "guard": cfg.guard.model_dump(mode="json"),
            "logging": cfg.logging.model_dump(mode="json"),
            "operation_mode": cfg.agent.op_mode,
            "yolo": cfg.agent.yolo,
            "tone": cfg.agent.tone,
            "custom_tones": cfg.agent.custom_tones,
            "mail": cfg.mail.model_dump(mode="json"),
            "cloud": cfg.cloud.model_dump(mode="json"),
            "backend_boundary": {
                "ollama_host": cfg.ollama.host,
                "ollama_binary": cfg.ollama.binary,
                "ollama_auto_start": cfg.ollama.auto_start,
                "lmstudio_url": cfg.lmstudio.base_url,
                "lmstudio_binary": cfg.lmstudio.binary_path,
                "lmstudio_auto_start": cfg.lmstudio.auto_start,
            },
        }

    def apply_config(self, cfg: Config, *, persist: bool = True) -> None:
        """Apply validated config in place so existing runtimes cannot retain stale policy."""
        if cfg.permission_mode is PermissionMode.FULL_ACCESS and self.config.permission_mode is not PermissionMode.FULL_ACCESS:
            cfg = cfg.model_copy(deep=True)
            cfg.permission_mode = PermissionMode.DANGEROUS
        old_security = self._security_snapshot(self.config)
        new_security = self._security_snapshot(cfg)
        security_changed = old_security != new_security
        config_changed = self.config.model_dump(mode="json") != cfg.model_dump(mode="json")
        cfg.policy_generation = max(self.config.policy_generation, cfg.policy_generation)
        if security_changed:
            cfg.policy_generation += 1
        old_workspace = self.workspace
        old_memory = self.config.memory.model_dump(mode="json")
        old_logging = self.config.logging.model_dump(mode="json")
        if config_changed or security_changed:
            # Security-only deltas (e.g. yolo, which is excluded from dumps)
            # must retire runtimes too — a live runtime cannot keep stale policy.
            self._retire_runtimes()
        # Existing PolicyEngine and AgentRuntime objects reference this instance.
        for name in Config.model_fields:
            setattr(self.config, name, copy.deepcopy(getattr(cfg, name)))
        self._backend_cache.clear()
        if old_workspace != self.workspace:
            for attr in ("_policy", "_policy_cfg", "_policy_ws"):
                with contextlib.suppress(AttributeError):
                    delattr(self, attr)
        if old_memory != self.config.memory.model_dump(mode="json") and hasattr(self, "_soul"):
            del self._soul
        if old_logging != self.config.logging.model_dump(mode="json") and hasattr(self, "_audit"):
            del self._audit
        if security_changed:
            self.approvals.invalidate_all()
        if persist:
            self.save_config()

    def reload_config(self) -> Config:
        """Re-read config.toml from disk without writing anything back: the
        GUI bridge is a separate process, so keys/provider/model changes it
        saves are invisible to this TUI until now."""
        cfg = self.config_store.load()
        self.apply_config(cfg, persist=False)
        return self.config

    def set_mode(self, mode: PermissionMode, *, phrase: str | None = None) -> bool:
        """Full access requires fresh process-local confirmation; changes invalidate grants."""
        if mode is PermissionMode.FULL_ACCESS:
            if (phrase or "").strip() != self.full_access_phrase():
                return False
        changed = self.config.permission_mode is not mode
        self.config.permission_mode = mode
        if changed:
            self.config.policy_generation += 1
            self._retire_runtimes()
            self.approvals.invalidate_all()
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
                url = self.config.ollama.host.rstrip("/")
                async with be._client(url, timeout=10.0) as client:
                    await client.post("/api/generate", json={"model": model, "keep_alive": 0})

    def status_snapshot(self, tok_s: float = 0.0, ctx_used: int = 0) -> dict[str, Any]:
        gen = self.config.settings_for(self.config.default_backend, self.current_model())
        ctx_total = gen.num_ctx
        if self.config.default_backend.value == "cloud":
            # a cloud model's window comes from the provider's own metadata
            # (OpenRouter context_length etc.), not the local llama setting
            be = self._backend_cache.get("cloud")
            real = getattr(be, "_ctx_map_", {}).get(self.current_model() or "")
            if real:
                ctx_total = int(real)
        return {
            "backend": self.config.default_backend.value,
            "model": self.current_model() or "—",
            "mode": self.config.permission_mode.value,
            "ultrathink": gen.ultrathink,
            "effort": gen.effort,
            "num_ctx": ctx_total,
            "ctx_used": ctx_used,
            "ctx_pct": round(100 * ctx_used / max(1, ctx_total)),
            "tokens_s": tok_s,
            "audit": self.audit.health(),
            "updated": time.time(),
            "op_mode": self.config.agent.op_mode,
            "yolo": bool(getattr(self.config.agent, "yolo", False)),
        }
