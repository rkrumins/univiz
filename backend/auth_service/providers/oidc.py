"""
OIDC identity provider — Authorization Code flow + PKCE.

Implements the ``IdentityProvider`` seam for any spec-compliant OpenID
Connect IdP (Entra ID / Auth0 / Ping / Keycloak …). The provider owns
the *protocol*; the routes in ``api/router.py`` own the browser
redirects and cookie handling, and ``service.py`` owns find-or-provision
+ linking. That split keeps the existing login/refresh flow untouched.

Security properties enforced here:

* Authorization **Code** flow only — no implicit flow.
* **PKCE** (S256) on every request.
* ``state`` (CSRF for the callback) and ``nonce`` (ID-token replay)
  round-trip in a signed, short-lived cookie (see ``core.tokens``).
* ID-token verified with **JWKS** via Authlib (``CodeIDToken``):
  signature, ``iss``, ``aud == client_id``, ``exp``/``iat`` with
  bounded clock skew, ``nonce``, and ``at_hash``.
* JWKS cached with TTL and **refetched once on a ``kid`` miss** so key
  rotation needs no redeploy.

Crypto is delegated to Authlib — nothing here hand-rolls signature or
hash verification.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from authlib.jose import jwt as jose_jwt, JsonWebKey
from authlib.jose.errors import JoseError as _AuthlibJoseError
from authlib.oidc.core import CodeIDToken

try:  # Authlib >= 1.7 delegates JWT crypto to joserfc; its errors differ.
    from joserfc.errors import JoseError as _JoseRfcError
except ImportError:  # pragma: no cover - joserfc ships with Authlib 1.7+
    _JoseRfcError = ()

# Any of these means "this token did not verify against this key set".
# ValueError / KeyError cover a ``kid`` miss or an empty/!rotated JWKS,
# which the retry loop resolves by force-refetching the key set.
_VERIFY_ERRORS = (_AuthlibJoseError, _JoseRfcError, ValueError, KeyError)

from .base import ProviderCredentials, ProviderIdentity

logger = logging.getLogger(__name__)

# Bounded clock skew for ID-token exp/iat/nbf validation (seconds).
_CLOCK_SKEW_LEEWAY = 60
# JWKS / discovery cache lifetime (seconds).
_METADATA_TTL = 3600
_HTTP_TIMEOUT = 10.0


class OidcError(Exception):
    """Any failure in the OIDC handshake (config, network, or token
    verification). The route maps this to a generic auth failure —
    details are logged, never surfaced to the browser."""


@dataclass(frozen=True)
class OidcSettings:
    enabled: bool
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    # Post-login redirect targets are restricted to relative paths only
    # (see _safe_next) so the callback can't be turned into an open
    # redirect.
    default_next: str = "/"


def load_oidc_settings() -> OidcSettings:
    """Read OIDC config from the environment. Never raises — when
    disabled or unconfigured the provider simply reports not-enabled."""
    enabled = os.getenv("OIDC_ENABLED", "false").lower() == "true"
    return OidcSettings(
        enabled=enabled,
        issuer=os.getenv("OIDC_ISSUER", "").rstrip("/"),
        client_id=os.getenv("OIDC_CLIENT_ID", ""),
        client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
        redirect_uri=os.getenv("OIDC_REDIRECT_URI", ""),
        scopes=os.getenv("OIDC_SCOPES", "openid email profile"),
        default_next=os.getenv("OIDC_DEFAULT_NEXT", "/"),
    )


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)  # 43..128 chars per RFC 7636
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OidcProvider:
    """OIDC provider. One instance per process, registered as ``oidc``."""

    name = "oidc"

    def __init__(self, settings: OidcSettings):
        self._s = settings
        self._meta: Optional[dict] = None
        self._meta_at: float = 0.0
        self._jwks = None
        self._jwks_at: float = 0.0

    @property
    def enabled(self) -> bool:
        s = self._s
        return bool(
            s.enabled and s.issuer and s.client_id
            and s.client_secret and s.redirect_uri
        )

    # ── IdentityProvider protocol ────────────────────────────────────
    #
    # OIDC is not a credential (email/password) authenticator — the
    # browser-redirect dance is driven by the dedicated routes. This
    # satisfies the Protocol so the registry/typing stays uniform.
    async def authenticate(
        self, credentials: ProviderCredentials, *, get_user_by_email,
    ) -> Optional[ProviderIdentity]:
        return None

    # ── Discovery / JWKS (cached) ─────────────────────────────────────

    async def _discovery(self) -> dict:
        now = time.monotonic()
        if self._meta is not None and (now - self._meta_at) < _METADATA_TTL:
            return self._meta
        url = f"{self._s.issuer}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                meta = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"discovery fetch failed: {exc}") from exc
        # Defence-in-depth: the discovery doc's issuer must match the
        # configured issuer (prevents a swapped metadata endpoint).
        if meta.get("issuer", "").rstrip("/") != self._s.issuer:
            raise OidcError("discovery issuer mismatch")
        self._meta, self._meta_at = meta, now
        return meta

    async def _load_jwks(self, *, force: bool = False):
        now = time.monotonic()
        if (
            not force and self._jwks is not None
            and (now - self._jwks_at) < _METADATA_TTL
        ):
            return self._jwks
        meta = await self._discovery()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(meta["jwks_uri"])
                resp.raise_for_status()
                jwks = resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise OidcError(f"jwks fetch failed: {exc}") from exc
        self._jwks = JsonWebKey.import_key_set(jwks)
        self._jwks_at = now
        return self._jwks

    # ── Leg 1: build the authorization redirect ──────────────────────

    async def build_authorization(self, next_path: str) -> tuple[str, dict]:
        """Return (authorization_url, flow_state).

        ``flow_state`` carries the values the callback must verify
        (state, nonce, PKCE verifier, post-login next) — the route
        signs it into the ``nx_oidc`` cookie via ``core.tokens``.
        """
        if not self.enabled:
            raise OidcError("OIDC is not enabled/configured")
        meta = await self._discovery()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        params = {
            "response_type": "code",
            "client_id": self._s.client_id,
            "redirect_uri": self._s.redirect_uri,
            "scope": self._s.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{meta['authorization_endpoint']}?{urlencode(params)}"
        flow_state = {
            "state": state,
            "nonce": nonce,
            "code_verifier": verifier,
            "next": next_path,
        }
        return auth_url, flow_state

    # ── Leg 2: exchange code + verify ID token ───────────────────────

    async def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> ProviderIdentity:
        """Exchange the auth code (with the PKCE verifier) and return a
        fully-verified ``ProviderIdentity``. Raises ``OidcError`` on any
        failure — the caller must not provision on a partial result."""
        if not self.enabled:
            raise OidcError("OIDC is not enabled/configured")
        meta = await self._discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._s.redirect_uri,
            "client_id": self._s.client_id,
            "client_secret": self._s.client_secret,
            "code_verifier": code_verifier,
        }
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(meta["token_endpoint"], data=data)
                resp.raise_for_status()
                token = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"token exchange failed: {exc}") from exc

        id_token = token.get("id_token")
        if not id_token:
            raise OidcError("token response missing id_token")
        access_token = token.get("access_token")

        claims = await self._verify_id_token(id_token, nonce, access_token)

        sub = claims.get("sub")
        email = claims.get("email")
        if not sub or not email:
            raise OidcError("id_token missing sub/email")
        return ProviderIdentity(
            provider="oidc",
            external_id=str(sub),
            email=str(email).strip().lower(),
            first_name=str(claims.get("given_name", "") or ""),
            last_name=str(claims.get("family_name", "") or ""),
            # raw_claims feeds the linking policy (email_verified) and is
            # stored on the user row for audit.
            raw_claims=dict(claims),
        )

    async def _verify_id_token(
        self, id_token: str, nonce: str, access_token: Optional[str],
    ) -> dict:
        claims_options = {
            "iss": {"essential": True, "values": [self._s.issuer]},
            "aud": {"essential": True, "values": [self._s.client_id]},
        }
        claims_params = {"nonce": nonce}
        if access_token:
            claims_params["access_token"] = access_token

        # First attempt with the cached JWKS; on a kid miss / signature
        # failure, refetch the key set once (handles IdP key rotation)
        # before giving up.
        for force in (False, True):
            keyset = await self._load_jwks(force=force)
            try:
                claims = jose_jwt.decode(
                    id_token,
                    keyset,
                    claims_cls=CodeIDToken,
                    claims_options=claims_options,
                    claims_params=claims_params,
                )
                claims.validate(leeway=_CLOCK_SKEW_LEEWAY)
                return dict(claims)
            except _VERIFY_ERRORS as exc:
                if not force:
                    logger.info("ID-token verify retrying after JWKS refresh: %s", exc)
                    continue
                raise OidcError(f"id_token verification failed: {exc}") from exc
        raise OidcError("id_token verification failed")
