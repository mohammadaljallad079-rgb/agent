"""Memory data contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import MemoryKind
from ..core.ids import PREFIX_MEMORY, new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryEntry(BaseModel):
    """A single durable memory record.

    Scope (tenant/user/run) is part of the record so isolation is enforced at
    the data layer, not only at the API layer.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_MEMORY))
    kind: MemoryKind
    tenant_id: str
    #: None means tenant-scoped (shared within the tenant, e.g. company context).
    user_id: str | None = None
    #: Optional link to the run that produced this memory.
    run_id: str | None = None
    #: Short natural-language key, e.g. "preferred_invoice_language".
    key: str
    content: str
    #: Free-form, non-secret attributes used for filtering.
    attributes: dict[str, Any] = Field(default_factory=dict)
    #: Importance 0..1; used to prioritise what enters the context window.
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    #: Optional expiry for time-bound memories.
    expires_at: datetime | None = None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _utcnow()) >= self.expires_at

    def matches_scope(self, *, tenant_id: str, user_id: str | None) -> bool:
        """Whether this entry is visible to the given scope.

        Tenant-scoped entries (``user_id is None``) are visible to all users in
        the tenant; user-scoped entries only to their owner.
        """
        if self.tenant_id != tenant_id:
            return False
        if self.user_id is None:
            return True
        return self.user_id == user_id


class MemoryQuery(BaseModel):
    """A request to retrieve relevant memories."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str | None = None
    kinds: tuple[MemoryKind, ...] = ()
    key_contains: str | None = None
    text_contains: str | None = None
    limit: int = 10
    min_importance: float = 0.0


class MemoryStore(ABC):
    """Persistence contract for memory entries."""

    @abstractmethod
    async def write(self, entry: MemoryEntry) -> MemoryEntry: ...

    @abstractmethod
    async def get(self, memory_id: str, *, tenant_id: str) -> MemoryEntry | None: ...

    @abstractmethod
    async def query(self, query: MemoryQuery) -> list[MemoryEntry]: ...

    @abstractmethod
    async def delete(self, memory_id: str, *, tenant_id: str) -> bool: ...

    @abstractmethod
    async def purge_expired(self, *, now: datetime | None = None) -> int: ...
