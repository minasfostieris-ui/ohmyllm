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
    # R5 F1: a yolo arm/disarm requested mid-turn is deferred to the NEXT
    # prompt — never applied to the running turn, never retires it.
    _yolo_pending: bool | None = field(default=None, repr=False)
    # R8 G3: the bridge wires its turn_lock here so an echo apply can snapshot
    # the live runtime flags atomically w.r.t. a settling turn. Brief hold only
    # — the apply itself stays OUTSIDE the lock (deadlock avoidance: never hold
    # turn_lock across a gated apply). None for TUI/plain/CLI (no turn loop).
    _runtime_lock: Any = field(default=None, repr=False)
    # R9 N-R9-2a/2b: dedicated config-mutation mutex (the bridge wires its
    # config_mutex here). Serializes the FULL merge->validate->apply echo
    # window and disk reloads against each other so concurrent echoes can
    # never merge from the same pre-apply live config (lost update / stale
    # security revert). NEVER turn_lock — apply stays OUTSIDE turn_lock
    # (deadlock law); lock order is config_mutex -> turn_lock. Re-entrant:
    # an echo site already holds it when apply_config re-acquires it.
    # None for TUI/plain/CLI (single-threaded config mutation).
    _config_mutex: Any = field(default=None, repr=False)

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

    def apply_config(self, cfg: Config, *, persist: bool = True, carry_runtime: bool = False) -> None:
        """Apply validated config in place so existing runtimes cannot retain stale policy.

        carry_runtime=False (default) applies the incoming config as-is: an
        intentional runtime-flag delta (e.g. a direct agent.yolo arm) is visible
        to the security snapshot and retires live runtimes (R1-H2 / R3-C5).
        carry_runtime=True is for the bridge config-mutation endpoints, which
        ECHO the live runtime flags with no yolo intent — see the carry below.
        The bridge signals this by marking the cfg (_omllm_carry_runtime) and
        calling apply_config positionally, so duck-typed fakes (lambda n: ...)
        keep working; direct callers may pass the kwarg instead.
        """
        # R9 N-R9-2a/2b: serialize applies against concurrent echo windows
        # (re-entrant when the calling site already holds the mutex).
        mutex = getattr(self, "_config_mutex", None)
        with mutex if mutex is not None else contextlib.nullcontext():
            self._apply_config_inner(cfg, persist=persist, carry_runtime=carry_runtime)

    def _apply_config_inner(self, cfg: Config, *, persist: bool, carry_runtime: bool) -> None:
        carry_runtime = carry_runtime or getattr(cfg, "_omllm_carry_runtime", False)
        if cfg.permission_mode is PermissionMode.FULL_ACCESS and self.config.permission_mode is not PermissionMode.FULL_ACCESS:
            cfg = cfg.model_copy(deep=True)
            cfg.permission_mode = PermissionMode.DANGEROUS
        if carry_runtime:
            # R7 (Y1/Y2/Y3/Y5): runtime-only flags (Field exclude=True, e.g.
            # agent.yolo) ARE the live next-prompt state. A config copy taken
            # before a turn's one-prompt disarm or deferred settle goes stale the
            # moment the turn ends; applying it must never resurrect a CONSUMED
            # arm (fail-open) nor wipe a SETTLED/idle arm (F1 break).
            # R7 gate blocker 1: this carry is OPT-IN (carry_runtime) so it only
            # touches runtime ECHOES, never an intentional yolo delta from a
            # direct caller (R1-H2 / R3-C5 retirement stays intact).
            # R8 G3: carry + old snapshot are ONE atomic read under the runtime
            # lock (the bridge's turn_lock, which work()'s finally settles
            # under) — three separate live reads let a settle land mid-apply
            # and resurrect a consumed arm / trip a spurious retire.
            lock = getattr(self, "_runtime_lock", None)
            with lock if lock is not None else contextlib.nullcontext():
                runtime_live: dict[tuple[str, str], Any] = {}
                for name, field_info in Config.model_fields.items():
                    live = getattr(self.config, name)
                    sub_fields = getattr(type(live), "model_fields", None)
                    if sub_fields is not None:
                        for sub_name, sub_info in sub_fields.items():
                            if sub_info.exclude:
                                runtime_live[(name, sub_name)] = getattr(live, sub_name)
                    elif field_info.exclude:
                        runtime_live[(name, "")] = live
                old_security = self._security_snapshot(self.config)
            # Carry the single snapshot into the incoming cfg BEFORE the new
            # snapshot, so a pure/stale echo shows NO spurious yolo delta (a
            # model/theme/memory save must not retire the running turn — C5).
            for (name, sub_name), value in runtime_live.items():
                if sub_name:
                    setattr(getattr(cfg, name), sub_name, value)
                else:
                    setattr(cfg, name, value)
            # Pin the old snapshot's runtime entries to the SAME single read:
            # the runtime's one-prompt disarm is lock-free (agents/runtime.py),
            # so it can still land between the locked read and the compare —
            # old==new for the carried flags no matter where the settle lands.
            for (name, sub_name), value in runtime_live.items():
                key = sub_name or name
                if key in old_security:
                    old_security[key] = value
        else:
            old_security = self._security_snapshot(self.config)
        new_security = self._security_snapshot(cfg)
        security_changed = old_security != new_security
        cfg.policy_generation = max(self.config.policy_generation, cfg.policy_generation)
        if security_changed:
            cfg.policy_generation += 1
        old_workspace = self.workspace
        old_memory = self.config.memory.model_dump(mode="json")
        old_logging = self.config.logging.model_dump(mode="json")
        if security_changed:
            # R3 C5: retire ONLY on security-boundary deltas (the snapshot
            # covers yolo/tone/op_mode, which are excluded from dumps).
            # Cosmetic/generation/ui changes apply in place below — a live
            # runtime reads them from this same config instance, and a
            # theme save must never cancel the user's running turn.
            self._retire_runtimes()
        # Existing PolicyEngine and AgentRuntime objects reference this instance.
        # R10 M8: snapshot the pre-apply state (persist path only) so a FAILED
        # save can roll the live config back to the on-disk state — a 500 must
        # not leave live flipped while disk is stale (no mtime move means
        # _sync_disk_config never reconciles). Runtime exclude flags are NEVER
        # restored (same law as the carry); the retire/invalidate that already
        # ran stay (fail-closed).
        pre_apply = self.config.model_copy(deep=True) if persist else None
        for name in Config.model_fields:
            live_sec = getattr(self.config, name)
            fresh_sec = getattr(cfg, name)
            sub_fields = getattr(type(live_sec), "model_fields", None)
            if carry_runtime and sub_fields is not None and any(
                    i.exclude for i in sub_fields.values()):
                # R8 G3: update the live section IN PLACE and never write the
                # runtime exclude flags — a settle/disarm landing mid-apply
                # (the runtime disarm is lock-free) is never overwritten, so
                # the carried snapshot can neither resurrect nor wipe. Live
                # wins by construction; the atomic snapshot above only feeds
                # the security COMPARE.
                for sub_name, sub_info in sub_fields.items():
                    if not sub_info.exclude:
                        setattr(live_sec, sub_name, copy.deepcopy(getattr(fresh_sec, sub_name)))
            elif carry_runtime and Config.model_fields[name].exclude:
                continue  # runtime-only top-level flag: live wins
            else:
                setattr(self.config, name, copy.deepcopy(fresh_sec))
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
            try:
                self.save_config()
            except Exception:
                # R10 M8: restore the non-runtime fields in place from the
                # pre-apply snapshot (live==disk(old)), refresh the backend
                # cache, and re-raise so the caller still reports the failure.
                # save_config stays the persist/injection point.
                assert pre_apply is not None
                for name in Config.model_fields:
                    live_sec = getattr(self.config, name)
                    old_sec = getattr(pre_apply, name)
                    sub_fields = getattr(type(live_sec), "model_fields", None)
                    if sub_fields is not None:
                        for sub_name, sub_info in sub_fields.items():
                            if not sub_info.exclude:
                                setattr(live_sec, sub_name,
                                        copy.deepcopy(getattr(old_sec, sub_name)))
                    elif Config.model_fields[name].exclude:
                        continue  # runtime-only top-level flag: live wins
                    else:
                        setattr(self.config, name, copy.deepcopy(old_sec))
                self._backend_cache.clear()
                raise

    def reload_config(self) -> Config:
        """Re-read config.toml from disk without writing anything back: the
        GUI bridge is a separate process, so keys/provider/model changes it
        saves are invisible to this TUI until now."""
        # R9 N-R9-2a: the disk LOAD + apply share the config-mutation mutex —
        # a stale disk read must not interleave with an echo's merge->apply
        # window either (re-entrant: apply_config re-acquires it).
        mutex = getattr(self, "_config_mutex", None)
        with mutex if mutex is not None else contextlib.nullcontext():
            cfg = self.config_store.load()
            # R3 U3: runtime-only flags (Field exclude=True, e.g. agent.yolo)
            # never reach config.toml — a wholesale replace on reload would
            # silently reset them, so any out-of-band save disarm /yolo.
            # R8 G3: carry through the UNIFIED atomic echo path at apply time
            # (carry_runtime=True) instead of a private pre-carry loop here —
            # that loop was a stale second live read (the same TOCTOU hole).
            # Real disk edits still apply; a no-op reload produces no security
            # delta (C5: nothing retires).
            self.apply_config(cfg, persist=False, carry_runtime=True)
        return self.config

    def set_mode(self, mode: PermissionMode, *, phrase: str | None = None) -> bool:
        """Full access requires fresh process-local confirmation; changes invalidate grants."""
        if mode is PermissionMode.FULL_ACCESS:
            if (phrase or "").strip() != self.full_access_phrase():
                return False
        # R10 N-R10-2: mutation + gen bump + retire + invalidate + save share
        # the config-mutation mutex so a concurrent stale echo/reload can never
        # revert a confirmed tightening (M5b fail-open). NEVER turn_lock; None
        # for TUI/plain/CLI (single-threaded). Phrase check + audit stay outside.
        mutex = getattr(self, "_config_mutex", None)
        with mutex if mutex is not None else contextlib.nullcontext():
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

    # ------------------------------------------------- yolo (R5 F1 contract)
    def effective_yolo(self) -> bool:
        """The NEXT prompt's yolo state: a deferred mid-turn delta if one is
        pending, else the live flag. This is what every UI must show."""
        if self._yolo_pending is not None:
            return self._yolo_pending
        return bool(getattr(self.config.agent, "yolo", False))

    def request_yolo(self, want: bool, *, busy: bool) -> bool:
        """Arm/disarm yolo. While a turn is busy the delta is DEFERRED: the
        live config stays untouched, so the running turn keeps its entry
        policy and no security retire/cancel can hit it. Returns True when
        the request was deferred (apply it with settle_yolo at turn end)."""
        if busy:
            self._yolo_pending = bool(want)
            return True
        self._yolo_pending = None
        self.config.agent.yolo = bool(want)
        return False

    def settle_yolo(self) -> None:
        """Turn end: apply a deferred delta AFTER the runtime's one-prompt
        finally-disarm (so a fresh mid-turn arm is never eaten by it) and
        BEFORE the leg clears its busy flag (so it can never leak into a
        turn that starts next). Idempotent; a no-op without a pending delta."""
        if self._yolo_pending is not None:
            self.config.agent.yolo = self._yolo_pending
            self._yolo_pending = None

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
            # R5 F1: truthful next-prompt state (a mid-turn delta is deferred)
            "yolo": self.effective_yolo(),
        }
