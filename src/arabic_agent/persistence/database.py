"""Async database engine and session management.

Postgres is the durable source of truth for the cloud runtime. The agent core
never imports this module: it depends on the store ABCs, and this module is
reached only through concrete Postgres store implementations selected by the
composition root.

The URL is read from ``AAP_DATABASE_URL`` (a ``SecretStr``); it is never logged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..core.errors import ConfigurationError


def normalize_database_url(url: str) -> str:
    """Ensure the URL uses the asyncpg driver.

    Accepts ``postgres://`` and ``postgresql://`` and rewrites them to
    ``postgresql+asyncpg://`` so callers can paste a plain connection string.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


class Database:
    """Owns the async engine and session factory for one process."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        echo: bool = False,
    ) -> None:
        if not url:
            raise ConfigurationError("database URL is required for Postgres persistence")
        self._engine: AsyncEngine = create_async_engine(
            normalize_database_url(url),
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            echo=echo,
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        """Return a new session. Callers own its lifecycle."""
        return self._sessionmaker()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Run a block in a transaction, committing on success and rolling back
        on any exception."""
        async with self._sessionmaker.begin() as session:
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()
