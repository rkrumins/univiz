"""
API endpoint tests for /api/v1/announcements/* (public) and /api/v1/admin/announcements/* (admin).

Tests announcement CRUD, config, and public listing endpoints
using the test_client fixture which overrides auth and DB session.
"""
from httpx import AsyncClient


# ── Helpers ────────────────────────────────────────────────────────────

def _announcement_payload(title: str = "Test Announcement", **overrides) -> dict:
    base = {
        "title": title,
        "message": "This is a test announcement.",
        "bannerType": "info",
        "isActive": True,
        "snoozeDurationMinutes": 0,
    }
    base.update(overrides)
    return base


async def _create_announcement(client: AsyncClient, title: str = "Test Announcement", **kw) -> dict:
    resp = await client.post(
        "/api/v1/admin/announcements",
        json=_announcement_payload(title, **kw),
    )
    assert resp.status_code == 201
    return resp.json()


# ── Public: GET /announcements ────────────────────────────────────────

async def test_list_active_announcements_empty(test_client: AsyncClient):
    """Initially the active announcements list is empty."""
    resp = await test_client.get("/api/v1/announcements")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_active_announcements_returns_active_only(test_client: AsyncClient):
    """After creating active and inactive announcements, only active ones are returned."""
    await _create_announcement(test_client, "Active Ann", isActive=True)
    await _create_announcement(test_client, "Inactive Ann", isActive=False)

    resp = await test_client.get("/api/v1/announcements")
    assert resp.status_code == 200
    # Active announcements should be present
    names = [a["title"] for a in resp.json()]
    assert "Active Ann" in names


# ── ETag / 304 revalidation (WS-2) ─────────────────────────────────────

async def test_announcements_sets_etag_header(test_client: AsyncClient):
    """Every 200 response from /announcements carries an ETag so polling
    clients have something to revalidate against. Without this the
    polling-stampede mitigation in WS-6 can't short-circuit."""
    resp = await test_client.get("/api/v1/announcements")
    assert resp.status_code == 200
    assert resp.headers.get("etag"), "missing ETag header on /announcements"
    # Quoted strong-validator form per RFC 7232.
    assert resp.headers["etag"].startswith('"') and resp.headers["etag"].endswith('"')


async def test_announcements_returns_304_on_matching_if_none_match(test_client: AsyncClient):
    """A second fetch with If-None-Match equal to the previous ETag
    must return 304 with no body. This is the bandwidth saving — at
    1000 users polling every 15s, ~95% of requests hit this path in
    steady state."""
    first = await test_client.get("/api/v1/announcements")
    assert first.status_code == 200
    etag = first.headers["etag"]

    second = await test_client.get(
        "/api/v1/announcements",
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304
    # 304 must not include an entity body (RFC 7232 §4.1).
    assert second.content == b""
    # The ETag is echoed so intermediaries can cache the validator.
    assert second.headers.get("etag") == etag


async def test_announcements_etag_flips_on_create(test_client: AsyncClient):
    """The ETag must change whenever the active-set materially changes.
    Otherwise clients would keep getting 304s and never see new
    announcements — exactly the cache-poisoning class of bug."""
    first = await test_client.get("/api/v1/announcements")
    first_etag = first.headers["etag"]

    await _create_announcement(test_client, "Fresh Banner", isActive=True)

    second = await test_client.get("/api/v1/announcements")
    second_etag = second.headers["etag"]
    assert second_etag != first_etag, (
        "ETag did not change after a new active announcement was added "
        "— clients would never revalidate to see new banners."
    )


async def test_announcements_304_with_stale_etag(test_client: AsyncClient):
    """An obsolete If-None-Match must fall through to a 200 with the
    current ETag — guarding against a 304 cache-poisoning bug where
    we always 304 regardless of which tag the client sent."""
    await _create_announcement(test_client, "Current", isActive=True)
    current = await test_client.get("/api/v1/announcements")
    current_etag = current.headers["etag"]

    resp = await test_client.get(
        "/api/v1/announcements",
        headers={"If-None-Match": '"obviously-stale-tag"'},
    )
    assert resp.status_code == 200
    assert resp.headers["etag"] == current_etag
    assert "Current" in [a["title"] for a in resp.json()]


# ── Public: GET /announcements/config ─────────────────────────────────

async def test_get_announcement_config_public(test_client: AsyncClient):
    """Public config endpoint returns polling config."""
    resp = await test_client.get("/api/v1/announcements/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "pollIntervalSeconds" in body
    assert "defaultSnoozeMinutes" in body


# ── Admin: GET /admin/announcements ───────────────────────────────────

async def test_admin_list_all_announcements_empty(test_client: AsyncClient):
    """Admin list returns empty list initially."""
    resp = await test_client.get("/api/v1/admin/announcements")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Admin: POST /admin/announcements ─────────────────────────────────

async def test_create_announcement(test_client: AsyncClient):
    """Create an announcement returns 201."""
    body = await _create_announcement(test_client, "New Banner")
    assert body["title"] == "New Banner"
    assert "id" in body
    assert body["bannerType"] == "info"
    assert body["isActive"] is True


async def test_create_announcement_warning_type(test_client: AsyncClient):
    """Create an announcement with warning banner type."""
    body = await _create_announcement(
        test_client, "Warning Banner",
        bannerType="warning",
    )
    assert body["bannerType"] == "warning"


async def test_create_announcement_success_type(test_client: AsyncClient):
    """Create an announcement with success banner type."""
    body = await _create_announcement(
        test_client, "Success Banner",
        bannerType="success",
    )
    assert body["bannerType"] == "success"


async def test_create_announcement_invalid_banner_type(test_client: AsyncClient):
    """Creating with invalid banner type returns 400."""
    resp = await test_client.post(
        "/api/v1/admin/announcements",
        json=_announcement_payload("Bad Type", bannerType="error"),
    )
    assert resp.status_code == 400


async def test_create_announcement_negative_snooze(test_client: AsyncClient):
    """Creating with negative snooze duration returns 400."""
    resp = await test_client.post(
        "/api/v1/admin/announcements",
        json=_announcement_payload("Bad Snooze", snoozeDurationMinutes=-1),
    )
    assert resp.status_code == 400


async def test_create_announcement_with_cta(test_client: AsyncClient):
    """Create an announcement with CTA text and URL."""
    body = await _create_announcement(
        test_client, "CTA Banner",
        ctaText="Click here",
        ctaUrl="https://example.com",
    )
    assert body["ctaText"] == "Click here"
    assert body["ctaUrl"] == "https://example.com"


async def test_create_announcement_missing_title(test_client: AsyncClient):
    """Creating without title returns 422."""
    resp = await test_client.post(
        "/api/v1/admin/announcements",
        json={"message": "No title here"},
    )
    assert resp.status_code == 422


# ── Admin: PATCH /admin/announcements/{id} ────────────────────────────

async def test_update_announcement(test_client: AsyncClient):
    """Update an announcement's title and message."""
    created = await _create_announcement(test_client, "Original Title")
    ann_id = created["id"]

    resp = await test_client.patch(
        f"/api/v1/admin/announcements/{ann_id}",
        json={"title": "Updated Title", "message": "Updated message"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"
    assert resp.json()["message"] == "Updated message"


async def test_update_announcement_banner_type(test_client: AsyncClient):
    """Update an announcement's banner type."""
    created = await _create_announcement(test_client, "Type Change")
    ann_id = created["id"]

    resp = await test_client.patch(
        f"/api/v1/admin/announcements/{ann_id}",
        json={"bannerType": "warning"},
    )
    assert resp.status_code == 200
    assert resp.json()["bannerType"] == "warning"


async def test_update_announcement_invalid_banner_type(test_client: AsyncClient):
    """Updating with invalid banner type returns 400."""
    created = await _create_announcement(test_client, "Bad Update Type")
    ann_id = created["id"]

    resp = await test_client.patch(
        f"/api/v1/admin/announcements/{ann_id}",
        json={"bannerType": "danger"},
    )
    assert resp.status_code == 400


async def test_update_announcement_negative_snooze(test_client: AsyncClient):
    """Updating with negative snooze returns 400."""
    created = await _create_announcement(test_client, "Bad Snooze Update")
    ann_id = created["id"]

    resp = await test_client.patch(
        f"/api/v1/admin/announcements/{ann_id}",
        json={"snoozeDurationMinutes": -5},
    )
    assert resp.status_code == 400


async def test_update_announcement_not_found(test_client: AsyncClient):
    """Updating a non-existent announcement returns 404."""
    resp = await test_client.patch(
        "/api/v1/admin/announcements/ann_nonexistent",
        json={"title": "Ghost"},
    )
    assert resp.status_code == 404


async def test_update_announcement_deactivate(test_client: AsyncClient):
    """Deactivate an announcement via PATCH."""
    created = await _create_announcement(test_client, "Deactivate Me")
    ann_id = created["id"]

    resp = await test_client.patch(
        f"/api/v1/admin/announcements/{ann_id}",
        json={"isActive": False},
    )
    assert resp.status_code == 200
    assert resp.json()["isActive"] is False


# ── Admin: DELETE /admin/announcements/{id} ───────────────────────────

async def test_delete_announcement(test_client: AsyncClient):
    """Delete an announcement returns 204."""
    created = await _create_announcement(test_client, "Delete Me")
    ann_id = created["id"]

    resp = await test_client.delete(f"/api/v1/admin/announcements/{ann_id}")
    assert resp.status_code == 204


async def test_delete_announcement_not_found(test_client: AsyncClient):
    """Deleting a non-existent announcement returns 404."""
    resp = await test_client.delete("/api/v1/admin/announcements/ann_nope")
    assert resp.status_code == 404


# ── Admin: GET /admin/announcements/config ────────────────────────────

async def test_admin_get_config(test_client: AsyncClient):
    """Admin config endpoint returns polling config."""
    resp = await test_client.get("/api/v1/admin/announcements/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "pollIntervalSeconds" in body
    assert "defaultSnoozeMinutes" in body


# ── Admin: PUT /admin/announcements/config ────────────────────────────

async def test_update_config(test_client: AsyncClient):
    """Update announcement config."""
    resp = await test_client.put(
        "/api/v1/admin/announcements/config",
        json={"pollIntervalSeconds": 30, "defaultSnoozeMinutes": 60},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pollIntervalSeconds"] == 30
    assert body["defaultSnoozeMinutes"] == 60


async def test_update_config_poll_interval_too_low(test_client: AsyncClient):
    """Setting pollIntervalSeconds < 5 returns 400."""
    resp = await test_client.put(
        "/api/v1/admin/announcements/config",
        json={"pollIntervalSeconds": 2},
    )
    assert resp.status_code == 400


async def test_update_config_negative_snooze(test_client: AsyncClient):
    """Setting negative defaultSnoozeMinutes returns 400."""
    resp = await test_client.put(
        "/api/v1/admin/announcements/config",
        json={"defaultSnoozeMinutes": -1},
    )
    assert resp.status_code == 400


# ── Full CRUD round-trip ──────────────────────────────────────────────

async def test_announcement_crud_roundtrip(test_client: AsyncClient):
    """Full create -> admin list -> update -> delete cycle."""
    # Create
    created = await _create_announcement(test_client, "Roundtrip")
    ann_id = created["id"]

    # Admin list includes it
    r = await test_client.get("/api/v1/admin/announcements")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()]
    assert ann_id in ids

    # Update
    r = await test_client.patch(
        f"/api/v1/admin/announcements/{ann_id}",
        json={"title": "Roundtrip Updated"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Roundtrip Updated"

    # Delete
    r = await test_client.delete(f"/api/v1/admin/announcements/{ann_id}")
    assert r.status_code == 204
