"""SQLAlchemy async engine + session factory for the **Graph Store DB**.

This is a *separate, decoupled* database from the management DB
(:mod:`backend.app.db.engine`). It is the durable, append-only,
content-addressed system-of-record for user-authored versioned graphs
(graphs / branches / commits / node+edge versions / partition manifests
/ working sets / per-attribute audit / its own transactional outbox).

Why a second database and not another schema in the management DB:

* It scales on a different axis (millions of rows per graph, hundreds
  of graphs) and must be vertically/horizontally scaled — partitioning,
  read replicas, later Spanner — *without* touching the management OLTP
  instance that serves auth/workspaces/views/RBAC.
* Writes here are the system-of-record; the graph provider (FalkorDB)
  is a strictly downstream, eventually-consistent read projection.

Cross-DB boundary rule: there are **no DB-level foreign keys or joins**
between this database and the management DB. References such as
``workspace_id`` / ``ontology_id`` / ``created_by`` are id strings
validated at the service layer (same discipline the ``aggregation``
schema already uses across its boundary). The single-transaction
guarantee is preserved by co-locating the write, the audit row and the
outbox event in *this* database's own transaction — no code path may
open a transaction spanning both databases.

Required env var:
    GRAPH_STORE_DB_URL  e.g. postgresql+asyncpg://synodic:synodic@graph-db:5432/synodic_graph

For local dev we fall back to a separate ``synodic_graph`` database on
the dev Postgres server (still a distinct database with its own
connection pool and migration lineage — the decoupling property holds
locally without forcing a second container).

Usage mirrors :mod:`backend.app.db.engine` exactly::

    async with get_graph_store_session() as session:           # web pool
        ...
    async with get_graph_store_jobs_session() as session:      # jobs pool
        ...   # graph-store outbox relay, materialization worker
"""
import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# DB-agnostic startup-error classification is shared with the management
# engine — there is no Graph-Store-specific bootstrap taxonomy.
from backend.app.db.engine import (
    BootstrapError,
    _is_transient_db_error,
    _permanent_bootstrap_reason,
)

logger = logging.getLogger(__name__)


class GraphStorePoolRole(str, Enum):
    """Logical access pattern, one connection pool each (bulkhead).

    * ``WEB`` — request handlers doing working-set / commit CRUD.
    * ``JOBS`` — graph-store outbox relay, materialization worker
      (long-running, checkpoint commits).
    * ``READONLY`` — history / diff / blame read endpoints; opened
      ``default_transaction_read_only=on`` so a stray write errors at
      the wire.
    * ``ADMIN`` — Alembic runner, lifespan init. Small pool.

    Note there is no ``PROVIDER_PROBE`` role here — provider probing
    targets the management DB's provider config, not this database.
    """

    WEB = "web"
    JOBS = "jobs"
    READONLY = "readonly"
    ADMIN = "admin"


# Independent sizing from the management DB. The graph store sees fewer
# but heavier connections (bulk COPY on commit, manifest scans) so the
# WEB default is smaller than management's 20+10 and JOBS a touch larger
# (relay + materialization both live here).
_POOL_DEFAULTS: dict[GraphStorePoolRole, dict[str, int]] = {
    GraphStorePoolRole.WEB:      {"pool_size": 12, "max_overflow": 8},
    GraphStorePoolRole.JOBS:     {"pool_size": 10, "max_overflow": 6},
    GraphStorePoolRole.READONLY: {"pool_size": 8,  "max_overflow": 4},
    GraphStorePoolRole.ADMIN:    {"pool_size": 2,  "max_overflow": 0},
}

_DEV_FALLBACK_URL = "postgresql+asyncpg://synodic:synodic@localhost:5432/synodic_graph"


def _build_graph_store_url() -> str:
    """Resolve the Graph Store DB URL.

    ``GRAPH_STORE_DB_URL`` is mandatory in any non-dev deployment and
    must point at a *different* instance/database than
    ``MANAGEMENT_DB_URL``. Local dev falls back to a separate
    ``synodic_graph`` database. Postgres-only, asyncpg-only — same
    constraint as the management engine.
    """
    url = os.getenv("GRAPH_STORE_DB_URL", _DEV_FALLBACK_URL)
    if not url.startswith("postgresql+asyncpg://"):
        raise RuntimeError(
            "The Graph Store requires Postgres v16+ via asyncpg. "
            f"GRAPH_STORE_DB_URL must start with 'postgresql+asyncpg://' (got: {url[:30]!r})."
        )
    return url


GRAPH_STORE_DATABASE_URL: str = _build_graph_store_url()

_engines: dict[GraphStorePoolRole, AsyncEngine] = {}
_session_factories: dict[GraphStorePoolRole, async_sessionmaker[AsyncSession]] = {}


def _pool_kwargs(role: GraphStorePoolRole) -> dict:
    """Pool-sizing knobs, overridable per role via
    ``GRAPH_DB_<ROLE>_POOL_SIZE`` / ``..._POOL_MAX_OVERFLOW`` (kept
    independent from the management DB's ``DB_*`` knobs so the two
    databases tune separately). Shared timeout knobs fall back to the
    same ``DB_POOL_*`` env vars the management engine uses.
    """
    defaults = _POOL_DEFAULTS[role]
    role_prefix = f"GRAPH_DB_{role.value.upper()}_"

    pool_size_env = os.getenv(f"{role_prefix}POOL_SIZE")
    pool_size = int(pool_size_env) if pool_size_env is not None else defaults["pool_size"]

    overflow_env = os.getenv(f"{role_prefix}POOL_MAX_OVERFLOW")
    max_overflow = int(overflow_env) if overflow_env is not None else defaults["max_overflow"]

    return {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT_SECS", "10")),
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE_SECS", "1800")),
        "pool_pre_ping": os.getenv("DB_POOL_PRE_PING", "true").lower() == "true",
    }


def _asyncpg_connect_args(role: GraphStorePoolRole) -> dict:
    """Per-connection asyncpg knobs. READONLY connections are opened
    read-only at the protocol layer (defence in depth, same as the
    management engine)."""
    args: dict = {
        "timeout": float(os.getenv("DB_CONNECT_TIMEOUT_SECS", "5")),
        "command_timeout": float(os.getenv("DB_COMMAND_TIMEOUT_SECS", "30")),
    }
    if role is GraphStorePoolRole.READONLY:
        args["server_settings"] = {"default_transaction_read_only": "on"}
    return args


def get_graph_store_engine(role: GraphStorePoolRole = GraphStorePoolRole.WEB) -> AsyncEngine:
    """Return the cached Graph Store engine for *role*, creating it on
    first use (lazy — a process that never touches the graph store opens
    no sockets to it)."""
    existing = _engines.get(role)
    if existing is not None:
        return existing
    kw = _pool_kwargs(role)
    connect_args = _asyncpg_connect_args(role)
    engine = create_async_engine(
        GRAPH_STORE_DATABASE_URL,
        echo=os.getenv("DB_ECHO", "false").lower() == "true",
        connect_args=connect_args,
        **kw,
    )
    _engines[role] = engine
    logger.info(
        "GraphStoreEngine[%s] pool: size=%d, max_overflow=%d, timeout=%ds, "
        "recycle=%ds, pre_ping=%s%s",
        role.value,
        kw["pool_size"], kw["max_overflow"], kw["pool_timeout"],
        kw["pool_recycle"], kw["pool_pre_ping"],
        " (read_only)" if role is GraphStorePoolRole.READONLY else "",
    )
    return engine


def get_graph_store_session_factory(
    role: GraphStorePoolRole = GraphStorePoolRole.WEB,
) -> async_sessionmaker[AsyncSession]:
    """Return the cached sessionmaker bound to *role*'s Graph Store engine."""
    existing = _session_factories.get(role)
    if existing is not None:
        return existing
    factory = async_sessionmaker(
        bind=get_graph_store_engine(role),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    _session_factories[role] = factory
    return factory


@asynccontextmanager
async def _session_scope(role: GraphStorePoolRole) -> AsyncGenerator[AsyncSession, None]:
    """Commit-on-success / rollback-on-error scope with cancellation-safe
    cleanup — identical semantics to the management engine's scope (see
    that module for the rationale on shielding ``CancelledError``)."""
    factory = get_graph_store_session_factory(role)
    session = factory()
    try:
        try:
            yield session
            await asyncio.shield(session.commit())
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(session.rollback())
            raise
        except Exception:
            await session.rollback()
            raise
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(session.close())


@asynccontextmanager
async def get_graph_store_session() -> AsyncGenerator[AsyncSession, None]:
    """WEB-pool Graph Store session (context manager) — working-set /
    commit CRUD from non-FastAPI code."""
    async with _session_scope(GraphStorePoolRole.WEB) as session:
        yield session


async def get_graph_store_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a WEB-pool Graph Store session per
    request::

        session: AsyncSession = Depends(get_graph_store_db_session)
    """
    async with _session_scope(GraphStorePoolRole.WEB) as session:
        yield session


@asynccontextmanager
async def get_graph_store_jobs_session() -> AsyncGenerator[AsyncSession, None]:
    """JOBS-pool Graph Store session — the graph-store outbox relay and
    the materialization worker. Isolated from WEB so a relay backlog
    cannot starve request handlers."""
    async with _session_scope(GraphStorePoolRole.JOBS) as session:
        yield session


@asynccontextmanager
async def get_graph_store_readonly_session() -> AsyncGenerator[AsyncSession, None]:
    """READONLY-pool Graph Store session — history / diff / blame reads.
    Connection is read-only at the protocol layer."""
    async with _session_scope(GraphStorePoolRole.READONLY) as session:
        yield session


async def get_graph_store_readonly_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — READONLY-pool Graph Store session per request
    (history / diff / blame endpoints)."""
    async with _session_scope(GraphStorePoolRole.READONLY) as session:
        yield session


@asynccontextmanager
async def get_graph_store_admin_session() -> AsyncGenerator[AsyncSession, None]:
    """ADMIN-pool Graph Store session — Alembic runner / lifespan init."""
    async with _session_scope(GraphStorePoolRole.ADMIN) as session:
        yield session


# ------------------------------------------------------------------ #
# Declarative base — its own MetaData, isolated from the management DB #
# ------------------------------------------------------------------ #

# A distinct MetaData object means Graph Store ORM models are registered
# completely separately from the management ``Base``. The management
# ``init_db()``'s metadata.create_all therefore never creates graph
# tables, and this database's create_all never creates management
# tables. Schema isolation is structural, not by convention.
graph_store_metadata = MetaData()


class GraphStoreBase(DeclarativeBase):
    """Declarative base for every Graph Store ORM model. Bind all
    user-graph / version-control / graph-store-outbox models to this
    base (NOT :class:`backend.app.db.engine.Base`)."""

    metadata = graph_store_metadata


def _graph_store_alembic_config():
    """Alembic config for the Graph Store DB's *own* migration lineage.

    The graph store has a separate ``alembic`` directory / version table
    so it can be provisioned and migrated independently of the
    management DB. Points at ``backend/alembic_graph_store/`` and the
    ``alembic_graph_store.ini`` config (delivered in the migrations
    unit of Phase 0).
    """
    from alembic.config import Config

    here = os.path.dirname(os.path.abspath(__file__))               # backend/app/db
    backend_dir = os.path.normpath(os.path.join(here, "..", ".."))  # backend
    ini_path = os.path.join(backend_dir, "alembic_graph_store.ini")
    cfg = Config(ini_path)
    cfg.set_main_option(
        "script_location", os.path.join(backend_dir, "alembic_graph_store")
    )
    return cfg


def _run_graph_store_alembic_upgrade() -> None:
    """Synchronous Alembic upgrade for the Graph Store lineage — invoked
    via ``asyncio.to_thread`` (Alembic is sync)."""
    from alembic import command

    command.upgrade(_graph_store_alembic_config(), "head")


async def init_graph_store_db() -> None:
    """Apply the Graph Store DB's own Alembic migrations.

    Boot resilience mirrors :func:`backend.app.db.engine.init_db`:
    bounded exponential-backoff retry while Postgres is unreachable,
    fail-fast on a permanent error (auth / missing db / bad migration).

    Until the dedicated Alembic lineage lands (next Phase-0 unit), this
    falls back to ``GraphStoreBase.metadata.create_all(checkfirst=True)``
    on the ADMIN pool — the same idempotent fallback pattern the
    management engine uses for the aggregation schema. Importing the
    graph-store models module here is what populates the metadata.
    """
    budget = float(os.getenv("GRAPH_DB_STARTUP_RETRY_TIMEOUT_SECS", "60"))
    import time as _time

    deadline = _time.monotonic() + budget
    delay = 1.0
    attempt = 0

    have_alembic_lineage = os.path.isdir(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "alembic_graph_store",
        )
    )

    while True:
        attempt += 1
        try:
            if have_alembic_lineage:
                await asyncio.to_thread(_run_graph_store_alembic_upgrade)
            else:
                # Fallback: create_all from the isolated metadata. No-op
                # once every table already exists (checkfirst=True).
                # Importing the models module is what registers the
                # tables onto graph_store_metadata.
                from backend.app.db import models_graph as _gs_models  # noqa: F401

                engine = get_graph_store_engine(GraphStorePoolRole.ADMIN)
                async with engine.begin() as conn:
                    await conn.run_sync(
                        lambda sync_conn: graph_store_metadata.create_all(
                            sync_conn, checkfirst=True
                        )
                    )
            if attempt > 1:
                logger.info(
                    "Graph Store DB init succeeded on attempt %d", attempt
                )
            logger.info(
                "Graph Store DB initialised at %s (%s)",
                GRAPH_STORE_DATABASE_URL,
                "alembic" if have_alembic_lineage else "metadata fallback",
            )
            return
        except Exception as exc:  # noqa: BLE001 — classified below
            remaining = deadline - _time.monotonic()
            reason = _permanent_bootstrap_reason(exc)
            if reason is not None:
                err = BootstrapError(reason, exc)
                logger.error("Graph Store bootstrap failed (%s):\n%s", reason, err)
                raise err from exc
            if not _is_transient_db_error(exc) or remaining <= 0:
                raise
            sleep_for = min(delay, remaining)
            logger.warning(
                "Graph Store DB init attempt %d failed (%.0fs budget left, "
                "retrying in %.1fs): %s",
                attempt, remaining, sleep_for, str(exc)[:200],
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 10.0)


async def close_graph_store_db() -> None:
    """Dispose every cached Graph Store engine on shutdown."""
    for role, engine in list(_engines.items()):
        try:
            await engine.dispose()
        except Exception as exc:  # pragma: no cover - best effort on shutdown
            logger.warning("GraphStoreEngine[%s] dispose warning: %s", role.value, exc)
    _engines.clear()
    _session_factories.clear()


def graph_store_pool_status() -> dict[str, dict[str, int | None]]:
    """Snapshot of every materialised Graph Store pool — for the
    bulkhead-isolation regression test (assert graph-store load cannot
    drain the management WEB pool) and the db metrics endpoint."""
    out: dict[str, dict[str, int | None]] = {}
    for role, engine in _engines.items():
        try:
            pool = engine.pool
            out[role.value] = {
                "checked_out": pool.checkedout(),
                "checked_in": pool.checkedin(),
                "overflow": pool.overflow(),
                "size": pool.size(),
            }
        except Exception:
            out[role.value] = {
                "checked_out": None,
                "checked_in": None,
                "overflow": None,
                "size": None,
            }
    return out
