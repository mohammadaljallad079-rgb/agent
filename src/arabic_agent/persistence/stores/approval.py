"""Postgres-backed approval store.

``resolve`` is a single conditional ``UPDATE ... WHERE status = 'pending'``, so
two concurrent decisions (or a duplicate click) cannot both succeed: the loser
gets a rowcount of 0 and a :class:`NotFoundError`. This is what makes
"approve once, resume once" hold under concurrency and across restarts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from ...core.enums import ApprovalStatus
from ...core.errors import NotFoundError
from ...permissions.approvals import Approval, ApprovalStore
from ...persistence.database import Database
from ...persistence.mapping import approval_from_row, approval_to_row
from ...persistence.schema import ApprovalRow


class PostgresApprovalStore(ApprovalStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, approval: Approval) -> Approval:
        async with self._db.transaction() as session:
            session.add(ApprovalRow(**approval_to_row(approval)))
        return approval

    async def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        async with self._db.transaction() as session:
            row = await session.get(ApprovalRow, approval_id)
        if row is None or row.tenant_id != tenant_id:
            # Do not distinguish "missing" from "other tenant" to avoid leaking
            # the existence of another tenant's records.
            raise NotFoundError("approval not found", details={"approval_id": approval_id})
        return approval_from_row(row)

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
        now = datetime.now(UTC)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.id == approval_id,
                    ApprovalRow.tenant_id == tenant_id,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                )
                .values(
                    status=status.value,
                    resolved_at=now,
                    resolved_by=resolved_by,
                    decision_reason=reason,
                )
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                row = await session.get(ApprovalRow, approval_id)
                if row is None or row.tenant_id != tenant_id:
                    raise NotFoundError("approval not found", details={"approval_id": approval_id})
                raise NotFoundError(
                    "approval already resolved",
                    details={"approval_id": approval_id, "status": row.status},
                )
            row = await session.get(ApprovalRow, approval_id)
        assert row is not None
        return approval_from_row(row)

    async def list_pending(self, *, tenant_id: str) -> list[Approval]:
        async with self._db.transaction() as session:
            rows = (
                (
                    await session.execute(
                        select(ApprovalRow)
                        .where(
                            ApprovalRow.tenant_id == tenant_id,
                            ApprovalRow.status == ApprovalStatus.PENDING.value,
                        )
                        .order_by(ApprovalRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return [approval_from_row(r) for r in rows]

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[Approval]:
        async with self._db.transaction() as session:
            rows = (
                (
                    await session.execute(
                        select(ApprovalRow)
                        .where(ApprovalRow.tenant_id == tenant_id, ApprovalRow.run_id == run_id)
                        .order_by(ApprovalRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
        return [approval_from_row(r) for r in rows]
