"""A generic webhook integration.

This is a real, working connector for any HTTP endpoint configured via
environment variables — the escape hatch that makes "future integrations"
possible without shipping vendor-specific code yet. It is intentionally
*disabled* unless a URL is configured; it never pretends to work.
"""

from __future__ import annotations

import httpx

from ..core.errors import ConfigurationError, ExternalServiceError, TransientError
from .base import Integration, IntegrationCall, IntegrationResult


class WebhookIntegration(Integration):
    """Posts JSON payloads to a configured HTTP endpoint."""

    operations = ("post",)

    def __init__(
        self,
        *,
        name: str,
        url: str = "",
        auth_header: str = "",
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._url = url
        self._auth_header = auth_header
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self.configured = bool(url)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def call(self, call: IntegrationCall) -> IntegrationResult:
        if not self.configured:
            raise ConfigurationError(f"integration {self.name!r} has no URL configured")
        if call.operation != "post":
            raise ConfigurationError(
                f"integration {self.name!r} does not support {call.operation!r}"
            )
        headers = {"Content-Type": "application/json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        client = await self._get_client()
        try:
            response = await client.post(self._url, headers=headers, json=call.payload)
        except httpx.HTTPError as exc:
            raise TransientError(f"{self.name} transport error: {exc}") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(f"{self.name} returned {response.status_code}")
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"{self.name} rejected request: {response.status_code}",
                details={"body": response.text[:300]},
            )
        return IntegrationResult.success(
            f"{self.name} accepted the request",
            data={"status": response.status_code},
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
