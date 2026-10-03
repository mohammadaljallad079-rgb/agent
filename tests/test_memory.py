"""Memory store and policy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arabic_agent.core.enums import MemoryKind
from arabic_agent.memory.base import MemoryEntry, MemoryQuery
from arabic_agent.memory.memory_store import InMemoryMemoryStore
from arabic_agent.memory.policy import MemoryPolicy


async def test_tenant_scoped_memory_is_visible_to_all_tenant_users():
    store = InMemoryMemoryStore()
    entry = MemoryEntry(
        kind=MemoryKind.COMPANY,
        tenant_id="tnt_1",
        user_id=None,
        key="company_name",
        content="شركة النور",
    )
    await store.write(entry)
    results = await store.query(MemoryQuery(tenant_id="tnt_1", user_id="usr_9"))
    assert [e.key for e in results] == ["company_name"]


async def test_user_scoped_memory_is_not_visible_to_other_users():
    store = InMemoryMemoryStore()
    await store.write(
        MemoryEntry(
            kind=MemoryKind.PREFERENCE,
            tenant_id="tnt_1",
            user_id="usr_1",
            key="lang",
            content="ar",
        )
    )
    assert await store.query(MemoryQuery(tenant_id="tnt_1", user_id="usr_2")) == []
    assert len(await store.query(MemoryQuery(tenant_id="tnt_1", user_id="usr_1"))) == 1


async def test_memory_is_not_visible_across_tenants():
    store = InMemoryMemoryStore()
    await store.write(
        MemoryEntry(
            kind=MemoryKind.KNOWLEDGE,
            tenant_id="tnt_1",
            user_id=None,
            key="k",
            content="v",
        )
    )
    assert await store.query(MemoryQuery(tenant_id="tnt_2")) == []


async def test_write_upserts_by_scope_and_key():
    store = InMemoryMemoryStore()
    first = await store.write(
        MemoryEntry(
            kind=MemoryKind.PREFERENCE, tenant_id="tnt_1", user_id="usr_1", key="tone", content="a"
        )
    )
    second = await store.write(
        MemoryEntry(
            kind=MemoryKind.PREFERENCE, tenant_id="tnt_1", user_id="usr_1", key="tone", content="b"
        )
    )
    assert first.id == second.id
    results = await store.query(MemoryQuery(tenant_id="tnt_1", user_id="usr_1"))
    assert len(results) == 1
    assert results[0].content == "b"


async def test_expired_memory_is_purged_and_not_returned():
    store = InMemoryMemoryStore()
    await store.write(
        MemoryEntry(
            kind=MemoryKind.TASK,
            tenant_id="tnt_1",
            user_id="usr_1",
            key="old",
            content="x",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    assert await store.query(MemoryQuery(tenant_id="tnt_1", user_id="usr_1")) == []
    assert await store.purge_expired() == 1


def test_policy_blocks_secret_like_content():
    policy = MemoryPolicy.default("tnt_1")
    allowed, reason = policy.check_write(MemoryKind.PREFERENCE, "my api_key is sk-abcdefghijklmnop")
    assert not allowed
    assert "secret" in reason


def test_policy_forbids_storing_conversation_memory():
    policy = MemoryPolicy.default("tnt_1")
    allowed, reason = policy.check_write(MemoryKind.CONVERSATION, "hello")
    assert not allowed
    assert "not writable" in reason


def test_policy_allows_durable_facts():
    policy = MemoryPolicy.default("tnt_1")
    allowed, _ = policy.check_write(MemoryKind.PREFERENCE, "prefers Arabic invoices")
    assert allowed
