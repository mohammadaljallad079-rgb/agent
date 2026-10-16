"""Browser URL/domain safety unit tests (Phase 8).

The URL policy is the first line of defence for an agent that opens URLs a model
or a hostile page can influence. These tests exercise the *real* policy: scheme
refusal, credential smuggling, SSRF/local-address refusal, ``file://`` escapes,
allow/deny lists, sensitive-target classification and the raising wrapper.
"""

from __future__ import annotations

import pytest

from arabic_agent.browser.url_security import UrlPolicy, UrlRisk, validate_url
from arabic_agent.core.errors import PermissionDeniedError


def test_public_https_is_safe():
    decision = UrlPolicy().evaluate("https://example.com/page")
    assert decision.allowed
    assert decision.risk is UrlRisk.SAFE
    assert decision.host == "example.com"
    assert decision.domain == "example.com"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "blob:https://example.com/abc",
        "vbscript:msgbox(1)",
        "about:blank",
    ],
)
def test_dangerous_schemes_are_blocked(url):
    decision = UrlPolicy().evaluate(url)
    assert not decision.allowed
    assert decision.risk is UrlRisk.BLOCKED
    assert "not permitted" in decision.reason


def test_unknown_scheme_and_missing_scheme_are_blocked():
    assert not UrlPolicy().evaluate("gopher://example.com").allowed
    assert not UrlPolicy().evaluate("example.com/no-scheme").allowed
    assert not UrlPolicy().evaluate("").allowed


def test_credentials_in_url_are_blocked():
    decision = UrlPolicy().evaluate("https://user:pass@example.com/")
    assert not decision.allowed
    assert "credentials" in decision.reason


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://172.16.0.1/",
        "http://foo.local/",
        "http://service.internal/",
    ],
)
def test_local_and_private_addresses_are_blocked_by_default(url):
    decision = UrlPolicy().evaluate(url)
    assert not decision.allowed
    assert decision.risk is UrlRisk.BLOCKED


def test_local_addresses_allowed_only_when_opted_in():
    policy = UrlPolicy(allow_local_addresses=True)
    decision = policy.evaluate("http://127.0.0.1:8000/")
    assert decision.allowed
    assert decision.risk is UrlRisk.SENSITIVE


def test_domain_deny_list_blocks_subdomains():
    policy = UrlPolicy(blocked_domains=("evil.com",))
    assert not policy.evaluate("https://evil.com/").allowed
    assert not policy.evaluate("https://api.evil.com/").allowed
    assert policy.evaluate("https://notevil.com/").allowed


def test_domain_allow_list_is_exclusive():
    policy = UrlPolicy(allowed_domains=("trusted.com",))
    assert policy.evaluate("https://trusted.com/").allowed
    assert policy.evaluate("https://app.trusted.com/").allowed
    assert not policy.evaluate("https://other.com/").allowed


def test_login_page_is_classified_sensitive():
    decision = UrlPolicy().evaluate("https://example.com/login")
    assert decision.allowed
    assert decision.risk is UrlRisk.SENSITIVE


def test_file_urls_blocked_unless_allowed_and_within_roots(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    inside = root / "index.html"
    inside.write_text("hi")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")

    assert not UrlPolicy().evaluate(inside.as_uri()).allowed

    policy = UrlPolicy(allow_file_urls=True, allowed_file_roots=(str(root),))
    assert policy.evaluate(inside.as_uri()).allowed
    escape = policy.evaluate(outside.as_uri())
    assert not escape.allowed
    assert "permitted roots" in escape.reason


def test_file_url_with_a_backslash_is_refused_as_non_canonical():
    # ``as_uri()`` never emits a backslash; a backslash in a file URL is an
    # untrusted alias that could otherwise dodge the root check on Windows.
    policy = UrlPolicy(allow_file_urls=True, allowed_file_roots=("/tmp",))
    decision = policy.evaluate("file:///tmp\\..\\etc\\passwd")
    assert not decision.allowed
    assert "backslash" in decision.reason


def test_require_raises_on_blocked_url():
    with pytest.raises(PermissionDeniedError):
        UrlPolicy().require("javascript:alert(1)")
    # An allowed URL returns its decision rather than raising.
    assert validate_url("https://example.com/", UrlPolicy()).allowed
