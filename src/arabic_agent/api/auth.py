"""Authentication and authorization.

Real API-key authentication: keys are supplied via the ``AAP_API_KEYS``
environment variable as a JSON map, never in source. Each key resolves to a
:class:`Principal` carrying tenant, user, roles and an optional permission
ceiling. Unknown keys are rejected; there is no "anonymous" fallback unless the
operator explicitly disables auth for a local run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from ..core.config import Settings
from ..core.enums import Locale
from ..core.errors import ConfigurationError
from ..permissions.base import Permission, Principal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiKeyRecord:
    """A registered API key and the principal it authenticates."""

    key: str
    tenant_id: str
    user_id: str
    roles: frozenset[str]
    permissions: frozenset[Permission] | None
    locale: Locale = Locale.AR


class ApiKeyAuth:
    """Resolves API keys to principals."""

    def __init__(self, records: dict[str, ApiKeyRecord]) -> None:
        self._records = records

    @property
    def enabled(self) -> bool:
        return bool(self._records)

    @classmethod
    def from_settings(cls, settings: Settings) -> ApiKeyAuth:
        raw = settings.api_keys.get_secret_value() if settings.api_keys else ""
        if not raw:
            if settings.require_auth and settings.is_production:
                raise ConfigurationError(
                    "AAP_API_KEYS must be set in production when auth is required"
                )
            return cls({})
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("AAP_API_KEYS must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ConfigurationError("AAP_API_KEYS must be a JSON object")
        records: dict[str, ApiKeyRecord] = {}
        for key, value in parsed.items():
            if not isinstance(value, dict):
                raise ConfigurationError("each API key entry must be an object")
            tenant_id = str(value.get("tenant_id") or "")
            user_id = str(value.get("user_id") or "")
            if not tenant_id or not user_id:
                raise ConfigurationError("each API key needs tenant_id and user_id")
            roles = frozenset(str(r) for r in value.get("roles", []))
            perms_raw = value.get("permissions")
            permissions = (
                frozenset(Permission(p) for p in perms_raw) if isinstance(perms_raw, list) else None
            )
            locale = Locale(str(value.get("locale") or Locale.AR.value))
            records[key] = ApiKeyRecord(
                key=key,
                tenant_id=tenant_id,
                user_id=user_id,
                roles=roles,
                permissions=permissions,
                locale=locale,
            )
        return cls(records)

    def resolve(self, key: str | None) -> Principal | None:
        if not key:
            return None
        record = self._records.get(key)
        if record is None:
            return None
        return Principal(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            roles=record.roles,
            granted_permissions=record.permissions,
        )

    def locale_for(self, key: str | None) -> Locale:
        record = self._records.get(key or "")
        return record.locale if record else Locale.AR


def build_principal_dependency(
    auth: ApiKeyAuth, settings: Settings
) -> Callable[..., Awaitable[Principal]]:
    """Create the FastAPI dependency that authenticates a request.

    Returns an async callable resolving to a :class:`Principal`, raising 401
    when auth is enabled and no valid key is presented.
    """

    async def dependency(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Principal:
        principal = auth.resolve(x_api_key)
        if principal is not None:
            return principal
        if auth.enabled:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing API key",
            )
        if settings.require_auth:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication is required but no API keys are configured",
            )
        # Explicit, non-production anonymous mode for local development.
        return Principal(tenant_id="local", user_id="local", roles=frozenset({"admin"}))

    return dependency
