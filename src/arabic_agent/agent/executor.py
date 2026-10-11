"""Tool executor: the policy gate between the agent and every tool.

The executor is the *only* place a tool is invoked. It evaluates policy for
each permission the tool requires, enforces the strictest outcome (deny beats
approval beats allow), records audit events, and returns a structured outcome.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from ..audit.base import AuditEventType, AuditLog
from ..core.enums import ApprovalStatus, PermissionEffect, StrEnum
from ..core.errors import PermissionDeniedError
from ..infrastructure.idempotency import (
    IdempotencyGuard,
    IdempotencyOutcome,
    digest,
    make_key,
)
from ..observability.metrics import Metrics
from ..observability.tracing import Tracer, span
from ..permissions.approvals import Approval, ApprovalStore
from ..permissions.base import Permission, PolicyDecision, Principal
from ..permissions.policy import PolicyEngine
from ..tools.base import Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry


class ExecutionOutcomeKind(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


#: Permissions that classify a tool as touching local files, so the executor can
#: emit a semantic local audit event (file accessed / modified / command) in
#: addition to the generic tool.call/result pair. This keeps local-runtime audit
#: honest without teaching every local tool about the audit log.
_LOCAL_READ_PERMISSIONS = frozenset({Permission.FILES_READ, Permission.FILES_SEARCH})
_LOCAL_WRITE_PERMISSIONS = frozenset(
    {Permission.FILES_WRITE, Permission.FILES_MOVE, Permission.FILES_DELETE}
)


def _local_audit_event(tool: Tool) -> AuditEventType | None:
    permissions = set(tool.permissions)
    if Permission.RUN_COMMAND in permissions:
        return AuditEventType.LOCAL_COMMAND_ATTEMPTED
    if permissions & _LOCAL_WRITE_PERMISSIONS:
        return AuditEventType.LOCAL_FILE_MODIFIED
    if permissions & _LOCAL_READ_PERMISSIONS:
        return AuditEventType.LOCAL_FILE_ACCESSED
    return None


#: Semantic audit events keyed by tool name, so a document parse, a retrieval
#: and a project inspection are visible on the audit trail by *meaning*, not
#: only as a generic tool.result. Values are the event emitted on success.
_SEMANTIC_AUDIT_EVENTS: dict[str, AuditEventType] = {
    "inspect_document": AuditEventType.DOCUMENT_PARSED,
    "read_document": AuditEventType.DOCUMENT_PARSED,
    "search_document": AuditEventType.DOCUMENT_RETRIEVED,
    "list_documents": AuditEventType.DOCUMENT_RETRIEVED,
    "inspect_archive": AuditEventType.DOCUMENT_PARSED,
    "inspect_project": AuditEventType.PROJECT_INSPECTED,
    "generate_document": AuditEventType.DOCUMENT_GENERATED,
    "create_presentation": AuditEventType.DOCUMENT_GENERATED,
    "create_spreadsheet": AuditEventType.DOCUMENT_GENERATED,
}


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
        idempotency: IdempotencyGuard | None = None,
        idempotency_ttl_seconds: int = 86_400,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        tool_metadata: dict[str, object] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._ttl = approval_ttl_seconds
        self._idempotency = idempotency
        self._idempotency_ttl = idempotency_ttl_seconds
        self._metrics = metrics
        self._tracer = tracer
        #: Ambient, non-secret metadata merged into every tool context. The
        #: local runtime uses this to pass the filesystem sandbox policy and the
        #: command policy to local tools without the agent runner or workflow
        #: engine needing to know about them.
        self._tool_metadata = dict(tool_metadata or {})

    def _merge_context(
        self, context: ToolContext, *, approval_granted: bool = False
    ) -> ToolContext:
        if not self._tool_metadata and not approval_granted:
            return context
        # ``approval_granted`` is set here, by the executor, from the approval
        # record -- never from model-supplied arguments -- so a tool that needs
        # a trusted "the human approved this" signal can read it safely.
        metadata = {
            **self._tool_metadata,
            **context.metadata,
            "approval_granted": approval_granted,
        }
        return context.model_copy(update={"metadata": metadata})

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        context: ToolContext,
        principal: Principal,
    ) -> ExecutionOutcome:
        tool = self._registry.get(tool_name)
        started = time.perf_counter()
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
            self._record_tool_metric(tool_name, "denied", started)
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
            if self._metrics is not None:
                self._metrics.approval_requested()
            self._record_tool_metric(tool_name, "approval_required", started)
            return ExecutionOutcome(
                kind=ExecutionOutcomeKind.APPROVAL_REQUIRED,
                tool=tool_name,
                approval=approval,
                decisions=decisions,
                reason=approval_decision.reason,
            )

        # Allowed: run it.
        with span(
            "tool.execute",
            attributes={"tool": tool_name, "permission_count": len(decisions)},
            tracer=self._tracer,
        ):
            result = await self._invoke(tool, arguments, self._merge_context(context), principal)
        self._record_tool_metric(tool_name, "completed" if result.ok else "error", started)
        return ExecutionOutcome(
            kind=ExecutionOutcomeKind.COMPLETED,
            tool=tool_name,
            result=result,
            decisions=decisions,
            reason="allowed",
        )

    def _record_tool_metric(self, tool_name: str, outcome: str, started: float) -> None:
        if self._metrics is None:
            return
        self._metrics.tool_call(
            tool=tool_name,
            outcome=outcome,
            duration_seconds=time.perf_counter() - started,
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
        started = time.perf_counter()
        decisions = self._evaluate(principal, tool)
        denial = next((d for d in decisions if d.effect is PermissionEffect.DENY), None)
        if denial is not None:
            self._record_tool_metric(approval.tool_name, "denied", started)
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
        with span(
            "tool.execute_approved",
            attributes={"tool": approval.tool_name, "approval_id": approval.id},
            tracer=self._tracer,
        ):
            result = await self._invoke(
                tool,
                dict(approval.arguments),
                self._merge_context(context, approval_granted=True),
                principal,
            )
        self._record_tool_metric(approval.tool_name, "completed" if result.ok else "error", started)
        return ExecutionOutcome(
            kind=ExecutionOutcomeKind.COMPLETED,
            tool=approval.tool_name,
            result=result,
            decisions=decisions,
            reason="approved",
        )

    async def _invoke(
        self,
        tool: Tool,
        arguments: dict[str, object],
        context: ToolContext,
        principal: Principal,
    ) -> ToolResult:
        """Run a cleared tool, applying the idempotency guard when relevant.

        For a side-effecting tool with a guard configured, an identical
        (tool, arguments, run) request is executed at most once: a redelivered
        job or a retried step replays the recorded result instead of repeating
        the external effect.
        """
        guard = self._idempotency if tool.side_effecting else None
        guard_key: str | None = None
        if guard is not None:
            guard_key = make_key(
                tenant_id=context.tenant_id,
                scope="tool",
                key=f"{tool.name}:{context.run_id}:{digest(arguments)}",
            )
            status = await guard.begin(
                key=guard_key,
                fingerprint=digest(arguments),
                ttl_seconds=self._idempotency_ttl,
            )
            if status.outcome is IdempotencyOutcome.COMPLETED:
                await self._audit.record(
                    AuditEventType.TOOL_RESULT,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    run_id=context.run_id,
                    message=f"replayed idempotent result for {tool.name}",
                    data={"tool": tool.name, "idempotent_replay": True},
                )
                data = dict(status.result or {})
                summary = str(data.pop("_summary", f"{tool.name} already completed"))
                return ToolResult.success(summary, data=data)
            if status.outcome is IdempotencyOutcome.IN_FLIGHT:
                await self._audit.record(
                    AuditEventType.TOOL_ERROR,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    run_id=context.run_id,
                    message=f"idempotent operation already in flight for {tool.name}",
                    data={"tool": tool.name, "idempotent_in_flight": True},
                )
                return ToolResult.failure(
                    f"{tool.name} is already running for this request; not repeating it.",
                    data={"tool": tool.name, "idempotent_in_flight": True},
                )

        await self._audit.record(
            AuditEventType.TOOL_CALLED,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            message=f"calling tool {tool.name}",
            data={"tool": tool.name, "arguments": arguments},
        )
        try:
            result = await tool.execute(arguments, context)
        except PermissionDeniedError:
            if guard is not None and guard_key is not None:
                await guard.abort(key=guard_key)
            raise
        except Exception as exc:
            if guard is not None and guard_key is not None:
                await guard.abort(key=guard_key)
            await self._audit.record(
                AuditEventType.TOOL_ERROR,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message=f"tool {tool.name} raised",
                data={"tool": tool.name, "error": str(exc)},
            )
            raise

        if guard is not None and guard_key is not None and result.ok:
            await guard.complete(
                key=guard_key,
                result={**result.data, "_summary": result.summary},
                ttl_seconds=self._idempotency_ttl,
            )

        await self._audit.record(
            AuditEventType.TOOL_RESULT,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            message=result.summary,
            data={"tool": tool.name, "ok": result.ok, "data": result.data},
        )
        local_event = _local_audit_event(tool)
        if local_event is not None:
            await self._audit.record(
                local_event,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message=f"local tool {tool.name} {'succeeded' if result.ok else 'failed'}",
                data={
                    "tool": tool.name,
                    "ok": result.ok,
                    "path": result.data.get("path")
                    or result.data.get("source")
                    or result.data.get("root"),
                },
            )
        semantic_event = _SEMANTIC_AUDIT_EVENTS.get(tool.name) if result.ok else None
        if semantic_event is not None:
            await self._audit.record(
                semantic_event,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message=f"{tool.name} completed",
                data={
                    "tool": tool.name,
                    "path": result.data.get("path"),
                    "format": result.data.get("format"),
                },
            )
        findings = result.data.get("sensitive_findings")
        if result.ok and isinstance(findings, list) and findings:
            await self._audit.record(
                AuditEventType.SECRET_DETECTED,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                message="likely secrets detected in a document",
                data={
                    "tool": tool.name,
                    "path": result.data.get("path"),
                    "kinds": [f.get("kind") for f in findings if isinstance(f, dict)],
                },
            )
        return result

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
