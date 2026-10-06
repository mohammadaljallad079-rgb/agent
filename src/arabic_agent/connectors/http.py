"""A controlled HTTP/REST connector.

This is the generic transport for any JSON HTTP API a business system exposes.
It is deliberately **not** an unrestricted, model-controlled ``curl``: the model
proposes a request, and this connector decides whether it is allowed and how it
is bounded.

Hard controls (all reusing Phase 8's URL security):

* **URL policy.** The target is validated by
  :class:`~arabic_agent.browser.url_security.UrlPolicy` *before* any socket is
  opened: dangerous schemes are refused, credential-bearing URLs are refused,
  and loopback/private/link-local hosts (including the cloud metadata address
  ``169.254.169.254``) are refused unless the operator explicitly opted in. An
  allow-list, when set, is the only set of hosts reachable.
* **Method policy.** Only the methods the operator enabled are accepted; DELETE
  and other destructive verbs must be opted into explicitly and are classified
  ``HIGH_RISK_ACTION``.
* **Bounded responses.** The body is read with a hard byte cap; a response that
  declares or streams more than the cap is truncated/refused rather than
  buffered into memory. ``Content-Length`` is checked first, then the streamed
  bytes.
* **Safe redirects.** Redirects are not followed automatically. Each ``Location``
  is re-validated against the URL policy and the redirect count is bounded, and
  the credential header is *stripped* when the redirect leaves the configured
  origin — so a redirect can neither reach a blocked host nor leak the secret to
  a third party.
* **Secret containment.** A credential is attached only to the configured host;
  it never travels to a redirect target on another domain and is never logged.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..browser.url_security import UrlPolicy
from ..core.enums import ActionRisk, ConnectorKind, ConnectorState
from ..core.errors import (
    ExternalServiceError,
    PermissionDeniedError,
    TransientError,
    ValidationError,
)
from .base import (
    Connector,
    ConnectorCall,
    ConnectorCapability,
    ConnectorHealth,
    ConnectorOperation,
    ConnectorResult,
    OperationKind,
)
from .credentials import CredentialRef, CredentialResolver, ResolvedCredential
from .retry import RetryPolicy, is_retryable_status, with_retry

#: Methods considered safe to expose by default. Destructive verbs are opt-in.
DEFAULT_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH")
DESTRUCTIVE_METHODS: frozenset[str] = frozenset({"DELETE"})
#: Default cap on a response body (1 MiB) and on redirects.
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_MAX_REDIRECTS = 3
#: Header names that must never be echoed back to the model or persisted.
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})


class HttpRequest(BaseModel):
    """A model-proposed HTTP request, before it is validated and bounded."""

    model_config = ConfigDict(frozen=True)

    method: str = "GET"
    path: str = ""
    url: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    #: A pre-serialised body. Used when the exact bytes must be signed (a
    #: webhook signature covers the precise payload, so it cannot be re-encoded).
    body: str | None = None
    timeout_seconds: float | None = None


class HttpResponse(BaseModel):
    """A normalized, bounded, redacted HTTP response."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    status: int
    content_type: str = ""
    #: Bounded, redacted body text (may be truncated).
    body: str = ""
    truncated: bool = False
    headers: dict[str, str] = Field(default_factory=dict)

    def summary(self) -> str:
        return f"HTTP {self.status} ({self.content_type or 'unknown'})"


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip credential-bearing headers from anything the model can see."""
    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


class HttpConnector(Connector):
    """A bounded, policy-checked JSON HTTP connector."""

    kind = ConnectorKind.HTTP
    capabilities = (ConnectorCapability.HTTP_REQUEST,)
    operations = (
        ConnectorOperation(
            name="request", kind=OperationKind.WRITE, risk=ActionRisk.LOW_RISK_WRITE
        ),
        ConnectorOperation(
            name="delete", kind=OperationKind.WRITE, risk=ActionRisk.HIGH_RISK_ACTION
        ),
    )

    def __init__(
        self,
        *,
        name: str,
        base_url: str = "",
        url_policy: UrlPolicy | None = None,
        allowed_methods: tuple[str, ...] = DEFAULT_METHODS,
        credential: CredentialRef | None = None,
        credentials: CredentialResolver | None = None,
        credential_header: str = "Authorization",
        credential_scheme: str = "Bearer",
        default_headers: dict[str, str] | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        timeout_seconds: float = 20.0,
        retry: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        # A connector with a base URL must still pass the URL policy; a bare
        # base URL on a private host is refused unless the policy allows it.
        self._url_policy = url_policy or UrlPolicy()
        self._methods = frozenset(m.upper() for m in allowed_methods)
        self._credential = credential
        self._credentials = credentials or CredentialResolver()
        self._credential_header = credential_header
        self._credential_scheme = credential_scheme
        self._default_headers = dict(default_headers or {})
        self._max_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._timeout = timeout_seconds
        self._retry = retry or RetryPolicy()
        self._client = client
        self._owns_client = client is None
        super().__init__()

    # -- Health ---------------------------------------------------------
    def health(self) -> ConnectorHealth:
        if not self._base_url:
            return ConnectorHealth(
                state=ConnectorState.NOT_CONFIGURED,
                detail="no base URL configured",
                read_only=not self.write_capable,
            )
        decision = self._url_policy.evaluate(self._base_url)
        if not decision.allowed:
            return ConnectorHealth(
                state=ConnectorState.UNAVAILABLE,
                configured=True,
                detail=f"base URL refused by policy: {decision.reason}",
                read_only=not self.write_capable,
            )
        return ConnectorHealth(
            state=ConnectorState.READY,
            configured=True,
            # Presence of a credential reference; resolution (and thus the real
            # value) happens only at the execution boundary.
            authenticated=self._credential is None or self._credentials.providers() != (),
            read_only=not self.write_capable,
            detail="configured",
        )

    async def healthcheck(self) -> ConnectorHealth:
        health = self.health()
        if health.state is not ConnectorState.READY or self._credential is None:
            return health
        present = await self._credentials.has(self._credential)
        if not present:
            return ConnectorHealth(
                state=ConnectorState.REAUTHENTICATION_REQUIRED,
                configured=True,
                authenticated=False,
                read_only=not self.write_capable,
                detail=f"credential {self._credential.name!r} is not available",
            )
        return health

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # follow_redirects is False: we validate every hop ourselves.
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        return self._client

    def _resolve_url(self, request: HttpRequest) -> str:
        if request.url:
            target = request.url
        elif request.path:
            if not self._base_url:
                raise ValidationError("connector has no base URL; provide an absolute url")
            target = f"{self._base_url}/{request.path.lstrip('/')}"
        elif self._base_url:
            target = self._base_url
        else:
            raise ValidationError("request must provide a url or path")
        decision = self._url_policy.evaluate(target)
        if not decision.allowed:
            raise PermissionDeniedError(
                f"HTTP request refused: {decision.reason}",
                details={"host": decision.host, "url": target},
            )
        return target

    def _auth_headers(self, credential: ResolvedCredential | None, *, host: str) -> dict[str, str]:
        headers = dict(self._default_headers)
        if credential is None:
            return headers
        # Attach the secret only to the configured origin. A redirect to another
        # host is sent without it (see _send).
        if self._base_url and host and not self._same_origin(self._base_url, host):
            return headers
        value = credential.reveal()
        headers[self._credential_header] = (
            f"{self._credential_scheme} {value}" if self._credential_scheme else value
        )
        return headers

    @staticmethod
    def _same_origin(base_url: str, host: str) -> bool:
        from urllib.parse import urlsplit

        return urlsplit(base_url).hostname == host

    async def call(self, call: ConnectorCall) -> ConnectorResult:
        request = self._parse(call)
        response = await self.request(request)
        data = {
            "status": response.status,
            "content_type": response.content_type,
            "body": response.body,
            "truncated": response.truncated,
        }
        if not response.ok:
            return ConnectorResult.failure(response.summary(), data=data)
        return ConnectorResult.success(response.summary(), data=data)

    def _parse(self, call: ConnectorCall) -> HttpRequest:
        if call.operation not in ("request", "delete"):
            raise ValidationError(f"http connector does not support {call.operation!r}")
        payload = dict(call.payload)
        if call.operation == "delete":
            payload.setdefault("method", "DELETE")
        try:
            return HttpRequest(**payload)
        except Exception as exc:  # pydantic ValidationError
            raise ValidationError(f"invalid http request: {exc}") from exc

    async def request(self, request: HttpRequest) -> HttpResponse:
        """Validate, bound, send and normalize one HTTP request."""
        method = request.method.upper()
        if method not in self._methods:
            raise PermissionDeniedError(
                f"HTTP method {method} is not permitted by this connector",
                details={"method": method, "allowed": sorted(self._methods)},
            )
        target = self._resolve_url(request)
        host = self._url_policy.evaluate(target).host
        credential: ResolvedCredential | None = None
        if self._credential is not None:
            credential = await self._credentials.resolve(self._credential)
        headers = self._auth_headers(credential, host=host)
        headers.update(request.headers)
        headers.setdefault("Accept", "application/json")

        client = await self._get_client()

        async def _attempt(_attempt: int) -> HttpResponse:
            return await self._send(
                client,
                method=method,
                url=target,
                params=request.params,
                headers=headers,
                json_body=request.json_body,
                body=request.body,
                timeout=request.timeout_seconds or self._timeout,
                credential=credential,
            )

        return await with_retry(
            _attempt,
            policy=self._retry,
            retry_after=lambda exc: (
                exc.details.get("retry_after") if isinstance(exc, TransientError) else None
            ),
        )

    async def _send(
        self,
        client: httpx.AsyncClient,
        *,
        method: str,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        body: str | None,
        timeout: float,  # noqa: ASYNC109 - a per-request budget, not an async timeout
        credential: ResolvedCredential | None,
    ) -> HttpResponse:
        current = url
        for hop in range(self._max_redirects + 1):
            request_kwargs: dict[str, Any] = {}
            if hop == 0:
                request_kwargs["params"] = params
                if body is not None:
                    request_kwargs["content"] = body.encode("utf-8")
                elif json_body is not None:
                    request_kwargs["json"] = json_body
            try:
                response = await client.request(
                    method,
                    current,
                    headers=headers,
                    timeout=timeout,
                    **request_kwargs,
                )
            except httpx.TimeoutException as exc:
                raise TransientError(
                    f"http request timed out: {exc}", details={"url": current}
                ) from exc
            except httpx.HTTPError as exc:
                raise TransientError(f"http transport error: {exc}") from exc

            if response.is_redirect and response.headers.get("location"):
                if hop >= self._max_redirects:
                    raise ExternalServiceError("too many redirects")
                location = response.headers["location"]
                next_url = str(httpx.URL(current).join(location))
                decision = self._url_policy.evaluate(next_url)
                if not decision.allowed:
                    raise PermissionDeniedError(
                        f"redirect refused: {decision.reason}",
                        details={"host": decision.host, "url": next_url},
                    )
                # Never carry the credential to a different origin.
                if credential is not None and not self._same_origin(current, decision.host):
                    headers = _safe_headers(headers)
                    headers.pop(self._credential_header, None)
                current = next_url
                continue

            if is_retryable_status(response.status_code):
                retry_after = _retry_after_seconds(response)
                raise TransientError(
                    f"http {response.status_code}",
                    details={"status": response.status_code, "retry_after": retry_after},
                )
            return self._normalize(response)

        raise ExternalServiceError("redirect loop not resolved")  # pragma: no cover

    def _normalize(self, response: httpx.Response) -> HttpResponse:
        content_type = response.headers.get("content-type", "")
        # Check the declared length first, then bound the actual read so a lying
        # or absent Content-Length cannot defeat the cap.
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            raise ExternalServiceError(
                "response exceeds the size limit",
                details={"limit": self._max_bytes, "declared": int(declared)},
            )
        raw = response.content
        truncated = len(raw) > self._max_bytes
        body_bytes = raw[: self._max_bytes]
        body = body_bytes.decode("utf-8", errors="replace")
        return HttpResponse(
            ok=response.status_code < 400,
            status=response.status_code,
            content_type=content_type,
            body=body,
            truncated=truncated,
            headers=_safe_headers(dict(response.headers)),
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_METHODS",
    "DESTRUCTIVE_METHODS",
    "HttpConnector",
    "HttpRequest",
    "HttpResponse",
]
