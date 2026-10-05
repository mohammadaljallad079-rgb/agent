"""Workflow service: the API-facing orchestration of versioned workflows.

Mirrors :class:`~arabic_agent.api.service.RunService` for workflows. It owns the
rules that must hold in both the inline (local) and queued (cloud) runtimes:

* authoring and publishing are policy-checked (``MANAGE_WORKFLOW``),
* starting a run is policy-checked (``RUN_WORKFLOW``) and idempotent,
* a run always pins the version it started with,
* a definition is validated before it is ever persisted,
* approval decisions advance a workflow run the same way they advance a run.

Every step's *tool* call still goes through the tool executor's own policy gate;
workflow permissions only govern authoring and starting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..audit.base import AuditEventType
from ..container import Platform
from ..core.config import Settings
from ..core.enums import (
    ApprovalStatus,
    PermissionEffect,
    RiskLevel,
    WorkflowRunStatus,
    WorkflowStatus,
)
from ..core.errors import (
    ApprovalRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..permissions.base import Permission, Principal
from ..worker.queue import Job, JobType
from ..workflows.models import Workflow, WorkflowStep, WorkflowVersion
from ..workflows.run import StepRun, WorkflowRun


class WorkflowService:
    """Creates, reads and advances versioned workflows and their runs."""

    def __init__(self, platform: Platform, *, settings: Settings | None = None) -> None:
        self._platform = platform
        self._settings = settings or platform.settings
        self._metrics = platform.metrics

    # ------------------------------------------------------------------
    # Authoring
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        principal: Principal,
        name: str,
        description: str = "",
        steps: list[WorkflowStep] | None = None,
        default_timeout_seconds: float = 600.0,
        run_timeout_seconds: float | None = None,
        publish: bool = False,
    ) -> tuple[Workflow, WorkflowVersion | None]:
        """Create a workflow; optionally publish its first version."""
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="workflow.create")
        workflow = Workflow(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            name=name,
            description=description,
        )
        try:
            workflow = await self._platform.workflows.create(workflow)
        except ConflictError:
            raise
        await self._audit(
            AuditEventType.WORKFLOW_CREATED,
            workflow,
            principal,
            message=f"workflow '{name}' created",
            data={"workflow_id": workflow.id},
        )

        version: WorkflowVersion | None = None
        if steps:
            version = await self._publish(
                principal,
                workflow,
                steps=steps,
                default_timeout_seconds=default_timeout_seconds,
                run_timeout_seconds=run_timeout_seconds,
                activate=publish,
            )
            workflow = await self._platform.workflows.get(
                workflow.id, tenant_id=principal.tenant_id
            )
        return workflow, version

    async def update(
        self,
        *,
        principal: Principal,
        workflow_id: str,
        name: str | None = None,
        description: str | None = None,
        steps: list[WorkflowStep] | None = None,
        default_timeout_seconds: float | None = None,
        run_timeout_seconds: float | None = None,
        publish: bool = True,
    ) -> tuple[Workflow, WorkflowVersion | None]:
        """Update a workflow, optionally publishing a new version."""
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="workflow.update")
        workflow = await self._platform.workflows.get(workflow_id, tenant_id=principal.tenant_id)
        if workflow.status is WorkflowStatus.ARCHIVED:
            raise ConflictError("archived workflows cannot be edited")
        updates: dict[str, object] = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if updates:
            workflow = await self._platform.workflows.update(
                workflow.model_copy(update=updates), expected_version=workflow.version
            )
            await self._audit(
                AuditEventType.WORKFLOW_UPDATED,
                workflow,
                principal,
                message=f"workflow '{workflow.name}' updated",
                data={"workflow_id": workflow.id},
            )

        version: WorkflowVersion | None = None
        if steps is not None:
            version = await self._publish(
                principal,
                workflow,
                steps=steps,
                default_timeout_seconds=(
                    default_timeout_seconds if default_timeout_seconds is not None else 600.0
                ),
                run_timeout_seconds=run_timeout_seconds,
                activate=publish,
            )
            workflow = await self._platform.workflows.get(
                workflow_id, tenant_id=principal.tenant_id
            )
        return workflow, version

    async def publish(
        self,
        *,
        principal: Principal,
        workflow_id: str,
        steps: list[WorkflowStep],
        default_timeout_seconds: float = 600.0,
        run_timeout_seconds: float | None = None,
    ) -> tuple[Workflow, WorkflowVersion]:
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="workflow.publish")
        workflow = await self._platform.workflows.get(workflow_id, tenant_id=principal.tenant_id)
        version = await self._publish(
            principal,
            workflow,
            steps=steps,
            default_timeout_seconds=default_timeout_seconds,
            run_timeout_seconds=run_timeout_seconds,
            activate=True,
        )
        assert version is not None
        workflow = await self._platform.workflows.get(workflow_id, tenant_id=principal.tenant_id)
        return workflow, version

    async def get(self, workflow_id: str, *, tenant_id: str) -> Workflow:
        return await self._platform.workflows.get(workflow_id, tenant_id=tenant_id)

    async def list_workflows(
        self, *, tenant_id: str, limit: int = 50, offset: int = 0
    ) -> list[Workflow]:
        return await self._platform.workflows.list_workflows(
            tenant_id=tenant_id, limit=limit, offset=offset
        )

    async def list_versions(self, *, tenant_id: str, workflow_id: str) -> list[WorkflowVersion]:
        return await self._platform.workflow_versions.list_versions(
            tenant_id=tenant_id, workflow_id=workflow_id
        )

    async def get_version(self, version_id: str, *, tenant_id: str) -> WorkflowVersion:
        return await self._platform.workflow_versions.get(version_id, tenant_id=tenant_id)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def start_run(
        self,
        *,
        principal: Principal,
        workflow_id: str,
        inputs: dict[str, Any] | None = None,
        workspace_dir: str | None = None,
        idempotency_key: str | None = None,
    ) -> WorkflowRun:
        """Persist a new workflow run and execute it inline or enqueue a job."""
        self._authorize(principal, Permission.RUN_WORKFLOW, tool_name="workflow.run")
        if idempotency_key:
            existing = await self._platform.workflow_runs.get_run_by_idempotency_key(
                tenant_id=principal.tenant_id, key=idempotency_key
            )
            if existing is not None:
                return existing

        workflow = await self._platform.workflows.get(workflow_id, tenant_id=principal.tenant_id)
        if not workflow.is_published or workflow.latest_version_id is None:
            raise ConflictError("workflow has no published version to run")
        version = await self._platform.workflow_versions.get(
            workflow.latest_version_id, tenant_id=principal.tenant_id
        )

        workspace = self._resolve_workspace(workspace_dir)
        run = WorkflowRun(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            workflow_version=version.version,
            input=dict(inputs or {}),
            workspace_dir=workspace,
            idempotency_key=idempotency_key,
            roles=tuple(sorted(principal.roles)),
            granted_permissions=(
                tuple(sorted(p.value for p in principal.granted_permissions))
                if principal.granted_permissions is not None
                else None
            ),
            run_timeout_seconds=version.run_timeout_seconds,
        )
        step_runs = [
            StepRun(
                tenant_id=principal.tenant_id,
                workflow_run_id=run.id,
                step_id=step.id,
                index=index,
                name=step.name,
                kind=step.kind,
                tool_name=step.tool,
            )
            for index, step in enumerate(version.steps)
        ]
        try:
            run = await self._platform.workflow_runs.create_run(run, step_runs)
        except ConflictError:
            existing = await self._platform.workflow_runs.get_run_by_idempotency_key(
                tenant_id=principal.tenant_id, key=idempotency_key or ""
            )
            if existing is not None:
                return existing
            raise

        self._metrics.workflow_run_started()
        if self._settings.execution_mode == "queued":
            job = Job(
                type=JobType.START_WORKFLOW,
                run_id=run.id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
            )
            await self._platform.queue.enqueue(job)
            self._metrics.job_enqueued(job_type=job.type.value)
            await self._platform.audit.record(
                AuditEventType.JOB_ENQUEUED,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.id,
                message="workflow run enqueued",
                data={"job_id": job.id},
            )
            return run

        await self._platform.workflow_engine.execute_run(run.id, tenant_id=run.tenant_id)
        return await self._platform.workflow_runs.get_run(run.id, tenant_id=run.tenant_id)

    async def get_run(self, run_id: str, *, tenant_id: str) -> WorkflowRun:
        return await self._platform.workflow_runs.get_run(run_id, tenant_id=tenant_id)

    async def list_runs(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None = None,
        status: WorkflowRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkflowRun]:
        return await self._platform.workflow_runs.list_runs(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def list_step_runs(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        return await self._platform.workflow_runs.list_steps(
            tenant_id=tenant_id, workflow_run_id=workflow_run_id
        )

    async def decide_approval(
        self,
        *,
        principal: Principal,
        approval_id: str,
        approve: bool,
        reason: str | None = None,
    ) -> WorkflowRun:
        """Resolve an approval and resume (or fail) the workflow run."""
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

        run = await self._platform.workflow_runs.get_run(
            resolved.run_id, tenant_id=principal.tenant_id
        )
        if self._settings.execution_mode == "queued":
            job = Job(
                type=JobType.RESUME_WORKFLOW,
                run_id=run.id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                payload={"approval_id": approval_id},
            )
            await self._platform.queue.enqueue(job)
            self._metrics.job_enqueued(job_type=job.type.value)
            return run

        await self._platform.workflow_engine.resume_run(
            run.id, tenant_id=run.tenant_id, approval_id=approval_id
        )
        return await self._platform.workflow_runs.get_run(run.id, tenant_id=run.tenant_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _publish(
        self,
        principal: Principal,
        workflow: Workflow,
        *,
        steps: list[WorkflowStep],
        default_timeout_seconds: float,
        run_timeout_seconds: float | None,
        activate: bool,
    ) -> WorkflowVersion:
        latest = await self._platform.workflow_versions.latest(
            tenant_id=principal.tenant_id, workflow_id=workflow.id
        )
        next_version = (latest.version + 1) if latest is not None else 1
        version = WorkflowVersion(
            workflow_id=workflow.id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            version=next_version,
            steps=tuple(steps),
            default_timeout_seconds=default_timeout_seconds,
            run_timeout_seconds=run_timeout_seconds,
        )
        version.validate_graph()
        version = await self._platform.workflow_versions.create(version)

        if activate:
            workflow = await self._platform.workflows.update(
                workflow.model_copy(
                    update={
                        "status": WorkflowStatus.PUBLISHED,
                        "latest_version_id": version.id,
                        "latest_version": version.version,
                    }
                ),
                expected_version=workflow.version,
            )
        await self._audit(
            AuditEventType.WORKFLOW_PUBLISHED,
            workflow,
            principal,
            message=f"workflow '{workflow.name}' v{version.version} published",
            data={"workflow_id": workflow.id, "version_id": version.id, "version": version.version},
        )
        return version

    def _authorize(self, principal: Principal, permission: Permission, *, tool_name: str) -> None:
        decision = self._platform.policy.evaluate(
            principal,
            tool_name=tool_name,
            permission=permission,
            risk=RiskLevel.MEDIUM,
        )
        if decision.effect is PermissionEffect.DENY:
            raise PermissionDeniedError(
                f"not permitted: {permission.value}",
                details={"permission": permission.value, "reason": decision.reason},
            )
        if decision.effect is PermissionEffect.REQUIRES_APPROVAL:
            raise ApprovalRequiredError(
                f"approval required for {permission.value}",
                details={"permission": permission.value, "reason": decision.reason},
            )

    async def _audit(
        self,
        event_type: AuditEventType,
        workflow: Workflow,
        principal: Principal,
        *,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._platform.audit.record(
            event_type,
            tenant_id=workflow.tenant_id,
            user_id=principal.user_id,
            run_id=workflow.id,
            message=message,
            data=data,
        )

    def _resolve_workspace(self, workspace_dir: str | None) -> str:
        if workspace_dir is not None:
            if not workspace_dir.strip():
                raise ValidationError("workspace_dir must not be blank")
            base = Path(workspace_dir)
        else:
            from ..core.ids import PREFIX_WORKFLOW_RUN, new_id

            base = Path(self._settings.local_store_dir) / "workspaces" / new_id(PREFIX_WORKFLOW_RUN)
        base.mkdir(parents=True, exist_ok=True)
        return str(base.resolve())
