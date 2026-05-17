"""
Cookie-based authentication endpoints.

Mounted at ``/api/v1/auth/`` alongside the legacy router (which still owns
signup, password reset, and invite verification — those endpoints don't
issue session cookies). All endpoints here go through the
``IdentityService`` on ``request.app.state``, so swapping the in-process
implementation for an HTTP client only requires touching app startup.
"""
from __future__ import annotations

import hmac
import logging

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..cookies import (
    clear_oidc_cookie,
    clear_session_cookies,
    read_access_cookie,
    read_oidc_cookie,
    read_refresh_cookie,
    set_oidc_cookie,
    set_session_cookies,
)
from ..core.tokens import create_oidc_state_token, decode_oidc_state_token
from ..interface import (
    IdentityService,
    InvalidCredentials,
    InvalidRefreshToken,
    SSOAuthError,
    User,
)
from ..providers import get_provider

logger = logging.getLogger(__name__)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


# ── Request / response models ─────────────────────────────────────────

class LoginBody(BaseModel):
    email: str
    password: str


class SessionResponse(BaseModel):
    """Returned by /login and /me. The access token lives in the
    ``nx_access`` cookie — never in the response body."""
    model_config = ConfigDict(populate_by_name=True)
    user: User


class _Ack(BaseModel):
    """Tiny ack body for /logout and /refresh so clients can switch on it."""
    ok: bool = True


# ── Helpers ───────────────────────────────────────────────────────────

def _identity_service(request: Request) -> IdentityService:
    """Pull the configured IdentityService off the app. Configured in main.py."""
    svc = getattr(request.app.state, "identity_service", None)
    if svc is None:
        raise RuntimeError(
            "IdentityService not configured on app.state. "
            "Set it during startup (see backend/app/main.py)."
        )
    return svc


# ── POST /auth/login ──────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=SessionResponse,
    response_model_by_alias=True,
)
@limiter.limit("10/minute")
async def login(
    request: Request,
    response: Response,
    body: LoginBody,
):
    """Authenticate by email + password.

    On success: sets ``nx_access``, ``nx_refresh``, and ``nx_csrf`` cookies
    and returns ``{ user }``. The access token is never in the response body.
    """
    svc = _identity_service(request)
    try:
        user, tokens = await svc.login(body.email, body.password)
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    set_session_cookies(response, tokens)
    logger.info("Login succeeded for user=%s", user.id)
    return SessionResponse(user=user)


# ── POST /auth/logout ─────────────────────────────────────────────────

@router.post("/logout", response_model=_Ack)
async def logout(request: Request, response: Response):
    """Revoke the refresh-token family and clear all session cookies.

    Idempotent: returning ``ok=true`` regardless of whether a session was
    present so clients can call this freely (e.g. on every app boot).
    """
    svc = _identity_service(request)
    refresh = read_refresh_cookie(request)
    await svc.logout(refresh)
    clear_session_cookies(response)
    return _Ack()


# ── POST /auth/refresh ────────────────────────────────────────────────

@router.post(
    "/refresh",
    response_model=SessionResponse,
    response_model_by_alias=True,
)
@limiter.limit("30/minute")
async def refresh(request: Request, response: Response):
    """Rotate the refresh token and reissue access + refresh + CSRF cookies.

    Returns the current ``user`` so the frontend can keep its in-memory
    profile fresh on long-lived tabs.
    """
    svc = _identity_service(request)
    token = read_refresh_cookie(request)
    if not token:
        clear_session_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )
    try:
        user, tokens = await svc.refresh(token)
    except InvalidRefreshToken as exc:
        clear_session_cookies(response)
        logger.info("Refresh rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token invalid or expired",
        )

    set_session_cookies(response, tokens)
    return SessionResponse(user=user)


# ── OIDC (Authorization Code + PKCE) ──────────────────────────────────

# Generic, relative landing pages. Absolute URLs are never used so the
# callback can't be turned into an open redirect.
_OIDC_FAILURE_PATH = "/login?sso_error=1"


def _oidc_provider():
    """Return the registered, enabled OIDC provider or 404."""
    try:
        provider = get_provider("oidc")
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not configured")
    if not getattr(provider, "enabled", False):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not configured")
    return provider


def _safe_next(raw: str | None) -> str:
    """Only allow a same-site relative path. Anything that could escape
    the origin (scheme, host, protocol-relative ``//``) falls back to
    the app root — an open-redirect guard on the post-login bounce."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/oidc/login")
async def oidc_login(request: Request, next: str | None = None):
    """Leg 1: build the IdP authorization URL and 302 there.

    The handshake parameters (state / nonce / PKCE verifier) are signed
    into the short-lived, HttpOnly ``nx_oidc`` cookie — no server-side
    session store.
    """
    provider = _oidc_provider()
    next_path = _safe_next(next)
    try:
        auth_url, flow = await provider.build_authorization(next_path)
    except Exception as exc:  # noqa: BLE001 — config/network → generic 503
        logger.warning("OIDC authorize build failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "OIDC temporarily unavailable"
        )

    state_token = create_oidc_state_token(
        state=flow["state"],
        nonce=flow["nonce"],
        code_verifier=flow["code_verifier"],
        next_path=flow["next"],
    )
    response = RedirectResponse(auth_url, status_code=status.HTTP_302_FOUND)
    set_oidc_cookie(response, state_token)
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Leg 2: validate ``state``, exchange the code, verify the ID
    token, then find-or-provision and issue the session.

    Every failure path clears the flow cookie and bounces to a generic
    error page — the browser never sees the reason (it's audited).
    """
    provider = _oidc_provider()
    svc = _identity_service(request)

    def _fail(reason: str) -> RedirectResponse:
        logger.info("OIDC callback failed: %s", reason)
        resp = RedirectResponse(
            _OIDC_FAILURE_PATH, status_code=status.HTTP_302_FOUND
        )
        clear_oidc_cookie(resp)
        return resp

    if error or not code or not state:
        return _fail(f"idp_error={error or 'missing_code_or_state'}")

    raw_cookie = read_oidc_cookie(request)
    if not raw_cookie:
        return _fail("missing_flow_cookie")
    try:
        flow = decode_oidc_state_token(raw_cookie)
    except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError) as exc:
        return _fail(f"bad_flow_cookie:{exc}")

    # Constant-time state comparison (CSRF defence for the callback).
    if not hmac.compare_digest(str(flow.get("state", "")), state):
        return _fail("state_mismatch")

    try:
        identity = await provider.fetch_identity(
            code=code,
            code_verifier=flow["code_verifier"],
            nonce=flow["nonce"],
        )
    except Exception as exc:  # noqa: BLE001 — OidcError etc. → generic
        return _fail(f"token_or_idtoken:{exc}")

    try:
        user, tokens = await svc.complete_sso_login(identity)
    except SSOAuthError as exc:
        return _fail(f"sso_login_rejected:{exc}")

    response = RedirectResponse(
        _safe_next(flow.get("next")), status_code=status.HTTP_302_FOUND
    )
    set_session_cookies(response, tokens)
    clear_oidc_cookie(response)
    logger.info("OIDC login succeeded for user=%s", user.id)
    return response


# ── GET /auth/me ──────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=SessionResponse,
    response_model_by_alias=True,
)
async def me(request: Request):
    """Validate the access cookie and return the current user.

    The frontend calls this on app boot to determine whether to render
    the dashboard (200) or redirect to /login (401).
    """
    svc = _identity_service(request)
    user = await svc.validate_session(read_access_cookie(request))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return SessionResponse(user=user)
