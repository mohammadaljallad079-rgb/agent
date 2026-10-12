"""Calendar connector tests: provider-neutral surface over a real HTTP transport.

The Google Calendar adapter is exercised against a *real* local HTTP server that
speaks Google's documented REST shape, so authentication header attachment, query
parameter mapping, response normalization, write verification and the refresh-
token exchange are proven against actual sockets rather than a stub. The connector
runtime and tool layer are exercised on top, so a calendar write really does pause
for a trusted approval and is never repeated on retry.
"""

from __future__ import annotations

import pytest

from arabic_agent.agent.executor import ExecutionOutcomeKind, ToolExecutor
from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.connectors.calendar import (
    CalendarConnector,
    CalendarEvent,
    GoogleCalendarProvider,
    normalized_event,
)
from arabic_agent.connectors.credentials import (
    CredentialRef,
    CredentialResolver,
    StaticCredentialProvider,
)
from arabic_agent.connectors.ledger import InMemoryActionLedger
from arabic_agent.connectors.runtime import ConnectorRuntime
from arabic_agent.core.enums import (
    ApprovalStatus,
    ConnectorState,
    PermissionEffect,
)
from arabic_agent.core.errors import ConfigurationError, ValidationError
from arabic_agent.permissions.approvals import InMemoryApprovalStore
from arabic_agent.permissions.base import Permission, PolicySet, Principal, Rule
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.connectors import CONNECTOR_TOOLS
from arabic_agent.tools.registry import ToolRegistry

from .fake_http import http_server


def _resolver(values: dict[str, str] | None = None):
    return CredentialResolver([StaticCredentialProvider(values or {})])


def _provider(server, *, resolver=None) -> GoogleCalendarProvider:
    return GoogleCalendarProvider(
        base_url=server.base_url,
        token_url=f"{server.base_url}/token",
        access_token=CredentialRef(
            provider="static", name="gcal_access", required=False, description="access token"
        ),
        credentials=resolver or _resolver({"gcal_access": "test-token"}),
    )


# --------------------------------------------------------------------------
# Provider: configuration honesty and normalization
# --------------------------------------------------------------------------
async def test_provider_reports_not_configured_without_credentials():
    provider = GoogleCalendarProvider(credentials=_resolver())
    assert provider.state() is ConnectorState.NOT_CONFIGURED
    assert await provider.credential_ready() is False

    connector = CalendarConnector(name="gcal", provider=provider)
    health = await connector.healthcheck()
    assert health.state is ConnectorState.NOT_CONFIGURED
    assert health.configured is False


async def test_provider_ready_when_access_token_present():
    provider = GoogleCalendarProvider(
        access_token=CredentialRef(provider="static", name="t", required=False),
        credentials=_resolver({"t": "abc"}),
    )
    assert await provider.credential_ready() is True


def test_normalized_event_maps_google_shape():
    raw = {
        "id": "evt_1",
        "summary": "Standup",
        "start": {"dateTime": "2026-01-01T09:00:00Z"},
        "end": {"dateTime": "2026-01-01T09:15:00Z"},
        "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}],
        "organizer": {"email": "owner@x.com"},
        "status": "confirmed",
    }
    event = normalized_event(raw, calendar_id="primary")
    assert event.id == "evt_1"
    assert event.summary == "Standup"
    assert event.all_day is False
    assert event.attendees == ("a@x.com", "b@x.com")
    assert event.organizer == "owner@x.com"


def test_normalized_event_maps_all_day_date():
    raw = {"id": "e", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}
    event = normalized_event(raw)
    assert event.all_day is True
    assert event.start == "2026-01-01"


# --------------------------------------------------------------------------
# Provider: real HTTP round trips
# --------------------------------------------------------------------------
async def test_list_events_attaches_auth_and_maps_params():
    with http_server() as server:
        server.set_default(
            body=(
                '{"items": [{"id": "e1", "summary": "A", '
                '"start": {"dateTime": "2026-01-01T09:00:00Z"}, '
                '"end": {"dateTime": "2026-01-01T10:00:00Z"}}]}'
            )
        )
        provider = _provider(server)
        events = await provider.list_events(
            calendar_id="primary",
            time_min="2026-01-01T00:00:00Z",
            time_max="2026-01-02T00:00:00Z",
            max_results=10,
            query="A",
        )
        assert [e.id for e in events] == ["e1"]
        request = server.requests[0]
        assert request.headers["authorization"] == "Bearer test-token"
        assert "timeMin=2026-01-01T00%3A00%3A00Z" in request.path
        assert "maxResults=10" in request.path
        assert "q=A" in request.path
        await provider.aclose()


async def test_create_event_posts_normalized_body_and_returns_event():
    with http_server() as server:
        server.set_default(
            body=(
                '{"id": "new_1", "summary": "Sync", '
                '"start": {"dateTime": "2026-02-01T09:00:00Z"}, '
                '"end": {"dateTime": "2026-02-01T09:30:00Z"}}'
            )
        )
        provider = _provider(server)
        created = await provider.create_event(
            calendar_id="primary",
            event=CalendarEvent(
                summary="Sync",
                start="2026-02-01T09:00:00Z",
                end="2026-02-01T09:30:00Z",
                attendees=("a@x.com",),
            ),
        )
        assert created.id == "new_1"
        body = server.requests[0].json()
        assert body["summary"] == "Sync"
        assert body["start"] == {"dateTime": "2026-02-01T09:00:00Z"}
        assert body["attendees"] == [{"email": "a@x.com"}]
        await provider.aclose()


async def test_read_refuses_invalid_instant_before_calling_provider():
    with http_server() as server:
        provider = _provider(server)
        with pytest.raises(ValidationError):
            await provider.list_events(calendar_id="primary", time_min="not-a-date")
        assert server.requests == []
        await provider.aclose()


async def test_refresh_token_exchange_uses_token_endpoint():
    with http_server() as server:
        server.route(
            "/token",
            body='{"access_token": "refreshed-token", "expires_in": 3600}',
        )
        server.set_default(body='{"items": []}')
        provider = GoogleCalendarProvider(
            base_url=server.base_url,
            token_url=f"{server.base_url}/token",
            refresh_token=CredentialRef(provider="static", name="rt", required=False),
            client_id=CredentialRef(provider="static", name="cid", required=False),
            client_secret=CredentialRef(provider="static", name="cs", required=False),
            credentials=_resolver({"rt": "refresh", "cid": "client", "cs": "secret"}),
        )
        assert await provider.credential_ready() is True
        await provider.list_events(calendar_id="primary")
        # First request is the token exchange, second is the API call.
        assert server.requests[0].path == "/token"
        assert server.requests[1].headers["authorization"] == "Bearer refreshed-token"
        await provider.aclose()


async def test_provider_surfaces_forbidden_as_configuration_error():
    with http_server() as server:
        server.set_default(status=403, body='{"error": {"message": "forbidden"}}')
        provider = _provider(server)
        with pytest.raises(ConfigurationError):
            await provider.list_calendars()
        await provider.aclose()


# --------------------------------------------------------------------------
# Connector runtime + tool layer
# --------------------------------------------------------------------------
def _calendar_registry(server) -> object:
    from arabic_agent.connectors.base import ConnectorRegistry

    registry = ConnectorRegistry()
    registry.register(CalendarConnector(name="gcal", provider=_provider(server)))
    return registry


def _runtime(registry, *, audit: AuditLog) -> ConnectorRuntime:
    return ConnectorRuntime(registry=registry, audit=audit, ledger=InMemoryActionLedger())


def _allow_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="allow-calendar",
            rules=tuple(
                Rule(effect=PermissionEffect.ALLOW, permission=perm, reason="test allow")
                for perm in (
                    Permission.CALENDAR_READ,
                    Permission.CALENDAR_WRITE,
                    Permission.CALENDAR_DELETE,
                )
            ),
        ),
    )
    return engine


def _executor(runtime, *, policy=None):
    approvals = InMemoryApprovalStore()
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in CONNECTOR_TOOLS]),
        policy=policy or _allow_policy(),
        approvals=approvals,
        audit=runtime._audit,
        tool_metadata={"connector_runtime": runtime},
    )
    return executor, approvals


def _principal() -> Principal:
    return Principal(tenant_id="tnt_1", user_id="usr_1")


def _context() -> ToolContext:
    return ToolContext(tenant_id="tnt_1", user_id="usr_1", run_id="run_1")


async def test_calendar_read_is_allowed_under_default_policy():
    with http_server() as server:
        server.set_default(body='{"items": []}')
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        executor, _ = _executor(runtime, policy=PolicyEngine())
        outcome = await executor.execute(
            tool_name="calendar_read",
            arguments={"connector": "gcal", "action": "list_events"},
            context=_context(),
            principal=_principal(),
        )
        assert outcome.kind is ExecutionOutcomeKind.COMPLETED
        assert outcome.result is not None
        assert outcome.result.data["count"] == 0
        await runtime.connector("gcal").aclose()


async def test_calendar_create_requires_approval_under_default_policy():
    with http_server() as server:
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        executor, _ = _executor(runtime, policy=PolicyEngine())
        outcome = await executor.execute(
            tool_name="calendar_write",
            arguments={
                "connector": "gcal",
                "action": "create_event",
                "summary": "Meeting",
                "start": "2026-03-01T09:00:00Z",
                "end": "2026-03-01T10:00:00Z",
            },
            context=_context(),
            principal=_principal(),
        )
        assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
        assert outcome.approval is not None
        assert outcome.approval.permission is Permission.CALENDAR_WRITE
        assert server.requests == []
        await runtime.connector("gcal").aclose()


async def test_calendar_delete_requires_approval_under_default_policy():
    with http_server() as server:
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        executor, _ = _executor(runtime, policy=PolicyEngine())
        outcome = await executor.execute(
            tool_name="calendar_delete",
            arguments={"connector": "gcal", "action": "delete_event", "event_id": "e1"},
            context=_context(),
            principal=_principal(),
        )
        assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
        assert outcome.approval is not None
        assert outcome.approval.permission is Permission.CALENDAR_DELETE
        await runtime.connector("gcal").aclose()


async def test_calendar_create_executes_and_verifies_after_approval():
    with http_server() as server:
        created = (
            '{"id": "evt_new", "summary": "Meeting", '
            '"start": {"dateTime": "2026-03-01T09:00:00Z"}, '
            '"end": {"dateTime": "2026-03-01T10:00:00Z"}}'
        )
        # The POST (collection) and the GET (verify) both hit the default route.
        server.set_default(body=created)
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        executor, approvals = _executor(runtime, policy=PolicyEngine())
        arguments = {
            "connector": "gcal",
            "action": "create_event",
            "summary": "Meeting",
            "start": "2026-03-01T09:00:00Z",
            "end": "2026-03-01T10:00:00Z",
        }
        requested = await executor.execute(
            tool_name="calendar_write",
            arguments=arguments,
            context=_context(),
            principal=_principal(),
        )
        assert requested.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
        assert requested.approval is not None
        await approvals.resolve(
            requested.approval.id,
            tenant_id="tnt_1",
            status=ApprovalStatus.APPROVED,
            resolved_by="usr_1",
        )
        granted = await approvals.get(requested.approval.id, tenant_id="tnt_1")
        outcome = await executor.execute_approved(
            approval=granted, context=_context(), principal=_principal()
        )
        assert outcome.kind is ExecutionOutcomeKind.COMPLETED
        assert outcome.result is not None
        assert outcome.result.data["event"]["id"] == "evt_new"
        # The provider saw the POST and then the GET that verified it.
        assert any(r.method == "POST" for r in server.requests)
        assert any(r.method == "GET" for r in server.requests)
        await runtime.connector("gcal").aclose()


async def test_calendar_create_is_not_repeated_on_identical_retry():
    with http_server() as server:
        created = (
            '{"id": "evt_once", "summary": "Once", '
            '"start": {"dateTime": "2026-04-01T09:00:00Z"}, '
            '"end": {"dateTime": "2026-04-01T10:00:00Z"}}'
        )
        server.set_default(body=created)
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        connector = runtime.connector("gcal")
        from arabic_agent.connectors.ledger import canonical_digest

        payload = {
            "calendar_id": "",
            "summary": "Once",
            "description": "",
            "location": "",
            "start": "2026-04-01T09:00:00Z",
            "end": "2026-04-01T10:00:00Z",
            "all_day": False,
            "attendees": [],
        }
        digest = canonical_digest(connector="gcal", operation="calendar.create", payload=payload)
        first = await runtime.invoke(
            connector_name="gcal",
            operation="calendar.create",
            payload=payload,
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
            step_id="step_1",
            approval_granted=True,
            approved_digest=digest,
        )
        assert first.ok
        second = await runtime.invoke(
            connector_name="gcal",
            operation="calendar.create",
            payload=payload,
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
            step_id="step_1",
            approval_granted=True,
            approved_digest=digest,
        )
        assert second.deduplicated is True
        # Exactly one POST reached the provider; the retry never re-created it.
        assert sum(1 for r in server.requests if r.method == "POST") == 1
        await connector.aclose()


async def test_approval_bound_to_payload_is_refused_when_changed():
    with http_server() as server:
        server.set_default(body='{"id": "e1"}')
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        from arabic_agent.connectors.ledger import canonical_digest

        other = {"calendar_id": "", "event_id": "e1", "summary": "Different"}
        digest = canonical_digest(connector="gcal", operation="calendar.create", payload=other)
        payload = {"calendar_id": "", "event_id": "e1", "summary": "Original"}
        from arabic_agent.core.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await runtime.invoke(
                connector_name="gcal",
                operation="calendar.create",
                payload=payload,
                tenant_id="tnt_1",
                user_id="usr_1",
                run_id="run_1",
                approval_granted=True,
                approved_digest=digest,
            )
        await runtime.connector("gcal").aclose()


async def test_calendar_audit_records_action_lifecycle():
    with http_server() as server:
        server.set_default(body='{"items": []}')
        sink = InMemoryAuditSink()
        runtime = _runtime(_calendar_registry(server), audit=AuditLog([sink]))
        executor, _ = _executor(runtime, policy=PolicyEngine())
        await executor.execute(
            tool_name="calendar_read",
            arguments={"connector": "gcal", "action": "list_events"},
            context=_context(),
            principal=_principal(),
        )
        events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
        types = {e.type for e in events}
        assert AuditEventType.EXTERNAL_ACTION_EXECUTING in types
        assert AuditEventType.EXTERNAL_ACTION_SUCCEEDED in types
        await runtime.connector("gcal").aclose()
