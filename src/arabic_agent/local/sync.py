"""Offline synchronization queue.

When the runtime works offline it still needs to remember *what it would sync*
if it were online — workflow status, task status, approved configuration,
selected memory, audit metadata and device status. It must not sync arbitrary
files, secrets or sensitive content, and the local operation must never block on
the network.

This module provides:

* :class:`SyncAction` — a queued, boundary-checked unit of synchronization.
* :class:`SyncQueue` — a durable queue ABC with an in-memory implementation.
* :class:`SyncCoordinator` — enqueues actions (checking the data boundary and
  audit) and, when online and a transport is configured, flushes them.

There is deliberately **no fake cloud**: :class:`SyncTransport` is an ABC. With
no transport configured, :meth:`SyncCoordinator.flush` is an honest no-op that
leaves actions ``PENDING`` and reports that nothing was uploaded.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import DataSensitivity, SyncKind, SyncStatus
from ..core.ids import PREFIX_SYNC, new_id
from ..core.redaction import redact
from .boundary import DataBoundaryPolicy


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Categories whose payloads are considered non-sensitive metadata by default.
_METADATA_KINDS = frozenset(
    {
        SyncKind.WORKFLOW_STATUS,
        SyncKind.TASK_STATUS,
        SyncKind.AUDIT_METADATA,
        SyncKind.DEVICE_STATUS,
    }
)


class SyncAction(BaseModel):
    """A single queued synchronization action."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_SYNC))
    tenant_id: str
    user_id: str
    device_id: str
    kind: SyncKind
    #: Redacted payload. Never contains secrets; the coordinator redacts and the
    #: boundary policy rejects anything that may not leave the machine.
    payload: dict[str, object] = Field(default_factory=dict)
    status: SyncStatus = SyncStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    attempts: int = 0
    error: str | None = None

    def mark(self, status: SyncStatus, *, error: str | None = None) -> SyncAction:
        return self.model_copy(
            update={
                "status": status,
                "updated_at": _utcnow(),
                "attempts": self.attempts + (1 if status is SyncStatus.FAILED else 0),
                "error": error,
            }
        )


class SyncQueue(ABC):
    """Persistence contract for the offline synchronization queue."""

    @abstractmethod
    async def add(self, action: SyncAction) -> SyncAction: ...

    @abstractmethod
    async def get(self, action_id: str, *, tenant_id: str) -> SyncAction: ...

    @abstractmethod
    async def list_pending(self, *, tenant_id: str) -> list[SyncAction]: ...

    @abstractmethod
    async def list_all(self, *, tenant_id: str) -> list[SyncAction]: ...

    @abstractmethod
    async def update(self, action: SyncAction) -> SyncAction: ...


class InMemorySyncQueue(SyncQueue):
    """Process-local sync queue (local runtime/tests)."""

    def __init__(self) -> None:
        self._items: dict[str, SyncAction] = {}
        self._lock = asyncio.Lock()

    async def add(self, action: SyncAction) -> SyncAction:
        async with self._lock:
            self._items[action.id] = action
        return action

    async def get(self, action_id: str, *, tenant_id: str) -> SyncAction:
        from ..core.errors import NotFoundError

        async with self._lock:
            action = self._items.get(action_id)
        if action is None or action.tenant_id != tenant_id:
            raise NotFoundError("sync action not found", details={"action_id": action_id})
        return action

    async def list_pending(self, *, tenant_id: str) -> list[SyncAction]:
        async with self._lock:
            return [
                a
                for a in self._items.values()
                if a.tenant_id == tenant_id and a.status is SyncStatus.PENDING
            ]

    async def list_all(self, *, tenant_id: str) -> list[SyncAction]:
        async with self._lock:
            return [a for a in self._items.values() if a.tenant_id == tenant_id]

    async def update(self, action: SyncAction) -> SyncAction:
        async with self._lock:
            self._items[action.id] = action
        return action


class SyncTransport(ABC):
    """Uploads a batch of approved synchronization actions to the cloud.

    Not implemented in this phase; the seam is defined so the cloud side can be
    added later without touching the coordinator. A transport must not be given
    anything the boundary policy refused.
    """

    @abstractmethod
    async def upload(self, actions: list[SyncAction]) -> list[str]:
        """Upload actions; return the ids that were accepted."""


class SyncCoordinator:
    """Boundary-checked enqueue plus online flush."""

    def __init__(
        self,
        queue: SyncQueue,
        *,
        boundary: DataBoundaryPolicy,
        transport: SyncTransport | None = None,
    ) -> None:
        self._queue = queue
        self._boundary = boundary
        self._transport = transport

    @property
    def transport_configured(self) -> bool:
        return self._transport is not None

    async def enqueue(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        kind: SyncKind,
        payload: dict[str, object],
    ) -> SyncAction:
        """Queue a synchronization action after a boundary check.

        Metadata kinds are treated as ``INTERNAL``; everything else as
        ``CONFIDENTIAL`` (so it needs an explicit opt-in to leave the machine).
        A refused payload is stored ``REJECTED`` rather than silently dropped,
        so the refusal is visible.
        """
        sensitivity = (
            DataSensitivity.INTERNAL if kind in _METADATA_KINDS else DataSensitivity.CONFIDENTIAL
        )
        from ..core.enums import DataBoundary

        decision = self._boundary.evaluate(boundary=DataBoundary.REMOTE, sensitivity=sensitivity)
        safe_payload = redact(payload)
        action = SyncAction(
            tenant_id=tenant_id,
            user_id=user_id,
            device_id=device_id,
            kind=kind,
            payload=safe_payload,
        )
        if not decision.allowed:
            return await self._queue.add(action.mark(SyncStatus.REJECTED, error=decision.reason))
        return await self._queue.add(action)

    async def flush(self, *, tenant_id: str) -> list[str]:
        """Upload pending actions if a transport exists; else do nothing.

        Returns the ids uploaded. With no transport configured this is a no-op
        that leaves every action ``PENDING`` — it does not fabricate success.
        """
        if self._transport is None:
            return []
        pending = await self._queue.list_pending(tenant_id=tenant_id)
        if not pending:
            return []
        accepted = await self._transport.upload(pending)
        accepted_set = set(accepted)
        for action in pending:
            if action.id in accepted_set:
                await self._queue.update(action.mark(SyncStatus.SYNCED))
            else:
                await self._queue.update(
                    action.mark(SyncStatus.FAILED, error="transport did not accept action")
                )
        return sorted(accepted_set)
