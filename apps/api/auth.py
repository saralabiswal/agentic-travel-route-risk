"""OIDC JWT validation and RouteShield claims-to-role mapping."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from domain.models import UserRole


@dataclass(frozen=True)
class AuthenticatedActor:
    actor_id: str
    tenant_id: str
    role: UserRole


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    """Reuse verified JWKS clients rather than fetching key metadata per request."""
    return PyJWKClient(url, cache_keys=True)


def validate_bearer_token(token: str) -> AuthenticatedActor:
    issuer = os.getenv("OIDC_ISSUER")
    audience = os.getenv("OIDC_AUDIENCE")
    jwks_url = os.getenv("OIDC_JWKS_URL")
    if not issuer or not audience or not jwks_url:
        raise ValueError("OIDC is not configured")
    signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token, signing_key, algorithms=["RS256", "ES256"], audience=audience, issuer=issuer
    )
    try:
        actor_claim = os.getenv("OIDC_ACTOR_ID_CLAIM", "sub")
        tenant_claim = os.getenv("OIDC_TENANT_ID_CLAIM", "tenant_id")
        role_claim = os.getenv("OIDC_ROLE_CLAIM", "role")
        return AuthenticatedActor(
            actor_id=str(claims[actor_claim]),
            tenant_id=str(claims[tenant_claim]),
            role=UserRole(claims[role_claim]),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("token is missing RouteShield authorization claims") from exc
