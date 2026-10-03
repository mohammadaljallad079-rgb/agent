"""Dependency-aware readiness probes.

Liveness and readiness are different questions. *Liveness* asks "is the process
alive?" — it must not fail because a downstream dependency is briefly slow, or
the orchestrator will restart a healthy process. *Readiness* asks "can this
process serve traffic right now?" — it must fail when a required dependency is
unreachable, so traffic is routed elsewhere until it recovers.

:class:`ReadinessService` runs a set of :class:`ReadinessProbe` implementations
and reports each one individually, distinguishing *critical* dependencies (a
failure means not ready) from *optional* ones (reported, but not fatal). Probe
details never include connection strings, credentials or exception internals —
only a short, safe status.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ProbeResult(BaseModel):
    """Outcome of a single dependency check."""

    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    critical: bool = True
    detail: str = ""
    duration_ms: float = 0.0


class ReadinessProbe(ABC):
    """Contract for one dependency check."""

    #: Stable, low-cardinality name used in logs and metrics.
    name: str
    #: When True, a failure makes the whole platform not ready.
    critical: bool = True

    @abstractmethod
    async def check(self) -> ProbeResult:
        """Return the current status of this dependency."""

    def _result(self, ok: bool, detail: str = "", *, started: float) -> ProbeResult:
        return ProbeResult(
            name=self.name,
            ok=ok,
            critical=self.critical,
            detail=detail,
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )


class ReadinessReport(BaseModel):
    """Aggregate readiness across all probes."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    results: tuple[ProbeResult, ...] = Field(default_factory=tuple)

    @property
    def failed_critical(self) -> tuple[ProbeResult, ...]:
        return tuple(r for r in self.results if r.critical and not r.ok)


class ReadinessService:
    """Runs probes and aggregates their results."""

    def __init__(self, probes: list[ReadinessProbe], *, timeout_seconds: float = 2.0) -> None:
        self._probes = probes
        self._timeout = timeout_seconds

    @property
    def probes(self) -> tuple[ReadinessProbe, ...]:
        return tuple(self._probes)

    async def check(self) -> ReadinessReport:
        results = await asyncio.gather(*(self._run(probe) for probe in self._probes))
        ready = all(r.ok for r in results if r.critical)
        return ReadinessReport(ready=ready, results=tuple(results))

    async def _run(self, probe: ReadinessProbe) -> ProbeResult:
        started = perf_counter()
        try:
            return await asyncio.wait_for(probe.check(), timeout=self._timeout)
        except TimeoutError:
            return probe._result(False, "timed out", started=started)
        except Exception:
            # Log the detail for operators; never return it over HTTP.
            logger.warning("readiness probe %s failed", probe.name, exc_info=True)
            return probe._result(False, "unreachable", started=started)


class DatabaseProbe(ReadinessProbe):
    """Checks that the Postgres engine can execute a trivial query."""

    name = "database"
    critical = True

    def __init__(self, database: object) -> None:
        self._database = database

    async def check(self) -> ProbeResult:
        from sqlalchemy import text

        started = perf_counter()
        try:
            async with self._database.transaction() as session:  # type: ignore[attr-defined]
                await session.execute(text("SELECT 1"))
        except Exception:
            logger.warning("database readiness check failed", exc_info=True)
            return self._result(False, "query failed", started=started)
        return self._result(True, "ok", started=started)


class RedisProbe(ReadinessProbe):
    """Checks that the Redis server responds to PING."""

    name = "redis"
    critical = True

    def __init__(self, client: object) -> None:
        self._client = client

    async def check(self) -> ProbeResult:
        started = perf_counter()
        try:
            await self._client.ping()  # type: ignore[attr-defined]
        except Exception:
            logger.warning("redis readiness check failed", exc_info=True)
            return self._result(False, "ping failed", started=started)
        return self._result(True, "ok", started=started)


class ModelProbe(ReadinessProbe):
    """Reports whether a default model is routable.

    This is a *configuration* check, not a network call: readiness must not
    depend on an external LLM being up, or a transient provider outage would
    take the whole platform out of rotation.
    """

    name = "model"
    critical = True

    def __init__(self, gateway: object) -> None:
        self._gateway = gateway

    async def check(self) -> ProbeResult:
        started = perf_counter()
        model = getattr(self._gateway, "default_model", "")
        if not model:
            return self._result(False, "no default model configured", started=started)
        try:
            self._gateway._registry.resolve(model)  # type: ignore[attr-defined]
        except Exception:
            return self._result(False, "default model is not routable", started=started)
        return self._result(True, model, started=started)


class AuditProbe(ReadinessProbe):
    """Reports whether at least one audit sink is attached.

    Optional: losing the audit trail is a serious degradation but the platform
    can still serve reads, so it does not gate readiness.
    """

    name = "audit"
    critical = False

    def __init__(self, audit: object) -> None:
        self._audit = audit

    async def check(self) -> ProbeResult:
        started = perf_counter()
        sinks = getattr(self._audit, "_sinks", [])
        if not sinks:
            return self._result(False, "no audit sinks configured", started=started)
        return self._result(True, f"{len(sinks)} sink(s)", started=started)
