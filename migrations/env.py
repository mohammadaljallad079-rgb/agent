"""Alembic environment.

The target metadata is the platform's ORM schema, and the URL comes from
``AAP_DATABASE_URL`` (via the application settings), so migrations run against
the same connection string the app uses. Alembic runs against an async engine
using the same ``asyncpg`` driver as the application, so no second database
driver is required.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from arabic_agent.core.config import Settings
from arabic_agent.persistence.database import normalize_database_url
from arabic_agent.persistence.schema import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    raw = Settings().database_url.get_secret_value()
    if not raw:
        raise RuntimeError(
            "AAP_DATABASE_URL is not set; export it before running migrations, "
            "e.g. postgresql://aap:aap_dev_password@localhost:5432/aap"
        )
    return normalize_database_url(raw)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
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
