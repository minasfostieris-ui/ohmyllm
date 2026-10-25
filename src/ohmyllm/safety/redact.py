"""Secret detection/redaction for audit logs and memory writes."""

from __future__ import annotations

import re
from typing import Final

_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PEM KEY]"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|pk|api|key|token|secret|ghp|gho|github_pat|xox[abpr])-[-_A-Za-z0-9]{10,}\b"), "[REDACTED TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api_?key|access_?key|auth)\b\s*[:=]\s*[\"']?[^\s\"']{4,}[\"']?"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "[REDACTED JWT]"),
]

_SECRET_SCAN: Final = tuple(p for p, _ in _PATTERNS)


def redact_secrets(text: str) -> str:
    """Replace obvious secrets with placeholders (used before logging)."""
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def contains_secrets(text: str) -> bool:
    """True when text looks like it carries credentials (memory refusal)."""
    return any(p.search(text) for p in _SECRET_SCAN)
