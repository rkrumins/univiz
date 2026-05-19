"""Unit tests for ``backend.app.services.revocation_service``.

Uses the in-memory backend so tests run without a live Redis. The
real backend is exercised in the integration test suite once Phase 2
wires it into endpoints.
"""
from __future__ import annotations

import pytest

from backend.app.services.revocation_service import (
    InMemoryBackend,
    RevocationBackendError,
    RevocationService,
    _key,
    _user_sids_key,
)


@pytest.mark.asyncio
async def test_revoke_round_trip():
    svc = RevocationService(InMemoryBackend())
    assert not await svc.is_revoked("sess_a")
    await svc.revoke_session("sess_a")
    assert await svc.is_revoked("sess_a")


@pytest.mark.asyncio
async def test_revoke_session_ignores_empty_sid():
    svc = RevocationService(InMemoryBackend())
    await svc.revoke_session("")
    # ``is_revoked`` short-circuits to False on empty sid even if the
    # backend somehow has the empty key.
    assert not await svc.is_revoked("")


@pytest.mark.asyncio
async def test_revoke_sessions_bulk():
    svc = RevocationService(InMemoryBackend())
    await svc.revoke_sessions(["sess_a", "sess_b", "sess_c"])
    assert await svc.is_revoked("sess_a")
    assert await svc.is_revoked("sess_b")
    assert await svc.is_revoked("sess_c")


@pytest.mark.asyncio
async def test_health_passes_with_in_memory_backend():
    svc = RevocationService(InMemoryBackend())
    assert await svc.health() is True


@pytest.mark.asyncio
async def test_revocation_backend_error_propagates():
    """A backend that raises ``RevocationBackendError`` from ``exists``
    bubbles up so ``requires(...)`` can apply its fail-open / fail-closed
    policy."""
    class BrokenBackend(InMemoryBackend):
        async def exists(self, key):
            raise RevocationBackendError("simulated outage")

    svc = RevocationService(BrokenBackend())
    with pytest.raises(RevocationBackendError):
        await svc.is_revoked("sess_x")


def test_key_prefix_is_stable():
    assert _key("sess_x") == "rbac:revoked:sess_x"


def test_user_sids_key_prefix_is_stable():
    assert _user_sids_key("usr_1") == "rbac:user_sids:usr_1"


@pytest.mark.asyncio
async def test_revoke_all_user_sessions_kills_recorded_sids():
    svc = RevocationService(InMemoryBackend())
    await svc.record_session("usr_1", "sess_a")
    await svc.record_session("usr_1", "sess_b")
    # A different user's session must be untouched.
    await svc.record_session("usr_2", "sess_z")

    await svc.revoke_all_user_sessions("usr_1")

    assert await svc.is_revoked("sess_a")
    assert await svc.is_revoked("sess_b")
    assert not await svc.is_revoked("sess_z")


@pytest.mark.asyncio
async def test_revoke_all_user_sessions_clears_index():
    """After a coarse revoke the index is dropped, so a fresh login's
    sid is not retro-revoked by a second revoke call."""
    svc = RevocationService(InMemoryBackend())
    await svc.record_session("usr_1", "sess_old")
    await svc.revoke_all_user_sessions("usr_1")

    await svc.record_session("usr_1", "sess_new")
    await svc.revoke_all_user_sessions("usr_1")
    assert await svc.is_revoked("sess_new")
    # Second revoke only saw the post-login sid; total revoked sids = 2.


@pytest.mark.asyncio
async def test_record_session_ignores_empty_args():
    svc = RevocationService(InMemoryBackend())
    await svc.record_session("", "sess_a")
    await svc.record_session("usr_1", "")
    await svc.revoke_all_user_sessions("usr_1")
    assert not await svc.is_revoked("sess_a")


@pytest.mark.asyncio
async def test_revoke_all_user_sessions_noop_for_unknown_user():
    svc = RevocationService(InMemoryBackend())
    await svc.revoke_all_user_sessions("nobody")  # must not raise
