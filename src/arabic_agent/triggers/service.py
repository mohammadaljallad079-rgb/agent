"""Trigger service: create, govern and fire triggers.

This is the one place where "something should start a workflow" becomes a real
:class:`~arabic_agent.workflows.run.WorkflowRun`. Every trigger kind — manual,
cron, webhook, event — converges here and calls the *existing*
:class:`~arabic_agent.api.workflow_service.WorkflowService` to start a run, so
triggered work is executed by the same worker and engine as any other run. There
is no second execution path.

Two guarantees are enforced here rather than left to callers:

* **Authorisation.** Creating/updating a trigger requires ``MANAGE_WORKFLOW``;
  firing one requires ``RUN_WORKFLOW``. A fired run is authorised as the trigger
  owner (their roles and permission ceiling were captured at creation), so a
  webhook or a cron job can never run with more authority than its author had.
* **Idempotency.** Each firing has a cause-derived idempotency key. The workflow
  run is created with that key, so a redelivered webhook, a retried event, or a
  scheduler that crashed after starting a run produces one run, not many.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..audit.base import AuditEventType
from ..core.config import Settings
from ..core.enums import (
    MisfirePolicy,
    PermissionEffect,
    RiskLevel,
    TriggerKind,
)
from ..core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..core.ids import PREFIX_WEBHOOK, new_id
from ..observability.context import correlation_scope
from ..observability.metrics import Metrics
from ..permissions.base import Permission, Principal
from ..workflows.run import WorkflowRun
from .cron import CronExpression, load_timezone, validate_cron
from .models import Trigger, TriggerRun
from .signing import verify_signature

if TYPE_CHECKING:
    from ..api.workflow_service import WorkflowService
    from ..container import Platform
    from .store import TriggerRunStore, TriggerStore

logger = logging.getLogger(__name__)

#: Trigger ids referenced by audit events live under the trigger id; using it as
#: the audit ``run_id`` keeps trigger history queryable with the existing API.
_WEBHOOK_SECRET_BYTES = 32


def _principal_from_trigger(trigger: Trigger) -> Principal:
    granted = (
        frozenset(Permission(p) for p in trigger.granted_permissions)
        if trigger.granted_permissions is not None
        else None
    )
    return Principal(
        tenant_id=trigger.tenant_id,
        user_id=trigger.user_id,
        roles=frozenset(trigger.roles),
        granted_permissions=granted,
    )


class TriggerService:
    """Governs triggers and turns their firings into workflow runs."""

    def __init__(
        self, platform: Platform | None = None, *, settings: Settings | None = None
    ) -> None:
        self._platform = platform
        self._settings = settings
        self._workflows: WorkflowService | None = None
        self._metrics = platform.metrics if platform is not None else Metrics()

    def bind(self, platform: Platform) -> None:
        """Attach the composed platform (breaks the construction cycle)."""
        self._platform = platform
        self._metrics = platform.metrics
        self._workflows = None

    @property
    def _p(self) -> Platform:
        if self._platform is None:  # pragma: no cover - construction invariant
            raise RuntimeError("trigger service is not bound to a platform")
        return self._platform

    @property
    def settings(self) -> Settings:
        return self._settings or self._p.settings

    @property
    def _workflow_service(self) -> WorkflowService:
        # Imported lazily to avoid a module-level cycle (api <-> triggers).
        if self._workflows is None:
            from ..api.workflow_service import WorkflowService

            self._workflows = WorkflowService(self._p, settings=self.settings)
        return self._workflows

    @property
    def _triggers(self) -> TriggerStore:
        return self._p.triggers

    @property
    def _trigger_runs(self) -> TriggerRunStore:
        return self._p.trigger_runs

    # ------------------------------------------------------------------
    # Authoring
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        principal: Principal,
        name: str,
        workflow_id: str,
        kind: TriggerKind,
        cron: str | None = None,
        timezone: str = "UTC",
        misfire_policy: MisfirePolicy = MisfirePolicy.SKIP,
        event_type: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> tuple[Trigger, str | None]:
        """Create a trigger. Returns the trigger and, for webhooks, its secret.

        The webhook signing secret is returned exactly once, here; it is never
        returned by any read endpoint.
        """
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="trigger.create")
        await self._require_runnable_workflow(principal.tenant_id, workflow_id)
        self._validate_kind_fields(kind=kind, cron=cron, event_type=event_type, timezone=timezone)

        now = datetime.now(UTC)
        webhook_id: str | None = None
        webhook_secret: str | None = None
        if kind is TriggerKind.WEBHOOK:
            webhook_id = new_id(PREFIX_WEBHOOK)
            webhook_secret = secrets.token_urlsafe(_WEBHOOK_SECRET_BYTES)

        next_fire_at = (
            self._next_fire(cron, timezone, after=now) if kind is TriggerKind.CRON else None
        )
        trigger = Trigger(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            name=name,
            kind=kind,
            workflow_id=workflow_id,
            cron=cron,
            timezone=timezone,
            misfire_policy=misfire_policy,
            next_fire_at=next_fire_at,
            webhook_id=webhook_id,
            webhook_secret=webhook_secret,
            event_type=event_type,
            inputs=dict(inputs or {}),
            roles=tuple(sorted(principal.roles)),
            granted_permissions=(
                tuple(sorted(p.value for p in principal.granted_permissions))
                if principal.granted_permissions is not None
                else None
            ),
        )
        trigger = await self._triggers.create(trigger)
        await self._audit(
            AuditEventType.TRIGGER_CREATED,
            trigger,
            principal,
            message=f"trigger '{name}' created",
            data={"trigger_id": trigger.id, "kind": kind.value},
        )
        return trigger, webhook_secret

    async def update(
        self,
        *,
        principal: Principal,
        trigger_id: str,
        name: str | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        misfire_policy: MisfirePolicy | None = None,
        event_type: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> Trigger:
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="trigger.update")
        trigger = await self._triggers.get(trigger_id, tenant_id=principal.tenant_id)
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if inputs is not None:
            updates["inputs"] = dict(inputs)
        if misfire_policy is not None:
            updates["misfire_policy"] = misfire_policy
        if timezone is not None:
            load_timezone(timezone)  # validate eagerly
            updates["timezone"] = timezone
        if cron is not None:
            if trigger.kind is not TriggerKind.CRON:
                raise ValidationError("only cron triggers have a schedule")
            updates["cron"] = cron
        if event_type is not None:
            if trigger.kind is not TriggerKind.EVENT:
                raise ValidationError("only event triggers subscribe to an event type")
            updates["event_type"] = event_type

        if trigger.kind is TriggerKind.CRON:
            effective_cron = updates.get("cron", trigger.cron)
            effective_tz = updates.get("timezone", trigger.timezone)
            self._validate_kind_fields(
                kind=TriggerKind.CRON,
                cron=effective_cron,
                event_type=None,
                timezone=effective_tz,
            )
            # Recompute the next occurrence whenever the schedule changes.
            if "cron" in updates or "timezone" in updates:
                updates["next_fire_at"] = self._next_fire(
                    effective_cron, effective_tz, after=datetime.now(UTC)
                )
        elif trigger.kind is TriggerKind.EVENT:
            effective_event = updates.get("event_type", trigger.event_type)
            if not effective_event:
                raise ValidationError("event trigger must set event_type")

        if not updates:
            return trigger
        updated = await self._triggers.update(
            trigger.model_copy(update={**updates, "updated_at": datetime.now(UTC)}),
            expected_version=trigger.version,
        )
        await self._audit(
            AuditEventType.TRIGGER_UPDATED,
            updated,
            principal,
            message=f"trigger '{updated.name}' updated",
            data={"trigger_id": updated.id},
        )
        return updated

    async def set_enabled(self, *, principal: Principal, trigger_id: str, enabled: bool) -> Trigger:
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="trigger.enable")
        trigger = await self._triggers.get(trigger_id, tenant_id=principal.tenant_id)
        if trigger.enabled == enabled:
            return trigger
        updates: dict[str, Any] = {"enabled": enabled, "updated_at": datetime.now(UTC)}
        if enabled and trigger.kind is TriggerKind.CRON:
            # Re-enabling resumes from now; it does not replay the disabled gap.
            updates["next_fire_at"] = self._next_fire(
                trigger.cron, trigger.timezone, after=datetime.now(UTC)
            )
        updated = await self._triggers.update(
            trigger.model_copy(update=updates), expected_version=trigger.version
        )
        await self._audit(
            AuditEventType.TRIGGER_ENABLED if enabled else AuditEventType.TRIGGER_DISABLED,
            updated,
            principal,
            message=f"trigger '{updated.name}' {'enabled' if enabled else 'disabled'}",
            data={"trigger_id": updated.id},
        )
        return updated

    async def delete(self, *, principal: Principal, trigger_id: str) -> None:
        self._authorize(principal, Permission.MANAGE_WORKFLOW, tool_name="trigger.delete")
        trigger = await self._triggers.get(trigger_id, tenant_id=principal.tenant_id)
        await self._triggers.delete(trigger_id, tenant_id=principal.tenant_id)
        await self._audit(
            AuditEventType.TRIGGER_DELETED,
            trigger,
            principal,
            message=f"trigger '{trigger.name}' deleted",
            data={"trigger_id": trigger.id},
        )

    async def get(self, trigger_id: str, *, tenant_id: str) -> Trigger:
        return await self._triggers.get(trigger_id, tenant_id=tenant_id)

    async def list_triggers(
        self,
        *,
        tenant_id: str,
        kind: TriggerKind | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Trigger]:
        return await self._triggers.list_triggers(
            tenant_id=tenant_id, kind=kind, workflow_id=workflow_id, limit=limit, offset=offset
        )

    async def list_firings(
        self, *, tenant_id: str, trigger_id: str, limit: int = 50
    ) -> list[TriggerRun]:
        # Ensures the trigger exists and belongs to the tenant before listing.
        await self._triggers.get(trigger_id, tenant_id=tenant_id)
        return await self._trigger_runs.list_for_trigger(
            tenant_id=tenant_id, trigger_id=trigger_id, limit=limit
        )

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------
    async def fire_manual(
        self,
        *,
        principal: Principal,
        trigger_id: str,
        inputs: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WorkflowRun:
        """Fire a trigger on demand, as the calling principal."""
        trigger = await self._triggers.get(trigger_id, tenant_id=principal.tenant_id)
        if not trigger.enabled:
            raise ConflictError("trigger is disabled", details={"trigger_id": trigger.id})
        cause = f"manual:{idempotency_key}" if idempotency_key else f"manual:{new_id('m')}"
        return await self._fire(
            trigger,
            cause=cause,
            principal=principal,
            extra_inputs=inputs,
            scheduled_for=None,
        )

    async def fire_webhook(
        self,
        *,
        webhook_id: str,
        body: bytes,
        timestamp: str,
        signature: str,
        delivery_id: str | None = None,
    ) -> WorkflowRun:
        """Authenticate and fire a webhook trigger.

        Raises :class:`NotFoundError` for an unknown webhook id and
        :class:`PermissionDeniedError` for a bad signature, so the HTTP layer can
        answer 404 and 401 respectively.
        """
        trigger = await self._triggers.get_by_webhook_id(webhook_id)
        if trigger is None or trigger.kind is not TriggerKind.WEBHOOK:
            raise NotFoundError("unknown webhook", details={"webhook_id": webhook_id})
        if not trigger.enabled:
            self._metrics.webhook_rejected(reason="disabled")
            raise ConflictError("webhook trigger is disabled", details={"webhook_id": webhook_id})
        assert trigger.webhook_secret is not None
        if not verify_signature(
            trigger.webhook_secret,
            timestamp=timestamp,
            body=body,
            signature=signature,
            tolerance_seconds=self.settings.webhook_tolerance_seconds,
        ):
            self._metrics.webhook_rejected(reason="bad_signature")
            await self._p.audit.record(
                AuditEventType.WEBHOOK_REJECTED,
                tenant_id=trigger.tenant_id,
                user_id=trigger.user_id,
                run_id=trigger.id,
                message="webhook signature rejected",
                data={"webhook_id": webhook_id},
            )
            raise PermissionDeniedError(
                "invalid webhook signature", details={"webhook_id": webhook_id}
            )
        cause = f"webhook:{delivery_id}" if delivery_id else f"webhook:{new_id('d')}"
        return await self._fire(
            trigger,
            cause=cause,
            principal=_principal_from_trigger(trigger),
            extra_inputs=None,
            scheduled_for=None,
        )

    async def fire_event(
        self,
        *,
        event_type: str,
        tenant_id: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> list[WorkflowRun]:
        """Fire every enabled event trigger subscribed to ``event_type``.

        ``tenant_id`` scopes delivery: an event only ever reaches triggers in the
        tenant that produced it.
        """
        triggers = await self._triggers.list_event_triggers(
            tenant_id=tenant_id, event_type=event_type
        )
        runs: list[WorkflowRun] = []
        for trigger in triggers:
            cause = f"event:{event_id}" if event_id else f"event:{new_id('e')}"
            merged = {**dict(payload or {}), **trigger.inputs}
            try:
                runs.append(
                    await self._fire(
                        trigger,
                        cause=cause,
                        principal=_principal_from_trigger(trigger),
                        extra_inputs=merged,
                        scheduled_for=None,
                    )
                )
            except (PermissionDeniedError, ConflictError) as exc:
                logger.warning("event trigger %s did not fire: %s", trigger.id, exc)
        return runs

    async def fire_cron(self, trigger: Trigger, *, scheduled_for: datetime) -> WorkflowRun | None:
        """Fire a cron trigger for a specific scheduled occurrence.

        Called by the scheduler after it has claimed the occurrence. The cause
        is the scheduled instant, so the same occurrence can never start two
        runs even across scheduler restarts.
        """
        if not trigger.enabled:
            return None
        epoch = int(scheduled_for.timestamp())
        return await self._fire(
            trigger,
            cause=f"cron:{epoch}",
            principal=_principal_from_trigger(trigger),
            extra_inputs=None,
            scheduled_for=scheduled_for,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _fire(
        self,
        trigger: Trigger,
        *,
        cause: str,
        principal: Principal,
        extra_inputs: dict[str, Any] | None,
        scheduled_for: datetime | None,
    ) -> WorkflowRun:
        idem = f"trigger:{trigger.id}:{cause}"
        # Fast path: this exact firing already produced a run.
        existing = await self._trigger_runs.get_by_idempotency_key(
            tenant_id=trigger.tenant_id, key=idem
        )
        if existing is not None and existing.workflow_run_id is not None:
            return await self._p.workflow_runs.get_run(
                existing.workflow_run_id, tenant_id=trigger.tenant_id
            )

        merged = {**trigger.inputs, **(extra_inputs or {})}
        with correlation_scope(
            tenant_id=trigger.tenant_id,
            user_id=trigger.user_id,
            trigger_id=trigger.id,
        ):
            try:
                run = await self._workflow_service.start_run(
                    principal=principal,
                    workflow_id=trigger.workflow_id,
                    inputs=merged,
                    idempotency_key=idem,
                )
            except (PermissionDeniedError, ConflictError, ValidationError) as exc:
                await self._record_failure(trigger, cause, exc)
                raise

            await self._record_firing(trigger, run, cause, scheduled_for)
            await self._record_success(trigger, run)
        self._metrics.trigger_fired(kind=trigger.kind.value, outcome="started")
        return run

    async def _record_firing(
        self,
        trigger: Trigger,
        run: WorkflowRun,
        cause: str,
        scheduled_for: datetime | None,
    ) -> None:
        idem = f"trigger:{trigger.id}:{cause}"
        try:
            await self._trigger_runs.create(
                TriggerRun(
                    tenant_id=trigger.tenant_id,
                    trigger_id=trigger.id,
                    workflow_id=trigger.workflow_id,
                    kind=trigger.kind,
                    workflow_run_id=run.id,
                    scheduled_for=scheduled_for,
                    idempotency_key=idem,
                    detail=f"{trigger.kind.value} fired",
                )
            )
        except ConflictError:
            # Another firing of the same cause won the race; the workflow run's
            # own idempotency key already guarantees a single run.
            logger.info("trigger %s firing already recorded", trigger.id)
        await self._p.audit.record(
            AuditEventType.TRIGGER_FIRED,
            tenant_id=trigger.tenant_id,
            user_id=trigger.user_id,
            run_id=trigger.id,
            message=f"trigger '{trigger.name}' fired",
            data={
                "trigger_id": trigger.id,
                "workflow_run_id": run.id,
                "kind": trigger.kind.value,
                "cause": cause,
            },
        )

    async def _record_success(self, trigger: Trigger, run: WorkflowRun) -> None:
        now = datetime.now(UTC)
        updated = trigger.model_copy(
            update={
                "last_fire_at": now,
                "last_run_id": run.id,
                "fire_count": trigger.fire_count + 1,
                "last_error": None,
                "updated_at": now,
            }
        )
        try:
            await self._triggers.update(updated, expected_version=trigger.version)
        except ConflictError:
            logger.info("trigger %s state advanced concurrently", trigger.id)

    async def _record_failure(self, trigger: Trigger, cause: str, exc: Exception) -> None:
        self._metrics.trigger_fired(kind=trigger.kind.value, outcome="failed")
        await self._p.audit.record(
            AuditEventType.TRIGGER_FIRE_FAILED,
            tenant_id=trigger.tenant_id,
            user_id=trigger.user_id,
            run_id=trigger.id,
            message=f"trigger '{trigger.name}' failed to fire: {exc}",
            data={"trigger_id": trigger.id, "cause": cause},
        )
        now = datetime.now(UTC)
        updated = trigger.model_copy(update={"last_error": str(exc)[:500], "updated_at": now})
        try:
            await self._triggers.update(updated, expected_version=trigger.version)
        except ConflictError:
            logger.info("trigger %s state advanced concurrently", trigger.id)

    # ------------------------------------------------------------------
    # Validation / helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_kind_fields(
        *,
        kind: TriggerKind,
        cron: str | None,
        event_type: str | None,
        timezone: str,
    ) -> None:
        load_timezone(timezone)
        if kind is TriggerKind.CRON:
            if not cron:
                raise ValidationError("cron trigger must set a schedule")
            validate_cron(cron)
        elif kind is TriggerKind.EVENT:
            if not event_type:
                raise ValidationError("event trigger must set event_type")
        elif kind is TriggerKind.WEBHOOK:
            if cron is not None or event_type is not None:
                raise ValidationError("webhook trigger takes neither a schedule nor an event type")
        elif kind is TriggerKind.MANUAL:
            if cron is not None or event_type is not None:
                raise ValidationError("manual trigger takes neither a schedule nor an event type")

    def _next_fire(self, cron: str | None, timezone: str, *, after: datetime) -> datetime:
        assert cron is not None
        expression: CronExpression = validate_cron(cron)
        return expression.next_after(after, tz=load_timezone(timezone))

    async def _require_runnable_workflow(self, tenant_id: str, workflow_id: str) -> None:
        workflow = await self._p.workflows.get(workflow_id, tenant_id=tenant_id)
        if not workflow.is_published or workflow.latest_version_id is None:
            raise ConflictError("workflow has no published version to trigger")

    def _authorize(self, principal: Principal, permission: Permission, *, tool_name: str) -> None:
        decision = self._p.policy.evaluate(
            principal, tool_name=tool_name, permission=permission, risk=RiskLevel.MEDIUM
        )
        if decision.effect is PermissionEffect.DENY:
            raise PermissionDeniedError(
                f"not permitted: {permission.value}",
                details={"permission": permission.value, "reason": decision.reason},
            )
        if decision.effect is PermissionEffect.REQUIRES_APPROVAL:
            raise PermissionDeniedError(
                f"approval required for {permission.value}",
                details={"permission": permission.value, "reason": decision.reason},
            )

    async def _audit(
        self,
        event_type: AuditEventType,
        trigger: Trigger,
        principal: Principal,
        *,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._p.audit.record(
            event_type,
            tenant_id=trigger.tenant_id,
            user_id=principal.user_id,
            run_id=trigger.id,
            message=message,
            data=data,
        )
