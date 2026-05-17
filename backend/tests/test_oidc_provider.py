"""Phase 1 — OIDC provider: PKCE/state/nonce + ID-token verification.

No live IdP. Discovery + token-exchange HTTP are stubbed; the ID token
is signed with a locally-generated RSA key and verified against a JWKS
built from that key's public half, so the Authlib verification path is
exercised end to end.
"""
from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore")  # silence AuthlibDeprecationWarning

from authlib.jose import jwt as jose_jwt, JsonWebKey

from backend.auth_service.providers.oidc import (
    OidcError,
    OidcProvider,
    OidcSettings,
    _pkce_pair,
)

_ISSUER = "https://idp.example.com"
_CLIENT_ID = "client-123"


def _settings() -> OidcSettings:
    return OidcSettings(
        enabled=True,
        issuer=_ISSUER,
        client_id=_CLIENT_ID,
        client_secret="secret",
        redirect_uri="https://app.example.com/api/v1/auth/oidc/callback",
        scopes="openid email profile",
    )


@pytest.fixture()
def rsa_key():
    return JsonWebKey.generate_key("RSA", 2048, {"kid": "k1"}, is_private=True)


@pytest.fixture()
def public_jwks(rsa_key):
    return JsonWebKey.import_key_set(
        {"keys": [rsa_key.as_dict(is_private=False)]}
    )


def _sign(rsa_key, **claims) -> str:
    base = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "idp-sub-1",
        "email": "Alice@Example.com",
        "email_verified": True,
        "given_name": "Alice",
        "family_name": "Smith",
        "nonce": "nonce-1",
        "exp": 9999999999,
        "iat": 1,
    }
    base.update(claims)
    tok = jose_jwt.encode({"alg": "RS256", "kid": "k1"}, base, rsa_key)
    return tok.decode() if isinstance(tok, bytes) else tok


def test_pkce_pair_is_rfc7636_compliant():
    verifier, challenge = _pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge  # base64url, no padding


@pytest.mark.asyncio
async def test_build_authorization_includes_pkce_state_nonce(monkeypatch):
    provider = OidcProvider(_settings())

    async def _meta():
        return {"authorization_endpoint": f"{_ISSUER}/authorize"}

    monkeypatch.setattr(provider, "_discovery", _meta)
    url, flow = await provider.build_authorization("/dashboard")

    assert url.startswith(f"{_ISSUER}/authorize?")
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert f"state={flow['state']}" in url
    assert f"nonce={flow['nonce']}" in url
    assert flow["next"] == "/dashboard"
    assert 43 <= len(flow["code_verifier"]) <= 128
    # implicit flow must never be offered
    assert "response_type=token" not in url
    assert "response_type=id_token" not in url


def _patch_token_endpoint(monkeypatch, id_token: str | None, access_token="at"):
    """Stub the token-endpoint POST inside fetch_identity."""
    import backend.auth_service.providers.oidc as oidc_mod

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            body = {"access_token": access_token}
            if id_token is not None:
                body["id_token"] = id_token
            return body

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(oidc_mod.httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_fetch_identity_happy_path(monkeypatch, rsa_key, public_jwks):
    provider = OidcProvider(_settings())

    async def _meta():
        return {"token_endpoint": f"{_ISSUER}/token"}

    async def _jwks(*, force=False):
        return public_jwks

    monkeypatch.setattr(provider, "_discovery", _meta)
    monkeypatch.setattr(provider, "_load_jwks", _jwks)
    _patch_token_endpoint(monkeypatch, _sign(rsa_key))

    identity = await provider.fetch_identity(
        code="abc", code_verifier="v", nonce="nonce-1",
    )
    assert identity.provider == "oidc"
    assert identity.external_id == "idp-sub-1"
    assert identity.email == "alice@example.com"  # normalised
    assert identity.first_name == "Alice"
    assert identity.raw_claims["email_verified"] is True


@pytest.mark.asyncio
async def test_fetch_identity_rejects_nonce_mismatch(
    monkeypatch, rsa_key, public_jwks
):
    provider = OidcProvider(_settings())
    monkeypatch.setattr(provider, "_discovery", lambda: _async({"token_endpoint": f"{_ISSUER}/token"}))
    monkeypatch.setattr(provider, "_load_jwks", lambda *, force=False: _async(public_jwks))
    _patch_token_endpoint(monkeypatch, _sign(rsa_key, nonce="attacker"))

    with pytest.raises(OidcError):
        await provider.fetch_identity(
            code="abc", code_verifier="v", nonce="nonce-1",
        )


@pytest.mark.asyncio
async def test_fetch_identity_rejects_tampered_signature(
    monkeypatch, rsa_key, public_jwks
):
    provider = OidcProvider(_settings())
    monkeypatch.setattr(provider, "_discovery", lambda: _async({"token_endpoint": f"{_ISSUER}/token"}))
    monkeypatch.setattr(provider, "_load_jwks", lambda *, force=False: _async(public_jwks))
    tampered = _sign(rsa_key)[:-3] + "AAA"
    _patch_token_endpoint(monkeypatch, tampered)

    with pytest.raises(OidcError):
        await provider.fetch_identity(
            code="abc", code_verifier="v", nonce="nonce-1",
        )


@pytest.mark.asyncio
async def test_fetch_identity_missing_id_token(monkeypatch):
    provider = OidcProvider(_settings())
    monkeypatch.setattr(provider, "_discovery", lambda: _async({"token_endpoint": f"{_ISSUER}/token"}))
    _patch_token_endpoint(monkeypatch, id_token=None)

    with pytest.raises(OidcError):
        await provider.fetch_identity(
            code="abc", code_verifier="v", nonce="nonce-1",
        )


@pytest.mark.asyncio
async def test_jwks_refetched_on_kid_miss(monkeypatch, rsa_key, public_jwks):
    """First JWKS load returns an empty key set (simulating rotation);
    the forced refetch returns the correct keys and verification then
    succeeds — proving the rotation retry path works."""
    provider = OidcProvider(_settings())
    empty = JsonWebKey.import_key_set({"keys": []})

    calls = {"n": 0}

    async def _jwks(*, force=False):
        calls["n"] += 1
        return empty if not force else public_jwks

    monkeypatch.setattr(provider, "_discovery", lambda: _async({"token_endpoint": f"{_ISSUER}/token"}))
    monkeypatch.setattr(provider, "_load_jwks", _jwks)
    _patch_token_endpoint(monkeypatch, _sign(rsa_key))

    identity = await provider.fetch_identity(
        code="abc", code_verifier="v", nonce="nonce-1",
    )
    assert identity.external_id == "idp-sub-1"
    assert calls["n"] == 2  # cached attempt, then forced refetch


def _async(value):
    async def _coro():
        return value
    return _coro()
