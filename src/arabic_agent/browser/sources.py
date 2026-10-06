"""Web research provenance and source quality.

Research answers must be traceable. This module keeps a bounded, per-run ledger
of the pages the agent actually read, so the final answer can cite real sources
instead of invented ones, and classifies each source's quality so the agent can
prefer primary/official material over aggregators.

Provenance is recorded from *observations*, not from the model: a source enters
the ledger only when the agent actually opened and read it. Nothing here calls a
model or the network -- it is pure bookkeeping plus a small, explicit quality
heuristic.
"""

from __future__ import annotations

import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ..core.enums import StrEnum as _StrEnum  # noqa: F401  (re-export convenience)


class SourceQuality(StrEnum):
    """How authoritative a source is judged to be (a heuristic, not truth)."""

    OFFICIAL = "official"  # gov/edu/intl-body, or a project's own docs/repo
    PRIMARY = "primary"  # a company's own site, a standards/spec page
    REFERENCE = "reference"  # encyclopedic / well-known reference
    GENERAL = "general"  # an ordinary site
    LOW = "low"  # a known aggregator/content farm pattern


#: Host suffixes that indicate an official/government/education source.
_OFFICIAL_SUFFIXES = (".gov", ".gov.uk", ".edu", ".ac.uk", ".mil", ".int", ".gouv.fr")
#: Hosts that are encyclopedic references.
_REFERENCE_HOSTS = ("wikipedia.org", "wikimedia.org", "britannica.com")
#: Host patterns that are typically low-quality aggregation.
_LOW_QUALITY_PATTERNS = (
    "pinterest.",
    "quora.com",
    "answers.",
    "contentfarm",
    "articlebiz",
    "ezinearticles",
)
#: Hosts that are a project's own documentation / source of truth.
_DOCS_HOSTS = (
    "docs.",
    "developer.",
    "readthedocs.io",
    "github.com",
    "gitlab.com",
    "python.org",
    "mozilla.org",
    "w3.org",
    "ietf.org",
    "rfc-editor.org",
)


def classify_source(url: str, *, title: str = "") -> SourceQuality:
    """Classify a source URL's quality by a small, explicit heuristic."""
    host = (url or "").lower()
    for suffix in _OFFICIAL_SUFFIXES:
        if f"{suffix}/" in host or host.endswith(suffix) or f"{suffix}:" in host:
            return SourceQuality.OFFICIAL
    if any(pattern in host for pattern in _LOW_QUALITY_PATTERNS):
        return SourceQuality.LOW
    if any(ref in host for ref in _REFERENCE_HOSTS):
        return SourceQuality.REFERENCE
    if any(docs in host for docs in _DOCS_HOSTS):
        return SourceQuality.PRIMARY
    if title and ("documentation" in title.lower() or "official" in title.lower()):
        return SourceQuality.PRIMARY
    return SourceQuality.GENERAL


class SourceRecord(BaseModel):
    """One source the agent read, with enough to cite it honestly."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""
    quality: SourceQuality = SourceQuality.GENERAL
    #: Character offset / heading the agent focused on, where practical.
    locator: str = ""
    #: When the page was observed (monotonic seconds since run start).
    observed_at: float = 0.0
    #: A short, bounded excerpt supporting the claim (never the whole page).
    excerpt: str = ""
    observation_id: str = ""


class SourceLedger:
    """A bounded, per-run ledger of read sources."""

    def __init__(self, *, max_sources: int = 20) -> None:
        self._max = max_sources
        self._by_run: dict[str, list[SourceRecord]] = {}

    def record(self, run_id: str, record: SourceRecord) -> SourceRecord:
        bucket = self._by_run.setdefault(run_id, [])
        # De-duplicate by URL, keeping the most recent read of that URL.
        bucket[:] = [existing for existing in bucket if existing.url != record.url]
        bucket.append(record)
        if len(bucket) > self._max:
            del bucket[0 : len(bucket) - self._max]
        return record

    def sources(self, run_id: str) -> list[SourceRecord]:
        return list(self._by_run.get(run_id, ()))

    def clear(self, run_id: str) -> None:
        self._by_run.pop(run_id, None)

    def render(self, run_id: str) -> list[dict[str, object]]:
        """A compact, model-facing source list ordered best-quality first."""
        order = {
            SourceQuality.OFFICIAL: 0,
            SourceQuality.PRIMARY: 1,
            SourceQuality.REFERENCE: 2,
            SourceQuality.GENERAL: 3,
            SourceQuality.LOW: 4,
        }
        ranked = sorted(self.sources(run_id), key=lambda s: (order[s.quality], s.url))
        return [
            {
                "url": s.url,
                "title": s.title,
                "quality": s.quality.value,
                "locator": s.locator,
                "excerpt": s.excerpt,
            }
            for s in ranked
        ]


def make_source(
    *,
    url: str,
    title: str = "",
    locator: str = "",
    excerpt: str = "",
    observation_id: str = "",
    observed_at: float | None = None,
) -> SourceRecord:
    """Build a :class:`SourceRecord`, classifying its quality."""
    return SourceRecord(
        url=url,
        title=title,
        quality=classify_source(url, title=title),
        locator=locator,
        excerpt=excerpt[:500],
        observation_id=observation_id,
        observed_at=observed_at if observed_at is not None else time.monotonic(),
    )


__all__ = [
    "SourceLedger",
    "SourceQuality",
    "SourceRecord",
    "classify_source",
    "make_source",
]
