"""
Public interface for the authentication service.

Anything outside the auth module — FastAPI dependencies, other services,
the future remote client — interacts with auth through ``IdentityService``
and the DTOs defined here. Implementations live in ``service.py``.

When this module is extracted into its own microservice, ``IdentityService``
becomes the wire contract: a ``RemoteIdentityService`` would implement the
same protocol over HTTP, and call sites would not change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


# ── Domain DTOs ──────────────────────────────────────────────────────

class User(BaseModel):
    """The authenticated identity as seen by consumers of this service.

    This is the cross-service contract: when auth becomes its own
    microservice, this is what /auth/me returns over HTTP.
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str
    email: str
    first_name: str = Field(alias="firstName")
    last_name: str = Field(alias="lastName")
    role: str
    status: str
    auth_provider: str = Field("local", alias="authProvider")
    created_at: str = Field("", alias="createdAt")
    updated_at: str = Field("", alias="updatedAt")

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass(frozen=True)
class SessionTokens:
    """The set of tokens issued by login/refresh.

    The CSRF token is *not* HttpOnly — the frontend reads it from the
    ``nx_csrf`` cookie and echoes it back as the ``X-CSRF-Token`` header
    on state-changing requests. The double-submit comparison is what
    proves the request was initiated by a same-origin script with cookie
    access.
    """
    access_token: str
    access_max_age_seconds: int
    refresh_token: str
    refresh_max_age_seconds: int
    csrf_token: str


# ── Service protocol ─────────────────────────────────────────────────

@runtime_checkable
class IdentityService(Protocol):
    """The boundary every auth consumer crosses.

    Today implemented in-process (``LocalIdentityService``); tomorrow can
    be implemented as an HTTP client (``RemoteIdentityService``) without
    any change to call sites.
    """

    async def validate_session(self, access_token: Optional[str]) -> Optional[User]:
        """Return the authenticated user or ``None`` if the token is missing,
        invalid, expired, or the user is not active."""
        ...

    async def login(self, email: str, password: str) -> tuple[User, SessionTokens]:
        """Authenticate by credentials and issue a fresh session.

        Raises ``InvalidCredentials`` for any failure (wrong password,
        unknown email, inactive account) — never reveals which.
        """
        ...

    async def logout(self, refresh_token: Optional[str]) -> None:
        """Revoke the refresh token and its rotation family. Idempotent."""
        ...

    async def refresh(self, refresh_token: str) -> tuple[User, SessionTokens]:
        """Rotate a refresh token: returns new (user, tokens).

        Raises ``InvalidRefreshToken`` if the token is missing/invalid/expired,
        or — critically — if the same refresh token is presented twice
        (reuse detection: revokes the entire family).
        """
        ...

    async def get_user(self, user_id: str) -> Optional[User]:
        """Look up a user by id. Returns ``None`` if not found or deleted."""
        ...

    async def complete_sso_login(self, identity) -> tuple[User, SessionTokens]:
        """Find-or-provision a user from a verified SSO ``ProviderIdentity``
        and issue a fresh session.

        Applies the identity-linking guardrails. Raises ``SSOAuthError``
        when linking is unsafe (the caller surfaces a generic failure).
        """
        ...


# ── Errors ───────────────────────────────────────────────────────────

class AuthError(Exception):
    """Base class for all auth-service errors that callers should handle."""


class InvalidCredentials(AuthError):
    """Wrong email / password combination, or account not active."""


class InvalidRefreshToken(AuthError):
    """Refresh token is missing, malformed, expired, or reused."""


class SSOAuthError(AuthError):
    """SSO login could not be completed — e.g. the IdP subject's email
    collides with an existing account and auto-linking is unsafe. The
    route maps this to a generic failure; the reason is audited, not
    shown to the browser."""


__all__ = [
    "User",
    "SessionTokens",
    "IdentityService",
    "AuthError",
    "InvalidCredentials",
    "InvalidRefreshToken",
    "SSOAuthError",
]
