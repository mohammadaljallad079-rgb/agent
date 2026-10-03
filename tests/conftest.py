"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, Responder


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Test settings: small budgets, temp workspace, no real credentials."""
    return Settings(
        environment="test",
        require_auth=False,
        local_store_dir=str(tmp_path / "data"),
        max_context_tokens=4000,
        max_steps_per_run=20,
        max_tool_retries=2,
        max_replans=2,
        max_run_seconds=30.0,
        # The unit suite must never reach Postgres/Redis, even if the developer
        # has AAP_DATABASE_URL/AAP_REDIS_URL exported in their shell.
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
    )


@pytest.fixture()
def make_platform(settings: Settings):
    """Return a factory building a platform around a responder."""

    def factory(respond: Responder, *, extra_tools: list | None = None) -> Platform:
        registry = ProviderRegistry()
        registry.register(FakeProvider(respond))
        return Platform.build(
            settings,
            provider_registry=registry,
            default_model="fake/model",
            extra_tools=extra_tools,
        )

    return factory


# --- Durable (Postgres) integration fixtures --------------------------------
# These are opt-in: without AAP_TEST_DATABASE_URL the whole module is skipped,
# so the default unit run needs no infrastructure. They exercise the real
# Postgres store implementations (not doubles) against a dedicated test DB.

TEST_DATABASE_URL = os.environ.get("AAP_TEST_DATABASE_URL", "")
TEST_REDIS_URL = os.environ.get("AAP_TEST_REDIS_URL", "")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="AAP_TEST_DATABASE_URL not set"
)
requires_redis = pytest.mark.skipif(not TEST_REDIS_URL, reason="AAP_TEST_REDIS_URL not set")


@pytest.fixture()
async def postgres_database():
    """A fresh schema in the test database, dropped when the test finishes."""
    from arabic_agent.persistence import Base, Database

    database = Database(TEST_DATABASE_URL)
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await database.dispose()


@pytest.fixture()
async def redis_client():
    """A Redis client against a dedicated test DB, flushed when done."""
    from arabic_agent.infrastructure.redis import build_redis_client

    client = build_redis_client(TEST_REDIS_URL)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
