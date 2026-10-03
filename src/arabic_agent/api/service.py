"""Run service: the API-facing orchestration of durable runs.

The HTTP layer stays thin; this service owns the rules that must hold whether
execution is inline (local runtime) or queued (cloud runtime):

* a run is always persisted *before* any execution is attempted,
* idempotency keys make run creation safe to retry,
* the workspace is fixed at creation time so a later worker uses the same path,
* approval decisions transition run state and then hand off to the executor or
  the queue.
"""

from __future__ import annotations

from pathlib import Path

from ..audit.base import AuditEventType
from ..container import Platform
from ..core.config import Settings
from ..core.enums import ApprovalStatus, RunStatus
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..permissions.base import Principal
from ..runs.base import RunRecord
from ..worker.queue import Job, JobType
from ..worker.runtime import RunExecutor


class RunService:
    """Creates, reads and advances durable runs."""

    def __init__(self, platform: Platform, *, settings: Settings | None = None) -> None:
        self._platform = platform
        self._settings = settings or platform.settings
        self._metrics = platform.metrics
        self._executor = RunExecutor(platform=platform, runs=platform.runs, settings=self._settings)

    async def start(
        self,
        *,
        principal: Principal,
        goal: str,
        workspace_dir: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunRecord:
        """Persist a new run and either execute it inline or enqueue a job."""
        if idempotency_key:
            existing = await self._platform.runs.get_by_idempotency_key(
                tenant_id=principal.tenant_id, key=idempotency_key
            )
            if existing is not None:
                return existing

        workspace = self._resolve_workspace(workspace_dir)
        record = RunRecord(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            goal=goal,
            workspace_dir=workspace,
            idempotency_key=idempotency_key,
            roles=tuple(sorted(principal.roles)),
            granted_permissions=(
                tuple(sorted(p.value for p in principal.granted_permissions))
                if principal.granted_permissions is not None
                else None
            ),
        )
        try:
            record = await self._platform.runs.create(record)
        except ConflictError:
            # Lost an idempotency race: return the winner.
            existing = await self._platform.runs.get_by_idempotency_key(
                tenant_id=principal.tenant_id, key=idempotency_key or ""
            )
            if existing is not None:
                return existing
            raise

        self._metrics.run_started(mode=self._settings.execution_mode)
        if self._settings.execution_mode == "queued":
            job = Job(
                type=JobType.START_RUN,
                run_id=record.id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
            )
            await self._platform.queue.enqueue(job)
            self._metrics.job_enqueued(job_type=job.type.value)
            await self._platform.audit.record(
                AuditEventType.JOB_ENQUEUED,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                run_id=record.id,
                message="run enqueued",
                data={"job_id": job.id},
            )
            return record

        # Inline: execute now and return the resulting record.
        await self._executor.execute_run(record.id, tenant_id=record.tenant_id)
        return await self._platform.runs.get(record.id, tenant_id=record.tenant_id)

    async def get(self, run_id: str, *, tenant_id: str) -> RunRecord:
        return await self._platform.runs.get(run_id, tenant_id=tenant_id)

    async def list(
        self,
        *,
        tenant_id: str,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        return await self._platform.runs.list_runs(
            tenant_id=tenant_id, status=status, limit=limit, offset=offset
        )

    async def decide_approval(
        self,
        *,
        principal: Principal,
        approval_id: str,
        approve: bool,
        reason: str | None = None,
    ) -> RunRecord:
        """Resolve an approval and advance the run accordingly."""
        approval = await self._platform.approvals.get(approval_id, tenant_id=principal.tenant_id)
        if not approval.is_pending:
            raise NotFoundError(
                "approval already resolved",
                details={"approval_id": approval_id, "status": approval.status.value},
            )
        if approval.is_expired():
            await self._platform.approvals.resolve(
                approval_id,
                tenant_id=principal.tenant_id,
                status=ApprovalStatus.EXPIRED,
                resolved_by=principal.user_id,
                reason="expired",
            )
            raise ValidationError("approval has expired", details={"approval_id": approval_id})

        new_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        resolved = await self._platform.approvals.resolve(
            approval_id,
            tenant_id=principal.tenant_id,
            status=new_status,
            resolved_by=principal.user_id,
            reason=reason,
        )
        await self._platform.audit.record(
            AuditEventType.APPROVAL_RESOLVED,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=resolved.run_id,
            message=f"approval {resolved.status.value}",
            data={"approval_id": approval_id, "approved": approve},
        )
        wait_seconds = (
            max(0.0, (resolved.resolved_at - approval.created_at).total_seconds())
            if resolved.resolved_at is not None
            else None
        )
        self._metrics.approval_resolved(
            decision="approved" if approve else "denied", wait_seconds=wait_seconds
        )

        record = await self._platform.runs.get(resolved.run_id, tenant_id=principal.tenant_id)
        if not approve:
            return await self._cancel(record, reason="approval denied")

        if self._settings.execution_mode == "queued":
            job = Job(
                type=JobType.RESUME_RUN,
                run_id=record.id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                payload={"approval_id": approval_id},
            )
            await self._platform.queue.enqueue(job)
            self._metrics.job_enqueued(job_type=job.type.value)
            return record

        await self._executor.execute_resume(
            record.id, tenant_id=record.tenant_id, approval_id=approval_id
        )
        return await self._platform.runs.get(record.id, tenant_id=record.tenant_id)

    async def _cancel(self, record: RunRecord, *, reason: str) -> RunRecord:
        if record.is_terminal:
            return record
        updated = record.transition(RunStatus.CANCELLED, error=reason, lease_expires_at=None)
        await self._platform.audit.record(
            AuditEventType.RUN_CANCELLED,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            run_id=record.id,
            message=reason,
        )
        return await self._platform.runs.update(updated, expected_version=record.version)

    def _resolve_workspace(self, workspace_dir: str | None) -> str:
        if workspace_dir is not None:
            if not workspace_dir.strip():
                raise ValidationError("workspace_dir must not be blank")
            base = Path(workspace_dir)
        else:
            from ..core.ids import PREFIX_RUN, new_id

            base = Path(self._settings.local_store_dir) / "workspaces" / new_id(PREFIX_RUN)
        base.mkdir(parents=True, exist_ok=True)
        return str(base.resolve())
