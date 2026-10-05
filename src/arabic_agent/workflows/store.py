"""Workflow persistence contracts and in-memory implementations.

Three stores, all tenant-scoped and all using optimistic concurrency on update:

* :class:`WorkflowStore` — definition envelopes.
* :class:`WorkflowVersionStore` — immutable published versions.
* :class:`WorkflowRunStore` — runs and their step runs.

The in-memory implementations mirror the Postgres semantics exactly (including
optimistic-concurrency conflicts), so the engine behaves identically in the
local/offline runtime and in the cloud runtime.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from ..core.enums import StepRunStatus, WorkflowRunStatus
from ..core.errors import ConflictError, NotFoundError
from .models import Workflow, WorkflowVersion
from .run import StepRun, WorkflowRun


class WorkflowStore(ABC):
    """Persistence for workflow definition envelopes."""

    @abstractmethod
    async def create(self, workflow: Workflow) -> Workflow: ...

    @abstractmethod
    async def get(self, workflow_id: str, *, tenant_id: str) -> Workflow: ...

    @abstractmethod
    async def update(self, workflow: Workflow, *, expected_version: int) -> Workflow: ...

    @abstractmethod
    async def list_workflows(
        self, *, tenant_id: str, limit: int = 50, offset: int = 0
    ) -> list[Workflow]: ...


class WorkflowVersionStore(ABC):
    """Persistence for immutable workflow versions."""

    @abstractmethod
    async def create(self, version: WorkflowVersion) -> WorkflowVersion: ...

    @abstractmethod
    async def get(self, version_id: str, *, tenant_id: str) -> WorkflowVersion: ...

    @abstractmethod
    async def latest(self, *, tenant_id: str, workflow_id: str) -> WorkflowVersion | None: ...

    @abstractmethod
    async def list_versions(self, *, tenant_id: str, workflow_id: str) -> list[WorkflowVersion]: ...


class WorkflowRunStore(ABC):
    """Persistence for workflow runs and their step runs."""

    @abstractmethod
    async def create_run(self, run: WorkflowRun, steps: list[StepRun]) -> WorkflowRun: ...

    @abstractmethod
    async def get_run(self, run_id: str, *, tenant_id: str) -> WorkflowRun: ...

    @abstractmethod
    async def get_run_by_idempotency_key(
        self, *, tenant_id: str, key: str
    ) -> WorkflowRun | None: ...

    @abstractmethod
    async def update_run(self, run: WorkflowRun, *, expected_version: int) -> WorkflowRun: ...

    @abstractmethod
    async def list_runs(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None = None,
        status: WorkflowRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkflowRun]: ...

    @abstractmethod
    async def list_stale_runs(self, *, now: datetime, limit: int = 100) -> list[WorkflowRun]:
        """Runs whose worker lease expired and that are still recoverable."""

    @abstractmethod
    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None: ...

    @abstractmethod
    async def reset_running_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        """Reset non-terminal in-flight steps so a recovered run can re-drive them.

        Only steps in ``RUNNING`` or ``RETRYING`` are reset (to ``PENDING``);
        terminal steps are never touched. This is an explicit recovery primitive,
        deliberately separate from :data:`STEP_RUN_TRANSITIONS` so normal
        execution can never move a step backwards. It is safe because recovery
        only runs for a run whose lease has expired, and any side-effecting tool
        call is still guarded by the executor's idempotency key.
        """

    # --- Step runs ------------------------------------------------------
    @abstractmethod
    async def list_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]: ...

    @abstractmethod
    async def get_step(self, step_run_id: str, *, tenant_id: str) -> StepRun: ...

    @abstractmethod
    async def update_step(self, step: StepRun, *, expected_version: int) -> StepRun: ...


class InMemoryWorkflowStore(WorkflowStore):
    """Process-local workflow store for the local runtime and tests."""

    def __init__(self) -> None:
        self._by_id: dict[str, Workflow] = {}
        self._lock = asyncio.Lock()

    async def create(self, workflow: Workflow) -> Workflow:
        async with self._lock:
            self._by_id[workflow.id] = workflow
        return workflow

    async def get(self, workflow_id: str, *, tenant_id: str) -> Workflow:
        async with self._lock:
            workflow = self._by_id.get(workflow_id)
        if workflow is None or workflow.tenant_id != tenant_id:
            raise NotFoundError("workflow not found", details={"workflow_id": workflow_id})
        return workflow

    async def update(self, workflow: Workflow, *, expected_version: int) -> Workflow:
        async with self._lock:
            current = self._by_id.get(workflow.id)
            if current is None or current.tenant_id != workflow.tenant_id:
                raise NotFoundError("workflow not found", details={"workflow_id": workflow.id})
            if current.version != expected_version:
                raise ConflictError(
                    "workflow was modified concurrently",
                    details={"workflow_id": workflow.id, "expected": expected_version},
                )
            updated = workflow.model_copy(update={"version": expected_version + 1})
            self._by_id[workflow.id] = updated
        return updated

    async def list_workflows(
        self, *, tenant_id: str, limit: int = 50, offset: int = 0
    ) -> list[Workflow]:
        async with self._lock:
            items = [w for w in self._by_id.values() if w.tenant_id == tenant_id]
        items.sort(key=lambda w: w.created_at, reverse=True)
        return items[offset : offset + limit]


class InMemoryWorkflowVersionStore(WorkflowVersionStore):
    """Process-local version store. Versions are immutable, so no update."""

    def __init__(self) -> None:
        self._by_id: dict[str, WorkflowVersion] = {}
        self._lock = asyncio.Lock()

    async def create(self, version: WorkflowVersion) -> WorkflowVersion:
        async with self._lock:
            self._by_id[version.id] = version
        return version

    async def get(self, version_id: str, *, tenant_id: str) -> WorkflowVersion:
        async with self._lock:
            version = self._by_id.get(version_id)
        if version is None or version.tenant_id != tenant_id:
            raise NotFoundError("workflow version not found", details={"version_id": version_id})
        return version

    async def latest(self, *, tenant_id: str, workflow_id: str) -> WorkflowVersion | None:
        versions = await self.list_versions(tenant_id=tenant_id, workflow_id=workflow_id)
        return versions[0] if versions else None

    async def list_versions(self, *, tenant_id: str, workflow_id: str) -> list[WorkflowVersion]:
        async with self._lock:
            items = [
                v
                for v in self._by_id.values()
                if v.tenant_id == tenant_id and v.workflow_id == workflow_id
            ]
        items.sort(key=lambda v: v.version, reverse=True)
        return items


class InMemoryWorkflowRunStore(WorkflowRunStore):
    """Process-local workflow run + step run store."""

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}
        self._steps: dict[str, StepRun] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, run: WorkflowRun, steps: list[StepRun]) -> WorkflowRun:
        async with self._lock:
            self._runs[run.id] = run
            for step in steps:
                self._steps[step.id] = step
        return run

    async def get_run(self, run_id: str, *, tenant_id: str) -> WorkflowRun:
        async with self._lock:
            run = self._runs.get(run_id)
        if run is None or run.tenant_id != tenant_id:
            raise NotFoundError("workflow run not found", details={"workflow_run_id": run_id})
        return run

    async def get_run_by_idempotency_key(self, *, tenant_id: str, key: str) -> WorkflowRun | None:
        async with self._lock:
            for run in self._runs.values():
                if run.tenant_id == tenant_id and run.idempotency_key == key:
                    return run
        return None

    async def update_run(self, run: WorkflowRun, *, expected_version: int) -> WorkflowRun:
        async with self._lock:
            current = self._runs.get(run.id)
            if current is None or current.tenant_id != run.tenant_id:
                raise NotFoundError("workflow run not found", details={"workflow_run_id": run.id})
            if current.version != expected_version:
                raise ConflictError(
                    "workflow run was modified concurrently",
                    details={"workflow_run_id": run.id, "expected": expected_version},
                )
            updated = run.model_copy(update={"version": expected_version + 1})
            self._runs[run.id] = updated
        return updated

    async def list_runs(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None = None,
        status: WorkflowRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkflowRun]:
        async with self._lock:
            items = [
                r
                for r in self._runs.values()
                if r.tenant_id == tenant_id
                and (workflow_id is None or r.workflow_id == workflow_id)
                and (status is None or r.status is status)
            ]
        items.sort(key=lambda r: r.created_at, reverse=True)
        return items[offset : offset + limit]

    async def list_stale_runs(self, *, now: datetime, limit: int = 100) -> list[WorkflowRun]:
        async with self._lock:
            items = [
                r
                for r in self._runs.values()
                if r.is_recoverable and (r.lease_expires_at is None or r.lease_expires_at <= now)
            ]
        items.sort(key=lambda r: r.updated_at)
        return items[:limit]

    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        async with self._lock:
            current = self._runs.get(run_id)
            if current is None or current.tenant_id != tenant_id:
                return
            self._runs[run_id] = current.model_copy(
                update={"worker_id": worker_id, "lease_expires_at": lease_expires_at}
            )

    async def reset_running_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        reset: list[StepRun] = []
        async with self._lock:
            for step_id, step in list(self._steps.items()):
                if (
                    step.tenant_id != tenant_id
                    or step.workflow_run_id != workflow_run_id
                    or step.status not in {StepRunStatus.RUNNING, StepRunStatus.RETRYING}
                ):
                    continue
                updated = step.model_copy(
                    update={
                        "status": StepRunStatus.PENDING,
                        "updated_at": datetime.now(UTC),
                        "finished_at": None,
                        "version": step.version + 1,
                    }
                )
                self._steps[step_id] = updated
                reset.append(updated)
        return reset

    async def list_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        async with self._lock:
            items = [
                s
                for s in self._steps.values()
                if s.tenant_id == tenant_id and s.workflow_run_id == workflow_run_id
            ]
        items.sort(key=lambda s: s.index)
        return items

    async def get_step(self, step_run_id: str, *, tenant_id: str) -> StepRun:
        async with self._lock:
            step = self._steps.get(step_run_id)
        if step is None or step.tenant_id != tenant_id:
            raise NotFoundError("step run not found", details={"step_run_id": step_run_id})
        return step

    async def update_step(self, step: StepRun, *, expected_version: int) -> StepRun:
        async with self._lock:
            current = self._steps.get(step.id)
            if current is None or current.tenant_id != step.tenant_id:
                raise NotFoundError("step run not found", details={"step_run_id": step.id})
            if current.version != expected_version:
                raise ConflictError(
                    "step run was modified concurrently",
                    details={"step_run_id": step.id, "expected": expected_version},
                )
            updated = step.model_copy(update={"version": expected_version + 1})
            self._steps[step.id] = updated
        return updated
