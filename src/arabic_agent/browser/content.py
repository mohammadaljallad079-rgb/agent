"""Bounded web-text helpers.

Large pages must never be dumped into model context. These pure helpers
normalise whitespace, bound a text extract and report *that* it was bounded, and
search within a page for a term while returning only a bounded window of context
around each match -- with a character offset for provenance.

Keeping these pure (no browser, no I/O) makes the bounded-reading behaviour
testable in isolation and identical across backends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

_WHITESPACE_RE = re.compile(r"[ \t\u00a0\u200f\u200e]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n\s*")


def normalize_text(text: str) -> str:
    """Collapse runs of spaces and excess blank lines without dropping lines."""
    if not text:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", text)
    collapsed = _BLANKLINES_RE.sub("\n", collapsed)
    return "\n".join(line.strip() for line in collapsed.splitlines() if line.strip())


class BoundedText(BaseModel):
    """A text extract plus honest reporting of what was left out."""

    model_config = ConfigDict(frozen=True)

    text: str
    total_chars: int
    truncated: bool

    @property
    def omitted_chars(self) -> int:
        return max(0, self.total_chars - len(self.text))


def bound_text(text: str, *, max_chars: int) -> BoundedText:
    """Return at most ``max_chars`` of ``text``, reporting truncation honestly."""
    normalized = normalize_text(text)
    total = len(normalized)
    if total <= max_chars:
        return BoundedText(text=normalized, total_chars=total, truncated=False)
    return BoundedText(text=normalized[:max_chars], total_chars=total, truncated=True)


@dataclass(frozen=True)
class TextMatch:
    """One match of a search term within page text."""

    term: str
    offset: int
    context: str


def search_text(
    text: str, term: str, *, max_matches: int = 5, context_chars: int = 200
) -> list[TextMatch]:
    """Find ``term`` in ``text`` and return bounded context around each match.

    Matching is case-insensitive and Unicode-aware, so an Arabic query matches
    Arabic text. Only a small window around each hit is returned, never the
    whole page.
    """
    if not text or not term.strip():
        return []
    matches: list[TextMatch] = []
    lowered = text.lower()
    needle = term.lower()
    start = 0
    while len(matches) < max_matches:
        index = lowered.find(needle, start)
        if index < 0:
            break
        left = max(0, index - context_chars // 2)
        right = min(len(text), index + len(term) + context_chars // 2)
        matches.append(TextMatch(term=term, offset=index, context=text[left:right].strip()))
        start = index + len(term)
    return matches


def text_sections(text: str, *, max_sections: int = 20, max_chars: int = 800) -> list[str]:
    """Split text into paragraph-ish sections, each bounded, for paginated reads."""
    normalized = normalize_text(text)
    if not normalized:
        return []
    blocks = [b.strip() for b in normalized.split("\n") if b.strip()]
    return [block[:max_chars] for block in blocks[:max_sections]]


__all__ = [
    "BoundedText",
    "TextMatch",
    "bound_text",
    "normalize_text",
    "search_text",
    "text_sections",
]
