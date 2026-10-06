"""Outgoing webhook connector.

An outgoing webhook posts a structured JSON payload to a configured endpoint and
proves authenticity with an HMAC signature over the *exact* bytes sent (the same
scheme the inbound trigger webhooks verify, so a platform can talk to itself or
to a partner symmetrically).

Properties:

* the endpoint is validated by the URL policy before the request (reusing the
  HTTP connector's SSRF/domain controls);
* the payload is signed with ``sha256=<hex>`` over ``"{timestamp}.{body}"`` and
  the timestamp is sent as a header, so the receiver can reject a replay;
* the delivery is a consequential external action, so the permission/approval
  layer gates it and the ledger records it;
* the response is bounded and redacted, and the signing secret is never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ActionRisk, ConnectorKind, ConnectorState
from ..core.errors import ConfigurationError, ValidationError
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
from .http import HttpConnector, HttpRequest

#: Header names used for the signature and its timestamp.
SIGNATURE_HEADER = "X-AAP-Signature"
TIMESTAMP_HEADER = "X-AAP-Timestamp"
DELIVERY_HEADER = "X-AAP-Delivery-Id"


def sign_payload(secret: str, *, timestamp: str, body: bytes) -> str:
    """``sha256=<hex>`` over ``"{timestamp}.{body}"`` (mirrors inbound verify)."""
    message = timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookPayload(BaseModel):
    """A structured webhook delivery."""

    model_config = ConfigDict(frozen=True)

    event: str = "event"
    data: dict[str, Any] = Field(default_factory=dict)
    #: A caller-supplied idempotency/delivery id, echoed so a receiver can dedupe.
    delivery_id: str = ""


class WebhookConnector(Connector):
    """Signs and posts structured payloads to a configured endpoint."""

    kind = ConnectorKind.WEBHOOK
    capabilities = (ConnectorCapability.WEBHOOK_SEND,)
    operations = (
        ConnectorOperation(
            name="webhook.send",
            kind=OperationKind.WRITE,
            risk=ActionRisk.CONSEQUENTIAL_WRITE,
        ),
    )

    def __init__(
        self,
        *,
        name: str = "webhook",
        url: str = "",
        transport: HttpConnector | None = None,
        secret: CredentialRef | None = None,
        credentials: CredentialResolver | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.name = name
        self._url = url
        self._secret = secret
        self._credentials = credentials or CredentialResolver()
        self._transport = transport or HttpConnector(
            name=f"{name}.transport",
            allowed_methods=("POST",),
            timeout_seconds=timeout_seconds,
        )
        super().__init__()

    def health(self) -> ConnectorHealth:
        if not self._url:
            return ConnectorHealth(
                state=ConnectorState.NOT_CONFIGURED, detail="no webhook URL configured"
            )
        transport_health = self._transport.health()
        if transport_health.state is not ConnectorState.READY:
            return transport_health
        return ConnectorHealth(
            state=ConnectorState.READY,
            configured=True,
            authenticated=self._secret is None,
            detail="configured",
        )

    async def healthcheck(self) -> ConnectorHealth:
        health = self.health()
        if health.state is not ConnectorState.READY or self._secret is None:
            return health
        if not await self._credentials.has(self._secret):
            return ConnectorHealth(
                state=ConnectorState.REAUTHENTICATION_REQUIRED,
                configured=True,
                authenticated=False,
                detail=f"signing secret {self._secret.name!r} is not available",
            )
        return health

    async def call(self, call: ConnectorCall) -> ConnectorResult:
        if call.operation != "webhook.send":
            raise ValidationError(f"unsupported webhook operation: {call.operation!r}")
        payload = WebhookPayload(**call.payload)
        return await self.send(payload)

    async def send(self, payload: WebhookPayload) -> ConnectorResult:
        if not self._url:
            raise ConfigurationError("webhook connector has no URL configured")
        body = json.dumps(
            {"event": payload.event, "data": payload.data, "delivery_id": payload.delivery_id},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if payload.delivery_id:
            headers[DELIVERY_HEADER] = payload.delivery_id
        if self._secret is not None:
            credential = await self._credentials.resolve(self._secret)
            if credential is None:
                raise ConfigurationError("webhook signing secret is not available")
            timestamp = str(int(time.time()))
            headers[TIMESTAMP_HEADER] = timestamp
            headers[SIGNATURE_HEADER] = sign_payload(
                credential.reveal(), timestamp=timestamp, body=body
            )
        response = await self._transport.request(
            HttpRequest(method="POST", url=self._url, headers=headers, body=body.decode("utf-8"))
        )
        if not response.ok:
            return ConnectorResult.failure(
                f"webhook delivery rejected: {response.summary()}",
                data={"status": response.status},
            )
        return ConnectorResult.success(
            f"webhook delivered ({response.status})",
            data={
                "status": response.status,
                "delivery_id": payload.delivery_id,
                "signed": self._secret is not None,
            },
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


__all__ = [
    "DELIVERY_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WebhookConnector",
    "WebhookPayload",
    "sign_payload",
]
