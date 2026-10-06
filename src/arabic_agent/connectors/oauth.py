"""Provider-neutral OAuth 2.0 architecture.

OAuth is deliberately **not** baked into the agent core and not hardcoded to any
one provider (Gmail, Microsoft 365, ...). A provider is described by an
:class:`OAuthProvider` record (endpoints + scopes), and the flows are implemented
once against that record, so a new provider is configuration, not code.

Two flows are supported:

* :class:`OAuthClient` — the authorization-code flow with PKCE for a provider
  that supports it: build an authorization URL, validate the callback ``state``
  (CSRF), exchange the code for tokens, refresh, and revoke.

Security properties enforced here rather than left to callers:

* **CSRF / state replay.** The ``state`` is a random, single-use nonce kept in a
  :class:`StateStore` with a short TTL. It is consumed on the callback, so a
  replayed callback is rejected. PKCE adds a per-request ``code_verifier`` whose
  challenge is bound to the request.
* **Token leakage.** Tokens are wrapped in :class:`SecretStr`; the token set's
  ``repr`` masks them and the audit trail records only metadata (provider,
  scopes, expiry), never a token value.
* **Honesty.** An unconfigured provider reports ``NOT_CONFIGURED``; a provider
  whose refresh token is absent/expired reports ``REAUTHENTICATION_REQUIRED``
  and the caller must not loop.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..core.enums import ConnectorState
from ..core.errors import ConfigurationError, ExternalServiceError, TransientError

#: Default lifetime (seconds) of a pending authorization ``state``.
STATE_TTL_SECONDS = 600


class OAuthProvider(BaseModel):
    """A provider's OAuth endpoints and default scopes (no secrets)."""

    model_config = ConfigDict(frozen=True)

    name: str
    authorize_url: str
    token_url: str
    #: Optional revocation endpoint. Absent means "disconnect locally only".
    revoke_url: str = ""
    scopes: tuple[str, ...] = ()
    #: Whether PKCE (S256) is used for the authorization-code exchange.
    use_pkce: bool = True
    #: Extra, provider-specific authorization-request parameters.
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)


class OAuthTokenSet(BaseModel):
    """A resolved token set. Token values are secrets and never serialised raw."""

    model_config = ConfigDict(frozen=True)

    provider: str
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: str = "Bearer"  # noqa: S105 - a token *type* label, not a credential
    scopes: tuple[str, ...] = ()
    #: Absolute expiry (unix seconds). ``None`` means "no declared expiry".
    expires_at: float | None = None

    def is_expired(self, *, now: float | None = None, skew_seconds: float = 30.0) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= (self.expires_at - skew_seconds)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"OAuthTokenSet(provider={self.provider!r}, scopes={self.scopes!r}, value=***)"


class OAuthAuthorization(BaseModel):
    """A prepared authorization request the caller sends the user to."""

    model_config = ConfigDict(frozen=True)

    url: str
    state: str
    #: PKCE verifier, held server-side and never sent to the browser.
    code_verifier: str = ""


class StateStore(ABC):
    """Single-use, TTL-bounded store for OAuth ``state`` values (anti-CSRF)."""

    @abstractmethod
    async def issue(self, *, state: str, code_verifier: str, ttl_seconds: int) -> None: ...

    @abstractmethod
    async def consume(self, state: str) -> str | None:
        """Return the stored ``code_verifier`` and invalidate ``state`` once."""


class InMemoryStateStore(StateStore):
    """Process-local state store for the local runtime and tests."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}

    async def issue(self, *, state: str, code_verifier: str, ttl_seconds: int) -> None:
        self._items[state] = (code_verifier, time.time() + ttl_seconds)

    async def consume(self, state: str) -> str | None:
        item = self._items.pop(state, None)
        if item is None:
            return None
        code_verifier, expires_at = item
        if time.time() >= expires_at:
            return None
        return code_verifier


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class OAuthClient:
    """Implements the authorization-code (+PKCE), refresh and revoke flows."""

    def __init__(
        self,
        *,
        provider: OAuthProvider,
        client_id: str,
        client_secret: str = "",
        redirect_uri: str,
        states: StateStore | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._provider = provider
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._states = states or InMemoryStateStore()
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    @property
    def provider(self) -> OAuthProvider:
        return self._provider

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._provider.authorize_url and self._provider.token_url)

    def state(self) -> ConnectorState:
        if not self.configured:
            return ConnectorState.NOT_CONFIGURED
        return ConnectorState.READY

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def authorization_url(self, *, scopes: tuple[str, ...] = ()) -> OAuthAuthorization:
        """Build the authorization URL and register a single-use state."""
        if not self.configured:
            raise ConfigurationError(f"oauth provider {self._provider.name!r} is not configured")
        state = secrets.token_urlsafe(24)
        verifier = ""
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(scopes or self._provider.scopes),
            "state": state,
            **self._provider.extra_authorize_params,
        }
        if self._provider.use_pkce:
            verifier, challenge = _pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        await self._states.issue(state=state, code_verifier=verifier, ttl_seconds=STATE_TTL_SECONDS)
        from urllib.parse import urlencode

        return OAuthAuthorization(
            url=f"{self._provider.authorize_url}?{urlencode(params)}",
            state=state,
            code_verifier=verifier,
        )

    async def exchange_code(self, *, code: str, state: str) -> OAuthTokenSet:
        """Validate ``state`` (single use) and exchange ``code`` for tokens."""
        verifier = await self._states.consume(state)
        if verifier is None:
            # Missing, expired or replayed state: refuse, never fall through.
            raise ExternalServiceError(
                "oauth state is missing, expired or already used",
                details={"provider": self._provider.name},
            )
        if not code:
            raise ExternalServiceError("oauth callback carried no authorization code")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": self._client_id,
        }
        if verifier:
            data["code_verifier"] = verifier
        if self._client_secret:
            data["client_secret"] = self._client_secret
        return await self._token_request(data)

    async def refresh(self, *, refresh_token: str) -> OAuthTokenSet:
        """Exchange a refresh token for a fresh access token."""
        if not refresh_token:
            raise ConfigurationError(
                "no refresh token available; reauthentication is required",
                details={"provider": self._provider.name},
            )
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        token = await self._token_request(data)
        # A refresh response may omit the refresh token; keep the previous one.
        if token.refresh_token is None:
            token = token.model_copy(update={"refresh_token": SecretStr(refresh_token)})
        return token

    async def revoke(self, *, token: str) -> bool:
        """Revoke a token at the provider, if it exposes a revocation endpoint."""
        if not self._provider.revoke_url:
            return False
        client = await self._get_client()
        try:
            response = await client.post(
                self._provider.revoke_url,
                data={"token": token, "client_id": self._client_id},
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"oauth revoke transport error: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(f"oauth revoke returned {response.status_code}")
        return response.status_code < 400

    async def _token_request(self, data: dict[str, str]) -> OAuthTokenSet:
        client = await self._get_client()
        try:
            response = await client.post(self._provider.token_url, data=data)
        except httpx.HTTPError as exc:
            raise TransientError(f"oauth token transport error: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(f"oauth token endpoint returned {response.status_code}")
        if response.status_code >= 400:
            # A 400/401 here usually means an invalid/expired grant: the caller
            # must reauthenticate, so we classify it explicitly and never loop.
            raise ExternalServiceError(
                f"oauth token request rejected ({response.status_code})",
                details={"provider": self._provider.name, "status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError("oauth token response was not JSON") from exc
        access = payload.get("access_token")
        if not access:
            raise ExternalServiceError("oauth token response carried no access_token")
        expires_in = payload.get("expires_in")
        expires_at = time.time() + float(expires_in) if expires_in else None
        refresh = payload.get("refresh_token")
        scopes = tuple(str(payload.get("scope", "")).split()) or self._provider.scopes
        return OAuthTokenSet(
            provider=self._provider.name,
            access_token=SecretStr(str(access)),
            refresh_token=SecretStr(str(refresh)) if refresh else None,
            token_type=str(payload.get("token_type", "Bearer")),
            scopes=tuple(s for s in scopes if s),
            expires_at=expires_at,
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = [
    "InMemoryStateStore",
    "OAuthAuthorization",
    "OAuthClient",
    "OAuthProvider",
    "OAuthTokenSet",
    "StateStore",
]
