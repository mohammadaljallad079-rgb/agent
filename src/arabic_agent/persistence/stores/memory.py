"""Postgres-backed memory store.

A write upserts on the ``(tenant_id, user_id, kind, key)`` unique constraint, so
re-writing the same fact updates it in place rather than duplicating. Tenant and
user scope are enforced in the SQL predicate, not just in Python.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.elements import ColumnElement

from ...memory.base import MemoryEntry, MemoryQuery, MemoryStore
from ...persistence.database import Database
from ...persistence.mapping import memory_from_row, memory_to_row
from ...persistence.schema import MemoryRow


class PostgresMemoryStore(MemoryStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        async with self._db.transaction() as session:
            existing = (
                await session.execute(
                    select(MemoryRow).where(
                        MemoryRow.tenant_id == entry.tenant_id,
                        MemoryRow.user_id.is_(None)
                        if entry.user_id is None
                        else MemoryRow.user_id == entry.user_id,
                        MemoryRow.kind == entry.kind.value,
                        MemoryRow.key == entry.key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.content = entry.content
                existing.attributes = dict(entry.attributes)
                existing.importance = entry.importance
                existing.updated_at = entry.updated_at
                existing.expires_at = entry.expires_at
                existing.run_id = entry.run_id
                await session.flush()
                return memory_from_row(existing)
            session.add(MemoryRow(**memory_to_row(entry)))
        return entry

    async def get(self, memory_id: str, *, tenant_id: str) -> MemoryEntry | None:
        async with self._db.transaction() as session:
            row = await session.get(MemoryRow, memory_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return memory_from_row(row)

    async def query(self, query: MemoryQuery) -> list[MemoryEntry]:
        now = datetime.now(UTC)
        # Tenant-scoped entries (user_id IS NULL) are visible to everyone in the
        # tenant; user-scoped entries only to their owner.
        scope: ColumnElement[bool] = MemoryRow.user_id.is_(None)
        if query.user_id is not None:
            scope = or_(scope, MemoryRow.user_id == query.user_id)
        stmt = select(MemoryRow).where(
            MemoryRow.tenant_id == query.tenant_id,
            scope,
            or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > now),
            MemoryRow.importance >= query.min_importance,
        )
        if query.kinds:
            stmt = stmt.where(MemoryRow.kind.in_([k.value for k in query.kinds]))
        if query.key_contains is not None:
            stmt = stmt.where(MemoryRow.key.contains(query.key_contains))
        if query.text_contains is not None:
            stmt = stmt.where(MemoryRow.content.contains(query.text_contains))
        stmt = stmt.order_by(MemoryRow.importance.desc(), MemoryRow.updated_at.desc()).limit(
            query.limit
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [memory_from_row(r) for r in rows]

    async def delete(self, memory_id: str, *, tenant_id: str) -> bool:
        async with self._db.transaction() as session:
            result = await session.execute(
                delete(MemoryRow).where(MemoryRow.id == memory_id, MemoryRow.tenant_id == tenant_id)
            )
        return cast(CursorResult[Any], result).rowcount > 0

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        async with self._db.transaction() as session:
            result = await session.execute(
                delete(MemoryRow).where(
                    MemoryRow.expires_at.is_not(None), MemoryRow.expires_at <= moment
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def touch(self, memory_id: str, *, tenant_id: str) -> None:
        """Utility used by tests/tools: bump ``updated_at`` without other changes."""
        async with self._db.transaction() as session:
            await session.execute(
                update(MemoryRow)
                .where(MemoryRow.id == memory_id, MemoryRow.tenant_id == tenant_id)
                .values(updated_at=func.now())
            )
