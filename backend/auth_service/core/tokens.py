"""
JWT token helpers — access, refresh, and invite.

Three token families share the same signing key but use distinct audiences
so a token of one type can never be presented in place of another:

    access  : aud = JWT_AUDIENCE                 (short-lived, ~15 min)
    refresh : aud = JWT_AUDIENCE + ":refresh"    (longer, ~7 days, carries jti+family)
    invite  : aud = JWT_AUDIENCE + ":invite"     (signup invite, ~72 h)
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import jwt

from .config import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    JWT_EXPIRY_MINUTES,
    JWT_ISSUER,
    JWT_AUDIENCE,
    JWT_REFRESH_EXPIRY_DAYS,
)

_REFRESH_AUDIENCE = f"{JWT_AUDIENCE}:refresh"
_INVITE_AUDIENCE = f"{JWT_AUDIENCE}:invite"
_OIDC_STATE_AUDIENCE = f"{JWT_AUDIENCE}:oidc_state"


# ── Access tokens ────────────────────────────────────────────────────

def create_access_token(
    user_id: str,
    email: str,
    role: str,
    extra: dict | None = None,
) -> str:
    """Create a signed access JWT."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRY_MINUTES),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode an access JWT.

    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure
    (including audience mismatch — i.e. a refresh token presented as access).
    """
    return jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
    )


# ── Refresh tokens ───────────────────────────────────────────────────

@dataclass(frozen=True)
class RefreshClaims:
    sub: str          # user id
    jti: str          # unique token id (for revocation tracking)
    family_id: str    # rotation chain id (for reuse detection)
    exp: int          # unix epoch


def create_refresh_token(
    user_id: str,
    family_id: str | None = None,
    extra: dict | None = None,
) -> tuple[str, RefreshClaims]:
    """Create a signed refresh JWT.

    Returns (token, claims). When *family_id* is None a new family is started
    (this is what /login does). Pass an existing family_id when rotating from
    /refresh so the chain can be tracked for reuse-detection.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=JWT_REFRESH_EXPIRY_DAYS)
    jti = secrets.token_urlsafe(16)
    fam = family_id or secrets.token_urlsafe(16)
    payload: dict = {
        "sub": user_id,
        "jti": jti,
        "fam": fam,
        "iss": JWT_ISSUER,
        "aud": _REFRESH_AUDIENCE,
        "iat": now,
        "exp": expires_at,
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    claims = RefreshClaims(
        sub=user_id,
        jti=jti,
        family_id=fam,
        exp=int(expires_at.timestamp()),
    )
    return token, claims


def decode_refresh_token(token: str) -> RefreshClaims:
    """Decode a refresh JWT into RefreshClaims.

    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    payload = jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        audience=_REFRESH_AUDIENCE,
    )
    sub = payload.get("sub")
    jti = payload.get("jti")
    fam = payload.get("fam")
    exp = payload.get("exp")
    if not (sub and jti and fam and exp):
        raise jwt.InvalidTokenError("Refresh token missing required claims")
    return RefreshClaims(sub=sub, jti=jti, family_id=fam, exp=int(exp))


# ── Invite tokens ────────────────────────────────────────────────────

def create_invite_token(
    role: str,
    created_by: str,
    expires_in_hours: int = 72,
) -> tuple[str, str]:
    """Create a signed invite JWT. Returns (token, expires_at_iso)."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=expires_in_hours)
    payload = {
        "purpose": "invite",
        "role": role,
        "created_by": created_by,
        "iss": JWT_ISSUER,
        "aud": _INVITE_AUDIENCE,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return token, expires_at.isoformat()


def decode_invite_token(token: str) -> dict:
    """Decode an invite JWT. Returns the payload dict.

    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    payload = jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        audience=_INVITE_AUDIENCE,
    )
    if payload.get("purpose") != "invite":
        raise jwt.InvalidTokenError("Not an invite token")
    return payload


# ── OIDC flow-state tokens ───────────────────────────────────────────
#
# The Authorization-Code + PKCE dance needs ``state``, ``nonce`` and the
# PKCE ``code_verifier`` to survive the round-trip to the IdP. Rather
# than a server-side session store we sign them into a short-lived,
# HttpOnly cookie. The signature makes the cookie tamper-proof; the
# short expiry bounds the window for a stolen-cookie replay.

def create_oidc_state_token(
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    next_path: str,
    expires_in_minutes: int = 10,
) -> str:
    """Sign the in-flight OIDC handshake parameters into a JWT."""
    now = datetime.now(timezone.utc)
    payload = {
        "purpose": "oidc_state",
        "state": state,
        "nonce": nonce,
        "cv": code_verifier,
        "next": next_path,
        "iss": JWT_ISSUER,
        "aud": _OIDC_STATE_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=expires_in_minutes),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_oidc_state_token(token: str) -> dict:
    """Decode an OIDC flow-state JWT. Returns the payload dict.

    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    payload = jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        audience=_OIDC_STATE_AUDIENCE,
    )
    if payload.get("purpose") != "oidc_state":
        raise jwt.InvalidTokenError("Not an OIDC state token")
    return payload
