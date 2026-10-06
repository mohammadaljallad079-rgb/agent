"""Desktop durable stores: run sessions and the external-action ledger.

These use a **real SQLite file** and rebuild the platform between steps, proving
that a desktop session (run) and a consequential external action both survive a
restart. The agent loop and policy engine are real; only the LLM is faked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from arabic_agent.connectors.ledger import ActionLedger, ActionRecord
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import ActionState, OperatingMode, RiskLevel, RunStatus, RuntimeKind
from arabic_agent.core.errors import ConflictError, NotFoundError
from arabic_agent.local.persistence.database import LocalDatabase
from arabic_agent.local.persistence.stores import LocalActionLedger, LocalRunStore
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.runs.base import RunRecord

from .helpers import FakeProvider, text_response


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        require_auth=False,
        runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=str(tmp_path / "data"),
        local_workspace_roots=(str(tmp_path / "approved"),),
        local_mode=OperatingMode.OFFLINE,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
        max_steps_per_run=20,
    )


def _build(tmp_path: Path) -> Platform:
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    return Platform.build(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )


def _db(tmp_path: Path) -> LocalDatabase:
    database = LocalDatabase(str(tmp_path / "data" / "local.db"))
    database.upgrade()
    return database


def _run(tenant: str = "tnt_1", goal: str = "مهمة") -> RunRecord:
    return RunRecord(tenant_id=tenant, user_id="usr_1", goal=goal, workspace_dir="/tmp/ws")


def _action(*, idempotency_key: str = "idem-1", run_id: str = "run_1") -> ActionRecord:
    return ActionRecord(
        tenant_id="tnt_1",
        run_id=run_id,
        step_id="stp_1",
        connector="erp",
        operation="create_invoice",
        risk=RiskLevel.HIGH,
        idempotency_key=idempotency_key,
        request_digest="a" * 64,
    )


# --- Run store --------------------------------------------------------------
def test_platform_uses_durable_run_store_in_local_mode(tmp_path):
    platform = _build(tmp_path)
    assert isinstance(platform.runs, LocalRunStore)


async def test_run_survives_store_restart(tmp_path):
    db1 = _db(tmp_path)
    store1 = LocalRunStore(db1)
    record = await store1.create(_run())
    await db1.dispose()

    db2 = _db(tmp_path)
    store2 = LocalRunStore(db2)
    loaded = await store2.get(record.id, tenant_id="tnt_1")
    assert loaded.id == record.id
    assert loaded.goal == "مهمة"
    assert loaded.status is RunStatus.PENDING
    await db2.dispose()


async def test_run_update_uses_optimistic_concurrency(tmp_path):
    db = _db(tmp_path)
    store = LocalRunStore(db)
    record = await store.create(_run())
    updated = await store.update(
        record.transition(RunStatus.PLANNING), expected_version=record.version
    )
    assert updated.version == record.version + 1
    with pytest.raises(ConflictError):
        await store.update(record.transition(RunStatus.RUNNING), expected_version=record.version)
    await db.dispose()


async def test_run_store_is_tenant_isolated(tmp_path):
    db = _db(tmp_path)
    store = LocalRunStore(db)
    record = await store.create(_run(tenant="tnt_a"))
    with pytest.raises(NotFoundError):
        await store.get(record.id, tenant_id="tnt_b")
    await db.dispose()


async def test_run_idempotency_key_is_enforced(tmp_path):
    db = _db(tmp_path)
    store = LocalRunStore(db)
    first = RunRecord(
        tenant_id="tnt_1", user_id="usr_1", goal="a", workspace_dir="/tmp", idempotency_key="k"
    )
    await store.create(first)
    second = RunRecord(
        tenant_id="tnt_1", user_id="usr_1", goal="b", workspace_dir="/tmp", idempotency_key="k"
    )
    with pytest.raises(ConflictError):
        await store.create(second)
    await db.dispose()


async def test_run_list_filters_by_status(tmp_path):
    db = _db(tmp_path)
    store = LocalRunStore(db)
    record = await store.create(_run())
    await store.update(record.transition(RunStatus.PLANNING), expected_version=record.version)
    pending = await store.list_runs(tenant_id="tnt_1", status=RunStatus.PENDING)
    planning = await store.list_runs(tenant_id="tnt_1", status=RunStatus.PLANNING)
    assert pending == []
    assert len(planning) == 1
    await db.dispose()


# --- Action ledger ----------------------------------------------------------
def test_platform_uses_durable_ledger_in_local_mode(tmp_path):
    """When connectors are configured, the runtime's ledger is the durable one."""
    import json

    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    settings = _settings(tmp_path).model_copy(
        update={
            "connectors_enabled": True,
            "connectors_json": json.dumps(
                [{"name": "api", "kind": "http", "base_url": "https://api.example.com"}]
            ),
        }
    )
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")
    assert platform.connector_runtime is not None
    assert isinstance(platform.connector_runtime.ledger, LocalActionLedger)


async def test_action_survives_ledger_restart(tmp_path):
    db1 = _db(tmp_path)
    ledger1 = LocalActionLedger(db1)
    record = await ledger1.add(_action())
    await db1.dispose()

    db2 = _db(tmp_path)
    ledger2 = LocalActionLedger(db2)
    loaded = await ledger2.get(record.id, tenant_id="tnt_1")
    assert loaded.id == record.id
    assert loaded.idempotency_key == "idem-1"
    assert loaded.state is ActionState.PREPARED
    # Idempotency lookup still finds the record after a restart.
    found = await ledger2.find_by_idempotency(tenant_id="tnt_1", idempotency_key="idem-1")
    assert found is not None and found.id == record.id
    await db2.dispose()


async def test_action_unknown_state_is_preserved(tmp_path):
    """An ambiguous timeout must survive a restart, so it is never blindly retried."""
    db = _db(tmp_path)
    ledger = LocalActionLedger(db)
    record = await ledger.add(_action())
    record = await ledger.update(record.with_state(ActionState.EXECUTING))
    record = await ledger.update(record.with_state(ActionState.UNKNOWN))
    await db.dispose()

    db2 = _db(tmp_path)
    ledger2 = LocalActionLedger(db2)
    loaded = await ledger2.get(record.id, tenant_id="tnt_1")
    assert loaded.state is ActionState.UNKNOWN
    await db2.dispose()


async def test_action_idempotency_key_unique_per_tenant(tmp_path):
    db = _db(tmp_path)
    ledger = LocalActionLedger(db)
    await ledger.add(_action(idempotency_key="dup"))
    with pytest.raises(ConflictError):
        await ledger.add(_action(idempotency_key="dup"))
    await db.dispose()


async def test_action_ledger_is_tenant_isolated(tmp_path):
    db = _db(tmp_path)
    ledger = LocalActionLedger(db)
    record = await ledger.add(_action())
    with pytest.raises(NotFoundError):
        await ledger.get(record.id, tenant_id="other")
    await db.dispose()


async def test_action_ledger_list_for_run(tmp_path):
    db = _db(tmp_path)
    ledger = LocalActionLedger(db)
    await ledger.add(_action(idempotency_key="a", run_id="run_1"))
    await ledger.add(_action(idempotency_key="b", run_id="run_2"))
    actions = await ledger.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert len(actions) == 1
    await db.dispose()


async def test_action_ledger_prune_keeps_unresolved(tmp_path):
    db = _db(tmp_path)
    ledger = LocalActionLedger(db)
    verified = await ledger.add(_action(idempotency_key="v"))
    verified = await ledger.update(verified.with_state(ActionState.EXECUTING))
    verified = await ledger.update(verified.with_state(ActionState.SUCCEEDED))
    verified = await ledger.update(verified.with_state(ActionState.VERIFYING))
    verified = await ledger.update(verified.with_state(ActionState.VERIFIED))

    unknown = await ledger.add(_action(idempotency_key="u"))
    unknown = await ledger.update(unknown.with_state(ActionState.EXECUTING))
    unknown = await ledger.update(unknown.with_state(ActionState.UNKNOWN))

    removed = await ledger.prune_before(cutoff=datetime.now(UTC) + timedelta(days=1))
    assert removed == 1
    # The ambiguous action is retained; only the terminal one was pruned.
    assert await ledger.get(unknown.id, tenant_id="tnt_1")
    with pytest.raises(NotFoundError):
        await ledger.get(verified.id, tenant_id="tnt_1")
    await db.dispose()


def test_durable_ledger_is_an_action_ledger(tmp_path):
    assert issubclass(LocalActionLedger, ActionLedger)
