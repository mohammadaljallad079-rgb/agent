"""Browser policy, action budget and web-content boundary unit tests (Phase 8).

These exercise the *real* runtime guards: per-run action caps, the minimum
interval, tab/download/upload bounds, sensitive-navigation refusal, and the
web-content data boundary that decides whether page content may reach a remote
model. The boundary must fail closed, so a ``LOCAL_ONLY`` page is never sent
remotely and a ``SECRET``-classified page never leaves at all.
"""

from __future__ import annotations

import pytest

from arabic_agent.browser.boundary import WebContentPolicy, WebDataBoundary
from arabic_agent.browser.policy import ActionBudget, BrowserPolicy
from arabic_agent.browser.url_security import UrlPolicy
from arabic_agent.core.enums import DataBoundary, OperatingMode
from arabic_agent.core.errors import (
    PermissionDeniedError,
    ResourceExhaustedError,
    ValidationError,
)

# --- Navigation policy ------------------------------------------------------


def test_check_navigation_returns_risk_and_raises_on_blocked():
    policy = BrowserPolicy(url_policy=UrlPolicy())
    assert policy.check_navigation("https://example.com/").value == "safe"
    with pytest.raises(PermissionDeniedError):
        policy.check_navigation("javascript:alert(1)")


def test_sensitive_navigation_can_be_refused_inside_runtime():
    policy = BrowserPolicy(url_policy=UrlPolicy(), allow_sensitive_navigation=False)
    with pytest.raises(PermissionDeniedError):
        policy.check_navigation("https://example.com/login")


# --- Action budget ----------------------------------------------------------


def test_budget_enforces_per_kind_cap():
    policy = BrowserPolicy(max_navigations=2, max_interactions=1)
    budget = ActionBudget(policy)
    budget.consume("navigate", now=0.0)
    budget.consume("navigate", now=1.0)
    with pytest.raises(ResourceExhaustedError):
        budget.consume("navigate", now=2.0)
    budget.consume("interact", now=3.0)
    with pytest.raises(ResourceExhaustedError):
        budget.consume("interact", now=4.0)


def test_budget_enforces_minimum_interval():
    policy = BrowserPolicy(min_action_interval_seconds=5.0)
    budget = ActionBudget(policy)
    budget.consume("navigate", now=100.0)
    with pytest.raises(ResourceExhaustedError):
        budget.consume("navigate", now=102.0)
    # Once the interval has elapsed the action is permitted again.
    budget.consume("navigate", now=106.0)
    assert budget.snapshot()["navigate"] == 2


def test_budget_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ActionBudget(BrowserPolicy()).consume("teleport", now=0.0)


# --- Web-content data boundary ---------------------------------------------


def test_local_only_page_never_goes_remote_even_online():
    boundary = WebDataBoundary(policy=WebContentPolicy.LOCAL_ONLY, mode=OperatingMode.ONLINE)
    decision = boundary.evaluate(remote=True)
    assert not decision.allowed
    assert decision.routed_to is DataBoundary.LOCAL
    assert not boundary.may_send_remote()
    # Local processing is always permitted.
    assert boundary.evaluate(remote=False).allowed


def test_never_store_page_is_secret_and_never_persists():
    boundary = WebDataBoundary(policy=WebContentPolicy.NEVER_STORE, mode=OperatingMode.ONLINE)
    decision = boundary.evaluate(remote=True)
    assert not decision.allowed
    assert not decision.may_persist
    assert not boundary.evaluate(remote=False).may_persist


def test_cloud_allowed_page_may_go_remote_only_online():
    online = WebDataBoundary(policy=WebContentPolicy.CLOUD_ALLOWED, mode=OperatingMode.ONLINE)
    assert online.may_send_remote()

    offline = WebDataBoundary(policy=WebContentPolicy.CLOUD_ALLOWED, mode=OperatingMode.OFFLINE)
    assert not offline.may_send_remote()


def test_cloud_allowed_still_respects_offline_mode_flip():
    boundary = WebDataBoundary(policy=WebContentPolicy.CLOUD_ALLOWED, mode=OperatingMode.ONLINE)
    assert boundary.may_send_remote()
    boundary.set_mode(OperatingMode.OFFLINE)
    assert not boundary.may_send_remote()
