"""Unit tests for the ETag/304 helper in backend.app.common.http_caching.

The integration sweep lives in test_api_announcements.py (real
endpoint, real request flow). These tests pin the bits of the helper
that are easy to break in isolation — strong-tag formatting, weak-tag
tolerance, the comma-separated list parse, the wildcard, and the
exact contents of the 304 Response.
"""
from starlette.requests import Request

from backend.app.common.http_caching import (
    if_none_match_matches,
    make_etag,
    maybe_not_modified,
)


def _request_with_header(name: str, value: str) -> Request:
    """Build a bare Starlette Request with one header set.

    Starlette stores headers as a list of byte-tuples on the ASGI scope.
    Bypassing the full FastAPI / TestClient stack keeps these tests
    dependency-free and fast.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1"))],
    }
    return Request(scope)


def _request_without_headers() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


# ── make_etag ─────────────────────────────────────────────────────────


def test_make_etag_is_deterministic():
    """Same inputs → same tag. Required for cross-pod consistency: two
    different worker pods serving the same data must emit the same tag
    or clients will never get a 304."""
    a = make_etag("announcements", 3, "2026-05-16T10:00:00Z")
    b = make_etag("announcements", 3, "2026-05-16T10:00:00Z")
    assert a == b


def test_make_etag_is_rfc7232_strong_quoted():
    """RFC 7232 requires the value to be quoted. We never emit weak
    (W/) tags so the prefix must be absent."""
    tag = make_etag("x")
    assert tag.startswith('"') and tag.endswith('"')
    assert not tag.startswith("W/")


def test_make_etag_distinguishes_adjacent_inputs():
    """``"ab", "cd"`` must not collide with ``"abc", "d"``. The NUL
    separator inside make_etag is what guarantees this; this test
    would fail if we ever swapped it for plain concatenation."""
    assert make_etag("ab", "cd") != make_etag("abc", "d")


def test_make_etag_distinguishes_value_changes():
    """Trivially: any single-byte change to any input flips the tag."""
    base = make_etag("cached-stats", "ds_abc", "2026-05-16T10:00:00Z")
    diff_id = make_etag("cached-stats", "ds_xyz", "2026-05-16T10:00:00Z")
    diff_ts = make_etag("cached-stats", "ds_abc", "2026-05-16T10:00:01Z")
    assert base != diff_id
    assert base != diff_ts
    assert diff_id != diff_ts


# ── if_none_match_matches ─────────────────────────────────────────────


def test_if_none_match_no_header_returns_false():
    """No If-None-Match → always 'fresh client, send 200'."""
    req = _request_without_headers()
    assert if_none_match_matches(req, '"abc"') is False


def test_if_none_match_exact_match():
    req = _request_with_header("If-None-Match", '"abc"')
    assert if_none_match_matches(req, '"abc"') is True


def test_if_none_match_mismatch():
    req = _request_with_header("If-None-Match", '"abc"')
    assert if_none_match_matches(req, '"xyz"') is False


def test_if_none_match_wildcard_matches_anything():
    """RFC 7232 §3.2 — ``*`` matches any existing resource. Browsers
    don't usually send it, but CDNs and some HTTP libraries do."""
    req = _request_with_header("If-None-Match", "*")
    assert if_none_match_matches(req, '"whatever"') is True


def test_if_none_match_comma_separated_list():
    """The header can carry multiple values; any of them matching is a
    hit. This is the standard browser bfcache pattern."""
    req = _request_with_header("If-None-Match", '"old", "current", "older-still"')
    assert if_none_match_matches(req, '"current"') is True
    assert if_none_match_matches(req, '"missing"') is False


def test_if_none_match_strips_weak_prefix():
    """A proxy may downgrade strong tags to weak by prefixing W/. We
    accept that as equivalent so clients aren't penalised for
    intermediary munging."""
    req = _request_with_header("If-None-Match", 'W/"abc"')
    assert if_none_match_matches(req, '"abc"') is True


# ── maybe_not_modified ────────────────────────────────────────────────


def test_maybe_not_modified_returns_none_on_miss():
    """No matching If-None-Match → caller should fall through and build
    the full response. None is the signal for that."""
    req = _request_without_headers()
    assert maybe_not_modified(req, '"abc"') is None


def test_maybe_not_modified_returns_304_on_match():
    req = _request_with_header("If-None-Match", '"abc"')
    resp = maybe_not_modified(req, '"abc"')
    assert resp is not None
    assert resp.status_code == 304
    # 304 must echo the ETag so intermediaries can cache the validator
    # (RFC 7232 §4.1). The Cache-Control hints at how aggressively the
    # client may avoid revalidating in the immediate future.
    assert resp.headers.get("ETag") == '"abc"'
    assert "must-revalidate" in resp.headers.get("Cache-Control", "")


def test_maybe_not_modified_304_has_empty_body():
    """RFC 7232 §4.1 — 304 MUST NOT have an entity body."""
    req = _request_with_header("If-None-Match", '"abc"')
    resp = maybe_not_modified(req, '"abc"')
    assert resp is not None
    assert resp.body == b""


def test_maybe_not_modified_custom_cache_control_honoured():
    """Per-endpoint policy override path — e.g. /announcements may
    want a different freshness lifetime than /cached-stats."""
    req = _request_with_header("If-None-Match", '"abc"')
    resp = maybe_not_modified(req, '"abc"', cache_control="public, max-age=60")
    assert resp is not None
    assert resp.headers["Cache-Control"] == "public, max-age=60"
