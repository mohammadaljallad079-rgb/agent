"""Declarative connector configuration.

Connectors are configured with data, not code. ``settings.connectors_json`` holds
a JSON array of :class:`ConnectorSpec` objects; this module parses it and builds
the corresponding connectors. Secrets are *never* inlined — a spec names the
environment variable that holds a secret (``*_env`` fields), which becomes a
:class:`CredentialRef`, and the value is resolved only at the execution boundary.

Every connector is built with the same safety defaults as the rest of the
platform: the URL policy refuses private/loopback/link-local and metadata hosts
unless the operator explicitly opts in, and the response size is capped. A
connector that is configured but not usable (missing host, missing credential) is
still registered so the agent is told it exists and is unusable, rather than
being silently omitted.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from ..browser.url_security import UrlPolicy
from ..core.enums import ConnectorKind
from ..core.errors import ConfigurationError
from ..local.sandbox import FilesystemPolicy
from .base import Connector, ConnectorRegistry
from .business import BusinessConnector, ResourceMapping
from .calendar import CalendarConnector, GoogleCalendarProvider
from .credentials import CredentialRef, CredentialResolver
from .email import EmailConnector
from .http import DEFAULT_METHODS, HttpConnector
from .messaging import MessagingConnector, WhatsAppBusinessProvider
from .retry import RetryPolicy
from .webhooks import WebhookConnector


class ConnectorSpec(BaseModel):
    """One connector's declarative configuration (no inline secrets)."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: ConnectorKind
    #: Whether this connector is enabled. Disabled specs are still parsed so a
    #: typo is reported rather than silently ignored.
    enabled: bool = True

    # -- HTTP -----------------------------------------------------------
    base_url: str = ""
    allowed_methods: tuple[str, ...] = DEFAULT_METHODS
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    credential_env: str = ""
    credential_header: str = "Authorization"
    credential_scheme: str = "Bearer"

    # -- Email ----------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password_env: str = ""
    smtp_use_tls: bool = True
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password_env: str = ""
    imap_mailbox: str = "INBOX"
    from_address: str = ""

    # -- Messaging ------------------------------------------------------
    provider: str = ""
    phone_number_id: str = ""
    access_token_env: str = ""

    # -- Webhook --------------------------------------------------------
    url: str = ""
    secret_env: str = ""

    # -- Business -------------------------------------------------------
    resources: tuple[ResourceMapping, ...] = ()

    # -- Calendar -------------------------------------------------------
    #: Default calendar id the agent operates on when none is named.
    calendar_id: str = "primary"
    #: A refresh token plus client credentials, for unattended renewals. The
    #: access token itself is taken from the shared ``access_token_env`` above.
    refresh_token_env: str = ""
    client_id_env: str = ""
    client_secret_env: str = ""


def parse_connector_specs(raw: str) -> list[ConnectorSpec]:
    """Parse ``connectors_json`` into specs, failing loudly on malformed input."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ConfigurationError(f"connectors_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ConfigurationError("connectors_json must be a JSON array of connector objects")
    specs: list[ConnectorSpec] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ConfigurationError(f"connector #{index} must be a JSON object")
        try:
            specs.append(ConnectorSpec(**item))
        except PydanticValidationError as exc:
            raise ConfigurationError(f"connector #{index} is invalid: {exc}") from exc
    return specs


def _credential_ref(
    env_name: str, *, description: str, required: bool = True
) -> CredentialRef | None:
    if not env_name:
        return None
    return CredentialRef(provider="env", name=env_name, required=required, description=description)


def build_connectors(
    specs: list[ConnectorSpec],
    *,
    credentials: CredentialResolver,
    filesystem_policy: FilesystemPolicy | None = None,
    workspace_dir: str = ".",
    allow_local_addresses: bool = False,
    max_response_bytes: int = 1_048_576,
    retry_max_attempts: int = 3,
) -> ConnectorRegistry:
    """Build a :class:`ConnectorRegistry` from parsed specs."""
    registry = ConnectorRegistry()
    retry = RetryPolicy(max_attempts=retry_max_attempts)
    for spec in specs:
        if not spec.enabled:
            continue
        connector = _build_one(
            spec,
            credentials=credentials,
            filesystem_policy=filesystem_policy,
            workspace_dir=workspace_dir,
            allow_local_addresses=allow_local_addresses,
            max_response_bytes=max_response_bytes,
            retry=retry,
        )
        registry.register(connector, replace=True)
    return registry


def _url_policy(spec: ConnectorSpec, *, allow_local_addresses: bool) -> UrlPolicy:
    return UrlPolicy(
        allowed_domains=spec.allowed_domains,
        blocked_domains=spec.blocked_domains,
        allow_local_addresses=allow_local_addresses,
    )


def _build_one(
    spec: ConnectorSpec,
    *,
    credentials: CredentialResolver,
    filesystem_policy: FilesystemPolicy | None,
    workspace_dir: str,
    allow_local_addresses: bool,
    max_response_bytes: int,
    retry: RetryPolicy,
) -> Connector:
    url_policy = _url_policy(spec, allow_local_addresses=allow_local_addresses)
    if spec.kind is ConnectorKind.HTTP:
        return HttpConnector(
            name=spec.name,
            base_url=spec.base_url,
            url_policy=url_policy,
            allowed_methods=spec.allowed_methods,
            credential=_credential_ref(spec.credential_env, description="http bearer token"),
            credentials=credentials,
            credential_header=spec.credential_header,
            credential_scheme=spec.credential_scheme,
            max_response_bytes=max_response_bytes,
            retry=retry,
        )
    if spec.kind is ConnectorKind.EMAIL:
        return EmailConnector(
            name=spec.name,
            smtp_host=spec.smtp_host,
            smtp_port=spec.smtp_port,
            smtp_username=spec.smtp_username,
            smtp_password=_credential_ref(spec.smtp_password_env, description="smtp password"),
            smtp_use_tls=spec.smtp_use_tls,
            imap_host=spec.imap_host,
            imap_port=spec.imap_port,
            imap_username=spec.imap_username,
            imap_password=_credential_ref(spec.imap_password_env, description="imap password"),
            imap_mailbox=spec.imap_mailbox,
            credentials=credentials,
            from_address=spec.from_address,
            filesystem_policy=filesystem_policy,
            workspace_dir=workspace_dir,
        )
    if spec.kind is ConnectorKind.MESSAGING:
        provider_name = spec.provider or "whatsapp_business"
        if provider_name != "whatsapp_business":
            raise ConfigurationError(
                f"unknown messaging provider: {provider_name!r}",
                details={"connector": spec.name},
            )
        provider = WhatsAppBusinessProvider(
            phone_number_id=spec.phone_number_id,
            access_token=_credential_ref(
                spec.access_token_env, description="whatsapp access token"
            ),
            credentials=credentials,
            retry=retry,
        )
        return MessagingConnector(name=spec.name, provider=provider)
    if spec.kind is ConnectorKind.WEBHOOK:
        transport = HttpConnector(
            name=f"{spec.name}.transport",
            url_policy=url_policy,
            allowed_methods=("POST",),
            max_response_bytes=max_response_bytes,
            retry=retry,
        )
        return WebhookConnector(
            name=spec.name,
            url=spec.url,
            transport=transport,
            secret=_credential_ref(spec.secret_env, description="webhook signing secret"),
            credentials=credentials,
        )
    if spec.kind is ConnectorKind.BUSINESS:
        transport = HttpConnector(
            name=f"{spec.name}.transport",
            base_url=spec.base_url,
            url_policy=url_policy,
            allowed_methods=spec.allowed_methods,
            credential=_credential_ref(spec.credential_env, description="business api token"),
            credentials=credentials,
            credential_header=spec.credential_header,
            credential_scheme=spec.credential_scheme,
            max_response_bytes=max_response_bytes,
            retry=retry,
        )
        return BusinessConnector(
            name=spec.name,
            transport=transport,
            resources=spec.resources,
        )
    if spec.kind is ConnectorKind.CALENDAR:
        # The provider is selected by name so a second calendar backend adds no
        # connector logic. Only the official Google adapter ships here.
        provider_name = spec.provider or "google_calendar"
        if provider_name != "google_calendar":
            raise ConfigurationError(
                f"unknown calendar provider: {provider_name!r}",
                details={"connector": spec.name},
            )
        calendar_provider = GoogleCalendarProvider(
            access_token=_credential_ref(
                spec.access_token_env, description="google oauth access token", required=False
            ),
            refresh_token=_credential_ref(
                spec.refresh_token_env, description="google oauth refresh token", required=False
            ),
            client_id=_credential_ref(
                spec.client_id_env, description="google oauth client id", required=False
            ),
            client_secret=_credential_ref(
                spec.client_secret_env, description="google oauth client secret", required=False
            ),
            credentials=credentials,
            retry=retry,
        )
        return CalendarConnector(
            name=spec.name,
            provider=calendar_provider,
            default_calendar=spec.calendar_id or "primary",
        )
    raise ConfigurationError(
        f"unsupported connector kind: {spec.kind.value!r}", details={"connector": spec.name}
    )


__all__ = ["ConnectorSpec", "build_connectors", "parse_connector_specs"]
