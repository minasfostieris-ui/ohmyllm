"""Collision-resistant, scoped approval flows."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..secureio import atomic_write, canonical_json, private_file_ok, private_mkdir
from .policy import Decision

_RULES_VERSION = 2


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    APPROVED_SESSION = "approved_session"
    APPROVED_ALWAYS = "approved_always"

    @property
    def approved(self) -> bool:
        return self in (
            ApprovalOutcome.APPROVED,
            ApprovalOutcome.APPROVED_SESSION,
            ApprovalOutcome.APPROVED_ALWAYS,
        )


@dataclass
class ApprovalRequest:
    tool: str
    args: dict
    decision: Decision
    preview: str = ""


PromptFn = Callable[[ApprovalRequest], Coroutine[Any, Any, ApprovalOutcome]]


def _scope(
    *, workspace: str = "", session_id: str = "", policy_generation: int = 0,
) -> dict[str, Any]:
    return {
        "workspace": str(workspace),
        "session_id": str(session_id),
        "policy_generation": int(policy_generation),
    }


def _key(
    tool: str,
    args: dict,
    *,
    workspace: str = "",
    session_id: str = "",
    policy_generation: int = 0,
) -> str:
    """Hash the complete canonical request; never normalize or truncate values."""
    identity = {
        "version": _RULES_VERSION,
        "tool": str(tool),
        "arguments": args,
        "scope": _scope(
            workspace=workspace,
            session_id=session_id,
            policy_generation=policy_generation,
        ),
    }
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return f"v{_RULES_VERSION}:sha256:{digest}"


def _permanent_key(
    tool: str,
    args: dict,
    *,
    workspace: str = "",
    policy_generation: int = 0,
) -> str:
    """Permanent grants omit session_id so they survive across chat sessions.

    Workspace and policy generation still bind the grant; any policy change
    invalidates it. Session-scoped grants keep using :func:`_key`.
    """
    identity = {
        "version": _RULES_VERSION,
        "tool": str(tool),
        "arguments": args,
        "scope": {
            "workspace": str(workspace),
            "policy_generation": int(policy_generation),
        },
    }
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return f"v{_RULES_VERSION}:sha256:{digest}"


@dataclass
class ApprovalManager:
    """Remembers exact scoped calls; shell authorization is always one-shot."""

    rules_path: Path | None = None
    _session: set[str] = field(default_factory=set)
    _permanent: set[str] = field(default_factory=set)
    _invalidation_epoch: int = field(default=0, init=False, repr=False)
    _pending: dict[asyncio.Task[Any], asyncio.AbstractEventLoop] = field(
        default_factory=dict, init=False, repr=False
    )
    _pending_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.load_rules()

    def load_rules(self) -> None:
        self._permanent = set()
        if not self.rules_path or not private_file_ok(self.rules_path):
            return
        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
            if data.get("version") != _RULES_VERSION:
                return  # all legacy, unscoped grant formats are intentionally invalidated
            self._permanent = {
                str(rule["key"])
                for rule in data.get("rules", [])
                if rule.get("decision") == "allow"
                and rule.get("kind") == "permanent"
                and str(rule.get("key", "")).startswith(f"v{_RULES_VERSION}:sha256:")
            }
            # Migration: legacy v2 keys embedded a session_id and can never be
            # re-derived (one-way hash). Dropping them here is fail-safe — the
            # dead grants expire instead of silently persisting.
        except (AttributeError, OSError, ValueError, TypeError, KeyError):
            self._permanent = set()

    def save_rules(self) -> None:
        if not self.rules_path:
            return
        payload = {
            "version": _RULES_VERSION,
            "rules": [
                {"key": key, "decision": "allow", "kind": "permanent"}
                for key in sorted(self._permanent)
            ],
        }
        private_mkdir(self.rules_path.parent)
        atomic_write(self.rules_path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def preapproved(
        self,
        tool: str,
        args: dict,
        *,
        workspace: str = "",
        session_id: str = "",
        policy_generation: int = 0,
    ) -> bool:
        session_key = _key(
            tool,
            args,
            workspace=workspace,
            session_id=session_id,
            policy_generation=policy_generation,
        )
        permanent_key = _permanent_key(
            tool,
            args,
            workspace=workspace,
            policy_generation=policy_generation,
        )
        return session_key in self._session or permanent_key in self._permanent

    def remember_session(
        self,
        tool: str,
        args: dict,
        *,
        workspace: str = "",
        session_id: str = "",
        policy_generation: int = 0,
    ) -> None:
        self._session.add(_key(
            tool,
            args,
            workspace=workspace,
            session_id=session_id,
            policy_generation=policy_generation,
        ))

    def add_permanent(
        self,
        tool: str,
        args: dict,
        *,
        workspace: str = "",
        session_id: str = "",
        policy_generation: int = 0,
    ) -> None:
        self._session.add(_key(
            tool,
            args,
            workspace=workspace,
            session_id=session_id,
            policy_generation=policy_generation,
        ))
        self._permanent.add(_permanent_key(
            tool,
            args,
            workspace=workspace,
            policy_generation=policy_generation,
        ))
        self.save_rules()

    def clear_session(self) -> None:
        self._session.clear()

    def invalidate_all(self) -> None:
        """Invalidate grants and actively deny prompts crossing a policy transition."""
        with self._pending_lock:
            self._invalidation_epoch += 1
            pending = tuple(self._pending.items())
            self._session.clear()
            self._permanent.clear()
        for task, loop in pending:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        self.save_rules()

    @property
    def permanent_rules(self) -> tuple[str, ...]:
        return tuple(sorted(self._permanent))

    async def decide(
        self,
        tool: str,
        args: dict,
        decision: Decision,
        prompt: PromptFn | None,
        preview: str = "",
        *,
        workspace: str = "",
        session_id: str = "",
        policy_generation: int = 0,
    ) -> ApprovalOutcome:
        """Return the final outcome without executing; failures deny."""
        if not decision.requires_approval:
            return ApprovalOutcome.APPROVED
        reusable = tool != "run_command"
        if reusable and self.preapproved(
            tool,
            args,
            workspace=workspace,
            session_id=session_id,
            policy_generation=policy_generation,
        ):
            return ApprovalOutcome.APPROVED_SESSION
        if prompt is None:
            return ApprovalOutcome.DENIED
        req = ApprovalRequest(tool=tool, args=args, decision=decision, preview=preview)
        loop = asyncio.get_running_loop()
        with self._pending_lock:
            epoch = self._invalidation_epoch
        prompt_task: asyncio.Task[ApprovalOutcome] = loop.create_task(prompt(req))
        with self._pending_lock:
            if epoch != self._invalidation_epoch:
                prompt_task.cancel()
            self._pending[prompt_task] = loop
        try:
            outcome = await prompt_task
        except asyncio.CancelledError:
            with self._pending_lock:
                invalidated = epoch != self._invalidation_epoch
            if invalidated:
                return ApprovalOutcome.DENIED
            raise
        except Exception:  # approval UI failure must fail closed
            return ApprovalOutcome.DENIED
        finally:
            with self._pending_lock:
                self._pending.pop(prompt_task, None)
        with self._pending_lock:
            if epoch != self._invalidation_epoch:
                return ApprovalOutcome.DENIED
        if not isinstance(outcome, ApprovalOutcome):
            try:
                outcome = ApprovalOutcome(str(outcome))
            except ValueError:
                return ApprovalOutcome.DENIED
        if tool == "run_command" and outcome.approved:
            return ApprovalOutcome.APPROVED  # prohibit reusable shell grants
        if outcome is ApprovalOutcome.APPROVED_SESSION:
            self.remember_session(
                tool,
                args,
                workspace=workspace,
                session_id=session_id,
                policy_generation=policy_generation,
            )
        elif outcome is ApprovalOutcome.APPROVED_ALWAYS:
            self.add_permanent(
                tool,
                args,
                workspace=workspace,
                session_id=session_id,
                policy_generation=policy_generation,
            )
        return outcome
