"""Structured secret detection and redaction for persisted state."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|auth|cookie|session)(?:$|[_-])"
)
_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PEM KEY]"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1Bearer [REDACTED]"),
    (re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(?:sk|pk|api|key|token|secret|gh[pousr]|github_pat|xox[abpr])[-_][-_A-Za-z0-9]{8,}\b"), "[REDACTED TOKEN]"),
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|pwd|secret|token|api_?key|access_?key|auth)[\"']?"
            r"\s*[:=]\s*)(?P<quote>[\"']?)[^\s\"',}]{4,}(?P=quote)"
        ),
        r"\1\g<quote>[REDACTED]\g<quote>",
    ),
    (re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "[REDACTED JWT]"),
]
_SECRET_SCAN: Final = tuple(pattern for pattern, _ in _PATTERNS)


def redact_secrets(text: str) -> str:
    """Replace recognizable credentials in free-form text."""
    out = str(text)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_value(value: Any, *, key: str = "") -> Any:
    """Redact nested structures before serialization, preserving their shape."""
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, bytes):
        return redact_secrets(value.decode("utf-8", errors="replace"))
    return value


def contains_secrets(text: str) -> bool:
    """Return true when text resembles credentials, including URL userinfo."""
    return any(pattern.search(str(text)) for pattern in _SECRET_SCAN)
