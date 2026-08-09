import pytest

from apps.api.auth import validate_bearer_token


def test_auth_fails_closed_without_oidc_configuration(monkeypatch):
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        validate_bearer_token("not-a-token")
