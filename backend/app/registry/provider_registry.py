"""
ProviderRegistry — lazy-initialised, async-safe registry of GraphDataProvider instances.

Workspace-centric: providers are cached by (provider_id, graph_name) tuple.
Legacy connection-based access is preserved for backward compatibility.

Every provider instance handed out is wrapped in a
:class:`CircuitBreakerProxy` so that a failing downstream (FalkorDB
unreachable, Neo4j hung, DataHub 5xx, …) fails fast with
:class:`ProviderUnavailable` instead of stalling the event loop or holding
operational-DB sessions open. The previously hand-rolled 30s negative cache
is removed — the breaker's per-instance state machine subsumes it correctly
under concurrency.
"""
import asyncio
import json
import logging
from typing import Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.adapters import CircuitBreakerProxy
from backend.common.interfaces.provider import GraphDataProvider

logger = logging.getLogger(__name__)


# Circuit-breaker defaults for graph-provider adapters. Tunable via env vars
# if operators need to adjust for a particularly flaky downstream.
import os as _os

_BREAKER_FAIL_MAX = int(_os.getenv("PROVIDER_BREAKER_FAIL_MAX", "5"))
_BREAKER_RESET_TIMEOUT = int(_os.getenv("PROVIDER_BREAKER_RESET_TIMEOUT_SECS", "30"))


def _wrap_in_breaker(provider: GraphDataProvider, name: str) -> GraphDataProvider:
    """Wrap a raw provider in a CircuitBreakerProxy. Returned object is
    type-compatible with :class:`GraphDataProvider` via attribute-forwarding."""
    return CircuitBreakerProxy(  # type: ignore[return-value]
        target=provider,
        name=name,
        fail_max=_BREAKER_FAIL_MAX,
        reset_timeout=_BREAKER_RESET_TIMEOUT,
    )


class ProviderRegistry:
    def __init__(self) -> None:
        # Workspace-centric cache: (provider_id, graph_name) → CircuitBreakerProxy-wrapped provider
        self._providers: Dict[Tuple[str, str], GraphDataProvider] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        # Legacy connection-based cache (kept during migration)
        self._legacy_providers: Dict[str, GraphDataProvider] = {}
        self._legacy_locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Workspace-centric API (new)                                          #
    # ------------------------------------------------------------------ #

    async def get_provider_for_workspace(
        self,
        workspace_id: str,
        session: AsyncSession,
        data_source_id: Optional[str] = None,
    ) -> GraphDataProvider:
        """
        Resolve workspace → data source → (provider_id, graph_name) → cached provider.
        If data_source_id is given, uses that specific source; otherwise uses the primary.
        """
        from ..db.repositories import data_source_repo

        if data_source_id:
            ds = await data_source_repo.get_data_source_orm(session, data_source_id)
            if ds is None:
                raise KeyError(f"Data source not found: {data_source_id}")
        else:
            ds = await data_source_repo.get_primary_data_source(session, workspace_id)
            if ds is None:
                raise KeyError(f"No data source for workspace: {workspace_id}")

        cache_key = (ds.provider_id, ds.graph_name or "")

        if cache_key not in self._locks:
            self._locks[cache_key] = asyncio.Lock()

        async with self._locks[cache_key]:
            if cache_key not in self._providers:
                logger.info(
                    "Instantiating provider for workspace=%s ds=%s provider=%s graph=%s",
                    workspace_id, ds.id, ds.provider_id, ds.graph_name,
                )
                ds_extra = json.loads(ds.extra_config) if getattr(ds, "extra_config", None) else None
                try:
                    raw_provider = await asyncio.wait_for(
                        self._instantiate_from_provider(
                            ds.provider_id, ds.graph_name, session,
                            ds_extra_config=ds_extra,
                        ),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    # Instantiation timed out — no provider to cache. The next
                    # caller retries; if the downstream is still unreachable,
                    # the breaker (wrapping the eventually-cached instance)
                    # will open after a few failures and fast-fail subsequent
                    # calls. We deliberately do not cache a "sick" provider.
                    raise ConnectionError(
                        f"Provider instantiation timed out for {cache_key}"
                    )
                # Wrap in per-instance circuit breaker before caching. After
                # this point every method call on the cached provider flows
                # through the breaker; a dead downstream cannot stall the
                # event loop or hold operational-DB sessions open.
                breaker_name = f"{ds.provider_id}:{ds.graph_name or ''}"
                self._providers[cache_key] = _wrap_in_breaker(raw_provider, breaker_name)

        return self._providers[cache_key]

    # ------------------------------------------------------------------ #
    # Legacy connection-based API (backward compat)                        #
    # ------------------------------------------------------------------ #

    async def get_provider(
        self,
        connection_id: Optional[str] = None,
        session: Optional[AsyncSession] = None,
    ) -> GraphDataProvider:
        """
        Legacy: return a cached provider for a connection_id.
        Kept for backward compatibility during migration.
        """
        if connection_id is None:
            raise ValueError("connection_id is required")

        resolved_id = connection_id

        if resolved_id not in self._legacy_locks:
            self._legacy_locks[resolved_id] = asyncio.Lock()

        async with self._legacy_locks[resolved_id]:
            if resolved_id not in self._legacy_providers:
                logger.info("Instantiating provider for connection_id=%s", resolved_id)
                try:
                    raw_provider = await asyncio.wait_for(
                        self._instantiate_from_connection(
                            resolved_id, session
                        ),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    raise ConnectionError(
                        f"Provider instantiation timed out for connection {resolved_id}"
                    )
                self._legacy_providers[resolved_id] = _wrap_in_breaker(
                    raw_provider, f"legacy:{resolved_id}"
                )

        return self._legacy_providers[resolved_id]

    # ------------------------------------------------------------------ #
    # Eviction                                                             #
    # ------------------------------------------------------------------ #

    async def evict_data_source(self, provider_id: str, graph_name: str) -> None:
        """Evict cached provider for a (provider_id, graph_name) pair."""
        cache_key = (provider_id, graph_name or "")
        provider = self._providers.pop(cache_key, None)
        if provider is not None:
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Error closing provider %s: %s", cache_key, exc)
        self._locks.pop(cache_key, None)
        logger.info("Evicted provider for key=%s", cache_key)

    async def evict_workspace(self, workspace_id: str, session: AsyncSession) -> None:
        """Evict all cached providers for all data sources in a workspace."""
        from ..db.repositories import data_source_repo
        sources = await data_source_repo.list_data_sources(session, workspace_id)
        for ds in sources:
            await self.evict_data_source(ds.provider_id, ds.graph_name or "")

    async def evict_provider(self, provider_id: str) -> None:
        """Evict all cached providers for a given provider_id (any graph_name)."""
        keys_to_evict = [k for k in self._providers if k[0] == provider_id]
        for key in keys_to_evict:
            await self.evict_data_source(key[0], key[1])

    async def evict(self, connection_id: str) -> None:
        """Legacy: evict by connection_id."""
        provider = self._legacy_providers.pop(connection_id, None)
        if provider is not None:
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Error closing provider %s: %s", connection_id, exc)
        self._legacy_locks.pop(connection_id, None)

    async def evict_all(self) -> None:
        """Evict all cached providers (workspace + legacy)."""
        for key in list(self._providers.keys()):
            await self.evict_data_source(key[0], key[1])
        for conn_id in list(self._legacy_providers.keys()):
            await self.evict(conn_id)

    # ------------------------------------------------------------------ #
    # Provider instantiation                                               #
    # ------------------------------------------------------------------ #

    async def _instantiate_from_provider(
        self,
        provider_id: str,
        graph_name: Optional[str],
        session: AsyncSession,
        ds_extra_config: Optional[dict] = None,
    ) -> GraphDataProvider:
        """Instantiate a GraphDataProvider from a ProviderORM row."""
        from ..db.repositories.provider_repo import get_provider_orm, get_credentials

        row = await get_provider_orm(session, provider_id)
        if row is None:
            raise KeyError(f"Provider not found: {provider_id}")

        credentials = await get_credentials(session, provider_id)
        provider_extra = json.loads(row.extra_config) if row.extra_config else None
        # Merge: data-source extra_config overrides provider-level
        merged_extra = self._merge_extra_config(provider_extra, ds_extra_config)
        return self._create_provider_instance(
            row.provider_type, row.host, row.port, graph_name,
            row.tls_enabled, credentials, extra_config=merged_extra,
        )

    async def _instantiate_from_connection(
        self,
        connection_id: str,
        session: Optional[AsyncSession],
    ) -> GraphDataProvider:
        """Legacy: instantiate from a GraphConnectionORM row."""
        from ..db.repositories.connection_repo import get_connection_orm, get_credentials

        if session is None:
            raise RuntimeError(f"Cannot instantiate provider for {connection_id}: no DB session.")

        row = await get_connection_orm(session, connection_id)
        if row is None:
            raise KeyError(f"Connection not found: {connection_id}")

        credentials = await get_credentials(session, connection_id)
        return self._create_provider_instance(
            row.provider_type, row.host, row.port, row.graph_name,
            row.tls_enabled, credentials,
        )

    @staticmethod
    def _merge_extra_config(
        provider_config: Optional[dict],
        datasource_config: Optional[dict],
    ) -> Optional[dict]:
        """Merge provider-level and data-source-level extra_config.
        DataSource values win on conflict (shallow merge at top-level,
        deep merge for ``schemaMapping`` sub-key).
        """
        if not provider_config and not datasource_config:
            return None
        base = dict(provider_config or {})
        override = dict(datasource_config or {})
        # Deep-merge the schemaMapping sub-key
        if "schemaMapping" in base and "schemaMapping" in override:
            merged_mapping = dict(base["schemaMapping"])
            merged_mapping.update(
                {k: v for k, v in override["schemaMapping"].items() if v is not None}
            )
            base.update(override)
            base["schemaMapping"] = merged_mapping
        else:
            base.update(override)
        return base

    def _create_provider_instance(
        self,
        provider_type: str,
        host: Optional[str],
        port: Optional[int],
        graph_name: Optional[str],
        tls_enabled: bool,
        credentials: dict,
        extra_config: Optional[dict] = None,
    ) -> GraphDataProvider:
        """Dispatch to the correct provider constructor."""
        ptype = provider_type.lower()

        if ptype == "falkordb":
            from backend.app.providers.falkordb_provider import FalkorDBProvider
            return FalkorDBProvider(
                host=host or "localhost",
                port=port or 6379,
                graph_name=graph_name or "nexus_lineage",
            )

        elif ptype == "neo4j":
            from backend.graph.adapters.neo4j_provider import Neo4jProvider
            return Neo4jProvider(
                uri=f"{'bolt+s' if tls_enabled else 'bolt'}://{host}:{port or 7687}",
                username=credentials.get("username", "neo4j"),
                password=credentials.get("password", ""),
                database=graph_name or "neo4j",
                extra_config=extra_config,
            )

        elif ptype == "datahub":
            from backend.graph.adapters.datahub_provider import DataHubGraphQLProvider
            return DataHubGraphQLProvider(
                base_url=host or "",
                token=credentials.get("token"),
            )

        elif ptype == "spanner":
            from backend.graph.adapters.spanner_provider import SpannerProvider
            cfg = dict(extra_config or {})
            project_id = cfg.get("projectId") or credentials.get("project_id")
            instance_id = cfg.get("instanceId")
            database_id = cfg.get("databaseId") or graph_name
            if not project_id or not instance_id or not database_id:
                raise ValueError(
                    "Spanner provider requires extra_config.projectId, "
                    "extra_config.instanceId, and (extra_config.databaseId or graph_name)."
                )
            return SpannerProvider(
                project_id=project_id,
                instance_id=instance_id,
                database_id=database_id,
                graph_name=cfg.get("graphName") or "UniViz",
                credentials_json=credentials.get("service_account_json"),
                use_emulator=bool(cfg.get("useEmulator", False)),
                extra_config=cfg,
            )

        raise ValueError(f"Unknown provider_type: {ptype!r}")

# Module-level singleton — used by FastAPI dependency and ContextEngine.
# DEPRECATED: Use provider_manager from backend.app.providers.manager instead.
# This instance is kept for backward compatibility during migration.
provider_registry = ProviderRegistry()

# Re-export the new ProviderManager singleton so code can migrate incrementally.
from backend.app.providers.manager import provider_manager  # noqa: E402, F401
