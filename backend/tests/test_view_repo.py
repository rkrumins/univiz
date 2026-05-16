"""
Tests for backend.app.db.repositories.view_repo.
"""
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.repositories import view_repo
from backend.app.db.models import WorkspaceORM, UserORM
from backend.common.models.management import (
    ViewCreateRequest,
    ViewUpdateRequest,
    ViewResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_workspace(session: AsyncSession, name: str = "Test WS") -> WorkspaceORM:
    """Insert a workspace prereq and return the ORM row."""
    ws = WorkspaceORM(name=name)
    session.add(ws)
    await session.flush()
    return ws


def _make_create_req(workspace_id: str, **overrides) -> ViewCreateRequest:
    defaults = dict(
        name="Test View",
        workspace_id=workspace_id,
        view_type="graph",
        visibility="private",
    )
    defaults.update(overrides)
    return ViewCreateRequest(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_create_view_returns_response(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    req = _make_create_req(ws.id)
    resp = await view_repo.create_view(db_session, req)

    assert isinstance(resp, ViewResponse)
    assert resp.name == "Test View"
    assert resp.visibility == "private"
    assert resp.view_type == "graph"


async def test_get_view(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    fetched = await view_repo.get_view(db_session, created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == created.name


async def test_get_view_missing(db_session: AsyncSession):
    result = await view_repo.get_view(db_session, "nonexistent")
    assert result is None


async def test_update_view(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    update_req = ViewUpdateRequest(name="Renamed View", visibility="enterprise")
    updated = await view_repo.update_view(db_session, created.id, update_req)

    assert updated is not None
    assert updated.name == "Renamed View"
    assert updated.visibility == "enterprise"


async def test_update_view_missing(db_session: AsyncSession):
    update_req = ViewUpdateRequest(name="Does Not Exist")
    result = await view_repo.update_view(db_session, "nonexistent", update_req)
    assert result is None


async def test_delete_view_soft_deletes(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    result = await view_repo.delete_view(db_session, created.id)
    assert result is True

    # Soft-deleted: still retrievable via get (no deleted_at filter in get_view)
    fetched = await view_repo.get_view(db_session, created.id)
    assert fetched is not None
    assert fetched.deleted_at is not None

    # Not included in default filtered listing
    listed = await view_repo.list_views_filtered(db_session)
    assert listed.total == 0
    assert listed.items == []


async def test_restore_view(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    await view_repo.delete_view(db_session, created.id)
    result = await view_repo.restore_view(db_session, created.id)
    assert result is True

    # Now shows up in listing
    listed = await view_repo.list_views_filtered(db_session)
    assert listed.total == 1
    assert len(listed.items) == 1


async def test_restore_non_deleted_returns_false(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    # View is not deleted, restore should return False
    result = await view_repo.restore_view(db_session, created.id)
    assert result is False


async def test_list_views_filtered_by_visibility(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    await view_repo.create_view(
        db_session, _make_create_req(ws.id, name="Private", visibility="private")
    )
    await view_repo.create_view(
        db_session, _make_create_req(ws.id, name="Enterprise", visibility="enterprise")
    )

    private_views = await view_repo.list_views_filtered(
        db_session, visibility="private"
    )
    assert private_views.total == 1
    assert private_views.items[0].name == "Private"

    enterprise_views = await view_repo.list_views_filtered(
        db_session, visibility="enterprise"
    )
    assert enterprise_views.total == 1
    assert enterprise_views.items[0].name == "Enterprise"


async def test_favourite_view(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    result = await view_repo.favourite_view(db_session, created.id, "user_1")
    assert result is True


async def test_unfavourite_view(db_session: AsyncSession):
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    await view_repo.favourite_view(db_session, created.id, "user_1")
    result = await view_repo.unfavourite_view(db_session, created.id, "user_1")
    assert result is True

    # Unfavouriting again returns False
    result2 = await view_repo.unfavourite_view(db_session, created.id, "user_1")
    assert result2 is False


async def test_favourite_view_is_idempotent(db_session: AsyncSession):
    """Second favourite call for the same user returns False (already favourited)."""
    ws = await _create_workspace(db_session)
    created = await view_repo.create_view(db_session, _make_create_req(ws.id))

    first = await view_repo.favourite_view(db_session, created.id, "user_1")
    assert first is True

    second = await view_repo.favourite_view(db_session, created.id, "user_1")
    assert second is False


# ---------------------------------------------------------------------------
# Batched enrichment (WS-1) — kills the per-row N+1
# ---------------------------------------------------------------------------

@contextmanager
def _count_queries(session: AsyncSession):
    """Count cursor executions on the session's underlying engine.

    Uses the sync engine's ``before_cursor_execute`` hook (the canonical
    SQLAlchemy mechanism). Filters out savepoint/transaction control
    statements so the count reflects real SELECTs / DMLs only.
    """
    counter = {"n": 0}
    engine = session.bind.sync_engine

    def _on_exec(conn, cursor, statement, params, context, executemany):
        stmt = statement.strip().upper()
        if stmt.startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK", "BEGIN", "COMMIT")):
            return
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _on_exec)


async def _seed_views(session: AsyncSession, ws: WorkspaceORM, n: int, creator_id: str | None = None):
    """Seed ``n`` views under ``ws`` with one creator and no datasource/context-model.

    The shape is what list_views_filtered emits in the common Explorer case:
    workspace set, datasource None, context_model None, creator set when
    a logged-in user owns the view. Keeps the prerequisite surface tight.
    """
    if creator_id:
        # Insert the creator user once so the batched users-lookup has
        # something to find. Without this the user_map stays empty
        # (creator name resolves to None) — still correct, just less
        # representative of the real workload.
        session.add(UserORM(
            id=creator_id,
            email=f"{creator_id}@example.com",
            password_hash="x",
            first_name="Test",
            last_name="Creator",
            status="active",
            auth_provider="local",
        ))
        await session.flush()
    for i in range(n):
        await view_repo.create_view(
            session,
            _make_create_req(ws.id, name=f"v{i}", visibility="workspace"),
            user_id=creator_id,
        )


async def test_list_views_filtered_query_count_is_bounded(db_session: AsyncSession):
    """N+1 kill switch: query count is bounded and independent of row count.

    Before WS-1: list_views_filtered with limit=20 issued ~1 (select) + 1
    (count) + 6×N (per-row enrichment) ≈ 122 queries. After WS-1, the
    enrichment is batched into at most 4 lookups (workspaces, datasources
    when present, context_models when present, users when present,
    favourite counts when view_ids non-empty, favourites-by-user when a
    user is supplied). With ds/cm/user/favs all populated that's 6 total;
    asserted ceiling here is 10 to leave slack for paging/order-by
    side-effects from the dialect.
    """
    ws = await _create_workspace(db_session)
    creator_id = "usr_creator_001"
    await _seed_views(db_session, ws, n=20, creator_id=creator_id)
    await db_session.commit()

    with _count_queries(db_session) as counter:
        listed = await view_repo.list_views_filtered(
            db_session, limit=20, user_id=creator_id,
        )

    assert listed.total == 20
    assert len(listed.items) == 20
    # If we ever regress to per-row enrichment, this will explode to ~120+.
    # 10 is a comfortable ceiling for the batched path on the SQLite test
    # backend; production Postgres should hit ~6.
    assert counter["n"] <= 10, (
        f"list_views_filtered issued {counter['n']} queries for 20 rows; "
        f"expected <=10 with batched enrichment. N+1 regression?"
    )


async def test_list_views_filtered_legacy_kill_switch(db_session: AsyncSession, monkeypatch):
    """VIEWS_BATCH_ENRICH=false falls back to the legacy per-row path.

    Verifies that the kill switch actually disables the new code path so
    we can roll back in seconds if the batched path is found to be
    misbehaving in prod. The legacy path is intentionally chatty, so we
    assert the query count explodes past the batched-path ceiling.
    """
    ws = await _create_workspace(db_session)
    creator_id = "usr_creator_legacy"
    # Pass a creator so the per-row UserORM lookup fires; pass a user_id
    # to list_views_filtered so the per-row favourite check fires too.
    # Without these, legacy short-circuits 4 of 6 helpers per row and the
    # query count is too close to the batched ceiling to distinguish.
    await _seed_views(db_session, ws, n=10, creator_id=creator_id)
    await db_session.commit()

    monkeypatch.setenv("VIEWS_BATCH_ENRICH", "false")

    with _count_queries(db_session) as counter:
        listed = await view_repo.list_views_filtered(
            db_session, limit=10, user_id=creator_id,
        )

    assert listed.total == 10
    # Legacy path with workspace + creator + user_id supplied:
    # ~1 select + 1 count + 4 awaits per row × 10 rows ≈ 42 queries.
    # The batched path is bounded under 10, so 30 is a safe separator.
    assert counter["n"] > 30, (
        f"Legacy path issued {counter['n']} queries; expected >30. "
        f"VIEWS_BATCH_ENRICH switch may be broken."
    )


async def test_batched_enrichment_matches_legacy_output(db_session: AsyncSession, monkeypatch):
    """The new batched path produces identical ViewResponses to the legacy path.

    Critical correctness check: WS-1 changes how the data is fetched but
    not *what* is returned. Compare item-by-item, ignoring volatile
    timestamps that the seed flow may slightly differ on between calls.
    """
    ws = await _create_workspace(db_session)
    creator_id = "usr_creator_002"
    await _seed_views(db_session, ws, n=5, creator_id=creator_id)
    # Add a couple of favourites so the favourite_count / is_favourited
    # paths actually carry signal in the comparison.
    listed = await view_repo.list_views_filtered(db_session, limit=5, user_id=creator_id)
    view_ids = [v.id for v in listed.items]
    await view_repo.favourite_view(db_session, view_ids[0], creator_id)
    await view_repo.favourite_view(db_session, view_ids[1], "usr_other")
    await db_session.commit()

    monkeypatch.setenv("VIEWS_BATCH_ENRICH", "true")
    batched = await view_repo.list_views_filtered(db_session, limit=5, user_id=creator_id)

    monkeypatch.setenv("VIEWS_BATCH_ENRICH", "false")
    legacy = await view_repo.list_views_filtered(db_session, limit=5, user_id=creator_id)

    assert batched.total == legacy.total
    assert len(batched.items) == len(legacy.items)

    def _key_fields(v: ViewResponse) -> dict:
        return {
            "id": v.id,
            "name": v.name,
            "workspace_name": v.workspace_name,
            "created_by_name": v.created_by_name,
            "created_by_email": v.created_by_email,
            "favourite_count": v.favourite_count,
            "is_favourited": v.is_favourited,
            "visibility": v.visibility,
        }

    by_id_batched = {v.id: _key_fields(v) for v in batched.items}
    by_id_legacy = {v.id: _key_fields(v) for v in legacy.items}
    assert by_id_batched == by_id_legacy


async def test_list_popular_views_query_count_is_bounded(db_session: AsyncSession):
    """list_popular_views is the second N+1 we fixed; pin its query count too.

    Before WS-1: 1 (popular query with fav_count JOIN) + 5×N awaits. After
    WS-1: 1 + ≤5 batched lookups; favourite counts are reused from the
    JOIN'd subquery so the GROUP BY query is skipped entirely.
    """
    ws = await _create_workspace(db_session)
    creator_id = "usr_creator_003"
    await _seed_views(db_session, ws, n=8, creator_id=creator_id)
    listed = await view_repo.list_views_filtered(db_session, limit=8, user_id=creator_id)
    # Give every view at least one favourite so they all qualify for
    # the "popular" set (popularity = fav_count > 0).
    for v in listed.items:
        await view_repo.favourite_view(db_session, v.id, "usr_voter")
    await db_session.commit()

    with _count_queries(db_session) as counter:
        popular = await view_repo.list_popular_views(
            db_session, limit=10, user_id=creator_id,
        )

    assert len(popular) == 8
    assert counter["n"] <= 8, (
        f"list_popular_views issued {counter['n']} queries for 8 rows; "
        f"expected <=8 with batched enrichment. N+1 regression?"
    )
