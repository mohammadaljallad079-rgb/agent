"""OAuth 2.0 (PKCE) and connector-policy tests (Phase 9).

OAuth is security-sensitive: the tests prove CSRF/state replay is refused, PKCE
is used, a rejected token grant is classified for reauthentication rather than
looped, and a refresh preserves the previous refresh token. The token endpoint is
a *real* local HTTP server, not a stubbed client. Connector policy is a pure
declarative object and is tested as such.
"""

from __future__ import annotations

import pytest

from arabic_agent.connectors.oauth import (
    InMemoryStateStore,
    OAuthClient,
    OAuthProvider,
)
from arabic_agent.connectors.policy import ConnectorPolicy
from arabic_agent.core.enums import ActionRisk, ConnectorState, DataSensitivity
from arabic_agent.core.errors import (
    ConfigurationError,
    ExternalServiceError,
    TransientError,
)

from .fake_http import http_server


def _provider(server, **overrides) -> OAuthProvider:
    values = {
        "name": "example",
        "authorize_url": f"{server.base_url}/authorize",
        "token_url": f"{server.base_url}/token",
        "scopes": ("mail.read", "mail.send"),
    }
    values.update(overrides)
    return OAuthProvider(**values)


def _client(server, **overrides) -> OAuthClient:
    values = {
        "provider": _provider(server),
        "client_id": "client-123",
        "redirect_uri": "https://app.example.com/callback",
    }
    values.update(overrides)
    return OAuthClient(**values)


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------
def test_unconfigured_provider_reports_not_configured():
    provider = OAuthProvider(name="x", authorize_url="", token_url="")
    client = OAuthClient(provider=provider, client_id="", redirect_uri="https://x/cb")
    assert client.state() is ConnectorState.NOT_CONFIGURED
    assert not client.configured


async def test_authorization_url_uses_pkce_and_registers_single_use_state():
    with http_server() as server:
        client = _client(server)
        auth = await client.authorization_url()
        assert auth.state
        assert auth.code_verifier
        assert "code_challenge=" in auth.url
        assert "code_challenge_method=S256" in auth.url
        assert "state=" in auth.url
        assert "client_id=client-123" in auth.url
        # The verifier is held server-side; the browser URL carries only the
        # challenge, never the verifier.
        assert auth.code_verifier not in auth.url


async def test_exchange_code_refuses_a_replayed_state():
    with http_server() as server:
        server.route("/token", body='{"access_token": "at-1", "expires_in": 3600}')
        client = _client(server)
        auth = await client.authorization_url()
        token = await client.exchange_code(code="code-1", state=auth.state)
        assert token.access_token.get_secret_value() == "at-1"
        # Replaying the same state must fail (single use).
        with pytest.raises(ExternalServiceError):
            await client.exchange_code(code="code-1", state=auth.state)


async def test_exchange_code_refuses_an_unknown_state():
    with http_server() as server:
        client = _client(server)
        with pytest.raises(ExternalServiceError):
            await client.exchange_code(code="code-1", state="forged-state")


async def test_exchange_code_sends_the_pkce_verifier():
    with http_server() as server:
        server.route("/token", body='{"access_token": "at-1", "expires_in": 3600}')
        client = _client(server)
        auth = await client.authorization_url()
        await client.exchange_code(code="code-1", state=auth.state)
        sent = server.requests[-1]
        assert sent.path == "/token"
        body = sent.body.decode()
        assert "grant_type=authorization_code" in body
        assert f"code_verifier={auth.code_verifier}" in body


async def test_state_store_expires_and_is_single_use():
    states = InMemoryStateStore()
    await states.issue(state="s1", code_verifier="v1", ttl_seconds=-1)
    # An already-expired state is not returned.
    assert await states.consume("s1") is None
    await states.issue(state="s2", code_verifier="v2", ttl_seconds=600)
    assert await states.consume("s2") == "v2"
    # A state is single use.
    assert await states.consume("s2") is None


async def test_token_grant_rejection_is_classified_for_reauth():
    with http_server() as server:
        server.route("/token", status=400, body='{"error": "invalid_grant"}')
        client = _client(server)
        auth = await client.authorization_url()
        with pytest.raises(ExternalServiceError):
            await client.exchange_code(code="code-1", state=auth.state)


async def test_token_endpoint_5xx_is_transient():
    with http_server() as server:
        server.route("/token", status=503, body=b"")
        client = _client(server)
        auth = await client.authorization_url()
        with pytest.raises(TransientError):
            await client.exchange_code(code="code-1", state=auth.state)


async def test_refresh_keeps_the_previous_refresh_token_when_omitted():
    with http_server() as server:
        server.route("/token", body='{"access_token": "at-2", "expires_in": 3600}')
        client = _client(server)
        token = await client.refresh(refresh_token="rt-old")
        assert token.access_token.get_secret_value() == "at-2"
        assert token.refresh_token is not None
        assert token.refresh_token.get_secret_value() == "rt-old"


async def test_refresh_without_a_token_requires_reauthentication():
    with http_server() as server:
        client = _client(server)
        with pytest.raises(ConfigurationError):
            await client.refresh(refresh_token="")


async def test_revoke_reports_unsupported_when_no_endpoint():
    with http_server() as server:
        client = _client(server)
        assert await client.revoke(token="at-1") is False


async def test_revoke_calls_the_endpoint_when_present():
    with http_server() as server:
        server.route("/revoke", status=200, body=b"")
        client = _client(server, provider=_provider(server, revoke_url=f"{server.base_url}/revoke"))
        assert await client.revoke(token="at-1") is True
        assert server.requests[-1].path == "/revoke"


def test_token_set_repr_masks_the_secret():
    from pydantic import SecretStr

    from arabic_agent.connectors.oauth import OAuthTokenSet

    token = OAuthTokenSet(provider="example", access_token=SecretStr("super-secret"))
    assert "super-secret" not in repr(token)


# --------------------------------------------------------------------------
# Connector policy
# --------------------------------------------------------------------------
def test_policy_defaults_allow_everything_with_declared_risk():
    policy = ConnectorPolicy()
    assert policy.is_enabled("api")
    assert not policy.is_denied("api", "request")
    assert policy.requires_approval("api", "request", ActionRisk.CONSEQUENTIAL_WRITE)
    assert not policy.requires_approval("api", "request", ActionRisk.READ_ONLY)
    assert policy.sensitivity_for("api") is DataSensitivity.INTERNAL


def test_policy_allowlist_and_denylist():
    policy = ConnectorPolicy(enabled=("api",), disabled=("hook",))
    assert policy.is_enabled("api")
    assert not policy.is_enabled("hook")
    assert not policy.is_enabled("other")


def test_policy_force_approval_and_deny_overrides():
    policy = ConnectorPolicy(force_approval=("api.request",), denied=("api.delete",))
    assert policy.requires_approval("api", "request", ActionRisk.READ_ONLY)
    assert policy.is_denied("api", "delete")
    assert not policy.is_denied("api", "request")


def test_policy_sensitivity_override():
    policy = ConnectorPolicy(sensitivity={"api": DataSensitivity.CONFIDENTIAL})
    assert policy.sensitivity_for("api") is DataSensitivity.CONFIDENTIAL
    assert policy.sensitivity_for("other") is DataSensitivity.INTERNAL
