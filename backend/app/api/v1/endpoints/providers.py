"""
Admin Provider endpoints — CRUD for physical database server registrations.
Providers are pure infrastructure: host/port/credentials, no graph or ontology.
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import List, Tuple
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.engine import (
    get_db_session,
    get_provider_probe_db_session,
    get_provider_probe_session,
    with_short_session,
)
from backend.app.db.repositories import provider_repo
from backend.app.providers.manager import provider_manager as provider_registry  # alias during migration
from backend.common.models.management import (
    ProviderCreateRequest,
    ProviderUpdateRequest,
    ProviderResponse,
    ConnectionTestResult,
    ProviderImpactResponse,
)

router = APIRouter()

# ── Provider test cache + in-flight dedup ──────────────────────────
# Reason: multiple hook instances may mount simultaneously and each kick
# off an initial probe sweep. The cache collapses duplicate simultaneous
# probes to the last real result, and the in-flight map collapses
# concurrent probes to a single awaitable. Keyed on
# (provider_id, provider.updated_at) so any credential or host change
# instantly invalidates stale entries without explicit eviction.
#
# TTL kept tight (10s) because an explicit user click on "Test" wants
# the current truth, not stale state. The old 60s TTL was written for a
# frontend stampede that ``useProviderHealthSweep`` already bounds
# (concurrency=3 + one-sweep-per-mount), so the longer window was
# vestigial and produced the "service is down but UI still says healthy"
# UX for up to a minute on both failure AND recovery transitions.
# Callers that want to force-bypass the cache (manual user click, post-
# edit revalidation, etc.) pass ``?fresh=true``.
_TEST_CACHE_TTL_SECS: float = 10.0
_test_cache: dict[str, Tuple[float, str, ConnectionTestResult]] = {}
_test_inflight: dict[str, "asyncio.Future[ConnectionTestResult]"] = {}

# ── /status bounded fan-out ─────────────────────────────────────────
# Resilience mandate: N providers should never mean N concurrent driver
# instantiations + N concurrent DB session opens. Cap concurrency so the
# management-DB pool (20 + 10 overflow) stays drained even when the
# operator has dozens of providers registered.
_STATUS_PROBE_CONCURRENCY: int = 5
_STATUS_PROBE_TIMEOUT_SECS: float = 1.5
_STATUS_OVERALL_TIMEOUT_SECS: float = 6.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _breaker_open_error(provider_id: str) -> str | None:
    """Inspect the registry's cached proxies for any open circuit breakers
    on *provider_id*. Returns a user-facing reason string when the breaker
    is tripped; otherwise ``None``.

    Replaces the hand-rolled negative cache — the pybreaker state machine
    inside each :class:`CircuitBreakerProxy` is the authoritative source of
    "recently failed" and is race-free under concurrency.
    """
    for cache_key, proxy in list(provider_registry._providers.items()):
        if cache_key[0] != provider_id:
            continue
        state = getattr(proxy, "breaker_state", None)
        if state != "open":
            continue
        breaker = getattr(proxy, "breaker", None)
        reset_timeout = int(getattr(breaker, "reset_timeout", 30)) if breaker else 30
        return f"Provider circuit open. Will probe downstream again in ~{reset_timeout}s."
    return None


def _provider_type_value(provider_type) -> str:
    return provider_type.value if hasattr(provider_type, "value") else str(provider_type)


async def _run_connectivity_probe(
    *,
    provider_type,
    host: str | None,
    port: int | None,
    tls_enabled: bool,
    creds: dict | None,
) -> ConnectionTestResult:
    """Bounded reachability probe used by the ``/test`` endpoint.

    P0.2: prefer ``provider.preflight(deadline_s=...)`` which does ONLY a
    fast TCP / handshake check (≤2s budget). The previous implementation
    wrapped ``get_stats()`` in a 10s timeout, but ``get_stats()`` triggered
    eager schema reconciliation in some adapters (FalkorDB ran 15
    ``CREATE INDEX`` queries with 3s timeouts each), so the 10s budget
    was measuring the wrong thing entirely — connect-time would routinely
    exceed 30s while the wait_for sat idle.

    With ``preflight()``, the probe finishes in ≤2.5s for an unreachable
    host, and ≤500ms for a reachable one.
    """
    PREFLIGHT_DEADLINE_S = 2.0
    PROBE_WALL_CLOCK_S = 2.5  # PREFLIGHT_DEADLINE_S + small slack

    instance = provider_registry._create_provider_instance(
        _provider_type_value(provider_type),
        host,
        port,
        None,
        tls_enabled,
        creds,
    )
    preflight = getattr(instance, "preflight", None)

    t0 = time.monotonic()
    try:
        if callable(preflight):
            # Outer wait_for is a backstop — preflight is contractually
            # bounded by deadline_s, but cap the wall clock anyway.
            result = await asyncio.wait_for(
                preflight(deadline_s=PREFLIGHT_DEADLINE_S),
                timeout=PROBE_WALL_CLOCK_S,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            if result.ok:
                return ConnectionTestResult(success=True, latencyMs=round(elapsed_ms, 1))
            return ConnectionTestResult(success=False, error=result.reason)

        # Fallback for adapters that haven't grown a preflight() yet.
        # Use the same tight budget so we don't regress the bug we just fixed.
        await asyncio.wait_for(instance.get_stats(), timeout=PROBE_WALL_CLOCK_S)
        latency = (time.monotonic() - t0) * 1000
        return ConnectionTestResult(success=True, latencyMs=round(latency, 1))
    except asyncio.TimeoutError:
        return ConnectionTestResult(
            success=False,
            error=f"Connection timed out after {PROBE_WALL_CLOCK_S:.1f}s",
        )
    except Exception as exc:
        return ConnectionTestResult(success=False, error=str(exc))
    finally:
        # Best-effort cleanup so a stale instance does not pin sockets.
        close = getattr(instance, "close", None)
        if callable(close):
            try:
                await asyncio.wait_for(close(), timeout=0.5)
            except Exception:
                pass


@router.get("/status")
async def list_provider_statuses(
    session: AsyncSession = Depends(get_provider_probe_db_session),
):
    """Return provider readiness — STRICT structural decoupling from
    provider state.

    This endpoint is polled continuously by the FE status banner and
    admin pages. The handler does ONLY:

      1. List registered providers from the PROVIDER_PROBE pool.
      2. Read in-memory breaker state (``provider_manager`` cache).
      3. Read background-warmup cache (``app.state.provider_warmup_cache``).
      4. Merge; return immediately.

    There is NO outbound work, NO provider construction, NO sockets
    opened. A registered provider host being DNS-unresolvable / TLS-
    broken / hung has zero effect on the response time of this endpoint
    — the request handler can never block on it.

    Provider state is OBSERVED OFFLINE by:
      - The background warmup loop (``backend/app/providers/warmup.py``),
        which probes each provider via ``preflight()`` in round-robin
        and updates the cache. Default cycle: ≥30s, ≤1.5s per probe.
      - Real traffic to the provider, which trips the per-instance
        circuit breaker on network failures. The breaker state is
        authoritative when present (it reflects actual user-observed
        truth); the warmup cache is the fallback for un-visited
        providers.

    With this contract, hosting 1 or 100 providers — any number of them
    unreachable — never affects the request path.
    """
    providers = await provider_repo.list_providers(session)
    if not providers:
        return []

    # Read in-memory state — both calls are O(1).
    try:
        breaker_states = provider_registry.report_provider_states()
    except Exception:
        breaker_states = {}
    warmup_cache = getattr(provider_registry, "warmup_cache", {}) or {}

    def _resolve_status(provider) -> dict:
        if not provider.is_active:
            return {
                "id": provider.id,
                "name": provider.name,
                "status": "unknown",
                "lastCheckedAt": None,
            }

        # 1. Authoritative breaker state when present — this reflects
        #    real traffic, the highest-fidelity signal.
        breaker_key_match = next(
            (k for k in breaker_states if k.startswith(f"{provider.id}:")),
            None,
        )
        breaker = breaker_states.get(breaker_key_match) if breaker_key_match else None
        if breaker == "healthy":
            return {
                "id": provider.id,
                "name": provider.name,
                "status": "ready",
                "lastCheckedAt": _iso_now(),
            }
        if breaker in ("unavailable", "instantiation_failed"):
            return {
                "id": provider.id,
                "name": provider.name,
                "status": "unavailable",
                "lastCheckedAt": _iso_now(),
                "error": f"Provider circuit: {breaker}",
            }
        if breaker == "degraded":
            return {
                "id": provider.id,
                "name": provider.name,
                "status": "unavailable",
                "lastCheckedAt": _iso_now(),
                "error": "Provider in degraded state (half-open)",
            }

        # 2. Fallback to warmup cache — populated by the background
        #    loop, so even un-visited providers carry a status.
        warmup = warmup_cache.get(provider.id)
        if warmup is not None:
            return {
                "id": provider.id,
                "name": provider.name,
                "status": "ready" if warmup.get("ok") else "unavailable",
                "lastCheckedAt": _iso_timestamp(warmup.get("checked_at")),
                "error": (None if warmup.get("ok") else warmup.get("reason")),
            }

        # 3. Truly unknown — registered but never probed (e.g. very new
        #    or warmup loop hasn't reached it yet).
        return {
            "id": provider.id,
            "name": provider.name,
            "status": "unknown",
            "lastCheckedAt": None,
        }

    return [_resolve_status(p) for p in providers]


def _iso_timestamp(epoch_seconds: float | None) -> str | None:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


@router.get("", response_model=List[ProviderResponse])
async def list_providers(
    session: AsyncSession = Depends(get_db_session),
):
    """List all registered providers."""
    return await provider_repo.list_providers(session)


@router.post("/test-connection", response_model=ConnectionTestResult)
async def test_unsaved_provider_connection(
    req: ProviderCreateRequest = Body(...),
):
    # Spanner is a managed gRPC service keyed on project / instance /
    # database (in extra_config). It does NOT use host/port. Reject
    # ambiguous requests so a misconfigured client doesn't silently
    # bypass the project/instance/database addressing — emulator mode
    # is opt-in via extra_config.useEmulator, not via host=localhost.
    if (req.provider_type or "").lower() == "spanner":
        if req.host or req.port:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Spanner is a managed service; host/port are not used. "
                    "Provide projectId, instanceId, databaseId via extra_config; "
                    "for the cloud-spanner-emulator set extra_config.useEmulator=true."
                ),
            )
    creds = req.credentials.model_dump() if req.credentials else None
    return await _run_connectivity_probe(
        provider_type=req.provider_type,
        host=req.host,
        port=req.port,
        tls_enabled=req.tls_enabled,
        creds=creds,
    )


@router.post("", response_model=ProviderResponse, status_code=201)
async def create_provider(
    req: ProviderCreateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Register a new provider (database server)."""
    return await provider_repo.create_provider(session, req)


@router.get("/{provider_id}", response_model=ProviderResponse)
async def get_provider(
    provider_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single provider."""
    prov = await provider_repo.get_provider(session, provider_id)
    if not prov:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    return prov


@router.put("/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: str = Path(...),
    req: ProviderUpdateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a provider. Evicts any cached provider instances."""
    prov = await provider_repo.update_provider(session, provider_id, req)
    if not prov:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    await provider_registry.evict_provider(provider_id)
    return prov


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a provider. Rejects if workspaces still reference it."""
    if await provider_repo.has_workspaces(session, provider_id):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete provider: one or more workspaces still reference it.",
        )
    deleted = await provider_repo.delete_provider(session, provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    await provider_registry.evict_provider(provider_id)


@router.get("/{provider_id}/impact", response_model=ProviderImpactResponse)
async def get_provider_impact(
    provider_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Calculate the blast radius of deleting a provider."""
    # Ensure provider exists first
    prov_row = await provider_repo.get_provider_orm(session, provider_id)
    if not prov_row:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    
    return await provider_repo.get_provider_impact(session, provider_id)


@router.post("/{provider_id}/test", response_model=ConnectionTestResult)
async def test_provider(
    provider_id: str = Path(...),
    fresh: bool = Query(
        False,
        description=(
            "Bypass the 10s cached result and run a fresh probe. Set by "
            "the UI on manual 'Test' button clicks so the user sees the "
            "current truth (not a stale cached success/failure)."
        ),
    ),
):
    """Test connectivity to a registered provider.

    Phase 2.5 §2.5.2 — short-session pattern: open a session only long
    enough to fetch the provider row + credentials, close it, then
    perform the (potentially slow) outbound call WITHOUT holding a DB
    connection. Keeps the pool drained even when many providers are
    being probed against unreachable hosts.

    Caches the last result for 10s keyed on the provider's updated_at
    (config change → instant invalidation). Concurrent probes of the
    same provider collapse onto a single in-flight awaitable. ``fresh``
    bypasses the cache read *and* write so a dead/recovered transition
    is reflected immediately on the next user click.
    """
    # 1. Short DB read on the PROVIDER_PROBE pool — close the session
    #    before the outbound preflight (P0.5: probe traffic isolated from
    #    WEB pool, so a status-page refresh storm cannot starve request
    #    handlers).
    async with get_provider_probe_session() as session:
        prov_row = await provider_repo.get_provider_orm(session, provider_id)
        if not prov_row:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
        fingerprint = str(prov_row.updated_at or "")
        ptype = prov_row.provider_type
        host = prov_row.host
        port = prov_row.port
        tls = prov_row.tls_enabled
        creds = await provider_repo.get_credentials(session, provider_id)
    # 2. Cache + in-flight dedup — pure in-memory, no DB. Explicit user
    #    clicks (fresh=True) bypass entirely and also invalidate the
    #    cache entry so subsequent background polls see the new truth.
    if fresh:
        _test_cache.pop(provider_id, None)
    else:
        cached = _test_cache.get(provider_id)
        if cached is not None:
            cached_at, cached_fp, cached_result = cached
            if cached_fp == fingerprint and (time.monotonic() - cached_at) < _TEST_CACHE_TTL_SECS:
                return cached_result

        existing = _test_inflight.get(provider_id)
        if existing is not None:
            return await existing

    loop = asyncio.get_running_loop()
    future: "asyncio.Future[ConnectionTestResult]" = loop.create_future()
    if not fresh:
        _test_inflight[provider_id] = future
    try:
        # 3. Outbound provider call — no DB session held during this window.
        result = await _run_connectivity_probe(
            provider_type=ptype,
            host=host,
            port=port,
            tls_enabled=tls,
            creds=creds,
        )

        # Always write the freshest result so any in-flight callers and
        # subsequent cached reads reflect current truth — including the
        # fresh=True path, which updates the cache for future non-fresh
        # callers rather than skipping the write.
        _test_cache[provider_id] = (time.monotonic(), fingerprint, result)
        if not future.done():
            future.set_result(result)
        return result
    finally:
        _test_inflight.pop(provider_id, None)
        if not future.done():
            # Guard: if an uncaught exception ever bubbles, don't leave
            # awaiters hanging forever.
            future.set_exception(RuntimeError("Provider test aborted"))
            # Mark the exception as retrieved so asyncio doesn't log
            # ``Future exception was never retrieved`` when no caller is
            # awaiting (common when the originating /test request was
            # cancelled mid-flight by the upstream timeout).
            try:
                future.exception()
            except Exception:
                pass


async def _load_provider_for_outbound(provider_id: str, asset_name: str | None):
    """Short-session helper: fetch the row + creds, snapshot fields, close session.

    Centralises the Phase 2.5 §2.5.2 pattern shared by every endpoint
    below this comment. Returns a ready-to-instantiate provider object.
    """
    async with with_short_session() as session:
        prov_row = await provider_repo.get_provider_orm(session, provider_id)
        if not prov_row:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
        creds = await provider_repo.get_credentials(session, provider_id)
        ptype, host, port, tls = (
            prov_row.provider_type, prov_row.host, prov_row.port, prov_row.tls_enabled,
        )
    return provider_registry._create_provider_instance(ptype, host, port, asset_name, tls, creds)


# ── Cache-only discovery endpoints moved to endpoints/insights.py ─────
# ``GET /admin/providers/{id}/assets`` and
# ``GET /admin/providers/{id}/assets/{name}/stats`` were synchronous
# live calls into the upstream provider with a 10s timeout. For large
# providers (50+ graphs) and slow upstreams they'd 504 under load. The
# replacements live at ``/admin/insights/providers/{id}/assets[/...]``
# and read only from ``asset_discovery_cache``; cache-miss enqueues a
# background discovery job. See backend/app/api/v1/endpoints/insights.py.


@router.post("/{provider_id}/discover-schema")
async def discover_schema(
    provider_id: str = Path(...),
    asset_name: str = Body(None, embed=True),
):
    """Introspect an asset's schema. Short-session pattern."""
    instance = await _load_provider_for_outbound(provider_id, asset_name)
    try:
        schema = await asyncio.wait_for(instance.discover_schema(), timeout=15)
        return schema
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Provider timed out while discovering schema")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

