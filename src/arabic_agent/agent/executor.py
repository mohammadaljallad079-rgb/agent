"""Tool executor: the policy gate between the agent and every tool.

The executor is the *only* place a tool is invoked. It evaluates policy for
each permission the tool requires, enforces the strictest outcome (deny beats
approval beats allow), records audit events, and returns a structured outcome.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..audit.base import AuditEventType, AuditLog
from ..core.enums import ApprovalStatus, PermissionEffect, StrEnum
from ..core.errors import PermissionDeniedError
from ..permissions.approvals import Approval, ApprovalStore
from ..permissions.base import Permission, PolicyDecision, Principal
from ..permissions.policy import PolicyEngine
from ..tools.base import Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry


class ExecutionOutcomeKind(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


class ExecutionOutcome(BaseModel):
    """Result of an executor attempt."""

    model_config = ConfigDict(frozen=True)

    kind: ExecutionOutcomeKind
    tool: str
    result: ToolResult | None = None
    approval: Approval | None = None
    decisions: tuple[PolicyDecision, ...] = Field(default_factory=tuple)
    reason: str = ""


class ToolExecutor:
    """Invokes tools only after policy clearance."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        approvals: ApprovalStore,
        audit: AuditLog,
        approval_ttl_seconds: int = 3600,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._ttl = approval_ttl_seconds

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        context: ToolContext,
        principal: Principal,
    ) -> ExecutionOutcome:
        tool = self._registry.get(tool_name)
        decisions = self._evaluate(principal, tool)

        # Deny wins over everything.
        denial = next((d for d in decisions if d.effect is PermissionEffect.DENY), None)
        if denial is not None:
            await self._audit.record(
                AuditEventType.POLICY_DECISION,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message=f"denied {tool_name}: {denial.reason}",
                data={
                    "tool": tool_name,
                    "permission": denial.permission.value,
                    "effect": denial.effect.value,
                },
            )
            return ExecutionOutcome(
                kind=ExecutionOutcomeKind.DENIED,
                tool=tool_name,
                decisions=decisions,
                reason=denial.reason,
            )

        # Approval required?
        approval_decision = next(
            (d for d in decisions if d.effect is PermissionEffect.REQUIRES_APPROVAL), None
        )
        if approval_decision is not None:
            approval = Approval.create(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                tool_name=tool_name,
                permission=approval_decision.permission,
                summary=self._approval_summary(tool_name, approval_decision),
                arguments=arguments,
                ttl_seconds=self._ttl,
            )
            await self._approvals.add(approval)
            await self._audit.record(
                AuditEventType.APPROVAL_REQUESTED,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message=f"approval requested for {tool_name}",
                data={
                    "approval_id": approval.id,
                    "tool": tool_name,
                    "permission": approval_decision.permission.value,
                },
            )
            return ExecutionOutcome(
                kind=ExecutionOutcomeKind.APPROVAL_REQUIRED,
                tool=tool_name,
                approval=approval,
                decisions=decisions,
                reason=approval_decision.reason,
            )

        # Allowed: run it.
        await self._audit.record(
            AuditEventType.TOOL_CALLED,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            message=f"calling tool {tool_name}",
            data={"tool": tool_name, "arguments": arguments},
        )
        try:
            result = await tool.execute(arguments, context)
        except PermissionDeniedError:
            raise
        except Exception as exc:
            await self._audit.record(
                AuditEventType.TOOL_ERROR,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message=f"tool {tool_name} raised",
                data={"tool": tool_name, "error": str(exc)},
            )
            raise

        await self._audit.record(
            AuditEventType.TOOL_RESULT,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            message=result.summary,
            data={"tool": tool_name, "ok": result.ok, "data": result.data},
        )
        return ExecutionOutcome(
            kind=ExecutionOutcomeKind.COMPLETED,
            tool=tool_name,
            result=result,
            decisions=decisions,
            reason="allowed",
        )

    async def execute_approved(
        self,
        *,
        approval: Approval,
        context: ToolContext,
        principal: Principal,
    ) -> ExecutionOutcome:
        """Run a tool whose approval has been granted, re-checking policy.

        Re-checking ensures a revoked permission cannot be bypassed by an
        approval that was granted earlier.
        """
        if approval.status is not ApprovalStatus.APPROVED:
            return ExecutionOutcome(
                kind=ExecutionOutcomeKind.APPROVAL_REQUIRED,
                tool=approval.tool_name,
                approval=approval,
                reason="approval is not granted",
            )
        tool = self._registry.get(approval.tool_name)
        decisions = self._evaluate(principal, tool)
        denial = next((d for d in decisions if d.effect is PermissionEffect.DENY), None)
        if denial is not None:
            return ExecutionOutcome(
                kind=ExecutionOutcomeKind.DENIED,
                tool=approval.tool_name,
                decisions=decisions,
                reason=denial.reason,
            )
        await self._audit.record(
            AuditEventType.TOOL_CALLED,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            message=f"calling approved tool {approval.tool_name}",
            data={
                "tool": approval.tool_name,
                "arguments": approval.arguments,
                "approval_id": approval.id,
            },
        )
        result = await tool.execute(dict(approval.arguments), context)
        await self._audit.record(
            AuditEventType.TOOL_RESULT,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            message=result.summary,
            data={"tool": approval.tool_name, "ok": result.ok, "data": result.data},
        )
        return ExecutionOutcome(
            kind=ExecutionOutcomeKind.COMPLETED,
            tool=approval.tool_name,
            result=result,
            decisions=decisions,
            reason="approved",
        )

    def _evaluate(self, principal: Principal, tool: Tool) -> tuple[PolicyDecision, ...]:
        return tuple(
            self._policy.evaluate(
                principal,
                tool_name=tool.name,
                permission=permission,
                risk=risk,
            )
            for permission, risk in tool.permission_risks().items()
        )

    @staticmethod
    def _approval_summary(tool_name: str, decision: PolicyDecision) -> str:
        return (
            f"Approval required to run '{tool_name}' "
            f"({decision.permission.value}): {decision.reason}"
        )

    @staticmethod
    def required_permissions(tool: Tool) -> tuple[Permission, ...]:
        return tool.permissions
