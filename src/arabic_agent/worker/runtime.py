"""The worker runtime: durable, recoverable agent execution.

The API never runs an agent inline in production. It persists a run and enqueues
a job; a worker process claims the job and executes the run through
:class:`RunExecutor`. The executor is where recovery becomes real:

* it takes a lease on the run (a heartbeat keeps it fresh),
* it re-reads durable state instead of trusting process memory,
* it rebuilds the authorising principal from the persisted run,
* it only transitions through valid states, using optimistic concurrency,
* and it records every outcome back to Postgres.

If the worker dies mid-run, the lease expires and :class:`Worker` reclaims the
run — no task is lost and no completed work is redone (completed steps are
terminal in the durable plan).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..agent.base import AgentRunResult
from ..audit.base import AuditEventType, AuditLog
from ..core.config import Settings
from ..core.enums import RunStatus
from ..core.errors import ConflictError, PlatformError
from ..core.ids import new_id
from ..permissions.base import Permission, Principal
from ..runs.base import RunRecord
from ..runs.store import RunStore
from .queue import Job, JobQueue, JobType

if TYPE_CHECKING:
    from ..container import Platform

logger = logging.getLogger(__name__)

#: A run in one of these states is already finished; a worker must not touch it.
_FINISHED = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)


def principal_from_run(record: RunRecord) -> Principal:
    """Rebuild the authorising principal from a persisted run.

    Roles and the permission ceiling are stored on the run (they are not
    secrets), so a worker can re-evaluate policy at execution time without
    holding the caller's API key.
    """
    granted = (
        frozenset(Permission(p) for p in record.granted_permissions)
        if record.granted_permissions is not None
        else None
    )
    return Principal(
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        roles=frozenset(record.roles),
        granted_permissions=granted,
    )


class RunExecutor:
    """Executes a persisted run with a lease, heartbeat and state persistence."""

    def __init__(
        self,
        *,
        platform: Platform,
        runs: RunStore,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self._platform = platform
        self._runs = runs
        self._settings = settings
        self._worker_id = worker_id or settings.worker_id or f"worker-{new_id('wkr')}"

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def execute_run(
        self,
        run_id: str,
        *,
        tenant_id: str,
        recovered: bool = False,
    ) -> AgentRunResult | None:
        """Load, lease and execute a run; persist its terminal/paused state."""
        record = await self._runs.get(run_id, tenant_id=tenant_id)
        if record.status in _FINISHED or record.status is RunStatus.WAITING_APPROVAL:
            # Idempotent no-op: a redelivered job for a finished run does
            # nothing, and a run paused for approval must not be executed here.
            return None

        async def call(leased: RunRecord) -> AgentRunResult:
            if recovered:
                await self._platform.audit.record(
                    AuditEventType.RUN_RETRIED,
                    tenant_id=leased.tenant_id,
                    user_id=leased.user_id,
                    run_id=run_id,
                    message="recovering run after worker loss",
                    data={"worker_id": self._worker_id},
                )
            return await self._run_agent(leased, recovered=recovered)

        return await self._execute_leased(record, call)

    async def execute_resume(
        self,
        run_id: str,
        *,
        tenant_id: str,
        approval_id: str,
    ) -> AgentRunResult | None:
        """Resume a run whose approval was granted (possibly after a restart)."""
        record = await self._runs.get(run_id, tenant_id=tenant_id)
        if record.status in _FINISHED:
            return None
        approval = await self._platform.approvals.get(approval_id, tenant_id=tenant_id)

        async def call(leased: RunRecord) -> AgentRunResult:
            runner = self._platform.build_runner()
            return await runner.resume_after_approval(
                principal=principal_from_run(leased),
                approval=approval,
                run_id=run_id,
                workspace_dir=leased.workspace_dir,
            )

        return await self._execute_leased(record, call)

    async def _execute_leased(
        self,
        record: RunRecord,
        call: Callable[[RunRecord], Awaitable[AgentRunResult]],
    ) -> AgentRunResult | None:
        # Claim the run: RUNNING with a lease. If the transition is not valid
        # from the current state, another worker already advanced it.
        try:
            claimed = record.transition(
                RunStatus.RUNNING,
                worker_id=self._worker_id,
                lease_expires_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.worker_lease_seconds),
            )
            leased = await self._runs.update(claimed, expected_version=record.version)
        except ConflictError:
            logger.info("run %s was claimed concurrently; skipping", record.id)
            return None

        heartbeat = asyncio.create_task(self._heartbeat(leased))
        try:
            result = await call(leased)
        except PlatformError as exc:
            result = AgentRunResult(
                run_id=leased.id,
                status=RunStatus.FAILED,
                error=exc.message,
                workspace_dir=leased.workspace_dir,
            )
        except Exception as exc:
            logger.exception("run %s failed unexpectedly", leased.id)
            result = AgentRunResult(
                run_id=leased.id,
                status=RunStatus.FAILED,
                error=f"internal error: {exc}",
                workspace_dir=leased.workspace_dir,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        await self._persist_outcome(leased, result)
        return result

    async def _run_agent(self, record: RunRecord, *, recovered: bool) -> AgentRunResult:
        principal = principal_from_run(record)
        runner = self._platform.build_runner()
        return await runner.run(
            principal=principal,
            goal=record.goal,
            workspace_dir=record.workspace_dir,
            run_id=record.id,
            resume=True,
            recovery_attempt=record.recovery_attempts if recovered else None,
        )

    async def _persist_outcome(self, record: RunRecord, result: AgentRunResult) -> None:
        """Write the run's new state, retrying once on an optimistic conflict."""
        target = result.status
        for attempt in range(2):
            try:
                current = await self._runs.get(record.id, tenant_id=record.tenant_id)
                if current.status in _FINISHED:
                    return
                updates = {
                    "result": result.result,
                    "verified": result.verified,
                    "error": result.error,
                    "pending_approval_id": result.pending_approval_id,
                    "steps_executed": result.steps_executed,
                    "tool_calls": result.tool_calls,
                    "retries": result.retries,
                    "replans": result.replans,
                    "usage": {
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                        "total_tokens": result.usage.total_tokens,
                    },
                    "lease_expires_at": None,
                }
                updated = current.transition(target, **updates)
                await self._runs.update(updated, expected_version=current.version)
                return
            except ConflictError:
                logger.info("conflict persisting run %s (attempt %d)", record.id, attempt + 1)
                continue
        logger.error("could not persist outcome for run %s after retries", record.id)

    async def _heartbeat(self, record: RunRecord) -> None:
        interval = self._settings.worker_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self._runs.heartbeat(
                    record.id,
                    tenant_id=record.tenant_id,
                    worker_id=self._worker_id,
                    lease_expires_at=datetime.now(UTC)
                    + timedelta(seconds=self._settings.worker_lease_seconds),
                )
            except Exception:
                logger.warning("heartbeat failed for run %s", record.id, exc_info=True)


class Worker:
    """Consumes jobs and executes runs, plus a background recovery loop.

    Recovery distinguishes states rather than blindly restarting everything:
    only runs whose lease has expired and that are still in a non-terminal,
    non-approval state are re-enqueued. A run waiting for approval, a completed
    run and a permanently failed run are all left alone.
    """

    def __init__(
        self,
        *,
        platform: Platform,
        runs: RunStore,
        queue: JobQueue,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self._platform = platform
        self._runs = runs
        self._queue = queue
        self._settings = settings
        self._executor = RunExecutor(
            platform=platform, runs=runs, settings=settings, worker_id=worker_id
        )
        self._audit: AuditLog = platform.audit
        self._stop = asyncio.Event()

    @property
    def worker_id(self) -> str:
        return self._executor.worker_id

    async def process_one(self) -> bool:
        """Claim and process a single job. Returns whether a job was processed."""
        claimed = await self._queue.claim(timeout_seconds=1.0)
        if claimed is None:
            return False
        job = claimed.job
        await self._audit.record(
            AuditEventType.JOB_CLAIMED,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            run_id=job.run_id,
            message=f"claimed job {job.type.value}",
            data={"job_id": job.id, "attempt": job.attempts, "worker_id": self.worker_id},
        )
        try:
            await self._dispatch_job(job)
        except Exception as exc:
            logger.exception("job %s failed", job.id)
            await self._audit.record(
                AuditEventType.JOB_FAILED,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                run_id=job.run_id,
                message=f"job failed: {exc}",
                data={"job_id": job.id, "attempt": job.attempts},
            )
            requeue = job.attempts < self._settings.job_max_attempts
            await self._queue.nack(claimed, requeue=requeue)
            if not requeue:
                await self._mark_failed(job, f"job exceeded max attempts: {exc}")
            return True
        await self._audit.record(
            AuditEventType.JOB_COMPLETED,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            run_id=job.run_id,
            message=f"completed job {job.type.value}",
            data={"job_id": job.id},
        )
        await self._queue.ack(claimed)
        return True

    async def _dispatch_job(self, job: Job) -> None:
        if job.type in (JobType.START_RUN, JobType.RESUME_RUN):
            await self._executor.execute_run(job.run_id, tenant_id=job.tenant_id)
        elif job.type is JobType.RECOVER_RUN:
            await self._executor.execute_run(job.run_id, tenant_id=job.tenant_id, recovered=True)

    async def _mark_failed(self, job: Job, message: str) -> None:
        try:
            record = await self._runs.get(job.run_id, tenant_id=job.tenant_id)
            if record.status in _FINISHED:
                return
            updated = record.transition(RunStatus.FAILED, error=message, lease_expires_at=None)
            await self._runs.update(updated, expected_version=record.version)
        except (ConflictError, PlatformError):
            logger.warning("could not mark run %s failed", job.run_id, exc_info=True)

    async def recover_stale(self) -> int:
        """Re-enqueue runs whose worker lease has expired. Returns how many."""
        now = datetime.now(UTC)
        stale = await self._runs.list_stale(now=now, limit=100)
        count = 0
        for record in stale:
            if record.recovery_attempts >= self._settings.job_max_attempts:
                # Too many recovery attempts: fail it rather than loop forever.
                with contextlib.suppress(ConflictError, PlatformError):
                    failed = record.transition(
                        RunStatus.FAILED,
                        error="exceeded recovery attempts",
                        lease_expires_at=None,
                    )
                    await self._runs.update(failed, expected_version=record.version)
                continue
            bumped = record.model_copy(
                update={
                    "recovery_attempts": record.recovery_attempts + 1,
                    "lease_expires_at": now
                    + timedelta(seconds=self._settings.worker_lease_seconds),
                    "worker_id": self.worker_id,
                }
            )
            try:
                await self._runs.update(bumped, expected_version=record.version)
            except ConflictError:
                continue
            await self._queue.enqueue(
                Job(
                    type=JobType.RECOVER_RUN,
                    run_id=record.id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                )
            )
            count += 1
        if count:
            logger.info("recovered %d stale run(s)", count)
        return count

    async def run_forever(self) -> None:
        """Poll the queue and periodically reclaim stale runs until stopped."""
        recovery = asyncio.create_task(self._recovery_loop())
        logger.info("worker %s started", self.worker_id)
        try:
            while not self._stop.is_set():
                processed = await self.process_one()
                if not processed:
                    await asyncio.sleep(self._settings.worker_poll_seconds)
        finally:
            recovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovery
            logger.info("worker %s stopped", self.worker_id)

    async def _recovery_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._settings.recovery_interval_seconds)
            try:
                await self._queue.reclaim_stale(
                    older_than_seconds=self._settings.worker_lease_seconds
                )
                await self.recover_stale()
            except Exception:
                logger.warning("recovery sweep failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
