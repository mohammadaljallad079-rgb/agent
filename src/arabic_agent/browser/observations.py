"""Browser observations and their bounded, ephemeral store.

A :class:`BrowserObservation` is the agent's view of a page: bounded text, a
bounded set of interactive elements with stable refs, headings, forms, links,
scroll metrics and any open dialog. It never contains a raw DOM dump -- that
would blow the context budget and drown the model in noise.

Observations are *freshness-tracked*: each carries a state identity and a
perceptual change hash, so the runtime can refuse an action taken on a stale
reference after the page has navigated or changed materially.

They are stored in memory only, bounded per run, and evicted oldest-first. A
screenshot, when taken, is attached to the observation as in-memory bytes and is
never written to disk or the audit log.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import NotFoundError
from .base import (
    BrowserPageState,
    DialogInfo,
    ElementInfo,
    FormInfo,
    PageSnapshot,
    Screenshot,
)
from .injection import InjectionIndicator, scan_for_injection

#: How many observations are retained per run. Small on purpose: the agent
#: re-observes rather than hoarding page snapshots.
DEFAULT_MAX_PER_RUN = 8


class BrowserObservation(BaseModel):
    """A bounded, normalised view of one page at one moment."""

    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    tab_id: str = ""
    url: str = ""
    title: str = ""
    state: BrowserPageState = BrowserPageState.EMPTY
    captured_at: float = 0.0

    text: str = ""
    text_truncated: bool = False
    headings: tuple[str, ...] = ()
    elements: tuple[ElementInfo, ...] = ()
    forms: tuple[FormInfo, ...] = ()
    links: tuple[ElementInfo, ...] = ()
    dialog: DialogInfo | None = None
    iframe_count: int = 0
    scroll_y: float = 0.0
    scroll_height: float = 0.0
    viewport_height: float = 0.0
    has_more_below: bool = False

    #: Suspicious prompt-injection indicators found in the page text. The page
    #: content is untrusted data; this is a *signal* to the model and the audit
    #: trail, never an instruction to follow.
    injection: tuple[InjectionIndicator, ...] = ()
    #: A perceptual hash of the observable page identity, for change detection.
    change_hash: str = ""

    #: A page screenshot, held in memory only (never persisted). Present only
    #: when the runtime chose to capture one for a vision-capable model.
    screenshot: Screenshot | None = Field(default=None, repr=False)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.captured_at)

    def is_stale(self, *, max_age: float) -> bool:
        return self.age_seconds > max_age

    def element(self, ref: str) -> ElementInfo | None:
        for element in self.elements:
            if element.ref == ref:
                return element
        return None

    def summary(self) -> dict[str, object]:
        """A compact, model-facing summary (bounded; no raw DOM)."""
        return {
            "observation_id": self.id,
            "url": self.url,
            "title": self.title,
            "state": self.state.value,
            "tab_id": self.tab_id,
            "headings": list(self.headings[:12]),
            "text": self.text,
            "text_truncated": self.text_truncated,
            "elements": [
                {
                    "ref": e.ref,
                    "role": e.role,
                    "name": e.name or e.text,
                    **({"href": e.href} if e.href else {}),
                    **({"value": e.value} if e.value else {}),
                    **({"checked": e.checked} if e.checked is not None else {}),
                    **({"disabled": True} if e.disabled else {}),
                }
                for e in self.elements
            ],
            "forms": [
                {
                    "ref": f.ref,
                    "name": f.name,
                    "action": f.action,
                    "fields": [
                        {
                            "ref": field.ref,
                            "label": field.label or field.name,
                            "type": field.input_type,
                            **({"value": field.value} if field.value else {}),
                            **({"options": list(field.options)} if field.options else {}),
                            **({"required": True} if field.required else {}),
                        }
                        for field in f.fields
                    ],
                }
                for f in self.forms
            ],
            "links": [{"ref": e.ref, "text": e.name or e.text, "href": e.href} for e in self.links],
            "dialog": None
            if self.dialog is None
            else {"type": self.dialog.type, "message": self.dialog.message},
            "iframe_count": self.iframe_count,
            "has_more_below": self.has_more_below,
            "scroll": {
                "y": round(self.scroll_y),
                "height": round(self.scroll_height),
                "viewport": round(self.viewport_height),
            },
            "injection_indicators": [i.kind for i in self.injection],
        }


def _change_hash(snapshot: PageSnapshot) -> str:
    """A stable hash of the observable page identity (not the full DOM)."""
    parts = [
        snapshot.url,
        snapshot.title,
        "|".join(snapshot.headings[:10]),
        "|".join(f"{e.ref}:{e.role}:{e.name or e.text}" for e in snapshot.elements[:40]),
        snapshot.text[:2000],
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def build_observation(
    *,
    observation_id: str,
    run_id: str,
    snapshot: PageSnapshot,
    max_text_chars: int,
    screenshot: Screenshot | None = None,
) -> BrowserObservation:
    """Turn a raw :class:`PageSnapshot` into a bounded observation."""
    text = snapshot.text or ""
    truncated = snapshot.text_truncated or len(text) > max_text_chars
    if len(text) > max_text_chars:
        text = text[:max_text_chars]
    return BrowserObservation(
        id=observation_id,
        run_id=run_id,
        tab_id="",
        url=snapshot.url,
        title=snapshot.title,
        state=snapshot.state,
        captured_at=time.monotonic(),
        text=text,
        text_truncated=truncated,
        headings=snapshot.headings,
        elements=snapshot.elements,
        forms=snapshot.forms,
        links=snapshot.links,
        dialog=snapshot.dialog,
        iframe_count=snapshot.iframe_count,
        scroll_y=snapshot.scroll_y,
        scroll_height=snapshot.scroll_height,
        viewport_height=snapshot.viewport_height,
        has_more_below=snapshot.has_more_below,
        injection=scan_for_injection(text),
        change_hash=_change_hash(snapshot),
        screenshot=screenshot,
    )


class ObservationStore:
    """Keeps the most recent observations per run, in memory only."""

    def __init__(
        self, *, max_per_run: int = DEFAULT_MAX_PER_RUN, ttl_seconds: float = 300.0
    ) -> None:
        self._max = max_per_run
        self._ttl = ttl_seconds
        self._by_run: dict[str, OrderedDict[str, BrowserObservation]] = {}

    def put(self, observation: BrowserObservation) -> None:
        bucket = self._by_run.setdefault(observation.run_id, OrderedDict())
        bucket[observation.id] = observation
        bucket.move_to_end(observation.id)
        while len(bucket) > self._max:
            bucket.popitem(last=False)

    def get(self, observation_id: str, *, run_id: str) -> BrowserObservation:
        bucket = self._by_run.get(run_id)
        observation = bucket.get(observation_id) if bucket else None
        if observation is None:
            raise NotFoundError(
                "no such browser observation for this run",
                details={"observation_id": observation_id},
            )
        return observation

    def latest(self, *, run_id: str) -> BrowserObservation | None:
        bucket = self._by_run.get(run_id)
        if not bucket:
            return None
        return next(reversed(bucket.values()))

    def evict_expired(self, *, now: float | None = None) -> int:
        moment = now if now is not None else time.monotonic()
        removed = 0
        for run_id, bucket in list(self._by_run.items()):
            for obs_id, observation in list(bucket.items()):
                if moment - observation.captured_at > self._ttl:
                    bucket.pop(obs_id, None)
                    removed += 1
            if not bucket:
                self._by_run.pop(run_id, None)
        return removed

    def clear(self, *, run_id: str) -> None:
        self._by_run.pop(run_id, None)


__all__ = [
    "DEFAULT_MAX_PER_RUN",
    "BrowserObservation",
    "ObservationStore",
    "build_observation",
]
