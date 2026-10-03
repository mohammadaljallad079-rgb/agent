"""Secret redaction.

Audit logs and error payloads must never contain credentials. Every value that
leaves a tool/model boundary passes through :func:`redact` before it is
persisted or logged. This is a defense-in-depth measure: we also avoid putting
secrets into payloads in the first place.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Key names whose values are always masked, matched case-insensitively.
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|secret|token|password|passwd|pwd|authorization|"
    r"auth|credential|private[_-]?key|client[_-]?secret|session[_-]?key|"
    r"refresh[_-]?token|access[_-]?token|bearer|cookie)",
    re.IGNORECASE,
)

# Value patterns that look like credentials even under an innocent key.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})\b"),  # OpenAI-style
    re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{16,})\b"),  # Anthropic
    re.compile(r"\b(ghp_[A-Za-z0-9]{20,})\b"),  # GitHub PAT
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})\b"),  # JWT
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
)

_MAX_DEPTH = 12


def _redact_string(value: str) -> str:
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive data masked.

    Handles nested dicts/lists/tuples/sets. Non-container scalars are returned
    as-is unless they match a known credential value pattern.
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (
                REDACTED if _SENSITIVE_KEY_RE.search(str(key)) else redact(val, _depth=_depth + 1)
            )
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, set):
        return {redact(item, _depth=_depth + 1) for item in value}
    if isinstance(value, str):
        return _redact_string(value)
    return value


def is_sensitive_key(key: str) -> bool:
    """Whether a key name should be treated as sensitive."""
    return bool(_SENSITIVE_KEY_RE.search(key))
