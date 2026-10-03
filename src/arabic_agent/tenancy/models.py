"""Tenant, user, role and agent-profile models."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import Locale
from ..core.ids import PREFIX_AGENT, PREFIX_TENANT, PREFIX_USER, new_id
from ..permissions.base import Permission


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Role(BaseModel):
    """A named bundle of permissions within a tenant."""

    model_config = ConfigDict(frozen=True)

    name: str
    permissions: frozenset[Permission] = frozenset()

    def grants(self, permission: Permission) -> bool:
        return permission in self.permissions


class User(BaseModel):
    """A person within a tenant."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_USER))
    tenant_id: str
    email: str
    display_name: str = ""
    roles: frozenset[str] = frozenset()
    locale: Locale = Locale.AR
    active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)


class AgentProfile(BaseModel):
    """A configured agent: which model, which tools, which locale."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_AGENT))
    tenant_id: str
    name: str
    model_id: str
    allowed_tools: tuple[str, ...] = ()
    locale: Locale = Locale.AR
    created_at: datetime = Field(default_factory=_utcnow)


class Tenant(BaseModel):
    """An isolated customer organization."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_TENANT))
    name: str
    slug: str
    locale: Locale = Locale.AR
    active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
