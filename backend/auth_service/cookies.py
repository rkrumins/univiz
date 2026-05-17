"""
Cookie names and helpers for session transport.

Three cookies make up a session:

* ``nx_access``  — the access JWT. ``HttpOnly``, ``Secure``, ``SameSite=Lax``,
  path ``/``. Sent on every request to the API; read by ``get_current_user``.
* ``nx_refresh`` — the refresh JWT. ``HttpOnly``, ``Secure``, ``SameSite=Lax``,
  path ``/api/v1/auth/refresh`` (so it's only sent to the refresh endpoint).
* ``nx_csrf``    — the CSRF token. *Readable* by JavaScript so the frontend
  can echo it back as ``X-CSRF-Token``. ``Secure``, ``SameSite=Lax``.

All cookie attributes are derived from environment-driven config in
``core.config`` so deployments can tune ``Secure`` (off in local HTTP dev),
``Domain`` (parent-domain sharing), and ``SameSite`` (e.g. ``strict``).
"""
from __future__ import annotations

from fastapi import Request, Response

from .core.config import COOKIE_DOMAIN, COOKIE_SAMESITE, COOKIE_SECURE
from .interface import SessionTokens

ACCESS_COOKIE_NAME = "nx_access"
REFRESH_COOKIE_NAME = "nx_refresh"
CSRF_COOKIE_NAME = "nx_csrf"
# Short-lived signed cookie holding the in-flight OIDC handshake
# (state / nonce / PKCE verifier). Scoped to the auth subtree so it is
# only ever sent to the callback. SameSite=Lax is required: the IdP
# redirects back via a top-level GET navigation.
OIDC_COOKIE_NAME = "nx_oidc"
OIDC_COOKIE_PATH = "/api/v1/auth/"
_OIDC_COOKIE_MAX_AGE = 600

# Refresh cookie is scoped to the /auth subtree so it's sent to /refresh
# AND /logout (logout needs to read it to revoke the rotation family)
# but is excluded from every data endpoint where it's never useful.
REFRESH_COOKIE_PATH = "/api/v1/auth/"


def _common_kwargs() -> dict:
    return {
        "secure": COOKIE_SECURE,
        "samesite": COOKIE_SAMESITE,
        "domain": COOKIE_DOMAIN,
    }


def set_session_cookies(response: Response, tokens: SessionTokens) -> None:
    """Attach the three session cookies to *response*. Called by /login and /refresh."""
    common = _common_kwargs()
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=tokens.access_token,
        max_age=tokens.access_max_age_seconds,
        httponly=True,
        path="/",
        **common,
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=tokens.refresh_token,
        max_age=tokens.refresh_max_age_seconds,
        httponly=True,
        path=REFRESH_COOKIE_PATH,
        **common,
    )
    # CSRF lifetime follows the refresh cookie, NOT the access cookie.
    # If the two matched, a user whose access cookie just expired would
    # lose the CSRF cookie at the same moment — the next write would
    # 403 on CSRF before the 401-triggered silent refresh could run,
    # forcing a re-login every ``JWT_EXPIRY_MINUTES``. While refresh is
    # still valid we want every state-changing request to be able to
    # mint the double-submit header.
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=tokens.csrf_token,
        max_age=tokens.refresh_max_age_seconds,
        httponly=False,
        path="/",
        **common,
    )


def clear_session_cookies(response: Response) -> None:
    """Remove the three session cookies. Called by /logout (and on auth failure)."""
    common = _common_kwargs()
    # Browsers only delete a cookie when the deletion call repeats the
    # original path/domain/secure attributes.
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/", **common)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, **common)
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", **common)


def read_access_cookie(request: Request) -> str | None:
    return request.cookies.get(ACCESS_COOKIE_NAME)


def read_refresh_cookie(request: Request) -> str | None:
    return request.cookies.get(REFRESH_COOKIE_NAME)


def read_csrf_cookie(request: Request) -> str | None:
    return request.cookies.get(CSRF_COOKIE_NAME)


def set_oidc_cookie(response: Response, state_token: str) -> None:
    response.set_cookie(
        key=OIDC_COOKIE_NAME,
        value=state_token,
        max_age=_OIDC_COOKIE_MAX_AGE,
        httponly=True,
        path=OIDC_COOKIE_PATH,
        **_common_kwargs(),
    )


def clear_oidc_cookie(response: Response) -> None:
    response.delete_cookie(
        OIDC_COOKIE_NAME, path=OIDC_COOKIE_PATH, **_common_kwargs()
    )


def read_oidc_cookie(request: Request) -> str | None:
    return request.cookies.get(OIDC_COOKIE_NAME)
