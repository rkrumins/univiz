"""Authentication helpers for the load tester.

Supports two modes, picked in priority order:

1. **Bearer token** — ``SYNODIC_BEARER_TOKEN`` set. The token is sent as
   ``Authorization: Bearer <token>`` on every request. Simplest for CI
   with a long-lived service-account token.

2. **Cookie login** — ``SYNODIC_USER`` + ``SYNODIC_PASSWORD`` set. Posts
   to ``/api/v1/auth/login``, captures the JWT + CSRF cookies, and
   threads the ``X-CSRF-Token`` header so non-GET requests pass the CSRF
   middleware. Matches the production browser flow.

Both modes mutate the Locust ``HttpUser.client`` so all subsequent
requests are authenticated transparently. Failure to authenticate
aborts the user (Locust marks it failed) rather than silently issuing
401-flooded traffic.
"""
from __future__ import annotations

import logging
import threading

from config import SETTINGS  # loadtest/ is on sys.path via locustfile.py

logger = logging.getLogger(__name__)

# Constants intentionally duplicated from the backend so this module
# stays import-free of backend code. If the backend changes them, the
# load test must be updated — that's deliberate decoupling.
_CSRF_COOKIE_NAME = "nx_csrf"
_CSRF_HEADER_NAME = "X-CSRF-Token"

# Process-wide shared-login state. The backend rate-limits /auth/login
# at 10/minute by default (see auth_service/api/router.py); a swarm of
# even 10 users each calling login on on_start saturates that bucket
# in the first second and the next scenario in `make smoke` gets 429s
# (or 500s if the backend's slowapi handler is misconfigured). So we
# log in exactly once per Locust process and replay the resulting
# session cookies + CSRF header into every spawned user. With Locust
# running under gevent-monkey-patched threading, this lock is a green
# semaphore — only the first user does the round-trip, others wait.
_shared_lock = threading.Lock()
_shared_done = False
_shared_cookies: list = []
_shared_csrf: str | None = None


class AuthError(RuntimeError):
    """Raised when the load-test user cannot authenticate."""


def authenticate(client) -> None:
    """Make ``client`` authenticated for the remainder of the run.

    Picks the strongest auth mode available from env. Mutates the
    client's default headers / cookies in place; subsequent requests
    issued via ``client.get/post/...`` are auth-bearing.

    For cookie login, only the FIRST user per Locust process actually
    hits /auth/login; subsequent users copy the shared cookies. This
    keeps the rate-limited login endpoint from going 429 under a
    typical 10–200-user smoke swarm.

    Raises ``AuthError`` if no credentials are configured or if the
    one shared login round-trip fails. Locust treats the user as failed
    in that case, so the swarm doesn't generate unauthenticated noise.
    """
    if SETTINGS.bearer_token:
        client.headers["Authorization"] = f"Bearer {SETTINGS.bearer_token}"
        logger.debug("Authenticated via bearer token")
        return

    if not (SETTINGS.username and SETTINGS.password):
        raise AuthError(
            "No credentials configured. Set SYNODIC_BEARER_TOKEN, or both "
            "SYNODIC_USER and SYNODIC_PASSWORD."
        )

    global _shared_done, _shared_cookies, _shared_csrf
    with _shared_lock:
        if not _shared_done:
            # First user: do the real login on its client, then snapshot
            # the resulting cookies + CSRF header for everyone else.
            _cookie_login(client, SETTINGS.username, SETTINGS.password)
            _shared_cookies = list(client.cookies)
            _shared_csrf = client.headers.get(_CSRF_HEADER_NAME)
            _shared_done = True
            logger.info(
                "Shared cookie login complete — %d cookie(s) cached; "
                "subsequent users will skip /auth/login.",
                len(_shared_cookies),
            )
            return

    # Subsequent users (lock already released): copy the cached session.
    for cookie in _shared_cookies:
        client.cookies.set_cookie(cookie)
    if _shared_csrf:
        client.headers[_CSRF_HEADER_NAME] = _shared_csrf
    logger.debug("Authenticated via shared cookie jar")


def _cookie_login(client, username: str, password: str) -> None:
    """Perform the production cookie-login dance.

    POST /api/v1/auth/login → server sets ``nx_csrf`` + the JWT cookie.
    We then mirror the CSRF cookie into the ``X-CSRF-Token`` header
    (double-submit pattern) so future non-GET requests pass the CSRF
    middleware. The Locust ``HttpUser`` keeps the cookie jar across
    requests, so we don't have to thread anything explicitly.
    """
    # ``name=login`` groups this request in Locust stats; the actual
    # request volume is dominated by the scenario traffic so the login
    # never bumps the percentiles.
    with client.post(
        SETTINGS.login_path,
        json={"email": username, "password": password},
        name="auth:login",
        catch_response=True,
    ) as resp:
        # Locust returns status_code=0 for transport-level failures
        # (connection refused, DNS error, TLS handshake fail). The
        # exception is stashed on the response — surface it so the
        # operator immediately sees "connection refused" instead of
        # the unhelpful "HTTP 0 body=''" we'd get otherwise.
        if resp.status_code == 0:
            underlying = getattr(resp, "error", None) or getattr(resp, "exception", None)
            host = getattr(client, "base_url", None) or SETTINGS.host
            resp.failure(f"transport error to {host}: {underlying!r}")
            raise AuthError(
                f"could not reach {host}{SETTINGS.login_path}: {underlying or 'no response'}. "
                "Common causes: backend not running, wrong SYNODIC_HOST/port, "
                "or VPN/firewall blocking the connection."
            )
        if resp.status_code != 200:
            resp.failure(f"login failed: HTTP {resp.status_code}")
            if resp.status_code in (401, 403):
                hint = "Check SYNODIC_USER / SYNODIC_PASSWORD."
            elif resp.status_code == 429:
                hint = (
                    "Backend rate-limited the login. Lower --users / --spawn-rate, "
                    "or wait a minute before re-running."
                )
            elif resp.status_code >= 500:
                hint = (
                    f"Backend 5xx — this is a server-side error, not a credentials issue. "
                    f"Check the backend logs at {SETTINGS.host} and verify SYNODIC_HOST "
                    f"points at the viz-service (typically port 8000), not the graph service."
                )
            elif resp.status_code == 404:
                hint = (
                    f"Login route not found. Verify SYNODIC_HOST ({SETTINGS.host}) and "
                    f"SYNODIC_LOGIN_PATH ({SETTINGS.login_path}) point at the viz-service."
                )
            else:
                hint = "Verify SYNODIC_HOST and credentials."
            raise AuthError(
                f"login failed: HTTP {resp.status_code} body={resp.text[:200]!r}. {hint}"
            )
        resp.success()

    csrf = client.cookies.get(_CSRF_COOKIE_NAME)
    if not csrf:
        raise AuthError(f"login did not set {_CSRF_COOKIE_NAME!r} cookie")
    client.headers[_CSRF_HEADER_NAME] = csrf

    # Backend defaults to AUTH_COOKIE_SECURE=true (see
    # backend/auth_service/core/config.py). When the test runs over
    # plain http (typical for local dev), requests will store but
    # NEVER resend cookies with Secure=True — every subsequent admin
    # call would 401. Strip Secure on the session cookies we just
    # received so the jar resends them over the same http connection
    # that just minted them. Safe: we're talking to the same backend
    # over the same loopback origin.
    if (SETTINGS.host or "").lower().startswith("http://"):
        stripped = 0
        for cookie in client.cookies:
            if cookie.secure:
                cookie.secure = False
                stripped += 1
        if stripped:
            logger.info(
                "Stripped Secure flag from %d session cookie(s) for plain-http host %s "
                "(set AUTH_COOKIE_SECURE=false on the backend to avoid this).",
                stripped, SETTINGS.host,
            )
    logger.debug("Authenticated via cookie login")
