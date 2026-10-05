"""The workflow engine: durable, recoverable execution of a workflow graph.

The engine executes a pinned :class:`WorkflowVersion` as a DAG. It never trusts
process memory: every step's state lives in :class:`StepRun` rows, and the
engine re-derives what is runnable from those rows on each pass. That is what
makes a workflow survive an API restart, a worker restart, a Redis restart, a
model failure, a tool failure or a long approval wait.

Execution model
---------------
1. Load the run and its step runs.
2. Find every step that is *ready*: its dependencies are all in a successful
   terminal state and it is not itself terminal.
3. Run all ready steps concurrently (bounded fan-out). Each step advances its
   own durable row through the enforced :data:`STEP_RUN_TRANSITIONS`.
4. If any step paused for approval, put the whole run in ``WAITING_APPROVAL``
   and stop — a worker will resume it when the decision arrives.
5. Repeat until no step is ready; then the run is ``COMPLETED`` (or ``FAILED``
   if a step failed under a ``fail_run`` policy).

Every external effect is reached through the *existing* runtime: agent steps go
through the agent runner, tool steps go through the policy-gated tool executor
(so permissions, approvals, sandboxing and idempotency are unchanged), and
deterministic steps go through a registry of pure, named actions — never
arbitrary code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..audit.base import AuditEventType
from ..core.config import Settings
from ..core.enums import (
    ApprovalStatus,
    ErrorClass,
    RunStatus,
    StepErrorPolicy,
    StepKind,
    StepRunStatus,
    WorkflowRunStatus,
)
from ..core.errors import ConflictError, NotFoundError, PlatformError
from ..core.ids import PREFIX_RUN, new_id
from ..models.base import Usage
from ..observability.context import correlation_scope
from ..observability.metrics import Metrics
from ..permissions.approvals import Approval
from ..permissions.base import Permission, Principal
from ..tools.base import ToolContext
from .actions import ActionRegistry
from .expressions import ExpressionError, evaluate_condition, render
from .models import MAX_PARALLELISM, WorkflowStep, WorkflowVersion
from .run import (
    SUCCESSFUL_STEP_RUN_STATUSES,
    StepRun,
    WorkflowRun,
)
from .store import WorkflowRunStore, WorkflowVersionStore

if TYPE_CHECKING:
    from ..container import Platform

logger = logging.getLogger(__name__)

#: Default timeout applied to a step that does not declare one.
DEFAULT_STEP_TIMEOUT_SECONDS = 600.0
#: Tool name recorded on workflow approval requests.
WORKFLOW_APPROVAL_TOOL = "workflow.approval"


class _StepResult:
    """Internal, non-durable result of a step attempt."""

    def __init__(
        self,
        *,
        outcome: str,
        summary: str = "",
        data: dict[str, Any] | None = None,
        error: str | None = None,
        error_class: ErrorClass | None = None,
        agent_run_id: str | None = None,
        approval_id: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        #: One of "completed", "failed", "paused".
        self.outcome = outcome
        self.summary = summary
        self.data = data or {}
        self.error = error
        self.error_class = error_class
        self.agent_run_id = agent_run_id
        self.approval_id = approval_id
        self.usage = usage or Usage()

    @property
    def retryable(self) -> bool:
        return self.outcome == "failed" and self.error_class in {
            ErrorClass.TRANSIENT,
            ErrorClass.TIMEOUT,
            ErrorClass.CONFLICT,
            ErrorClass.EXTERNAL,
        }


class WorkflowEngine:
    """Executes workflow runs against the real runtime, durably."""

    def __init__(
        self,
        *,
        platform: Platform | None,
        runs: WorkflowRunStore,
        versions: WorkflowVersionStore,
        actions: ActionRegistry,
    ) -> None:
        self._platform = platform
        self._runs = runs
        self._versions = versions
        self._actions = actions

    def bind(self, platform: Platform) -> None:
        """Attach the composed platform (used to break the construction cycle).

        The engine needs the platform to reuse the runner, tool executor, policy
        engine, approval store and audit log, but the platform also holds the
        engine. The container constructs the engine, builds the platform, then
        binds it here.
        """
        self._platform = platform

    @property
    def _p(self) -> Platform:
        if self._platform is None:  # pragma: no cover - construction invariant
            raise RuntimeError("workflow engine is not bound to a platform")
        return self._platform

    @property
    def _settings(self) -> Settings:
        return self._p.settings

    @property
    def _metrics(self) -> Metrics:
        return self._p.metrics

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    async def execute_run(
        self,
        run_id: str,
        *,
        tenant_id: str,
        recovered: bool = False,
    ) -> WorkflowRun | None:
        """Drive a run as far as it can go; return its resulting state.

        Safe to call repeatedly: a run that is already terminal, or waiting on
        an approval, is left untouched. This is what makes a redelivered job or
        a duplicate recovery sweep harmless.
        """
        run = await self._runs.get_run(run_id, tenant_id=tenant_id)
        if run.is_terminal or run.status is WorkflowRunStatus.WAITING_APPROVAL:
            return None
        version = await self._versions.get(run.workflow_version_id, tenant_id=tenant_id)

        with correlation_scope(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            workflow_id=run.workflow_id,
            workflow_run_id=run.id,
            worker_id=self._p.settings.worker_id or None,
        ):
            if recovered:
                await self._audit(
                    AuditEventType.WORKFLOW_RUN_STARTED,
                    run,
                    message="recovering workflow run after worker loss",
                    data={"recovered": True},
                )
                self._metrics.workflow_run_recovered()
            return await self._drive(run, version)

    async def resume_run(
        self,
        run_id: str,
        *,
        tenant_id: str,
        approval_id: str,
    ) -> WorkflowRun | None:
        """Resume a run that was waiting on ``approval_id``."""
        run = await self._runs.get_run(run_id, tenant_id=tenant_id)
        if run.is_terminal:
            return None
        approval = await self._p.approvals.get(approval_id, tenant_id=tenant_id)
        version = await self._versions.get(run.workflow_version_id, tenant_id=tenant_id)
        with correlation_scope(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            workflow_id=run.workflow_id,
            workflow_run_id=run.id,
        ):
            await self._audit(
                AuditEventType.WORKFLOW_APPROVAL_RESOLVED,
                run,
                message=f"approval {approval.status.value}",
                data={"approval_id": approval_id, "status": approval.status.value},
            )
            if run.status is WorkflowRunStatus.WAITING_APPROVAL:
                # Move back to RUNNING so the paused step is re-driven; the
                # step itself re-reads the resolved approval.
                run = await self._update_run(
                    run.transition(WorkflowRunStatus.RUNNING, pending_approval_id=None)
                )
            return await self._drive(run, version)

    # ------------------------------------------------------------------
    # Driving loop
    # ------------------------------------------------------------------
    async def _drive(self, run: WorkflowRun, version: WorkflowVersion) -> WorkflowRun:
        if run.status is WorkflowRunStatus.PENDING:
            run = await self._update_run(run.transition(WorkflowRunStatus.RUNNING))
            await self._audit(
                AuditEventType.WORKFLOW_RUN_STARTED,
                run,
                message="workflow run started",
                data={"workflow_id": run.workflow_id, "version": run.workflow_version},
            )
            self._metrics.workflow_run_started()

        started = time.perf_counter()
        while True:
            steps = await self._runs.list_steps(tenant_id=run.tenant_id, workflow_run_id=run.id)
            by_id = {s.step_id: s for s in steps}

            # Run-level timeout is a durable, terminal failure — never a zombie.
            if run.run_timeout_seconds is not None and run.started_at is not None:
                elapsed = (datetime.now(UTC) - run.started_at).total_seconds()
                if elapsed > run.run_timeout_seconds:
                    return await self._fail_run(run, "workflow run timed out")

            ready = await self._ready_steps(run, version, by_id)
            if not ready:
                break

            results = await self._run_ready_steps(run, version, ready, by_id)
            paused = any(r.outcome == "paused" for r in results.values())
            if paused:
                return await self._mark_waiting(run, results)

            failed = [sid for sid, r in results.items() if r.outcome == "failed"]
            if failed:
                return await self._fail_run(run, f"step(s) failed: {', '.join(sorted(failed))}")

        return await self._complete_run(run, started)

    async def _ready_steps(
        self, run: WorkflowRun, version: WorkflowVersion, by_id: dict[str, StepRun]
    ) -> list[WorkflowStep]:
        """Steps whose dependencies are all successful and that can still run.

        A step paused for approval is included once its approval is no longer
        pending — that is what lets a resumed run pick up exactly where it
        stopped instead of stalling forever.
        """
        definitions = {s.id: s for s in version.steps}
        ready: list[WorkflowStep] = []
        for step in version.steps:
            current = by_id.get(step.id)
            if current is None or current.is_terminal:
                continue
            if current.status is StepRunStatus.RUNNING:
                continue
            if current.status is StepRunStatus.WAITING_APPROVAL:
                if await self._approval_pending(run, current):
                    continue
                ready.append(step)
                continue
            if not self._dependencies_satisfied(step, by_id, definitions):
                continue
            ready.append(step)
        return ready

    async def _approval_pending(self, run: WorkflowRun, step_run: StepRun) -> bool:
        if step_run.approval_id is None:
            return False
        approval = await self._p_approvals_get(step_run.approval_id, run.tenant_id)
        return approval is not None and approval.is_pending

    async def _p_approvals_get(self, approval_id: str, tenant_id: str) -> Approval | None:
        try:
            return await self._p.approvals.get(approval_id, tenant_id=tenant_id)
        except NotFoundError:
            return None

    @staticmethod
    def _dependencies_satisfied(
        step: WorkflowStep,
        by_id: dict[str, StepRun],
        definitions: dict[str, WorkflowStep],
    ) -> bool:
        """Whether every dependency of ``step`` is terminal and unblocking.

        A dependency unblocks dependents when it succeeded, or when it failed
        but its own ``error_policy`` is ``continue`` (a tolerated failure).
        """
        for dep_id in step.depends_on:
            dep = by_id.get(dep_id)
            if dep is None or not dep.is_terminal:
                return False
            if dep.status in SUCCESSFUL_STEP_RUN_STATUSES:
                continue
            definition = definitions.get(dep_id)
            if definition is not None and definition.error_policy is StepErrorPolicy.CONTINUE:
                continue
            return False
        return True

    async def _run_ready_steps(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        ready: list[WorkflowStep],
        by_id: dict[str, StepRun],
    ) -> dict[str, _StepResult]:
        results: dict[str, _StepResult] = {}
        for batch in _batches(ready, MAX_PARALLELISM):
            outcomes = await asyncio.gather(
                *(self._execute_step(run, version, step, by_id[step.id]) for step in batch)
            )
            for step, result in zip(batch, outcomes, strict=True):
                results[step.id] = result
        return results

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------
    async def _execute_step(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        step_run: StepRun,
    ) -> _StepResult:
        with correlation_scope(step_run_id=step_run.id):
            return await self._execute_step_inner(run, version, step, step_run)

    async def _execute_step_inner(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        step_run: StepRun,
    ) -> _StepResult:
        # Re-read the freshest step row: a concurrent pass may have advanced it.
        step_run = await self._runs.get_step(step_run.id, tenant_id=run.tenant_id)
        if step_run.is_terminal:
            return _StepResult(outcome="completed", summary=step_run.summary)

        # A condition that is false skips the step (and satisfies dependents).
        # Only evaluated before the step has ever started: a step resumed from
        # an approval must not be re-evaluated and silently skipped.
        if step.condition is not None and step_run.status is StepRunStatus.PENDING:
            try:
                context = await self._template_context(run, version)
                if not evaluate_condition(step.condition, context):
                    skipped = await self._set_step(
                        step_run,
                        StepRunStatus.SKIPPED,
                        summary="condition not met",
                    )
                    await self._audit(
                        AuditEventType.WORKFLOW_STEP_SKIPPED,
                        run,
                        message=f"step '{step.id}' skipped: condition not met",
                        data={"step_id": step.id},
                    )
                    self._metrics.workflow_step(
                        kind=step.kind.value, outcome="skipped", duration_seconds=0.0
                    )
                    return _StepResult(outcome="completed", summary=skipped.summary)
            except ExpressionError as exc:
                return await self._fail_step(run, step, step_run, str(exc), ErrorClass.VALIDATION)

        step_run = await self._begin_step(run, step, step_run)
        timeout = step.timeout_seconds or version.default_timeout_seconds
        max_attempts = step.retry.max_attempts
        attempts_done = step_run.attempts
        while True:
            step_started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._dispatch_step(run, version, step, step_run), timeout=timeout
                )
            except TimeoutError:
                result = _StepResult(
                    outcome="failed",
                    error=f"step timed out after {timeout:g}s",
                    error_class=ErrorClass.TIMEOUT,
                )
            except PlatformError as exc:
                result = _StepResult(
                    outcome="failed", error=exc.message, error_class=exc.error_class
                )
            except Exception as exc:  # never let an unexpected error kill the worker
                logger.exception("workflow step '%s' failed unexpectedly", step.id)
                result = _StepResult(
                    outcome="failed",
                    error=f"internal error: {exc}",
                    error_class=ErrorClass.INTERNAL,
                )

            if result.outcome == "paused":
                await self._set_step(
                    step_run,
                    StepRunStatus.WAITING_APPROVAL,
                    approval_id=result.approval_id,
                    summary=result.summary,
                    agent_run_id=result.agent_run_id,
                )
                self._metrics.workflow_step(
                    kind=step.kind.value,
                    outcome="paused",
                    duration_seconds=time.perf_counter() - step_started,
                )
                return result

            if result.outcome == "completed":
                output = await self._resolve_output(run, version, step, result.data)
                await self._set_step(
                    step_run,
                    StepRunStatus.COMPLETED,
                    output=output,
                    summary=result.summary,
                    error=None,
                    error_class=None,
                    agent_run_id=result.agent_run_id,
                    approval_id=result.approval_id,
                )
                await self._audit(
                    AuditEventType.WORKFLOW_STEP_COMPLETED,
                    run,
                    message=f"step '{step.id}' completed",
                    data={"step_id": step.id, "kind": step.kind.value, "attempt": attempts_done},
                )
                self._metrics.workflow_step(
                    kind=step.kind.value,
                    outcome="completed",
                    duration_seconds=time.perf_counter() - step_started,
                )
                return result

            # Failure: retry if the policy allows and the budget remains.
            retryable = result.retryable and step.retry.allows(
                result.error_class.value if result.error_class else ""
            )
            if retryable and attempts_done < max_attempts:
                step_run = await self._set_step(
                    step_run,
                    StepRunStatus.RETRYING,
                    attempts=attempts_done,
                    error=result.error,
                    error_class=result.error_class,
                )
                await self._audit(
                    AuditEventType.WORKFLOW_STEP_RETRY,
                    run,
                    message=f"step '{step.id}' retrying (attempt {attempts_done + 1})",
                    data={"step_id": step.id, "error": result.error},
                )
                self._metrics.workflow_step_retry(kind=step.kind.value)
                delay = step.retry.backoff_seconds * attempts_done
                if delay > 0:
                    await asyncio.sleep(delay)
                step_run = await self._begin_step(run, step, step_run)
                attempts_done = step_run.attempts
                continue

            self._metrics.workflow_step(
                kind=step.kind.value,
                outcome="failed",
                duration_seconds=time.perf_counter() - step_started,
            )
            return await self._fail_step(
                run,
                step,
                step_run,
                result.error or "step failed",
                result.error_class or ErrorClass.INTERNAL,
                attempts=attempts_done,
            )

    async def _dispatch_step(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        step_run: StepRun,
    ) -> _StepResult:
        if step.kind is StepKind.DETERMINISTIC:
            return await self._execute_deterministic(run, version, step)
        if step.kind is StepKind.AGENT:
            return await self._execute_agent(run, version, step, step_run)
        if step.kind is StepKind.TOOL:
            return await self._execute_tool(run, version, step, step_run)
        if step.kind is StepKind.APPROVAL:
            return await self._execute_approval(run, version, step, step_run)
        raise PlatformError(f"unsupported step kind: {step.kind}")  # pragma: no cover

    # --- Deterministic --------------------------------------------------
    async def _execute_deterministic(
        self, run: WorkflowRun, version: WorkflowVersion, step: WorkflowStep
    ) -> _StepResult:
        assert step.action is not None
        action = self._actions.get(step.action)
        context = await self._template_context(run, version)
        arguments = render(step.arguments, context)
        data = await action.run(arguments)
        return _StepResult(outcome="completed", summary=f"{step.action} completed", data=data)

    # --- Agent ----------------------------------------------------------
    async def _execute_agent(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        step_run: StepRun,
    ) -> _StepResult:
        assert step.goal is not None
        context = await self._template_context(run, version)
        goal = render(step.goal, context)
        if not isinstance(goal, str):
            goal = str(goal)
        agent_run_id = step_run.agent_run_id or new_id(PREFIX_RUN)
        principal = _principal_from_run(run)
        runner = self._p.build_runner()
        result = await runner.run(
            principal=principal,
            goal=goal,
            workspace_dir=run.workspace_dir or None,
            run_id=agent_run_id,
        )
        if result.status is RunStatus.COMPLETED:
            return _StepResult(
                outcome="completed",
                summary=result.result or "agent completed",
                data={"result": result.result, "verified": result.verified},
                agent_run_id=agent_run_id,
                usage=result.usage,
            )
        error_class = (
            ErrorClass.PERMISSION
            if result.status is RunStatus.WAITING_APPROVAL
            else ErrorClass.EXTERNAL
        )
        return _StepResult(
            outcome="failed",
            error=result.error or f"agent finished with status {result.status.value}",
            error_class=error_class,
            agent_run_id=agent_run_id,
            usage=result.usage,
        )

    # --- Tool -----------------------------------------------------------
    async def _execute_tool(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        step_run: StepRun,
    ) -> _StepResult:
        assert step.tool is not None
        principal = _principal_from_run(run)
        context = await self._template_context(run, version)
        arguments = render(step.arguments, context)
        if not isinstance(arguments, dict):
            return _StepResult(
                outcome="failed",
                error="tool arguments must be an object",
                error_class=ErrorClass.VALIDATION,
            )
        tool_context = ToolContext(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.id,
            step_id=step.id,
            workspace_dir=run.workspace_dir or ".",
            timeout_seconds=step.timeout_seconds or version.default_timeout_seconds,
        )

        # Already holding an approval? Consume its decision.
        if step_run.approval_id is not None:
            approval = await self._p.approvals.get(step_run.approval_id, tenant_id=run.tenant_id)
            if approval.status is ApprovalStatus.PENDING:
                return _StepResult(outcome="paused", approval_id=approval.id)
            if approval.status is not ApprovalStatus.APPROVED:
                return _StepResult(
                    outcome="failed",
                    error=f"approval {approval.status.value}",
                    error_class=ErrorClass.PERMISSION,
                )
            outcome = await self._p.executor.execute_approved(
                approval=approval, context=tool_context, principal=principal
            )
        else:
            outcome = await self._p.executor.execute(
                tool_name=step.tool,
                arguments=arguments,
                context=tool_context,
                principal=principal,
            )

        from ..agent.executor import ExecutionOutcomeKind

        if outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED:
            assert outcome.approval is not None
            return _StepResult(
                outcome="paused",
                summary=outcome.approval.summary,
                approval_id=outcome.approval.id,
            )
        if outcome.kind is ExecutionOutcomeKind.DENIED:
            return _StepResult(
                outcome="failed",
                error=f"denied by policy: {outcome.reason}",
                error_class=ErrorClass.PERMISSION,
            )
        assert outcome.result is not None
        result = outcome.result
        if result.ok:
            return _StepResult(outcome="completed", summary=result.summary, data=dict(result.data))
        return _StepResult(outcome="failed", error=result.summary, error_class=ErrorClass.EXTERNAL)

    # --- Approval -------------------------------------------------------
    async def _execute_approval(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        step_run: StepRun,
    ) -> _StepResult:
        assert step.prompt is not None
        context = await self._template_context(run, version)
        prompt = render(step.prompt, context)
        if not isinstance(prompt, str):
            prompt = str(prompt)

        if step_run.approval_id is None:
            approval = Approval.create(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.id,
                tool_name=WORKFLOW_APPROVAL_TOOL,
                permission=Permission.MANAGE_WORKFLOW,
                summary=prompt,
                arguments={"step_id": step.id},
                ttl_seconds=self._settings.approval_ttl_seconds,
            )
            await self._p.approvals.add(approval)
            await self._audit(
                AuditEventType.WORKFLOW_APPROVAL_REQUESTED,
                run,
                message=f"approval requested at step '{step.id}'",
                data={"approval_id": approval.id, "step_id": step.id},
            )
            self._metrics.approval_requested()
            return _StepResult(outcome="paused", summary=prompt, approval_id=approval.id)

        approval = await self._p.approvals.get(step_run.approval_id, tenant_id=run.tenant_id)
        if approval.status is ApprovalStatus.PENDING:
            return _StepResult(outcome="paused", approval_id=approval.id)
        if approval.status is ApprovalStatus.APPROVED:
            return _StepResult(
                outcome="completed",
                summary="approved",
                data={
                    "approved": True,
                    "resolved_by": approval.resolved_by,
                    "reason": approval.decision_reason,
                },
            )
        if approval.status is ApprovalStatus.DENIED:
            return _StepResult(
                outcome="failed",
                error="approval denied",
                error_class=ErrorClass.PERMISSION,
            )
        return _StepResult(
            outcome="failed", error="approval expired", error_class=ErrorClass.TIMEOUT
        )

    # ------------------------------------------------------------------
    # Step / run state helpers
    # ------------------------------------------------------------------
    async def _begin_step(self, run: WorkflowRun, step: WorkflowStep, step_run: StepRun) -> StepRun:
        if step_run.status in {
            StepRunStatus.PENDING,
            StepRunStatus.RETRYING,
            StepRunStatus.WAITING_APPROVAL,
        }:
            step_run = await self._set_step(
                step_run, StepRunStatus.RUNNING, attempts=step_run.attempts + 1
            )
            await self._audit(
                AuditEventType.WORKFLOW_STEP_STARTED,
                run,
                message=f"step '{step.id}' started",
                data={"step_id": step.id, "kind": step.kind.value},
            )
        return step_run

    async def _fail_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        step_run: StepRun,
        error: str,
        error_class: ErrorClass,
        *,
        attempts: int | None = None,
    ) -> _StepResult:
        await self._audit(
            AuditEventType.WORKFLOW_STEP_FAILED,
            run,
            message=f"step '{step.id}' failed: {error}",
            data={"step_id": step.id, "error": error, "error_class": error_class.value},
        )
        if step.error_policy is StepErrorPolicy.SKIP:
            await self._set_step(
                step_run,
                StepRunStatus.SKIPPED,
                error=error,
                error_class=error_class,
                summary=f"skipped after failure: {error}",
                attempts=attempts,
            )
            return _StepResult(outcome="completed", summary=f"skipped: {error}")
        if step.error_policy is StepErrorPolicy.CONTINUE:
            await self._set_step(
                step_run,
                StepRunStatus.FAILED,
                error=error,
                error_class=error_class,
                summary=error,
                attempts=attempts,
            )
            # Treated as "handled": dependents are satisfied and the run goes on.
            return _StepResult(outcome="completed", summary=error)
        await self._set_step(
            step_run,
            StepRunStatus.FAILED,
            error=error,
            error_class=error_class,
            summary=error,
            attempts=attempts,
        )
        return _StepResult(outcome="failed", error=error, error_class=error_class)

    async def _set_step(self, step_run: StepRun, status: StepRunStatus, **updates: Any) -> StepRun:
        """Persist a step transition, retrying once on an optimistic conflict."""
        for attempt in range(3):
            try:
                current = await self._runs.get_step(step_run.id, tenant_id=step_run.tenant_id)
                if current.status is status and not updates:
                    return current
                updated = current.transition(status, **updates)
                return await self._runs.update_step(updated, expected_version=current.version)
            except ConflictError:
                logger.info("step %s advanced concurrently (attempt %d)", step_run.id, attempt + 1)
                continue
        return await self._runs.get_step(step_run.id, tenant_id=step_run.tenant_id)

    async def _mark_waiting(self, run: WorkflowRun, results: dict[str, _StepResult]) -> WorkflowRun:
        approval_id = next(
            (r.approval_id for r in results.values() if r.outcome == "paused" and r.approval_id),
            None,
        )
        if run.status is WorkflowRunStatus.RUNNING:
            run = await self._update_run(
                run.transition(WorkflowRunStatus.WAITING_APPROVAL, pending_approval_id=approval_id)
            )
        else:
            run = await self._update_run(
                run.model_copy(update={"pending_approval_id": approval_id})
            )
        return run

    async def _complete_run(self, run: WorkflowRun, started: float) -> WorkflowRun:
        steps = await self._runs.list_steps(tenant_id=run.tenant_id, workflow_run_id=run.id)
        output = {s.step_id: s.output for s in steps if s.is_terminal}
        run = await self._update_run(
            run.transition(WorkflowRunStatus.COMPLETED, output=output, error=None)
        )
        await self._audit(
            AuditEventType.WORKFLOW_RUN_COMPLETED,
            run,
            message="workflow run completed",
            data={"steps": len(steps)},
        )
        self._metrics.workflow_run_finished(
            status=WorkflowRunStatus.COMPLETED.value,
            duration_seconds=time.perf_counter() - started,
        )
        return run

    async def _fail_run(self, run: WorkflowRun, error: str) -> WorkflowRun:
        if run.is_terminal:
            return run
        run = await self._update_run(
            run.transition(WorkflowRunStatus.FAILED, error=error, lease_expires_at=None)
        )
        await self._audit(
            AuditEventType.WORKFLOW_RUN_FAILED, run, message=error, data={"error": error}
        )
        self._metrics.workflow_run_finished(
            status=WorkflowRunStatus.FAILED.value, duration_seconds=0.0
        )
        return run

    async def _update_run(self, run: WorkflowRun) -> WorkflowRun:
        for attempt in range(3):
            try:
                current = await self._runs.get_run(run.id, tenant_id=run.tenant_id)
                if current.is_terminal and not run.is_terminal:
                    return current
                merged = run.model_copy(update={"version": current.version})
                return await self._runs.update_run(merged, expected_version=current.version)
            except ConflictError:
                logger.info("run %s advanced concurrently (attempt %d)", run.id, attempt + 1)
                continue
        return await self._runs.get_run(run.id, tenant_id=run.tenant_id)

    # ------------------------------------------------------------------
    # Context / templating
    # ------------------------------------------------------------------
    async def _template_context(self, run: WorkflowRun, version: WorkflowVersion) -> dict[str, Any]:
        steps = await self._runs.list_steps(tenant_id=run.tenant_id, workflow_run_id=run.id)
        return {
            "input": dict(run.input),
            "workflow": {
                "id": run.workflow_id,
                "run_id": run.id,
                "version": run.workflow_version,
            },
            "user": {"id": run.user_id, "tenant_id": run.tenant_id},
            "now": datetime.now(UTC).isoformat(),
            "steps": {
                s.step_id: {
                    "status": s.status.value,
                    "output": dict(s.output),
                    "summary": s.summary,
                }
                for s in steps
            },
        }

    async def _resolve_output(
        self,
        run: WorkflowRun,
        version: WorkflowVersion,
        step: WorkflowStep,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        if not step.output:
            return dict(data)
        context = await self._template_context(run, version)
        context["result"] = data
        resolved: dict[str, Any] = {}
        for key, expression in step.output.items():
            try:
                resolved[key] = render(expression, context)
            except ExpressionError as exc:
                raise PlatformError(
                    f"output '{key}' could not be resolved: {exc}",
                    error_class=ErrorClass.VALIDATION,
                ) from exc
        return resolved

    async def _audit(
        self,
        event_type: AuditEventType,
        run: WorkflowRun,
        *,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._p.audit.record(
            event_type,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.id,
            message=message,
            data=data,
        )


def _principal_from_run(run: WorkflowRun) -> Principal:
    granted = (
        frozenset(Permission(p) for p in run.granted_permissions)
        if run.granted_permissions is not None
        else None
    )
    return Principal(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        roles=frozenset(run.roles),
        granted_permissions=granted,
    )


def _batches(items: list[WorkflowStep], size: int) -> list[list[WorkflowStep]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


__all__ = [
    "DEFAULT_STEP_TIMEOUT_SECONDS",
    "WORKFLOW_APPROVAL_TOOL",
    "WorkflowEngine",
]
