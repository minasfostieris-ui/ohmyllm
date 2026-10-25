"""Permission/policy/safety layer — the only path to tool execution."""

from .approvals import (
    ApprovalManager,
    ApprovalOutcome,
    ApprovalRequest,
)
from .audit import AuditLogger, format_event
from .modes import FULL_ACCESS_PHRASE, MODE_ORDER, SAFE_COMMANDS, SUBCOMMAND_ALLOWLIST
from .policy import (
    TOOL_CLASSES,
    Decision,
    PolicyEngine,
    ToolClass,
    classify,
    describe,
)
from .redact import contains_secrets, redact_secrets
from .risk import CommandRisk, RiskScorer, level_for

__all__ = [
    "ApprovalManager",
    "ApprovalOutcome",
    "ApprovalRequest",
    "AuditLogger",
    "CommandRisk",
    "Decision",
    "FULL_ACCESS_PHRASE",
    "MODE_ORDER",
    "PolicyEngine",
    "RiskScorer",
    "SAFE_COMMANDS",
    "SUBCOMMAND_ALLOWLIST",
    "TOOL_CLASSES",
    "ToolClass",
    "classify",
    "contains_secrets",
    "describe",
    "format_event",
    "level_for",
    "redact_secrets",
]
