"""
FalkorDB graph provider - persists graph data in FalkorDB and loads it via the application.
Implements GraphDataProvider interface using FalkorDB async client and Cypher queries.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, List, Optional, Dict, Any, Set, Tuple

from ..models.graph import (
    GraphNode, GraphEdge, NodeQuery, EdgeQuery,
    LineageResult, GraphSchemaStats,
    PropertyFilter, TagFilter, TextFilter, FilterOperator,
    EntityTypeSummary, EdgeTypeSummary, TagSummary,
    OntologyMetadata, EdgeTypeMetadata, EntityTypeHierarchy,
    AggregatedEdgeResult, AggregatedEdgeInfo,
    ChildrenWithEdgesResult, TopLevelNodesResult,
    TraceResult, TraceFocus,
)
from .base import GraphDataProvider
from backend.common.interfaces.provider import ProviderConfigurationError

logger = logging.getLogger(__name__)


class AggregationBatchAbort(Exception):
    """Raised when sustained provider failure makes continuing pointless.

    The worker's outer try/except marks the job ``status=failed`` and
    preserves ``last_cursor`` so the job can be resumed once the
    provider recovers.
    """


def _sanitize_label(s: str) -> str:
    """Sanitize string for use as FalkorDB label/relationship type (alphanumeric + underscore)."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(s))


def _node_from_props(props: Dict[str, Any], entity_type_str: Optional[str] = None) -> Optional[GraphNode]:
    """Build GraphNode from FalkorDB node properties."""
    if not props or "urn" not in props:
        return None
    entity_type = entity_type_str or props.get("entityType", "unknown")
    try:
        return GraphNode(
            urn=props["urn"],
            entityType=str(entity_type),
            displayName=props.get("displayName", ""),
            qualifiedName=props.get("qualifiedName"),
            description=props.get("description"),
            properties=json.loads(props["properties"]) if isinstance(props.get("properties"), str) else (props.get("properties") or {}),
            tags=json.loads(props["tags"]) if isinstance(props.get("tags"), str) else (props.get("tags") or []),
            layerAssignment=props.get("layerAssignment"),
            childCount=props.get("childCount"),
            sourceSystem=props.get("sourceSystem"),
            lastSyncedAt=props.get("lastSyncedAt"),
        )
    except Exception as e:
        logger.warning(f"Failed to build GraphNode from props: {e}")
        return None


def _edge_from_row(source_urn: str, target_urn: str, rel_type: str, props: Dict[str, Any]) -> GraphEdge:
    """Build GraphEdge from FalkorDB edge data."""
    edge_id = props.get("id") or f"{source_urn}|{rel_type}|{target_urn}"
    return GraphEdge(
        id=edge_id,
        sourceUrn=source_urn,
        targetUrn=target_urn,
        edgeType=str(rel_type),
        confidence=props.get("confidence"),
        properties=json.loads(props["properties"]) if isinstance(props.get("properties"), str) else (props.get("properties") or {}),
    )


class FalkorDBProvider(GraphDataProvider):
    """
    Graph data provider backed by FalkorDB.
    Schema: nodes have label = entityType, properties include urn, displayName, etc.
    Edges use relationship type = edgeType (CONTAINS, PRODUCES, etc.).
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        graph_name: str = "nexus_lineage",
        seed_file: Optional[str] = None,
        projection_mode: str = "in_source",
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self._host = host
        self._port = port
        self._graph_name = graph_name
        self._seed_file = seed_file
        self._projection_mode = projection_mode  # "in_source" or "dedicated"
        # P1.6 — credentials previously dropped silently in
        # ProviderManager._create_provider_instance, causing NOAUTH errors
        # to be mis-classified as network failures and triggering false
        # breaker storms. They're now plumbed end-to-end:
        #   __init__ → preflight (RESP AUTH before PING)
        #            → _ensure_connected (driver auth via from_url args)
        self._username = username
        self._password = password
        self._graph = None
        self._proj_graph = None  # Dedicated projection graph (when mode = "dedicated")
        self._pool = None       # Graph query pool (used by FalkorDB)
        self._redis_pool = None  # Separate pool for Redis data-structure ops (caching, SADD, etc.)
        self._db = None
        # P2.3 — graceful cache-disable mode. When the cache Redis is
        # unreachable but the FalkorDB graph is fine, set this to False
        # so cache reads return None silently and cache writes are
        # dropped. Provider works DEGRADED (slower reads, no
        # materialization tracking) but does NOT fail availability —
        # mirroring Neo4j's pattern at line 271-276 of neo4j_provider.py.
        self._redis_available: bool = True

    @property
    def _proj(self):
        """Transparent access to the projection graph.

        When projection_mode is "in_source", AGGREGATED edges live in the
        same graph as source data. When "dedicated", they go to a separate
        graph key (e.g. nexus_lineage_proj) on the same Redis instance.
        """
        if self._projection_mode == "dedicated" and self._proj_graph is not None:
            return self._proj_graph
        return self._graph

    async def preflight(self, *, deadline_s: float = 1.5):
        """Fast reachability probe — TCP connect + Redis PING within
        ``deadline_s``. Does NOT touch the production pool, does NOT run
        any DDL. Returns a ``PreflightResult``; never raises for network
        failure.

        The ``/test`` admin endpoint and the manager's preflight gate
        invoke this before any expensive driver work, so an unreachable
        host fails fast (≤1.5s) instead of triggering 30-45s of half-
        blocking init in ``_ensure_connected``.

        P1.6 — credential plumbing: when a password is configured,
        ``redis_ping_preflight`` runs ``AUTH`` before ``PING``. Without
        this, an auth-protected FalkorDB would fail preflight with
        NOAUTH and trigger the same false breaker storm we're trying to
        prevent for unreachable hosts.
        """
        from backend.common.interfaces.preflight import redis_ping_preflight
        return await redis_ping_preflight(
            self._host, self._port,
            deadline_s=deadline_s,
            password=self._password,
        )

    async def _ensure_connected(self):
        """Lazy connection to FalkorDB.

        Schema reconciliation (``ensure_indices``, ``ensure_projections``)
        is intentionally NOT run here — it is dispatched as a fire-and-
        forget background task on first successful connect so a slow DDL
        sweep cannot extend the request-path budget. See
        ``_schedule_reconcile_once`` below.
        """
        if self._graph is not None:
            return
        try:
            # Non-blocking ConnectionPool: on exhaustion raises ConnectionError
            # immediately instead of blocking the caller (and, for asyncio
            # BlockingConnectionPool, stalling the event loop while waiting
            # on a semaphore inside the loop itself). The circuit-breaker
            # proxy around this provider translates the failure into
            # ProviderUnavailable before it reaches the web tier.
            from redis.asyncio import ConnectionPool, Redis
            from falkordb.asyncio import FalkorDB

            # Pool for graph (Cypher) queries — used by FalkorDB client.
            # FALKORDB_SOCKET_TIMEOUT controls how long a single Cypher
            # query can run before the socket times out.  The default 10s
            # is generous for normal reads but necessary for batch MERGE
            # operations in the aggregation worker.  The old 3s default
            # caused "Timeout reading from localhost:6379" on any
            # moderately-sized UNWIND+MERGE, tripping the circuit breaker
            # and killing the entire provider (including API traffic).
            graph_pool_size = int(os.getenv("FALKORDB_GRAPH_POOL_SIZE", "24"))
            socket_timeout = float(os.getenv("FALKORDB_SOCKET_TIMEOUT", "10"))
            # P1.6 — auth credentials propagated to the driver. When the
            # operator has configured a password on the FalkorDB instance,
            # the ConnectionPool issues AUTH transparently on every new
            # connection. Without this, queries return NOAUTH, the breaker
            # mis-classifies as a network failure, and we trip a false
            # outage.
            _pool_kwargs: dict = {
                "host": self._host,
                "port": self._port,
                "max_connections": graph_pool_size,
                "socket_connect_timeout": 2.0,
                "socket_timeout": socket_timeout,
                "decode_responses": True,
            }
            if self._username:
                _pool_kwargs["username"] = self._username
            if self._password:
                _pool_kwargs["password"] = self._password
            self._pool = ConnectionPool(**_pool_kwargs)
            # Redis for non-graph ops (caching, materialization tracking,
            # ancestor chains, stats). When CACHE_REDIS_URL is set, these
            # go to a DEDICATED Redis instance — fully decoupled from
            # FalkorDB so a graph outage doesn't take out caching/state.
            # When unset, falls back to the FalkorDB instance (dev compat).
            from backend.common.adapters import TimeoutRedis
            cache_redis_url = os.getenv("CACHE_REDIS_URL")
            redis_pool_size = int(os.getenv("FALKORDB_REDIS_POOL_SIZE", "16"))
            redis_op_timeout = float(os.getenv("FALKORDB_REDIS_OP_TIMEOUT", "3"))
            # P2.3 — cache Redis is a BEST-EFFORT dependency. Wrapped in
            # its own try/except so an unreachable cache Redis sets
            # ``self._redis_available=False`` and degrades gracefully
            # instead of taking the whole provider down. Graph queries
            # (the load-bearing path) still work; cache misses just go
            # to the source. Without this, a cache Redis outage kills
            # FalkorDB availability even when FalkorDB itself is healthy.
            self._redis_available = True
            try:
                if cache_redis_url:
                    # CACHE_REDIS_URL embeds its own auth (e.g.
                    # redis://:password@host:port/0) so we don't pass our
                    # FalkorDB password here — the cache Redis is a separate
                    # instance that may have separate credentials.
                    _raw_redis = Redis.from_url(
                        cache_redis_url,
                        max_connections=redis_pool_size,
                        socket_connect_timeout=2.0,
                        socket_timeout=socket_timeout,
                        decode_responses=True,
                    )
                    self._redis_pool = None  # managed by from_url
                else:
                    # Cache Redis falls back to the FalkorDB instance — same
                    # host, same auth (P1.6).
                    _redis_pool_kwargs: dict = {
                        "host": self._host,
                        "port": self._port,
                        "max_connections": redis_pool_size,
                        "socket_connect_timeout": 2.0,
                        "socket_timeout": socket_timeout,
                        "decode_responses": True,
                    }
                    if self._username:
                        _redis_pool_kwargs["username"] = self._username
                    if self._password:
                        _redis_pool_kwargs["password"] = self._password
                    self._redis_pool = ConnectionPool(**_redis_pool_kwargs)
                    _raw_redis = Redis(connection_pool=self._redis_pool)
                # Wrap in TimeoutRedis — every async call and pipeline.execute()
                # automatically gets an asyncio.wait_for() deadline. No call-site
                # wrapping needed. See backend/common/adapters/timeout_redis.py.
                self._redis = TimeoutRedis(_raw_redis, timeout=redis_op_timeout)
            except Exception as exc:
                # Cache Redis construction failed. Provider continues
                # without cache; queries are slower but available.
                logger.warning(
                    "FalkorDB cache Redis unavailable (%s) — provider running "
                    "in cache-disabled mode (DEGRADED).", exc,
                )
                self._redis = None
                self._redis_available = False
            self._db = FalkorDB(connection_pool=self._pool)
            self._graph = self._db.select_graph(self._graph_name)

            # Set up projection graph if using dedicated mode
            if self._projection_mode == "dedicated":
                self._proj_graph = self._db.select_graph(f"{self._graph_name}_proj")

            # Verify the pool with one cheap round-trip — if this fails, we
            # treat the connect as failed and the caller's circuit breaker
            # records it. Bounded so a half-open socket cannot stall the
            # connect path.
            _init_timeout = float(os.getenv("FALKORDB_INIT_TIMEOUT", "3"))
            await asyncio.wait_for(
                self._graph.ro_query("RETURN 1", params={}),
                timeout=_init_timeout,
            )

            # Schema reconciliation runs OFF the request path. Fire-and-
            # forget background task; failures are logged but do not affect
            # connect outcome. Subsequent connects are no-ops because of the
            # ``_graph is not None`` guard above, so reconcile fires once
            # per provider instance, not once per query.
            self._schedule_reconcile_once()

            # Optional lazy seed (cheap when graph is non-empty; bounded by
            # the same init_timeout for the count query).
            if self._seed_file:
                count_result = await asyncio.wait_for(
                    self._graph.ro_query("MATCH (n) RETURN count(n) AS c", params={}),
                    timeout=_init_timeout,
                )
                if count_result.result_set and count_result.result_set[0][0] == 0:
                    await self._seed_from_file()
        except Exception as e:
            logger.error(f"FalkorDB connection failed: {e}")
            raise

    def _schedule_reconcile_once(self) -> None:
        """Schedule ``ensure_indices`` + ``ensure_projections`` as a
        background task. Idempotent — guarded by ``_reconcile_started``.

        Failures are logged at WARNING and do NOT raise into the connect
        path. The next call requiring a missing index will surface a
        logical error from the query, which is the correct signal — not
        a 30-45s connect-time stall.
        """
        if getattr(self, "_reconcile_started", False):
            return
        self._reconcile_started = True

        async def _run():
            try:
                await self.ensure_indices()
                await self.ensure_projections()
                logger.info("FalkorDB reconcile complete (host=%s port=%s)", self._host, self._port)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "FalkorDB reconcile failed (host=%s port=%s): %s — provider remains usable",
                    self._host, self._port, exc,
                )

        # Detach the task — we don't await it. Hold a reference to prevent
        # GC under Python's "task may be GC'd before completion" rule.
        self._reconcile_task = asyncio.create_task(
            _run(), name=f"falkordb-reconcile-{self._host}:{self._port}"
        )

    # ── Timeout-guarded query helpers ────────────────────────────────
    # Every Cypher query routed through these methods gets an
    # asyncio.wait_for() deadline. TimeoutError is a network-class
    # exception — the CircuitBreakerProxy counts it toward the failure
    # budget and opens the breaker after fail_max consecutive failures.
    # See backend/app/config/resilience.py for full reference of all tunables.
    _READ_TIMEOUT = float(os.getenv("FALKORDB_QUERY_TIMEOUT", "5"))
    _WRITE_TIMEOUT = float(os.getenv("FALKORDB_WRITE_TIMEOUT", "15"))

    async def _ro_query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded read-only query on the source graph."""
        t = timeout if timeout is not None else self._READ_TIMEOUT
        return await asyncio.wait_for(
            self._graph.ro_query(cypher, params=params or {}),
            timeout=t,
        )

    async def _query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded write query on the source graph."""
        t = timeout if timeout is not None else self._WRITE_TIMEOUT
        return await asyncio.wait_for(
            self._graph.query(cypher, params=params or {}),
            timeout=t,
        )

    async def _proj_ro_query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded read-only query on the projection graph."""
        t = timeout if timeout is not None else self._READ_TIMEOUT
        return await asyncio.wait_for(
            self._proj.ro_query(cypher, params=params or {}),
            timeout=t,
        )

    async def _proj_query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded write query on the projection graph."""
        t = timeout if timeout is not None else self._WRITE_TIMEOUT
        return await asyncio.wait_for(
            self._proj.query(cypher, params=params or {}),
            timeout=t,
        )

    async def _seed_from_file(self):
        """Load graph from seed JSON file if graph is empty."""
        import os as _os
        path = self._seed_file
        if not path or not _os.path.exists(path):
            logger.warning(f"Seed file not found: {path}")
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            nodes = [GraphNode(**n) for n in data.get("nodes", [])]
            edges = [GraphEdge(**e) for e in data.get("edges", [])]
            # Limit for large files
            if len(nodes) > 50000:
                nodes = nodes[:50000]
            if len(edges) > 100000:
                edges = edges[:100000]
            await self.save_custom_graph(nodes, edges)
            logger.info(f"Seeded {len(nodes)} nodes and {len(edges)} edges from {path}")
        except Exception as e:
            logger.error(f"Seed failed: {e}")

    async def ensure_indices(self, entity_type_ids: Optional[List[str]] = None):
        """Create indices for node labels and properties.

        When *entity_type_ids* is provided (e.g. from the resolved ontology),
        those labels are indexed in addition to the hardcoded defaults.
        """
        default_labels = [
            "domain",
            "dataPlatform",
            "container",
            "dataset",
            "schemaField",
        ]
        extra = list(entity_type_ids) if entity_type_ids else []
        seen: set[str] = set()
        labels: list[str] = []
        for lbl in default_labels + extra:
            if lbl not in seen:
                seen.add(lbl)
                labels.append(lbl)

        # `level` indexed for trace queries that filter by hierarchy level
        # (Cypher: WHERE n.level = $level). Idempotent CREATE INDEX is fine
        # if the index already exists.
        properties = ["urn", "displayName", "qualifiedName", "level"]

        _init_timeout = float(os.getenv("FALKORDB_INIT_TIMEOUT", "3"))
        for label in labels:
            for prop in properties:
                try:
                    await asyncio.wait_for(
                        self._graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})"),
                        timeout=_init_timeout,
                    )
                except Exception:
                    pass

    @property
    def name(self) -> str:
        return "FalkorDBProvider"

    def set_containment_edge_types(self, types: List[str], from_ontology: bool = True) -> None:
        """Called by ContextEngine after ontology resolution to inject the
        authoritative containment edge types from the resolver.

        Parameters
        ----------
        types : list
            The containment edge types. Empty list means the ontology explicitly
            defines no containment types (flat graph, no hierarchy).
        from_ontology : bool
            True if these came from a real ontology definition (assigned or system).
            False if from introspection-only — an empty list should NOT suppress
            the hardcoded fallback.

        When the resolved type set actually changes, the per-graph
        ancestors Redis cache is asynchronously invalidated as a
        defence-in-depth against caller paths that bypass the worker's
        explicit ``reset_ancestors_cache`` call (e.g. ContextEngine
        re-resolving an ontology at request time on the same sticky
        provider instance).
        """
        if from_ontology or types:
            new_set: Set[str] = {t.upper() for t in types}
            old_set = getattr(self, "_resolved_containment_types", None)
            self._resolved_containment_types = new_set
            self._resolved_containment_types_set = True
            if old_set is not None and old_set != new_set:
                # Sync method, async work — only schedule if there is a
                # running loop. Outside one (test harness, sync init)
                # the worker's explicit reset covers production.
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.reset_ancestors_cache())
                except RuntimeError:
                    pass
        # else: introspection-only with no containment found — don't set sentinel

    def set_entity_type_levels(self, mapping: Dict[str, int]) -> None:
        """Called by ContextEngine after ontology resolution to inject the
        entity-type → hierarchy.level mapping. Used both at write time
        (populates ``n.level`` on upsert for the level index) and at read
        time (resolves levels via ``labels(n)[0]`` so trace queries work
        even when ``n.level`` hasn't been backfilled on existing nodes).
        """
        self._entity_type_levels: Dict[str, int] = dict(mapping)

    def _get_node_level(self, entity_type: Any) -> Optional[int]:
        """Resolve a node's hierarchy level from the cached mapping. Returns
        None when ontology hasn't been resolved or the entity type is unknown
        — backfill or read-time fallback handles those cases.
        """
        mapping = getattr(self, "_entity_type_levels", None)
        if not mapping:
            return None
        return mapping.get(str(entity_type))

    def _types_at_level(self, level: int) -> List[str]:
        """Return entity-type IDs whose ontology hierarchy.level == ``level``.

        Used by trace/expand to filter via ``labels(n)[0] IN $typesAtLevel``
        instead of ``n.level = $level`` — the label-based filter works
        immediately on every existing graph (labels are written at upsert),
        whereas ``n.level`` only works after backfill_node_levels.py runs.
        """
        mapping = getattr(self, "_entity_type_levels", None) or {}
        return [t for t, lvl in mapping.items() if lvl == level]

    async def set_projection_mode(self, mode: str) -> None:
        """Dynamically switch the projection target for aggregation operations.

        Because provider instances are cached and shared across data sources,
        projection_mode cannot be baked into the constructor.  The aggregation
        worker calls this per-job to route AGGREGATED edges to the correct
        graph (source or dedicated ``{graph_name}_proj``).

        Must be called AFTER ``_ensure_connected()`` so ``self._db`` is ready.
        """
        await self._ensure_connected()
        old = self._projection_mode
        self._projection_mode = mode
        if mode == "dedicated":
            if self._proj_graph is None:
                self._proj_graph = self._db.select_graph(f"{self._graph_name}_proj")
        else:
            # Switching back to in_source — clear proj_graph so _proj returns _graph
            self._proj_graph = None
        logger.info(
            "Projection mode changed %s → %s for graph %s",
            old, mode, self._graph_name,
        )

    def set_resolved_edge_metadata(
        self,
        edge_type_metadata: Dict[str, Any],
        lineage_edge_types: List[str],
    ) -> None:
        """Called by ContextEngine after ontology resolution to inject the
        authoritative edge classification from the resolver.
        When set, get_ontology_metadata() uses this instead of
        re-deriving from env vars and hardcoded type names.
        """
        self._resolved_edge_metadata = {k.upper(): v for k, v in edge_type_metadata.items()}
        self._resolved_lineage_types: Set[str] = {t.upper() for t in lineage_edge_types}
        self._resolved_edge_metadata_set = True

    def _get_containment_edge_types(self) -> Set[str]:
        """Return the authoritative containment edge type set.

        Single source of truth: the ontology-resolved types injected by
        ContextEngine / aggregation. Empty is a valid resolved state
        (flat graph with no containment hierarchy). Anything else
        raises ``ProviderConfigurationError`` — silently defaulting in
        a multi-tenant system masks ontology-coverage bugs the
        resolution gate is meant to surface.

        The legacy ``CONTAINMENT_EDGE_TYPES`` env-var fallback was
        removed: it was an operator escape hatch from the era before
        the resolution gate, and it lets aggregation paths bypass the
        per-data-source ontology assignment. Operators that need to
        configure containment now do so by editing the ontology.
        """
        if getattr(self, "_resolved_containment_types_set", False):
            return self._resolved_containment_types
        raise ProviderConfigurationError(
            "Containment edge types are not configured for this provider. "
            "ContextEngine / aggregation must call set_containment_edge_types() "
            "with the resolved ontology before invoking provider methods that "
            "depend on containment classification."
        )

    def _extract_node_from_result(self, row) -> Optional[GraphNode]:
        """Extract GraphNode from a FalkorDB result row (Node or dict of properties)."""
        if not row:
            return None
        cell = row[0] if isinstance(row, (list, tuple)) else row
        if hasattr(cell, "properties"):
            props = cell.properties or {}
            labels = getattr(cell, "labels", None) or []
            entity_type = labels[0] if labels else props.get("entityType", "unknown")
            return _node_from_props(props, entity_type)
        if isinstance(cell, dict):
            return _node_from_props(cell)
        return None

    # ---- URN → label cache (Redis Hash) ----

    def _urn_label_key(self) -> str:
        return f"{self._graph_name}:urn_labels"

    def _agg_last_materialized_key(self) -> str:
        return f"{self._graph_name}:agg:last_materialized_at"

    def _agg_in_flight_key(self, ds_id: str) -> str:
        return f"materialize:in-flight:{ds_id}"

    async def _cache_urn_label(self, urn: str, label: str) -> None:
        """Store a single urn→label mapping."""
        try:
            await self._redis.hset(self._urn_label_key(), urn, label)
        except Exception:
            pass  # best-effort

    async def _cache_urn_labels_bulk(self, mapping: Dict[str, str]) -> None:
        """Bulk-store urn→label mappings via pipeline."""
        if not mapping:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            key = self._urn_label_key()
            for urn, label in mapping.items():
                pipe.hset(key, urn, label)
            await pipe.execute()
        except Exception:
            pass  # best-effort

    async def _get_cached_label(self, urn: str) -> Optional[str]:
        """Look up the label for a URN from Redis cache."""
        try:
            return await self._redis.hget(self._urn_label_key(), urn)
        except Exception:
            return None

    async def get_node(self, urn: str) -> Optional[GraphNode]:
        await self._ensure_connected()

        # Try label-aware lookup first (index-assisted, 10-50x faster)
        label = await self._get_cached_label(urn)
        if label:
            result = await self._ro_query(
                f"MATCH (n:{_sanitize_label(label)} {{urn: $urn}}) RETURN n",
                params={"urn": urn},
            )
            if result.result_set and len(result.result_set) > 0:
                return self._extract_node_from_result(result.result_set[0])

        # Fallback: label-less scan (still works, just slower)
        result = await self._ro_query(
            "MATCH (n) WHERE n.urn = $urn RETURN n",
            params={"urn": urn},
        )
        if result.result_set and len(result.result_set) > 0:
            node = self._extract_node_from_result(result.result_set[0])
            # Backfill the cache for next time
            if node:
                await self._cache_urn_label(urn, str(node.entity_type))
            return node
        return None

    async def get_nodes(self, query: NodeQuery) -> List[GraphNode]:
        await self._ensure_connected()

        params: Dict[str, Any] = {}
        conditions = []

        # Label-indexed matching: use per-label MATCH with UNION for O(1) index lookup
        # instead of MATCH (n) WHERE toLower(labels(n)[0]) IN $types which scans all nodes.
        use_label_union = bool(query.entity_types) and not query.urns
        if use_label_union:
            types = [str(t) for t in query.entity_types]
            # Build per-label conditions (shared across all UNION branches)
            shared_conditions = []
        else:
            shared_conditions = None  # not used

        if not use_label_union:
            if query.entity_types:
                # Fallback for combined entity_types + urns queries
                types_lower = [t.lower() for t in [str(t) for t in query.entity_types]]
                params["entityTypesLower"] = types_lower
                conditions.append("toLower(labels(n)[0]) IN $entityTypesLower")

        if query.urns:
            if len(query.urns) == 1:
                conditions.append("n.urn = $urn0")
                params["urn0"] = query.urns[0]
            else:
                params["urnList"] = query.urns
                conditions.append("n.urn IN $urnList")

        if query.tags:
            # Tags stored as JSON array string - match quoted tag in JSON
            params["tagVal"] = json.dumps(query.tags[0])
            tag_cond = "(n.tags IS NOT NULL AND n.tags CONTAINS $tagVal)"
            conditions.append(tag_cond)
            if shared_conditions is not None:
                shared_conditions.append(tag_cond)

        if query.search_query:
            params["search"] = query.search_query.lower()
            search_cond = "(toLower(toString(n.displayName)) CONTAINS $search OR toLower(toString(n.urn)) CONTAINS $search)"
            conditions.append(search_cond)
            if shared_conditions is not None:
                shared_conditions.append(search_cond)

        offset = int(query.offset or 0)
        limit = query.limit or 100
        params["skip"] = offset
        params["limit"] = limit

        # Child count: only compute when needed (skip for bulk lineage fetches)
        include_child_count = query.include_child_count

        if use_label_union:
            # Build UNION query with per-label MATCH clauses (uses FalkorDB label indices)
            where_suffix = (" WHERE " + " AND ".join(shared_conditions)) if shared_conditions else ""
            union_branches = []
            for t in types:
                safe_label = _sanitize_label(t)
                union_branches.append(f"MATCH (n:{safe_label}){where_suffix} RETURN n")
            # Wrap in subquery pattern: UNION all branches, then paginate + child count
            inner = " UNION ".join(union_branches)
            if include_child_count:
                containment = list(self._get_containment_edge_types())
                containment_rel_types = "|".join([_sanitize_label(t) for t in containment])
                if containment_rel_types:
                    cypher = (
                        f"CALL {{ {inner} }} "
                        f"WITH n ORDER BY n.displayName SKIP $skip LIMIT $limit "
                        f"OPTIONAL MATCH (n)-[:{containment_rel_types}]->(child) "
                        f"RETURN n, count(child) as childCount"
                    )
                else:
                    cypher = (
                        f"CALL {{ {inner} }} "
                        f"WITH n ORDER BY n.displayName SKIP $skip LIMIT $limit "
                        f"RETURN n, 0 as childCount"
                    )
            else:
                cypher = (
                    f"CALL {{ {inner} }} "
                    f"WITH n ORDER BY n.displayName SKIP $skip LIMIT $limit "
                    f"RETURN n"
                )
        else:
            # Original non-UNION path (URN lookups, no entity_types, etc.)
            clauses = ["MATCH (n)"]
            if conditions:
                clauses.append("WHERE " + " AND ".join(conditions))

            if include_child_count:
                containment = list(self._get_containment_edge_types())
                containment_rel_types = "|".join([_sanitize_label(t) for t in containment])
                clauses.append("WITH n SKIP $skip LIMIT $limit")
                if containment_rel_types:
                    clauses.append(f"OPTIONAL MATCH (n)-[:{containment_rel_types}]->(child)")
                    clauses.append("RETURN n, count(child) as childCount")
                else:
                    clauses.append("RETURN n, 0 as childCount")
            else:
                clauses.append("RETURN n SKIP $skip LIMIT $limit")

            cypher = " ".join(clauses)

        try:
            result = await self._ro_query(cypher, params=params)
        except Exception as e:
            logger.warning(f"get_nodes query failed: {e}")
            return []

        nodes = []
        for row in (result.result_set or []):
            if include_child_count:
                n = self._extract_node_from_result(row[0])
                child_count = row[1]
            else:
                n = self._extract_node_from_result(row)
                child_count = None
            if n:
                if query.property_filters and not self._match_property_filters(n, query.property_filters):
                    continue
                if query.tag_filters and not self._match_tag_filters(n, query.tag_filters):
                    continue
                if query.name_filter and not self._match_text_filter(n.display_name, query.name_filter):
                    continue

                # Apply dynamic child count when available
                if child_count is not None:
                    n.child_count = int(child_count)
                    if n.properties:
                        n.properties['childCount'] = int(child_count)

                nodes.append(n)
                if len(nodes) >= limit:
                    break
        return nodes

    def _match_property_filters(self, node: GraphNode, filters: List[PropertyFilter]) -> bool:
        for f in filters:
            val = node.properties.get(f.field)
            if hasattr(node, f.field):
                val = getattr(node, f.field)
            if not self._match_operator(val, f.operator, f.value):
                return False
        return True

    def _match_operator(self, actual: Any, op: FilterOperator, target: Any) -> bool:
        if op == FilterOperator.EXISTS:
            return actual is not None
        if op == FilterOperator.NOT_EXISTS:
            return actual is None
        if actual is None:
            return False
        if op == FilterOperator.EQUALS:
            return actual == target
        if op == FilterOperator.CONTAINS:
            return str(target).lower() in str(actual).lower()
        if op == FilterOperator.STARTS_WITH:
            return str(actual).lower().startswith(str(target).lower())
        if op == FilterOperator.ENDS_WITH:
            return str(actual).lower().endswith(str(target).lower())
        try:
            if op == FilterOperator.GT:
                return actual > target
            if op == FilterOperator.LT:
                return actual < target
        except Exception:
            return False
        if op == FilterOperator.IN:
            return isinstance(target, list) and actual in target
        if op == FilterOperator.NOT_IN:
            return isinstance(target, list) and actual not in target
        return True

    def _match_tag_filters(self, node: GraphNode, filter: TagFilter) -> bool:
        node_tags = set(node.tags or [])
        target_tags = set(filter.tags)
        if filter.mode == "any":
            return not node_tags.isdisjoint(target_tags)
        if filter.mode == "all":
            return target_tags.issubset(node_tags)
        if filter.mode == "none":
            return node_tags.isdisjoint(target_tags)
        return True

    def _match_text_filter(self, text: str, filter: TextFilter) -> bool:
        t = text if filter.case_sensitive else text.lower()
        q = filter.text if filter.case_sensitive else filter.text.lower()
        if filter.operator == "equals":
            return t == q
        if filter.operator == "contains":
            return q in t
        if filter.operator == "startsWith":
            return t.startswith(q)
        if filter.operator == "endsWith":
            return t.endswith(q)
        return True

    async def search_nodes(self, query: str, limit: int = 10, offset: int = 0) -> List[GraphNode]:
        q = NodeQuery(search_query=query, limit=limit, offset=offset)
        return await self.get_nodes(q)

    async def get_edges(self, query: EdgeQuery) -> List[GraphEdge]:
        await self._ensure_connected()

        cypher = "MATCH (a)-[r]->(b)"
        params: Dict[str, Any] = {}
        conditions: List[str] = []

        if query.source_urns:
            params["sourceUrns"] = query.source_urns
            conditions.append("a.urn IN $sourceUrns")
        if query.target_urns:
            params["targetUrns"] = query.target_urns
            conditions.append("b.urn IN $targetUrns")
        if query.any_urns:
            params["anyUrns"] = query.any_urns
            conditions.append("(a.urn IN $anyUrns OR b.urn IN $anyUrns)")
        if query.edge_types:
            types = [t.value if hasattr(t, "value") else str(t) for t in query.edge_types]
            params["edgeTypes"] = types
            conditions.append("type(r) IN $edgeTypes")
        if query.min_confidence is not None:
            params["minConf"] = query.min_confidence
            conditions.append("r.confidence >= $minConf")

        if conditions:
            cypher += " WHERE " + " AND ".join(conditions)

        offset = query.offset or 0
        limit = query.limit or 100
        params["skip"] = offset
        params["limit"] = limit
        cypher += " RETURN a.urn AS src, b.urn AS tgt, type(r) AS relType, properties(r) AS rprops SKIP $skip LIMIT $limit"

        result = await self._ro_query(cypher, params=params)
        edges = []
        for row in (result.result_set or []):
            src, tgt, rel_type, rprops = row[0], row[1], row[2], (row[3] or {})
            edges.append(_edge_from_row(src, tgt, rel_type, rprops))
        return edges

    async def get_children(
        self,
        parent_urn: str,
        entity_types: Optional[List[str]] = None,
        edge_types: Optional[List[str]] = None,
        search_query: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
        sort_property: Optional[str] = "displayName",
        cursor: Optional[str] = None,
    ) -> List[GraphNode]:
        await self._ensure_connected()
        # None = caller didn't specify, use ontology/fallback; [] = explicitly no containment
        target_edge_types = set(edge_types) if edge_types is not None else set(self._get_containment_edge_types())
        rel_list = list(target_edge_types)
        if not rel_list:
            # No containment types defined — hierarchy is flat, no children exist
            return []

        search_where = ""
        params: Dict[str, Any] = {"parent": parent_urn, "lim": limit, "relTypes": rel_list}

        if search_query:
            search_where = "AND (toLower(c.displayName) CONTAINS toLower($searchQuery) OR toLower(c.urn) CONTAINS toLower($searchQuery)) "
            params["searchQuery"] = search_query

        # Cursor-based pagination: use WHERE c.displayName > $cursor instead of SKIP
        # This is O(log N) with FalkorDB indices vs O(N) for SKIP-based pagination.
        cursor_where = ""
        if cursor:
            cursor_where = "AND c.displayName > $cursor "
            params["cursor"] = cursor
        else:
            # Fallback to offset when no cursor (first page or legacy callers)
            params["skip"] = offset

        # Build ORDER BY suffix for the WITH clause
        order_suffix = ""
        if sort_property:
            safe_prop = _sanitize_label(sort_property)
            order_suffix = f" ORDER BY c.{safe_prop}"

        # Use SKIP only when no cursor is provided (first page)
        skip_clause = "" if cursor else " SKIP $skip"

        if len(rel_list) == 1:
            rel = _sanitize_label(rel_list[0])
            cypher = (
                f"MATCH (p)-[r:{rel}]->(c) "
                f"WHERE p.urn = $parent {search_where}{cursor_where}"
                f"WITH c{order_suffix}{skip_clause} LIMIT $lim "
                f"OPTIONAL MATCH (c)-[rc]->(gc) WHERE type(rc) IN $relTypes "
                f"RETURN c, count(gc) as childCount"
            )
        else:
            cypher = (
                f"MATCH (p)-[r]->(c) "
                f"WHERE p.urn = $parent AND type(r) IN $relTypes {search_where}{cursor_where}"
                f"WITH c{order_suffix}{skip_clause} LIMIT $lim "
                f"OPTIONAL MATCH (c)-[rc]->(gc) WHERE type(rc) IN $relTypes "
                f"RETURN c, count(gc) as childCount"
            )

        result = await self._ro_query(cypher, params=params)
        nodes = []
        for row in (result.result_set or []):
            # Extract node and childCount
            n = self._extract_node_from_result(row[0])
            child_count = row[1]
            if n and (not entity_types or n.entity_type in entity_types):
                # Valid dynamic child count overrides static property if present, or fills gap
                if child_count is not None:
                    n.child_count = int(child_count)
                    # Also update properties so it serializes correctly if needed (though Pydantic model uses field)
                    if n.properties:
                        n.properties['childCount'] = int(child_count)
                nodes.append(n)
        return nodes

    async def get_children_with_edges(
        self,
        parent_urn: str,
        edge_types: Optional[List[str]] = None,
        lineage_edge_types: Optional[List[str]] = None,
        search_query: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
        include_lineage_edges: bool = True,
        sort_property: Optional[str] = "displayName",
        cursor: Optional[str] = None,
    ) -> ChildrenWithEdgesResult:
        """Optimized single-roundtrip: children + containment edges + cross-child lineage edges.

        Supports cursor-based pagination for O(log N) performance at any page depth.
        When `cursor` is provided, it takes precedence over `offset`.
        """
        await self._ensure_connected()

        # --- Step 1: Fetch children with containment edges (returns edge r) ---
        target_edge_types = set(edge_types) if edge_types is not None else set(self._get_containment_edge_types())
        rel_list = list(target_edge_types)
        if not rel_list:
            # No containment types — return empty result
            return ChildrenWithEdgesResult(
                children=[], containmentEdges=[], lineageEdges=[],
                totalChildren=0, hasMore=False,
            )

        search_where = ""
        params: Dict[str, Any] = {"parent": parent_urn, "lim": limit, "relTypes": rel_list}

        if search_query:
            search_where = "AND (toLower(c.displayName) CONTAINS toLower($searchQuery) OR toLower(c.urn) CONTAINS toLower($searchQuery)) "
            params["searchQuery"] = search_query

        # Cursor-based pagination: WHERE c.displayName > $cursor is O(log N) vs SKIP's O(N)
        cursor_where = ""
        if cursor:
            cursor_where = "AND c.displayName > $cursor "
            params["cursor"] = cursor
        else:
            params["skip"] = offset

        # Build ORDER BY suffix for the WITH clause
        order_suffix = ""
        if sort_property:
            safe_prop = _sanitize_label(sort_property)
            order_suffix = f" ORDER BY c.{safe_prop}"

        skip_clause = "" if cursor else " SKIP $skip"

        # Query returns child node, containment edge properties, and grandchild count
        if len(rel_list) == 1:
            rel = _sanitize_label(rel_list[0])
            cypher = (
                f"MATCH (p)-[r:{rel}]->(c) "
                f"WHERE p.urn = $parent {search_where}{cursor_where}"
                f"WITH p, r, c{order_suffix}{skip_clause} LIMIT $lim "
                f"OPTIONAL MATCH (c)-[rc]->(gc) WHERE type(rc) IN $relTypes "
                f"RETURN c, count(gc) as childCount, p.urn as parentUrn, type(r) as relType, properties(r) as rprops"
            )
        else:
            cypher = (
                f"MATCH (p)-[r]->(c) "
                f"WHERE p.urn = $parent AND type(r) IN $relTypes {search_where}{cursor_where}"
                f"WITH p, r, c{order_suffix}{skip_clause} LIMIT $lim "
                f"OPTIONAL MATCH (c)-[rc]->(gc) WHERE type(rc) IN $relTypes "
                f"RETURN c, count(gc) as childCount, p.urn as parentUrn, type(r) as relType, properties(r) as rprops"
            )

        result = await self._ro_query(cypher, params=params)

        children: List[GraphNode] = []
        containment_edges: List[GraphEdge] = []
        child_urns: List[str] = []

        for row in (result.result_set or []):
            n = self._extract_node_from_result(row[0])
            child_count = row[1]
            parent_u = row[2]
            rel_type = row[3]
            rprops = row[4] or {}

            if n:
                if child_count is not None:
                    n.child_count = int(child_count)
                    if n.properties:
                        n.properties['childCount'] = int(child_count)
                children.append(n)
                child_urns.append(n.urn)

                # Build containment edge from the matched relationship
                containment_edges.append(_edge_from_row(parent_u, n.urn, rel_type, rprops))

        # --- Step 2: Fetch cross-child lineage edges (scoped to current page only) ---
        # Only use the current page's child URNs + parent, NOT cumulative URNs.
        # This keeps the query O(pageSize²) instead of O(totalLoaded²).
        lineage_edges_list: List[GraphEdge] = []
        if include_lineage_edges and len(child_urns) >= 2:
            page_urns = [parent_urn] + child_urns
            exclude_types = list(target_edge_types) + ["AGGREGATED"]

            lineage_params: Dict[str, Any] = {"pageUrns": page_urns}
            if lineage_edge_types:
                lineage_where = "AND type(lr) IN $lineageTypes"
                lineage_params["lineageTypes"] = lineage_edge_types
            else:
                lineage_where = "AND NOT type(lr) IN $excludeTypes"
                lineage_params["excludeTypes"] = exclude_types

            lineage_cypher = (
                f"MATCH (a)-[lr]->(b) "
                f"WHERE a.urn IN $pageUrns AND b.urn IN $pageUrns {lineage_where} "
                f"RETURN a.urn, b.urn, type(lr), properties(lr)"
            )

            lr_result = await self._ro_query(lineage_cypher, params=lineage_params)
            for row in (lr_result.result_set or []):
                lineage_edges_list.append(_edge_from_row(row[0], row[1], row[2], row[3] or {}))

        has_more = len(children) >= limit
        total = offset + len(children) + (1 if has_more else 0)
        next_cursor = children[-1].display_name if children and has_more else None

        return ChildrenWithEdgesResult(
            children=children,
            containmentEdges=containment_edges,
            lineageEdges=lineage_edges_list,
            totalChildren=total,
            hasMore=has_more,
            nextCursor=next_cursor,
        )

    async def get_parent(self, child_urn: str) -> Optional[GraphNode]:
        await self._ensure_connected()
        containment = self._get_containment_edge_types()
        if not containment:
            # No containment types — flat graph, no parent
            return None
        # Match any containment-type edge where child is target
        result = await self._ro_query(
            "MATCH (p)-[r]->(c) WHERE c.urn = $child AND type(r) IN $ctypes RETURN p",
            params={"child": child_urn, "ctypes": list(containment)},
        )
        if result.result_set and len(result.result_set) > 0:
            return self._extract_node_from_result(result.result_set[0])
        return None

    async def get_top_level_or_orphan_nodes(
        self,
        *,
        root_entity_types: Optional[List[str]] = None,
        entity_types: Optional[List[str]] = None,
        search_query: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        include_child_count: bool = True,
    ) -> TopLevelNodesResult:
        """Return structurally top-level nodes (no incoming containment edge).

        Mixes ontology root-type instances and orphan non-root instances so the
        wizard can show both in one list, with a root/orphan split in the
        badge text. Classification is done in Python on the returned rows.

        Pagination is cursor-based on displayName for stability under writes:
        callers pass cursor=None for the first page and the returned
        next_cursor for subsequent pages.
        """
        await self._ensure_connected()

        # Raises ProviderConfigurationError if no types resolvable — surfaced
        # as HTTP 400 by the endpoint. An empty set is a valid state meaning
        # "flat graph, every node is top-level".
        containment = self._get_containment_edge_types()
        containment_rel_types = "|".join([_sanitize_label(t) for t in sorted(containment)])
        root_types_set = {str(t) for t in (root_entity_types or [])}

        params: Dict[str, Any] = {"limit": int(limit)}

        # ── Build optional filters ────────────────────────────────────────
        # Each filter produces a WHERE fragment applied uniformly to both the
        # page query and the count query.
        filter_fragments: List[str] = []

        if search_query:
            params["search"] = search_query.lower()
            filter_fragments.append(
                "(toLower(toString(n.displayName)) CONTAINS $search "
                "OR toLower(toString(n.urn)) CONTAINS $search)"
            )

        # Structural top-level predicate — the whole point of this method.
        # Empty containment set = flat graph, skip the predicate entirely.
        #
        # IMPORTANT: Use openCypher 1.0 pattern negation `NOT ()-[:T]->(n)`
        # NOT `NOT EXISTS { MATCH ... }` which is Neo4j 4.x+ / ISO GQL syntax
        # and is NOT supported by FalkorDB. The subquery form silently throws,
        # gets caught below, and returns empty — which was the original bug.
        if containment_rel_types:
            filter_fragments.append(
                "NOT ()-[:" + containment_rel_types + "]->(n)"
            )

        # ── Build MATCH clause: label UNION if entity_types specified ─────
        use_label_union = bool(entity_types)
        safe_types: List[str] = []
        if use_label_union:
            safe_types = [_sanitize_label(str(t)) for t in entity_types if str(t)]
            if not safe_types:
                use_label_union = False

        # Page-query cursor: keyset over displayName for stability under writes.
        page_filters = list(filter_fragments)
        if cursor is not None:
            params["cursor"] = str(cursor)
            page_filters.append("toString(n.displayName) > $cursor")

        def _build_match(filters: List[str]) -> str:
            where_clause = (" WHERE " + " AND ".join(filters)) if filters else ""
            if use_label_union:
                branches = [
                    f"MATCH (n:{label}){where_clause} RETURN n"
                    for label in safe_types
                ]
                return "CALL { " + " UNION ".join(branches) + " }"
            return f"MATCH (n){where_clause}"

        # ── Page query ────────────────────────────────────────────────────
        if include_child_count and containment_rel_types:
            page_cypher = (
                _build_match(page_filters)
                + " WITH n ORDER BY toString(n.displayName) ASC LIMIT $limit"
                + f" OPTIONAL MATCH (n)-[:{containment_rel_types}]->(child)"
                + " RETURN n, count(child) as childCount"
            )
        else:
            page_cypher = (
                _build_match(page_filters)
                + " WITH n ORDER BY toString(n.displayName) ASC LIMIT $limit"
                + " RETURN n, 0 as childCount"
            )

        try:
            page_result = await self._ro_query(page_cypher, params=params)
        except Exception as e:
            logger.warning(f"get_top_level_or_orphan_nodes page query failed: {e}")
            page_result = None

        nodes: List[GraphNode] = []
        root_type_count = 0
        orphan_count = 0
        if page_result and page_result.result_set:
            for row in page_result.result_set:
                node = self._extract_node_from_result(row[0] if isinstance(row, (list, tuple)) else row)
                if not node:
                    continue
                try:
                    child_count = int(row[1]) if isinstance(row, (list, tuple)) and len(row) > 1 else None
                except (TypeError, ValueError):
                    child_count = None
                if child_count is not None:
                    node.child_count = child_count
                    if node.properties is not None:
                        node.properties["childCount"] = child_count
                # Classify: root-type instance vs orphan of non-root type
                if root_types_set and str(node.entity_type) in root_types_set:
                    root_type_count += 1
                else:
                    orphan_count += 1
                nodes.append(node)

        has_more = len(nodes) >= int(limit)
        next_cursor = nodes[-1].display_name if (has_more and nodes) else None

        # ── Total count query (no cursor filter) ──────────────────────────
        # We run this separately so the page result reflects the cursor, but
        # the total accurately shows how many top-level entities exist.
        count_params: Dict[str, Any] = {}
        if "search" in params:
            count_params["search"] = params["search"]

        if use_label_union:
            where_clause = (" WHERE " + " AND ".join(filter_fragments)) if filter_fragments else ""
            count_branches = [
                f"MATCH (n:{label}){where_clause} RETURN n"
                for label in safe_types
            ]
            count_cypher = "CALL { " + " UNION ".join(count_branches) + " } RETURN count(n) as total"
        else:
            where_clause = (" WHERE " + " AND ".join(filter_fragments)) if filter_fragments else ""
            count_cypher = f"MATCH (n){where_clause} RETURN count(n) as total"

        total_count = 0
        try:
            count_result = await self._ro_query(count_cypher, params=count_params)
            if count_result and count_result.result_set:
                first = count_result.result_set[0]
                total_count = int(first[0] if isinstance(first, (list, tuple)) else first)
        except Exception as e:
            logger.warning(f"get_top_level_or_orphan_nodes count query failed: {e}")
            total_count = len(nodes)

        return TopLevelNodesResult(
            nodes=nodes,
            totalCount=total_count,
            hasMore=has_more,
            nextCursor=next_cursor,
            rootTypeCount=root_type_count,
            orphanCount=orphan_count,
        )

    async def _traverse_lineage(
        self,
        start_urn: str,
        direction: str,
        depth: int,
        descendant_types: Optional[List[str]] = None,
    ) -> Set[str]:
        """Single-query lineage traversal using bounded variable-length Cypher paths.

        Uses *1..{depth} (literal bound) instead of unbounded *1.. so the
        query planner can prune early. Entity-type filtering is pushed into
        Cypher via labels(neighbor)[0] rather than fetching all nodes to
        filter in Python.
        """
        await self._ensure_connected()
        containment = list(self._get_containment_edge_types())
        safe_depth = max(1, min(int(depth), 20))  # Clamp to sane range
        params: Dict[str, Any] = {
            "startUrn": start_urn,
            "containmentTypes": containment,
        }

        # Entity-type filter pushed into Cypher
        type_clause = ""
        if descendant_types:
            allowed = [t.value if hasattr(t, "value") else str(t) for t in descendant_types]
            params["allowedTypes"] = allowed
            type_clause = "AND labels(neighbor)[0] IN $allowedTypes "

        if direction == "upstream":
            cypher = (
                f"MATCH (start) WHERE start.urn = $startUrn "
                f"MATCH path = (neighbor)-[*1..{safe_depth}]->(start) "
                f"WHERE ALL(r IN relationships(path) WHERE NOT type(r) IN $containmentTypes) "
                f"{type_clause}"
                f"RETURN DISTINCT neighbor.urn AS urn"
            )
        else:
            cypher = (
                f"MATCH (start) WHERE start.urn = $startUrn "
                f"MATCH path = (start)-[*1..{safe_depth}]->(neighbor) "
                f"WHERE ALL(r IN relationships(path) WHERE NOT type(r) IN $containmentTypes) "
                f"{type_clause}"
                f"RETURN DISTINCT neighbor.urn AS urn"
            )

        result = await self._ro_query(cypher, params=params)
        return {
            row[0] for row in (result.result_set or [])
            if row[0] and row[0] != start_urn
        }

    async def get_upstream(
        self,
        urn: str,
        depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        upstream_urns = await self._traverse_lineage(urn, "upstream", depth, descendant_types)
        all_urns = upstream_urns | {urn}
        nodes = await self.get_nodes(NodeQuery(urns=list(all_urns), limit=len(all_urns), include_child_count=False))
        node_ids = {n.urn for n in nodes}
        edges = await self.get_edges(EdgeQuery(any_urns=list(all_urns), limit=len(all_urns) * 10))
        edges = [e for e in edges if e.source_urn in node_ids and e.target_urn in node_ids]
        return LineageResult(
            nodes=nodes,
            edges=edges,
            upstreamUrns=upstream_urns,
            downstreamUrns=set(),
            totalCount=len(nodes),
            hasMore=False,
        )

    async def get_downstream(
        self,
        urn: str,
        depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        downstream_urns = await self._traverse_lineage(urn, "downstream", depth, descendant_types)
        all_urns = downstream_urns | {urn}
        nodes = await self.get_nodes(NodeQuery(urns=list(all_urns), limit=len(all_urns), include_child_count=False))
        node_ids = {n.urn for n in nodes}
        edges = await self.get_edges(EdgeQuery(any_urns=list(all_urns), limit=len(all_urns) * 10))
        edges = [e for e in edges if e.source_urn in node_ids and e.target_urn in node_ids]
        return LineageResult(
            nodes=nodes,
            edges=edges,
            upstreamUrns=set(),
            downstreamUrns=downstream_urns,
            totalCount=len(nodes),
            hasMore=False,
        )

    async def get_full_lineage(
        self,
        urn: str,
        upstream_depth: int,
        downstream_depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        up = await self._traverse_lineage(urn, "upstream", upstream_depth, descendant_types)
        down = await self._traverse_lineage(urn, "downstream", downstream_depth, descendant_types)
        all_urns = up | down | {urn}
        nodes = await self.get_nodes(NodeQuery(urns=list(all_urns), limit=len(all_urns), include_child_count=False))
        node_ids = {n.urn for n in nodes}
        edges = await self.get_edges(EdgeQuery(any_urns=list(all_urns), limit=len(all_urns) * 10))
        edges = [e for e in edges if e.source_urn in node_ids and e.target_urn in node_ids]
        return LineageResult(
            nodes=nodes,
            edges=edges,
            upstreamUrns=up,
            downstreamUrns=down,
            totalCount=len(nodes),
            hasMore=False,
        )


    # ------------------------------------------------------------------ #
    # Projection / Materialization Lifecycle Hooks                         #
    # ------------------------------------------------------------------ #

    async def ensure_projections(self) -> None:
        """Create indices on the projection target for fast AGGREGATED reads."""

        try:
            await self._proj_query("CREATE INDEX FOR (n:_Projection) ON (n.urn)")
        except Exception:
            pass  # Index may already exist

    async def reset_ancestors_cache(self) -> None:
        """Wipe the per-graph ancestor cache for the start of an aggregation run.

        The cache (`{graph_name}:ancestors` Redis Hash) is intra-job
        memoization: many batches in a single job touch overlapping
        URNs, so caching across batches is valuable. Persisting it
        across jobs is *not*: the most common reason to re-aggregate is
        an ontology classification change or a graph structure change,
        and either invalidates ancestor chains. Without this reset, a
        first job run with empty `containment_edge_types` caches `[]`
        for every URN, and every subsequent job sees those as cache
        hits and skips the graph walk — producing only leaf-to-leaf
        AGGREGATED edges instead of propagating up the tree.

        Best-effort: Redis errors are swallowed so a flush failure
        never fails an aggregation job.
        """
        try:
            await self._redis.delete(f"{self._graph_name}:ancestors")
        except Exception as exc:
            logger.debug(
                "reset_ancestors_cache: Redis delete failed for %s: %s",
                self._graph_name, exc,
            )

    async def _get_ancestor_chain(self, urn: str) -> List[str]:
        """Get pre-computed ancestor chain from Redis Hash, or compute + cache it.

        Returns list of URNs from immediate parent to root (ordered).
        Uses Redis Hash `{graph_name}:ancestors` for O(1) lookup.
        """
        cache_key = f"{self._graph_name}:ancestors"
        try:
            raw = await self._redis.execute_command("HGET", cache_key, urn)
            if raw:
                return json.loads(raw)
        except Exception:
            pass

        # Cache miss — compute from graph and store
        ancestors = await self._compute_ancestor_chain(urn)
        try:
            await self._redis.execute_command(
                "HSET", cache_key, urn, json.dumps(ancestors)
            )
        except Exception as e:
            logger.debug(f"Failed to cache ancestor chain for {urn}: {e}")
        return ancestors

    async def _compute_ancestor_chain(self, urn: str) -> List[str]:
        """Single Cypher query to walk containment edges upward (1 query instead of N)."""
        containment = list(self._get_containment_edge_types())
        if not containment:
            # No containment types — flat graph, no ancestors
            return []
        containment_cypher = "|".join(_sanitize_label(t) for t in containment)

        # Variable-length path: returns ordered list of ancestor URNs
        # nodes(path) gives [child, parent, grandparent, ...] — skip index 0 (self)
        result = await self._ro_query(
            f"MATCH path = (child)<-[:{containment_cypher}*1..10]-(ancestor) "
            f"WHERE child.urn = $urn "
            f"WITH path ORDER BY length(path) DESC LIMIT 1 "
            f"RETURN [n IN nodes(path)[1..] | n.urn] AS chain",
            params={"urn": urn},
        )
        if result.result_set and result.result_set[0][0]:
            return result.result_set[0][0]
        return []

    async def _compute_and_store_ancestors_bulk(
        self,
        urns: List[str],
    ) -> Dict[str, List[str]]:
        """Compute and cache ancestor chains for multiple URNs at once.

        Uses Redis pipeline for batch HSET — zero extra round-trips.
        """
        cache_key = f"{self._graph_name}:ancestors"
        result: Dict[str, List[str]] = {}

        # First, try to fetch all from cache in one pipeline
        try:
            pipe = self._redis.pipeline(transaction=False)
            for u in urns:
                pipe.execute_command("HGET", cache_key, u)
            cached = await pipe.execute()

            missing_urns = []
            for i, u in enumerate(urns):
                if cached[i]:
                    result[u] = json.loads(cached[i])
                else:
                    missing_urns.append(u)
        except Exception:
            missing_urns = list(urns)

        # Compute missing chains with bounded concurrency.  A semaphore
        # (not chunked gather) ensures at most _MAX_ANCESTOR_CONCURRENCY
        # Cypher queries are in-flight at once, leaving headroom in the
        # shared graph connection pool for batch fetches and MERGEs.
        if missing_urns:
            _MAX_ANCESTOR_CONCURRENCY = 4
            sem = asyncio.Semaphore(_MAX_ANCESTOR_CONCURRENCY)

            async def _compute_with_sem(urn: str) -> tuple[str, list]:
                async with sem:
                    try:
                        return urn, await self._compute_ancestor_chain(urn)
                    except Exception as exc:
                        logger.warning("Failed to compute ancestor chain for %s: %s", urn, exc)
                        return urn, []

            computed = await asyncio.gather(
                *(_compute_with_sem(u) for u in missing_urns),
            )
            for u, chain in computed:
                result[u] = chain

            # Batch-store all computed chains in one pipeline
            store_pipe = self._redis.pipeline(transaction=False)
            for u in missing_urns:
                store_pipe.execute_command("HSET", cache_key, u, json.dumps(result.get(u, [])))
            try:
                await store_pipe.execute()
            except Exception as e:
                logger.debug(f"Failed to batch-store ancestor chains: {e}")

        return result

    # ------------------------------------------------------------------ #
    # Batch-level materialization (used by materialize_aggregated_edges_batch)
    # ------------------------------------------------------------------ #

    # Max ancestor pairs per Cypher UNWIND+MERGE call.  Each input edge
    # fans out to ~4 ancestor pairs (s_chain × t_chain), so 5000 input
    # edges produce ~20K pairs.  A single MERGE with 20K items + REDUCE
    # exceeds FalkorDB's 3s socket_timeout.  500 pairs keeps each call
    # well under 1s while still being 500× fewer round-trips than the
    # old per-edge approach.
    _MERGE_SUB_BATCH_SIZE = 500

    async def _materialize_edges_batched(
        self,
        rows: list,
        ancestors_cache: Dict[str, List[str]],
        *,
        intra_batch_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        baseline_aggregated: int = 0,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> tuple[int, int]:
        """Batch-level materialization — all Redis + Cypher ops in ~4 round-trips.

        Replaces the previous per-edge loop that did 3 round-trips per edge
        (SADD pipeline + SCARD pipeline + Cypher MERGE × N edges). Now:

        1. Compute all ancestor pairs across ALL edges in memory (O(1) per
           edge using ``ancestors_cache`` populated by bulk pre-compute).
        2. ONE Redis SADD pipeline for all pairs across all edges.
        3. ONE Redis SCARD pipeline for newly-added pairs only.
        4. ONE (or a few) Cypher UNWIND+MERGE for all new pairs.

        Returns (created_count, error_count).
        Raises AggregationBatchAbort on sustained provider failure.
        """
        await self._ensure_connected()
        members_key_prefix = f"{self._graph_name}:agg_members"

        # Step 1: Compute all ancestor pairs across ALL edges in the batch.
        # Each edge (s, t) with edge_id and edge_type generates pairs from
        # s_chain × t_chain (excluding self-loops).
        all_sadd_ops: list[tuple[str, str, str, str, str]] = []  # (redis_key, edge_id, s, t, edge_type)
        for row in rows:
            s_urn, t_urn, edge_type, edge_id = row[0], row[1], row[2], row[3]
            if not edge_id:
                edge_id = f"{s_urn}|{edge_type}|{t_urn}"

            s_chain = [s_urn] + ancestors_cache.get(s_urn, [])
            t_chain = [t_urn] + ancestors_cache.get(t_urn, [])

            for s in s_chain:
                for t in t_chain:
                    if s != t:
                        key = f"{members_key_prefix}:{s}:{t}"
                        all_sadd_ops.append((key, edge_id, s, t, edge_type))

        if not all_sadd_ops:
            return 0, 0

        # Step 2: ONE Redis SADD pipeline for all pairs.
        # SADD returns 1 if the member was newly added, 0 if already present.
        pipe = self._redis.pipeline(transaction=False)
        for redis_key, edge_id_val, _, _, _ in all_sadd_ops:
            pipe.execute_command("SADD", redis_key, edge_id_val)
        sadd_results = await pipe.execute()

        # Step 3: Collect newly-added pairs and their keys for SCARD.
        # Deduplicate by (s, t) — multiple input edges may generate the
        # same ancestor pair, but we only need one SCARD + one MERGE per pair.
        new_pair_keys: dict[tuple[str, str], tuple[str, str]] = {}  # (s,t) -> (redis_key, edge_type)
        for i, (redis_key, _, s, t, etype) in enumerate(all_sadd_ops):
            if sadd_results[i] != 0:
                pair = (s, t)
                if pair not in new_pair_keys:
                    new_pair_keys[pair] = (redis_key, etype)

        if not new_pair_keys:
            return 0, 0

        # ONE Redis SCARD pipeline for all newly-added pairs.
        ordered_pairs = list(new_pair_keys.items())
        pipe = self._redis.pipeline(transaction=False)
        for (_, _), (redis_key, _) in ordered_pairs:
            pipe.execute_command("SCARD", redis_key)
        scard_results = await pipe.execute()

        # Step 4: Build merge batch with edge type lists per pair.
        # Collect all distinct edge types per (s, t) pair so we can
        # store them in sourceEdgeTypes in a single Cypher call.
        pair_edge_types: dict[tuple[str, str], set[str]] = {}
        for _, _, s, t, etype in all_sadd_ops:
            if (s, t) in new_pair_keys:
                pair_edge_types.setdefault((s, t), set()).add(etype)

        merge_batch: list[dict[str, Any]] = []
        for i, ((s, t), _) in enumerate(ordered_pairs):
            weight = scard_results[i] if scard_results[i] else 1
            etypes = list(pair_edge_types.get((s, t), set()))
            merge_batch.append({
                "s": s, "t": t, "w": int(weight), "et": etypes,
            })

        # Execute ONE Cypher UNWIND+MERGE per sub-batch.  The Cypher
        # REDUCE accumulates all edge types into sourceEdgeTypes in a
        # single pass — no per-edge-type iteration needed.

        created = 0
        for chunk_start in range(0, len(merge_batch), self._MERGE_SUB_BATCH_SIZE):
            # Cooperative cancel between MERGE sub-batches. The previous
            # sub-batch's MERGE has fully landed in FalkorDB before we
            # reach this check, so raising here cannot orphan a Cypher
            # transaction. Without this hook, a single outer batch
            # (~100+ sub-batches over several minutes) cannot be
            # cancelled without ``task.cancel()`` interrupting a
            # mid-flight MERGE.
            if should_cancel is not None and should_cancel():
                from backend.app.services.aggregation.cancel import JobCancelled
                from datetime import datetime, timezone
                raise JobCancelled(
                    job_id="<provider-cancel>",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )

            chunk = merge_batch[chunk_start:chunk_start + self._MERGE_SUB_BATCH_SIZE]
            await self._proj_query(
                "UNWIND $batch AS item "
                "MERGE (s {urn: item.s}) "
                "MERGE (t {urn: item.t}) "
                "MERGE (s)-[r:AGGREGATED]->(t) "
                "SET r.weight = item.w, "
                "    r.latestUpdate = timestamp(), "
                "    r.sourceEdgeTypes = REDUCE(acc = "
                "      CASE WHEN r.sourceEdgeTypes IS NULL THEN [] "
                "           ELSE r.sourceEdgeTypes END, "
                "      et IN item.et | "
                "      CASE WHEN et IN acc THEN acc "
                "           ELSE acc + et END)",
                params={"batch": chunk},
            )
            created += len(chunk)

            # Intra-batch heartbeat. A single outer batch fans out to
            # tens of thousands of ancestor pairs and runs ~100+ Cypher
            # MERGE sub-batches; each sub-batch is ~1–3s, so the outer
            # batch can take many minutes. Without an intra-batch hook
            # the worker's checkpoint can't update ``created_edges`` /
            # ``last_checkpoint_at`` for the duration, leaving the UI
            # apparently frozen even though aggregation is making
            # steady progress in FalkorDB.
            #
            # The callback receives the running aggregated total
            # (across all completed outer batches up to and including
            # this sub-batch). It deliberately doesn't touch the input
            # cursor — that's the caller's job after the full outer
            # batch lands, so a mid-batch crash still resumes from the
            # last fully-committed boundary (writes are idempotent via
            # MERGE + the SADD idempotency tracker, but we keep the
            # boundary clean for clarity).
            if intra_batch_callback is not None:
                try:
                    await intra_batch_callback(baseline_aggregated + created)
                except Exception as cb_exc:
                    logger.error(
                        "Intra-batch progress callback failed at sub-batch "
                        "ending %d (continuing): %s",
                        chunk_start + len(chunk), cb_exc, exc_info=True,
                    )

        return created, 0

    async def on_lineage_edge_written(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
        edge_type: str,
    ) -> int:
        """Materialize AGGREGATED edges when a lineage edge is written.

        Used for real-time per-edge materialization on individual writes.
        For bulk aggregation, use ``_materialize_edges_batched`` instead.

        Uses pre-computed ancestor chains instead of Cypher variable-length
        paths, eliminating the Cartesian product explosion.

        Idempotency: Uses Redis Sets to track which leaf edges contribute
        to each AGGREGATED pair. SADD is naturally idempotent.

        Batching: Collects all new pairs, then issues a single UNWIND+MERGE
        instead of one Cypher call per ancestor pair.

        Returns the number of AGGREGATED pairs whose graph edge was
        newly created or had its weight/sourceEdgeTypes updated as a
        result of this call. Returns 0 if every pair was already
        recorded in the Redis idempotency set (nothing to do). Callers
        sum this across the batch to report *actual graph edges
        affected* rather than *input edges processed*.
        """
        await self._ensure_connected()

        s_ancestors = await self._get_ancestor_chain(source_urn)
        t_ancestors = await self._get_ancestor_chain(target_urn)

        s_chain = [source_urn] + s_ancestors
        t_chain = [target_urn] + t_ancestors

        members_key_prefix = f"{self._graph_name}:agg_members"

        # Phase 1: Redis SADD pipeline to check idempotency for all pairs at once
        pairs_to_check = []
        for s_urn in s_chain:
            for t_urn in t_chain:
                if s_urn != t_urn:
                    pairs_to_check.append((s_urn, t_urn))

        if not pairs_to_check:
            return 0

        # Pipeline: SADD for all pairs.
        # Do NOT silently fallback on Redis failure — the previous
        # ``except: sadd_results = [1] * len(...)`` treated every pair
        # as "newly added" and set weight=1, producing incorrect
        # AGGREGATED edges. Let the exception propagate so the caller
        # can count it as an error and, on sustained failure, abort the
        # job via AggregationBatchAbort.
        pipe = self._redis.pipeline(transaction=False)
        for s_urn, t_urn in pairs_to_check:
            member_key = f"{members_key_prefix}:{s_urn}:{t_urn}"
            pipe.execute_command("SADD", member_key, edge_id)
        sadd_results = await pipe.execute()

        # Phase 2: SCARD pipeline for pairs that were newly added
        new_pairs = [(pairs_to_check[i], sadd_results[i]) for i in range(len(pairs_to_check)) if sadd_results[i] != 0]
        if not new_pairs:
            return 0

        # Same rationale as SADD above — silent fallback to [1]*N
        # produces incorrect weights. Let failures propagate.
        pipe = self._redis.pipeline(transaction=False)
        for (s_urn, t_urn), _ in new_pairs:
            member_key = f"{members_key_prefix}:{s_urn}:{t_urn}"
            pipe.execute_command("SCARD", member_key)
        scard_results = await pipe.execute()

        # Phase 3: Single UNWIND+MERGE for all new pairs
        merge_batch = []
        for i, ((s_urn, t_urn), _) in enumerate(new_pairs):
            weight = scard_results[i] if scard_results[i] else 1
            merge_batch.append({"s": s_urn, "t": t_urn, "w": int(weight)})

        # Do NOT catch exceptions here — the previous ``except: return 0``
        # silently swallowed MERGE failures (including the "Batched
        # AGGREGATED_MERGE failed: timeout" error). The caller in
        # materialize_aggregated_edges_batch has a per-edge try/except
        # that logs and increments the error counter; on sustained
        # failure, AggregationBatchAbort aborts the job and preserves
        # last_cursor for resume.

        await self._proj_query(
            "UNWIND $batch AS item "
            "MERGE (s {urn: item.s}) "
            "MERGE (t {urn: item.t}) "
            "MERGE (s)-[r:AGGREGATED]->(t) "
            "SET r.weight = item.w, "
            "r.sourceEdgeTypes = CASE "
            "  WHEN r.sourceEdgeTypes IS NULL THEN [$edgeType] "
            "  WHEN NOT $edgeType IN r.sourceEdgeTypes "
            "    THEN r.sourceEdgeTypes + $edgeType "
            "  ELSE r.sourceEdgeTypes END, "
            "r.latestUpdate = timestamp()",
            params={"batch": merge_batch, "edgeType": edge_type},
        )
        return len(merge_batch)

    async def on_lineage_edge_deleted(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
    ) -> None:
        """Decrement AGGREGATED edge weights when a lineage edge is removed.

        Batched: single SREM pipeline → single SCARD pipeline →
        one UNWIND+SET for weight updates + one UNWIND+DELETE for empty pairs.
        """
        await self._ensure_connected()

        s_ancestors = await self._get_ancestor_chain(source_urn)
        t_ancestors = await self._get_ancestor_chain(target_urn)

        s_chain = [source_urn] + s_ancestors
        t_chain = [target_urn] + t_ancestors

        members_key_prefix = f"{self._graph_name}:agg_members"
        pairs = [(s, t) for s in s_chain for t in t_chain if s != t]
        if not pairs:
            return

        # Phase 1: Pipeline SREM for all pairs
        try:
            pipe = self._redis.pipeline(transaction=False)
            for s_urn, t_urn in pairs:
                pipe.srem(f"{members_key_prefix}:{s_urn}:{t_urn}", edge_id)
            await pipe.execute()
        except Exception:
            pass

        # Phase 2: Pipeline SCARD to get remaining counts
        try:
            pipe = self._redis.pipeline(transaction=False)
            for s_urn, t_urn in pairs:
                pipe.scard(f"{members_key_prefix}:{s_urn}:{t_urn}")
            counts = await pipe.execute()
        except Exception:
            return  # Can't determine counts — bail

        # Phase 3: Separate into delete (count=0) vs update (count>0)
        delete_batch = []
        update_batch = []
        cleanup_keys = []
        for i, (s_urn, t_urn) in enumerate(pairs):
            remaining = counts[i] if i < len(counts) else None
            if remaining == 0:
                delete_batch.append({"s": s_urn, "t": t_urn})
                cleanup_keys.append(f"{members_key_prefix}:{s_urn}:{t_urn}")
            elif remaining is not None:
                update_batch.append({"s": s_urn, "t": t_urn, "w": int(remaining)})


        if delete_batch:
            try:
                await self._proj_query(
                    "UNWIND $batch AS item "
                    "MATCH (s {urn: item.s})-[r:AGGREGATED]->(t {urn: item.t}) "
                    "DELETE r",
                    params={"batch": delete_batch},
                )
                # Clean up empty Redis keys
                pipe = self._redis.pipeline(transaction=False)
                for key in cleanup_keys:
                    pipe.delete(key)
                await pipe.execute()
            except Exception as e:
                logger.error(f"Batched AGGREGATED DELETE failed: {e}")

        if update_batch:
            try:
                await self._proj_query(
                    "UNWIND $batch AS item "
                    "MATCH (s {urn: item.s})-[r:AGGREGATED]->(t {urn: item.t}) "
                    "SET r.weight = item.w, r.latestUpdate = timestamp()",
                    params={"batch": update_batch},
                )
            except Exception as e:
                logger.error(f"Batched AGGREGATED weight update failed: {e}")

    async def on_containment_changed(self, urn: str) -> None:
        """Invalidate ancestor cache for a node and its descendants, then rebuild.

        When a node's parent changes, its entire subtree's ancestor chains
        are invalidated and lazily recomputed on next access.
        """
        await self._ensure_connected()
        cache_key = f"{self._graph_name}:ancestors"

        # Invalidate this node's cached chain
        try:
            await self._redis.hdel(cache_key, urn)
        except Exception:
            pass

        # Invalidate descendants (BFS through containment)
        containment = list(self._get_containment_edge_types())
        queue = deque([urn])
        visited: Set[str] = {urn}

        while queue:
            current = queue.popleft()
            result = await self._ro_query(
                "MATCH (p)-[r]->(c) WHERE p.urn = $urn AND type(r) IN $ctypes RETURN c.urn",
                params={"urn": current, "ctypes": containment},
            )
            child_urns = [row[0] for row in (result.result_set or []) if row[0] and row[0] not in visited]
            if child_urns:
                try:
                    pipe = self._redis.pipeline(transaction=False)
                    for cu in child_urns:
                        pipe.execute_command("HDEL", cache_key, cu)
                        visited.add(cu)
                        queue.append(cu)
                    await pipe.execute()
                except Exception:
                    pass

        logger.info(f"Invalidated ancestor cache for {len(visited)} nodes under {urn}")

    async def count_aggregated_edges(self) -> int:
        """Cheap COUNT for purge progress reporting. Returns the current
        number of materialized AGGREGATED edges in the projection graph.
        """
        await self._ensure_connected()
        result = await self._proj_query(
            "MATCH ()-[r:AGGREGATED]->() RETURN count(r) AS total"
        )
        return int(result.result_set[0][0]) if result.result_set else 0

    async def purge_aggregated_edges(
        self,
        *,
        batch_size: int = 10_000,
        progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Remove ALL materialized AGGREGATED edges from the graph.

        Also deletes the Redis ``{graph_name}:agg_members:*`` tracking
        sets. These sets are the idempotency state used by
        :meth:`on_lineage_edge_written` (SADD returns 0 when an edge_id
        is already a member, short-circuiting the MERGE). If they are
        NOT purged together with the graph edges, the next materialize
        run silently no-ops — the source edges appear "already
        contributed" even though the AGGREGATED edges they produced are
        gone from the graph, and the caller sees
        ``aggregated_edges_affected`` numbers that match the input
        count but 0 edges actually written to the graph.

        The deletion runs in batches of ``batch_size`` so multi-million-
        edge purges (a) report progress to the caller via
        ``progress_callback`` and (b) cannot silently truncate at the
        single hard-coded LIMIT 100000 the previous one-shot DELETE used.
        Each iteration's actual deleted count is summed into the
        running total handed to the callback.

        The Redis key prefix was renamed from ``agg:sourceEdgeIds:`` to
        ``agg_members:`` in an earlier refactor of
        :meth:`on_lineage_edge_written`; this method's scan pattern was
        not updated and so cleaned nothing until this fix.
        """
        await self._ensure_connected()

        # Clamp to a safe, non-zero range. 0 / negative would loop
        # forever; very large values defeat the progress-reporting
        # purpose this method exists for.
        if batch_size <= 0:
            batch_size = 10_000
        batch_size = min(batch_size, 100_000)

        try:
            total_deleted = 0
            while True:
                # Cooperative cancel between DELETE batches. The previous
                # batch's DELETE already landed in FalkorDB, so raising
                # here cannot orphan a Cypher transaction. Without this
                # hook a multi-million-edge purge cannot be cancelled
                # without ``task.cancel()`` interrupting a mid-flight
                # DELETE — same pattern as the materialise path.
                if should_cancel is not None and should_cancel():
                    from backend.app.services.aggregation.cancel import JobCancelled
                    from datetime import datetime, timezone
                    raise JobCancelled(
                        job_id="<provider-cancel>",
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )

                result = await self._proj_query(
                    f"MATCH ()-[r:AGGREGATED]->() "
                    f"WITH r LIMIT {int(batch_size)} "
                    f"DELETE r "
                    f"RETURN count(r) AS deleted"
                )
                deleted_in_batch = (
                    int(result.result_set[0][0]) if result.result_set else 0
                )
                total_deleted += deleted_in_batch

                if progress_callback is not None:
                    try:
                        await progress_callback(total_deleted)
                    except Exception as cb_exc:
                        # Progress reporting must never abort the actual
                        # deletion — log and keep going.
                        logger.warning(
                            "purge_aggregated_edges progress_callback raised: %s",
                            cb_exc,
                        )

                # Anything less than a full batch means we've drained
                # the AGGREGATED relations.
                if deleted_in_batch < batch_size:
                    break

            # Clean up Redis tracking keys for this graph. Must match the
            # prefix used by on_lineage_edge_written exactly (see
            # docstring). Done after all graph DELETEs succeed so a
            # mid-purge crash can't leave the tracker keys cleared while
            # AGGREGATED edges still exist (which would silently no-op
            # the next materialize run).
            pattern = f"{self._graph_name}:agg_members:*"
            cursor = 0
            cleaned = 0
            while True:
                cursor, keys = await self._redis.scan(cursor, match=pattern, count=500)
                if keys:
                    await self._redis.delete(*keys)
                    cleaned += len(keys)
                if cursor == 0:
                    break

            logger.info(
                "Purged %d AGGREGATED edges and %d Redis tracking keys from %s",
                total_deleted, cleaned, self._graph_name,
            )
            return total_deleted
        except Exception as e:
            logger.error("Failed to purge AGGREGATED edges: %s", e)
            raise

    async def materialize_lineage_for_edge(
        self,
        source_urn: str,
        target_urn: str,
        lineage_edge_type: str,
    ) -> bool:
        """Legacy wrapper — delegates to on_lineage_edge_written."""
        try:
            edge_id = f"{source_urn}|{lineage_edge_type}|{target_urn}"
            await self.on_lineage_edge_written(source_urn, target_urn, edge_id, lineage_edge_type)
            return True
        except Exception as e:
            logger.error(f"Failed to materialize lineage: {e}")
            return False

    async def materialize_aggregated_edges_batch(
        self,
        batch_size: int = 1000,
        containment_edge_types: Optional[List[str]] = None,
        lineage_edge_types: Optional[List[str]] = None,
        last_cursor: Optional[str] = None,
        progress_callback: Optional[Any] = None,
        intra_batch_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Batch materialization using ancestor-chain approach with cursor-based pagination.

        Instead of Cypher variable-length paths with Cartesian products,
        this uses pre-computed ancestor chains stored in Redis Hashes.

        CURSOR-BASED PAGINATION (CRIT-2):
        - Uses stable cursor on sorted composite key (s.urn + '|' + t.urn)
        - Eliminates O(n²) degradation from SKIP at large offsets
        - Safe under concurrent graph mutations
        - Resume from last_cursor after crash/restart

        Args:
            batch_size: Number of edges to process per batch
            containment_edge_types: Structural edge types (from ontology)
            lineage_edge_types: Functional edge types (from ontology)
            last_cursor: Resume point — composite key of last processed edge
            progress_callback: async fn(processed, total, cursor, created_count) for checkpointing
            intra_batch_callback: async fn(running_aggregated_total) called after each
                Cypher MERGE sub-batch within an outer batch. A single outer batch can
                fan out to 100+ MERGE sub-batches running for several minutes; without
                this hook the operator UI's ``last_checkpoint_at`` and ``created_edges``
                would freeze for that whole window.
        """
        await self._ensure_connected()

        containment = containment_edge_types or list(self._get_containment_edge_types())
        exclude_types = list(containment) + ["AGGREGATED"]

        # Filter AGGREGATED out of any explicit lineage whitelist. The
        # ontology can legitimately list AGGREGATED as a lineage-category
        # relationship (it is the *result* of aggregation), but feeding
        # existing AGGREGATED edges back into this loop produces new
        # AGGREGATED edges from ancestor chains of the previously-
        # aggregated pairs, compounding on every re-run. This was the
        # cause of the API vs seed_falkordb count divergence: after the
        # first materialization, each API run multiplied the AGGREGATED
        # count whereas the seed-script fallback branch (``NOT IN
        # exclude_types``) already excluded AGGREGATED correctly.
        if lineage_edge_types:
            effective_lineage_types = [t for t in lineage_edge_types if t != "AGGREGATED"]
            if not effective_lineage_types:
                logger.warning(
                    "materialize_aggregated_edges_batch: lineage_edge_types contained "
                    "only AGGREGATED after filtering; no leaf lineage edges to process. "
                    "Check the ontology's is_lineage flags."
                )
                return {"processed": 0, "aggregated_edges_affected": 0, "errors": 0}
            type_filter = "WHERE type(r) IN $lineageEdges"
            type_params: Dict[str, Any] = {"lineageEdges": effective_lineage_types}
        else:
            type_filter = "WHERE NOT type(r) IN $excludeTypes"
            type_params = {"excludeTypes": exclude_types}

        # Count total lineage edges
        count_cypher = f"MATCH ()-[r]->() {type_filter} RETURN count(r)"
        count_res = await self._ro_query(count_cypher, params=type_params)
        total = count_res.result_set[0][0] if count_res.result_set else 0

        logger.info(f"Batch materialization: {total} lineage edges to process (cursor: {last_cursor or 'start'})")

        processed = 0
        errors = 0
        created_count = 0
        current_cursor = last_cursor
        batch_num = 0

        while True:
            batch_num += 1
            # Cursor-based batch fetch — sorted composite key for stable ordering
            if current_cursor:
                batch_cypher = (
                    f"MATCH (s)-[r]->(t) {type_filter} "
                    f"AND (s.urn + '|' + t.urn) > $cursor "
                    f"RETURN s.urn, t.urn, type(r), r.id "
                    f"ORDER BY s.urn + '|' + t.urn LIMIT $limit"
                )
                batch_params = {**type_params, "cursor": current_cursor, "limit": batch_size}
            else:
                batch_cypher = (
                    f"MATCH (s)-[r]->(t) {type_filter} "
                    f"RETURN s.urn, t.urn, type(r), r.id "
                    f"ORDER BY s.urn + '|' + t.urn LIMIT $limit"
                )
                batch_params = {**type_params, "limit": batch_size}

            # Do NOT silently break on batch-fetch failure — that path
            # lets a provider outage mid-aggregation flow through the
            # worker as if the job completed successfully (the worker
            # reads our ``stats`` dict, sees no exception, and marks
            # status=completed with whatever ``processed`` count we
            # managed before the failure). Re-raise so the worker's
            # outer try/except transitions the job to ``failed`` and
            # preserves ``last_cursor`` for crash-resume. The provider
            # is either back (resume succeeds) or still down (breaker
            # opens and triggers 503 upstream).
            t0 = time.monotonic()
            res = await self._ro_query(batch_cypher, params=batch_params)
            rows = res.result_set or []
            t_fetch = (time.monotonic() - t0) * 1000

            if not rows:
                break

            # Pre-compute ancestor chains for all URNs in this batch.
            # The returned dict maps URN → ancestor list (ordered parent
            # to root). We pass this cache directly to the batched
            # materializer to avoid per-edge Redis HGET round-trips.
            t0 = time.monotonic()
            all_urns = set()
            for row in rows:
                all_urns.add(row[0])
                all_urns.add(row[1])
            ancestors_cache = await self._compute_and_store_ancestors_bulk(list(all_urns))
            t_ancestors = (time.monotonic() - t0) * 1000

            # Batch-level materialization: all Redis pipelines + Cypher
            # MERGE in 4 round-trips total, regardless of batch size.
            # This replaces the previous per-edge loop that did 3 round-
            # trips per edge (SADD + SCARD + MERGE × N edges).
            # Cooperative cancel check at the start of each outer batch
            # — cheap, predicate-only, no exception out of this provider
            # if the worker hasn't asked us to bail. Inside
            # ``_materialize_edges_batched`` the same predicate fires
            # between MERGE sub-batches so a multi-minute outer batch
            # can be aborted cleanly without orphaning a Cypher
            # transaction. Importing locally keeps the provider free of
            # an aggregation-package coupling at module load.
            if should_cancel is not None and should_cancel():
                from backend.app.services.aggregation.cancel import JobCancelled
                from datetime import datetime, timezone
                raise JobCancelled(
                    job_id=last_cursor or "<no-cursor>",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )

            t0 = time.monotonic()
            # Pass ``created_count`` as the baseline so the intra-batch
            # callback always receives the cumulative aggregated total
            # across the whole job, not just within the current outer
            # batch. The worker uses this as ``job.created_edges``
            # directly so the UI sees a monotonically rising counter.
            batch_created, batch_errors = await self._materialize_edges_batched(
                rows, ancestors_cache,
                intra_batch_callback=intra_batch_callback,
                baseline_aggregated=created_count,
                should_cancel=should_cancel,
            )
            created_count += batch_created
            errors += batch_errors

            t_materialize = (time.monotonic() - t0) * 1000

            processed += len(rows)
            # Update cursor to last row's composite key
            last_row = rows[-1]
            current_cursor = f"{last_row[0]}|{last_row[1]}"

            logger.info(
                "Batch %d: %d/%d edges | fetch=%.1fms ancestors=%.1fms "
                "materialize=%.1fms | created=%d errors=%d",
                batch_num, processed, total,
                t_fetch, t_ancestors, t_materialize,
                created_count, errors,
            )

            # Checkpoint via callback (for worker DB persistence). The
            # callback is the only path by which the running job's
            # progress + cursor reach the operator UI; without ``exc_info``
            # a silent failure here meant the user saw ``processed_edges =
            # 0`` indefinitely while AGGREGATED edges accumulated in
            # FalkorDB. Log the full traceback so the underlying cause is
            # diagnosable, and continue the loop — the materialisation
            # itself is independent of progress reporting.
            if progress_callback:
                try:
                    await progress_callback(processed, total, current_cursor, created_count)
                except Exception as e:
                    logger.error(
                        "Progress callback failed at batch %d (processed=%d/%d, "
                        "created=%d): %s — continuing materialisation",
                        batch_num, processed, total, created_count, e,
                        exc_info=True,
                    )

            # If we got fewer rows than batch_size, we've reached the end
            if len(rows) < batch_size:
                break

        stats = {
            "processed": processed,
            # Historical key name — kept for back-compat with
            # aggregation_jobs.created_edges; now correctly counts the
            # number of AGGREGATED graph edges created or updated, not
            # the number of input lineage edges iterated.
            "aggregated_edges_affected": created_count,
            # New stat so callers + dashboards can distinguish
            # "touched N input edges" from "wrote M aggregated edges".
            # On a clean run these two are typically proportional; when
            # they diverge the operator has a clear signal that the
            # Redis idempotency sets are in a surprising state.
            "input_edges_processed": processed,
            "errors": errors,
        }
        try:
            if self._redis is not None:
                from datetime import datetime, timezone
                await self._redis.set(
                    self._agg_last_materialized_key(),
                    datetime.now(timezone.utc).isoformat(),
                )
        except Exception as e:
            logger.warning("Failed to stamp aggregated materialization timestamp: %s", e)
        logger.info(f"Batch materialization complete: {stats}")
        return stats

    async def get_aggregated_edges_between(
        self,
        source_urns: List[str],
        target_urns: Optional[List[str]],
        granularity: Any,
        containment_edges: List[str],
        lineage_edges: List[str],
        *,
        timeout: Optional[float] = None,
    ) -> AggregatedEdgeResult:
        """Read pre-materialized AGGREGATED edges from the projection graph.

        Pure index lookup — O(|sourceUrns|), sub-millisecond at any scale.
        No live fallback: if materialization hasn't run, returns empty result
        so the caller knows to trigger a backfill.
        """
        from fastapi import HTTPException
        from ..config.resilience import AGGREGATED_SOURCE_URN_BATCH_SIZE

        if len(source_urns) > 100_000:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "TOO_MANY_SOURCE_URNS",
                    "limit": 100000,
                    "received": len(source_urns),
                },
            )

        await self._ensure_connected()

        if target_urns:
            cypher = (
                "MATCH (s)-[r:AGGREGATED]->(t) "
                "WHERE s.urn IN $sourceUrns AND t.urn IN $targetUrns "
                "AND s.urn <> t.urn "
                "RETURN s.urn AS sUrn, t.urn AS tUrn, "
                "r.weight AS weight, r.sourceEdgeTypes AS types "
                "ORDER BY r.weight DESC"
            )
        else:
            cypher = (
                "MATCH (s)-[r:AGGREGATED]->(t) "
                "WHERE s.urn IN $sourceUrns "
                "AND s.urn <> t.urn "
                "RETURN s.urn AS sUrn, t.urn AS tUrn, "
                "r.weight AS weight, r.sourceEdgeTypes AS types "
                "ORDER BY r.weight DESC"
            )

        async def _run_batch(batch: List[str]) -> list:
            params: Dict[str, Any] = {"sourceUrns": batch}
            if target_urns:
                params["targetUrns"] = target_urns
            try:
                result = await self._proj_ro_query(cypher, params=params, timeout=timeout)
                return result.result_set or []
            except Exception as e:
                logger.warning(f"AGGREGATED edge read failed: {e}")
                return []

        batch_size = AGGREGATED_SOURCE_URN_BATCH_SIZE
        if len(source_urns) > batch_size:
            batches = [source_urns[i:i + batch_size] for i in range(0, len(source_urns), batch_size)]
            batch_results = await asyncio.gather(*[_run_batch(b) for b in batches])
            merged: Dict[Tuple[str, str], list] = {}
            for batch_rows in batch_results:
                for row in batch_rows:
                    key = (row[0], row[1])
                    existing = merged.get(key)
                    if existing is None:
                        merged[key] = list(row)
                    else:
                        existing[2] = (int(existing[2]) if existing[2] else 0) + (int(row[2]) if row[2] else 0)
                        ex_types = existing[3] if isinstance(existing[3], list) else ([existing[3]] if existing[3] else [])
                        new_types = row[3] if isinstance(row[3], list) else ([row[3]] if row[3] else [])
                        existing[3] = list(dict.fromkeys([*ex_types, *new_types]))
            rows = list(merged.values())
        else:
            rows = await _run_batch(source_urns)

        last_materialized_at: Optional[str] = None
        try:
            if self._redis is not None:
                raw = await self._redis.get(self._agg_last_materialized_key())
                if raw is not None:
                    last_materialized_at = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception as e:
            logger.warning("Failed to read aggregated materialization timestamp: %s", e)

        return self._rows_to_aggregated_result(rows, last_materialized_at=last_materialized_at)

    # ------------------------------------------------------------------
    # Helpers for get_aggregated_edges_between
    # ------------------------------------------------------------------

    def _rows_to_aggregated_result(
        self,
        rows: list,
        *,
        last_materialized_at: Optional[str] = None,
    ) -> AggregatedEdgeResult:
        """Convert raw Cypher result rows into AggregatedEdgeResult."""
        from ..config.resilience import AGGREGATED_EDGE_RESULT_CAP
        aggregated = []
        total_edges = 0
        for row in rows:
            s_urn, t_urn, weight, types = row[0], row[1], row[2], row[3]
            w = int(weight) if weight else 1
            edge_types = types if isinstance(types, list) else [str(types)] if types else []
            aggregated.append(AggregatedEdgeInfo(
                id=f"agg-{s_urn}-{t_urn}",
                sourceUrn=s_urn,
                targetUrn=t_urn,
                edgeCount=w,
                edgeTypes=edge_types,
                confidence=1.0,
                sourceEdgeIds=[],
            ))
            total_edges += w
        return AggregatedEdgeResult(
            aggregatedEdges=aggregated,
            totalSourceEdges=total_edges,
            truncated=len(aggregated) >= AGGREGATED_EDGE_RESULT_CAP,
            lastMaterializedAt=last_materialized_at,
        )

    async def get_trace_lineage(
        self,
        urn: str,
        direction: str,
        depth: int,
        containment_edges: List[str],
        lineage_edges: List[str],
    ) -> LineageResult:
        """
        Execute a targeted lineage trace using dynamic edge lists.
        1. Start at target URN.
        2. Traverse DOWN containment to find children (if any).
        3. Traverse ACROSS lineage edges (upstream/downstream).
        4. Traverse UP containment to find structural context.
        """
        await self._ensure_connected()
        
        safe_containment = [_sanitize_label(t) for t in containment_edges]
        safe_lineage = [_sanitize_label(t) for t in lineage_edges]
        
        # If no lineage edges defined, return just the node
        if not safe_lineage:
            node = await self.get_node(urn)
            return LineageResult(
                nodes=[node] if node else [],
                edges=[],
                upstreamUrns=set(), 
                downstreamUrns=set(),
                totalCount=1 if node else 0,
                hasMore=False
            )

        # 1. Expand Scope: Target + Children
        # Find children using containment edges
        start_urns = {urn}
        if safe_containment:
            # Get children (depth 1 for now, or use *1.. if needed)
            cypher_kids = (
                f"MATCH (p)-[r]->(c) "
                f"WHERE p.urn = $urn AND type(r) IN $containment "
                f"RETURN c.urn"
            )
            res_kids = await self._ro_query(
                cypher_kids, 
                params={"urn": urn, "containment": safe_containment}
            )
            for row in (res_kids.result_set or []):
                start_urns.add(row[0])
        
        # 2. Trace Lineage
        collected_nodes: Dict[str, GraphNode] = {}
        collected_edges: Dict[str, GraphEdge] = {}
        
        upstream_urns = set()
        downstream_urns = set()
        
        if not start_urns:
             return LineageResult(nodes=[], edges=[], upstreamUrns=set(), downstreamUrns=set(), totalCount=0, hasMore=False)

        # Batched BFS: 1 Cypher query per depth level instead of 1 per node.
        # Each iteration processes the entire frontier at once.
        visited_lineage = set(start_urns)
        current_frontier = list(start_urns)

        for current_depth in range(depth):
            if not current_frontier:
                break

            next_frontier_upstream: List[str] = []
            next_frontier_downstream: List[str] = []

            # Build direction-specific batch queries
            dir_queries = []
            if direction in ["upstream", "both"]:
                # Find all nodes that flow INTO the current frontier
                cypher_up = (
                    "MATCH (src)-[r]->(tgt) "
                    "WHERE tgt.urn IN $frontier AND type(r) IN $lineage "
                    "RETURN src, r, tgt"
                )
                dir_queries.append(("upstream", cypher_up))
            if direction in ["downstream", "both"]:
                # Find all nodes that flow OUT of the current frontier
                cypher_down = (
                    "MATCH (src)-[r]->(tgt) "
                    "WHERE src.urn IN $frontier AND type(r) IN $lineage "
                    "RETURN src, r, tgt"
                )
                dir_queries.append(("downstream", cypher_down))

            for dir_label, cypher_q in dir_queries:
                res = await self._ro_query(
                    cypher_q,
                    params={"frontier": current_frontier, "lineage": safe_lineage}
                )

                for row in (res.result_set or []):
                    src_node_obj = self._extract_node_from_result(row[0])
                    edge_obj_raw = row[1]
                    tgt_node_obj = self._extract_node_from_result(row[2])

                    if not src_node_obj or not tgt_node_obj:
                        continue

                    r_type = getattr(edge_obj_raw, "relation", None) or getattr(edge_obj_raw, "type", None) or "UNKNOWN"
                    r_props = getattr(edge_obj_raw, "properties", {})

                    edge = _edge_from_row(src_node_obj.urn, tgt_node_obj.urn, r_type, r_props)

                    if edge.id not in collected_edges:
                        collected_edges[edge.id] = edge
                        collected_nodes[src_node_obj.urn] = src_node_obj
                        collected_nodes[tgt_node_obj.urn] = tgt_node_obj

                        if dir_label == "upstream":
                            neighbor = src_node_obj
                            if neighbor.urn not in visited_lineage:
                                visited_lineage.add(neighbor.urn)
                                upstream_urns.add(neighbor.urn)
                                next_frontier_upstream.append(neighbor.urn)
                        else:
                            neighbor = tgt_node_obj
                            if neighbor.urn not in visited_lineage:
                                visited_lineage.add(neighbor.urn)
                                downstream_urns.add(neighbor.urn)
                                next_frontier_downstream.append(neighbor.urn)

            # Merge frontiers for next depth level
            current_frontier = next_frontier_upstream + next_frontier_downstream

        # 3. Structural Context (Traverse UP)
        # For all collected nodes, find their parents/containers
        all_lineage_urns = list(collected_nodes.keys())
        if all_lineage_urns and safe_containment:
             # Find parents recursively or just immediate? 
             # Usually tracing up to Root is good. keyspace -> table -> column
             
             # Cypher to find ancestors:
             # MATCH (child)<-[r*1..5]-(parent) WHERE child.urn IN $urns AND type(r) IN $containment RETURN parent, r
             # Note: variable length relationship with type filter might be syntax sensitive in FalkorDB
             # MATCH (child)<-[r*1..5]-(parent) ...
             # We can just fetch all ancestors.
             
             # We can process in batches if many nodes
             batch_urns = all_lineage_urns # optimize if huge
             
             # We assume containment is child<-parent (parent IS SOURCE of CONTAINS edge)
             # So we match (parent)-[:CONTAINS]->(child)
             
             cypher_structure = (
                 f"MATCH (parent)-[r]->(child) "
                 f"WHERE child.urn IN $urns AND type(r) IN $containment "
                 f"RETURN parent, r, child"
             )
             
             # We might need to iterate this to go up multiple levels?
             # Or use *1..5
             # Let's try to get full hierarchy for the visible nodes.
             
             # For simpler implementation: Use a loop to climb up.
             # Or rely on get_ancestors if it wasn't one-by-one.
             
             # Let's do a single pass for immediate parents, then loop?
             # Actually, simpler: Just fetch all ancestors for these nodes.
             
             # Batched ancestor fetch — climb containment levels
             current_level_urns = all_lineage_urns
             seen_parents: Set[str] = set(collected_nodes.keys())
             for _ in range(5):  # up to 5 containment levels
                 if not current_level_urns:
                     break

                 res_struct = await self._ro_query(
                     cypher_structure,
                     params={"urns": current_level_urns, "containment": safe_containment}
                 )

                 next_level_urns = []

                 for row in (res_struct.result_set or []):
                     parent = self._extract_node_from_result(row[0])
                     r_raw = row[1]
                     child = self._extract_node_from_result(row[2])

                     if parent and child:
                         collected_nodes[child.urn] = child

                         r_type = getattr(r_raw, "relation", None) or getattr(r_raw, "type", None) or "UNKNOWN"
                         r_props = getattr(r_raw, "properties", {})

                         edge = _edge_from_row(parent.urn, child.urn, r_type, r_props)
                         collected_edges[edge.id] = edge

                         # Only add parent to next level if we haven't seen it before
                         if parent.urn not in seen_parents:
                             seen_parents.add(parent.urn)
                             collected_nodes[parent.urn] = parent
                             next_level_urns.append(parent.urn)

                 if not next_level_urns:
                     break
                 current_level_urns = next_level_urns

        # Ensure original urn is in collected nodes
        if urn not in collected_nodes:
            start_node = await self.get_node(urn)
            if start_node:
                collected_nodes[urn] = start_node

        return LineageResult(
            nodes=list(collected_nodes.values()),
            edges=list(collected_edges.values()),
            upstreamUrns=upstream_urns,
            downstreamUrns=downstream_urns,
            totalCount=len(collected_nodes),
            hasMore=False
        )

    # ------------------------------------------------------------------ #
    # Trace v2 — Cypher-native, ontology-aware lineage                    #
    #                                                                     #
    # Filters AGGREGATED edges by node-level (s.level/t.level) at the    #
    # database layer. Per-hop set-based BFS orchestrated in Python — the  #
    # hot path is a single UNWIND $frontier MATCH per hop, capped by     #
    # LIMIT. Cost is proportional to result size, not graph size.        #
    #                                                                     #
    # Assumes ``in_source`` projection mode (the default): AGGREGATED    #
    # edges and source nodes live in the same graph, so the level filter #
    # can join on s.level/t.level. ``dedicated`` mode requires the       #
    # materializer to project node levels onto shadow nodes — out of     #
    # scope here.                                                         #
    # ------------------------------------------------------------------ #

    async def trace_at_level(
        self,
        urn: str,
        level: int,
        upstream_depth: int,
        downstream_depth: int,
        lineage_edge_types: List[str],
        containment_edge_types: List[str],
        max_nodes: int,
        timeout_ms: int,
        include_containment_edges: bool = False,
        include_inherited_lineage: bool = True,
    ) -> TraceResult:
        await self._ensure_connected()
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        # Normalize edge type lists to UPPERCASE — matches what type(r) returns
        # in FalkorDB and what set_containment_edge_types stores internally.
        ctypes = [t.upper() for t in (containment_edge_types or [])]
        ltypes = [t.upper() for t in (lineage_edge_types or [])] if lineage_edge_types else None

        # Focus node — needed for the response shape regardless of trace outcome
        focus_node = await self.get_node(urn)
        focus_level = self._get_node_level(focus_node.entity_type) if focus_node else level
        focus_entity_type = str(focus_node.entity_type) if focus_node else "unknown"

        # 1. Resolve anchor at the requested level (climb containment if needed)
        anchor_urn = await self._resolve_anchor_at_level(urn, level, ctypes)

        # 2. Inherited-lineage fallback
        is_inherited = False
        inherited_from = None
        if include_inherited_lineage and not await self._has_aggregated_at_level(anchor_urn, level, ltypes):
            parent = await self._find_ancestor_with_lineage(anchor_urn, level, ctypes, ltypes)
            if parent and parent != anchor_urn:
                inherited_from = anchor_urn
                anchor_urn = parent
                is_inherited = True

        # 3. Seed BFS state
        nodes_by_urn: Dict[str, GraphNode] = {}
        anchor_node = await self.get_node(anchor_urn)
        if anchor_node:
            nodes_by_urn[anchor_urn] = anchor_node
        edges_by_id: Dict[str, GraphEdge] = {}
        upstream_urns: Set[str] = set()
        downstream_urns: Set[str] = set()
        visited: Set[str] = {anchor_urn}
        up_frontier: Set[str] = {anchor_urn} if upstream_depth > 0 else set()
        down_frontier: Set[str] = {anchor_urn} if downstream_depth > 0 else set()
        truncation_reason: Optional[str] = None

        # 4. Per-hop set-based expansion
        max_depth = max(upstream_depth, downstream_depth)
        for hop in range(max_depth):
            if time.monotonic() > deadline:
                truncation_reason = "timeout"
                break
            if len(nodes_by_urn) >= max_nodes:
                truncation_reason = "max_nodes"
                break
            budget = max_nodes - len(nodes_by_urn)

            tasks = []
            if hop < upstream_depth and up_frontier:
                tasks.append(("up", self._expand_aggregated_set(
                    list(up_frontier), "incoming", level, ltypes, budget,
                )))
            if hop < downstream_depth and down_frontier:
                tasks.append(("down", self._expand_aggregated_set(
                    list(down_frontier), "outgoing", level, ltypes, budget,
                )))
            if not tasks:
                break

            results = await asyncio.gather(
                *(t[1] for t in tasks), return_exceptions=True
            )

            new_up: Set[str] = set()
            new_down: Set[str] = set()
            for (direction, _), recs in zip(tasks, results):
                if isinstance(recs, Exception):
                    logger.warning("trace_at_level expand (%s) failed: %s", direction, recs)
                    continue
                for rec in recs:
                    edge_id = rec["edgeId"]
                    if edge_id not in edges_by_id:
                        # Use the actual relationship type — AGGREGATED for
                        # rolled-up lineage, or the raw lineage type
                        # (TRANSFORMS, FLOWS_TO, …) when tracing at fine-
                        # grained levels where lineage is not pre-aggregated.
                        actual_type = rec.get("edgeType") or "AGGREGATED"
                        edges_by_id[edge_id] = GraphEdge(
                            id=edge_id,
                            sourceUrn=rec["sourceUrn"],
                            targetUrn=rec["targetUrn"],
                            edgeType=actual_type,
                            properties={
                                "sourceEdgeTypes": rec.get("edgeTypes") or [actual_type],
                                "weight": rec.get("weight") or 1,
                            },
                        )
                    new_node = rec.get("node")
                    if new_node and new_node.urn not in nodes_by_urn:
                        nodes_by_urn[new_node.urn] = new_node
                    other_urn = rec["sourceUrn"] if direction == "up" else rec["targetUrn"]
                    if other_urn not in visited:
                        visited.add(other_urn)
                        if direction == "up":
                            new_up.add(other_urn)
                            upstream_urns.add(other_urn)
                        else:
                            new_down.add(other_urn)
                            downstream_urns.add(other_urn)

            up_frontier = new_up
            down_frontier = new_down
            if not up_frontier and not down_frontier:
                break

        # 5. ALWAYS hydrate the containment chain. A trace returns lineage URNs
        # at whatever level was requested (peer-level by default, finer levels
        # via expand). For the canvas to position those URNs in the layered
        # hierarchy it needs every containment ancestor (Dataset → Container →
        # Domain) AND the parent-child edges linking them. Without this the
        # frontend treats trace nodes as orphans, layer assignment can't place
        # them, and the user sees nothing — which is exactly the schemaField
        # trace bug. The `include_containment_edges` flag is intentionally
        # ignored here: hierarchy context is non-optional for trace responses.
        containment_edges_list: List[GraphEdge] = []
        if ctypes and nodes_by_urn:
            ancestor_urns = await self._collect_ancestor_urns(
                list(nodes_by_urn.keys()), ctypes,
            )
            new_ancestors = [u for u in ancestor_urns if u not in nodes_by_urn]
            if new_ancestors:
                ancestor_nodes = await self.get_nodes_batch(new_ancestors)
                for n in ancestor_nodes:
                    if n:
                        nodes_by_urn[n.urn] = n
            # Containment edges between every returned node — both lineage
            # participants and their hydrated ancestors.
            if len(nodes_by_urn) > 1:
                containment_edges_list = await self._fetch_containment_edges(
                    list(nodes_by_urn.keys()), ctypes,
                )

        return TraceResult(
            nodes=list(nodes_by_urn.values()),
            edges=list(edges_by_id.values()),
            containmentEdges=containment_edges_list,
            upstreamUrns=upstream_urns,
            downstreamUrns=downstream_urns,
            focus=TraceFocus(
                urn=urn,
                level=focus_level if focus_level is not None else level,
                entityType=focus_entity_type,
            ),
            effectiveLevel=level,
            isInherited=is_inherited,
            inheritedFromUrn=inherited_from,
            truncated=(truncation_reason is not None),
            truncationReason=truncation_reason,
        )

    async def expand_aggregated(
        self,
        source_urn: str,
        target_urn: str,
        next_level: int,
        lineage_edge_types: List[str],
        containment_edge_types: List[str],
        max_nodes: int,
        timeout_ms: int,
        use_raw_edges: bool = False,
        include_containment_edges: bool = False,
    ) -> TraceResult:
        await self._ensure_connected()
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        ctypes = [t.upper() for t in (containment_edge_types or [])]
        ltypes = [t.upper() for t in (lineage_edge_types or [])] if lineage_edge_types else None

        # Steps 1+2 in parallel: collect descendants of each anchor at next_level
        s_task = self._collect_descendants_at_level(source_urn, next_level, ctypes, max_nodes)
        t_task = self._collect_descendants_at_level(target_urn, next_level, ctypes, max_nodes)
        s_urns, t_urns = await asyncio.gather(s_task, t_task)

        truncation_reason: Optional[str] = None
        if time.monotonic() > deadline:
            truncation_reason = "timeout"

        # Step 3: edges between the two URN sets — set membership, not Cartesian
        edges: List[GraphEdge] = []
        node_urns_in_edges: Set[str] = set()
        if s_urns and t_urns and not truncation_reason:
            edges = await self._edges_between_sets(
                s_urns, t_urns, next_level, ltypes,
                use_raw=use_raw_edges, limit=max_nodes,
            )
            for e in edges:
                node_urns_in_edges.add(e.source_urn)
                node_urns_in_edges.add(e.target_urn)

        # Hydrate nodes for every URN that appears in the result
        all_urns = (set(s_urns) | set(t_urns)) & node_urns_in_edges if edges else (set(s_urns) | set(t_urns))
        # Cap to max_nodes — favour nodes that participate in edges
        if len(all_urns) > max_nodes:
            in_edges = list(node_urns_in_edges)[:max_nodes]
            all_urns = set(in_edges)
            truncation_reason = truncation_reason or "max_nodes"

        nodes = await self.get_nodes_batch(list(all_urns)) if all_urns else []
        nodes_by_urn = {n.urn: n for n in nodes if n}

        # Always hydrate containment ancestors + edges so the drilled-into
        # nodes can be positioned in the canvas hierarchy. See trace_at_level
        # for the rationale — the `include_containment_edges` flag is
        # intentionally ignored because hierarchy context is non-optional.
        containment_edges_list: List[GraphEdge] = []
        if ctypes and nodes_by_urn:
            ancestor_urns = await self._collect_ancestor_urns(
                list(nodes_by_urn.keys()), ctypes,
            )
            new_ancestors = [u for u in ancestor_urns if u not in nodes_by_urn]
            if new_ancestors:
                ancestor_nodes = await self.get_nodes_batch(new_ancestors)
                for n in ancestor_nodes:
                    if n:
                        nodes_by_urn[n.urn] = n
            if len(nodes_by_urn) > 1:
                containment_edges_list = await self._fetch_containment_edges(
                    list(nodes_by_urn.keys()), ctypes,
                )

        # Focus node for response — use the source anchor of the drill
        anchor_node = nodes_by_urn.get(source_urn)
        if anchor_node is None:
            anchor_node = await self.get_node(source_urn)
        focus_level_actual = (
            self._get_node_level(anchor_node.entity_type) if anchor_node else next_level
        )

        return TraceResult(
            nodes=list(nodes_by_urn.values()),
            edges=edges,
            containmentEdges=containment_edges_list,
            upstreamUrns=set(),
            downstreamUrns=set(),
            focus=TraceFocus(
                urn=source_urn,
                level=focus_level_actual if focus_level_actual is not None else next_level,
                entityType=str(anchor_node.entity_type) if anchor_node else "unknown",
            ),
            effectiveLevel=next_level,
            isInherited=False,
            inheritedFromUrn=None,
            truncated=(truncation_reason is not None),
            truncationReason=truncation_reason,
        )

    # ---- trace v2 helpers ---------------------------------------------------

    async def _resolve_anchor_at_level(
        self, urn: str, level: int, ctypes: List[str],
    ) -> str:
        """Walk UP containment from ``urn`` to find the nearest ancestor whose
        entity type sits at ``level``. Returns ``urn`` itself when it's already
        at the target level or no qualifying ancestor exists.

        Filters by ``labels(anc)[0] IN $types`` rather than ``anc.level``: labels
        are written at every upsert, so this works on every existing graph
        without requiring backfill_node_levels.py to have run.
        """
        if not ctypes:
            return urn
        types = self._types_at_level(level)
        if not types:
            # Ontology hasn't been injected, or no types at this level.
            # Without a label filter we'd unconditionally walk up containment;
            # safer to return the focus as-is and let the BFS try its luck.
            return urn
        cypher = (
            "MATCH (focus {urn: $urn}) "
            "OPTIONAL MATCH path = (focus)<-[c*0..10]-(anc) "
            "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            "  AND labels(anc)[0] IN $types "
            "RETURN coalesce(anc.urn, focus.urn) AS anchorUrn "
            "ORDER BY length(path) ASC LIMIT 1"
        )
        try:
            result = await self._ro_query(
                cypher, params={"urn": urn, "ctypes": ctypes, "types": types},
            )
            rows = result.result_set or []
            if rows and rows[0]:
                return rows[0][0] or urn
        except Exception as exc:
            logger.warning("trace_at_level: anchor resolution failed for %s: %s", urn, exc)
        return urn

    async def _has_aggregated_at_level(
        self, anchor_urn: str, level: int, ltypes: Optional[List[str]] = None,
    ) -> bool:
        """True iff the anchor has AT LEAST ONE lineage edge to a peer at
        the given level. Counts both AGGREGATED rollups AND raw lineage edges
        of any type listed in ``ltypes`` — without this, fine-grained focuses
        whose lineage is expressed as TRANSFORMS / FLOWS_TO / etc. would be
        misclassified as "no lineage", triggering the inherited-lineage
        fallback to climb to a coarser ancestor.
        """
        types = self._types_at_level(level)
        if not types:
            # If we can't tell which entity types belong to this level, assume
            # the focus has direct lineage so the inherited-lineage fallback
            # doesn't fire — that fallback only makes sense with type info.
            return True

        # Build the relationship-type filter: AGGREGATED OR any raw lineage
        # type the caller declared. We match any relationship and filter via
        # type(r) so the same query covers both.
        if ltypes:
            ltype_clause = "AND (type(r) = 'AGGREGATED' OR type(r) IN $ltypes) "
        else:
            ltype_clause = "AND type(r) = 'AGGREGATED' "

        cypher = (
            "MATCH (a {urn: $anchor})-[r]-(peer) "
            "WHERE labels(peer)[0] IN $types "
            + ltype_clause
            + "RETURN 1 LIMIT 1"
        )
        params: Dict[str, Any] = {"anchor": anchor_urn, "types": types}
        if ltypes:
            params["ltypes"] = ltypes
        try:
            result = await self._proj_ro_query(cypher, params=params)
            return bool(result.result_set)
        except Exception as exc:
            logger.warning("trace_at_level: has-lineage check failed for %s: %s", anchor_urn, exc)
            return True  # fail-open: skip the inherited-lineage fallback

    async def _find_ancestor_with_lineage(
        self, anchor_urn: str, level: int, ctypes: List[str],
        ltypes: Optional[List[str]] = None,
    ) -> Optional[str]:
        if not ctypes:
            return None
        types = self._types_at_level(level)
        if not types:
            return None
        cypher_ancestors = (
            "MATCH (a {urn: $anchor})<-[c*1..10]-(parent) "
            "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            "  AND labels(parent)[0] IN $types "
            "RETURN parent.urn AS urn, length(c) AS depth "
            "ORDER BY length(c) ASC LIMIT 5"
        )
        try:
            result = await self._ro_query(
                cypher_ancestors, params={"anchor": anchor_urn, "ctypes": ctypes, "types": types},
            )
            for row in (result.result_set or []):
                candidate = row[0]
                if candidate and await self._has_aggregated_at_level(candidate, level, ltypes):
                    return candidate
        except Exception as exc:
            logger.warning("trace_at_level: find-ancestor-with-lineage failed for %s: %s", anchor_urn, exc)
        return None

    async def _expand_aggregated_set(
        self, frontier: List[str], direction: str, level: int,
        ltypes: Optional[List[str]], limit: int,
    ) -> List[Dict[str, Any]]:
        """Per-hop expansion. Direction: 'incoming' (BFS upstream) or 'outgoing'
        (BFS downstream). Returns a list of dicts shaped for the BFS loop:
        {sourceUrn, targetUrn, edgeId, edgeType, edgeTypes, weight, node}.

        Walks ANY lineage edge type listed in ``ltypes`` — not just the
        materialized AGGREGATED rollup. Critical for fine-grained levels
        (column / schemaField) where lineage is expressed as raw TRANSFORMS,
        FLOWS_TO, or other ontology-classified edges that are never aggregated.

        For AGGREGATED relationships we keep the original ``r.sourceEdgeTypes``
        intersection check (so the user's edge-type filter still narrows the
        rollup). For raw lineage relationships we just check ``type(r)`` is in
        ``ltypes`` — the relationship type itself IS the edge classification.

        Filters by ``labels(other)[0] IN $types`` rather than ``other.level``
        so the trace works on every existing graph (labels are written at every
        upsert; ``n.level`` only after backfill_node_levels.py runs). When the
        ontology has no entity types at the requested level, the level filter
        is dropped entirely.
        """
        if not frontier or limit <= 0:
            return []

        types = self._types_at_level(level)
        # Two switches: direction (incoming/outgoing) and type-filter presence.
        type_filter = ""
        if types:
            other = "s" if direction == "incoming" else "t"
            type_filter = f"AND labels({other})[0] IN $types "

        # Edge-type filter:
        # - If ltypes provided, walk relationships whose type matches AGGREGATED
        #   (with the sub-type intersection on r.sourceEdgeTypes) OR whose type
        #   is one of the raw lineage types in ltypes.
        # - If ltypes is empty, walk only AGGREGATED (legacy behavior).
        if ltypes:
            ltype_filter = (
                "AND ("
                "  (type(r) = 'AGGREGATED' AND r.sourceEdgeTypes IS NOT NULL "
                "     AND any(et IN r.sourceEdgeTypes WHERE et IN $ltypes)) "
                "  OR (type(r) IN $ltypes) "
                ") "
            )
            rel_pattern = ""  # match any relationship type
        else:
            ltype_filter = ""
            rel_pattern = ":AGGREGATED"  # legacy fallback

        if direction == "incoming":
            # Find sources flowing INTO frontier targets
            cypher = (
                "UNWIND $frontier AS srcUrn "
                f"MATCH (s)-[r{rel_pattern}]->(t) "
                "WHERE t.urn = srcUrn "
                + type_filter
                + ltype_filter
                + "RETURN s.urn AS sourceUrn, t.urn AS targetUrn, "
                "id(r) AS edgeId, type(r) AS edgeType, "
                "COALESCE(r.sourceEdgeTypes, [type(r)]) AS edgeTypes, "
                "COALESCE(r.weight, 1) AS weight, s AS otherNode "
                "LIMIT $limit"
            )
        else:
            cypher = (
                "UNWIND $frontier AS srcUrn "
                f"MATCH (s)-[r{rel_pattern}]->(t) "
                "WHERE s.urn = srcUrn "
                + type_filter
                + ltype_filter
                + "RETURN s.urn AS sourceUrn, t.urn AS targetUrn, "
                "id(r) AS edgeId, type(r) AS edgeType, "
                "COALESCE(r.sourceEdgeTypes, [type(r)]) AS edgeTypes, "
                "COALESCE(r.weight, 1) AS weight, t AS otherNode "
                "LIMIT $limit"
            )

        params: Dict[str, Any] = {"frontier": frontier, "limit": limit}
        if types:
            params["types"] = types
        if ltypes:
            params["ltypes"] = ltypes

        try:
            result = await self._proj_ro_query(cypher, params=params)
        except Exception as exc:
            logger.warning("trace_at_level: expand (%s) failed: %s", direction, exc)
            return []

        out: List[Dict[str, Any]] = []
        for row in (result.result_set or []):
            try:
                edge_type = str(row[3]) if row[3] is not None else "AGGREGATED"
                rec = {
                    "sourceUrn": row[0],
                    "targetUrn": row[1],
                    "edgeId": str(row[2]) if row[2] is not None else f"{edge_type.lower()}-{row[0]}-{row[1]}",
                    "edgeType": edge_type,
                    "edgeTypes": row[4] if isinstance(row[4], list) else ([row[4]] if row[4] else [edge_type]),
                    "weight": int(row[5]) if row[5] is not None else 1,
                    "node": self._extract_node_from_result([row[6]]) if row[6] is not None else None,
                }
                out.append(rec)
            except Exception:
                continue
        return out

    async def _collect_ancestor_urns(
        self, urns: List[str], ctypes: List[str],
    ) -> List[str]:
        """Collect ALL containment ancestors of the given URNs in one query.

        Foundational for trace responses: a trace returns lineage URNs at
        whatever level the user picked (e.g. column-level schemaFields), but
        the canvas needs the full ancestor chain (Dataset → Container →
        Domain) to position those URNs in the layered hierarchy. Without
        this, the trace nodes render as orphans or get filtered out by layer
        assignment.

        Set-based, deduped, capped at depth 10. Returns URNs only; caller
        fetches the node payloads via ``get_nodes_batch``.
        """
        if not urns or not ctypes:
            return []
        cypher = (
            "UNWIND $urns AS u "
            "MATCH (n {urn: u})<-[c*1..10]-(ancestor) "
            "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            "RETURN DISTINCT ancestor.urn AS ancestorUrn"
        )
        try:
            result = await self._ro_query(cypher, params={"urns": urns, "ctypes": ctypes})
            out: List[str] = []
            for row in (result.result_set or []):
                if row and row[0]:
                    out.append(row[0])
            return out
        except Exception as exc:
            logger.warning("trace_at_level: ancestor collection failed for %d urns: %s", len(urns), exc)
            return []

    async def _collect_descendants_at_level(
        self, anchor_urn: str, target_level: int, ctypes: List[str], limit: int,
    ) -> List[str]:
        """Collect URNs of descendants of ``anchor_urn`` whose entity type
        sits at ``target_level``. Bounded depth-10 containment descent;
        capped by limit. Filters by labels (always present) rather than
        ``n.level`` (requires backfill).
        """
        types = self._types_at_level(target_level)
        if not types:
            return []

        if not ctypes:
            cypher = (
                "MATCH (a {urn: $anchor}) WHERE labels(a)[0] IN $types "
                "RETURN [a.urn] AS urns"
            )
            params = {"anchor": anchor_urn, "types": types}
        else:
            cypher = (
                "MATCH (a {urn: $anchor})-[c*0..10]->(child) "
                "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
                "  AND labels(child)[0] IN $types "
                "RETURN collect(DISTINCT child.urn)[..$limit] AS urns"
            )
            params = {"anchor": anchor_urn, "ctypes": ctypes, "types": types, "limit": limit}
        try:
            result = await self._ro_query(cypher, params=params)
            rows = result.result_set or []
            if rows and rows[0]:
                value = rows[0][0]
                if isinstance(value, list):
                    return [u for u in value if u]
        except Exception as exc:
            logger.warning("trace_at_level: descendant collection failed for %s: %s", anchor_urn, exc)
        return []

    async def _edges_between_sets(
        self, s_urns: List[str], t_urns: List[str], level: int,
        ltypes: Optional[List[str]], use_raw: bool, limit: int,
    ) -> List[GraphEdge]:
        """Fetch edges between two URN sets — set membership, not Cartesian.

        ``use_raw=True`` reads raw lineage edges (for finest level where
        AGGREGATED == raw). Otherwise reads AGGREGATED.
        """
        if not s_urns or not t_urns:
            return []

        if use_raw:
            # Raw lineage edges by type — caller passes ltypes (lineage types)
            ltypes_eff = ltypes or []
            if not ltypes_eff:
                return []
            cypher = (
                "UNWIND $sUrns AS srcUrn "
                "MATCH (s {urn: srcUrn})-[r]->(t) "
                "WHERE t.urn IN $tUrns AND type(r) IN $ltypes "
                "RETURN s.urn AS sUrn, t.urn AS tUrn, type(r) AS edgeType, "
                "id(r) AS edgeId, properties(r) AS props "
                "LIMIT $limit"
            )
            params = {"sUrns": s_urns, "tUrns": t_urns, "ltypes": ltypes_eff, "limit": limit}
            graph_query = self._ro_query
        else:
            cypher = (
                "UNWIND $sUrns AS srcUrn "
                "MATCH (s {urn: srcUrn})-[r:AGGREGATED]->(t) "
                "WHERE t.urn IN $tUrns "
                + ("AND any(et IN r.sourceEdgeTypes WHERE et IN $ltypes) " if ltypes else "")
                + "RETURN s.urn AS sUrn, t.urn AS tUrn, 'AGGREGATED' AS edgeType, "
                "id(r) AS edgeId, "
                "{sourceEdgeTypes: r.sourceEdgeTypes, weight: r.weight} AS props "
                "LIMIT $limit"
            )
            params = {"sUrns": s_urns, "tUrns": t_urns, "limit": limit}
            if ltypes:
                params["ltypes"] = ltypes
            graph_query = self._proj_ro_query

        try:
            result = await graph_query(cypher, params=params)
        except Exception as exc:
            logger.warning("expand_aggregated: edge fetch failed: %s", exc)
            return []

        out: List[GraphEdge] = []
        seen_ids: Set[str] = set()
        for row in (result.result_set or []):
            try:
                edge_id = str(row[3]) if row[3] is not None else f"{row[2]}-{row[0]}-{row[1]}"
                if edge_id in seen_ids:
                    continue
                seen_ids.add(edge_id)
                props = row[4] if isinstance(row[4], dict) else {}
                out.append(GraphEdge(
                    id=edge_id,
                    sourceUrn=row[0],
                    targetUrn=row[1],
                    edgeType=str(row[2]),
                    properties=props or {},
                ))
            except Exception:
                continue
        return out

    async def _fetch_containment_edges(
        self, urns: List[str], ctypes: List[str],
    ) -> List[GraphEdge]:
        """One Cypher: containment edges where both endpoints are in ``urns``."""
        if not urns or not ctypes:
            return []
        cypher = (
            "UNWIND $urns AS u "
            "MATCH (s {urn: u})-[r]->(t) "
            "WHERE t.urn IN $urns AND type(r) IN $ctypes "
            "RETURN s.urn AS sUrn, t.urn AS tUrn, type(r) AS edgeType, "
            "id(r) AS edgeId"
        )
        try:
            result = await self._ro_query(cypher, params={"urns": urns, "ctypes": ctypes})
        except Exception as exc:
            logger.warning("trace_at_level: containment edge fetch failed: %s", exc)
            return []
        out: List[GraphEdge] = []
        for row in (result.result_set or []):
            try:
                out.append(GraphEdge(
                    id=str(row[3]),
                    sourceUrn=row[0],
                    targetUrn=row[1],
                    edgeType=str(row[2]),
                    properties={},
                ))
            except Exception:
                continue
        return out

    async def get_nodes_batch(self, urns: List[str]) -> List[GraphNode]:
        """Bulk node fetch by URN — used by trace v2 to hydrate nodes after BFS."""
        if not urns:
            return []
        try:
            result = await self._ro_query(
                "MATCH (n) WHERE n.urn IN $urns RETURN n",
                params={"urns": urns},
            )
            out: List[GraphNode] = []
            for row in (result.result_set or []):
                node = self._extract_node_from_result(row)
                if node:
                    out.append(node)
            return out
        except Exception as exc:
            logger.warning("get_nodes_batch failed: %s", exc)
            return []

    # Schema-level caches are persisted in Postgres by the stats service;
    # this in-memory Redis layer is just a short-term memoization for
    # repeated calls within a polling interval. Default 300s (5 min) —
    # matches the stats service poll interval. Set to 0 to disable.
    _SCHEMA_CACHE_TTL = int(os.getenv("FALKORDB_SCHEMA_CACHE_TTL", "300"))

    async def get_stats(self) -> Dict[str, Any]:
        await self._ensure_connected()

        # Check Redis cache (best-effort; Postgres is the source of truth)
        cache_key = f"{self._graph_name}:stats_cache"
        if self._SCHEMA_CACHE_TTL > 0:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        # Optimize: Combine node counting with type aggregation
        type_res = await self._ro_query(
            "MATCH (n) RETURN labels(n)[0] AS lbl, count(*) AS c"
        )
        entity_type_counts = {}
        node_count = 0
        for row in (type_res.result_set or []):
            lbl = row[0] or "unknown"
            cnt = row[1]
            entity_type_counts[lbl] = cnt
            node_count += cnt

        # Optimize: Combine edge counting with type aggregation
        edge_type_res = await self._ro_query(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c"
        )
        edge_type_counts = {}
        edge_count = 0
        for row in (edge_type_res.result_set or []):
            t = row[0] or "UNKNOWN"
            cnt = row[1]
            edge_type_counts[t] = cnt
            edge_count += cnt

        result = {
            "nodeCount": node_count,
            "edgeCount": edge_count,
            "entityTypeCounts": entity_type_counts,
            "edgeTypeCounts": edge_type_counts,
        }

        if self._SCHEMA_CACHE_TTL > 0:
            try:
                await self._redis.setex(cache_key, self._SCHEMA_CACHE_TTL, json.dumps(result))
            except Exception:
                pass

        return result

    async def get_schema_stats(self) -> GraphSchemaStats:
        await self._ensure_connected()
        
        # Single query: counts + samples per label using collect() with slicing
        type_res = await self._ro_query(
            "MATCH (n) "
            "WITH labels(n)[0] AS lbl, n.displayName AS name "
            "WITH lbl, count(*) AS c, collect(name)[0..3] AS samples "
            "RETURN lbl, c, samples"
        )

        entity_stats = []
        total_nodes = 0

        for row in (type_res.result_set or []):
            lbl = row[0] or "unknown"
            cnt = row[1]
            samples = [s for s in (row[2] or []) if s]
            total_nodes += cnt
            entity_stats.append(EntityTypeSummary(id=lbl, name=lbl, count=cnt, sampleNames=samples))

        edge_type_res = await self._ro_query(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c"
        )
        edge_stats = []
        total_edges = 0
        
        for row in (edge_type_res.result_set or []):
            t = row[0] or "UNKNOWN"
            cnt = row[1]
            edge_stats.append(EdgeTypeSummary(id=t, name=t, count=cnt))
            total_edges += cnt

        # Tag stats - kept as is for now, but ensured safe execution
        try:
            tag_res = await self._ro_query(
                "MATCH (n) WHERE n.tags IS NOT NULL AND n.tags <> '[]' RETURN n.tags"
            )
            tag_counts: Dict[str, int] = {}
            tag_types: Dict[str, Set[str]] = {}
            for row in (tag_res.result_set or []):
                tags_raw = row[0]
                try:
                    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
                except Exception:
                    continue
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if tag not in tag_types:
                        tag_types[tag] = set()
                    tag_types[tag].add("entity")
            tag_stats = [TagSummary(tag=t, count=c, entityTypes=list(tag_types.get(t, {"entity"}))) for t, c in tag_counts.items()]
        except Exception as e:
            logger.warning(f"Failed to fetch tag stats: {e}")
            tag_stats = []

        return GraphSchemaStats(
            totalNodes=total_nodes,
            totalEdges=total_edges,
            entityTypeStats=entity_stats,
            edgeTypeStats=edge_stats,
            tagStats=tag_stats,
        )

    async def get_ontology_metadata(self) -> OntologyMetadata:
        """
        Build ontology metadata including containment and lineage roles.
        Optimized to use Cypher aggregations instead of full scans.
        Cached in Redis with 60s TTL — ontology rarely changes.
        """
        await self._ensure_connected()

        cache_key = f"{self._graph_name}:ontology_cache"
        if self._SCHEMA_CACHE_TTL > 0:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    return OntologyMetadata(**json.loads(cached))
            except Exception:
                pass

        containment = list(self._get_containment_edge_types())
        containment_upper = {t.upper() for t in containment}
        
        # 1. Determine Lineage Types
        # Instead of fetching all edges, we query distinct types
        type_res = await self._ro_query("MATCH ()-[r]->() RETURN DISTINCT type(r)")
        all_types = [row[0] for row in (type_res.result_set or [])]
        
        # Use ontology-resolved edge metadata if available, otherwise fall back to heuristics
        resolved_meta = getattr(self, "_resolved_edge_metadata", None)
        resolved_lineage = getattr(self, "_resolved_lineage_types", None)

        if resolved_meta is not None and resolved_lineage is not None:
            # Ontology-driven classification
            lineage_types = [t for t in all_types if t.upper() in resolved_lineage]
        else:
            # Heuristic fallback (pre-ontology or no ontology)
            config_lineage = os.getenv("LINEAGE_EDGE_TYPES", "").strip()
            if config_lineage:
                lineage_types = [t.strip() for t in config_lineage.split(",") if t.strip()]
            else:
                config_metadata = os.getenv("METADATA_EDGE_TYPES", "").strip()
                metadata_types = {t.strip().upper() for t in config_metadata.split(",") if t.strip()} if config_metadata else set()
                lineage_types = []
                for t in all_types:
                    if t.upper() not in containment_upper and t.upper() not in metadata_types and t.upper() != "AGGREGATED":
                        lineage_types.append(t)

        lineage_upper = {t.upper() for t in lineage_types}

        # 2. Build Edge Metadata
        edge_type_metadata: Dict[str, EdgeTypeMetadata] = {}
        for et in all_types:
            et_upper = et.upper()
            is_containment = et_upper in containment_upper
            is_lineage = et_upper in lineage_upper

            # Prefer resolved ontology metadata for direction/category
            if resolved_meta and et_upper in resolved_meta:
                meta = resolved_meta[et_upper]
                direction = meta.get("direction", "bidirectional") if isinstance(meta, dict) else getattr(meta, "direction", "bidirectional")
                category = meta.get("category", "association") if isinstance(meta, dict) else getattr(meta, "category", "association")
            elif is_containment:
                category = "structural"
                direction = "parent-to-child"
            elif is_lineage:
                category = "flow"
                direction = "source-to-target"
            else:
                category = "association"
                direction = "bidirectional"

            edge_type_metadata[et] = EdgeTypeMetadata(
                isContainment=is_containment,
                isLineage=is_lineage,
                direction=direction,
                category=category,
                description=f"{category} relationship: {et}",
            )

        # 3. Build Entity Hierarchy
        # Query containment relationships directly
        hierarchy_cypher = (
            "MATCH (p)-[r]->(c) "
            "WHERE type(r) IN $containment "
            "RETURN DISTINCT labels(p)[0], labels(c)[0], type(r)"
        )
        hierarchy_res = await self._ro_query(
            hierarchy_cypher, 
            params={"containment": containment}
        )
        
        entity_type_hierarchy: Dict[str, EntityTypeHierarchy] = {}
        found_parent_types = set()
        found_child_types = set()
        
        for row in (hierarchy_res.result_set or []):
            p_type, c_type, r_type = row[0], row[1], row[2]
            if not p_type or not c_type: continue
            
            # Normalize for direction
            meta = edge_type_metadata.get(r_type)
            if meta and meta.direction == "child-to-parent":
                parent_t, child_t = c_type, p_type
            else:
                parent_t, child_t = p_type, c_type
                
            if parent_t not in entity_type_hierarchy:
                entity_type_hierarchy[parent_t] = EntityTypeHierarchy(canContain=[], canBeContainedBy=[])
            if child_t not in entity_type_hierarchy:
                entity_type_hierarchy[child_t] = EntityTypeHierarchy(canContain=[], canBeContainedBy=[])
                
            if child_t not in entity_type_hierarchy[parent_t].can_contain:
                entity_type_hierarchy[parent_t].can_contain.append(child_t)
            if parent_t not in entity_type_hierarchy[child_t].can_be_contained_by:
                entity_type_hierarchy[child_t].can_be_contained_by.append(parent_t)
                
            found_parent_types.add(parent_t)
            found_child_types.add(child_t)

        root_entity_types = list(found_parent_types - found_child_types)

        result = OntologyMetadata(
            containmentEdgeTypes=containment,
            lineageEdgeTypes=lineage_types,
            edgeTypeMetadata=edge_type_metadata,
            entityTypeHierarchy=entity_type_hierarchy,
            rootEntityTypes=root_entity_types,
        )

        if self._SCHEMA_CACHE_TTL > 0:
            try:
                await self._redis.setex(cache_key, self._SCHEMA_CACHE_TTL, result.model_dump_json())
            except Exception:
                pass

        return result

    async def get_distinct_values(self, property_name: str) -> List[Any]:
        await self._ensure_connected()
        if property_name in ("entityType", "entitytype"):
            res = await self._ro_query("MATCH (n) RETURN DISTINCT labels(n)[0] AS lbl")
            return [row[0] for row in (res.result_set or []) if row[0]]
        if property_name == "tags":
            res = await self._ro_query("MATCH (n) RETURN n.tags")
            seen = set()
            for row in (res.result_set or []):
                raw = row[0]
                try:
                    tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
                    for t in tags:
                        seen.add(t)
                except Exception:
                    pass
            return list(seen)
        safe_prop = "".join(c for c in property_name if c.isalnum() or c == "_") or "urn"
        try:
            res = await self._ro_query(
                f"MATCH (n) WHERE n.{safe_prop} IS NOT NULL RETURN DISTINCT n.{safe_prop} AS v LIMIT 100"
            )
            return [row[0] for row in (res.result_set or [])]
        except Exception:
            return []

    async def get_ancestors(self, urn: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        """Get ancestors using pre-computed Redis chain (2 calls: 1 Redis + 1 Cypher)."""
        await self._ensure_connected()
        chain = await self._get_ancestor_chain(urn)
        chain = chain[offset : offset + limit]
        if not chain:
            return []
        nodes = await self.get_nodes(NodeQuery(urns=chain, limit=len(chain), include_child_count=False))
        # Preserve containment order (parent → grandparent → ...)
        urn_to_node = {n.urn: n for n in nodes}
        return [urn_to_node[u] for u in chain if u in urn_to_node]

    async def get_descendants(
        self,
        urn: str,
        depth: int = 5,
        entity_types: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[GraphNode]:
        """Single Cypher query to fetch descendants instead of per-node BFS."""
        await self._ensure_connected()
        containment = list(self._get_containment_edge_types())
        if not containment:
            # No containment types — flat graph, no descendants
            return []
        containment_cypher = "|".join([_sanitize_label(t) for t in containment])

        conditions = ["root.urn = $urn"]
        params: Dict[str, Any] = {"urn": urn, "skip": offset, "lim": limit}

        if entity_types:
            types = [t.value if hasattr(t, "value") else str(t) for t in entity_types]
            params["entityTypes"] = types
            conditions.append("labels(desc)[0] IN $entityTypes")

        where = " AND ".join(conditions)
        cypher = (
            f"MATCH (root)-[:{containment_cypher}*1..{depth}]->(desc) "
            f"WHERE {where} "
            f"RETURN DISTINCT desc "
            f"SKIP $skip LIMIT $lim"
        )

        result = await self._ro_query(cypher, params=params)
        nodes = []
        for row in (result.result_set or []):
            n = self._extract_node_from_result(row)
            if n:
                nodes.append(n)
        return nodes

    async def get_nodes_by_tag(self, tag: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        await self._ensure_connected()
        tag_pattern = json.dumps(tag)
        result = await self._ro_query(
            "MATCH (n) WHERE n.tags IS NOT NULL AND n.tags CONTAINS $tag RETURN n SKIP $skip LIMIT $limit",
            params={"tag": tag_pattern, "skip": offset, "limit": limit},
        )
        nodes = []
        for row in (result.result_set or []):
            n = self._extract_node_from_result(row)
            if n and tag in (n.tags or []):
                nodes.append(n)
        return nodes

    async def get_nodes_by_layer(self, layer_id: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        await self._ensure_connected()
        result = await self._ro_query(
            "MATCH (n) WHERE n.layerAssignment = $lid RETURN n SKIP $skip LIMIT $limit",
            params={"lid": layer_id, "skip": offset, "limit": limit},
        )
        return [self._extract_node_from_result(row) for row in (result.result_set or []) if self._extract_node_from_result(row)]

    async def save_custom_graph(self, nodes: List[GraphNode], edges: List[GraphEdge]) -> bool:
        """Batch-save nodes and edges using UNWIND for bulk writes.

        Groups nodes by label (entity type) so each UNWIND+MERGE targets
        a single label — enabling index-assisted lookups. Turns N individual
        queries into ceil(N/batch_size) queries per label.
        """
        await self._ensure_connected()
        batch_size = 500

        # Group nodes by label for label-specific MERGE
        nodes_by_label: Dict[str, list] = defaultdict(list)
        for node in nodes:
            label = _sanitize_label(str(node.entity_type))
            nodes_by_label[label].append({
                "urn": node.urn,
                "displayName": node.display_name or "",
                "qualifiedName": node.qualified_name or "",
                "description": node.description or "",
                "properties": json.dumps(node.properties),
                "tags": json.dumps(node.tags or []),
                "layerAssignment": node.layer_assignment or "",
                "childCount": node.child_count or 0,
                "sourceSystem": node.source_system or "",
                "lastSyncedAt": node.last_synced_at or "",
                "level": self._get_node_level(node.entity_type),
            })

        # Bulk-cache urn→label mappings
        label_mapping = {}
        for label, items in nodes_by_label.items():
            for item in items:
                label_mapping[item["urn"]] = label
            for i in range(0, len(items), batch_size):
                batch = items[i : i + batch_size]
                try:
                    # Note: SET n.level is conditional via COALESCE — if the engine
                    # hasn't injected the entity-type→level map (e.g. seed-from-file
                    # before ontology resolution), level is null and we leave the
                    # existing value untouched. Backfill script handles those nodes.
                    await self._query(
                        f"UNWIND $batch AS item "
                        f"MERGE (n:{label} {{urn: item.urn}}) "
                        f"SET n.displayName = item.displayName, "
                        f"n.qualifiedName = item.qualifiedName, "
                        f"n.description = item.description, "
                        f"n.properties = item.properties, "
                        f"n.tags = item.tags, "
                        f"n.layerAssignment = item.layerAssignment, "
                        f"n.childCount = item.childCount, "
                        f"n.sourceSystem = item.sourceSystem, "
                        f"n.lastSyncedAt = item.lastSyncedAt, "
                        f"n.level = coalesce(item.level, n.level)",
                        params={"batch": batch},
                    )
                except Exception as e:
                    logger.warning(f"Batch node merge failed for label {label}: {e}")
        await self._cache_urn_labels_bulk(label_mapping)

        # Group edges by relationship type for type-specific MERGE
        edges_by_type: Dict[str, list] = defaultdict(list)
        for edge in edges:
            rel_type = _sanitize_label(str(edge.edge_type))
            edges_by_type[rel_type].append({
                "src": edge.source_urn,
                "tgt": edge.target_urn,
                "eid": edge.id,
                "conf": edge.confidence,
                "props": json.dumps(edge.properties),
            })

        for rel_type, items in edges_by_type.items():
            for i in range(0, len(items), batch_size):
                batch = items[i : i + batch_size]
                try:
                    await self._query(
                        f"UNWIND $batch AS item "
                        f"MATCH (a {{urn: item.src}}) "
                        f"MATCH (b {{urn: item.tgt}}) "
                        f"MERGE (a)-[r:{rel_type}]->(b) "
                        f"SET r.id = item.eid, r.confidence = item.conf, "
                        f"r.properties = item.props",
                        params={"batch": batch},
                    )
                except Exception as e:
                    logger.warning(f"Batch edge merge failed for type {rel_type}: {e}")

        return True

    async def create_node(self, node: GraphNode, containment_edge: Optional[GraphEdge] = None) -> bool:
        await self._ensure_connected()
        try:
            label = _sanitize_label(str(node.entity_type))
            params = {
                "urn": node.urn,
                "displayName": node.display_name or "",
                "qualifiedName": node.qualified_name or "",
                "description": node.description or "",
                "properties": json.dumps(node.properties),
                "tags": json.dumps(node.tags or []),
                "layerAssignment": node.layer_assignment or "",
                "childCount": node.child_count,
                "sourceSystem": node.source_system or "",
                "lastSyncedAt": node.last_synced_at or "",
            }
            # Only include level when the engine has injected the mapping;
            # otherwise omit the key so SET n += $p doesn't overwrite an
            # existing level with null.
            level = self._get_node_level(node.entity_type)
            if level is not None:
                params["level"] = level
            await self._query(
                f"MERGE (n:{label} {{urn: $urn}}) SET n += $p",
                params={"urn": node.urn, "p": params},
            )
            await self._cache_urn_label(node.urn, label)
            if containment_edge:
                rel_type = _sanitize_label(str(containment_edge.edge_type))
                await self._query(
                    f"""
                    MATCH (a {{urn: $src}}) MATCH (b {{urn: $tgt}})
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r.id = $eid, r.confidence = $conf
                    """,
                    params={
                        "src": containment_edge.source_urn,
                        "tgt": containment_edge.target_urn,
                        "eid": containment_edge.id,
                        "conf": containment_edge.confidence,
                    },
                )
            return True
        except Exception as e:
            logger.error(f"create_node failed: {e}")
            return False

    async def create_edge(self, edge: GraphEdge) -> bool:
        """Create a single edge in FalkorDB."""
        await self._ensure_connected()
        try:
            rel_type = _sanitize_label(str(edge.edge_type))
            await self._query(
                f"MATCH (a {{urn: $src}}) MATCH (b {{urn: $tgt}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"SET r.id = $eid, r.confidence = $conf, r.properties = $props",
                params={
                    "src": edge.source_urn,
                    "tgt": edge.target_urn,
                    "eid": edge.id,
                    "conf": edge.confidence or 1.0,
                    "props": json.dumps(edge.properties or {}),
                },
            )
            return True
        except Exception as e:
            logger.error(f"create_edge failed: {e}")
            return False

    async def update_edge(self, edge_id: str, properties: Dict[str, Any]) -> Optional[GraphEdge]:
        """Update edge properties by edge ID."""
        await self._ensure_connected()
        try:
            result = await self._query(
                "MATCH (a)-[r]->(b) WHERE r.id = $eid "
                "SET r.properties = $props "
                "RETURN a.urn, b.urn, type(r), properties(r)",
                params={"eid": edge_id, "props": json.dumps(properties)},
            )
            if not result.result_set:
                return None
            row = result.result_set[0]
            return _edge_from_row(row[0], row[1], row[2], row[3] or {})
        except Exception as e:
            logger.error(f"update_edge failed: {e}")
            return None

    async def delete_edge(self, edge_id: str) -> bool:
        """Delete an edge by its ID property."""
        await self._ensure_connected()
        try:
            result = await self._query(
                "MATCH ()-[r]->() WHERE r.id = $eid DELETE r RETURN count(r)",
                params={"eid": edge_id},
            )
            if result.result_set and result.result_set[0][0] > 0:
                return True
            return False
        except Exception as e:
            logger.error(f"delete_edge failed: {e}")
            return False

    # ------------------------------------------------------------------ #
    # ProviderRegistry lifecycle helpers                                   #
    # ------------------------------------------------------------------ #

    async def list_graphs(self) -> list:
        """Return all graph keys on this FalkorDB instance via GRAPH.LIST."""
        await self._ensure_connected()
        try:
            # GRAPH.LIST is a one-off Redis-protocol command on the FalkorDB
            # client (not Cypher, not the TimeoutRedis proxy) so it has no
            # natural wrapper.  Bound it inline at the read-query timeout to
            # honour the per-operation deadline contract.
            result = await asyncio.wait_for(
                self._db.execute_command("GRAPH.LIST"),
                timeout=self._READ_TIMEOUT,
            )
            return list(result) if result else []
        except Exception as exc:
            logger.warning("GRAPH.LIST failed: %s", exc)
            return []

    async def close(self) -> None:
        """Release both connection pools held by this provider."""
        # Pool teardown still hits the network (graceful socket close) so it
        # qualifies under the per-operation deadline contract.  Use the
        # short init/teardown timeout — a stuck shutdown should fail fast,
        # not block the event loop forever.
        _close_timeout = float(os.getenv("FALKORDB_INIT_TIMEOUT", "3"))

        # P1.7 — cancel any in-flight reconcile task FIRST so it doesn't
        # keep using the pool we're about to close. Without this:
        #   - shutdown can stall (reconcile holds a Redis connection that
        #     keeps the pool's aclose() waiting)
        #   - on eviction-then-rebuild, two reconcile tasks can race on
        #     the same FalkorDB graph (idempotent CREATE INDEX is fine,
        #     but the warnings spam logs)
        reconcile_task = getattr(self, "_reconcile_task", None)
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
            try:
                await asyncio.wait_for(reconcile_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:
                logger.warning(
                    "FalkorDB reconcile task raised on close: %s", exc,
                )
        # Reset so a re-instantiated provider can schedule a fresh
        # reconcile without colliding with the cancelled one.
        self._reconcile_task = None
        self._reconcile_started = False

        try:
            if hasattr(self, "_redis") and self._redis is not None:
                await asyncio.wait_for(self._redis.aclose(), timeout=_close_timeout)
            if self._redis_pool is not None:
                await asyncio.wait_for(self._redis_pool.aclose(), timeout=_close_timeout)
            if self._pool is not None:
                await asyncio.wait_for(self._pool.aclose(), timeout=_close_timeout)
        except Exception as exc:
            logger.warning("Error closing FalkorDB pools: %s", exc)
        finally:
            self._graph = None
            self._proj_graph = None
            self._pool = None
            self._redis_pool = None
            self._redis = None
            self._db = None
