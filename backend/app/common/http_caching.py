"""HTTP cache-validator helpers (RFC 7232).

Adds a minimal, generic ETag/304 path to FastAPI handlers. Used by
read-only endpoints whose responses change rarely relative to how often
they're polled — `/api/v1/announcements`, `/api/v1/admin/.../cached-stats`,
and (once WS-6 lands) `/api/v1/providers/status` plus
`/api/v1/aggregation/history`.

The win: a steady-state client revalidating a stable resource gets a
304 with an empty body. For the announcements lockstep poll at 1000
users (~67 req/s baseline) this drops the response-body bandwidth and
serialisation cost by ~95% in steady state.

Design notes:

* **Weak vs strong ETags.** We emit strong ETags (no ``W/`` prefix)
  derived from canonical inputs like ``updated_at`` plus the resource
  identifier. They're stable across worker pods because the derivation
  is deterministic; no per-process state.
* **No state.** This module owns no cache or store. The ETag is computed
  from data the handler already has — usually an ``updated_at``
  timestamp and an identifier — so no shared infrastructure is needed.
* **Bypasses ``response_model`` validation on 304.** Returning a bare
  :class:`Response` short-circuits FastAPI's serialisation, which is
  what we want (a 304 has no body).
* **Strict equality on If-None-Match.** Per RFC 7232 we should also
  honour a wildcard ``*`` and a comma-separated list. We do both.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Optional

from fastapi import Request, Response


def make_etag(*parts: object) -> str:
    """Compute a strong ETag from arbitrary parts.

    Each part is stringified and separated by a NUL byte so adjacent
    values can't collide (``"ab", "cd"`` differs from ``"abc", "d"``).
    Returned with the surrounding double quotes RFC 7232 requires; pass
    it straight into a ``ETag:`` header.

    Truncated to 16 bytes (32 hex chars). For our use case — proving
    "did this resource change since you last saw it" — the full SHA-256
    is overkill; 128 bits of collision resistance is plenty and keeps
    the header small.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\0")
    return f'"{h.hexdigest()[:32]}"'


def _parse_if_none_match(header_value: str) -> Iterable[str]:
    """Yield individual ETag values from an ``If-None-Match`` header.

    Per RFC 7232 the header can be:
    * a single ETag (``"abc"``)
    * a comma-separated list (``"abc", "def"``)
    * the wildcard ``*`` (matches any extant resource)

    We strip whitespace and the ``W/`` weak-validator prefix so weak
    and strong forms compare equal for our purposes (we never emit
    weak ETags ourselves, but proxies sometimes downgrade).
    """
    for raw in header_value.split(","):
        token = raw.strip()
        if not token:
            continue
        if token.startswith("W/"):
            token = token[2:]
        yield token


def if_none_match_matches(request: Request, etag: str) -> bool:
    """Return ``True`` when the client's ``If-None-Match`` says they
    already have this exact ``etag`` (or ``*`` — match anything)."""
    raw = request.headers.get("if-none-match")
    if not raw:
        return False
    for token in _parse_if_none_match(raw):
        if token == "*":
            return True
        if token == etag:
            return True
    return False


def maybe_not_modified(
    request: Request,
    etag: str,
    *,
    cache_control: str = "private, max-age=0, must-revalidate",
) -> Optional[Response]:
    """Return a 304 response if the client already has ``etag``; else None.

    Standard pattern at a handler::

        etag = make_etag("announcements", count, latest_updated_at)
        not_modified = maybe_not_modified(request, etag)
        if not_modified is not None:
            return not_modified
        # ... compute the full response, set ETag on it:
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = cache_control
        return body

    The ``Cache-Control`` default mirrors what a polling banner wants:
    private, must-revalidate, no freshness lifetime. Override for
    resources that are safe to serve from a local cache for N seconds
    without revalidation.
    """
    if if_none_match_matches(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    return None


__all__ = ["make_etag", "if_none_match_matches", "maybe_not_modified"]
