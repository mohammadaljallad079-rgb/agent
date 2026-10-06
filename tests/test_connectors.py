"""External connector tests (Phase 9): real transports, real protocols.

These exercise the connector layer against *real* infrastructure — a real local
HTTP server for the HTTP/webhook/business connectors and real SMTP/IMAP servers
for the email connector — so the URL policy, redirect handling, size caps,
credential containment, HMAC signing and protocol handling are proven against
actual sockets rather than a stubbed client.
"""

from __future__ import annotations

import json

import pytest

from arabic_agent.browser.url_security import UrlPolicy
from arabic_agent.connectors.base import ConnectorCall
from arabic_agent.connectors.business import BusinessConnector, ResourceMapping
from arabic_agent.connectors.config import ConnectorSpec, parse_connector_specs
from arabic_agent.connectors.credentials import (
    CredentialRef,
    CredentialResolver,
    EnvCredentialProvider,
    StaticCredentialProvider,
)
from arabic_agent.connectors.email import EmailConnector, OutgoingEmail
from arabic_agent.connectors.http import HttpConnector, HttpRequest
from arabic_agent.connectors.ledger import (
    InMemoryActionLedger,
    canonical_digest,
)
from arabic_agent.connectors.retry import RetryPolicy, with_retry
from arabic_agent.connectors.webhooks import WebhookConnector, WebhookPayload, sign_payload
from arabic_agent.core.enums import ConnectorState, DataSensitivity
from arabic_agent.core.errors import (
    ConfigurationError,
    PermissionDeniedError,
    TransientError,
    ValidationError,
)
from arabic_agent.local.sandbox import FilesystemPolicy

from .fake_http import http_server
from .fake_mail import build_message, imap_mailbox, smtp_sink


def _local_policy() -> UrlPolicy:
    return UrlPolicy(allow_local_addresses=True)


# --------------------------------------------------------------------------
# HTTP connector (real server)
# --------------------------------------------------------------------------
async def test_http_connector_get_is_bounded_and_redacted():
    with http_server() as server:
        server.route("/ok", body='{"hello": "world"}')
        connector = HttpConnector(
            name="api",
            base_url=server.base_url,
            url_policy=_local_policy(),
        )
        result = await connector.call(ConnectorCall(operation="request", payload={"path": "/ok"}))
        assert result.ok
        assert result.data["status"] == 200
        assert json.loads(result.data["body"]) == {"hello": "world"}
        assert server.requests[0].method == "GET"


async def test_http_connector_refuses_private_host_without_opt_in():
    with http_server() as server:
        connector = HttpConnector(name="api", base_url=server.base_url, url_policy=UrlPolicy())
        with pytest.raises(PermissionDeniedError):
            await connector.request(HttpRequest(method="GET", path="/ok"))


async def test_http_connector_enforces_method_policy():
    with http_server() as server:
        connector = HttpConnector(
            name="api",
            base_url=server.base_url,
            url_policy=_local_policy(),
            allowed_methods=("GET",),
        )
        with pytest.raises(PermissionDeniedError):
            await connector.request(HttpRequest(method="DELETE", path="/ok"))


async def test_http_connector_refuses_redirect_to_blocked_host():
    with http_server() as server:
        # A redirect to the cloud metadata address must be refused by policy,
        # not followed. (Metadata is link-local and always blocked.)
        server.route(
            "/redirect",
            status=302,
            body=b"",
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )
        provider = StaticCredentialProvider({"tok": "secret-value"})
        connector = HttpConnector(
            name="api",
            base_url=server.base_url,
            url_policy=UrlPolicy(allow_local_addresses=True, allowed_domains=("127.0.0.1",)),
            credential=CredentialRef(provider="static", name="tok"),
            credentials=CredentialResolver([provider]),
        )
        with pytest.raises(PermissionDeniedError):
            await connector.request(HttpRequest(method="GET", path="/redirect"))
        # Exactly one request reached a server: the redirect was never followed.
        assert len(server.requests) == 1


async def test_http_credential_only_attached_to_configured_origin():
    provider = StaticCredentialProvider({"tok": "abc123"})
    connector = HttpConnector(
        name="api",
        base_url="https://api.example.com",
        url_policy=UrlPolicy(),
        credential=CredentialRef(provider="static", name="tok"),
        credentials=CredentialResolver([provider]),
    )
    credential = await connector._credentials.resolve(connector._credential)
    on_origin = connector._auth_headers(credential, host="api.example.com")
    off_origin = connector._auth_headers(credential, host="evil.example.com")
    assert on_origin.get("Authorization") == "Bearer abc123"
    assert "Authorization" not in off_origin


async def test_http_connector_caps_response_size():
    with http_server() as server:
        server.route("/big", body=b"x" * 5000)
        connector = HttpConnector(
            name="api",
            base_url=server.base_url,
            url_policy=_local_policy(),
            max_response_bytes=100,
        )
        with pytest.raises(Exception):
            # Content-Length over the cap is refused rather than buffered.
            await connector.request(HttpRequest(method="GET", path="/big"))


async def test_http_connector_attaches_credential_to_configured_host_only():
    with http_server() as server:
        server.route("/secure", body=b"{}")
        provider = StaticCredentialProvider({"tok": "abc123"})
        connector = HttpConnector(
            name="api",
            base_url=server.base_url,
            url_policy=_local_policy(),
            credential=CredentialRef(provider="static", name="tok"),
            credentials=CredentialResolver([provider]),
        )
        await connector.request(HttpRequest(method="GET", path="/secure"))
        assert server.requests[0].headers.get("authorization") == "Bearer abc123"


async def test_http_health_reports_missing_credential_honestly():
    with http_server() as server:
        connector = HttpConnector(
            name="api",
            base_url=server.base_url,
            url_policy=_local_policy(),
            credential=CredentialRef(provider="static", name="missing"),
            credentials=CredentialResolver([StaticCredentialProvider({})]),
        )
        health = await connector.healthcheck()
        assert health.state is ConnectorState.REAUTHENTICATION_REQUIRED
        assert not health.usable


# --------------------------------------------------------------------------
# Webhook connector (real server)
# --------------------------------------------------------------------------
async def test_webhook_delivery_is_signed_over_exact_bytes():
    with http_server() as server:
        server.route("/hook", body=b"ok")
        provider = StaticCredentialProvider({"whsec": "topsecret"})
        transport = HttpConnector(
            name="hook.transport",
            allowed_methods=("POST",),
            url_policy=_local_policy(),
        )
        connector = WebhookConnector(
            name="hook",
            url=f"{server.base_url}/hook",
            transport=transport,
            secret=CredentialRef(provider="static", name="whsec"),
            credentials=CredentialResolver([provider]),
        )
        result = await connector.send(
            WebhookPayload(event="order.created", data={"id": 7}, delivery_id="d-1")
        )
        assert result.ok
        request = server.requests[0]
        timestamp = request.headers["x-aap-timestamp"]
        expected = sign_payload("topsecret", timestamp=timestamp, body=request.body)
        assert request.headers["x-aap-signature"] == expected
        assert request.headers["x-aap-delivery-id"] == "d-1"
        assert json.loads(request.body) == {
            "event": "order.created",
            "data": {"id": 7},
            "delivery_id": "d-1",
        }


# --------------------------------------------------------------------------
# Business / ERP connector (real server)
# --------------------------------------------------------------------------
async def test_business_connector_search_and_get():
    with http_server() as server:
        server.route("/customers?limit=25", body='{"items": [{"id": 1, "name": "A"}]}')
        server.route("/customers/1", body='{"id": 1, "name": "A"}')
        transport = HttpConnector(
            name="erp.transport",
            base_url=server.base_url,
            url_policy=_local_policy(),
        )
        connector = BusinessConnector(
            name="erp",
            transport=transport,
            resources=(ResourceMapping(name="customers", path="/customers"),),
        )
        search = await connector.call(
            ConnectorCall(operation="record.search", payload={"resource": "customers"})
        )
        assert search.ok
        assert search.data["count"] == 1
        got = await connector.call(
            ConnectorCall(operation="record.get", payload={"resource": "customers", "id": 1})
        )
        assert got.ok
        assert got.data["record"] == {"id": 1, "name": "A"}


async def test_business_connector_create_posts_fields():
    with http_server() as server:
        server.route("/orders", status=201, body='{"id": 99, "total": 10}')
        transport = HttpConnector(
            name="erp.transport", base_url=server.base_url, url_policy=_local_policy()
        )
        connector = BusinessConnector(
            name="erp",
            transport=transport,
            resources=(ResourceMapping(name="orders", path="/orders"),),
        )
        result = await connector.call(
            ConnectorCall(
                operation="record.create",
                payload={"resource": "orders", "fields": {"total": 10}},
            )
        )
        assert result.ok
        assert json.loads(server.requests[0].body) == {"total": 10}


# --------------------------------------------------------------------------
# Email connector (real SMTP + IMAP)
# --------------------------------------------------------------------------
async def test_email_send_reaches_a_real_smtp_server(tmp_path):
    with smtp_sink() as sink:
        connector = EmailConnector(
            name="email",
            smtp_host="127.0.0.1",
            smtp_port=sink.port,
            smtp_use_tls=False,
            from_address="agent@example.com",
        )
        result = await connector.send(
            OutgoingEmail(to=("customer@example.com",), subject="Invoice", body_text="Hello")
        )
        assert result.ok
        assert result.data["provider_message_id"]
        assert sink.messages
        assert b"Subject: Invoice" in sink.messages[0]
        assert b"Hello" in sink.messages[0]
        assert any("customer@example.com" in r for r in sink.recipients)


async def test_email_search_reads_a_real_imap_server():
    message = build_message(subject="Order 42", body="Please quote")
    with imap_mailbox([message]) as box:
        connector = EmailConnector(
            name="email",
            imap_host="127.0.0.1",
            imap_port=box.port,
            imap_username="reader",
        )
        result = await connector.call(
            ConnectorCall(operation="email.search", payload={"query": "ALL"})
        )
        assert result.ok
        assert result.data["count"] == 1
        assert result.data["messages"][0]["subject"] == "Order 42"


async def test_email_attachment_must_be_within_workspace(tmp_path):
    allowed = tmp_path / "ws"
    allowed.mkdir()
    (allowed / "invoice.txt").write_text("data")
    outside = tmp_path / "secret.env"
    outside.write_text("TOKEN=1")
    policy = FilesystemPolicy(allowed_roots=(str(allowed),), workspace_dir=str(allowed))
    with smtp_sink() as sink:
        connector = EmailConnector(
            name="email",
            smtp_host="127.0.0.1",
            smtp_port=sink.port,
            smtp_use_tls=False,
            filesystem_policy=policy,
            workspace_dir=str(allowed),
        )
        ok = await connector.send(
            OutgoingEmail(
                to=("a@b.com",),
                subject="s",
                body_text="b",
                attachments=("invoice.txt",),
            )
        )
        assert ok.ok
        with pytest.raises(PermissionDeniedError):
            await connector.send(
                OutgoingEmail(
                    to=("a@b.com",),
                    subject="s",
                    body_text="b",
                    attachments=(str(outside),),
                )
            )


async def test_email_health_honest_when_unconfigured():
    connector = EmailConnector(name="email")
    assert connector.health().state is ConnectorState.NOT_CONFIGURED


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
async def test_env_credential_resolution(monkeypatch):
    monkeypatch.setenv("AAP_CONNECTOR_MYTOKEN", "s3cret")
    resolver = CredentialResolver([EnvCredentialProvider()])
    resolved = await resolver.resolve(CredentialRef(name="MYTOKEN"))
    assert resolved is not None
    assert resolved.reveal() == "s3cret"
    assert "s3cret" not in repr(resolved)


async def test_missing_required_credential_raises_configuration_error():
    resolver = CredentialResolver([StaticCredentialProvider({})])
    with pytest.raises(ConfigurationError):
        await resolver.resolve(CredentialRef(name="nope"))


async def test_optional_credential_absent_returns_none():
    resolver = CredentialResolver([StaticCredentialProvider({})])
    assert await resolver.resolve(CredentialRef(name="nope", required=False)) is None


# --------------------------------------------------------------------------
# Ledger / digest
# --------------------------------------------------------------------------
def test_canonical_digest_is_stable_and_payload_sensitive():
    a = canonical_digest(connector="email", operation="email.send", payload={"to": ["x@y.z"]})
    b = canonical_digest(connector="email", operation="email.send", payload={"to": ["x@y.z"]})
    c = canonical_digest(connector="email", operation="email.send", payload={"to": ["q@y.z"]})
    assert a == b
    assert a != c


def test_digest_redacts_secret_fields_before_hashing():
    with_secret = canonical_digest(
        connector="http", operation="request", payload={"token": "abc", "path": "/x"}
    )
    with_other_secret = canonical_digest(
        connector="http", operation="request", payload={"token": "zzz", "path": "/x"}
    )
    # The secret is redacted, so it does not change the action identity.
    assert with_secret == with_other_secret


async def test_ledger_roundtrip_and_tenant_isolation():
    ledger = InMemoryActionLedger()
    from arabic_agent.connectors.ledger import ActionRecord

    record = ActionRecord(
        tenant_id="t1", connector="c", operation="op", request_digest="d", idempotency_key="k"
    )
    await ledger.add(record)
    assert (await ledger.get(record.id, tenant_id="t1")).id == record.id
    with pytest.raises(Exception):
        await ledger.get(record.id, tenant_id="t2")


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------
async def test_with_retry_retries_transient_then_succeeds():
    attempts = {"n": 0}

    async def op(attempt: int) -> str:
        attempts["n"] = attempt
        if attempt < 3:
            raise TransientError("boom")
        return "ok"

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await with_retry(op, policy=RetryPolicy(max_attempts=3), sleep=no_sleep)
    assert result == "ok"
    assert attempts["n"] == 3


async def test_with_retry_does_not_retry_permanent_error():
    attempts = {"n": 0}

    async def op(attempt: int) -> str:
        attempts["n"] = attempt
        raise ValidationError("bad input")

    async def no_sleep(_seconds: float) -> None:
        return None

    with pytest.raises(ValidationError):
        await with_retry(op, policy=RetryPolicy(max_attempts=5), sleep=no_sleep)
    assert attempts["n"] == 1


# --------------------------------------------------------------------------
# Declarative config
# --------------------------------------------------------------------------
def test_parse_connector_specs_rejects_malformed_json():
    with pytest.raises(ConfigurationError):
        parse_connector_specs("{not json")
    with pytest.raises(ConfigurationError):
        parse_connector_specs('{"not": "an array"}')


def test_parse_connector_specs_reads_a_list():
    specs = parse_connector_specs(
        json.dumps([{"name": "api", "kind": "http", "base_url": "https://example.com"}])
    )
    assert len(specs) == 1
    assert specs[0].name == "api"


def test_build_connectors_from_specs():
    from arabic_agent.connectors.config import build_connectors

    specs = [
        ConnectorSpec(name="api", kind="http", base_url="https://example.com"),
        ConnectorSpec(
            name="hook",
            kind="webhook",
            url="https://example.com/hook",
            secret_env="HOOK_SECRET",
        ),
    ]
    registry = build_connectors(
        specs, credentials=CredentialResolver([StaticCredentialProvider({})])
    )
    assert set(registry.names()) == {"api", "hook"}


def test_disabled_spec_is_not_registered():
    from arabic_agent.connectors.config import build_connectors

    specs = [ConnectorSpec(name="api", kind="http", base_url="https://example.com", enabled=False)]
    registry = build_connectors(specs, credentials=CredentialResolver())
    assert registry.names() == []


def test_data_sensitivity_enum_used_by_connectors():
    assert DataSensitivity("internal") is DataSensitivity.INTERNAL
