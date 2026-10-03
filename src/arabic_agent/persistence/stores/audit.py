"""Postgres-backed audit sink.

Implements the existing :class:`~arabic_agent.audit.base.AuditSink` contract, so
it plugs into the fan-out :class:`AuditLog` unchanged. Events are already
redacted before they reach a sink (the event constructor redacts payloads), so
nothing here can persist a secret.
"""

from __future__ import annotations

from sqlalchemy import select

from ...audit.base import AuditEvent, AuditSink
from ...persistence.database import Database
from ...persistence.mapping import audit_from_row, audit_to_row
from ...persistence.schema import AuditEventRow


class PostgresAuditStore(AuditSink):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def emit(self, event: AuditEvent) -> None:
        async with self._db.transaction() as session:
            session.add(AuditEventRow(**audit_to_row(event)))

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        async with self._db.transaction() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditEventRow)
                        .where(
                            AuditEventRow.tenant_id == tenant_id,
                            AuditEventRow.run_id == run_id,
                        )
                        .order_by(AuditEventRow.timestamp, AuditEventRow.sequence)
                    )
                )
                .scalars()
                .all()
            )
        return [audit_from_row(r) for r in rows]

    async def list_for_tenant(
        self, *, tenant_id: str, limit: int = 200, offset: int = 0
    ) -> list[AuditEvent]:
        async with self._db.transaction() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditEventRow)
                        .where(AuditEventRow.tenant_id == tenant_id)
                        .order_by(AuditEventRow.timestamp.desc(), AuditEventRow.sequence.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return [audit_from_row(r) for r in rows]
