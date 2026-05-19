"""Alembic environment for the **Graph Store DB** — Postgres v16 only.

Separate lineage / version table from the management DB's
``backend/alembic`` so the Graph Store can be provisioned and migrated
independently (and live on a different instance entirely).

Resolves ``GRAPH_STORE_DB_URL`` (asyncpg form) and rewrites it to its
sync ``postgresql+psycopg2://`` equivalent because Alembic runs sync;
the application keeps using asyncpg at runtime. Dev falls back to the
separate ``synodic_graph`` database on the dev Postgres.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the backend package importable regardless of invocation CWD.
HERE = Path(__file__).resolve().parent           # backend/alembic_graph_store
BACKEND_DIR = HERE.parent                        # backend
REPO_ROOT = BACKEND_DIR.parent                   # repo root
for path in (REPO_ROOT, BACKEND_DIR):
    p = str(path)
    if p not in sys.path:
        sys.path.insert(0, p)

# Import the Graph Store base + every Graph Store ORM module so the
# isolated metadata is fully populated. Add new graph-store model
# modules here as they are introduced.
from backend.app.db.graph_store_engine import GraphStoreBase  # noqa: E402
from backend.app.db import models_graph as _models_graph  # noqa: E402,F401

target_metadata = GraphStoreBase.metadata

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False so init_graph_store_db()'s retry
    # path doesn't silence the app's own loggers (same rationale as the
    # management env.py).
    fileConfig(config.config_file_name, disable_existing_loggers=False)
logger = logging.getLogger("alembic.graph_store.env")


_DEV_FALLBACK_URL = "postgresql+asyncpg://synodic:synodic@localhost:5432/synodic_graph"
_ASYNC_PREFIX = "postgresql+asyncpg://"
_SYNC_PREFIX = "postgresql+psycopg2://"


def _resolve_async_url() -> str:
    url = os.getenv("GRAPH_STORE_DB_URL", _DEV_FALLBACK_URL)
    if not url.startswith(_ASYNC_PREFIX):
        raise RuntimeError(
            f"The Graph Store requires Postgres v16+ via asyncpg. "
            f"GRAPH_STORE_DB_URL must start with '{_ASYNC_PREFIX}' "
            f"(got: {url[:30]!r})."
        )
    return url


def _to_sync_url(async_url: str) -> str:
    return _SYNC_PREFIX + async_url[len(_ASYNC_PREFIX):]


SYNC_DB_URL = _to_sync_url(_resolve_async_url())
config.set_main_option("sqlalchemy.url", SYNC_DB_URL)
logger.info("Graph Store Alembic resolved DB URL: %s", SYNC_DB_URL.split("@")[-1])


def run_migrations_offline() -> None:
    context.configure(
        url=SYNC_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connect_timeout = int(os.getenv("DB_CONNECT_TIMEOUT_SECS", "5"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": connect_timeout},
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
