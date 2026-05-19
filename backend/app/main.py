import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse

from .api.v1.api import api_router
from .db.engine import init_db, close_db, get_async_session, get_jobs_session, BootstrapError
from .db.seed_templates import seed_templates
from .db.repositories import user_repo
from .db.repositories.refresh_token_repo import make_refresh_store
from .middleware.request_id import RequestIdMiddleware
from .middleware.logging import StructuredLoggingMiddleware, configure_json_logging
from .middleware.security_headers import SecurityHeadersMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from .providers.manager import provider_manager
from backend.auth_service.csrf import CSRFMiddleware
from backend.auth_service.providers import LocalIdentityProvider, register_provider
from backend.auth_service.service import LocalIdentityService

logger = logging.getLogger(__name__)

try:
    from redis.exceptions import ConnectionError as _RedisConnectionError
    from redis.exceptions import TimeoutError as _RedisTimeoutError
except Exception:  # pragma: no cover - redis is part of runtime deps
    _RedisConnectionError = ConnectionError
    _RedisTimeoutError = TimeoutError


# ------------------------------------------------------------------ #
# Lifespan                                                             #
# ------------------------------------------------------------------ #

async def _degraded_recovery_loop(_app: FastAPI, interval: float = 15.0) -> None:
    """Background task: probe the management DB every `interval` seconds.
    On first successful probe, clear the degraded flag. We do NOT re-run
    seeds / auth / aggregation init — the operator should restart the
    service for full functionality. The flag clears so that DB-backed
    endpoints stop 503-ing and the health endpoint reports healthy.
    """
    from .db.engine import get_engine
    from sqlalchemy import text as _sa_text

    logger.info(
        "Degraded-mode recovery loop started (interval=%.0fs, reason=%s)",
        interval, getattr(_app.state, "degraded_reason", "unknown"),
    )
    while True:
        try:
            await asyncio.sleep(interval)
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(_sa_text("SELECT 1"))
            _app.state.degraded = False
            prev_reason = getattr(_app.state, "degraded_reason", None)
            _app.state.degraded_reason = None
            logger.info(
                "Recovery complete — transitioned from degraded to healthy "
                "(was: %s). Restart the service to rerun bootstrap (seeds, "
                "admin, aggregation) for full functionality.",
                prev_reason,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Degraded recovery probe failed: %s", exc)


async def _db_health_loop(_app: FastAPI, interval: float = 15.0) -> None:
    """P2.1 — permanent management-DB health probe.

    The pre-existing ``_degraded_recovery_loop`` only fires on the
    bootstrap-failure path; if Postgres goes down POST-bootstrap,
    nothing flipped ``app.state.degraded`` and the FE banner had no
    structured signal. This loop runs from lifespan-start to shutdown,
    ``SELECT 1`` against the READONLY pool every ``interval`` seconds.

    State transitions:
      healthy → flip ``app.state.degraded=False``, ``degraded_reason=None``
      unhealthy → flip ``app.state.degraded=True``, ``degraded_reason=
                  'db_unreachable'``, log at WARN

    Idempotent: only logs on transitions, not on every poll.
    """
    from .db.engine import get_engine, PoolRole
    from sqlalchemy import text as _sa_text

    logger.info("DB health loop started (interval=%.0fs)", interval)
    last_state: Optional[bool] = None
    while True:
        try:
            await asyncio.sleep(interval)
            try:
                engine = get_engine(PoolRole.READONLY)
                async with asyncio.timeout(2.0):
                    async with engine.connect() as conn:
                        await conn.execute(_sa_text("SELECT 1"))
                healthy = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                healthy = False
                if last_state is not False:
                    logger.warning(
                        "DB health probe failed — flipping app.state.degraded=True (%s)",
                        exc,
                    )

            if healthy and last_state is not True:
                if getattr(_app.state, "degraded", False):
                    logger.info(
                        "DB recovered — clearing app.state.degraded "
                        "(was: %s)",
                        getattr(_app.state, "degraded_reason", "unknown"),
                    )
                _app.state.degraded = False
                _app.state.degraded_reason = None
            elif not healthy:
                _app.state.degraded = True
                _app.state.degraded_reason = "db_unreachable"

            last_state = healthy
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("DB health loop iteration failed unexpectedly: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup / shutdown lifecycle.

    If bootstrap (DB migration, seeds, auth init) fails, the app starts in
    degraded mode: `app.state.degraded = True` and a background task
    probes the DB periodically. DB-backed endpoints will surface 503s via
    the existing OperationalError handler; the health endpoint reports
    degraded explicitly. The operator can fix the DB (e.g. `./dev.sh
    repair`) and either restart or wait for the recovery loop to clear
    the flag.
    """
    configure_json_logging()

    _app.state.degraded = False
    _app.state.degraded_reason = None
    _app.state._recovery_task = None
    # P1.10 — readiness gate. Until lifespan completes core initialisation
    # (init_db, auth wiring, etc.), TimeoutMiddleware refuses non-liveness
    # requests with 503 + Retry-After. Without this gate, a request that
    # arrives during the lifespan window (very short, but real under
    # cold-start traffic) can hit half-initialised global state and either
    # hang or 500 with cryptic stack traces. Liveness probe (/health/live)
    # always answers regardless of this flag.
    _app.state.live = False

    # 1. Initialise management DB tables (idempotent — safe to run every restart)
    try:
        await init_db()
    except BootstrapError as exc:
        _app.state.degraded = True
        _app.state.degraded_reason = exc.reason
        logger.error(
            "Bootstrap failed — starting in degraded mode (reason=%s):\n%s",
            exc.reason, exc,
        )
        _app.state._recovery_task = asyncio.create_task(
            _degraded_recovery_loop(_app)
        )
        yield
        # Shutdown path for degraded start
        if _app.state._recovery_task and not _app.state._recovery_task.done():
            _app.state._recovery_task.cancel()
            try:
                await _app.state._recovery_task
            except asyncio.CancelledError:
                pass
        await close_db()
        logger.info("Synodic Visualization Service stopped (was degraded)")
        return
    except Exception as exc:
        _app.state.degraded = True
        _app.state.degraded_reason = "database_unavailable"
        logger.error(
            "Bootstrap failed with unexpected error — starting in degraded mode: %s",
            exc,
        )
        _app.state._recovery_task = asyncio.create_task(
            _degraded_recovery_loop(_app)
        )
        yield
        if _app.state._recovery_task and not _app.state._recovery_task.done():
            _app.state._recovery_task.cancel()
            try:
                await _app.state._recovery_task
            except asyncio.CancelledError:
                pass
        await close_db()
        logger.info("Synodic Visualization Service stopped (was degraded)")
        return

    # 1b. Graph Store DB (decoupled system-of-record for user-authored
    #     versioned graphs) + its outbox relay. Best-effort: this is an
    #     additive feature on a SEPARATE database, so its unavailability
    #     must NOT take the whole service down — it degrades to "authored
    #     graphs unavailable" while everything else runs.
    _app.state._graph_relay_task = None
    try:
        from .db.graph_store_engine import init_graph_store_db  # noqa: E402
        from .services.graph_outbox_relay import (  # noqa: E402
            run_graph_outbox_relay,
        )

        await init_graph_store_db()
        _graph_relay_stop = asyncio.Event()
        _app.state._graph_relay_stop = _graph_relay_stop
        _app.state._graph_relay_task = asyncio.create_task(
            run_graph_outbox_relay(stop_event=_graph_relay_stop)
        )
        logger.info("Graph Store ready; outbox relay started")
    except Exception as exc:  # noqa: BLE001 — feature-isolated, never fatal
        logger.warning(
            "Graph Store init/relay unavailable — authored-graph feature "
            "degraded (service continues): %s",
            exc,
        )

    # 2. Seed Quick Start Templates (idempotent — skips if already present)
    async with get_async_session() as session:
        await seed_templates(session)

    # 2a. Seed feature system — each seed gets its own session so a failure
    #      in one (e.g. multi-worker IntegrityError) doesn't roll back the others.
    from .db.seed_feature_registry import seed_feature_registry, seed_feature_flags, seed_feature_registry_meta  # noqa: E402
    try:
        async with get_async_session() as session:
            await seed_feature_registry(session)
    except Exception as exc:
        logger.warning("Feature registry seed warning: %s", exc)
    try:
        async with get_async_session() as session:
            await seed_feature_flags(session)
    except Exception as exc:
        logger.warning("Feature flags seed warning: %s", exc)
    try:
        async with get_async_session() as session:
            await seed_feature_registry_meta(session)
    except Exception as exc:
        logger.warning("Feature registry meta seed warning: %s", exc)

    # 2b. Seed system default ontology (idempotent — merge-not-overwrite strategy)
    try:
        from .ontology.adapters.sqlalchemy_repo import SQLAlchemyOntologyRepository
        from .ontology.service import LocalOntologyService
        async with get_async_session() as session:
            repo = SQLAlchemyOntologyRepository(session)
            svc = LocalOntologyService(repo)
            await svc.seed_system_defaults()
            await session.commit()
    except Exception as exc:
        logger.warning("System default ontology seed warning: %s", exc)

    # 2c. Bootstrap system admin (idempotent — skips if any user exists)
    # Always ensures at least one admin account is present.
    # Customizable via ADMIN_EMAIL / ADMIN_PASSWORD env vars; defaults provided.
    try:
        import os
        from .db.repositories import user_repo
        from .auth.password import hash_password
        admin_email = os.getenv("ADMIN_EMAIL", "admin@nexuslineage.local")
        admin_password = os.getenv("ADMIN_PASSWORD", "changeme")
        async with get_async_session() as session:
            user_count = await user_repo.count_users(session)
            if user_count == 0:
                user = await user_repo.create_user(
                    session,
                    email=admin_email,
                    password_hash=hash_password(admin_password),
                    first_name="System",
                    last_name="Admin",
                    status="active",
                )
                await user_repo.assign_role(session, user.id, "admin")
                await user_repo.create_approval(
                    session, user.id, status="approved", approved_by="system",
                )
                logger.info(
                    "System admin created: %s (change password after first login!)",
                    admin_email,
                )
    except Exception as exc:
        logger.warning("Admin bootstrap warning: %s", exc)

    # 3. Environment bootstrap is no longer auto-invoked on startup.
    #    Fresh installs go through the admin wizard; Docker quickstart /
    #    CI fixtures invoke `python -m backend.scripts.seed_default_environment`
    #    explicitly. Startup must never mutate user data.
    logger.info(
        "Startup is side-effect-free. Run "
        "`python -m backend.scripts.seed_default_environment` for dev seed; "
        "admin wizard handles production onboarding."
    )

    # 4. Wire up the auth service. The IdentityService is the single
    #    boundary every consumer crosses; today it's an in-process
    #    LocalIdentityService, tomorrow (post-extraction) a remote HTTP
    #    client implementing the same protocol.
    register_provider("local", LocalIdentityProvider())

    async def _emit_user_event(session, event_type: str, payload: dict) -> None:
        await user_repo.create_outbox_event(session, event_type=event_type, payload=payload)

    # RBAC Phase 1: resolve permission claims at login/refresh and embed
    # them in the access JWT. The auth service forwards the dict
    # opaquely; the FastAPI ``requires(...)`` dependency reads it back.
    from backend.app.services import permission_service

    async def _resolve_claims(session, user_id: str) -> dict:
        claims = await permission_service.resolve(session, user_id)
        return claims.to_jwt_dict()

    _app.state.identity_service = LocalIdentityService(
        session_factory=get_async_session,
        user_repo=user_repo,
        refresh_store_factory=make_refresh_store,
        outbox_emit=_emit_user_event,
        claims_resolver=_resolve_claims,
    )
    logger.info("Auth service initialised (provider=local, rbac_claims=on)")

    # 5. Wire up the aggregation service (role-gated)
    from .runtime.role import current_role, runs_scheduler, runs_recovery
    role = current_role()

    # Proxy mode: when AGGREGATION_PROXY_ENABLED=true, the viz-service
    # does NOT instantiate any aggregation objects locally. All 13
    # aggregation endpoints are proxied to the Control Plane (port 8091).
    # No dispatcher, no worker, no scheduler, no recovery.
    aggregation_proxy_enabled = os.getenv(
        "AGGREGATION_PROXY_ENABLED", "false"
    ).lower() == "true"

    if aggregation_proxy_enabled:
        logger.info(
            "Aggregation: proxy mode enabled — all endpoints forwarded to %s",
            os.getenv("AGGREGATION_SERVICE_URL", "http://localhost:8091"),
        )
        # Start event listener to sync aggregation status from Control Plane
        # into local workspace_data_sources table.
        _agg_event_listener = None
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                from .services.aggregation.redis_client import get_redis
                from .services.aggregation.event_listener import AggregationEventListener
                _agg_event_listener = AggregationEventListener(
                    redis_client=get_redis(),
                    session_factory=get_jobs_session,
                )
                _app.state._agg_event_listener = _agg_event_listener
                _app.state._agg_event_listener_task = asyncio.create_task(
                    _agg_event_listener.start()
                )
                logger.info("Aggregation event listener started (syncs status from Control Plane)")
            except Exception as exc:
                logger.warning("Aggregation event listener startup failed: %s", exc)
    else:
        try:
            from .services.aggregation import (
                AggregationService, AggregationWorker,
                InProcessDispatcher, AggregationScheduler,
            )
            from .services.aggregation.dispatcher import PostgresDispatcher
            from .runtime.role import runs_worker

            # Choose dispatcher based on role + dispatch mode.
            # - redis:     RedisStreamDispatcher  (production — workers consume via XREADGROUP)
            # - postgres:  PostgresDispatcher     (legacy — workers consume via LISTEN/NOTIFY)
            # - dual:      DualDispatcher         (migration — writes to both Redis + Postgres)
            # - inprocess: InProcessDispatcher    (dev — all-in-one single process)
            # - auto:      auto-detect from SYNODIC_ROLE + REDIS_URL presence
            dispatch_mode = os.getenv("AGGREGATION_DISPATCH_MODE", "auto")

            if dispatch_mode == "redis":
                from .services.aggregation.redis_client import get_redis
                from .services.aggregation.dispatcher import RedisStreamDispatcher
                agg_dispatcher = RedisStreamDispatcher(get_redis())
                logger.info("Aggregation dispatch: RedisStreamDispatcher (workers consume via Redis Streams)")
            elif dispatch_mode == "dual":
                from .services.aggregation.redis_client import get_redis
                from .services.aggregation.dispatcher import RedisStreamDispatcher, DualDispatcher
                agg_dispatcher = DualDispatcher(
                    postgres_dispatcher=PostgresDispatcher(get_jobs_session),
                    redis_dispatcher=RedisStreamDispatcher(get_redis()),
                )
                logger.info("Aggregation dispatch: DualDispatcher (Redis + Postgres for zero-downtime migration)")
            elif dispatch_mode == "postgres":
                agg_dispatcher = PostgresDispatcher(get_jobs_session)
                logger.info("Aggregation dispatch: PostgresDispatcher (legacy standalone worker)")
            elif dispatch_mode == "auto":
                # Auto-detect: if REDIS_URL is set and role is not worker, use Redis
                if os.getenv("REDIS_URL") and not runs_worker():
                    from .services.aggregation.redis_client import get_redis
                    from .services.aggregation.dispatcher import RedisStreamDispatcher
                    agg_dispatcher = RedisStreamDispatcher(get_redis())
                    logger.info("Aggregation dispatch: RedisStreamDispatcher (auto-detected from REDIS_URL)")
                elif not runs_worker():
                    agg_dispatcher = PostgresDispatcher(get_jobs_session)
                    logger.info("Aggregation dispatch: PostgresDispatcher (auto — no REDIS_URL)")
                else:
                    agg_worker = AggregationWorker(get_jobs_session, provider_manager)
                    agg_dispatcher = InProcessDispatcher(agg_worker)
                    logger.info("Aggregation dispatch: InProcessDispatcher (auto — worker role)")
            else:
                # inprocess or unknown — dev/single-process mode
                agg_worker = AggregationWorker(get_jobs_session, provider_manager)
                agg_dispatcher = InProcessDispatcher(agg_worker)
                logger.info("Aggregation dispatch: InProcessDispatcher (all-in-one dev mode)")

            # Get ontology service reference for monolith-mode resolution.
            #
            # The aggregation worker's ontology resolution path needs a
            # long-lived OntologyService (no per-request session to
            # bind to). Until the SQLAlchemyOntologyRepository was
            # taught to accept a session_factory, this passed
            # ``SQLAlchemyOntologyRepository(None)`` with a comment
            # promising "session injected per-call" — but that mechanism
            # didn't exist, so every aggregation lookup raised
            # ``AttributeError: 'NoneType' object has no attribute
            # 'execute'`` and silently fell back to a DB-only path.
            #
            # Now: the repo's ``_scope()`` opens a fresh JOBS-pool session
            # via the factory for each call. Aggregation resolution
            # works without falling back, and a stale-session bug in one
            # query can't poison the next.
            ontology_svc = None
            try:
                from .ontology.adapters.sqlalchemy_repo import SQLAlchemyOntologyRepository
                from .ontology.service import LocalOntologyService
                ontology_svc = LocalOntologyService(
                    SQLAlchemyOntologyRepository(
                        session_factory=get_jobs_session,
                    )
                )
            except Exception:
                logger.warning("Ontology service not available for aggregation — will use DB fallback")

            agg_service = AggregationService(
                dispatcher=agg_dispatcher,
                registry=provider_manager,
                session_factory=get_jobs_session,
                ontology_service=ontology_svc,
            )

            # Register as app state for endpoint access
            _app.state.aggregation_service = agg_service

            # Recovery and scheduler only run on control-plane / dev roles.
            # Web tier never starts background tasks — it is fully stateless.
            if runs_recovery():
                recovered = await agg_service.recover_interrupted_jobs()
                if recovered:
                    logger.info("Recovered %d interrupted aggregation jobs", recovered)

            if runs_scheduler():
                agg_scheduler = AggregationScheduler(get_jobs_session, provider_manager)
                asyncio.create_task(agg_scheduler.start())
                logger.info("Aggregation scheduler started")

            # Stuck-job reconciler — runs wherever the aggregation worker
            # lives. Detects rows whose worker died without writing a
            # terminal status (deploy mid-job, OOM, segfault) and marks
            # them ``failed`` with a clear reason so operators get a
            # working Resume button instead of a perpetual "running"
            # state. Sweep-only; auto-redispatch is Phase 2 work when
            # the dispatch substrate is XAUTOCLAIM-safe.
            from .services.aggregation.reconciler import run_reconciler
            _app.state._reconciler_shutdown = asyncio.Event()
            _app.state._reconciler_task = asyncio.create_task(
                run_reconciler(get_jobs_session, _app.state._reconciler_shutdown),
                name="aggregation-stuck-job-reconciler",
            )
            logger.info("Stuck-job reconciler started")

            # Cross-process cancel bridge. The web tier subscribes so
            # InProcessDispatcher-hosted jobs get cancelled instantly;
            # cancel broadcasts to OTHER processes (insights workers,
            # standalone aggregation workers) reach those processes via
            # their own CancelListeners.
            from .services.aggregation.cancel import CancelListener
            from .services.aggregation.redis_client import get_redis as _get_redis_for_cancel
            try:
                # P2.7 — pass the factory, NOT an eagerly-constructed
                # client. If Redis is down at startup, the factory call
                # inside _run_loop raises, the loop's existing reconnect
                # logic handles it, and the listener auto-recovers when
                # Redis comes back. Without this, a startup-time Redis
                # outage permanently disables the cancel bridge until
                # the next service restart.
                _app.state._cancel_listener = CancelListener(
                    redis_factory=_get_redis_for_cancel,
                )
                await _app.state._cancel_listener.start()
            except Exception as exc:
                logger.warning(
                    "Cancel listener failed to start (cancels will fall back "
                    "to local + direct DB write only): %s", exc,
                )
                _app.state._cancel_listener = None

            logger.info("Aggregation service started (role=%s)", role.value)
        except Exception as exc:
            logger.warning("Aggregation service startup warning: %s", exc)

    # Background provider warmup loop — the single source of provider
    # health observability. The loop probes each registered provider via
    # preflight() in round-robin and updates ``app.state.provider_warmup_cache``
    # so that EVERY status / health endpoint can read state from memory
    # instead of triggering live outbound work on the request path. With
    # this loop in place, hosting 1 or 100 providers — any number of
    # them unreachable — never affects the request path.
    _app.state._warmup_shutdown = asyncio.Event()
    try:
        from .providers.warmup import initial_fast_pass, supervised_warmup_loop
        from .db.engine import get_provider_probe_session
        from .db.repositories import provider_repo as _provider_repo

        async def _list_providers_for_warmup():
            async with get_provider_probe_session() as session:
                rows = await _provider_repo.list_providers(session)
                out = []
                for row in rows:
                    creds = await _provider_repo.get_credentials(session, row.id)
                    out.append({
                        "id": row.id,
                        "provider_type": (
                            row.provider_type.value
                            if hasattr(row.provider_type, "value")
                            else str(row.provider_type)
                        ),
                        "host": row.host,
                        "port": row.port,
                        "tls": row.tls_enabled,
                        "creds": creds,
                    })
                return out

        def _build_provider_for_warmup(cfg):
            return provider_manager._create_provider_instance(
                cfg["provider_type"],
                cfg.get("host"),
                cfg.get("port"),
                None,
                cfg.get("tls", False),
                cfg.get("creds") or {},
            )

        # P1.3 — warmup→manager state-machine callbacks.
        async def _on_recovery(provider_id: str, entry: dict) -> None:
            await provider_manager.record_probe_success(
                provider_id,
                source="warmup",
                elapsed_ms=int(entry.get("elapsed_ms", 0)),
            )

        async def _on_failure(provider_id: str, entry: dict) -> None:
            await provider_manager.record_probe_failure(
                provider_id,
                reason=str(entry.get("reason", "unknown"))[:200],
                source="warmup",
                elapsed_ms=int(entry.get("elapsed_ms", 0)),
            )

        # P1.4 — heartbeat for /health/deps liveness signal.
        import time as _time

        async def _on_cycle_complete() -> None:
            provider_manager.warmup_last_cycle_at = _time.monotonic()

        # P1.5 — initial fast-pass: hard-deadline-bounded one-shot warmup
        # that populates the cache for the cold-start window. Runs
        # concurrently with first request handling; does NOT block
        # lifespan completion. Schedule BEFORE the supervisor so it can
        # observe transitions from the empty cache.
        _app.state._warmup_initial_pass_task = asyncio.create_task(
            initial_fast_pass(
                cache=provider_manager.warmup_cache,
                list_providers=_list_providers_for_warmup,
                build_instance=_build_provider_for_warmup,
                on_recovery=_on_recovery,
                on_failure=_on_failure,
            ),
            name="provider-warmup-initial-pass",
        )

        _app.state._warmup_task = asyncio.create_task(
            supervised_warmup_loop(
                cache=provider_manager.warmup_cache,
                shutdown_event=_app.state._warmup_shutdown,
                list_providers=_list_providers_for_warmup,
                build_instance=_build_provider_for_warmup,
                on_recovery=_on_recovery,
                on_failure=_on_failure,
                on_cycle_complete=_on_cycle_complete,
            ),
            name="provider-warmup-supervisor",
        )
        logger.info("Provider warmup supervisor + initial fast-pass scheduled")
    except Exception as exc:
        logger.warning(
            "Provider warmup loop failed to start: %s — status endpoints "
            "will fall back to in-memory breaker state only", exc,
        )
        _app.state._warmup_task = None

    # P2.1 — start the permanent DB health loop. Unlike the bootstrap-
    # failure-only ``_degraded_recovery_loop``, this runs continuously
    # so a steady-state Postgres outage flips ``app.state.degraded`` and
    # the FE banner gets a structured signal.
    _app.state._db_health_task = asyncio.create_task(
        _db_health_loop(_app), name="db-health-loop",
    )

    # P3.1 — event-loop lag canary. Detects sync code blocking the loop
    # (the failure mode that produced the original 5-min freeze). Stats
    # are read by /health/deps and exposed via metrics. Running this
    # task is the single most-valuable diagnostic for "app frozen"
    # incidents.
    from .observability.event_loop_monitor import (
        EventLoopLagStats,
        run_event_loop_monitor,
    )
    _app.state.event_loop_lag_stats = EventLoopLagStats()
    _app.state._event_loop_shutdown = asyncio.Event()
    _app.state._event_loop_task = asyncio.create_task(
        run_event_loop_monitor(
            stats=_app.state.event_loop_lag_stats,
            shutdown=_app.state._event_loop_shutdown,
        ),
        name="event-loop-monitor",
    )

    # P1.10 — flip the readiness gate. From this point on, the
    # TimeoutMiddleware accepts non-liveness requests; before this, it
    # returns 503 + Retry-After: 5. Setting this AFTER all sync init
    # finishes (DB, auth, aggregation wiring) but BEFORE we yield, so the
    # gate is never falsely-open during partial initialisation.
    _app.state.live = True

    logger.info("Synodic Visualization Service started (role=%s)", role.value)
    yield

    # Shutdown — stop event listener, release providers, close connections.

    # Stop the background warmup loop FIRST so it doesn't try to probe
    # providers during DB / pool teardown.
    _warmup_shutdown = getattr(_app.state, "_warmup_shutdown", None)
    _warmup_task = getattr(_app.state, "_warmup_task", None)
    if _warmup_shutdown is not None:
        _warmup_shutdown.set()
    if _warmup_task is not None and not _warmup_task.done():
        try:
            await asyncio.wait_for(_warmup_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _warmup_task.cancel()
            try:
                await _warmup_task
            except (asyncio.CancelledError, Exception):
                pass

    # P2.1 — stop the DB health loop.
    _db_health_task = getattr(_app.state, "_db_health_task", None)
    if _db_health_task is not None and not _db_health_task.done():
        _db_health_task.cancel()
        try:
            await asyncio.wait_for(_db_health_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    # P3.1 — stop the event-loop lag canary.
    _event_loop_shutdown = getattr(_app.state, "_event_loop_shutdown", None)
    _event_loop_task = getattr(_app.state, "_event_loop_task", None)
    if _event_loop_shutdown is not None:
        _event_loop_shutdown.set()
    if _event_loop_task is not None and not _event_loop_task.done():
        try:
            await asyncio.wait_for(_event_loop_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    # Stop the stuck-job reconciler so it doesn't try to commit during
    # DB pool teardown. Setting the shutdown event lets it exit at the
    # next sleep boundary; the cancel + await is the belt-and-braces
    # path for the case where it's mid-sweep.
    _reconciler_shutdown = getattr(_app.state, "_reconciler_shutdown", None)
    _reconciler_task = getattr(_app.state, "_reconciler_task", None)
    if _reconciler_shutdown is not None:
        _reconciler_shutdown.set()
    if _reconciler_task is not None and not _reconciler_task.done():
        try:
            await asyncio.wait_for(_reconciler_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _reconciler_task.cancel()
            try:
                await _reconciler_task
            except (asyncio.CancelledError, Exception):
                pass

    # Stop the cross-process cancel listener.
    _cancel_listener = getattr(_app.state, "_cancel_listener", None)
    if _cancel_listener is not None:
        try:
            await _cancel_listener.stop()
        except Exception:
            pass

    # Stop aggregation event listener (if running in proxy mode)
    _agg_listener = getattr(_app.state, "_agg_event_listener", None)
    if _agg_listener is not None:
        await _agg_listener.stop()
        _agg_task = getattr(_app.state, "_agg_event_listener_task", None)
        if _agg_task and not _agg_task.done():
            _agg_task.cancel()
            try:
                await _agg_task
            except asyncio.CancelledError:
                pass
        # Close the Redis client used by the event listener
        try:
            from .services.aggregation.redis_client import close_redis
            await close_redis()
        except Exception:
            pass
        logger.info("Aggregation event listener stopped")

    # Release all provider connection pools (with timeout so a hung
    # provider doesn't block graceful shutdown indefinitely).
    try:
        await asyncio.wait_for(provider_manager.evict_all(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("Provider shutdown timed out after 5s — forcing exit")

    # Stop the Graph Store outbox relay + release its pools.
    _relay_task = getattr(_app.state, "_graph_relay_task", None)
    if _relay_task is not None and not _relay_task.done():
        stop = getattr(_app.state, "_graph_relay_stop", None)
        if stop is not None:
            stop.set()
        _relay_task.cancel()
        try:
            await _relay_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — best effort on shutdown
            pass
    try:
        from .db.graph_store_engine import close_graph_store_db  # noqa: E402

        await close_graph_store_db()
    except Exception:  # noqa: BLE001
        pass

    await close_db()
    logger.info("Synodic Visualization Service stopped")


# ------------------------------------------------------------------ #
# App                                                                  #
# ------------------------------------------------------------------ #

app = FastAPI(
    title="Synodic Visualization Service",
    description=(
        "Graph metadata, lineage, ontology, and reference model API. "
        "Supports multiple graph database connections via ProviderRegistry."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# Rate-limit 429 handler
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Global handler for management DB failures — returns structured 503 instead of
# raw 500 with stack trace, so the frontend can show a meaningful message.
from sqlalchemy.exc import OperationalError as _SAOperationalError

@app.exception_handler(_SAOperationalError)
async def _db_operational_error_handler(_request, exc):
    logger.error("Management DB unavailable: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Management database is temporarily unavailable. Please try again."},
    )

def _provider_unavailable_payload(request, exc) -> dict:
    provider_id = request.query_params.get("connectionId")
    return {
        "detail": {
            "code": "PROVIDER_UNAVAILABLE",
            "providerId": provider_id,
            "reason": str(exc),
        }
    }


# P2.1 — paths where a generic OSError/ConnectionError/asyncio.TimeoutError
# really IS likely a graph-provider failure. Anything outside this list,
# we treat as a generic infra / DB / Redis issue and surface as
# DB_UNAVAILABLE rather than the misleading PROVIDER_UNAVAILABLE code.
_PROVIDER_BOUND_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/graph/",
    "/api/v1/admin/providers/",
    "/api/v1/aggregation/",
    "/api/v1/views/",
    "/api/v1/workspaces/",
    "/api/v1/admin/workspaces/",
    "/api/v1/ws_",                       # legacy /ws_<id>/graph routes
)


def _is_provider_bound_path(path: str) -> bool:
    return any(path.startswith(p) for p in _PROVIDER_BOUND_PATH_PREFIXES)


async def _provider_error_handler(request, exc):
    """Fallback handler for raw connectivity errors that bypass the circuit
    breaker (e.g. during provider instantiation).

    P2.1 — path-discriminate: a generic OSError/ConnectionError on a
    non-provider-bound endpoint (e.g. /api/v1/announcements when Postgres
    is down) used to be classified as ``PROVIDER_UNAVAILABLE`` which
    misled the FE banner to blame the graph provider. Now: if the path
    isn't provider-bound, we surface ``DB_UNAVAILABLE`` (or
    ``INFRA_UNAVAILABLE`` for unknown causes) so the user gets an
    accurate signal.
    """
    path = request.url.path
    if _is_provider_bound_path(path):
        logger.warning("Provider connectivity error on %s: %s", path, exc)
        return JSONResponse(
            status_code=503,
            content=_provider_unavailable_payload(request, exc),
        )
    # Non-provider-bound path — surface as a generic infra error.
    logger.warning(
        "Infra/DB connectivity error on non-provider path %s: %s", path, exc,
    )
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "5"},
        content={
            "detail": {
                "code": "DB_UNAVAILABLE",
                "reason": str(exc)[:200],
            }
        },
    )


# Primary handler for provider failures: raised by the CircuitBreakerProxy
# around every graph-provider instance. Carries a retry-after hint and a
# sanitized reason (no redis.exceptions details leak to the client). When
# the breaker is open, this handler fires in <1ms with no network I/O.
from backend.common.adapters import (
    ProviderBusy as _ProviderBusy,
    ProviderUnavailable as _ProviderUnavailable,
)


# ProviderBusy is a subclass of ProviderUnavailable but semantically
# distinct: the provider is healthy, just overloaded right now. Map to
# 429 (Too Many Requests) so clients back off instead of escalating to
# circuit-breaker territory like they would on a 503. Registered BEFORE
# the parent handler so FastAPI's MRO match picks this one for busy
# instances. Retry-After hint is verbatim from the raising site.
@app.exception_handler(_ProviderBusy)
async def _provider_busy_handler(request, exc: _ProviderBusy):
    logger.info(
        "Provider busy on %s: provider=%s reason=%s retry_after=%ds",
        request.url.path, exc.provider_name, exc.reason, exc.retry_after_seconds,
    )
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(exc.retry_after_seconds)},
        content={
            "detail": {
                "code": "PROVIDER_BUSY",
                "providerName": exc.provider_name,
                "reason": exc.reason,
                "retryAfterSeconds": exc.retry_after_seconds,
            }
        },
    )


@app.exception_handler(_ProviderUnavailable)
async def _provider_unavailable_handler(request, exc: _ProviderUnavailable):
    logger.warning(
        "Provider unavailable on %s: provider=%s reason=%s retry_after=%ds",
        request.url.path, exc.provider_name, exc.reason, exc.retry_after_seconds,
    )
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(exc.retry_after_seconds)},
        content={
            "detail": {
                "code": "PROVIDER_UNAVAILABLE",
                "providerName": exc.provider_name,
                "reason": exc.reason,
                "retryAfterSeconds": exc.retry_after_seconds,
            }
        },
    )


# Fallback handlers for raw connectivity errors that bypass the breaker
# (e.g. errors raised during provider instantiation, before the proxy is in
# place). In steady state these should be rare because every cached provider
# is breaker-wrapped.
app.add_exception_handler(ConnectionError, _provider_error_handler)
app.add_exception_handler(OSError, _provider_error_handler)
app.add_exception_handler(asyncio.TimeoutError, _provider_error_handler)
app.add_exception_handler(_RedisConnectionError, _provider_error_handler)
app.add_exception_handler(_RedisTimeoutError, _provider_error_handler)

# ------------------------------------------------------------------ #
# Timeout middleware (raw ASGI — avoids BaseHTTPMiddleware streaming   #
# issues). Wraps every HTTP request in asyncio.wait_for so a hung     #
# provider can never block a request indefinitely.                     #
# ------------------------------------------------------------------ #

class _TimeoutMiddleware:
    """ASGI middleware: tiered per-path timeout for HTTP requests.

    Tiers (P1.8 — anchored prefix match, ordered most-specific first):
        ANY path in SSE_PATHS                exempt entirely (long-lived)
        prefix /api/v1/health        ->  5s  (probes must be fast)
        prefix /api/v1/graph         -> 15s  (read queries, per-query bounded)
        prefix /api/v1/aggregation/  -> 45s  (write-heavy operations)
        everything else              -> 30s  (default)

    Why anchored prefixes (vs. substring): substring matching meant
    ``"/health" in path`` matched ``/api/v1/internal/health-metrics``,
    ``/api/v1/admin/aggregation-foo``, etc., classifying them into the
    wrong tier. Anchored prefixes are unambiguous — a route is either
    in the tier or it isn't.

    Why an explicit SSE registry (vs. ``endswith("/events")``): suffix
    matching binds us to one URL convention. If we ever add ``/stream``
    or ``/live`` SSE endpoints, they'd silently get killed at 30s. The
    registry is the single place to declare "this is long-lived".

    Response-stream invariants (P0.1, fixes the "never recovers" freeze):

        T-1  Exactly one terminal ``http.response.body`` (more_body=False) is
             sent per request, regardless of inner-app behaviour.
        T-2  If the inner app started a response and the deadline fires, we
             emit a final empty body chunk to close the stream cleanly. We
             never re-issue ``http.response.start``, never raise.
        T-3  If the inner app finished a response and the deadline fires
             *afterwards* (race), we emit nothing further.
        T-4  ``send`` is never called after a terminal body chunk has been
             sent — ``tracked_send`` short-circuits in that state.
        T-5  We use ``asyncio.timeout()`` (Python 3.11+, lexically scoped,
             integrates cleanly with cancellation) — NOT ``asyncio.wait_for``,
             which wrapped the wrong shape and orphaned the response stream.
        T-6  First state transition wins; subsequent transitions are no-ops.

    Without these invariants, a single timeout-after-response-started leaves
    the ASGI stream half-open, ``BaseHTTPMiddleware`` raises ``RuntimeError:
    No response returned`` cascading through every middleware layer, and
    uvicorn raises ``Response content shorter than Content-Length`` — which
    poisons the keepalive connection and ratchets the backend toward an
    unrecoverable state.
    """

    # P1.8 — explicit SSE path registry. Routers may extend this at
    # startup via ``register_sse_path("/api/v1/.../my-stream")``. The
    # default suffix ``/events`` matches today's only SSE convention but
    # the contract is explicit registration, not endswith heuristics.
    _SSE_PATH_SUFFIXES: tuple[str, ...] = ("/events",)
    _SSE_EXACT_PATHS: frozenset[str] = frozenset()

    def __init__(self, app):
        self.app = app
        # P1.8 — anchored prefix tiers, ordered most-specific first.
        # Substring matching previously sorted ``/api/v1/internal/health-
        # metrics`` into the 5s health tier and ``/api/v1/admin/aggregation-
        # workers`` into the 45s aggregation tier. Anchored matching is
        # unambiguous: a path either has the exact prefix or it doesn't.
        # Tier list order matters when prefixes nest (e.g. /api/v1/health
        # vs. /api/v1/health/providers — both legitimate; longer matches
        # wins via list-then-startswith).
        # Trace routes get their own (longer) tier ahead of the generic
        # /graph/ tier so the prefix-longest-wins lookup picks them up.
        # Both /api/v1 and /api/v2 surface trace endpoints; main.py
        # collapses the workspace segment so the listed prefixes match
        # /api/v1/{ws}/graph/trace/... and /api/v2/{ws}/graph/trace/...
        self._tiers: list[tuple[str, float]] = [
            ("/api/v1/health",        float(os.getenv("HTTP_TIMEOUT_HEALTH_SECS", "5"))),
            ("/health",               float(os.getenv("HTTP_TIMEOUT_HEALTH_SECS", "5"))),
            ("/api/v1/graph/edges/aggregated", float(os.getenv("HTTP_TIMEOUT_AGGREGATION_SECS", "45"))),
            ("/api/v1/graph/edges/between",    float(os.getenv("HTTP_TIMEOUT_AGGREGATION_SECS", "45"))),
            ("/api/v1/graph/trace",   float(os.getenv("HTTP_TIMEOUT_TRACE_SECS", "60"))),
            ("/api/v2/graph/trace",   float(os.getenv("HTTP_TIMEOUT_TRACE_SECS", "60"))),
            ("/api/v1/graph/",        float(os.getenv("HTTP_TIMEOUT_GRAPH_SECS", "15"))),
            ("/api/v2/graph/",        float(os.getenv("HTTP_TIMEOUT_GRAPH_SECS", "15"))),
            ("/api/v1/aggregation/",  float(os.getenv("HTTP_TIMEOUT_AGGREGATION_SECS", "45"))),
        ]
        self._default_timeout: float = float(os.getenv("HTTP_TIMEOUT_DEFAULT_SECS", "30"))

    def _resolve_timeout(self, path: str) -> float:
        # Workspace-scoped routes are mounted under
        # /api/v{1,2}/{ws_id}/graph/... — collapse the dynamic segment
        # so the literal-prefix tiers above can still match.
        candidates = [path]
        for api_prefix in ("/api/v1/", "/api/v2/"):
            if path.startswith(api_prefix):
                tail = path[len(api_prefix):]
                sep = tail.find("/")
                if sep > 0:
                    candidates.append(api_prefix.rstrip("/") + tail[sep:])
                break
        for pattern, timeout in self._tiers:
            for candidate in candidates:
                if candidate.startswith(pattern):
                    return timeout
        return self._default_timeout

    def _is_sse_path(self, path: str) -> bool:
        if path in self._SSE_EXACT_PATHS:
            return True
        for suffix in self._SSE_PATH_SUFFIXES:
            if path.endswith(suffix):
                return True
        return False

    # P1.10 — paths that are ALWAYS allowed through, even when the app
    # has not flipped its readiness gate. Liveness probes must answer
    # regardless of init state; the back-compat alias /health gets the
    # same treatment for the FE banner.
    _LIVE_GATE_EXEMPT_PREFIXES: tuple[str, ...] = (
        "/health/live",
        "/api/v1/health/live",
        "/health",            # alias for /health/live
        "/api/v1/health",     # alias for /health/live
    )

    @staticmethod
    def _is_live_gate_exempt(path: str) -> bool:
        for prefix in _TimeoutMiddleware._LIVE_GATE_EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # SSE bypass — let the stream run until the client disconnects
        # or the worker emits a terminal event and the handler closes
        # the response. No timeout, no cancellation, no spurious
        # "Request timed out after response started" log noise.
        if self._is_sse_path(path):
            await self.app(scope, receive, send)
            return

        # P1.10 — readiness gate. Until lifespan flips ``app.state.live``,
        # refuse non-liveness requests with 503 + Retry-After. Eliminates
        # the lifespan-incomplete request race where a user request hits
        # half-initialised global state. Exempt paths get through always.
        if not self._is_live_gate_exempt(path):
            try:
                live = bool(getattr(scope.get("app").state, "live", True))
            except Exception:
                live = True   # belt-and-braces: never refuse on attribute lookup error
            if not live:
                response = JSONResponse(
                    {"detail": "Service is starting up. Please retry shortly."},
                    status_code=503,
                    headers={"Retry-After": "5"},
                )
                await response(scope, receive, send)
                return

        timeout = self._resolve_timeout(path)
        original_send = send

        # ASGI guarantees sends within a single request are sequential within
        # the inner task — no concurrent ``tracked_send`` invocations. The
        # timeout handler below runs ONLY after the inner task is cancelled,
        # so there is no shared-state race; flat dict suffices.
        state = {"started": False, "terminal": False}

        async def tracked_send(message):
            # T-4: refuse any send after the response is terminal. Protects
            # the wire if the inner task somehow keeps emitting after we've
            # closed the stream from the timeout handler.
            if state["terminal"]:
                return
            msg_type = message["type"]
            if msg_type == "http.response.start":
                state["started"] = True
            elif msg_type == "http.response.body" and not message.get("more_body", False):
                # Terminal body chunk — happy path.
                state["terminal"] = True
            await original_send(message)

        try:
            async with asyncio.timeout(timeout):
                await self.app(scope, receive, tracked_send)
        except TimeoutError:
            # ``asyncio.timeout()`` converts the inner CancelledError to a
            # TimeoutError. The inner task is already torn down — no more
            # ``tracked_send`` calls will arrive.
            if state["terminal"]:
                # T-3: race — inner finished cleanly just before the deadline.
                return
            if not state["started"]:
                # T-2 (clean case): we own the wire. Send a fresh 504.
                response = JSONResponse(
                    {"detail": f"Request timed out after {timeout:.0f}s — the graph provider may be unreachable."},
                    status_code=504,
                )
                await response(scope, receive, original_send)
                state["terminal"] = True
                return
            # T-2 (stream-corruption case): the inner app already sent
            # ``http.response.start`` (and possibly partial body) before the
            # deadline fired. Emit a terminal empty body chunk to satisfy the
            # ASGI/HTTP protocol so uvicorn does not raise
            # "Response content shorter than Content-Length" and Starlette
            # does not raise "No response returned" upstream.
            try:
                await original_send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })
            except Exception:
                # Client connection already torn down — nothing further to do.
                pass
            state["terminal"] = True
            logger.warning(
                "Request timed out after response started: %s (timeout=%.0fs)",
                path, timeout,
            )

# Must be added FIRST so it wraps all other middleware.
app.add_middleware(_TimeoutMiddleware)

# ------------------------------------------------------------------ #
# Middleware (outermost → innermost order)                             #
# ------------------------------------------------------------------ #

_cors_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else ["http://localhost:3000", "http://localhost:5173"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
)

# GZip compression for responses > 1 KB
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Structured JSON access log + X-Process-Time header
app.add_middleware(StructuredLoggingMiddleware)

# X-Request-ID generation / propagation
app.add_middleware(RequestIdMiddleware)

# Security headers (X-Content-Type-Options, X-Frame-Options, CSP, HSTS, etc.)
app.add_middleware(SecurityHeadersMiddleware)

# CSRF double-submit. Innermost so it runs closest to the route — the
# preceding middleware (CORS, security headers) must complete first so
# that browser preflight checks succeed before we enforce the CSRF rule.
app.add_middleware(CSRFMiddleware)

# ------------------------------------------------------------------ #
# Routers                                                              #
# ------------------------------------------------------------------ #

app.include_router(api_router, prefix="/api/v1")

# Internal pool-pressure metrics (Phase 2.5 §2.5.3) — opt-in via
# INTERNAL_METRICS_ENABLED=true. Restrict at ingress in production.
from .middleware.db_metrics import router as db_metrics_router  # noqa: E402
app.include_router(db_metrics_router)


# ------------------------------------------------------------------ #
# Health endpoint                                                       #
# ------------------------------------------------------------------ #

# ────────────────────────────────────────────────────────────────────
# Three-tier health endpoints (P0.3).
#
#   /health/live   — process liveness. NO I/O, NO DB, NO providers.
#                    Constant-time. Used by k8s livenessProbe and the
#                    frontend's banner — must NEVER fail unless the
#                    event loop is dead.
#   /health/ready  — readiness. Single 1s-budgeted DB ping (READONLY
#                    pool). Provider state does NOT affect readiness.
#                    Used by k8s readinessProbe.
#   /health/deps   — deep dependency report. DB ping + in-memory
#                    breaker state. For dashboards / on-call only.
#                    NEVER on a probe hot path.
#
#   /health        — back-compat alias for /health/live (one-release
#                    deprecation; CHANGELOG documents migration).
# ────────────────────────────────────────────────────────────────────


@app.get("/health/live", tags=["health"])
@app.get("/api/v1/health/live", tags=["health"], include_in_schema=False)
async def liveness_check():
    """Process liveness — constant-time, zero I/O. Always 200.

    A failure of /health/live means the event loop is wedged or the
    process is shutting down. By construction it can never be a 5xx
    because we don't await anything; uvicorn returning 5xx implies
    the process itself is unable to schedule a coroutine.
    """
    return {"status": "live", "version": "0.2.0"}


@app.get("/health", tags=["health"])
@app.get("/api/v1/health", tags=["health"], include_in_schema=False)
async def health_alias():
    """Back-compat alias for /health/live.

    Existing infrastructure (k8s manifests, load-balancer probes, the
    frontend banner) historically polled /health expecting a status
    field. Returning the same lightweight shape preserves that contract
    while migrating new deployments to the explicit /health/live path.
    """
    return {"status": "live", "version": "0.2.0"}


@app.get("/health/deps", tags=["health"])
@app.get("/api/v1/health/deps", tags=["health"], include_in_schema=False)
async def dependency_health():
    """Deep dependency report — for dashboards and on-call.

    Includes:
      - Management DB ping (1s budget; READONLY pool)
      - Provider breaker states (in-memory, no I/O)
      - Degraded-mode flag + reason

    NEVER call this from a hot probe path. K8s probes use /health/live
    and /health/ready; this endpoint is for humans investigating an
    incident.
    """
    from .db.engine import get_engine
    from sqlalchemy import text

    degraded = getattr(app.state, "degraded", False)
    degraded_reason = getattr(app.state, "degraded_reason", None)

    result: dict = {
        "status": "healthy",
        "version": "0.2.0",
        "dependencies": {},
        "providers": {},
    }

    # Management DB ping (bounded; never blocks the response indefinitely).
    try:
        engine = get_engine()
        async with asyncio.timeout(1.0):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        result["dependencies"]["management_db"] = "healthy"
    except Exception as exc:
        result["dependencies"]["management_db"] = f"unhealthy: {exc}"
        result["status"] = "unhealthy"

    # Provider breaker state (O(1), in-memory only — no provider I/O).
    try:
        result["providers"] = provider_manager.report_provider_states()
    except Exception as exc:
        result["providers"] = {"_error": str(exc)[:200]}

    # P3.1 — event-loop lag surface. p99 lag > 500ms implies the loop
    # is wedged; > 50ms implies coroutines are queueing.
    lag_stats = getattr(app.state, "event_loop_lag_stats", None)
    if lag_stats is not None:
        result["dependencies"]["event_loop_lag_p99_ms"] = round(
            lag_stats.p99_s * 1000, 1,
        )
        result["dependencies"]["event_loop_lag_peak_ms"] = round(
            lag_stats.peak_s * 1000, 1,
        )
        if lag_stats.p99_s >= 0.5:
            result["dependencies"]["event_loop"] = (
                f"critical: p99={lag_stats.p99_s * 1000:.0f}ms"
            )
            if result["status"] == "healthy":
                result["status"] = "degraded"
                result.setdefault("reason", "event_loop_wedged")
        elif lag_stats.p99_s >= 0.05:
            result["dependencies"]["event_loop"] = (
                f"degraded: p99={lag_stats.p99_s * 1000:.0f}ms"
            )
        else:
            result["dependencies"]["event_loop"] = "healthy"

    # P3.3 — warmup-loop heartbeat surface.
    # warmup_last_cycle_at is None until the first cycle finishes; any age
    # greater than ~3× MIN_FULL_CYCLE_S indicates the supervisor is not
    # making progress (DB outage, persistent crash, deadlock).
    import time as _time
    last_cycle = getattr(provider_manager, "warmup_last_cycle_at", None)
    if last_cycle is None:
        result["dependencies"]["warmup_loop"] = "starting"
    else:
        age_s = _time.monotonic() - last_cycle
        # 90s threshold = 3× default MIN_FULL_CYCLE_S; tune via env if needed.
        warmup_stale_threshold_s = float(
            os.getenv("WARMUP_HEARTBEAT_STALE_THRESHOLD_S", "90")
        )
        if age_s > warmup_stale_threshold_s:
            result["dependencies"]["warmup_loop"] = (
                f"degraded: stalled {age_s:.0f}s"
            )
            if result["status"] == "healthy":
                result["status"] = "degraded"
                result.setdefault("reason", "warmup_loop_stalled")
        else:
            result["dependencies"]["warmup_loop"] = (
                f"healthy (cycle {age_s:.0f}s ago)"
            )

    if degraded:
        result["status"] = "degraded"
        result["reason"] = degraded_reason or "database_unavailable"

    return result


@app.get("/health/ready", tags=["health"])
@app.get("/api/v1/health/ready", tags=["health"], include_in_schema=False)
async def readiness_check():
    """
    Readiness probe — Postgres must be reachable. Provider health is
    reported informally but does NOT affect the readiness verdict, so
    non-graph endpoints remain available during provider outages.

    K8s: use this for readinessProbe; use /health for livenessProbe.
    """
    from .db.engine import get_engine
    from sqlalchemy import text

    result: dict = {
        "status": "ready",
        "version": "0.2.0",
        "postgres": "healthy",
        "providers": {},
    }

    # Postgres check (required for readiness)
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "postgres": f"unhealthy: {exc}",
                "providers": {},
            },
        )

    # Provider health from ProviderManager — no new connections, just
    # reads in-memory breaker state for cached/attempted providers.
    result["providers"] = provider_manager.report_provider_states()

    return result


# P2.2 — workspace×data_source enumeration cache, populated lazily by
# ``provider_health_check`` and TTL'd. With 50+ workspaces × 67 RPS of
# FE polling, the previous N+1 query pattern was a scaling cliff. The
# cache + single JOIN brings it to one DB roundtrip every 5s regardless
# of poll rate. Invalidation is implicit via TTL — workspace/datasource
# CRUD is rare enough that 5s staleness is acceptable; the warmup loop
# itself doesn't depend on this cache.
_DS_INDEX_CACHE: dict = {"data": None, "ts": 0.0}
_DS_INDEX_TTL_S: float = float(os.getenv("PROVIDER_HEALTH_DS_INDEX_TTL_S", "5"))
_DS_INDEX_LOAD_DEADLINE_S: float = float(
    os.getenv("PROVIDER_HEALTH_DS_INDEX_DEADLINE_S", "1.0"),
)

# Last-known last-good response for /health/providers. Used when DB is
# down so the FE preserves its provider map instead of seeing an empty
# 200 (which the FE store interprets as "no providers").
_LAST_KNOWN_HEALTH_PROVIDERS: dict = {"data": None, "ts": 0.0}


async def _load_ds_index() -> list[tuple[str, str, str]]:
    """One-shot JOIN that returns ``(workspace_id, data_source_id,
    provider_id)`` for every active data source.

    Replaces the N+1 pattern (1 + N queries for N workspaces). At 50+
    workspaces × 67 RPS poll, the previous version saturated the
    PROVIDER_PROBE pool; this one issues exactly one roundtrip.
    """
    import time as _time
    cached = _DS_INDEX_CACHE.get("data")
    cache_ts = _DS_INDEX_CACHE.get("ts", 0.0)
    if cached is not None and (_time.monotonic() - cache_ts) < _DS_INDEX_TTL_S:
        return cached  # type: ignore[return-value]

    from .db.engine import get_provider_probe_session
    from .db.models import WorkspaceORM, WorkspaceDataSourceORM
    from sqlalchemy import select

    async with asyncio.timeout(_DS_INDEX_LOAD_DEADLINE_S):
        async with get_provider_probe_session() as session:
            stmt = (
                select(
                    WorkspaceORM.id,
                    WorkspaceDataSourceORM.id,
                    WorkspaceDataSourceORM.provider_id,
                )
                .join(
                    WorkspaceDataSourceORM,
                    WorkspaceDataSourceORM.workspace_id == WorkspaceORM.id,
                )
                .where(WorkspaceORM.deleted_at.is_(None))
            )
            rows = (await session.execute(stmt)).all()

    result: list[tuple[str, str, str]] = [
        (str(ws_id), str(ds_id), str(prov_id))
        for ws_id, ds_id, prov_id in rows
    ]
    _DS_INDEX_CACHE["data"] = result
    _DS_INDEX_CACHE["ts"] = _time.monotonic()
    return result


@app.get("/api/v1/health/providers", tags=["health"])
async def provider_health_check():
    """
    Per-workspace provider health — STRICT structural decoupling from
    provider state.

    Polled continuously by the FE banner. MUST NOT do any outbound work
    and MUST NOT blank the FE state under DB pressure (G5 fix).

    Contract:
      - Single JOIN on the PROVIDER_PROBE pool, cached 5s (P2.2 — fixes
        the N+1 query that previously saturated the pool at scale).
      - Reads breaker + warmup state with timestamp-aware tie-breaking
        via the unified ``ProviderState`` (P1.1, fixes G7 — stale negative
        on recovery).
      - On DB timeout / failure: return last-known map with stalenessSecs,
        NOT an empty 200 (G5 fix).
      - NO outbound work, NO provider construction, NO sockets opened.
    """
    import time as _time

    # 1. Enumerate data sources via the cached single-JOIN. On failure,
    #    fall back to last-known so the FE never sees an empty 200.
    ds_meta: list[tuple[str, str, str]]
    db_error: Optional[str] = None
    try:
        ds_meta = await _load_ds_index()
    except (asyncio.TimeoutError, TimeoutError):
        db_error = "db_read_deadline_exceeded"
        ds_meta = []
    except Exception as exc:
        db_error = f"db_error: {str(exc)[:160]}"
        ds_meta = []

    if db_error and _LAST_KNOWN_HEALTH_PROVIDERS["data"] is not None:
        # Serve last-known with staleness so the FE preserves continuity.
        last = _LAST_KNOWN_HEALTH_PROVIDERS["data"]
        staleness_s = max(0.0, _time.monotonic() - _LAST_KNOWN_HEALTH_PROVIDERS["ts"])
        return {
            **last,
            "stalenessSecs": int(staleness_s),
            "error": db_error,
        }

    if not ds_meta:
        empty = {
            "providers": {},
            "dataSourceCount": 0,
            "configured": False,
        }
        if db_error:
            empty["error"] = db_error
        return empty

    # 2. Read in-memory breaker state and warmup cache — O(1), no I/O.
    try:
        breaker_states = provider_manager.report_provider_states()
    except Exception:
        breaker_states = {}
    warmup_cache = getattr(provider_manager, "warmup_cache", {}) or {}

    # 3. Map each (ws_id, ds_id, prov_id) to its best-known status with
    #    timestamp-aware tie-breaking (G7 fix).
    providers: dict = {}
    for ws_id, ds_id, prov_id in ds_meta:
        ds_key = f"{ws_id}:{ds_id}"

        # Breaker state — keyed on (prov_id, graph_name); pick first match.
        # NOTE: removed dead `breaker_key = f"{prov_id}:"` lookup (G25 fix —
        # the trailing-colon key never existed because graph_name is always
        # non-empty for FalkorDB/Neo4j).
        breaker_key = next(
            (k for k in breaker_states if k.startswith(f"{prov_id}:")),
            None,
        )
        breaker = breaker_states.get(breaker_key) if breaker_key else None
        warmup = warmup_cache.get(prov_id)

        # Timestamp-aware tie-break: if the manager has a unified state for
        # this (prov_id, graph_name) AND the warmup observation is more
        # recent than the breaker's open transition, trust warmup. This
        # closes G7: a recovered provider with a stale OPEN breaker no
        # longer reports unhealthy when warmup has confirmed recovery.
        prefer_warmup = False
        if breaker_key:
            graph_name = breaker_key[len(prov_id) + 1:]
            snapshot = provider_manager.snapshot_state(prov_id, graph_name)
            if snapshot is not None and snapshot.warmup_overrides_breaker():
                prefer_warmup = True

        # Resolution order: breaker (real traffic) → warmup (offline obs) →
        # unknown. Reversed when timestamp tie-break says warmup is fresher.
        if not prefer_warmup and breaker == "healthy":
            providers[ds_key] = {"status": "healthy", "providerId": prov_id}
        elif not prefer_warmup and breaker in ("unavailable", "instantiation_failed"):
            providers[ds_key] = {
                "status": "unhealthy",
                "providerId": prov_id,
                "error": f"provider_circuit: {breaker}",
            }
        elif not prefer_warmup and breaker == "degraded":
            providers[ds_key] = {
                "status": "unhealthy",
                "providerId": prov_id,
                "error": "provider_circuit: degraded (half-open)",
            }
        elif warmup is not None:
            providers[ds_key] = {
                "status": "healthy" if warmup.get("ok") else "unhealthy",
                "providerId": prov_id,
                "error": warmup.get("reason") if not warmup.get("ok") else None,
                "checkedAt": warmup.get("checked_at"),
            }
        else:
            providers[ds_key] = {"status": "unknown", "providerId": prov_id}

    response = {
        "providers": providers,
        "dataSourceCount": len(ds_meta),
        "configured": True,
    }
    # Cache last-known so DB outages don't blank the FE map.
    _LAST_KNOWN_HEALTH_PROVIDERS["data"] = response
    _LAST_KNOWN_HEALTH_PROVIDERS["ts"] = _time.monotonic()
    return response
