"""Credential references and resolution.

Raw secrets must **not** normally enter model context: they are not arguments,
not tool output, not audit payloads, not memory. The platform therefore passes
around a :class:`CredentialRef` — a *reference* to a secret, never the secret —
and resolves it to a real value only at the execution boundary (the moment a
connector is about to make a network call).

The reference is extensible across providers so the same connector code works
against environment configuration today and an OS keychain or an enterprise
secret manager tomorrow:

* :class:`EnvCredentialProvider` — resolve a reference from an environment
  variable (the default, 12-factor friendly).
* :class:`StaticCredentialProvider` — an in-memory mapping, for tests and for a
  caller that already holds the secret (e.g. an OAuth token it just refreshed).

Resolution returns a :class:`ResolvedCredential`, which wraps the value in a
:class:`~pydantic.SecretStr` so an accidental ``repr``/log masks it; the real
value is reachable only by an explicit :meth:`ResolvedCredential.reveal`.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, SecretStr

from ..core.errors import ConfigurationError


class CredentialRef(BaseModel):
    """A reference to a secret, never the secret itself."""

    model_config = ConfigDict(frozen=True)

    #: Which provider resolves this reference (``env``, ``static``, ...).
    provider: str = "env"
    #: The locator within the provider (an env var name, a keychain entry, ...).
    name: str
    #: OAuth scopes associated with the credential, if any (non-secret).
    scopes: tuple[str, ...] = ()
    #: Whether the absence of this credential is a hard error.
    required: bool = True
    description: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"CredentialRef(provider={self.provider!r}, name={self.name!r})"


@dataclass
class ResolvedCredential:
    """A resolved secret value, masked by default.

    The value is a :class:`SecretStr`; ``repr`` and ``str`` mask it, so a
    credential that accidentally reaches a log line is redacted. Only
    :meth:`reveal` exposes the value, and callers should use it solely to build
    the outbound request at the execution boundary.
    """

    ref: CredentialRef
    secret: SecretStr = field(repr=False)
    #: OAuth access-token expiry, when relevant (non-secret).
    expires_at: float | None = None

    @property
    def name(self) -> str:
        return self.ref.name

    def reveal(self) -> str:
        """Return the raw secret. Call only at the execution boundary."""
        return self.secret.get_secret_value()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ResolvedCredential(name={self.ref.name!r}, value=***REDACTED***)"


class CredentialProvider(ABC):
    """Contract for resolving credential references."""

    #: The provider id references use to select this provider.
    name: str

    @abstractmethod
    async def resolve(self, ref: CredentialRef) -> ResolvedCredential | None:
        """Return the credential, or ``None`` when it is absent."""

    async def aclose(self) -> None:
        return None


class EnvCredentialProvider(CredentialProvider):
    """Resolve credentials from environment variables.

    Reads the environment at resolution time (never at import), so a process can
    pick up a rotated secret without a code change and a test can set one
    without touching global state at import.
    """

    name = "env"

    def __init__(self, *, prefix: str = "AAP_CONNECTOR_") -> None:
        self._prefix = prefix

    async def resolve(self, ref: CredentialRef) -> ResolvedCredential | None:
        key = ref.name if ref.name.startswith(self._prefix) else f"{self._prefix}{ref.name}"
        value = os.environ.get(key)
        if not value:
            return None
        return ResolvedCredential(ref=ref, secret=SecretStr(value))


class StaticCredentialProvider(CredentialProvider):
    """Resolve credentials from an in-memory mapping (tests, OAuth refresh)."""

    name = "static"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def clear(self, name: str) -> None:
        self._values.pop(name, None)

    async def resolve(self, ref: CredentialRef) -> ResolvedCredential | None:
        value = self._values.get(ref.name)
        if value is None:
            return None
        return ResolvedCredential(ref=ref, secret=SecretStr(value))


class CredentialResolver:
    """Resolves a :class:`CredentialRef` through the registered providers.

    Providers are tried in order; the first that holds the credential wins. A
    missing *required* credential raises :class:`ConfigurationError` with a
    non-secret message — the connector then reports ``NOT_CONFIGURED`` rather
    than pretending to work.
    """

    def __init__(self, providers: list[CredentialProvider] | None = None) -> None:
        self._providers = list(providers or [EnvCredentialProvider()])

    def register(self, provider: CredentialProvider) -> None:
        self._providers.append(provider)

    def providers(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._providers)

    async def resolve(self, ref: CredentialRef) -> ResolvedCredential | None:
        """Resolve ``ref``, returning ``None`` when it is absent and optional."""
        for provider in self._providers:
            if provider.name != ref.provider:
                continue
            credential = await provider.resolve(ref)
            if credential is not None:
                return credential
        if ref.required:
            raise ConfigurationError(
                f"credential {ref.name!r} is not configured",
                details={"provider": ref.provider, "credential": ref.name},
            )
        return None

    async def has(self, ref: CredentialRef) -> bool:
        """Whether the credential is present, without revealing it."""
        try:
            return await self.resolve(ref) is not None
        except ConfigurationError:
            return False

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()


__all__ = [
    "CredentialProvider",
    "CredentialRef",
    "CredentialResolver",
    "EnvCredentialProvider",
    "ResolvedCredential",
    "StaticCredentialProvider",
]
