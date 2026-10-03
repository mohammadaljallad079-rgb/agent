"""The agent runner: the plan → act → observe → verify → recover loop.

This is the heart of the platform. Every transition is explicit so behaviour is
testable and observable:

    plan → for each step: (assemble context → call model → execute tool → observe)
        on failure → classify → recover (retry/repair/replan/skip/abort)
        on sensitive op → pause for approval
    → verify → finish

Guard rails (max steps, max retries, max replans, wall-clock deadline) make
infinite loops impossible by construction.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..audit.base import AuditEventType, AuditLog
from ..context.manager import ContextManager
from ..core.config import Settings
from ..core.enums import ErrorClass, Locale, MemoryKind, RunStatus, StepStatus
from ..core.errors import ApprovalRequiredError, PlatformError
from ..core.ids import PREFIX_RUN, new_id
from ..memory.base import MemoryEntry, MemoryQuery, MemoryStore
from ..memory.policy import MemoryPolicy
from ..models.base import ChatMessage, ModelRequest, Usage
from ..models.gateway import ModelGateway
from ..permissions.approvals import Approval, ApprovalStore
from ..permissions.base import Principal
from ..tasks.base import TaskPlan, TaskStep
from ..tasks.engine import TaskEngine
from ..tools.base import ToolContext
from ..tools.registry import ToolRegistry
from .base import AgentRunResult, RecoveryAction
from .executor import ExecutionOutcomeKind, ToolExecutor
from .planner import Planner
from .prompts import system_prompt
from .recovery import RecoveryPolicy

logger = logging.getLogger(__name__)

FINISH_TOOL = "finish"
_HISTORY_WINDOW = 30

StepAction = Literal["continue", "replan", "finish"]


@dataclass
class _RunStats:
    """Mutable run counters."""

    steps_executed: int = 0
    tool_calls: int = 0
    retries: int = 0
    replans: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass
class _StepOutcome:
    """Internal result of a completed step (not an approval pause or abort)."""

    action: StepAction
    plan: TaskPlan
    usage: Usage
    tool_calls: int = 0
    retries: int = 0


class AgentRunner:
    """Runs a goal to completion, pausing for approvals when required."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        tools: ToolRegistry,
        executor: ToolExecutor,
        tasks: TaskEngine,
        context: ContextManager,
        memory: MemoryStore,
        memory_policy: MemoryPolicy,
        audit: AuditLog,
        approvals: ApprovalStore,
        settings: Settings,
        recovery: RecoveryPolicy | None = None,
        locale: Locale = Locale.AR,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._executor = executor
        self._tasks = tasks
        self._context = context
        self._memory = memory
        self._memory_policy = memory_policy
        self._audit = audit
        self._approvals = approvals
        self._settings = settings
        self._recovery = recovery or RecoveryPolicy(
            max_retries=settings.max_tool_retries, max_replans=settings.max_replans
        )
        self._locale = locale
        self._planner = Planner(gateway, locale=locale)

    async def run(
        self,
        *,
        principal: Principal,
        goal: str,
        workspace_dir: str | None = None,
        run_id: str | None = None,
        resume: bool = False,
        recovery_attempt: int | None = None,
    ) -> AgentRunResult:
        run_id = run_id or new_id(PREFIX_RUN)
        workspace = self._prepare_workspace(workspace_dir, run_id)
        started = time.monotonic()

        await self._audit.record(
            (
                AuditEventType.RUN_RECOVERED
                if recovery_attempt is not None
                else AuditEventType.RUN_STARTED
            ),
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message="run recovered" if recovery_attempt is not None else "run started",
            data={"goal": goal, "workspace": workspace, "recovery_attempt": recovery_attempt},
        )

        plan = await self._resolve_plan(
            principal=principal, goal=goal, run_id=run_id, resume=resume
        )
        await self._audit.record(
            AuditEventType.PLAN_CREATED,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message=f"plan with {len(plan.steps)} steps",
            data={"plan": plan.render()},
        )

        history: list[ChatMessage] = [ChatMessage.user(goal)]
        stats = _RunStats()

        while True:
            guard = self._check_guards(stats, started)
            if guard is not None:
                return await self._fail(principal, run_id, plan, guard, stats, workspace=workspace)

            step = plan.current_step()
            if step is None:
                break

            plan = await self._tasks.start_step(plan, step.id)
            step = plan.current_step() or step
            await self._audit.record(
                AuditEventType.STEP_STARTED,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                run_id=run_id,
                message=f"step {step.index + 1}: {step.description}",
                data={"step_id": step.id, "attempt": step.attempts},
            )

            outcome = await self._run_step(
                principal=principal,
                plan=plan,
                step=step,
                history=history,
                workspace=workspace,
                run_id=run_id,
            )

            if isinstance(outcome, AgentRunResult):
                outcome.usage = stats.usage + outcome.usage
                return outcome

            stats.tool_calls += outcome.tool_calls
            stats.retries += outcome.retries
            stats.usage = stats.usage + outcome.usage
            plan = outcome.plan

            if outcome.action == "finish":
                break
            if outcome.action == "replan":
                stats.replans += 1
                plan = await self._replan(principal, plan, step, run_id)
                continue
            stats.steps_executed += 1

        verified, final_text = await self._verify(principal, plan, stats.usage)
        await self._audit.record(
            AuditEventType.RUN_COMPLETED,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message="run completed",
            data={"verified": verified},
        )
        await self._remember_outcome(principal, plan, final_text, run_id)

        return AgentRunResult(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            result=final_text,
            verified=verified,
            plan=plan,
            workspace_dir=workspace,
            steps_executed=stats.steps_executed,
            tool_calls=stats.tool_calls,
            retries=stats.retries,
            replans=stats.replans,
            usage=stats.usage,
        )

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------
    async def _run_step(
        self,
        *,
        principal: Principal,
        plan: TaskPlan,
        step: TaskStep,
        history: list[ChatMessage],
        workspace: str,
        run_id: str,
    ) -> _StepOutcome | AgentRunResult:
        attempt = 0
        tool_calls = 0
        max_attempts = self._settings.max_tool_retries + 1
        usage = Usage()

        while attempt < max_attempts:
            attempt += 1
            memories = await self._recall(principal)
            package = self._context.build(
                system_prompt=system_prompt(self._locale),
                goal=plan.goal,
                plan=plan,
                recent_messages=history[-_HISTORY_WINDOW:],
                memories=memories,
                tool_specs=self._tools.specs(),
                scratchpad=self._scratchpad(step),
                locale=self._locale,
            )
            request = ModelRequest(messages=package.messages, tools=package.tools)
            try:
                response = await self._gateway.complete(request)
            except PlatformError as exc:
                recovery = self._recovery.decide(
                    exc.error_class, step_attempts=attempt, replans_used=0
                )
                if recovery is RecoveryAction.ABORT:
                    return await self._fail(
                        principal,
                        run_id,
                        plan,
                        f"model error: {exc.message}",
                        _RunStats(tool_calls=tool_calls, retries=attempt - 1, usage=usage),
                        workspace=workspace,
                    )
                continue

            usage = usage + response.usage
            await self._audit.record(
                AuditEventType.MODEL_CALL,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                run_id=run_id,
                message="model responded",
                data={"tool_calls": [c.name for c in response.tool_calls]},
            )

            if not response.tool_calls:
                history.append(response.message)
                history.append(
                    ChatMessage.user(
                        "Act using a tool, or call `finish` if the goal is already met."
                    )
                )
                continue

            history.append(response.message)
            for call in response.tool_calls:
                tool_calls += 1
                try:
                    outcome = await self._executor.execute(
                        tool_name=call.name,
                        arguments=call.arguments,
                        context=self._tool_context(principal, run_id, workspace, step),
                        principal=principal,
                    )
                except PlatformError as exc:
                    # A tool raised a classified error: recover rather than crash.
                    failed = await self._tasks.fail_step(
                        plan,
                        step.id,
                        error_class=exc.error_class,
                        message=exc.message,
                    )
                    recovery = self._recovery.decide(
                        exc.error_class, step_attempts=attempt, replans_used=0
                    )
                    if recovery is RecoveryAction.REPLAN:
                        return _StepOutcome(
                            action="replan",
                            plan=failed,
                            usage=usage,
                            tool_calls=tool_calls,
                            retries=attempt - 1,
                        )
                    if recovery is RecoveryAction.SKIP:
                        skipped = await self._tasks.skip_step(plan, step.id, reason=exc.message)
                        return _StepOutcome(
                            action="continue",
                            plan=skipped,
                            usage=usage,
                            tool_calls=tool_calls,
                            retries=attempt - 1,
                        )
                    if recovery is RecoveryAction.ABORT:
                        return await self._fail(
                            principal,
                            run_id,
                            failed,
                            exc.message,
                            _RunStats(tool_calls=tool_calls, retries=attempt - 1, usage=usage),
                            workspace=workspace,
                        )
                    history.append(
                        ChatMessage.tool_result(
                            call.id,
                            f"ERROR ({exc.error_class.value}): {exc.message}. "
                            "Adjust your approach and retry.",
                            name=call.name,
                        )
                    )
                    continue

                if outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED:
                    assert outcome.approval is not None
                    blocked = await self._tasks.block_step(
                        plan, step.id, reason="awaiting approval"
                    )
                    return AgentRunResult(
                        run_id=run_id,
                        status=RunStatus.WAITING_APPROVAL,
                        plan=blocked,
                        pending_approval_id=outcome.approval.id,
                        result=outcome.approval.summary,
                        workspace_dir=workspace,
                        usage=usage,
                    )

                if outcome.kind is ExecutionOutcomeKind.DENIED:
                    history.append(
                        ChatMessage.tool_result(
                            call.id,
                            f"DENIED by policy: {outcome.reason}. "
                            "Choose a different approach or stop.",
                            name=call.name,
                        )
                    )
                    continue

                assert outcome.result is not None
                result = outcome.result
                history.append(
                    ChatMessage.tool_result(
                        call.id,
                        _render_tool_result(result.ok, result.summary, result.data),
                        name=call.name,
                    )
                )

                if result.ok:
                    completed = await self._tasks.complete_step(
                        plan, step.id, summary=result.summary, data=result.data
                    )
                    step_action: StepAction = "finish" if call.name == FINISH_TOOL else "continue"
                    return _StepOutcome(
                        action=step_action,
                        plan=completed,
                        usage=usage,
                        tool_calls=tool_calls,
                        retries=attempt - 1,
                    )

                # Structured tool failure → recovery.
                failed = await self._tasks.fail_step(
                    plan, step.id, error_class=ErrorClass.EXTERNAL, message=result.summary
                )
                recovery = self._recovery.decide(
                    ErrorClass.EXTERNAL, step_attempts=attempt, replans_used=0
                )
                if recovery is RecoveryAction.REPLAN:
                    return _StepOutcome(
                        action="replan",
                        plan=failed,
                        usage=usage,
                        tool_calls=tool_calls,
                        retries=attempt - 1,
                    )
                if recovery is RecoveryAction.SKIP:
                    skipped = await self._tasks.skip_step(plan, step.id, reason=result.summary)
                    return _StepOutcome(
                        action="continue",
                        plan=skipped,
                        usage=usage,
                        tool_calls=tool_calls,
                        retries=attempt - 1,
                    )
                if recovery is RecoveryAction.ABORT:
                    return await self._fail(
                        principal,
                        run_id,
                        failed,
                        result.summary,
                        _RunStats(tool_calls=tool_calls, retries=attempt - 1, usage=usage),
                        workspace=workspace,
                    )
                continue

        return await self._fail(
            principal,
            run_id,
            plan,
            f"step {step.index + 1} exceeded retry limit ({max_attempts} attempts)",
            _RunStats(tool_calls=tool_calls, retries=attempt, usage=usage),
            workspace=workspace,
        )

    # ------------------------------------------------------------------
    # Approval resumption
    # ------------------------------------------------------------------
    async def resume_after_approval(
        self,
        *,
        principal: Principal,
        approval: Approval,
        run_id: str,
        workspace_dir: str,
    ) -> AgentRunResult:
        """Execute an approved tool, mark its step, then continue the run.

        Re-entering :meth:`run` with ``resume=True`` is safe because the plan is
        loaded from the store and the just-completed step is terminal, so it is
        not retried.
        """
        from ..core.errors import NotFoundError

        plan = await self._tasks.load_by_run(tenant_id=principal.tenant_id, run_id=run_id)
        if plan is None:
            raise NotFoundError("no plan for run", details={"run_id": run_id})
        step = plan.current_step()
        if step is not None:
            outcome = await self._executor.execute_approved(
                approval=approval,
                context=self._tool_context(principal, run_id, workspace_dir, step),
                principal=principal,
            )
            if outcome.kind is ExecutionOutcomeKind.COMPLETED and outcome.result is not None:
                if outcome.result.ok:
                    plan = await self._tasks.complete_step(
                        plan, step.id, summary=outcome.result.summary, data=outcome.result.data
                    )
                else:
                    plan = await self._tasks.fail_step(
                        plan,
                        step.id,
                        error_class=ErrorClass.EXTERNAL,
                        message=outcome.result.summary,
                    )
            elif outcome.kind is ExecutionOutcomeKind.DENIED:
                plan = await self._tasks.skip_step(
                    plan, step.id, reason=f"denied: {outcome.reason}"
                )
        return await self.run(
            principal=principal,
            goal=plan.goal,
            workspace_dir=workspace_dir,
            run_id=run_id,
            resume=True,
        )

    # ------------------------------------------------------------------
    # Planning helpers
    # ------------------------------------------------------------------
    async def _resolve_plan(
        self, *, principal: Principal, goal: str, run_id: str, resume: bool
    ) -> TaskPlan:
        if resume:
            existing = await self._tasks.load_by_run(tenant_id=principal.tenant_id, run_id=run_id)
            if existing is not None:
                return existing
        plan = await self._planner.plan(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            goal=goal,
        )
        return await self._tasks.create_plan(plan)

    async def _replan(
        self, principal: Principal, plan: TaskPlan, failed_step: TaskStep, run_id: str
    ) -> TaskPlan:
        await self._audit.record(
            AuditEventType.PLAN_REVISED,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message=f"replanning after step {failed_step.index + 1}",
            data={"failed_step": failed_step.description},
        )
        fresh = await self._planner.plan(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            goal=plan.goal,
        )
        return await self._tasks.replan(plan, new_steps=list(fresh.steps), reason="recovery replan")

    # ------------------------------------------------------------------
    # Verification & memory
    # ------------------------------------------------------------------
    async def _verify(self, principal: Principal, plan: TaskPlan, usage: Usage) -> tuple[bool, str]:
        """Ask the model whether the goal is satisfied; never fabricate a pass."""
        completed = [s for s in plan.steps if s.status is StepStatus.DONE]
        if not completed:
            return False, "No steps completed."
        summary = "\n".join(f"- {s.description}: {s.result_summary or 'done'}" for s in completed)
        request = ModelRequest(
            messages=(
                ChatMessage.system(
                    "You are a strict verifier. Answer with JSON "
                    '{"verified": true|false, "result": "<final answer to the user>"}. '
                    "Set verified=true only if the goal is fully achieved."
                ),
                ChatMessage.user(f"Goal:\n{plan.goal}\n\nCompleted steps:\n{summary}"),
            ),
            temperature=0.0,
        )
        try:
            response = await self._gateway.complete(request)
        except PlatformError:
            return False, f"Completed {len(completed)} step(s); verification unavailable."
        return _parse_verification(response.content, summary)

    async def _remember_outcome(
        self, principal: Principal, plan: TaskPlan, result: str, run_id: str
    ) -> None:
        allowed, _ = self._memory_policy.check_write(MemoryKind.TASK, result)
        if not allowed:
            return
        entry = MemoryEntry(
            kind=MemoryKind.TASK,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            key=f"run:{run_id}",
            content=result[:2000],
            importance=0.4,
        )
        await self._memory.write(entry)
        await self._audit.record(
            AuditEventType.MEMORY_WRITE,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message="stored task outcome",
            data={"key": entry.key},
        )

    async def _recall(self, principal: Principal) -> list[MemoryEntry]:
        return await self._memory.query(
            MemoryQuery(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                kinds=(MemoryKind.PREFERENCE, MemoryKind.COMPANY, MemoryKind.KNOWLEDGE),
                limit=5,
            )
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _check_guards(self, stats: _RunStats, started: float) -> str | None:
        if stats.steps_executed >= self._settings.max_steps_per_run:
            return "max steps exceeded"
        if time.monotonic() - started > self._settings.max_run_seconds:
            return "run deadline exceeded"
        return None

    def _tool_context(
        self, principal: Principal, run_id: str, workspace: str, step: TaskStep
    ) -> ToolContext:
        return ToolContext(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            step_id=step.id,
            workspace_dir=workspace,
            timeout_seconds=min(30.0, self._settings.max_run_seconds),
        )

    @staticmethod
    def _scratchpad(step: TaskStep) -> str:
        return f"Current step: {step.description}"

    def _prepare_workspace(self, workspace_dir: str | None, run_id: str) -> str:
        base = (
            Path(workspace_dir)
            if workspace_dir
            else Path(self._settings.local_store_dir) / "workspaces" / run_id
        )
        base.mkdir(parents=True, exist_ok=True)
        return str(base.resolve())

    async def _fail(
        self,
        principal: Principal,
        run_id: str,
        plan: TaskPlan,
        message: str,
        stats: _RunStats,
        *,
        workspace: str,
    ) -> AgentRunResult:
        await self._audit.record(
            AuditEventType.RUN_FAILED,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message=message,
        )
        return AgentRunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            error=message,
            plan=plan,
            workspace_dir=workspace,
            steps_executed=stats.steps_executed,
            tool_calls=stats.tool_calls,
            retries=stats.retries,
            replans=stats.replans,
            usage=stats.usage,
        )


def _render_tool_result(ok: bool, summary: str, data: dict[str, object]) -> str:
    status = "OK" if ok else "FAILED"
    payload = json.dumps(data, ensure_ascii=False, default=str)
    if len(payload) > 4000:
        payload = payload[:4000] + "…(truncated)"
    return f"[{status}] {summary}\n{payload}"


def _parse_verification(content: str, fallback_summary: str) -> tuple[bool, str]:
    match = re.search(r"\{.*\}", content or "", re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict):
                return bool(payload.get("verified", False)), str(
                    payload.get("result") or fallback_summary
                )
        except json.JSONDecodeError:
            pass
    return False, fallback_summary


__all__ = ["AgentRunner", "ApprovalRequiredError"]
