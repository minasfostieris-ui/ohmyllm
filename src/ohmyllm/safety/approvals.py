"""Approval flows: per-session memory and permanent rules."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .policy import Decision


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    APPROVED_SESSION = "approved_session"
    APPROVED_ALWAYS = "approved_always"

    @property
    def approved(self) -> bool:
        return self in (ApprovalOutcome.APPROVED, ApprovalOutcome.APPROVED_SESSION, ApprovalOutcome.APPROVED_ALWAYS)


@dataclass
class ApprovalRequest:
    tool: str
    args: dict
    decision: Decision
    preview: str = ""


PromptFn = Callable[[ApprovalRequest], Awaitable[ApprovalOutcome]]


def _key(tool: str, args: dict) -> str:
    def norm(v: object) -> str:
        if isinstance(v, str):
            v = re.sub(r"\s+", " ", v.strip())
            if len(v) > 120:
                v = v[:120] + "…"
        return json.dumps(v, sort_keys=True, default=str)

    inner = ",".join(f"{k}={norm(args[k])}" for k in sorted(args))
    return f"{tool}:{inner}"


@dataclass
class ApprovalManager:
    """Remembers approved calls for the session; optional permanent rules file."""

    rules_path: Path | None = None
    _session: set[str] = field(default_factory=set)
    _permanent: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.load_rules()

    # ------------------------------------------------------------ rules
    def load_rules(self) -> None:
        self._permanent = set()
        if self.rules_path and self.rules_path.is_file():
            try:
                data = json.loads(self.rules_path.read_text(encoding="utf-8"))
                self._permanent = {r["key"] for r in data.get("rules", []) if r.get("decision") == "allow"}
            except Exception:  # noqa: BLE001 - corrupt rules file shouldn't brick the app
                self._permanent = set()

    def save_rules(self) -> None:
        if not self.rules_path:
            return
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rules": [{"key": k, "decision": "allow"} for k in sorted(self._permanent)]}
        tmp = self.rules_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.rules_path)

    # ------------------------------------------------------------ checks
    def preapproved(self, tool: str, args: dict) -> bool:
        return _key(tool, args) in (self._session | self._permanent)

    def remember_session(self, tool: str, args: dict) -> None:
        self._session.add(_key(tool, args))

    def add_permanent(self, tool: str, args: dict) -> None:
        self._session.add(_key(tool, args))
        self._permanent.add(_key(tool, args))
        self.save_rules()

    def clear_session(self) -> None:
        self._session.clear()

    @property
    def permanent_rules(self) -> tuple[str, ...]:
        return tuple(sorted(self._permanent))

    # ------------------------------------------------------------ flow
    async def decide(
        self,
        tool: str,
        args: dict,
        decision: Decision,
        prompt: PromptFn | None,
        preview: str = "",
    ) -> ApprovalOutcome:
        """Returns the final outcome; never executes anything itself."""
        if not decision.requires_approval:
            return ApprovalOutcome.APPROVED
        if self.preapproved(tool, args):
            return ApprovalOutcome.APPROVED_SESSION
        if prompt is None:
            return ApprovalOutcome.DENIED  # non-interactive context: fail closed
        req = ApprovalRequest(tool=tool, args=args, decision=decision, preview=preview)
        outcome = await prompt(req)
        if outcome is ApprovalOutcome.APPROVED_SESSION:
            self.remember_session(tool, args)
        elif outcome is ApprovalOutcome.APPROVED_ALWAYS:
            self.add_permanent(tool, args)
        return outcome
