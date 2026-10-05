"""The local durable database: an embedded SQLite store.

Technology choice
-----------------
SQLite is used for local durable state, over the alternatives considered:

* **SQLite** — embedded, transactional (ACID), single file, crash-safe via its
  write-ahead log, ships with the Python standard library, works with no server
  and no network, and runs identically on Windows, macOS and Linux. It is the
  natural embedded companion to the project's existing SQLAlchemy/Alembic
  toolchain, so the stores reuse the same ORM patterns as the cloud runtime.
* **A separate embedded server (e.g. DuckDB, a bundled Postgres)** — heavier,
  adds a second driver and a process to manage, and offers nothing the local
  runtime needs.
* **A hand-rolled file/JSON store** — would re-implement transactions, crash
  recovery and concurrent access badly; rejected.

Reliability is the deciding factor: SQLite's WAL gives atomic commits and
torn-write protection, which is exactly what "survive process termination" needs.

Concurrency model
-----------------
The local runtime is single-process. The engine runs with ``check_same_thread``
disabled (required for the async driver) and each connection enables WAL,
``busy_timeout`` and ``foreign_keys`` so a second local process (e.g. a desktop
shell and a background worker) can share the file without corrupting it.

Migrations
----------
The schema is owned by Alembic, not created ad hoc at startup. :meth:`upgrade`
runs the ``migrations_local`` revision chain programmatically, so a fresh store
is migrated to head and an existing store is upgraded in place. The migration
lives in its own directory and uses its own version table
(``local_alembic_version``) so the local store and the cloud Postgres schema
never share a revision history.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ...core.errors import ConfigurationError

#: Directory (relative to the repository root) holding the local migrations.
LOCAL_MIGRATIONS_DIR = "migrations_local"
#: Alembic version table for the local store, distinct from the cloud one.
LOCAL_VERSION_TABLE = "local_alembic_version"


def local_database_url(db_path: str | Path) -> str:
    """Return the async SQLite URL for ``db_path``.

    Uses ``Path.as_posix`` so the URL is valid on Windows (``C:/...``) as well
    as POSIX; SQLAlchemy's SQLite URL accepts forward slashes on every platform.
    """
    return f"sqlite+aiosqlite:///{Path(db_path).expanduser().as_posix()}"


def default_store_path(store_dir: str | Path) -> Path:
    """Resolve the SQLite file for a store directory, creating nothing yet."""
    base = Path(store_dir).expanduser()
    return base / "local.db"


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Enable WAL, a busy timeout and foreign keys on every connection.

    WAL is the crash-safety mechanism: committed transactions are durable and a
    torn write cannot corrupt the database. ``busy_timeout`` lets a second local
    process wait rather than fail immediately on a locked database.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class LocalDatabase:
    """Owns the async SQLite engine and session factory for the local runtime."""

    def __init__(self, db_path: str | Path, *, echo: bool = False) -> None:
        self._path = Path(db_path).expanduser()
        if self._path.name in {"", "."}:
            raise ConfigurationError("a local database file path is required")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: AsyncEngine = create_async_engine(
            local_database_url(self._path),
            echo=echo,
            # A local SQLite file is served best by no pool: each operation opens
            # and closes its connection, which releases the file handle and the
            # aiosqlite worker thread deterministically (important on Windows,
            # where an open handle blocks file replacement).
            poolclass=NullPool,
        )
        event.listen(self._engine.sync_engine, "connect", _apply_sqlite_pragmas)
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        """Return a new session. Callers own its lifecycle."""
        return self._sessionmaker()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Run a block in a transaction, committing or rolling back atomically."""
        async with self._sessionmaker.begin() as session:
            yield session

    def upgrade(self, *, migrations_dir: str | Path | None = None) -> None:
        """Migrate the local store to head, safely from sync or async code.

        The migration environment calls ``asyncio.run`` internally, which cannot
        run inside an event loop; when a loop is already running (the platform is
        often built from async code) the migration is offloaded to a worker
        thread. Idempotent — running it on an up-to-date store is a no-op.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._run_migration(migrations_dir)
        else:  # pragma: no cover - exercised via Platform.build in async tests
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(self._run_migration, migrations_dir).result()

    def _run_migration(self, migrations_dir: str | Path | None) -> None:
        from alembic import command
        from alembic.config import Config

        script_location = (
            Path(migrations_dir)
            if migrations_dir
            else Path(__file__).resolve().parents[4] / LOCAL_MIGRATIONS_DIR
        )
        if not script_location.is_dir():
            raise ConfigurationError(f"local migrations directory not found: {script_location}")
        config = Config()
        config.set_main_option("script_location", str(script_location))
        config.set_main_option("sqlalchemy.url", local_database_url(self._path))
        config.set_main_option("version_table", LOCAL_VERSION_TABLE)
        command.upgrade(config, "head")

    async def upgrade_async(self, *, migrations_dir: str | Path | None = None) -> None:
        await asyncio.to_thread(self._run_migration, migrations_dir)

    async def dispose(self) -> None:
        await self._engine.dispose()


__all__ = [
    "LOCAL_MIGRATIONS_DIR",
    "LOCAL_VERSION_TABLE",
    "LocalDatabase",
    "default_store_path",
    "local_database_url",
]
