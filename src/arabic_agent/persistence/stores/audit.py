"""Postgres-backed audit sink.

Implements the existing :class:`~arabic_agent.audit.base.AuditSink` contract, so
it plugs into the fan-out :class:`AuditLog` unchanged. Events are already
redacted before they reach a sink (the event constructor redacts payloads), so
nothing here can persist a secret.

Sequence assignment is owned here, not by an in-process counter. The API and a
worker both write audit events for the same run, and the schema enforces
``UNIQUE (run_id, sequence)``; two processes deriving ``sequence`` from their own
memory would collide. Instead the sequence is computed from the durable maximum
for the run, and a lost cross-process race is retried — the same
optimistic-insert pattern used for runs.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ...audit.base import AuditEvent, AuditSink
from ...persistence.database import Database
from ...persistence.mapping import audit_from_row, audit_to_row
from ...persistence.schema import AuditEventRow

#: Bounded retries for a cross-process sequence race before giving up.
_SEQUENCE_RETRIES = 5


class PostgresAuditStore(AuditSink):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def next_sequence(self, *, run_id: str) -> int:
        """The next sequence for a run, derived from durable storage."""
        async with self._db.transaction() as session:
            highest = (
                await session.execute(
                    select(func.max(AuditEventRow.sequence)).where(AuditEventRow.run_id == run_id)
                )
            ).scalar_one_or_none()
        return int(highest or 0) + 1

    async def emit(self, event: AuditEvent) -> None:
        """Persist an event, retrying the sequence if another process won the race.

        The caller (``AuditLog``) resolves the sequence up front; if a concurrent
        writer inserted that number first, the unique constraint fires and we
        recompute from the new durable maximum. The event's other fields are
        preserved — only the sequence changes.
        """
        current = event
        last_error: IntegrityError | None = None
        for _ in range(_SEQUENCE_RETRIES):
            try:
                async with self._db.transaction() as session:
                    session.add(AuditEventRow(**audit_to_row(current)))
                return
            except IntegrityError as exc:
                last_error = exc
                next_sequence = await self.next_sequence(run_id=current.run_id)
                if next_sequence <= current.sequence:
                    # Someone else advanced it; retry with a fresh value.
                    next_sequence = current.sequence + 1
                current = current.model_copy(update={"sequence": next_sequence})
        # Exhausted retries: surface the failure so ``AuditLog`` logs it rather
        # than dropping the event silently.
        assert last_error is not None
        raise last_error

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
