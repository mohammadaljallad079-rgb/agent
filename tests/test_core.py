"""Redaction and configuration tests."""

from __future__ import annotations

import pytest

from arabic_agent.core.config import Settings
from arabic_agent.core.errors import ConfigurationError
from arabic_agent.core.redaction import REDACTED, redact


def test_sensitive_keys_are_masked():
    result = redact({"api_key": "abc", "nested": {"password": "x", "ok": "y"}})
    assert result["api_key"] == REDACTED
    assert result["nested"]["password"] == REDACTED
    assert result["nested"]["ok"] == "y"


def test_credential_value_patterns_are_masked():
    result = redact("token=sk-abcdefghijklmnopqrstuvwxyz1234")
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in result
    assert REDACTED in result


def test_jwt_is_masked():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcdefghijklmnop"
    assert jwt not in redact(f"Authorization: Bearer {jwt}")


def test_lists_are_redacted():
    result = redact([{"secret": "a"}, {"fine": "b"}])
    assert result[0]["secret"] == REDACTED
    assert result[1]["fine"] == "b"


def test_settings_have_no_default_secrets(monkeypatch):
    # The invariant is about *defaults*, so clear any ambient AAP_* values a
    # developer may have exported (e.g. AAP_DATABASE_URL when running the
    # Postgres integration suite) before constructing settings.
    for key in ("AAP_OPENAI_API_KEY", "AAP_API_KEYS", "AAP_DATABASE_URL", "AAP_REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(environment="test")
    assert settings.openai_api_key.get_secret_value() == ""
    assert settings.api_keys.get_secret_value() == ""
    assert settings.database_url.get_secret_value() == ""


def test_settings_reject_invalid_numeric_bounds():
    with pytest.raises(Exception):
        Settings(environment="test", max_steps_per_run=0)


def test_cors_origins_are_split_from_csv():
    settings = Settings(environment="test", cors_origins="https://a.com,https://b.com")
    assert settings.cors_origins == ("https://a.com", "https://b.com")


def test_production_requires_api_keys_when_auth_enabled():
    from arabic_agent.api.auth import ApiKeyAuth

    settings = Settings(environment="prod", require_auth=True)
    with pytest.raises(ConfigurationError):
        ApiKeyAuth.from_settings(settings)


def test_api_keys_parse_into_principals():
    from arabic_agent.api.auth import ApiKeyAuth

    settings = Settings(
        environment="test",
        api_keys='{"k1": {"tenant_id": "tnt_1", "user_id": "usr_1", "roles": ["admin"]}}',
    )
    auth = ApiKeyAuth.from_settings(settings)
    principal = auth.resolve("k1")
    assert principal is not None
    assert principal.tenant_id == "tnt_1"
    assert principal.has_role("admin")
    assert auth.resolve("unknown") is None
