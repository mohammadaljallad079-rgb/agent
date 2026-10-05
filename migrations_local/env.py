"""Alembic environment for the local SQLite store.

The URL is taken from the Alembic config (set programmatically by
:meth:`arabic_agent.local.persistence.database.LocalDatabase.upgrade`), not from
application settings, so the local store's schema is independent of the cloud
``AAP_DATABASE_URL``. The target metadata is ``LocalBase.metadata`` — the local
subset schema.

Alembic runs against an async SQLite engine using ``aiosqlite``, the same driver
the local runtime uses, so no second database driver is required.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from arabic_agent.local.persistence.schema import LocalBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = LocalBase.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "sqlalchemy.url is not set; the local migrations must be run through "
            "LocalDatabase.upgrade() (which sets it) or with -x url=<sqlite url>"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=_version_table(),
    )
    with context.begin_transaction():
        context.run_migrations()


def _version_table() -> str:
    return config.get_main_option("version_table") or "local_alembic_version"


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=_version_table(),
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
