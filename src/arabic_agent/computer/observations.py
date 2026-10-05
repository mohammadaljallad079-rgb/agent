"""A bounded, ephemeral store of recent observations.

Observations carry a screenshot, so they must not accumulate. This store keeps
only the most recent observations for a run, evicts the oldest past a small cap,
and never persists to disk. Its purpose is to let a later action reference a
recent observation by id (for freshness checks and post-action verification)
without the image ever leaving memory.
"""

from __future__ import annotations

import time
from collections import OrderedDict

from ..core.errors import NotFoundError
from .perception import Observation

#: How many observations are retained per run. Small on purpose: the agent
#: re-observes rather than hoarding screenshots.
DEFAULT_MAX_PER_RUN = 8


class ObservationStore:
    """Keeps the most recent observations per run, in memory only."""

    def __init__(
        self, *, max_per_run: int = DEFAULT_MAX_PER_RUN, ttl_seconds: float = 120.0
    ) -> None:
        self._max = max_per_run
        self._ttl = ttl_seconds
        self._by_run: dict[str, OrderedDict[str, Observation]] = {}

    def put(self, observation: Observation) -> None:
        bucket = self._by_run.setdefault(observation.run_id, OrderedDict())
        bucket[observation.id] = observation
        bucket.move_to_end(observation.id)
        while len(bucket) > self._max:
            bucket.popitem(last=False)

    def get(self, observation_id: str, *, run_id: str) -> Observation:
        bucket = self._by_run.get(run_id)
        observation = bucket.get(observation_id) if bucket else None
        if observation is None:
            raise NotFoundError(
                "no such observation for this run",
                details={"observation_id": observation_id},
            )
        return observation

    def latest(self, *, run_id: str) -> Observation | None:
        bucket = self._by_run.get(run_id)
        if not bucket:
            return None
        return next(reversed(bucket.values()))

    def evict_expired(self, *, now: float | None = None) -> int:
        """Drop observations older than the TTL; return how many were dropped."""
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


__all__ = ["DEFAULT_MAX_PER_RUN", "ObservationStore"]
