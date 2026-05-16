"""
API endpoint tests for /api/v1/views/*.

Tests the view CRUD, visibility, favourite, soft-delete and restore endpoints
using the test_client fixture which overrides auth and DB session.
"""
from httpx import AsyncClient


# ── Helpers ────────────────────────────────────────────────────────────

async def _create_workspace(client: AsyncClient) -> str:
    """Create a workspace and return its ID (views require a workspace_id)."""
    resp = await client.post(
        "/api/v1/admin/workspaces",
        json={"name": "View Test WS", "dataSources": []},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _view_payload(workspace_id: str, name: str = "Test View", **overrides) -> dict:
    base = {
        "name": name,
        "workspaceId": workspace_id,
        "viewType": "graph",
        "config": {},
        "visibility": "private",
    }
    base.update(overrides)
    return base


async def _create_view(client: AsyncClient, workspace_id: str, name: str = "Test View", **kw) -> dict:
    resp = await client.post("/api/v1/views/", json=_view_payload(workspace_id, name, **kw))
    assert resp.status_code == 201
    return resp.json()


# ── GET /views (empty) ─────────────────────────────────────────────────

async def test_list_views_empty(test_client: AsyncClient):
    """Initially the view list is empty and envelope reports total=0."""
    resp = await test_client.get("/api/v1/views/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["hasMore"] is False
    assert body["nextOffset"] is None


# ── POST /views ────────────────────────────────────────────────────────

async def test_create_view(test_client: AsyncClient):
    """Create a view returns 201 with the created resource."""
    ws_id = await _create_workspace(test_client)
    body = await _create_view(test_client, ws_id, "My View")
    assert body["name"] == "My View"
    assert "id" in body
    assert body["workspaceId"] == ws_id
    assert body["viewType"] == "graph"
    assert body["visibility"] == "private"


async def test_create_view_with_optional_fields(test_client: AsyncClient):
    """Create a view with description and tags."""
    ws_id = await _create_workspace(test_client)
    body = await _create_view(
        test_client, ws_id, "Tagged View",
        description="A test view",
        tags=["tag1", "tag2"],
    )
    assert body["description"] == "A test view"
    assert body["tags"] == ["tag1", "tag2"]


async def test_create_view_missing_workspace_id(test_client: AsyncClient):
    """Creating a view without workspaceId fails with 422."""
    resp = await test_client.post(
        "/api/v1/views/",
        json={"name": "No WS", "viewType": "graph"},
    )
    assert resp.status_code == 422


# ── GET /views/{view_id} ──────────────────────────────────────────────

async def test_get_view(test_client: AsyncClient):
    """Fetch a created view by ID."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Fetch Me")
    view_id = created["id"]

    resp = await test_client.get(f"/api/v1/views/{view_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == view_id
    assert resp.json()["name"] == "Fetch Me"


async def test_get_view_not_found(test_client: AsyncClient):
    """Fetching a non-existent view returns 404."""
    resp = await test_client.get("/api/v1/views/view_nonexistent")
    assert resp.status_code == 404


# ── PUT /views/{view_id} ──────────────────────────────────────────────

async def test_update_view(test_client: AsyncClient):
    """Update view name and description."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Old Name")
    view_id = created["id"]

    resp = await test_client.put(
        f"/api/v1/views/{view_id}",
        json={"name": "New Name", "description": "Updated desc"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["description"] == "Updated desc"


async def test_update_view_not_found(test_client: AsyncClient):
    """Updating a non-existent view returns 404."""
    resp = await test_client.put(
        "/api/v1/views/view_ghost",
        json={"name": "Ghost"},
    )
    assert resp.status_code == 404


# ── DELETE /views/{view_id} (soft) ────────────────────────────────────

async def test_soft_delete_view(test_client: AsyncClient):
    """Soft-deleting a view returns 204."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Soft Delete Me")
    view_id = created["id"]

    resp = await test_client.delete(f"/api/v1/views/{view_id}")
    assert resp.status_code == 204


async def test_delete_view_not_found(test_client: AsyncClient):
    """Deleting a non-existent view returns 404."""
    resp = await test_client.delete("/api/v1/views/view_nope")
    assert resp.status_code == 404


# ── DELETE /views/{view_id}?permanent=true ─────────────────────────────

async def test_permanent_delete_view(test_client: AsyncClient):
    """Permanently deleting a view returns 204 and it is gone."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Perm Delete Me")
    view_id = created["id"]

    resp = await test_client.delete(f"/api/v1/views/{view_id}?permanent=true")
    assert resp.status_code == 204

    # View should be truly gone
    resp = await test_client.get(f"/api/v1/views/{view_id}")
    assert resp.status_code == 404


# ── POST /views/{view_id}/restore ──────────────────────────────────────

async def test_restore_soft_deleted_view(test_client: AsyncClient):
    """Restoring a soft-deleted view brings it back."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Restore Me")
    view_id = created["id"]

    # Soft-delete
    resp = await test_client.delete(f"/api/v1/views/{view_id}")
    assert resp.status_code == 204

    # Restore
    resp = await test_client.post(f"/api/v1/views/{view_id}/restore")
    assert resp.status_code == 200
    assert resp.json()["id"] == view_id
    assert resp.json()["name"] == "Restore Me"


async def test_restore_not_found(test_client: AsyncClient):
    """Restoring a non-existent view returns 404."""
    resp = await test_client.post("/api/v1/views/view_nope/restore")
    assert resp.status_code == 404


# ── PUT /views/{view_id}/visibility ───────────────────────────────────

async def test_update_visibility(test_client: AsyncClient):
    """Update view visibility to workspace."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Visibility Test")
    view_id = created["id"]

    resp = await test_client.put(
        f"/api/v1/views/{view_id}/visibility",
        json={"visibility": "workspace"},
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "workspace"


async def test_update_visibility_enterprise(test_client: AsyncClient):
    """Update view visibility to enterprise."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Enterprise Vis")
    view_id = created["id"]

    resp = await test_client.put(
        f"/api/v1/views/{view_id}/visibility",
        json={"visibility": "enterprise"},
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "enterprise"


async def test_update_visibility_invalid(test_client: AsyncClient):
    """Invalid visibility value returns 422. ``public`` is explicitly
    rejected because it was the old whitelist value before the rename to
    ``enterprise`` (migration 0006); guarding against accidental
    regression of the rename."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Bad Vis")
    view_id = created["id"]

    resp = await test_client.put(
        f"/api/v1/views/{view_id}/visibility",
        json={"visibility": "public"},
    )
    assert resp.status_code == 422


async def test_update_visibility_not_found(test_client: AsyncClient):
    """Updating visibility of non-existent view returns 404."""
    resp = await test_client.put(
        "/api/v1/views/view_nope/visibility",
        json={"visibility": "private"},
    )
    assert resp.status_code == 404


# ── POST /views/{view_id}/favourite ───────────────────────────────────

async def test_favourite_view(test_client: AsyncClient):
    """Favouriting a view returns 201."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Fave Me")
    view_id = created["id"]

    resp = await test_client.post(f"/api/v1/views/{view_id}/favourite")
    assert resp.status_code == 201
    body = resp.json()
    assert body["favourited"] is True


async def test_favourite_view_not_found(test_client: AsyncClient):
    """Favouriting a non-existent view returns 404."""
    resp = await test_client.post("/api/v1/views/view_nope/favourite")
    assert resp.status_code == 404


# ── DELETE /views/{view_id}/favourite ─────────────────────────────────

async def test_unfavourite_view(test_client: AsyncClient):
    """Unfavouriting a previously favourited view returns 204."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "Unfave Me")
    view_id = created["id"]

    # Favourite first
    resp = await test_client.post(f"/api/v1/views/{view_id}/favourite")
    assert resp.status_code == 201

    # Unfavourite
    resp = await test_client.delete(f"/api/v1/views/{view_id}/favourite")
    assert resp.status_code == 204


async def test_unfavourite_not_found(test_client: AsyncClient):
    """Unfavouriting when no favourite exists returns 404."""
    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "No Fave")
    view_id = created["id"]

    resp = await test_client.delete(f"/api/v1/views/{view_id}/favourite")
    assert resp.status_code == 404


# ── GET /views/popular ────────────────────────────────────────────────

async def test_list_popular_views_empty(test_client: AsyncClient):
    """Popular views returns empty list initially."""
    resp = await test_client.get("/api/v1/views/popular")
    assert resp.status_code == 200
    assert resp.json() == []


# ── GET /views/ — embedded ?include=popular (WS-5) ────────────────────

async def test_list_views_include_popular_omitted_by_default(test_client: AsyncClient):
    """Default response carries no ``popular`` payload — the field is
    only populated when the caller opts in. Keeps the common-case
    response small for callers that don't render a trending strip.
    """
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Plain List View")

    resp = await test_client.get("/api/v1/views/?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    # Either absent or explicitly null; never an empty array (that would
    # imply "we tried and found nothing" rather than "didn't ask").
    assert body.get("popular") is None


async def test_list_views_include_popular_returns_trending_strip(test_client: AsyncClient):
    """``?include=popular`` folds the trending strip into the same
    response, eliminating the Explorer's second round-trip. Visibility
    rules apply: zero-favourite views are excluded from popular even
    though they appear in ``items``.
    """
    ws_id = await _create_workspace(test_client)
    favoured = await _create_view(test_client, ws_id, "Trending", visibility="enterprise")
    await _create_view(test_client, ws_id, "Unloved", visibility="enterprise")

    # Bookmark one of the two so it qualifies as "popular" (fav_count > 0).
    fav_resp = await test_client.post(f"/api/v1/views/{favoured['id']}/favourite")
    assert fav_resp.status_code == 201

    resp = await test_client.get("/api/v1/views/?limit=10&include=popular&popularLimit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) >= 2
    assert isinstance(body.get("popular"), list)
    popular_ids = {v["id"] for v in body["popular"]}
    assert favoured["id"] in popular_ids


async def test_list_views_include_popular_respects_popular_limit(test_client: AsyncClient):
    """``popularLimit`` caps the embedded popular list independently of
    the main ``limit`` (which paginates ``items``).
    """
    ws_id = await _create_workspace(test_client)
    for i in range(4):
        created = await _create_view(
            test_client, ws_id, f"PopCap {i}", visibility="enterprise",
        )
        await test_client.post(f"/api/v1/views/{created['id']}/favourite")
        # Unfavourite/refavourite is awkward across the same user; just
        # leave each one with one favourite from the test admin user.

    resp = await test_client.get("/api/v1/views/?limit=10&include=popular&popularLimit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["popular"]) <= 2


# ── Filtering ─────────────────────────────────────────────────────────

async def test_list_views_filter_by_workspace(test_client: AsyncClient):
    """List views filtered by workspaceId."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "WS View")

    resp = await test_client.get(f"/api/v1/views/?workspaceId={ws_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) >= 1
    assert body["total"] >= 1
    assert all(v["workspaceId"] == ws_id for v in body["items"])


async def test_list_views_with_search(test_client: AsyncClient):
    """List views with search query returns matching items."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Unique Search Name XYZ")

    resp = await test_client.get("/api/v1/views/?search=Unique Search Name XYZ")
    assert resp.status_code == 200
    body = resp.json()
    assert any("Unique Search Name" in v["name"] for v in body["items"])


async def test_list_views_pagination(test_client: AsyncClient):
    """List views with limit and offset returns a properly-populated envelope."""
    ws_id = await _create_workspace(test_client)
    for i in range(3):
        await _create_view(test_client, ws_id, f"Page View {i}")

    resp = await test_client.get("/api/v1/views/?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 3
    assert body["hasMore"] is True
    assert body["nextOffset"] == 2

    # Fetch the next page using the server-advertised offset.
    resp = await test_client.get(f"/api/v1/views/?limit=2&offset={body['nextOffset']}")
    assert resp.status_code == 200
    page2 = resp.json()
    assert page2["total"] == body["total"]


# ── Full CRUD round-trip ──────────────────────────────────────────────

async def test_view_crud_roundtrip(test_client: AsyncClient):
    """Full create -> read -> update -> delete -> restore cycle."""
    ws_id = await _create_workspace(test_client)

    # Create
    created = await _create_view(test_client, ws_id, "Roundtrip")
    view_id = created["id"]

    # Read
    r = await test_client.get(f"/api/v1/views/{view_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "Roundtrip"

    # Update
    r = await test_client.put(
        f"/api/v1/views/{view_id}",
        json={"name": "Roundtrip Updated"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Roundtrip Updated"

    # Soft delete
    r = await test_client.delete(f"/api/v1/views/{view_id}")
    assert r.status_code == 204

    # Restore
    r = await test_client.post(f"/api/v1/views/{view_id}/restore")
    assert r.status_code == 200
    assert r.json()["name"] == "Roundtrip Updated"

    # Permanent delete
    r = await test_client.delete(f"/api/v1/views/{view_id}?permanent=true")
    assert r.status_code == 204

    # Gone
    r = await test_client.get(f"/api/v1/views/{view_id}")
    assert r.status_code == 404


# ── created_by attribution ────────────────────────────────────────────

async def test_create_view_records_created_by(test_client: AsyncClient, fake_user):
    """POST /views records the authenticated user as created_by."""
    ws_id = await _create_workspace(test_client)
    body = await _create_view(test_client, ws_id, "Attributed View")
    assert body["createdBy"] == fake_user.id


async def test_list_views_filter_by_created_by(test_client: AsyncClient, fake_user):
    """createdBy filter returns only views authored by the given user."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Mine 1")
    await _create_view(test_client, ws_id, "Mine 2")

    resp = await test_client.get(f"/api/v1/views/?createdBy={fake_user.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    assert all(v["createdBy"] == fake_user.id for v in body["items"])

    # A different user id filter yields no results.
    resp = await test_client.get("/api/v1/views/?createdBy=usr_nobody")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ── workspaceIds (multi) ──────────────────────────────────────────────

async def test_list_views_filter_by_multi_workspaces(test_client: AsyncClient):
    """workspaceIds returns the union across multiple workspaces."""
    ws_a = await _create_workspace(test_client)
    ws_b = await _create_workspace(test_client)
    ws_c = await _create_workspace(test_client)
    await _create_view(test_client, ws_a, "In A")
    await _create_view(test_client, ws_b, "In B")
    await _create_view(test_client, ws_c, "In C")

    resp = await test_client.get(
        f"/api/v1/views/?workspaceIds={ws_a}&workspaceIds={ws_b}"
    )
    assert resp.status_code == 200
    body = resp.json()
    ws_ids = {v["workspaceId"] for v in body["items"]}
    assert ws_a in ws_ids
    assert ws_b in ws_ids
    assert ws_c not in ws_ids


async def test_workspace_ids_wins_over_workspace_id(test_client: AsyncClient):
    """When both workspaceId and workspaceIds are sent, the multi-value param wins."""
    ws_a = await _create_workspace(test_client)
    ws_b = await _create_workspace(test_client)
    await _create_view(test_client, ws_a, "A")
    await _create_view(test_client, ws_b, "B")

    resp = await test_client.get(
        f"/api/v1/views/?workspaceId={ws_a}&workspaceIds={ws_b}"
    )
    assert resp.status_code == 200
    ws_ids = {v["workspaceId"] for v in resp.json()["items"]}
    assert ws_ids == {ws_b}


# ── createdAfter ──────────────────────────────────────────────────────

async def test_list_views_filter_by_created_after(test_client: AsyncClient):
    """createdAfter returns views whose created_at >= cutoff."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Recent")

    # Past cutoff — should match.
    resp = await test_client.get(
        "/api/v1/views/?createdAfter=2000-01-01T00:00:00Z"
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    # Far-future cutoff — should not match anything.
    resp = await test_client.get(
        "/api/v1/views/?createdAfter=2999-01-01T00:00:00Z"
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ── visibilityIn ──────────────────────────────────────────────────────

async def test_list_views_filter_by_visibility_in(test_client: AsyncClient):
    """visibilityIn returns the union of the specified visibilities."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Priv", visibility="private")
    await _create_view(test_client, ws_id, "Work", visibility="workspace")
    await _create_view(test_client, ws_id, "Ent", visibility="enterprise")

    resp = await test_client.get(
        "/api/v1/views/?visibilityIn=workspace&visibilityIn=enterprise"
    )
    assert resp.status_code == 200
    body = resp.json()
    visibilities = {v["visibility"] for v in body["items"]}
    assert visibilities == {"workspace", "enterprise"}


# ── Popular view privacy scope ────────────────────────────────────────

async def test_list_popular_views_includes_non_private_with_favs(test_client: AsyncClient):
    """Workspace- and enterprise-visible favourited views appear in popular."""
    ws_id = await _create_workspace(test_client)
    w = await _create_view(test_client, ws_id, "WS-Vis", visibility="workspace")
    e = await _create_view(test_client, ws_id, "Ent-Vis", visibility="enterprise")

    for v in (w, e):
        resp = await test_client.post(f"/api/v1/views/{v['id']}/favourite")
        assert resp.status_code == 201

    resp = await test_client.get("/api/v1/views/popular")
    assert resp.status_code == 200
    ids = {v["id"] for v in resp.json()}
    assert w["id"] in ids
    assert e["id"] in ids


async def test_list_popular_views_owner_sees_own_private(test_client: AsyncClient):
    """A private view surfaces in popular for its creator when favourited."""
    ws_id = await _create_workspace(test_client)
    priv = await _create_view(test_client, ws_id, "Secret", visibility="private")

    resp = await test_client.post(f"/api/v1/views/{priv['id']}/favourite")
    assert resp.status_code == 201

    resp = await test_client.get("/api/v1/views/popular")
    assert resp.status_code == 200
    assert priv["id"] in {v["id"] for v in resp.json()}


async def test_list_popular_views_excludes_others_private(
    test_client: AsyncClient, db_session
):
    """A private view belonging to someone else never appears in popular."""
    from sqlalchemy import update as _sa_update
    from backend.app.db.models import ViewORM as _V

    ws_id = await _create_workspace(test_client)
    created = await _create_view(test_client, ws_id, "NotMine", visibility="private")
    vid = created["id"]

    # Favourite to qualify for popular.
    resp = await test_client.post(f"/api/v1/views/{vid}/favourite")
    assert resp.status_code == 201

    # Reattribute to a different creator directly via the DB so we can
    # simulate user B trying to see user A's private favourited view.
    await db_session.execute(
        _sa_update(_V).where(_V.id == vid).values(created_by="usr_other")
    )
    await db_session.commit()

    resp = await test_client.get("/api/v1/views/popular")
    assert resp.status_code == 200
    assert vid not in {v["id"] for v in resp.json()}


async def test_list_popular_views_excludes_zero_fav(test_client: AsyncClient):
    """Views with zero favourites are excluded from popular."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Unloved", visibility="enterprise")

    resp = await test_client.get("/api/v1/views/popular")
    assert resp.status_code == 200
    assert resp.json() == []


# ── viewType / viewTypes filter ───────────────────────────────────────

async def test_list_views_filter_by_view_type(test_client: AsyncClient):
    """viewType filter returns only views of the given type."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "G", viewType="graph")
    await _create_view(test_client, ws_id, "H", viewType="hierarchy")
    await _create_view(test_client, ws_id, "T", viewType="table")

    resp = await test_client.get("/api/v1/views/?viewType=hierarchy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["viewType"] == "hierarchy"


async def test_list_views_filter_by_view_types_multi(test_client: AsyncClient):
    """viewTypes returns the union of supplied types and wins over single viewType."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "G", viewType="graph")
    await _create_view(test_client, ws_id, "H", viewType="hierarchy")
    await _create_view(test_client, ws_id, "T", viewType="table")

    resp = await test_client.get(
        "/api/v1/views/?viewTypes=graph&viewTypes=table&viewType=hierarchy"
    )
    assert resp.status_code == 200
    types = {v["viewType"] for v in resp.json()["items"]}
    # Multi wins over single; hierarchy is excluded.
    assert types == {"graph", "table"}


# ── createdByIn filter ────────────────────────────────────────────────

async def test_list_views_filter_by_created_by_in(test_client: AsyncClient, fake_user, db_session):
    """createdByIn returns the union of creators."""
    from sqlalchemy import update as _sa_update
    from backend.app.db.models import ViewORM as _V

    ws_id = await _create_workspace(test_client)
    mine = await _create_view(test_client, ws_id, "Mine")
    other = await _create_view(test_client, ws_id, "Other")

    # Reattribute "other" to a synthetic user id.
    await db_session.execute(
        _sa_update(_V).where(_V.id == other["id"]).values(created_by="usr_other")
    )
    await db_session.commit()

    resp = await test_client.get(
        f"/api/v1/views/?createdByIn={fake_user.id}&createdByIn=usr_other"
    )
    assert resp.status_code == 200
    ids = {v["id"] for v in resp.json()["items"]}
    assert mine["id"] in ids
    assert other["id"] in ids


# ── Tag filter (SQL) ──────────────────────────────────────────────────

async def test_list_views_filter_by_tags(test_client: AsyncClient):
    """tags returns views whose JSON tag array contains any of the supplied tags."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Fin", tags=["finance", "pii"])
    await _create_view(test_client, ws_id, "Eng", tags=["engineering"])
    await _create_view(test_client, ws_id, "Both", tags=["engineering", "finance"])
    await _create_view(test_client, ws_id, "None")

    resp = await test_client.get("/api/v1/views/?tags=finance")
    assert resp.status_code == 200
    body = resp.json()
    names = {v["name"] for v in body["items"]}
    # Total must be SQL-accurate, not "all views minus post-filtered".
    assert body["total"] == 2
    assert names == {"Fin", "Both"}


async def test_list_views_filter_by_tags_multi_is_or(test_client: AsyncClient):
    """Multiple tags use OR semantics (match if any tag is present)."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Fin", tags=["finance"])
    await _create_view(test_client, ws_id, "Eng", tags=["engineering"])
    await _create_view(test_client, ws_id, "None")

    resp = await test_client.get("/api/v1/views/?tags=finance&tags=engineering")
    assert resp.status_code == 200
    names = {v["name"] for v in resp.json()["items"]}
    assert names == {"Fin", "Eng"}


# ── GET /views/facets ─────────────────────────────────────────────────

async def test_facets_empty(test_client: AsyncClient):
    """Facets endpoint returns empty lists when no views exist."""
    resp = await test_client.get("/api/v1/views/facets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tags"] == []
    assert body["viewTypes"] == []
    assert body["creators"] == []


# ── GET /views/stats ───────────────────────────────────────────────────

async def test_stats_empty(test_client: AsyncClient):
    """Stats endpoint returns zero counts when no views exist."""
    resp = await test_client.get("/api/v1/views/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["recentlyAdded"] == 0
    assert body["needsAttention"] == 0
    assert body["lastActivityAt"] is None


async def test_stats_populated(test_client: AsyncClient):
    """Stats reflect the catalog: total count, recent count, last activity."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "A")
    await _create_view(test_client, ws_id, "B")
    await _create_view(test_client, ws_id, "C")

    resp = await test_client.get("/api/v1/views/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    # All three views were just created so recently_added should cover them.
    assert body["recentlyAdded"] == 3
    assert body["lastActivityAt"] is not None


async def test_stats_respect_workspace_filter(test_client: AsyncClient):
    """Stats narrow when a workspace filter is applied — core of the ask."""
    ws_a = await _create_workspace(test_client)
    ws_b = await _create_workspace(test_client)
    await _create_view(test_client, ws_a, "A1")
    await _create_view(test_client, ws_a, "A2")
    await _create_view(test_client, ws_b, "B1")

    # Global: all 3
    resp = await test_client.get("/api/v1/views/stats")
    assert resp.json()["total"] == 3

    # Scoped to workspace A: 2
    resp = await test_client.get(f"/api/v1/views/stats?workspaceId={ws_a}")
    assert resp.json()["total"] == 2

    # Multi-workspace: union of A + B = 3
    resp = await test_client.get(
        f"/api/v1/views/stats?workspaceIds={ws_a}&workspaceIds={ws_b}"
    )
    assert resp.json()["total"] == 3


async def test_stats_respect_search_filter(test_client: AsyncClient):
    """Search filter scopes the stats numbers."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "Finance Dashboard")
    await _create_view(test_client, ws_id, "Engineering Dashboard")
    await _create_view(test_client, ws_id, "Finance Reports")

    resp = await test_client.get("/api/v1/views/stats?search=Finance")
    assert resp.json()["total"] == 2


async def test_stats_respect_view_type_filter(test_client: AsyncClient):
    """viewType filter scopes stats to that type."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "G", viewType="graph")
    await _create_view(test_client, ws_id, "H", viewType="hierarchy")
    await _create_view(test_client, ws_id, "T", viewType="table")

    resp = await test_client.get("/api/v1/views/stats?viewType=graph")
    assert resp.json()["total"] == 1


async def test_stats_attention_only_matches_category(test_client: AsyncClient):
    """attentionOnly returns the same count as the Attention category list filter."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "A")
    await _create_view(test_client, ws_id, "B")

    resp_stats = await test_client.get("/api/v1/views/stats?attentionOnly=true")
    resp_list = await test_client.get("/api/v1/views/?attentionOnly=true")
    assert resp_stats.status_code == 200
    assert resp_list.status_code == 200
    # The list "total" and the stats "total" must agree.
    assert resp_stats.json()["total"] == resp_list.json()["total"]


async def test_facets_tags_view_types_creators(test_client: AsyncClient, fake_user):
    """Facets aggregate tags, view types, and creators with counts."""
    ws_id = await _create_workspace(test_client)
    await _create_view(test_client, ws_id, "A", viewType="graph", tags=["finance", "pii"])
    await _create_view(test_client, ws_id, "B", viewType="graph", tags=["finance"])
    await _create_view(test_client, ws_id, "C", viewType="hierarchy")

    resp = await test_client.get("/api/v1/views/facets")
    assert resp.status_code == 200
    body = resp.json()

    # Tags sorted by count desc, then alpha.
    tag_map = {t["value"]: t["count"] for t in body["tags"]}
    assert tag_map == {"finance": 2, "pii": 1}

    # View types sorted by count desc.
    vt_map = {t["value"]: t["count"] for t in body["viewTypes"]}
    assert vt_map == {"graph": 2, "hierarchy": 1}

    # Creators — single fake_user authored all three.
    assert len(body["creators"]) == 1
    creator = body["creators"][0]
    assert creator["userId"] == fake_user.id
    assert creator["count"] == 3
    assert creator["displayName"] == f"{fake_user.first_name} {fake_user.last_name}"


async def test_facets_ignores_soft_deleted_views(test_client: AsyncClient):
    """Facets exclude soft-deleted views so dropdowns don't surface ghost values."""
    ws_id = await _create_workspace(test_client)
    alive = await _create_view(test_client, ws_id, "Alive", tags=["keep"])
    dead = await _create_view(test_client, ws_id, "Dead", tags=["gone"])

    # Soft-delete the "Dead" view.
    r = await test_client.delete(f"/api/v1/views/{dead['id']}")
    assert r.status_code == 204

    resp = await test_client.get("/api/v1/views/facets")
    assert resp.status_code == 200
    tag_values = {t["value"] for t in resp.json()["tags"]}
    assert "keep" in tag_values
    assert "gone" not in tag_values
    # Silence unused-var lint.
    assert alive["id"]
