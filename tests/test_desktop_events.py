"""Desktop event broker tests.

The broker is an :class:`AuditSink`; these tests prove it mirrors real audit
events, redacts them, isolates tenants and drops rather than blocks on a slow
subscriber. No timer or fabricated progress is involved.
"""

from __future__ import annotations

import asyncio

import pytest

from arabic_agent.audit.base import AuditEvent, AuditEventType
from arabic_agent.desktop.events import DesktopEvent, DesktopEventBroker, sse_format


def _event(
    *,
    event_type: AuditEventType = AuditEventType.TOOL_CALLED,
    tenant_id: str = "tnt_1",
    run_id: str = "run_1",
    message: str = "called",
    data: dict | None = None,
    sequence: int = 1,
) -> AuditEvent:
    return AuditEvent.create(
        event_type,
        tenant_id=tenant_id,
        user_id="usr_1",
        run_id=run_id,
        sequence=sequence,
        message=message,
        data=data or {},
    )


async def test_broker_mirrors_audit_event_to_subscriber():
    broker = DesktopEventBroker()
    subscriber = broker.subscribe(tenant_id="tnt_1")
    await broker.emit(_event(message="tool called"))
    received = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
    assert received.type == AuditEventType.TOOL_CALLED.value
    assert received.run_id == "run_1"
    assert received.message == "tool called"
    assert broker.subscriber_count == 1


async def test_broker_redacts_secret_payloads():
    broker = DesktopEventBroker()
    subscriber = broker.subscribe()
    await broker.emit(_event(data={"api_key": "sk-abcdefghijklmnop1234567890", "safe": "ok"}))
    received = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
    assert received.data["api_key"] == "***REDACTED***"
    assert received.data["safe"] == "ok"


async def test_broker_isolates_tenants():
    broker = DesktopEventBroker()
    sub_a = broker.subscribe(tenant_id="tnt_a")
    sub_b = broker.subscribe(tenant_id="tnt_b")
    await broker.emit(_event(tenant_id="tnt_a"))
    assert sub_a.queue.qsize() == 1
    assert sub_b.queue.qsize() == 0


async def test_broker_unsubscribe_stops_delivery():
    broker = DesktopEventBroker()
    subscriber = broker.subscribe()
    broker.unsubscribe(subscriber)
    await broker.emit(_event())
    assert broker.subscriber_count == 0
    assert subscriber.queue.qsize() == 0


async def test_broker_drops_oldest_when_subscriber_lags():
    broker = DesktopEventBroker()
    subscriber = broker.subscribe()
    for i in range(600):
        await broker.emit(_event(sequence=i))
    # The queue is bounded; the newest events are retained, the oldest dropped.
    assert subscriber.queue.qsize() <= 500
    last = None
    while not subscriber.queue.empty():
        last = subscriber.queue.get_nowait()
    assert last is not None and last.sequence == 599


async def test_broker_recent_and_replay():
    broker = DesktopEventBroker()
    await broker.emit(_event(run_id="run_a", sequence=1))
    await broker.emit(_event(run_id="run_b", sequence=2))
    assert len(broker.recent()) == 2
    assert len(broker.recent(run_id="run_a")) == 1

    received: list[DesktopEvent] = []

    async def consume() -> None:
        async for event in broker.stream(tenant_id="tnt_1"):
            received.append(event)
            if len(received) == 2:
                return

    await asyncio.wait_for(consume(), timeout=1)
    assert [e.run_id for e in received] == ["run_a", "run_b"]


async def test_broker_list_for_run_is_empty_no_persistence():
    broker = DesktopEventBroker()
    await broker.emit(_event())
    assert await broker.list_for_run(tenant_id="tnt_1", run_id="run_1") == []


def test_sse_format_is_valid_frame():
    event = DesktopEvent(
        type="tool.called",
        run_id="run_1",
        tenant_id="tnt_1",
        message="مرحبا",
        sequence=3,
        timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    frame = sse_format(event)
    assert frame.startswith("event: agent\n")
    assert "data: " in frame
    assert frame.endswith("\n\n")
    assert "مرحبا" in frame


def test_desktop_event_from_audit_preserves_sequence():
    event = DesktopEvent.from_audit(_event(sequence=7))
    assert event.sequence == 7


@pytest.mark.parametrize("bad", [{"password": "hunter2"}, {"token": "abc123"}])
async def test_broker_redacts_sensitive_keys(bad: dict):
    broker = DesktopEventBroker()
    subscriber = broker.subscribe()
    await broker.emit(_event(data=bad))
    received = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
    assert all(v == "***REDACTED***" for v in received.data.values())
