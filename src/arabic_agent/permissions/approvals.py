"""Human-in-the-loop approvals.

An approval is a first-class, persisted entity — not a chat message. It records
who asked, for which tool and arguments, when it expires, and who resolved it,
so the whole exchange lands in the audit log.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ApprovalStatus
from ..core.errors import NotFoundError
from ..core.ids import PREFIX_APPROVAL, new_id
from ..core.redaction import redact
from .base import Permission


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Approval(BaseModel):
    """A request for a human to authorise a sensitive operation."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_APPROVAL))
    tenant_id: str
    user_id: str
    run_id: str
    tool_name: str
    permission: Permission
    #: Redacted tool arguments, safe to persist and display.
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Localized, human-readable prompt (Arabic-first by default).
    summary: str
    #: Structured, non-secret, read-only preview of the operation's concrete
    #: effect, produced by the tool before it runs (e.g. the target file and the
    #: exact text change). Empty when the tool has no preview to offer. This is
    #: what makes the approval a real decision rather than a yes/no on a name.
    preview: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    decision_reason: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.expires_at

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        tool_name: str,
        permission: Permission,
        summary: str,
        arguments: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
        ttl_seconds: int = 3600,
    ) -> Approval:
        now = _utcnow()
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            tool_name=tool_name,
            permission=permission,
            arguments=redact(arguments or {}),
            summary=summary,
            preview=redact(preview or {}),
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )


class ApprovalStore(ABC):
    """Persistence contract for approvals."""

    @abstractmethod
    async def add(self, approval: Approval) -> Approval: ...

    @abstractmethod
    async def get(self, approval_id: str, *, tenant_id: str) -> Approval: ...

    @abstractmethod
    async def resolve(
        self,
        approval_id: str,
        *,
        tenant_id: str,
        status: ApprovalStatus,
        resolved_by: str,
        reason: str | None = None,
    ) -> Approval: ...

    @abstractmethod
    async def list_pending(self, *, tenant_id: str) -> list[Approval]: ...

    @abstractmethod
    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[Approval]: ...


class InMemoryApprovalStore(ApprovalStore):
    """Process-local approval store.

    Suitable for the local/offline runtime and tests. Cloud deployments swap in
    a Postgres-backed implementation behind the same interface.
    """

    def __init__(self) -> None:
        self._items: dict[str, Approval] = {}
        self._lock = asyncio.Lock()

    async def add(self, approval: Approval) -> Approval:
        async with self._lock:
            self._items[approval.id] = approval
        return approval

    async def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        async with self._lock:
            approval = self._items.get(approval_id)
        if approval is None or approval.tenant_id != tenant_id:
            # Do not distinguish "missing" from "other tenant" to avoid
            # leaking the existence of another tenant's records.
            raise NotFoundError("approval not found", details={"approval_id": approval_id})
        return approval

    async def resolve(
        self,
        approval_id: str,
        *,
        tenant_id: str,
        status: ApprovalStatus,
        resolved_by: str,
        reason: str | None = None,
    ) -> Approval:
        if status is ApprovalStatus.PENDING:
            raise ValueError("cannot resolve an approval back to pending")
        async with self._lock:
            current = self._items.get(approval_id)
            if current is None or current.tenant_id != tenant_id:
                raise NotFoundError("approval not found", details={"approval_id": approval_id})
            if not current.is_pending:
                raise NotFoundError(
                    "approval already resolved",
                    details={"approval_id": approval_id, "status": current.status.value},
                )
            updated = current.model_copy(
                update={
                    "status": status,
                    "resolved_at": _utcnow(),
                    "resolved_by": resolved_by,
                    "decision_reason": reason,
                }
            )
            self._items[approval_id] = updated
        return updated

    async def list_pending(self, *, tenant_id: str) -> list[Approval]:
        async with self._lock:
            return [a for a in self._items.values() if a.tenant_id == tenant_id and a.is_pending]

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[Approval]:
        async with self._lock:
            return [
                a for a in self._items.values() if a.tenant_id == tenant_id and a.run_id == run_id
            ]
