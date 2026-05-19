"""Phase 1 — SSO find-or-provision + identity-linking guardrails.

Exercises ``LocalIdentityService.complete_sso_login`` directly with a
verified ``ProviderIdentity`` (no IdP/network), against the per-test
SQLite DB.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from backend.app.db.repositories import user_repo
from backend.auth_service.interface import SSOAuthError
from backend.auth_service.providers.base import ProviderIdentity
from backend.auth_service.service import LocalIdentityService


def _identity(**over) -> ProviderIdentity:
    base = dict(
        provider="oidc",
        external_id="sub-1",
        email="alice@example.com",
        first_name="Alice",
        last_name="Smith",
        raw_claims={"email_verified": True},
    )
    base.update(over)
    return ProviderIdentity(**base)


@pytest.fixture()
def svc_and_events(db_session):
    events: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def _factory():
        # Reuse the test session; the fixture owns commit/rollback.
        yield db_session

    async def _outbox(session, event_type, payload):
        events.append((event_type, payload))

    svc = LocalIdentityService(
        session_factory=_factory,
        user_repo=user_repo,
        refresh_store_factory=lambda s: None,
        outbox_emit=_outbox,
        claims_resolver=None,
    )
    return svc, events


async def _seed_local_user(db_session, *, email, status="active"):
    user = await user_repo.create_user(
        db_session,
        email=email,
        password_hash="argon2-placeholder",
        first_name="Local",
        last_name="User",
        status=status,
    )
    await db_session.flush()
    return user


@pytest.mark.asyncio
async def test_provisions_new_subject(svc_and_events, db_session):
    svc, events = svc_and_events
    user, tokens = await svc.complete_sso_login(_identity())

    assert user.email == "alice@example.com"
    row = await user_repo.get_user_by_external_identity(db_session, "oidc", "sub-1")
    assert row is not None
    assert row.auth_provider == "oidc"
    assert row.status == "active"
    assert tokens.access_token and tokens.refresh_token
    types = [e[0] for e in events]
    assert "user.sso_provisioned" in types
    assert "user.logged_in" in types


@pytest.mark.asyncio
async def test_returning_subject_is_reused_not_reprovisioned(
    svc_and_events, db_session
):
    svc, events = svc_and_events
    existing = await user_repo.create_sso_user(
        db_session,
        email="bob@example.com",
        first_name="Bob",
        last_name="B",
        auth_provider="oidc",
        external_id="sub-9",
        password_hash="x",
    )
    await db_session.flush()

    user, _ = await svc.complete_sso_login(
        _identity(external_id="sub-9", email="bob@example.com")
    )
    assert user.id == existing.id
    types = [e[0] for e in events]
    assert "user.sso_provisioned" not in types
    assert "user.logged_in" in types


@pytest.mark.asyncio
async def test_safe_auto_link_to_verified_active_local_account(
    svc_and_events, db_session
):
    svc, events = svc_and_events
    local = await _seed_local_user(db_session, email="carol@example.com")
    old_hash = local.password_hash

    user, _ = await svc.complete_sso_login(
        _identity(external_id="sub-c", email="carol@example.com",
                  raw_claims={"email_verified": True})
    )

    assert user.id == local.id
    refreshed = await user_repo.get_user_by_id(db_session, local.id)
    assert refreshed.auth_provider == "oidc"
    assert refreshed.external_id == "sub-c"
    # Password login disabled — hash replaced.
    assert refreshed.password_hash != old_hash
    assert "user.sso_linked" in [e[0] for e in events]


@pytest.mark.asyncio
async def test_unsafe_link_unverified_email_is_denied_and_audited(
    svc_and_events, db_session
):
    svc, events = svc_and_events
    await _seed_local_user(db_session, email="dave@example.com")

    with pytest.raises(SSOAuthError):
        await svc.complete_sso_login(
            _identity(external_id="sub-d", email="dave@example.com",
                      raw_claims={"email_verified": False})
        )

    denied = [e for e in events if e[0] == "user.sso_link_denied"]
    assert denied and denied[0][1]["reason"] == "unsafe_auto_link"
    # No link occurred.
    row = await user_repo.get_user_by_external_identity(db_session, "oidc", "sub-d")
    assert row is None


@pytest.mark.asyncio
async def test_unsafe_link_inactive_local_account_is_denied(
    svc_and_events, db_session
):
    svc, events = svc_and_events
    await _seed_local_user(db_session, email="erin@example.com", status="pending")

    with pytest.raises(SSOAuthError):
        await svc.complete_sso_login(
            _identity(external_id="sub-e", email="erin@example.com",
                      raw_claims={"email_verified": True})
        )
    assert "user.sso_link_denied" in [e[0] for e in events]


@pytest.mark.asyncio
async def test_inactive_sso_account_is_rejected(svc_and_events, db_session):
    svc, _ = svc_and_events
    await user_repo.create_sso_user(
        db_session,
        email="frank@example.com",
        first_name="F",
        last_name="K",
        auth_provider="oidc",
        external_id="sub-f",
        password_hash="x",
    )
    # Suspend the provisioned account.
    u = await user_repo.get_user_by_external_identity(db_session, "oidc", "sub-f")
    u.status = "suspended"
    await db_session.flush()

    with pytest.raises(SSOAuthError):
        await svc.complete_sso_login(
            _identity(external_id="sub-f", email="frank@example.com")
        )
