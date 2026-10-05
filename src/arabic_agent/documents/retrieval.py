"""Document retrieval.

The agent must never dump a whole document (or a folder of documents) into the
model context. Retrieval answers a focused query over an already-parsed
:class:`Document`, returning only the most relevant sections *with their
provenance* and an honest note of how much was left out.

The ranking here is lexical (term overlap + heading bonus). That is deliberate:
it needs no model, works fully offline, and is deterministic and testable. The
:class:`Retriever` protocol is the extension point where semantic/embedding
retrieval can be added later without touching the agent or the tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ..context.budget import estimate_tokens
from .models import Document, DocumentSection

_WORD_RE = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)


def _terms(text: str) -> list[str]:
    """Lowercased word tokens, Arabic-aware (keeps diacritic-free word chars)."""
    return [match.group(0).casefold() for match in _WORD_RE.finditer(text)]


@dataclass(frozen=True)
class RetrievedSection:
    """A retrieved section plus its relevance score and provenance."""

    section: DocumentSection
    score: float

    @property
    def provenance(self) -> str:
        return self.section.provenance.describe()


@dataclass(frozen=True)
class RetrievalResult:
    """The outcome of a retrieval query."""

    query: str
    matches: tuple[RetrievedSection, ...]
    total_sections: int
    considered: int
    truncated: bool
    #: Section ids that were *not* returned, so the agent knows what it skipped.
    omitted_section_ids: tuple[str, ...] = ()

    @property
    def provenance(self) -> tuple[str, ...]:
        return tuple(m.provenance for m in self.matches)

    def total_tokens(self) -> int:
        return sum(estimate_tokens(m.section.text) for m in self.matches)


class Retriever(Protocol):
    """Extension point: any object that can answer a query over a document."""

    def retrieve(
        self, document: Document, query: str, *, limit: int, max_tokens: int
    ) -> RetrievalResult: ...


class LexicalRetriever:
    """Deterministic, offline, dependency-free retrieval.

    Scores each section by query-term overlap, normalised by section length so a
    long section cannot win on volume alone, with a small bonus for headings and
    for an exact phrase hit. Ties break on document order, so results are stable.
    """

    def retrieve(
        self,
        document: Document,
        query: str,
        *,
        limit: int = 5,
        max_tokens: int = 4000,
    ) -> RetrievalResult:
        query_terms = set(_terms(query))
        phrase = query.strip().casefold()
        scored: list[RetrievedSection] = []
        for section in document.sections:
            section_terms = _terms(section.text)
            if not section_terms:
                continue
            overlap = sum(1 for term in set(section_terms) if term in query_terms)
            if overlap == 0 and phrase and phrase not in section.text.casefold():
                continue
            # Normalise by the square root of length so length helps but does
            # not dominate, then add structural bonuses.
            score = overlap / (len(section_terms) ** 0.5)
            if section.kind == "heading":
                score += 0.5
            if phrase and phrase in section.text.casefold():
                score += 1.0
            if score > 0:
                scored.append(RetrievedSection(section=section, score=score))

        scored.sort(key=lambda item: (-item.score, item.section.id))
        chosen: list[RetrievedSection] = []
        omitted: list[str] = []
        tokens_used = 0
        for item in scored:
            if len(chosen) >= limit:
                omitted.append(item.section.id)
                continue
            cost = estimate_tokens(item.section.text)
            if chosen and tokens_used + cost > max_tokens:
                omitted.append(item.section.id)
                continue
            chosen.append(item)
            tokens_used += cost

        return RetrievalResult(
            query=query,
            matches=tuple(chosen),
            total_sections=document.section_count,
            considered=len(scored),
            truncated=bool(omitted),
            omitted_section_ids=tuple(omitted),
        )


__all__ = [
    "LexicalRetriever",
    "RetrievalResult",
    "RetrievedSection",
    "Retriever",
]
