"""External connectors and real-world actions.

A *connector* is a real transport to an external system (HTTP business API, SMTP
email, a messaging provider, a webhook sink). Connectors are reached only through
the existing tool → permission → approval → audit path; the connector runtime
adds the Phase 9 controls (data boundary, approval binding, idempotency ledger,
ambiguous-timeout handling, verification) in one place.

Provider neutrality is a hard rule: the agent core never learns a provider's
name, and no provider-specific schema lives in the core. ``Gmail``,
``Microsoft 365``, official ``WhatsApp Business`` and a real ``ERP`` are
*architecture-supported* but **not verified** in this environment — the shipped
adapters are real code, and the acceptance suite exercises the same code path
against real local protocol services.
"""

from .base import (
    Connector,
    ConnectorCall,
    ConnectorCapability,
    ConnectorHealth,
    ConnectorInfo,
    ConnectorOperation,
    ConnectorRegistry,
    ConnectorResult,
    OperationKind,
)
from .business import BusinessConnector, ResourceMapping
from .calendar import (
    CalendarConnector,
    CalendarEvent,
    CalendarListEntry,
    CalendarProvider,
    FreeBusySlot,
    GoogleCalendarProvider,
)
from .credentials import (
    CredentialProvider,
    CredentialRef,
    CredentialResolver,
    EnvCredentialProvider,
    ResolvedCredential,
    StaticCredentialProvider,
)
from .email import (
    EmailAddress,
    EmailAttachment,
    EmailConnector,
    EmailMessage,
    EmailPreview,
    OutgoingEmail,
)
from .http import HttpConnector, HttpRequest, HttpResponse
from .ledger import (
    ActionLedger,
    ActionRecord,
    ActionTransitionError,
    InMemoryActionLedger,
    LedgerDecision,
    LedgerOutcome,
    canonical_digest,
    check_and_claim,
)
from .messaging import (
    DeliveryResult,
    MessagingConnector,
    MessagingMessage,
    MessagingProvider,
    WhatsAppBusinessProvider,
)
from .oauth import (
    InMemoryStateStore,
    OAuthAuthorization,
    OAuthClient,
    OAuthProvider,
    OAuthTokenSet,
    StateStore,
)
from .policy import ConnectorPolicy
from .retry import RetryPolicy, with_retry
from .runtime import ConnectorInvocation, ConnectorRuntime
from .webhooks import WebhookConnector, WebhookPayload, sign_payload

__all__ = [
    "ActionLedger",
    "ActionRecord",
    "ActionTransitionError",
    "BusinessConnector",
    "CalendarConnector",
    "CalendarEvent",
    "CalendarListEntry",
    "CalendarProvider",
    "Connector",
    "ConnectorCall",
    "ConnectorCapability",
    "ConnectorHealth",
    "ConnectorInfo",
    "ConnectorInvocation",
    "ConnectorOperation",
    "ConnectorPolicy",
    "ConnectorRegistry",
    "ConnectorResult",
    "ConnectorRuntime",
    "CredentialProvider",
    "CredentialRef",
    "CredentialResolver",
    "DeliveryResult",
    "EmailAddress",
    "EmailAttachment",
    "EmailConnector",
    "EmailMessage",
    "EmailPreview",
    "EnvCredentialProvider",
    "FreeBusySlot",
    "GoogleCalendarProvider",
    "HttpConnector",
    "HttpRequest",
    "HttpResponse",
    "InMemoryActionLedger",
    "InMemoryStateStore",
    "LedgerDecision",
    "LedgerOutcome",
    "MessagingConnector",
    "MessagingMessage",
    "MessagingProvider",
    "OAuthAuthorization",
    "OAuthClient",
    "OAuthProvider",
    "OAuthTokenSet",
    "OperationKind",
    "OutgoingEmail",
    "ResolvedCredential",
    "ResourceMapping",
    "RetryPolicy",
    "StateStore",
    "StaticCredentialProvider",
    "WebhookConnector",
    "WebhookPayload",
    "WhatsAppBusinessProvider",
    "canonical_digest",
    "check_and_claim",
    "sign_payload",
    "with_retry",
]
