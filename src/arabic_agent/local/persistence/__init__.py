"""Durable local persistence for the offline runtime.

The cloud runtime's source of truth is PostgreSQL. The local runtime may run on
a machine with no network and no Postgres, so it needs its own durable store: an
embedded, transactional database that survives process termination and machine
restart. SQLite is used (see :mod:`arabic_agent.local.persistence.database` for
the rationale).

This package deliberately **reuses the existing persistence contracts** — the
store ABCs in ``workflows.store``, ``permissions.approvals``, ``audit.base`` and
``local.sync`` — and implements them against SQLite. The workflow engine, the
permission/approval layer and the sync coordinator are unchanged; only the
storage backend differs. The local schema is a deliberate *subset* of the cloud
schema: only the state the local runtime actually needs is persisted, not a
duplicate of every cloud table.

Nothing here is imported by the cloud runtime, and nothing here imports the
agent core's composition root.
"""

from __future__ import annotations

from .database import LocalDatabase, local_database_url
from .schema import LocalBase

__all__ = ["LocalBase", "LocalDatabase", "local_database_url"]
