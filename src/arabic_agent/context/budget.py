"""Token budgeting.

A cheap, dependency-free estimator good enough for *allocation* decisions (the
provider reports true usage for accounting). We deliberately over-estimate for
Arabic text, where a Latin-centric heuristic would under-count and blow the
window.
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass, field

#: Average characters per token for Latin script.
_LATIN_CHARS_PER_TOKEN = 4.0
#: Arabic is tokenised less efficiently by most tokenisers; assume fewer
#: characters per token so we reserve more budget.
_ARABIC_CHARS_PER_TOKEN = 2.5


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` with script-aware weighting."""
    if not text:
        return 0
    arabic = sum(1 for ch in text if _is_arabic(ch))
    other = len(text) - arabic
    arabic_tokens = arabic / _ARABIC_CHARS_PER_TOKEN
    other_tokens = other / _LATIN_CHARS_PER_TOKEN
    return max(1, math.ceil(arabic_tokens + other_tokens))


def estimate_tool_schema(name: str, description: str, parameters: object) -> int:
    """Estimate the tokens a single advertised tool adds to a request.

    Providers (and llama.cpp's chat template) serialise the *whole* function
    object — the ``{"type": "function", ...}`` wrapper plus the JSON-schema
    ``parameters`` — not just the name and description. Counting the full
    payload keeps the context budget and the local-model guard honest, so a
    small window is never overflowed.
    """
    payload = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }
    return estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _is_arabic(ch: str) -> bool:
    code = ord(ch)
    # Arabic, Arabic Supplement, Arabic Extended-A/B, Arabic Presentation Forms.
    return (
        0x0600 <= code <= 0x06FF
        or 0x0750 <= code <= 0x077F
        or 0x08A0 <= code <= 0x08FF
        or 0xFB50 <= code <= 0xFDFF
        or 0xFE70 <= code <= 0xFEFF
    )


@dataclass
class TokenBudget:
    """Tracks token allocation across context sections."""

    total: int
    used: int = 0
    sections: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def can_fit(self, tokens: int) -> bool:
        return self.used + tokens <= self.total

    def reserve(self, section: str, tokens: int) -> bool:
        """Reserve ``tokens`` for a section if they fit. Returns success."""
        if not self.can_fit(tokens):
            return False
        self.used += tokens
        self.sections[section] = self.sections.get(section, 0) + tokens
        return True

    def force_reserve(self, section: str, tokens: int) -> None:
        """Reserve unconditionally (for non-negotiable sections)."""
        self.used += tokens
        self.sections[section] = self.sections.get(section, 0) + tokens


def normalize_text(text: str) -> str:
    """Normalize Unicode so Arabic variants compare consistently."""
    return unicodedata.normalize("NFC", text)
