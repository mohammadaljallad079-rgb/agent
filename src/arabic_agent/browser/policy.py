"""Browser policy and action budgeting.

The permission layer decides *whether* the agent may use a browser tool at all;
this policy decides *how far* a single action may go and how often it may
repeat. It is a small, pure, declarative guard that sits inside the browser
runtime, before any automation call:

* navigation policy (delegated to :class:`~.url_security.UrlPolicy`);
* bounded observations (how many elements, how much text);
* per-run action budgets and a minimum interval between actions, so a model
  cannot turn browsing into a runaway clicker or crawler;
* tab limits, so the agent cannot explode the tab count;
* download limits (count and size) and an upload allow-list;
* a freshness requirement, so a dangerous action cannot be taken on a stale
  observation;
* a loop guard on repeated identical navigations.

Every refusal is a classified :class:`PlatformError` the agent loop can recover
from, and is auditable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ResourceExhaustedError, ValidationError
from .url_security import UrlPolicy, UrlRisk


class BrowserPolicy(BaseModel):
    """Declarative bounds for browser use."""

    model_config = ConfigDict(frozen=True)

    #: Navigation safety (schemes, domains, local addresses, file URLs).
    url_policy: UrlPolicy = Field(default_factory=UrlPolicy)

    #: How many interactive elements one observation may carry.
    max_elements: int = Field(default=60, ge=1, le=400)
    #: How many characters of page text one observation may carry.
    max_text_chars: int = Field(default=6000, ge=200, le=100_000)
    #: How many forms/fields one observation may carry.
    max_forms: int = Field(default=10, ge=1)
    #: How many links one observation may carry.
    max_links: int = Field(default=40, ge=1)

    #: Per-run caps on browser actions (anti-runaway).
    max_navigations: int = Field(default=40, ge=1)
    max_interactions: int = Field(default=80, ge=1)
    max_observations: int = Field(default=60, ge=1)
    max_downloads: int = Field(default=10, ge=0)
    max_uploads: int = Field(default=10, ge=0)
    #: Maximum size of a single downloaded file, in bytes.
    max_download_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    #: Maximum number of open tabs.
    max_tabs: int = Field(default=8, ge=1)
    #: Minimum seconds between two actions of the same kind (anti-runaway).
    min_action_interval_seconds: float = Field(default=0.0, ge=0.0)

    #: Whether an action must reference a fresh observation.
    require_fresh_observation: bool = True
    max_observation_age_seconds: float = Field(default=60.0, gt=0)

    #: Default navigation/action timeout, in milliseconds.
    default_timeout_ms: int = Field(default=15_000, ge=100, le=120_000)
    #: Whether a SENSITIVE navigation (login page, local address) is allowed
    #: without a separate approval. Off by default: sensitive navigation is
    #: still permission-gated by the tool layer, but this flag lets a deployment
    #: additionally refuse it inside the runtime.
    allow_sensitive_navigation: bool = True

    #: Whether submitting a form is treated as a consequential action that must
    #: go through approval. This is a *runtime* backstop; the real gate is the
    #: tool's own permission, but this refuses a submit that slipped past it.
    require_approval_for_submit: bool = True

    def check_navigation(self, url: str) -> UrlRisk:
        """Validate a URL and return its risk, raising on a blocked URL."""
        decision = self.url_policy.require(url)
        if decision.risk is UrlRisk.SENSITIVE and not self.allow_sensitive_navigation:
            from ..core.errors import PermissionDeniedError

            raise PermissionDeniedError(
                "sensitive navigation is not permitted by policy",
                details={"url": url, "host": decision.host},
            )
        return decision.risk


class ActionBudget:
    """Counts actions within a run and enforces the per-kind caps.

    A single instance is created per run so the caps are real, not advisory.
    """

    _KINDS = ("navigate", "interact", "observe", "download", "upload")

    def __init__(self, policy: BrowserPolicy) -> None:
        self._policy = policy
        self._counts: dict[str, int] = dict.fromkeys(self._KINDS, 0)
        self._last_at: dict[str, float] = {}

    def _cap(self, kind: str) -> int:
        return {
            "navigate": self._policy.max_navigations,
            "interact": self._policy.max_interactions,
            "observe": self._policy.max_observations,
            "download": self._policy.max_downloads,
            "upload": self._policy.max_uploads,
        }[kind]

    def consume(self, kind: str, *, now: float) -> None:
        """Record one action of ``kind`` or refuse if the budget is exhausted."""
        if kind not in self._counts:
            raise ValidationError(f"unknown browser action kind: {kind}")
        interval = self._policy.min_action_interval_seconds
        if interval > 0:
            last = self._last_at.get(kind)
            if last is not None and now - last < interval:
                raise ResourceExhaustedError(
                    "browser action rate limit reached",
                    details={"kind": kind, "min_interval": interval},
                )
        if self._counts[kind] >= self._cap(kind):
            raise ResourceExhaustedError(
                f"per-run {kind} action budget exhausted",
                details={"kind": kind, "cap": self._cap(kind)},
            )
        self._counts[kind] += 1
        self._last_at[kind] = now

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


__all__ = ["ActionBudget", "BrowserPolicy"]
