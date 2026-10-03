"""In-memory memory store with scope isolation and expiry."""

from __future__ import annotations

import asyncio
from datetime import datetime

from .base import MemoryEntry, MemoryQuery, MemoryStore


class InMemoryMemoryStore(MemoryStore):
    """Process-local store. Cloud deployments replace it with Postgres/pgvector.

    Scope isolation is enforced here as well as in the policy layer, so a
    retrieval bug cannot leak another tenant's or user's memory.
    """

    def __init__(self) -> None:
        self._items: dict[str, MemoryEntry] = {}
        self._lock = asyncio.Lock()

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        async with self._lock:
            existing = self._find_by_key(entry)
            if existing is not None:
                merged = existing.model_copy(
                    update={
                        "content": entry.content,
                        "attributes": entry.attributes,
                        "importance": entry.importance,
                        "updated_at": entry.updated_at,
                        "expires_at": entry.expires_at,
                    }
                )
                self._items[existing.id] = merged
                return merged
            self._items[entry.id] = entry
            return entry

    def _find_by_key(self, entry: MemoryEntry) -> MemoryEntry | None:
        for candidate in self._items.values():
            if (
                candidate.kind == entry.kind
                and candidate.tenant_id == entry.tenant_id
                and candidate.user_id == entry.user_id
                and candidate.key == entry.key
            ):
                return candidate
        return None

    async def get(self, memory_id: str, *, tenant_id: str) -> MemoryEntry | None:
        async with self._lock:
            entry = self._items.get(memory_id)
        if entry is None or entry.tenant_id != tenant_id:
            return None
        return entry

    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        async with self._lock:
            candidates = list(self._items.values())
        results = [
            entry
            for entry in candidates
            if entry.matches_scope(tenant_id=query.tenant_id, user_id=query.user_id)
            and not entry.is_expired()
            and (not query.kinds or entry.kind in query.kinds)
            and (query.key_contains is None or query.key_contains in entry.key)
            and (query.text_contains is None or query.text_contains in entry.content)
            and entry.importance >= query.min_importance
        ]
        results.sort(key=lambda e: (-e.importance, -e.updated_at.timestamp()))
        return results[: query.limit]

    async def delete(self, memory_id: str, *, tenant_id: str) -> bool:
        async with self._lock:
            entry = self._items.get(memory_id)
            if entry is None or entry.tenant_id != tenant_id:
                return False
            del self._items[memory_id]
            return True

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        async with self._lock:
            expired = [mid for mid, entry in self._items.items() if entry.is_expired(now=now)]
            for mid in expired:
                del self._items[mid]
        return len(expired)
