"""Trigger domain models.

A *trigger* is a durable, tenant-owned rule that starts a workflow run without a
human calling the API. Four kinds share one execution path:

* ``MANUAL``  — an explicit "fire now" (an authenticated API call).
* ``CRON``    — a schedule; a scheduler loop claims due triggers and starts runs.
* ``WEBHOOK`` — an inbound HTTP call authenticated by an HMAC signature.
* ``EVENT``   — an internal domain event (e.g. ``customer.created``).

Every kind converges on the same outcome: a normal persistent
:class:`~arabic_agent.workflows.run.WorkflowRun`, created through the existing
:class:`~arabic_agent.api.workflow_service.WorkflowService`. There is no second
execution engine for triggered work — the existing worker and workflow engine do
the executing.

The webhook signing secret is stored (so the HMAC can be verified) but is never
returned by the API and never written to the audit log. See ``ARCHITECTURE.md``
§8b for the trust model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import MisfirePolicy, TriggerKind
from ..core.ids import PREFIX_TRIGGER, PREFIX_TRIGGER_RUN, new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Trigger(BaseModel):
    """A durable rule that starts workflow runs."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_TRIGGER))
    tenant_id: str
    user_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: TriggerKind
    workflow_id: str
    enabled: bool = True

    # --- cron -----------------------------------------------------------
    cron: str | None = Field(default=None, max_length=200)
    timezone: str = Field(default="UTC", max_length=64)
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
    next_fire_at: datetime | None = None

    # --- webhook --------------------------------------------------------
    #: Public identifier used in the webhook URL; not a secret.
    webhook_id: str | None = Field(default=None, max_length=64)
    #: HMAC-SHA256 signing secret. Stored to verify signatures; never exposed.
    webhook_secret: str | None = Field(default=None, max_length=200)

    # --- event ----------------------------------------------------------
    #: Domain event name this trigger subscribes to (e.g. ``customer.created``).
    event_type: str | None = Field(default=None, max_length=120)

    # --- shared ---------------------------------------------------------
    #: Static inputs merged into every run this trigger starts.
    inputs: dict[str, Any] = Field(default_factory=dict)

    #: Principal identity captured at creation, so a fired run can be
    #: authorized without the creator's API key (mirrors ``RunRecord``). These
    #: are not secrets; the run re-checks RUN_WORKFLOW against them at fire time.
    roles: tuple[str, ...] = ()
    granted_permissions: tuple[str, ...] | None = None

    # --- observed state -------------------------------------------------
    last_fire_at: datetime | None = None
    last_run_id: str | None = None
    fire_count: int = 0
    #: Last firing error (bounded, redacted), for operator visibility.
    last_error: str | None = None

    # --- scheduler lease (durable, CAS-protected) -----------------------
    lease_owner: str | None = Field(default=None, max_length=120)
    lease_expires_at: datetime | None = None

    version: int = 1
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def is_due(self, now: datetime) -> bool:
        return (
            self.enabled
            and self.kind is TriggerKind.CRON
            and self.next_fire_at is not None
            and self.next_fire_at <= now
        )

    def lease_free(self, now: datetime) -> bool:
        return self.lease_expires_at is None or self.lease_expires_at <= now

    def public_view(self) -> dict[str, Any]:
        """A serialisation safe to return over HTTP (never includes the secret)."""
        return self.model_dump(exclude={"webhook_secret"})


class TriggerRun(BaseModel):
    """Durable history of one trigger firing.

    ``idempotency_key`` is derived from the firing's cause (a cron occurrence, a
    webhook delivery id, an event id) and is unique per tenant. That uniqueness
    is what makes a redelivered webhook, a retried event, or a scheduler that
    crashed mid-firing start *one* workflow run rather than many.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_TRIGGER_RUN))
    tenant_id: str
    trigger_id: str
    workflow_id: str
    kind: TriggerKind
    #: The run this firing produced (None if the firing was rejected/skipped).
    workflow_run_id: str | None = None
    #: The scheduled instant, for cron firings.
    scheduled_for: datetime | None = None
    idempotency_key: str
    detail: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
