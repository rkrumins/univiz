"""
Auth-service configuration — environment-driven.

``JWT_SECRET_KEY`` MUST be set explicitly (>= 32 chars) in every
environment — production, dev, and test. There is intentionally **no
ephemeral fallback**: a per-process random key silently invalidates
every outstanding session on restart and masks a missing-secret
misconfiguration in production. Absence or a too-weak value fails fast
at import so the process never starts in an insecure state.
"""
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_ALGORITHM = "HS256"
# HS256 needs a high-entropy shared secret. 32 chars is the floor we
# accept; anything shorter is rejected as weak.
_MIN_SECRET_LENGTH = 32
# RBAC Phase 1: short access-token TTL paired with Redis revocation
# set. Old default was 15 minutes; the design plan calls for ≤5 min so
# revocation lag stays within enterprise tolerances. Operators can
# override JWT_EXPIRY_MINUTES to fall back to the longer window if the
# revocation set is unavailable in their environment.
_DEFAULT_ACCESS_EXPIRY_MINUTES = 5
_DEFAULT_REFRESH_EXPIRY_DAYS = 7


class MissingSigningSecret(RuntimeError):
    """Raised at import when JWT_SECRET_KEY is unset or too weak."""


def _resolve_secret() -> str:
    key = os.getenv("JWT_SECRET_KEY")
    if not key:
        raise MissingSigningSecret(
            "JWT_SECRET_KEY is not set. Set a high-entropy secret "
            f"(>= {_MIN_SECRET_LENGTH} chars) in the environment — there "
            "is no ephemeral fallback. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
        )
    if len(key) < _MIN_SECRET_LENGTH:
        raise MissingSigningSecret(
            f"JWT_SECRET_KEY is too weak ({len(key)} chars); "
            f"require >= {_MIN_SECRET_LENGTH}."
        )
    return key


JWT_SECRET_KEY: str = _resolve_secret()
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", _DEFAULT_ALGORITHM)
JWT_EXPIRY_MINUTES: int = int(
    os.getenv("JWT_EXPIRY_MINUTES", str(_DEFAULT_ACCESS_EXPIRY_MINUTES))
)
JWT_REFRESH_EXPIRY_DAYS: int = int(
    os.getenv("JWT_REFRESH_EXPIRY_DAYS", str(_DEFAULT_REFRESH_EXPIRY_DAYS))
)
JWT_ISSUER: str = os.getenv("JWT_ISSUER", "nexus-lineage")
JWT_AUDIENCE: str = os.getenv("JWT_AUDIENCE", "nexus-lineage")

# Cookie configuration. SameSite=Lax is safe for top-level navigation;
# Secure is enforced by default and can only be disabled in dev/test.
COOKIE_SECURE: bool = os.getenv("AUTH_COOKIE_SECURE", "true").lower() != "false"
COOKIE_DOMAIN: str | None = os.getenv("AUTH_COOKIE_DOMAIN") or None
COOKIE_SAMESITE: str = os.getenv("AUTH_COOKIE_SAMESITE", "lax").lower()
