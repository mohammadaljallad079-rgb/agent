"""Triggers: start workflow runs without a human calling the API.

A trigger is a durable, tenant-owned rule. Four kinds share one execution path:
manual, cron, webhook and event. Every firing becomes a normal persistent
workflow run created through the existing :class:`WorkflowService`, so the
existing worker and workflow engine execute it — there is no second engine.

* :mod:`~arabic_agent.triggers.cron` — dependency-free cron parsing/evaluation.
* :mod:`~arabic_agent.triggers.scheduler` — claims due cron triggers durably.
* :mod:`~arabic_agent.triggers.service` — governance and the single firing path.
* :mod:`~arabic_agent.triggers.signing` — HMAC webhook signature verification.
* :mod:`~arabic_agent.triggers.events` — in-process domain event bus.
"""

from __future__ import annotations

from .cron import CronExpression, load_timezone, validate_cron
from .events import DomainEvent, EventBus
from .models import Trigger, TriggerRun
from .scheduler import TriggerScheduler
from .service import TriggerService
from .signing import compute_signature, verify_signature
from .store import (
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
    TriggerRunStore,
    TriggerStore,
)

__all__ = [
    "CronExpression",
    "DomainEvent",
    "EventBus",
    "InMemoryTriggerRunStore",
    "InMemoryTriggerStore",
    "Trigger",
    "TriggerRun",
    "TriggerRunStore",
    "TriggerScheduler",
    "TriggerService",
    "TriggerStore",
    "compute_signature",
    "load_timezone",
    "validate_cron",
    "verify_signature",
]
