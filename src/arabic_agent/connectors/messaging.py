"""Provider-neutral messaging connector.

Messaging (WhatsApp Business, SMS, other chat platforms) is expressed through one
provider-neutral abstraction: a *conversation*, a *recipient*, a *message* with
optional attachments, a *send*, a *read*, and a *delivery result*. A provider is
an adapter that implements :class:`MessagingProvider`; the connector is the
agent-facing wrapper that classifies operations and gates sending.

The only provider shipped here is the **official WhatsApp Business Cloud API**
adapter, implemented against Meta's documented Graph API endpoints. It is honest
about verification:

    Real WhatsApp provider: NOT VERIFIED — OFFICIAL PROVIDER CREDENTIALS NOT AVAILABLE.

The adapter is real code that would talk to the real API given a token and phone
number id; the acceptance suite cannot mark it verified because no official
credentials are available in this environment. What *is* verified is the
provider-neutral architecture and the local mock adapter that exercises the same
code path against a real local HTTP service. We never reverse-engineer personal
WhatsApp, never scrape WhatsApp Web, and never bypass platform protections.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from ..core.enums import ActionRisk, ConnectorKind, ConnectorState
from ..core.errors import (
    ConfigurationError,
    ExternalServiceError,
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
from .credentials import CredentialRef, CredentialResolver
from .retry import RetryPolicy, is_retryable_status, with_retry


class MessagingMessage(BaseModel):
    """A normalized message to send or one that was received."""

    model_config = ConfigDict(frozen=True)

    conversation_id: str = ""
    recipient: str = ""
    body: str = ""
    #: Workspace paths of files to attach (authorized by the caller's sandbox).
    attachments: tuple[str, ...] = ()
    #: The provider's own message id, once sent/received.
    provider_message_id: str = ""


class DeliveryResult(BaseModel):
    """The provider's delivery result for a sent message."""

    model_config = ConfigDict(frozen=True)

    provider_message_id: str
    status: str = "accepted"
    accepted: bool = True


class MessagingProvider(ABC):
    """A provider adapter: the only provider-specific code in messaging."""

    name: str

    @abstractmethod
    async def send(self, message: MessagingMessage) -> DeliveryResult:
        """Send a message; raise a PlatformError subclass on failure."""

    @abstractmethod
    async def read(self, *, conversation_id: str, limit: int) -> list[MessagingMessage]:
        """Read recent messages from a conversation."""

    @abstractmethod
    async def verify(self, provider_message_id: str) -> DeliveryResult:
        """Look up a message by provider id to confirm delivery (timeout safety)."""

    def state(self) -> ConnectorState:
        return ConnectorState.READY

    def detail(self) -> str:
        return "configured"

    async def aclose(self) -> None:
        return None


class WhatsAppBusinessProvider(MessagingProvider):
    """Official WhatsApp Business Cloud API (Meta Graph API) adapter.

    Real code for the real API; marked NOT VERIFIED in this environment because
    no official credentials are available. It never touches personal WhatsApp or
    WhatsApp Web.
    """

    name = "whatsapp_business"

    def __init__(
        self,
        *,
        phone_number_id: str = "",
        api_version: str = "v20.0",
        base_url: str = "https://graph.facebook.com",
        access_token: CredentialRef | None = None,
        credentials: CredentialResolver | None = None,
        timeout_seconds: float = 20.0,
        retry: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._credentials = credentials or CredentialResolver()
        self._timeout = timeout_seconds
        self._retry = retry or RetryPolicy()
        self._client = client
        self._owns_client = client is None

    def state(self) -> ConnectorState:
        if not self._phone_number_id:
            return ConnectorState.NOT_CONFIGURED
        return ConnectorState.READY

    def detail(self) -> str:
        if not self._phone_number_id:
            return "official WhatsApp Business Cloud API: no phone_number_id configured"
        return "official WhatsApp Business Cloud API"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _messages_url(self) -> str:
        return f"{self._base_url}/{self._api_version}/{self._phone_number_id}/messages"

    async def _token(self) -> str:
        if self._access_token is None:
            raise ConfigurationError("whatsapp access token is not configured")
        credential = await self._credentials.resolve(self._access_token)
        if credential is None:
            raise ConfigurationError("whatsapp access token is not available")
        return credential.reveal()

    async def send(self, message: MessagingMessage) -> DeliveryResult:
        if self.state() is not ConnectorState.READY:
            raise ConfigurationError("whatsapp business provider is not configured")
        if not message.recipient:
            raise ValidationError("whatsapp send requires a recipient phone number")
        if message.attachments:
            # Attaching media requires a two-step upload; not implemented, and
            # said so honestly rather than silently dropping the attachment.
            raise ValidationError("whatsapp media attachments are not implemented in this phase")
        token = await self._token()
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message.body},
        }
        client = await self._get_client()

        async def _attempt(_n: int) -> DeliveryResult:
            try:
                response = await client.post(
                    self._messages_url(),
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
            except httpx.TimeoutException as exc:
                raise TransientError(f"whatsapp send timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise TransientError(f"whatsapp transport error: {exc}") from exc
            if is_retryable_status(response.status_code):
                raise TransientError(f"whatsapp returned {response.status_code}")
            if response.status_code >= 400:
                raise ExternalServiceError(
                    f"whatsapp rejected request ({response.status_code})",
                    details={"status": response.status_code, "body": response.text[:300]},
                )
            return self._delivery(response.json())

        return await with_retry(_attempt, policy=self._retry)

    def _delivery(self, payload: dict[str, Any]) -> DeliveryResult:
        messages = payload.get("messages") or []
        message_id = str(messages[0].get("id", "")) if messages else ""
        return DeliveryResult(provider_message_id=message_id, status="accepted")

    async def read(self, *, conversation_id: str, limit: int) -> list[MessagingMessage]:
        # Reading inbound messages requires a webhook subscription, which is a
        # deployment concern beyond this phase; report honestly.
        raise ValidationError("whatsapp inbound read requires a configured webhook subscription")

    async def verify(self, provider_message_id: str) -> DeliveryResult:
        # The Cloud API does not expose a per-message status GET; delivery
        # receipts arrive via webhook. Verification is therefore not available
        # synchronously and is reported as unknown rather than faked as accepted.
        return DeliveryResult(
            provider_message_id=provider_message_id,
            status="unknown",
            accepted=False,
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class MessagingConnector(Connector):
    """Agent-facing messaging connector over a provider adapter."""

    kind = ConnectorKind.MESSAGING
    capabilities = (
        ConnectorCapability.MESSAGE_SEND,
        ConnectorCapability.MESSAGE_READ,
    )
    operations = (
        ConnectorOperation(
            name="message.send", kind=OperationKind.WRITE, risk=ActionRisk.CONSEQUENTIAL_WRITE
        ),
        ConnectorOperation(name="message.read", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY),
        ConnectorOperation(
            name="message.verify", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
        ),
    )

    def __init__(self, *, name: str, provider: MessagingProvider) -> None:
        self.name = name
        self._provider = provider
        super().__init__()

    def health(self) -> ConnectorHealth:
        state = self._provider.state()
        return ConnectorHealth(
            state=state,
            configured=state is not ConnectorState.NOT_CONFIGURED,
            authenticated=state is ConnectorState.READY,
            read_only=not self.write_capable,
            detail=self._provider.detail(),
        )

    async def healthcheck(self) -> ConnectorHealth:
        return self.health()

    async def call(self, call: ConnectorCall) -> ConnectorResult:
        payload = dict(call.payload)
        match call.operation:
            case "message.send":
                result = await self._provider.send(MessagingMessage(**payload))
                return ConnectorResult.success(
                    f"sent message via {self.name}",
                    data={
                        "provider_message_id": result.provider_message_id,
                        "status": result.status,
                        "accepted": result.accepted,
                    },
                )
            case "message.read":
                messages = await self._provider.read(
                    conversation_id=str(payload.get("conversation_id") or ""),
                    limit=max(1, min(int(payload.get("limit") or 20), 100)),
                )
                return ConnectorResult.success(
                    f"read {len(messages)} message(s)",
                    data={"messages": [m.model_dump() for m in messages]},
                )
            case "message.verify":
                result = await self._provider.verify(str(payload.get("provider_message_id") or ""))
                return ConnectorResult.success(
                    f"delivery status: {result.status}",
                    data=result.model_dump(),
                )
            case _:
                raise ValidationError(f"unsupported messaging operation: {call.operation!r}")

    async def aclose(self) -> None:
        await self._provider.aclose()


__all__ = [
    "DeliveryResult",
    "MessagingConnector",
    "MessagingMessage",
    "MessagingProvider",
    "WhatsAppBusinessProvider",
]
