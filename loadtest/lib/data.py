"""Workspace + datasource ID pool for realistic test traffic.

A cold load test against random/synthetic IDs would hit the backend's
not-found paths repeatedly and tell us nothing about the real workload.
This module fetches a sample of real workspace/datasource IDs once per
Locust user at start-up, then exposes ``pick_*`` helpers that scenarios
call to choose a target for each request.

If discovery fails (e.g. the admin endpoint isn't reachable from the
test account), scenarios fall back to a configured static list via the
``SYNODIC_FALLBACK_WS_ID`` / ``SYNODIC_FALLBACK_DS_ID`` env vars. That
keeps the harness usable in restricted environments.
"""
from __future__ import annotations

import logging
import os
import random
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from config import SETTINGS  # loadtest/ is on sys.path via locustfile.py

logger = logging.getLogger(__name__)

# Process-wide shared discovery state. At 1000 concurrent users each
# spawning would otherwise hit the admin workspaces endpoint 1000 times
# during ramp-up, plus N×workspaces datasource-list calls — easily
# saturating the admin tier before scenario traffic even starts.
# Compute once per Locust process and reuse.
_pool_lock = threading.Lock()
_pool_done = False
_shared_pool: "IdPool | None" = None


@dataclass
class IdPool:
    """Discovered workspace + datasource IDs available for a Locust user."""

    workspace_ids: List[str] = field(default_factory=list)
    # Maps workspace_id → list of datasource IDs in that workspace, so
    # the scenarios can pick a (ws, ds) pair that's actually valid
    # (the /admin/workspaces/{ws}/datasources/{ds}/... routes 404 when
    # the ds isn't in the workspace).
    ws_to_ds: dict[str, List[str]] = field(default_factory=dict)

    def pick_workspace(self) -> Optional[str]:
        return random.choice(self.workspace_ids) if self.workspace_ids else None

    def pick_ws_ds(self) -> Optional[Tuple[str, str]]:
        """Pick a (workspace_id, datasource_id) pair where the ds is in the ws."""
        candidates = [(w, ds) for w, dss in self.ws_to_ds.items() for ds in dss]
        if not candidates:
            return None
        return random.choice(candidates)


def discover(client) -> IdPool:
    """Best-effort discovery of workspace and datasource IDs.

    Calls ``GET /api/v1/admin/workspaces/`` and, for the first
    ``SETTINGS.id_pool_limit`` workspaces, ``GET /api/v1/admin/workspaces/
    {ws_id}/data-sources`` to enumerate datasources. Errors are logged
    and absorbed; the resulting :class:`IdPool` may be empty, in which
    case scenarios fall back to the env-configured static IDs.

    Only the first user per Locust process actually hits the backend;
    subsequent users get the cached pool. Keeps a 1000-user swarm from
    pummeling the admin tier with redundant discovery calls during the
    spawn-up window.
    """
    global _pool_done, _shared_pool
    with _pool_lock:
        if _pool_done and _shared_pool is not None:
            return _shared_pool
        pool = _discover_uncached(client)
        _shared_pool = pool
        _pool_done = True
        return pool


def _discover_uncached(client) -> IdPool:
    pool = IdPool()

    with client.get(
        "/api/v1/admin/workspaces/",
        name="discover:workspaces",
        catch_response=True,
    ) as resp:
        if resp.status_code != 200:
            logger.warning(
                "Workspace discovery failed: HTTP %s — falling back to env-configured IDs",
                resp.status_code,
            )
            resp.success()  # don't penalise the run with a discovery failure
            return _env_fallback(pool)
        resp.success()
        payload = resp.json() if resp.text else {}

    # The backend envelopes most list responses as ``{data: [...]}`` or
    # plain arrays depending on the endpoint. Handle both shapes
    # defensively so a non-load-test schema change doesn't kill the
    # harness silently.
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        logger.warning("Unexpected workspace list payload shape: %r", type(items))
        return _env_fallback(pool)

    pool.workspace_ids = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
    pool.workspace_ids = pool.workspace_ids[: SETTINGS.id_pool_limit]

    # Enumerate datasources per workspace. Cap the per-ws fetches; the
    # full Cartesian product is irrelevant for representative load.
    #
    # Path is `/data-sources` (with hyphen, no trailing slash) to match
    # backend/app/api/v1/endpoints/workspaces.py:193 — the cached-stats
    # endpoint at workspaces.py:393 uses `/datasources/{ds_id}` (no
    # hyphen) which is a confusing inconsistency in the backend itself.
    for ws_id in pool.workspace_ids:
        with client.get(
            f"/api/v1/admin/workspaces/{ws_id}/data-sources",
            name="discover:datasources",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                # Log loudly — silent suppression here hid a 404 caused
                # by a path mismatch and left ws_to_ds empty, which then
                # made the cached-stats scenario fire /__no_target__
                # rows instead of real traffic.
                logger.warning(
                    "Datasource discovery for ws=%s returned HTTP %s — ws_to_ds will be empty for it.",
                    ws_id, resp.status_code,
                )
                resp.success()
                continue
            resp.success()
            ds_payload = resp.json() if resp.text else {}
        ds_items = ds_payload.get("data") if isinstance(ds_payload, dict) else ds_payload
        if isinstance(ds_items, list):
            pool.ws_to_ds[ws_id] = [
                it.get("id") for it in ds_items if isinstance(it, dict) and it.get("id")
            ]

    total_ds = sum(len(v) for v in pool.ws_to_ds.values())
    logger.info(
        "Discovered %d workspace(s), %d datasource(s) across them.",
        len(pool.workspace_ids), total_ds,
    )
    if not pool.workspace_ids:
        return _env_fallback(pool)
    return pool


def _env_fallback(pool: IdPool) -> IdPool:
    """Seed the pool from env vars when live discovery yields nothing.

    Useful for restricted environments where the test account can hit
    the user-facing endpoints but not the admin workspace list. Set
    ``SYNODIC_FALLBACK_WS_ID`` and ``SYNODIC_FALLBACK_DS_ID`` (comma-
    separated for multiple) to keep traffic targeted at known-good IDs.
    """
    ws_raw = os.getenv("SYNODIC_FALLBACK_WS_ID", "").strip()
    ds_raw = os.getenv("SYNODIC_FALLBACK_DS_ID", "").strip()
    if not ws_raw:
        return pool
    ws_ids = [w for w in (s.strip() for s in ws_raw.split(",")) if w]
    ds_ids = [d for d in (s.strip() for s in ds_raw.split(",")) if d]
    pool.workspace_ids = ws_ids
    if ds_ids:
        # Pair every fallback ds against every fallback ws — small set, fine.
        pool.ws_to_ds = {w: list(ds_ids) for w in ws_ids}
    return pool
