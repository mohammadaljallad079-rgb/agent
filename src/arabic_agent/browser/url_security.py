"""URL and domain safety.

Navigation is where a browser agent is most exposed: a hostile page, a model
mistake or an injected instruction can all produce a URL. This module answers,
deterministically and without any network probe, whether a URL may be opened,
and classifies it so the runtime can apply a configurable domain policy.

Threats explicitly handled here:

* **Unsupported/dangerous schemes** -- ``javascript:``, ``data:``, ``blob:``,
  ``vbscript:`` and friends are refused outright; only ``http``/``https`` (and
  ``file`` when explicitly permitted, for local test sites) are allowed.
* **``file://`` escapes** -- a ``file`` URL is refused unless the policy allows
  it, and even then it must resolve inside an allowed root.
* **SSRF / local-network access** -- ``localhost``, loopback, private ranges and
  link-local addresses (including the cloud metadata address ``169.254.169.254``)
  are refused unless the policy explicitly permits local addresses.
* **Credential smuggling** -- a URL carrying a username/password is refused.
* **Domain policy** -- an allow-list (when non-empty) and a deny-list, matched
  against the registrable host and its subdomains.

The result is a :class:`UrlDecision` carrying a :class:`UrlRisk`, so the runtime
can both *refuse* dangerous URLs and *require approval* for merely sensitive
ones.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from ..core.errors import PermissionDeniedError

#: Schemes the browser may ever use. Anything else is refused.
_ALLOWED_SCHEMES = frozenset({"http", "https"})
#: The only additional scheme, and only when the policy opts in (local sites).
_FILE_SCHEME = "file"


class UrlRisk(StrEnum):
    """How risky a navigation target is."""

    SAFE = "safe"  # a public http(s) URL
    SENSITIVE = "sensitive"  # e.g. a login page or a local address (approval-worthy)
    BLOCKED = "blocked"  # refused outright


class UrlDecision(BaseModel):
    """The outcome of evaluating one URL against the navigation policy."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    risk: UrlRisk
    reason: str
    scheme: str = ""
    host: str = ""
    #: The registrable-ish host used for policy matching (lowercased).
    domain: str = ""

    @classmethod
    def allow(
        cls, *, risk: UrlRisk, reason: str, scheme: str, host: str, domain: str
    ) -> UrlDecision:
        return cls(
            allowed=True,
            risk=risk,
            reason=reason,
            scheme=scheme,
            host=host,
            domain=domain,
        )

    @classmethod
    def block(cls, *, reason: str, scheme: str = "", host: str = "") -> UrlDecision:
        return cls(
            allowed=False,
            risk=UrlRisk.BLOCKED,
            reason=reason,
            scheme=scheme,
            host=host,
        )


def _is_private_host(host: str) -> bool:
    """Whether a host resolves syntactically to a local/private address.

    This is a *syntactic* check on IP literals and well-known local names; it is
    deliberately not a DNS lookup (which would be slow, flaky and itself an SSRF
    vector). Hostnames that resolve to private space are the operator's concern
    via the domain policy; IP literals and ``localhost`` are refused here.
    """
    lowered = host.lower().strip("[]")
    if lowered in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if (
        lowered.endswith(".localhost")
        or lowered.endswith(".local")
        or lowered.endswith(".internal")
    ):
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _domain_of(host: str) -> str:
    """A lowercased host used for policy matching (no port, no userinfo)."""
    return host.lower().strip(".")


class UrlPolicy(BaseModel):
    """Declarative navigation safety policy."""

    model_config = ConfigDict(frozen=True)

    #: When non-empty, only these domains (and their subdomains) may be opened.
    allowed_domains: tuple[str, ...] = ()
    #: Domains that are always refused, even if otherwise allowed.
    blocked_domains: tuple[str, ...] = ()
    #: Whether private/loopback/link-local hosts may be opened at all. Off by
    #: default; a controlled local test site turns it on deliberately.
    allow_local_addresses: bool = False
    #: Whether ``file://`` URLs may be opened (local test sites). Off by default.
    allow_file_urls: bool = False
    #: Roots a ``file://`` URL must resolve inside when file URLs are allowed.
    allowed_file_roots: tuple[str, ...] = ()
    #: Path/query fragments that mark a page as sensitive (login, checkout).
    sensitive_markers: tuple[str, ...] = (
        "login",
        "signin",
        "sign-in",
        "checkout",
        "payment",
        "account",
        "password",
    )

    def _domain_matches(self, host: str, pattern: str) -> bool:
        host = host.lower()
        pattern = pattern.lower().lstrip(".")
        return host == pattern or host.endswith("." + pattern)

    def evaluate(self, url: str) -> UrlDecision:
        """Classify ``url``; never raises, always returns a decision."""
        raw = (url or "").strip()
        if not raw:
            return UrlDecision.block(reason="empty URL")

        # ``urlsplit`` never raises on a malformed URL; a missing scheme means a
        # relative/invalid target, which we refuse rather than guess.
        parts = urlsplit(raw)
        scheme = (parts.scheme or "").lower()

        if scheme in {"javascript", "data", "blob", "vbscript", "about"}:
            return UrlDecision.block(reason=f"scheme {scheme!r} is not permitted", scheme=scheme)
        if parts.username or parts.password:
            return UrlDecision.block(
                reason="URLs carrying credentials are not permitted", scheme=scheme
            )

        if scheme == _FILE_SCHEME:
            if not self.allow_file_urls:
                return UrlDecision.block(reason="file URLs are not permitted", scheme=scheme)
            from pathlib import Path

            target = Path(parts.path).expanduser()
            resolved = target.resolve()
            roots = [Path(root).expanduser().resolve() for root in self.allowed_file_roots]
            if not any(resolved == root or root in resolved.parents for root in roots):
                return UrlDecision.block(
                    reason="file URL is outside the permitted roots",
                    scheme=scheme,
                    host=str(resolved),
                )
            return UrlDecision.allow(
                risk=UrlRisk.SENSITIVE,
                reason="file URL within a permitted root",
                scheme=scheme,
                host=str(resolved),
                domain=str(resolved),
            )

        if scheme not in _ALLOWED_SCHEMES:
            return UrlDecision.block(
                reason=f"scheme {scheme!r} is not permitted" if scheme else "URL has no scheme",
                scheme=scheme,
            )

        host = parts.hostname or ""
        if not host:
            return UrlDecision.block(reason="URL has no host", scheme=scheme)

        domain = _domain_of(host)

        for pattern in self.blocked_domains:
            if self._domain_matches(host, pattern):
                return UrlDecision.block(
                    reason=f"domain {pattern!r} is blocked", scheme=scheme, host=host
                )

        if _is_private_host(host) and not self.allow_local_addresses:
            return UrlDecision.block(
                reason="local/private addresses are not permitted",
                scheme=scheme,
                host=host,
            )

        if self.allowed_domains and not any(
            self._domain_matches(host, pattern) for pattern in self.allowed_domains
        ):
            return UrlDecision.block(
                reason="domain is not on the allow-list", scheme=scheme, host=host
            )

        path_query = f"{parts.path}?{parts.query}".lower()
        sensitive = _is_private_host(host) or any(
            marker in path_query for marker in self.sensitive_markers
        )
        return UrlDecision.allow(
            risk=UrlRisk.SENSITIVE if sensitive else UrlRisk.SAFE,
            reason=(
                "sensitive target (local address or credential page)"
                if sensitive
                else "public URL permitted by policy"
            ),
            scheme=scheme,
            host=host,
            domain=domain,
        )

    def require(self, url: str) -> UrlDecision:
        """Like :meth:`evaluate` but raises on a blocked URL."""
        decision = self.evaluate(url)
        if not decision.allowed:
            raise PermissionDeniedError(
                f"navigation refused: {decision.reason}",
                details={"url": url, "host": decision.host},
            )
        return decision


def validate_url(url: str, policy: UrlPolicy) -> UrlDecision:
    """Convenience wrapper: raise on a blocked URL, else return the decision."""
    return policy.require(url)


__all__ = ["UrlDecision", "UrlPolicy", "UrlRisk", "validate_url"]
