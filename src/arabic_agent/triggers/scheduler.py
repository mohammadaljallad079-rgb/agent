"""The trigger scheduler: turns due cron triggers into workflow runs.

The scheduler is a loop, not a timer per trigger. On each pass it asks the store
for cron triggers that are due and unclaimed, and for each one it:

1. computes the missed window from the durable ``next_fire_at``,
2. applies the trigger's misfire policy to decide which occurrences to run,
3. **atomically claims** the trigger (advancing ``next_fire_at`` and taking a
   lease in one conditional update), and
4. fires each claimed occurrence through :class:`TriggerService`.

Because the claim is a single conditional update, running several schedulers is
safe: two schedulers racing the same occurrence cannot both advance the row, so
exactly one fires it. And because ``next_fire_at`` is durable, a scheduler that
restarts picks up exactly where it left off — the misfire policy then decides
what to do about any occurrences that elapsed while it was down.

The scheduler does not execute work. It only starts durable workflow runs, which
the existing worker executes. That separation is what lets the scheduler be a
small, restartable loop rather than another execution engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..core.config import Settings
from ..core.enums import MisfirePolicy
from ..core.errors import ConflictError, PlatformError, ValidationError
from ..core.ids import new_id
from ..observability.context import correlation_scope
from .cron import CronExpression, load_timezone, validate_cron
from .models import Trigger
from .service import TriggerService
from .store import TriggerStore

if TYPE_CHECKING:
    from ..container import Platform

logger = logging.getLogger(__name__)


class TriggerScheduler:
    """Claims due cron triggers and starts their workflow runs."""

    def __init__(
        self,
        *,
        platform: Platform,
        triggers: TriggerStore,
        service: TriggerService,
        settings: Settings,
        owner: str | None = None,
    ) -> None:
        self._platform = platform
        self._triggers = triggers
        self._service = service
        self._settings = settings
        self._metrics = platform.metrics
        self._owner = owner or settings.worker_id or f"scheduler-{new_id('sch')}"
        self._stop = asyncio.Event()

    @property
    def owner(self) -> str:
        return self._owner

    async def run_once(self) -> int:
        """One scheduling pass. Returns how many occurrences were fired."""
        now = datetime.now(UTC)
        due = await self._triggers.list_due(now=now, limit=100)
        fired = 0
        for trigger in due:
            try:
                fired += await self._process(trigger, now=now)
            except PlatformError:
                logger.warning("scheduler could not process trigger %s", trigger.id, exc_info=True)
            except Exception:
                logger.exception("scheduler error on trigger %s", trigger.id)
        return fired

    async def _process(self, trigger: Trigger, *, now: datetime) -> int:
        if trigger.cron is None or trigger.next_fire_at is None:
            return 0
        try:
            expression = validate_cron(trigger.cron)
            tz = load_timezone(trigger.timezone)
        except ValidationError as exc:
            logger.warning("trigger %s has an invalid schedule: %s", trigger.id, exc)
            return 0

        occurrence = trigger.next_fire_at
        late_by = (now - occurrence).total_seconds()
        on_time = late_by <= self._settings.scheduler_misfire_grace_seconds

        if on_time:
            occurrences = [occurrence]
        elif trigger.misfire_policy is MisfirePolicy.SKIP:
            self._metrics.trigger_skipped(kind=trigger.kind.value, reason="misfire_skip")
            occurrences = []
        elif trigger.misfire_policy is MisfirePolicy.RUN_ONE:
            self._metrics.trigger_skipped(kind=trigger.kind.value, reason="misfire_run_one")
            occurrences = [occurrence]
        else:  # RUN_ALL
            occurrences = self._catch_up(expression, tz, occurrence, now)
            if len(occurrences) >= self._settings.scheduler_max_catchup:
                self._metrics.trigger_skipped(kind=trigger.kind.value, reason="catchup_capped")

        new_next = expression.next_after(now, tz=tz)
        claimed = await self._triggers.claim_due(
            trigger.id,
            now=now,
            next_fire_at=new_next,
            owner=self._owner,
            lease_expires_at=now + timedelta(seconds=self._settings.scheduler_lease_seconds),
        )
        if claimed is None:
            # Another scheduler claimed this occurrence, or the trigger changed.
            return 0

        fired = 0
        for scheduled_for in occurrences:
            with correlation_scope(
                tenant_id=claimed.tenant_id,
                user_id=claimed.user_id,
                trigger_id=claimed.id,
                scheduler_id=self._owner,
            ):
                try:
                    run = await self._service.fire_cron(claimed, scheduled_for=scheduled_for)
                except (ConflictError, PlatformError) as exc:
                    logger.warning("trigger %s failed to fire: %s", claimed.id, exc)
                    self._metrics.trigger_fired(kind=claimed.kind.value, outcome="failed")
                    continue
            if run is not None:
                fired += 1
        return fired

    def _catch_up(
        self,
        expression: CronExpression,
        tz: ZoneInfo,
        first: datetime,
        now: datetime,
    ) -> list[datetime]:
        """Occurrences from ``first`` through ``now``, bounded by max_catchup."""
        cap = self._settings.scheduler_max_catchup
        occurrences: list[datetime] = []
        cursor: datetime | None = first
        while cursor is not None and cursor <= now and len(occurrences) < cap:
            occurrences.append(cursor)
            cursor = expression.next_after(cursor, tz=tz)
        return occurrences

    async def run_forever(self) -> None:
        """Run scheduling passes until :meth:`stop` is called."""
        logger.info("trigger scheduler %s started", self._owner)
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.warning("scheduler pass failed", exc_info=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._settings.scheduler_interval_seconds
                    )
        finally:
            logger.info("trigger scheduler %s stopped", self._owner)

    def stop(self) -> None:
        self._stop.set()
