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
        # Application-layer concurrency cap for Cypher queries. Pool size
        # is FALKORDB_GRAPH_POOL_SIZE (default 24); we cap query-issuing
        # tasks below that so a burst of slow traces cannot exhaust the
        # pool and surface as opaque socket timeouts. The remaining pool
        # headroom is reserved for non-trace work (writes, schema
        # introspection, health checks).
        self._query_semaphore = asyncio.Semaphore(
            int(os.getenv("FALKORDB_QUERY_CONCURRENCY", "20"))
        )
        # AIMD state for aggregation MERGE sub-batch sizing. Starts at the
        # ceiling and shrinks on observed latency creep; per-instance so
        # different graphs on the same provider keep independent state
        # (each ProviderManager cache key is (provider_id, graph_name)).
        self._aggregation_sub_batch_size: int = self._MERGE_SUB_BATCH_SIZE
        self._aggregation_sub_batch_under_target_run: int = 0

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

    # FalkorDB engine cancels the query 500ms before the asyncio deadline so
    # the DB-side cancel races first (frees the worker thread + the pool
    # connection); asyncio.wait_for is the safety net for socket-level hangs.
    @staticmethod
    def _db_timeout_ms(seconds: float) -> int:
        return max(500, int(seconds * 1000) - 500)

    async def _ro_query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded read-only query on the source graph."""
        t = timeout if timeout is not None else self._READ_TIMEOUT
        async with self._query_semaphore:
            return await asyncio.wait_for(
                self._graph.ro_query(cypher, params=params or {}, timeout=self._db_timeout_ms(t)),
                timeout=t,
            )

    async def _query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded write query on the source graph."""
        t = timeout if timeout is not None else self._WRITE_TIMEOUT
        async with self._query_semaphore:
            return await asyncio.wait_for(
                self._graph.query(cypher, params=params or {}, timeout=self._db_timeout_ms(t)),
                timeout=t,
            )

    async def _proj_ro_query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded read-only query on the projection graph."""
        t = timeout if timeout is not None else self._READ_TIMEOUT
        async with self._query_semaphore:
            return await asyncio.wait_for(
                self._proj.ro_query(cypher, params=params or {}, timeout=self._db_timeout_ms(t)),
                timeout=t,
            )

    async def _proj_query(self, cypher: str, params: dict = None, *, timeout: float = None):
        """Timeout-guarded write query on the projection graph."""
        t = timeout if timeout is not None else self._WRITE_TIMEOUT
        async with self._query_semaphore:
            return await asyncio.wait_for(
                self._proj.query(cypher, params=params or {}, timeout=self._db_timeout_ms(t)),
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

        # Edge-property indices on :AGGREGATED powering the level-pair
        # fast path used by ``_expand_aggregated_set``. With these in
        # place, ``WHERE r.sourceLevel = $L AND r.targetLevel = $L``
        # becomes a composite index seek instead of a per-edge property
        # read after the rel-typed MATCH. Idempotent CREATE INDEX, best-
        # effort: older FalkorDB releases may not support edge-property
        # indices, in which case the trace continues to work via the
        # legacy neighbour-label scan fallback.
        # Composite index attempt first — when supported by the FalkorDB
        # version this is a single index seek on (sourceLevel, targetLevel)
        # rather than two single-column lookups OR-merged by the planner.
        # Idempotent; falls back to two single-column indices below if the
        # planner does not support composite edge indices.
        aggregated_edge_indices = [
            "CREATE INDEX FOR ()-[r:AGGREGATED]-() ON (r.sourceLevel, r.targetLevel)",
            "CREATE INDEX FOR ()-[r:AGGREGATED]-() ON (r.sourceLevel)",
            "CREATE INDEX FOR ()-[r:AGGREGATED]-() ON (r.targetLevel)",
        ]
        for index_cypher in aggregated_edge_indices:
            try:
                await asyncio.wait_for(
                    self._graph.query(index_cypher), timeout=_init_timeout,
                )
            except Exception:
                pass  # Older FalkorDB or already exists — ignore

        # AGGREGATED edge level-stamping state.
        #
        # The probe runs lazily — it needs the level map (and its digest)
        # before it can ask "are stamps fresh?". `set_entity_type_levels`
        # triggers the probe whenever the digest changes. Until then,
        # ``_levels_backfilled`` stays None and the trace fast path uses
        # the label-scan fallback (correct, slower).
        #
        # ``_level_digest`` is the SHA-256 of the entity_type→level map
        # currently injected onto this provider. AGGREGATED edges carry
        # ``r.levelDigest`` set to whatever digest was current when they
        # were stamped; a mismatch means the ontology drifted and stamps
        # need a re-run of backfill_aggregated_levels.py.
        #
        # ``_levels_warning_for_digest`` throttles the "edges not stamped"
        # warning to at most one log line per (provider lifetime, digest)
        # pair, so per-request probes don't spam.
        self._levels_backfilled: Optional[bool] = None
        self._level_digest: Optional[str] = None
        self._levels_warning_for_digest: Optional[str] = None

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

        Cache invalidation is implicit: the ancestors cache key
        (``_ancestors_cache_key``) hashes the resolved type set, so a
        change to ``types`` automatically routes reads/writes to a
        different Redis namespace. No manual flush is needed; old
        namespaces are simply unreachable and lazy-evicted by Redis.
        """
        if from_ontology or types:
            self._resolved_containment_types: Set[str] = {t.upper() for t in types}
            self._resolved_containment_types_set = True
        # else: introspection-only with no containment found — don't set sentinel

    def set_entity_type_levels(self, mapping: Dict[str, int]) -> None:
        """Called by ContextEngine after ontology resolution to inject the
        entity-type → hierarchy.level mapping. Used both at write time
        (populates ``n.level`` on upsert for the level index) and at read
        time (resolves levels via ``labels(n)[0]`` so trace queries work
        even when ``n.level`` hasn't been backfilled on existing nodes).

        Also computes a ``levelDigest`` over the map. AGGREGATED edges
        stamp this digest at materialization time; the cold-start probe
        compares stamped digests to the current one to decide whether
        backfill is needed. When the digest changes (ontology edited),
        we re-trigger the probe so the staleness state refreshes without
        a process restart.
        """
        from backend.app.services.ontology_levels import compute_level_digest

        self._entity_type_levels: Dict[str, int] = dict(mapping)
        new_digest = compute_level_digest(self._entity_type_levels)

        if new_digest != self._level_digest:
            self._level_digest = new_digest
            # New digest → re-probe in the background. Don't block here;
            # the probe runs against the graph and we don't want
            # ontology resolution to wait for it.
            try:
                asyncio.create_task(self._check_levels_backfilled())
            except RuntimeError:
                # No running loop (rare — usually only in synchronous
                # test paths). The probe will run on first trace.
                pass

    def _get_node_level(self, entity_type: Any) -> Optional[int]:
        """Resolve a node's hierarchy level from the cached mapping. Returns
        None when ontology hasn't been resolved or the entity type is unknown
        — backfill or read-time fallback handles those cases.
        """
        mapping = getattr(self, "_entity_type_levels", None)
        if not mapping:
            return None
        return mapping.get(str(entity_type))

    # Per-frontier-node AGGREGATED out-degree cap. When a single node has
    # more aggregated peers than this, the BFS keeps the top-N by weight
    # and emits a MegaNodeInfo so the frontend can render a "+N more"
    # chip. Override via env. Default 5000 — high enough that legitimate
    # hub Domains (lots of underlying lineage) aren't truncated.
    TRACE_DEGREE_CAP: int = int(os.getenv("TRACE_DEGREE_CAP", "5000"))

    async def _check_levels_backfilled(self) -> None:
        """Probe: are :AGGREGATED edges stamped with the CURRENT level digest?

        Sets ``self._levels_backfilled`` to ``True | False``:
          - True  → all edges carry ``r.levelDigest == self._level_digest``
                    → the level-pair fast path can be trusted.
          - False → some edges are missing the digest or carry a stale one
                    (ontology drifted) → the trace path falls back to the
                    label-scan codepath for those edges (correct, slower).

        Logs at most once per (provider lifetime, digest) pair via
        ``_levels_warning_for_digest`` — re-runs with the same digest stay
        quiet. A new digest (ontology edit) re-arms the warning.

        Traces are never refused — the legacy label-scan codepath returns
        correct results during backfill windows. Refusing would break every
        trace whenever the ontology changes.

        Best-effort: if the level map hasn't been injected yet, or the
        probe itself fails (FalkorDB not ready), we leave the flag as None
        and a later call will re-probe.
        """
        digest = self._level_digest
        if not digest:
            # No level map yet — backfilled status is undefined.
            return

        try:
            result = await asyncio.wait_for(
                self._graph.query(
                    "MATCH ()-[r:AGGREGATED]->() "
                    "WHERE r.levelDigest IS NULL OR r.levelDigest <> $digest "
                    "RETURN count(r) AS stale LIMIT 1",
                    params={"digest": digest},
                ),
                timeout=3.0,
            )
            rows = getattr(result, "result_set", None) or []
            stale = int(rows[0][0]) if rows and rows[0] else 0
            self._levels_backfilled = (stale == 0)
            if stale > 0 and self._levels_warning_for_digest != digest:
                logger.warning(
                    "trace: %d AGGREGATED edges have stale or missing "
                    "levelDigest (current=%s) — run "
                    "backfill_aggregated_levels.py to refresh stamps",
                    stale, digest[:12],
                )
                self._levels_warning_for_digest = digest
        except Exception as exc:
            logger.warning("trace: levels_backfilled check failed: %s", exc)
            # Leave None — probed again on demand if needed

    async def _resolve_root_anchor(
        self, urn: str, ctypes: List[str],
    ) -> Tuple[str, int]:
        """Walk containment UP to the absolute Root (a node with no incoming
        containment edge). Returns ``(root_urn, root_level)``.

        Used by skeleton-first trace when ``level=0``: regardless of
        starting nesting depth, we end up at the topmost reachable
        ancestor. When no level-0 ancestor exists (orphan), we return
        the highest level actually reached — caller surfaces this as
        ``meta.fallbackLevel``.

        Cycle-safe: the variable-length walk uses a node-uniqueness
        predicate so a self-referencing typedef ``CONTAINS`` edge can't
        cause runaway expansion.
        """
        if not ctypes:
            # No containment configured — focus is its own root.
            return urn, -1

        max_depth = max(len(getattr(self, "_entity_type_levels", {}) or {}), 10)
        # Find topmost containment ancestor — the deepest reachable walk
        # via incoming containment edges. We use `*1..N` (not `*0..N`)
        # because FalkorDB's planner trips on `ALL(rel IN c WHERE …)` when
        # the variable-length match produces zero-length paths (c can be
        # Edge instead of List). Handle the "focus is already top" case
        # with COALESCE on the outer query (anc is null → return focus).
        cypher = (
            "MATCH (focus {urn: $urn}) "
            f"OPTIONAL MATCH (focus)<-[c*1..{max_depth}]-(anc) "
            "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            "WITH focus, anc, size(c) AS depth "
            "ORDER BY depth DESC LIMIT 1 "
            "RETURN COALESCE(anc.urn, focus.urn) AS urn, "
            "       COALESCE(anc.level, focus.level, -1) AS level"
        )
        try:
            result = await self._ro_query(
                cypher, params={"urn": urn, "ctypes": ctypes}, timeout=1.5,
            )
            rows = result.result_set or []
            if rows and rows[0]:
                root_urn = rows[0][0] or urn
                lvl = rows[0][1]
                level = int(lvl) if lvl is not None else -1
                return root_urn, level
        except Exception as exc:
            logger.warning("trace: root anchor resolution failed for %s: %s", urn, exc)
        return urn, -1

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
        # Direction-reversed from the original `NOT ()-[:T]->(n)` so n
        # (already bound by the outer MATCH) is the anchor of the pattern.
        # Same semantics — "no incoming :T edge to n" — but the planner
        # walks n's incoming adjacency list directly instead of scanning
        # all :T relationships. Avoids the O(N) full-graph scan that was
        # a top contributor to the FalkorDB CPU pin under load.
        #
        # IMPORTANT: keep the openCypher-1.0 pattern-negation form. Do NOT
        # rewrite to `NOT EXISTS { MATCH ... }` — that is Neo4j 4.x+ / ISO
        # GQL syntax and is NOT supported by FalkorDB. The subquery form
        # silently throws, gets caught below, and returns empty — which
        # was the original bug.
        if containment_rel_types:
            filter_fragments.append(
                "NOT (n)<-[:" + containment_rel_types + "]-()"
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
        """Create indices on the projection target for fast AGGREGATED reads
        and (critically) for the unlabeled MERGE that runs on the write path.

        The aggregation worker issues ``MERGE (s {urn: item.s})`` without a
        label. Per-label URN indexes (created in ``_initialize_indices``)
        don't help here — FalkorDB's planner can't fan out across labeled
        indexes for an unlabeled MATCH. Without a property-only URN index,
        every MERGE in the aggregation hot path becomes a full node scan,
        which is the root cause of the 200% CPU spikes observed on million-
        node graphs (one outer batch fans out to ~100 sub-batches × 500
        MERGEs, each scanning all nodes).

        FalkorDB versions vary on whether ``CREATE INDEX FOR (n) ON (n.urn)``
        without a label predicate is supported; we attempt it best-effort
        and fall through silently on older releases (the existing per-label
        URN indexes remain in place for labeled queries).
        """

        try:
            await self._proj_query("CREATE INDEX FOR (n:_Projection) ON (n.urn)")
        except Exception:
            pass  # Index may already exist

        # Unlabeled URN index for the aggregation MERGE hot path. Best-effort;
        # supported on recent FalkorDB versions (>=2.10). On older releases
        # the CREATE fails and the planner continues to scan — flagged for
        # the operator via the slow-query metric exported in WS4.
        try:
            await self._proj_query("CREATE INDEX FOR (n) ON (n.urn)")
        except Exception as exc:
            logger.info(
                "ensure_projections: unlabeled URN index not supported on this "
                "FalkorDB version (%s); falling back to per-label indexes. "
                "Aggregation MERGEs will scan unless every label has a URN "
                "index in place.", exc,
            )

    def _ancestors_cache_key(self) -> str:
        """Return the Redis Hash key for ancestor chains in this graph,
        scoped by the resolved containment-types fingerprint.

        Different containment configurations resolve to different
        ancestor chains for the same URN, so they must live in
        different cache namespaces. Without this scoping, a prior job
        that ran with empty ``containment_edge_types`` would cache
        ``"[]"`` for every URN and every subsequent job (with proper
        types) would silently see cache hits and produce only
        leaf-to-leaf AGGREGATED edges instead of propagating up the
        containment tree.

        The fingerprint is a short SHA1 over the sorted, upper-cased
        type names. Empty / unset → a stable empty-set fingerprint
        that flat-graph aggregations reuse safely. Identical
        configurations (across jobs, across caller paths) reuse the
        same key — full intra- and cross-job caching preserved.
        """
        import hashlib

        types = getattr(self, "_resolved_containment_types", None) or set()
        if not isinstance(types, (set, frozenset, list, tuple)):
            types = set()
        normalised = ",".join(sorted(t.upper() for t in types))
        digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]
        return f"{self._graph_name}:ancestors:{digest}"

    async def _get_ancestor_chain(self, urn: str) -> List[str]:
        """Get pre-computed ancestor chain from Redis Hash, or compute + cache it.

        Returns list of URNs from immediate parent to root (ordered).
        The cache key includes a containment-types fingerprint so a
        change to the resolved containment configuration cannot return
        a stale chain from a prior config (see ``_ancestors_cache_key``).
        """
        cache_key = self._ancestors_cache_key()
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
        """Single Cypher query to walk containment edges upward (1 query instead of N).

        Variable-length depth bound is the number of entity-type levels
        in the resolved ontology (clamped to a 10 floor for safety on
        cold caches). This is tighter and more correct than the legacy
        hardcoded ``*1..10`` for shallow ontologies, and extends to
        deeper ones without code edits.
        """
        containment = list(self._get_containment_edge_types())
        if not containment:
            # No containment types — flat graph, no ancestors
            return []
        containment_cypher = "|".join(_sanitize_label(t) for t in containment)
        max_depth = max(len(getattr(self, "_entity_type_levels", {}) or {}), 10)

        # Variable-length path: returns ordered list of ancestor URNs
        # nodes(path) gives [child, parent, grandparent, ...] — skip index 0 (self)
        result = await self._ro_query(
            f"MATCH path = (child)<-[:{containment_cypher}*1..{max_depth}]-(ancestor) "
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

        Uses Redis pipeline for batch HGET/HSET and a single bulk Cypher
        (``UNWIND $urns AS u``) to compute every missing chain in one
        round-trip per chunk, eliminating the per-URN compile + send +
        receive overhead that previously dominated this path on large
        outer batches. Cache namespace is scoped by containment-types
        fingerprint (see ``_ancestors_cache_key``) so a config change
        cannot leak stale chains from a prior configuration.

        On bulk-Cypher failure, falls back to the per-URN path with
        bounded concurrency so a single planner hiccup doesn't fail the
        whole outer batch.
        """
        cache_key = self._ancestors_cache_key()
        result: Dict[str, List[str]] = {}

        if not urns:
            return result

        # First, try to fetch all from cache in one pipeline
        try:
            pipe = self._redis.pipeline(transaction=False)
            for u in urns:
                pipe.execute_command("HGET", cache_key, u)
            cached = await pipe.execute()

            missing_urns = []
            for i, u in enumerate(urns):
                if cached[i]:
                    try:
                        result[u] = json.loads(cached[i])
                    except Exception:
                        missing_urns.append(u)
                else:
                    missing_urns.append(u)
        except Exception:
            missing_urns = list(urns)

        if missing_urns:
            try:
                computed = await self._compute_ancestor_chains_bulk_cypher(missing_urns)
            except Exception as exc:
                logger.warning(
                    "Bulk ancestor Cypher failed for %d urns (%s); "
                    "falling back to per-URN computation.",
                    len(missing_urns), exc,
                )
                _MAX_ANCESTOR_CONCURRENCY = 4
                sem = asyncio.Semaphore(_MAX_ANCESTOR_CONCURRENCY)

                async def _compute_with_sem(urn: str) -> tuple[str, list]:
                    async with sem:
                        try:
                            return urn, await self._compute_ancestor_chain(urn)
                        except Exception as e:
                            logger.warning(
                                "Failed to compute ancestor chain for %s: %s", urn, e,
                            )
                            return urn, []

                pairs = await asyncio.gather(
                    *(_compute_with_sem(u) for u in missing_urns),
                )
                computed = {u: chain for u, chain in pairs}

            for u in missing_urns:
                result[u] = computed.get(u, [])

            # Batch-store all computed chains in one pipeline
            store_pipe = self._redis.pipeline(transaction=False)
            for u in missing_urns:
                store_pipe.execute_command(
                    "HSET", cache_key, u, json.dumps(result.get(u, [])),
                )
            try:
                await store_pipe.execute()
            except Exception as e:
                logger.debug(f"Failed to batch-store ancestor chains: {e}")

        return result

    async def _compute_ancestor_chains_bulk_cypher(
        self,
        urns: List[str],
    ) -> Dict[str, List[str]]:
        """Compute ancestor chains for many URNs in a single Cypher.

        Preserves the longest-path semantics of
        ``_compute_ancestor_chain``: each URN's chain is the ordered
        ``[parent, grandparent, ...]`` along the longest containment
        path, matching what callers that depend on parent-before-
        grandparent ordering already expect.

        Internally chunked to bound the per-query parameter size; the
        planner sees one set of bound variables per chunk and only one
        round-trip is paid per chunk regardless of how many URNs miss
        the cache. This is the fix for the per-URN scan amplification
        documented in the aggregation hardening plan.
        """
        out: Dict[str, List[str]] = {u: [] for u in urns}
        if not urns:
            return out

        containment = list(self._get_containment_edge_types())
        if not containment:
            # Flat graph — no ancestors for any URN.
            return out

        containment_cypher = "|".join(_sanitize_label(t) for t in containment)
        max_depth = max(len(getattr(self, "_entity_type_levels", {}) or {}), 10)

        # Keep parameter lists bounded so a single misconfigured outer
        # batch (e.g. 10k URNs) doesn't generate a single oversized
        # query plan that itself spikes provider CPU.
        chunk_size = 500

        cypher = (
            "UNWIND $urns AS u "
            "MATCH (child {urn: u}) "
            f"OPTIONAL MATCH path = (child)<-[:{containment_cypher}*1..{max_depth}]-(a) "
            "WITH u, "
            "     [n IN nodes(path)[1..] | n.urn] AS chain_candidate, "
            "     coalesce(length(path), 0) AS plen "
            "ORDER BY u, plen DESC "
            "WITH u, collect(chain_candidate) AS candidates "
            "RETURN u, coalesce(candidates[0], []) AS chain"
        )

        for i in range(0, len(urns), chunk_size):
            chunk = urns[i : i + chunk_size]
            result = await self._ro_query(cypher, params={"urns": chunk})
            for row in result.result_set or []:
                urn = row[0]
                chain = row[1] or []
                # Preserve list-of-str shape; FalkorDB may return None
                # entries inside the list if a node lacked .urn — drop
                # them so callers don't have to defend against None.
                out[urn] = [c for c in chain if c]

        return out

    # ------------------------------------------------------------------ #
    # Batch-level materialization (used by materialize_aggregated_edges_batch)
    # ------------------------------------------------------------------ #

    # Max ancestor pairs per Cypher UNWIND+MERGE call.  Each input edge
    # fans out to ~4 ancestor pairs (s_chain × t_chain), so 5000 input
    # edges produce ~20K pairs.  A single MERGE with 20K items + REDUCE
    # exceeds FalkorDB's 3s socket_timeout.  500 pairs keeps each call
    # well under 1s while still being 500× fewer round-trips than the
    # old per-edge approach. This is the *ceiling*; the per-graph
    # adaptive sizer (``_aggregation_sub_batch_size``) shrinks toward
    # ``_MERGE_SUB_BATCH_MIN`` when MERGE latency creeps past
    # ``_MERGE_SUB_BATCH_TARGET_HIGH_S`` (AIMD), and grows back toward
    # the ceiling after a run of healthy sub-batches.
    _MERGE_SUB_BATCH_SIZE = 500
    _MERGE_SUB_BATCH_MIN = 50
    _MERGE_SUB_BATCH_TARGET_HIGH_S = 2.0
    _MERGE_SUB_BATCH_TARGET_LOW_S = 0.8
    _MERGE_SUB_BATCH_GROW_AFTER = 5
    _MERGE_SUB_BATCH_GROW_STEP = 100

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

        # Resolve s/t levels for the level-pair fast path. Pre-fetches
        # labels for every URN in the batch from the Redis URN→label cache
        # (populated as a side effect of node upserts / get_node calls),
        # then maps label → hierarchy.level via the in-process entity-type
        # level map injected by the ContextEngine. URNs without a resolved
        # level are left as None and the Cypher uses ``coalesce`` so a
        # missing level never clobbers an existing one written by the
        # backfill script.
        entity_levels: Dict[str, int] = getattr(self, "_entity_type_levels", None) or {}
        url_levels: Dict[str, Optional[int]] = {}
        if entity_levels:
            all_urns = sorted({s for (s, _), _ in ordered_pairs}
                              | {t for (_, t), _ in ordered_pairs})
            if all_urns:
                try:
                    label_key = self._urn_label_key()
                    label_pipe = self._redis.pipeline(transaction=False)
                    for u in all_urns:
                        label_pipe.hget(label_key, u)
                    label_rows = await label_pipe.execute()
                    for u, raw in zip(all_urns, label_rows):
                        if not raw:
                            continue
                        lbl = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        lvl = entity_levels.get(lbl)
                        if lvl is not None:
                            url_levels[u] = lvl
                except Exception as exc:
                    # Best-effort: if Redis is down we skip the level
                    # annotation. Backfill_aggregated_levels.py covers
                    # any edges materialised without props.
                    logger.warning(
                        "materialize: level lookup pipeline failed (%d urns): %s",
                        len(all_urns), exc,
                    )

        merge_batch: list[dict[str, Any]] = []
        for i, ((s, t), _) in enumerate(ordered_pairs):
            weight = scard_results[i] if scard_results[i] else 1
            etypes = list(pair_edge_types.get((s, t), set()))
            merge_batch.append({
                "s": s, "t": t, "w": int(weight), "et": etypes,
                "sl": url_levels.get(s),
                "tl": url_levels.get(t),
            })

        # Execute ONE Cypher UNWIND+MERGE per sub-batch.  The Cypher
        # REDUCE accumulates all edge types into sourceEdgeTypes in a
        # single pass — no per-edge-type iteration needed.

        created = 0
        chunk_start = 0
        while chunk_start < len(merge_batch):
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

            # Adaptive sub-batch size: starts at the ceiling and shrinks
            # toward _MERGE_SUB_BATCH_MIN when MERGE latency creeps past
            # _MERGE_SUB_BATCH_TARGET_HIGH_S. This is the WS1.4 backpressure
            # mechanism — when FalkorDB CPU spikes, sub-batches slow down,
            # and shrinking the size both reduces per-call MERGE work and
            # makes the cooperative-cancel check fire more often.
            sub_batch_size = self._aggregation_sub_batch_size
            chunk = merge_batch[chunk_start:chunk_start + sub_batch_size]
            chunk_start += len(chunk)

            t_merge_start = time.monotonic()
            await self._proj_query(
                "UNWIND $batch AS item "
                "MERGE (s {urn: item.s}) "
                "MERGE (t {urn: item.t}) "
                "MERGE (s)-[r:AGGREGATED]->(t) "
                "SET r.weight = item.w, "
                "    r.latestUpdate = timestamp(), "
                # Level-pair fast-path props. ``coalesce`` keeps a previously
                # backfilled value when the new resolution couldn't find a
                # label (cold cache / unknown entity type) — never regresses
                # known level metadata to NULL.
                "    r.sourceLevel = coalesce(item.sl, r.sourceLevel), "
                "    r.targetLevel = coalesce(item.tl, r.targetLevel), "
                "    r.sourceEdgeTypes = REDUCE(acc = "
                "      CASE WHEN r.sourceEdgeTypes IS NULL THEN [] "
                "           ELSE r.sourceEdgeTypes END, "
                "      et IN item.et | "
                "      CASE WHEN et IN acc THEN acc "
                "           ELSE acc + et END)",
                params={"batch": chunk},
            )
            t_merge_elapsed = time.monotonic() - t_merge_start
            created += len(chunk)

            # AIMD adjustment. Shrink fast (multiplicative decrease) when
            # latency creeps; grow slow (additive increase) only after a
            # sustained run of healthy sub-batches. Bounds: [_MIN, _MAX].
            current = self._aggregation_sub_batch_size
            if t_merge_elapsed > self._MERGE_SUB_BATCH_TARGET_HIGH_S:
                new_size = max(self._MERGE_SUB_BATCH_MIN, current // 2)
                if new_size != current:
                    logger.warning(
                        "Aggregation MERGE sub-batch latency %.2fs > %.1fs "
                        "target on %s; halving sub-batch size %d -> %d to "
                        "relieve provider load.",
                        t_merge_elapsed, self._MERGE_SUB_BATCH_TARGET_HIGH_S,
                        self._graph_name, current, new_size,
                    )
                    self._aggregation_sub_batch_size = new_size
                self._aggregation_sub_batch_under_target_run = 0
            elif t_merge_elapsed < self._MERGE_SUB_BATCH_TARGET_LOW_S:
                self._aggregation_sub_batch_under_target_run += 1
                if (
                    self._aggregation_sub_batch_under_target_run
                    >= self._MERGE_SUB_BATCH_GROW_AFTER
                    and current < self._MERGE_SUB_BATCH_SIZE
                ):
                    new_size = min(
                        self._MERGE_SUB_BATCH_SIZE,
                        current + self._MERGE_SUB_BATCH_GROW_STEP,
                    )
                    logger.info(
                        "Aggregation MERGE sub-batch healthy (%d consecutive "
                        "< %.1fs) on %s; growing sub-batch size %d -> %d.",
                        self._aggregation_sub_batch_under_target_run,
                        self._MERGE_SUB_BATCH_TARGET_LOW_S,
                        self._graph_name, current, new_size,
                    )
                    self._aggregation_sub_batch_size = new_size
                    self._aggregation_sub_batch_under_target_run = 0
            else:
                # In the steady-state band; reset growth counter so growth
                # only triggers after a run of clearly-under-target calls.
                self._aggregation_sub_batch_under_target_run = 0

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

    # ====================================================================== #
    # Bulk Rebuild Path (Phase 1 of aggregation hardening)                    #
    #                                                                        #
    # Adopts the solidatus-generator pattern: pre-dedupe pairs in worker     #
    # memory, ensure per-label URN indexes, drop existing AGGREGATED edges,  #
    # group pairs by (src_label, tgt_label), bulk-CREATE with label-         #
    # qualified MATCH. Replaces MERGE-on-relationship (O(out_degree) in      #
    # FalkorDB — no relationship-existence index) with CREATE (O(1) per      #
    # row), eliminating the 200% CPU pathology on graphs with high-degree    #
    # ancestor nodes.                                                        #
    #                                                                        #
    # Trade-off: wipe-and-rebuild semantics mean trace reads on the same     #
    # projection graph see a partial AGGREGATED set during the rebuild       #
    # window. Phase 3 (blue-green projection slots) eliminates this; Phase   #
    # 1 accepts it. Recovery: bulk rebuild always restarts from cursor=NULL  #
    # rather than resuming from a partial mid-rebuild state — the wipe      #
    # phase cleans up any partial AGGREGATED writes from a prior attempt.    #
    # ====================================================================== #

    _BULK_CREATE_BATCH_SIZE = 2000   # matches solidatus-generator BATCH_SIZE
    _BULK_WIPE_BATCH_SIZE = 50000    # cursored DELETE chunk for AGGREGATED wipe

    async def _wipe_aggregated_edges(
        self,
        *,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Drop all :AGGREGATED edges on the projection graph in cursored chunks.

        Returns the total number of edges deleted. Each chunk is bounded so
        a single statement can't exceed the write timeout on a graph with
        millions of AGGREGATED edges; the loop converges when a chunk
        deletes zero rows.

        Short-circuits with a single cheap existence probe before issuing
        any DELETE — on a fresh graph (first bulk rebuild ever), this
        saves the millisecond-scale empty-DELETE round-trip; more
        importantly, on a graph where AGGREGATED happens to already be
        empty, the probe returns instantly and we don't pay any wipe
        time at all.
        """
        probe = await self._proj_query(
            "MATCH ()-[r:AGGREGATED]->() RETURN r LIMIT 1"
        )
        if not (probe.result_set or []):
            logger.info(
                "Bulk wipe AGGREGATED on %s: graph has no AGGREGATED edges, "
                "skipping wipe phase.", self._graph_name,
            )
            return 0

        total_deleted = 0
        while True:
            if should_cancel is not None and should_cancel():
                from backend.app.services.aggregation.cancel import JobCancelled
                from datetime import datetime, timezone
                raise JobCancelled(
                    job_id="<bulk-wipe-cancel>",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )
            res = await self._proj_query(
                "MATCH ()-[r:AGGREGATED]->() "
                f"WITH r LIMIT {self._BULK_WIPE_BATCH_SIZE} "
                "DELETE r RETURN count(r) AS n"
            )
            n = 0
            if res.result_set:
                first = res.result_set[0]
                n = (first[0] if first else 0) or 0
            total_deleted += int(n)
            if n == 0:
                break
            logger.info(
                "Bulk wipe AGGREGATED on %s: chunk deleted %d (running total %d)",
                self._graph_name, n, total_deleted,
            )
        return total_deleted

    async def _purge_aggregated_idempotency_namespace(self) -> None:
        """Drop all Redis SADD members tracking AGGREGATED edge contributors.

        Required before a bulk rebuild — stale members from a prior attempt
        would inflate weights or carry stale contributor edge_ids forward
        into the rebuilt graph.
        """
        pattern = f"{self._graph_name}:agg_members:*"
        cursor: int = 0
        deleted = 0
        try:
            while True:
                reply = await self._redis.execute_command(
                    "SCAN", cursor, "MATCH", pattern, "COUNT", 1000,
                )
                # python-redis returns (cursor, [keys]); both may be bytes.
                next_cursor, keys = reply[0], reply[1]
                if isinstance(next_cursor, (bytes, bytearray)):
                    next_cursor = int(next_cursor)
                else:
                    next_cursor = int(next_cursor)
                if keys:
                    pipe = self._redis.pipeline(transaction=False)
                    for k in keys:
                        pipe.delete(k)
                    await pipe.execute()
                    deleted += len(keys)
                cursor = next_cursor
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning(
                "Idempotency namespace purge failed on %s (continuing — stale "
                "members may inflate the first incremental edge's weight): %s",
                self._graph_name, exc,
            )
            return
        if deleted:
            logger.info(
                "Purged %d Redis agg_members keys on %s before bulk rebuild.",
                deleted, self._graph_name,
            )

    async def _resolve_urn_labels_bulk(
        self, urns: List[str],
    ) -> Dict[str, Optional[str]]:
        """Resolve URN → sanitized-label for many URNs at once.

        First consults the Redis URN→label cache populated as a side
        effect of node upserts / get_node calls. For misses, falls back
        to a single bulk Cypher querying labels for the missing URNs
        (one round-trip regardless of miss count). Caches results back
        to Redis for subsequent calls.

        Returns dict with every input URN as a key; the value is
        ``None`` when the URN's label could not be resolved (caller
        routes through the unlabeled fallback CREATE path for these).
        """
        out: Dict[str, Optional[str]] = {}
        if not urns:
            return out

        label_key = self._urn_label_key()
        missing: List[str] = []

        try:
            pipe = self._redis.pipeline(transaction=False)
            for u in urns:
                pipe.hget(label_key, u)
            raws = await pipe.execute()
            for u, raw in zip(urns, raws):
                if raw is None:
                    missing.append(u)
                else:
                    lbl = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                    out[u] = _sanitize_label(lbl)
        except Exception:
            missing = list(urns)

        if missing:
            try:
                # Single bulk Cypher round-trip for label lookup. Uses
                # ``WHERE n.urn IN $urns`` form (not ``UNWIND $urns AS u
                # MATCH (n {urn:u})``) so FalkorDB plans ONE node-scan
                # for the whole batch rather than N scans (one per
                # UNWIND iteration). On a 5M-node graph with 1M missing
                # URNs that's the difference between one O(N) scan and
                # 5 trillion node comparisons — and was the bottleneck
                # of the bulk-rebuild label-resolution phase before this
                # change.
                #
                # If an unlabeled URN index exists (FalkorDB >=2.10), the
                # planner can use it directly and the whole call becomes
                # a multi-key index seek. If not, the single full scan
                # is still vastly cheaper than per-row scans.
                res = await self._ro_query(
                    "MATCH (n) WHERE n.urn IN $urns "
                    "RETURN n.urn AS u, labels(n)[0] AS label",
                    params={"urns": missing},
                )
                store_pipe = self._redis.pipeline(transaction=False)
                store_count = 0
                for row in res.result_set or []:
                    urn, label = row[0], row[1]
                    if label:
                        safe = _sanitize_label(label)
                        out[urn] = safe
                        store_pipe.hset(label_key, urn, safe)
                        store_count += 1
                    else:
                        out[urn] = None
                if store_count > 0:
                    try:
                        await store_pipe.execute()
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning(
                    "Bulk URN label resolution failed for %d URNs (will fall "
                    "back to unlabeled MATCH for these): %s",
                    len(missing), exc,
                )

        for u in urns:
            out.setdefault(u, None)
        return out

    async def _ensure_label_urn_indexes(self, labels: Set[str]) -> None:
        """Create per-label URN indexes for every label that will be
        matched during bulk-CREATE. Idempotent — best-effort on failure.

        Mirrors the pattern in
        solidatus-generator/app/falkordb_client.py:67-85 — indexes go in
        BEFORE any writes so every MATCH/CREATE row is an index seek.
        """
        if not labels:
            return
        _init_timeout = float(os.getenv("FALKORDB_INIT_TIMEOUT", "3"))
        for label in labels:
            try:
                await asyncio.wait_for(
                    self._proj.query(
                        f"CREATE INDEX FOR (n:{label}) ON (n.urn)",
                    ),
                    timeout=_init_timeout,
                )
            except Exception:
                pass  # already exists or unsupported

    async def _bulk_create_aggregated_edges_from_pairs(
        self,
        *,
        pair_data: Dict[Tuple[str, str], Dict[str, Any]],
        urn_label_map: Dict[str, Optional[str]],
        level_digest: Optional[str],
        intra_batch_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        baseline_aggregated: int = 0,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Bulk-CREATE all AGGREGATED edges from a deduped pair set.

        Groups pairs by (src_label, tgt_label) so each Cypher query has
        a uniform shape that FalkorDB plans once and executes via index
        seeks on both endpoint URNs. Pairs whose endpoint label could
        not be resolved fall through to an unlabeled MATCH branch
        (slower but rare and bounded).

        Pre-dedup + CREATE is the core of the solidatus pattern: each
        CREATE is O(1) per row vs MERGE-on-relationship's O(out_degree).
        Caller must guarantee no duplicate pairs in ``pair_data``;
        ``Dict`` keying by ``(src_urn, tgt_urn)`` already enforces this.

        Returns total edges created.
        """
        if not pair_data:
            return 0

        from collections import defaultdict
        grouped: Dict[
            Tuple[Optional[str], Optional[str]], List[Dict[str, Any]]
        ] = defaultdict(list)
        for (s, t), meta in pair_data.items():
            sl = urn_label_map.get(s)
            tl = urn_label_map.get(t)
            item = {
                "s": s,
                "t": t,
                "w": int(meta.get("weight", 1)),
                "et": list(meta.get("edge_types") or []),
                "sl": meta.get("source_level"),
                "tl": meta.get("target_level"),
            }
            grouped[(sl, tl)].append(item)

        created = 0
        digest_val = level_digest or ""

        for (sl_label, tl_label), items in grouped.items():
            if sl_label and tl_label:
                cypher = (
                    f"UNWIND $batch AS item "
                    f"MATCH (a:{sl_label} {{urn: item.s}}) "
                    f"MATCH (b:{tl_label} {{urn: item.t}}) "
                    f"CREATE (a)-[r:AGGREGATED {{"
                    f"weight: item.w, "
                    f"sourceLevel: item.sl, "
                    f"targetLevel: item.tl, "
                    f"sourceEdgeTypes: item.et, "
                    f"levelDigest: $digest, "
                    f"latestUpdate: timestamp()"
                    f"}}]->(b)"
                )
            else:
                # Unlabeled fallback: no index help, but bounded to URNs
                # whose label couldn't be resolved. Logged so an operator
                # can investigate if the count is non-trivial.
                cypher = (
                    "UNWIND $batch AS item "
                    "MATCH (a {urn: item.s}) "
                    "MATCH (b {urn: item.t}) "
                    "CREATE (a)-[r:AGGREGATED {"
                    "weight: item.w, "
                    "sourceLevel: item.sl, "
                    "targetLevel: item.tl, "
                    "sourceEdgeTypes: item.et, "
                    "levelDigest: $digest, "
                    "latestUpdate: timestamp()"
                    "}]->(b)"
                )
                if len(items) > 1000:
                    logger.warning(
                        "Bulk CREATE: %d pairs with unresolved endpoint labels "
                        "(src=%s, tgt=%s) — these use the unlabeled MATCH "
                        "fallback. Investigate the URN→label cache.",
                        len(items), sl_label, tl_label,
                    )

            for i in range(0, len(items), self._BULK_CREATE_BATCH_SIZE):
                if should_cancel is not None and should_cancel():
                    from backend.app.services.aggregation.cancel import JobCancelled
                    from datetime import datetime, timezone
                    raise JobCancelled(
                        job_id="<bulk-create-cancel>",
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )

                chunk = items[i : i + self._BULK_CREATE_BATCH_SIZE]
                t_batch_start = time.monotonic()
                await self._proj_query(
                    cypher,
                    params={"batch": chunk, "digest": digest_val},
                )
                t_batch = (time.monotonic() - t_batch_start) * 1000
                created += len(chunk)

                logger.debug(
                    "Bulk CREATE chunk on %s: group=(%s,%s) size=%d "
                    "elapsed=%.1fms total_created=%d",
                    self._graph_name, sl_label, tl_label,
                    len(chunk), t_batch, created,
                )

                if intra_batch_callback is not None:
                    try:
                        await intra_batch_callback(baseline_aggregated + created)
                    except Exception as cb_exc:
                        logger.error(
                            "Intra-batch progress callback failed during bulk "
                            "CREATE (continuing): %s", cb_exc, exc_info=True,
                        )

        return created

    async def _rebuild_idempotency_state_from_pairs(
        self,
        pair_data: Dict[Tuple[str, str], Dict[str, Any]],
        *,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Repopulate the Redis SADD-based contributor tracking from the
        in-memory pair set. Required so subsequent incremental writes
        (``on_lineage_edge_written``) see correct existing-pair state and
        don't double-count freshly-rebuilt edges.

        Issued in batched pipelines (500 keys per pipeline) so a single
        round-trip never carries an oversized payload on million-pair
        rebuilds.
        """
        members_key_prefix = f"{self._graph_name}:agg_members"
        pipe_size = 500
        sent = 0

        pipe = self._redis.pipeline(transaction=False)
        pipe_count = 0
        for (s, t), meta in pair_data.items():
            if pipe_count == 0 and should_cancel is not None and should_cancel():
                from backend.app.services.aggregation.cancel import JobCancelled
                from datetime import datetime, timezone
                raise JobCancelled(
                    job_id="<bulk-idem-cancel>",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )
            contributors = meta.get("contributors") or []
            if not contributors:
                continue
            member_key = f"{members_key_prefix}:{s}:{t}"
            pipe.execute_command("SADD", member_key, *contributors)
            pipe_count += 1
            if pipe_count >= pipe_size:
                try:
                    await pipe.execute()
                    sent += pipe_count
                except Exception as exc:
                    logger.warning(
                        "Idempotency rebuild pipeline failed (continuing — "
                        "incremental writes for these pairs will be treated "
                        "as net-new): %s", exc,
                    )
                pipe = self._redis.pipeline(transaction=False)
                pipe_count = 0
        if pipe_count > 0:
            try:
                await pipe.execute()
                sent += pipe_count
            except Exception as exc:
                logger.warning(
                    "Idempotency rebuild pipeline (tail) failed: %s", exc,
                )

        logger.info(
            "Idempotency rebuild on %s complete: %d pair member sets "
            "written to Redis.",
            self._graph_name, sent,
        )

    async def _materialize_aggregated_edges_bulk_rebuild(
        self,
        *,
        batch_size: int,
        containment_edge_types: Optional[List[str]],
        lineage_edge_types: Optional[List[str]],
        progress_callback: Optional[Any],
        intra_batch_callback: Optional[Callable[[int], Awaitable[None]]],
        should_cancel: Optional[Callable[[], bool]],
        last_cursor: Optional[str],
    ) -> Dict[str, Any]:
        """Bulk-rebuild orchestrator — Phase 1 of aggregation hardening.

        Replaces the MERGE-based ``_materialize_edges_batched`` path with
        wipe → accumulate-pairs-in-memory → label-resolve → ensure-
        indexes → grouped-bulk-CREATE → idempotency-rebuild. Eliminates
        the MERGE-on-relationship O(out_degree) cost that pegs FalkorDB
        at 200% CPU on high-degree ancestor nodes (Domains, Platforms,
        top-level Containers).

        Recovery semantics: bulk rebuild always starts from cursor=NULL.
        ``last_cursor`` is logged but otherwise ignored — on crash mid-
        rebuild, the next run wipes and restarts. The wipe-first
        ordering means partial AGGREGATED writes from a failed prior
        attempt are cleaned up automatically.

        Memory cost: ~200 bytes per (src_anc, tgt_anc) pair held until
        the bulk-CREATE phase. Estimated ~200MB for 1M pairs, which is
        well within worker pod memory budgets. If a future graph
        produces enough pairs to exhaust memory, Phase 1.5 stages
        ``pair_data`` to Redis or Postgres.
        """
        containment = containment_edge_types or list(self._get_containment_edge_types())
        exclude_types = list(containment) + ["AGGREGATED"]

        # Filter AGGREGATED out of any explicit lineage whitelist —
        # feeding existing AGGREGATED edges back through aggregation
        # produces second-order edges that compound on every re-run.
        if lineage_edge_types:
            effective_lineage_types = [t for t in lineage_edge_types if t != "AGGREGATED"]
            if not effective_lineage_types:
                logger.warning(
                    "bulk_rebuild: lineage_edge_types contained only AGGREGATED "
                    "after filtering; no leaf lineage edges to process.",
                )
                return {
                    "processed": 0,
                    "aggregated_edges_affected": 0,
                    "input_edges_processed": 0,
                    "errors": 0,
                }
            type_filter = "WHERE type(r) IN $lineageEdges"
            type_params: Dict[str, Any] = {"lineageEdges": effective_lineage_types}
        else:
            type_filter = "WHERE NOT type(r) IN $excludeTypes"
            type_params = {"excludeTypes": exclude_types}

        if last_cursor:
            logger.info(
                "bulk_rebuild on %s: ignoring last_cursor=%s — bulk rebuild "
                "always processes from start (wipe-first semantics).",
                self._graph_name, last_cursor,
            )

        # Total count — informational, used by progress callback.
        count_cypher = f"MATCH ()-[r]->() {type_filter} RETURN count(r)"
        count_res = await self._ro_query(count_cypher, params=type_params)
        total = count_res.result_set[0][0] if count_res.result_set else 0
        logger.info(
            "bulk_rebuild on %s starting: %d lineage edges to scan.",
            self._graph_name, total,
        )

        # ===== PHASE A: Wipe + purge idempotency =====
        t_phase_a_start = time.monotonic()
        deleted = await self._wipe_aggregated_edges(should_cancel=should_cancel)
        await self._purge_aggregated_idempotency_namespace()
        t_phase_a = (time.monotonic() - t_phase_a_start) * 1000
        logger.info(
            "bulk_rebuild phase A (wipe): %d AGGREGATED edges deleted in %.1fms",
            deleted, t_phase_a,
        )

        # ===== PHASE B: Stream lineage, accumulate pair_data =====
        pair_data: Dict[Tuple[str, str], Dict[str, Any]] = {}
        entity_levels: Dict[str, int] = getattr(self, "_entity_type_levels", None) or {}
        level_digest = getattr(self, "_level_digest", None)

        processed = 0
        current_cursor: Optional[str] = None
        batch_num = 0
        t_phase_b_start = time.monotonic()

        while True:
            batch_num += 1
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

            t0 = time.monotonic()
            res = await self._ro_query(batch_cypher, params=batch_params)
            rows = res.result_set or []
            t_fetch = (time.monotonic() - t0) * 1000

            if not rows:
                break

            if should_cancel is not None and should_cancel():
                from backend.app.services.aggregation.cancel import JobCancelled
                from datetime import datetime, timezone
                raise JobCancelled(
                    job_id="<bulk-rebuild-scan-cancel>",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )

            # Bulk ancestor fetch — uses WS1.1 single-Cypher bulk path.
            t0 = time.monotonic()
            all_urns: Set[str] = set()
            for row in rows:
                all_urns.add(row[0])
                all_urns.add(row[1])
            ancestors_cache = await self._compute_and_store_ancestors_bulk(
                list(all_urns),
            )
            t_ancestors = (time.monotonic() - t0) * 1000

            # Python-side Cartesian + accumulate. Per leaf edge, expand
            # (s_chain × t_chain) excluding self-loops. Dict keying by
            # (src_anc, tgt_anc) gives free deduplication across leaf
            # edges that share the same ancestor pair — the bulk-CREATE
            # phase then has a guaranteed-unique input.
            for s_urn, t_urn, edge_type, edge_id in rows:
                if not edge_id:
                    edge_id = f"{s_urn}|{edge_type}|{t_urn}"
                s_chain = [s_urn] + (ancestors_cache.get(s_urn, []) or [])
                t_chain = [t_urn] + (ancestors_cache.get(t_urn, []) or [])
                for sa in s_chain:
                    for ta in t_chain:
                        if sa == ta:
                            continue
                        pair = (sa, ta)
                        meta = pair_data.get(pair)
                        if meta is None:
                            meta = {
                                "weight": 0,
                                "edge_types": set(),
                                "contributors": [],
                                "source_level": None,
                                "target_level": None,
                            }
                            pair_data[pair] = meta
                        meta["weight"] += 1
                        meta["edge_types"].add(edge_type)
                        meta["contributors"].append(edge_id)

            processed += len(rows)
            last_row = rows[-1]
            current_cursor = f"{last_row[0]}|{last_row[1]}"

            logger.info(
                "bulk_rebuild scan batch %d: %d/%d edges | fetch=%.1fms "
                "ancestors=%.1fms | pairs_accumulated=%d",
                batch_num, processed, total, t_fetch, t_ancestors,
                len(pair_data),
            )

            if progress_callback is not None:
                try:
                    # Bulk rebuild's "created_count" during scan is the
                    # accumulated pair count — it becomes the actual
                    # created edge count after the CREATE phase. We
                    # report it here so the UI gets an early signal of
                    # how many AGGREGATED edges the run will produce.
                    await progress_callback(
                        processed, total, current_cursor, len(pair_data),
                    )
                except Exception as cb_exc:
                    logger.error(
                        "bulk_rebuild progress callback failed at batch %d: %s "
                        "(continuing)", batch_num, cb_exc, exc_info=True,
                    )

            if len(rows) < batch_size:
                break

        t_phase_b = (time.monotonic() - t_phase_b_start) * 1000
        logger.info(
            "bulk_rebuild phase B (scan): %d lineage edges, %d unique pairs "
            "in %.1fms",
            processed, len(pair_data), t_phase_b,
        )

        if not pair_data:
            logger.info("bulk_rebuild: no pairs to materialize, exiting early.")
            return {
                "processed": processed,
                "aggregated_edges_affected": 0,
                "input_edges_processed": processed,
                "errors": 0,
            }

        # ===== PHASE C: Resolve URN labels + ensure indexes =====
        t_phase_c_start = time.monotonic()
        pair_urns: Set[str] = set()
        for s, t in pair_data:
            pair_urns.add(s)
            pair_urns.add(t)
        urn_label_map = await self._resolve_urn_labels_bulk(list(pair_urns))

        # Stamp source/target level on each pair from the entity-type level map.
        if entity_levels:
            for (s, t), meta in pair_data.items():
                sl = urn_label_map.get(s)
                tl = urn_label_map.get(t)
                if sl is not None:
                    meta["source_level"] = entity_levels.get(sl)
                if tl is not None:
                    meta["target_level"] = entity_levels.get(tl)

        distinct_labels = {l for l in urn_label_map.values() if l}
        await self._ensure_label_urn_indexes(distinct_labels)
        t_phase_c = (time.monotonic() - t_phase_c_start) * 1000
        logger.info(
            "bulk_rebuild phase C (labels): %d distinct labels indexed in %.1fms",
            len(distinct_labels), t_phase_c,
        )

        # ===== PHASE D: Bulk-CREATE =====
        t_phase_d_start = time.monotonic()
        created = await self._bulk_create_aggregated_edges_from_pairs(
            pair_data=pair_data,
            urn_label_map=urn_label_map,
            level_digest=level_digest,
            intra_batch_callback=intra_batch_callback,
            baseline_aggregated=0,
            should_cancel=should_cancel,
        )
        t_phase_d = (time.monotonic() - t_phase_d_start) * 1000
        rate = (created * 1000.0 / max(t_phase_d, 1.0))
        logger.info(
            "bulk_rebuild phase D (CREATE): %d AGGREGATED edges in %.1fms "
            "(%.0f edges/s)",
            created, t_phase_d, rate,
        )

        # ===== PHASE E: Rebuild Redis idempotency state =====
        t_phase_e_start = time.monotonic()
        await self._rebuild_idempotency_state_from_pairs(
            pair_data, should_cancel=should_cancel,
        )
        t_phase_e = (time.monotonic() - t_phase_e_start) * 1000
        logger.info(
            "bulk_rebuild phase E (idempotency): %.1fms",
            t_phase_e,
        )

        # Final progress flush so the UI's created_edges counter lands
        # at the true CREATE count, not the in-scan pair-accumulation
        # estimate.
        if progress_callback is not None:
            try:
                await progress_callback(processed, total, current_cursor, created)
            except Exception:
                pass

        try:
            if self._redis is not None:
                from datetime import datetime, timezone
                await self._redis.set(
                    self._agg_last_materialized_key(),
                    datetime.now(timezone.utc).isoformat(),
                )
        except Exception as e:
            logger.warning(
                "Failed to stamp aggregated materialization timestamp: %s", e,
            )

        return {
            "processed": processed,
            "aggregated_edges_affected": created,
            "input_edges_processed": processed,
            "errors": 0,
        }

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

        # Phase 3: resolve s/t levels for the level-pair fast path (best-
        # effort via the URN→label cache + entity-type level map), then
        # single UNWIND+MERGE for all new pairs. Same rationale as the
        # batched materializer: coalesce in the Cypher SET preserves
        # backfilled level values when a fresh resolution misses.
        entity_levels: Dict[str, int] = getattr(self, "_entity_type_levels", None) or {}
        url_levels: Dict[str, Optional[int]] = {}
        if entity_levels:
            urn_set = {p[0] for p, _ in new_pairs} | {p[1] for p, _ in new_pairs}
            if urn_set:
                try:
                    label_key = self._urn_label_key()
                    label_pipe = self._redis.pipeline(transaction=False)
                    ordered = list(urn_set)
                    for u in ordered:
                        label_pipe.hget(label_key, u)
                    rows = await label_pipe.execute()
                    for u, raw in zip(ordered, rows):
                        if not raw:
                            continue
                        lbl = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        lvl = entity_levels.get(lbl)
                        if lvl is not None:
                            url_levels[u] = lvl
                except Exception as exc:
                    logger.warning(
                        "on_lineage_edge_written: level lookup failed: %s", exc,
                    )

        # UNKNOWN_LEVEL sentinel for endpoints whose label has no declared
        # level. Stamping -1 (instead of leaving sourceLevel NULL) keeps
        # the backfill convergent: the digest WHERE filter sees the edge
        # as "stamped" and skips it on re-runs.
        from backend.app.services.ontology_levels import UNKNOWN_LEVEL

        merge_batch = []
        for i, ((s_urn, t_urn), _) in enumerate(new_pairs):
            weight = scard_results[i] if scard_results[i] else 1
            merge_batch.append({
                "s": s_urn, "t": t_urn, "w": int(weight),
                "sl": url_levels.get(s_urn, UNKNOWN_LEVEL),
                "tl": url_levels.get(t_urn, UNKNOWN_LEVEL),
            })

        # Stamp the current levelDigest so the cold-start probe doesn't
        # flag freshly-created edges as needing backfill. When the
        # ontology drifts later, these edges go stale alongside the
        # pre-existing ones and the next backfill run re-stamps them.
        digest = self._level_digest or ""

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
            "r.sourceLevel = item.sl, "
            "r.targetLevel = item.tl, "
            "r.levelDigest = $digest, "
            "r.sourceEdgeTypes = CASE "
            "  WHEN r.sourceEdgeTypes IS NULL THEN [$edgeType] "
            "  WHEN NOT $edgeType IN r.sourceEdgeTypes "
            "    THEN r.sourceEdgeTypes + $edgeType "
            "  ELSE r.sourceEdgeTypes END, "
            "r.latestUpdate = timestamp()",
            params={"batch": merge_batch, "edgeType": edge_type, "digest": digest},
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
        are invalidated and lazily recomputed on next access. Targets the
        current containment-types namespace; older namespaces are
        unreachable so they don't need to be touched.
        """
        await self._ensure_connected()
        cache_key = self._ancestors_cache_key()

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

        # Phase 1 of aggregation hardening: dispatch to bulk-rebuild
        # (wipe + accumulate-in-memory + bulk-CREATE) when the env flag
        # is set. Defaults to enabled. Operators can roll back to the
        # legacy MERGE-per-batch path by setting
        # AGGREGATION_BULK_REBUILD_ENABLED=false if a production
        # regression surfaces. See plan at
        # /Users/rkrumins/.claude/plans/i-want-you-to-merry-minsky.md.
        _bulk_flag = os.getenv("AGGREGATION_BULK_REBUILD_ENABLED", "true")
        if str(_bulk_flag).strip().lower() in ("1", "true", "yes", "on"):
            return await self._materialize_aggregated_edges_bulk_rebuild(
                batch_size=batch_size,
                containment_edge_types=containment_edge_types,
                lineage_edge_types=lineage_edge_types,
                progress_callback=progress_callback,
                intra_batch_callback=intra_batch_callback,
                should_cancel=should_cancel,
                last_cursor=last_cursor,
            )

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

        # Cold-start / drift observability: the probe at
        # _check_levels_backfilled logs once per digest when stamps are
        # missing or stale. We do NOT re-log here per trace — the probe's
        # one-time log is enough and per-request logging spams when many
        # traces run against the same provider.
        #
        # The trace path itself stays correct in either state: stamped
        # edges take the level-pair fast path; unstamped (or -1-stamped)
        # edges fall back to the label-scan path inside
        # _expand_aggregated_set.

        # 1. Resolve anchor at the requested level (climb containment if needed).
        #
        #    Skeleton-first (level=0) branches:
        #      (a) focus_level known + ctypes present → try root anchor.
        #          If found at level 0, anchor there. If found at level>0
        #          (orphan), anchor there and report fallbackLevel. If
        #          resolution fails, fall through to legacy resolver.
        #      (b) focus_level unknown (ontology doesn't declare a level
        #          for the focus's entity type, e.g. Solidatus "layer") →
        #          skip root-anchor entirely. Anchor at the focus itself
        #          and signal effective_level=-1 so _expand_aggregated_set
        #          uses the peer-label fallback (same-label neighbours
        #          only). This is what stops layer→layer trace from
        #          spilling into attributes.
        fallback_level: Optional[int] = None
        effective_level = level
        if level == 0 and ctypes and focus_level is not None:
            root_urn, root_level = await self._resolve_root_anchor(urn, ctypes)
            if root_level == 0:
                anchor_urn = root_urn
            elif root_level > 0:
                anchor_urn = root_urn
                effective_level = root_level
                fallback_level = root_level
            else:
                anchor_urn = await self._resolve_anchor_at_level(urn, level, ctypes)
                if anchor_urn == urn and focus_level != 0:
                    effective_level = focus_level
                    fallback_level = focus_level
        elif level == 0 and focus_level is None:
            # Ontology has no declared level for the focus's entity type.
            # Anchor at the focus and rely on peer-label rollup downstream.
            anchor_urn = urn
            effective_level = -1
            fallback_level = -1
        else:
            anchor_urn = await self._resolve_anchor_at_level(urn, level, ctypes)

        # 2. Inherited-lineage fallback
        is_inherited = False
        inherited_from = None
        if include_inherited_lineage and not await self._has_aggregated_at_level(anchor_urn, effective_level, ltypes):
            parent = await self._find_ancestor_with_lineage(anchor_urn, effective_level, ctypes, ltypes)
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
        # Per-source-URN contribution counts. After BFS, any source that hit
        # TRACE_DEGREE_CAP is a mega-node candidate — emitted in meta.megaNodes
        # so the UI can render a "+N more" chip and offer targeted re-expand.
        per_source_count: Dict[str, int] = {}

        # 4. Per-hop set-based expansion
        max_depth = max(upstream_depth, downstream_depth)
        for hop in range(max_depth):
            remaining_secs = deadline - time.monotonic()
            if remaining_secs <= 0:
                truncation_reason = "timeout"
                break
            if len(nodes_by_urn) >= max_nodes:
                truncation_reason = "max_nodes"
                break
            budget = max_nodes - len(nodes_by_urn)

            # Build frontier→label maps from already-fetched nodes. New
            # frontier members were hydrated by the previous hop's
            # `rec.get("node")` payload, so their entity_type is known
            # without an extra round-trip.
            up_labels = {
                u: _sanitize_label(str(nodes_by_urn[u].entity_type))
                for u in up_frontier if u in nodes_by_urn
            }
            down_labels = {
                u: _sanitize_label(str(nodes_by_urn[u].entity_type))
                for u in down_frontier if u in nodes_by_urn
            }

            # Per-hop wall-clock budget. Up to two directions run in
            # parallel, each issuing 1-2 sub-queries — splitting the
            # remaining budget across them lets a slow hop fail fast
            # rather than starving subsequent hops.
            hop_timeout_secs = max(0.6, min(1.5, remaining_secs / 2))

            tasks = []
            if hop < upstream_depth and up_frontier:
                tasks.append(("up", self._expand_aggregated_set(
                    list(up_frontier), up_labels, "incoming",
                    effective_level, ltypes, budget, hop_timeout_secs,
                    default_peer_label=focus_entity_type,
                )))
            if hop < downstream_depth and down_frontier:
                tasks.append(("down", self._expand_aggregated_set(
                    list(down_frontier), down_labels, "outgoing",
                    effective_level, ltypes, budget, hop_timeout_secs,
                    default_peer_label=focus_entity_type,
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
                        # Track aggregated edges per anchor (the frontier-side
                        # URN). Direction-aware: for upstream BFS the anchor
                        # is the target; for downstream it's the source.
                        if actual_type == "AGGREGATED":
                            anchor_for_count = (
                                rec["targetUrn"] if direction == "up"
                                else rec["sourceUrn"]
                            )
                            per_source_count[anchor_for_count] = (
                                per_source_count.get(anchor_for_count, 0) + 1
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

        # SAFETY NET: if skeleton-first (level=0) yielded zero lineage edges,
        # retry at the focus's own level (legacy "auto" peer-rollup). Two
        # paths trigger this:
        #   (a) focus_level known → retry at that int level
        #   (b) focus_level None (ontology missing the focus's level) →
        #       retry with level=-1 (sentinel meaning "no level filter,
        #       use peer-label fallback in _expand_aggregated_set")
        # The frontend safety-net memo: never return empty when the wire
        # had lineage to give. One retry; no recursion.
        needs_retry = (
            not edges_by_id
            and level == 0
            and effective_level == 0
            and not is_inherited  # don't retry if inherited-fallback already moved us
            and (
                (focus_level is not None and focus_level != 0)
                or focus_level is None
            )
        )
        if needs_retry:
            retry_level = focus_level if focus_level is not None else -1
            logger.info(
                "trace: level=0 yielded no lineage for %s (focus_level=%s) — "
                "retrying at level=%s (peer-rollup)", urn, focus_level, retry_level,
            )
            effective_level = retry_level
            fallback_level = retry_level
            # Re-anchor at focus URN itself for peer rollup at focus level
            anchor_urn = urn
            anchor_node = focus_node
            if anchor_node:
                nodes_by_urn = {anchor_urn: anchor_node}
            else:
                nodes_by_urn = {}
            edges_by_id = {}
            upstream_urns = set()
            downstream_urns = set()
            visited = {anchor_urn}
            up_frontier = {anchor_urn} if upstream_depth > 0 else set()
            down_frontier = {anchor_urn} if downstream_depth > 0 else set()
            per_source_count = {}

            # Single retry pass — same loop body, but bounded
            for hop in range(max_depth):
                remaining_secs = deadline - time.monotonic()
                if remaining_secs <= 0:
                    truncation_reason = "timeout"
                    break
                if len(nodes_by_urn) >= max_nodes:
                    truncation_reason = "max_nodes"
                    break
                budget = max_nodes - len(nodes_by_urn)
                up_labels = {
                    u: _sanitize_label(str(nodes_by_urn[u].entity_type))
                    for u in up_frontier if u in nodes_by_urn
                }
                down_labels = {
                    u: _sanitize_label(str(nodes_by_urn[u].entity_type))
                    for u in down_frontier if u in nodes_by_urn
                }
                hop_timeout_secs = max(0.6, min(1.5, remaining_secs / 2))
                tasks = []
                if hop < upstream_depth and up_frontier:
                    tasks.append(("up", self._expand_aggregated_set(
                        list(up_frontier), up_labels, "incoming",
                        effective_level, ltypes, budget, hop_timeout_secs,
                        default_peer_label=focus_entity_type,
                    )))
                if hop < downstream_depth and down_frontier:
                    tasks.append(("down", self._expand_aggregated_set(
                        list(down_frontier), down_labels, "outgoing",
                        effective_level, ltypes, budget, hop_timeout_secs,
                        default_peer_label=focus_entity_type,
                    )))
                if not tasks:
                    break
                results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
                new_up: Set[str] = set()
                new_down: Set[str] = set()
                for (direction, _), recs in zip(tasks, results):
                    if isinstance(recs, Exception):
                        logger.warning("trace_at_level retry expand (%s) failed: %s", direction, recs)
                        continue
                    for rec in recs:
                        edge_id = rec["edgeId"]
                        if edge_id not in edges_by_id:
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
            try:
                ancestor_urns = await self._collect_ancestor_urns(
                    list(nodes_by_urn.keys()), ctypes,
                )
            except Exception:
                # Lineage was already collected; surface the partial result
                # via truncationReason so the frontend safety-net renders
                # the lineage without the (now-missing) ancestor chain.
                ancestor_urns = []
                truncation_reason = truncation_reason or "ancestors_failed"
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

        # Mega-node detection: any anchor whose AGGREGATED contribution
        # exceeded the per-source degree cap is reported back to the
        # engine via a private attribute. Used by ContextEngine to fill
        # TraceMeta.megaNodes — the UI renders a "+N more" chip and
        # offers a targeted re-expand.
        mega_nodes_dicts: List[Dict[str, Any]] = []
        for source_urn, count in per_source_count.items():
            if count >= self.TRACE_DEGREE_CAP:
                direction_hint = (
                    "downstream" if source_urn in downstream_urns or source_urn == anchor_urn
                    else "upstream"
                )
                mega_nodes_dicts.append({
                    "urn": source_urn,
                    "shown": count,
                    "total": count,  # actual total unknown without extra round-trip
                    "direction": direction_hint,
                })
                if truncation_reason is None:
                    truncation_reason = "degree_cap"

        result = TraceResult(
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
            effectiveLevel=effective_level,
            isInherited=is_inherited,
            inheritedFromUrn=inherited_from,
            truncated=(truncation_reason is not None),
            truncationReason=truncation_reason,
        )
        # Stash extras outside the pydantic schema for the engine to read.
        # `object.__setattr__` bypasses pydantic's __setattr__ guard so we
        # don't have to widen the public model just for transport.
        if mega_nodes_dicts:
            object.__setattr__(result, "_mega_nodes", mega_nodes_dicts)
        if fallback_level is not None:
            object.__setattr__(result, "_fallback_level", fallback_level)
        return result

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

        # Single-query pair fetch: source + target descendants in one
        # UNION'd Cypher round-trip. Saves one planner pass and frees a
        # pool slot for the duration. Surfaces the (now-single) failure
        # mode via truncationReason rather than aborting the expand.
        truncation_reason: Optional[str] = None
        try:
            s_urns, t_urns = await self._collect_descendants_pair_at_level(
                source_urn, target_urn, next_level, ctypes, max_nodes,
            )
        except Exception:
            s_urns, t_urns = [], []
            truncation_reason = "descendants_failed"

        if time.monotonic() > deadline:
            truncation_reason = truncation_reason or "timeout"

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
            try:
                ancestor_urns = await self._collect_ancestor_urns(
                    list(nodes_by_urn.keys()), ctypes,
                )
            except Exception:
                ancestor_urns = []
                truncation_reason = truncation_reason or "ancestors_failed"
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

        Cache-first: reads the ancestor chain from the Redis cache populated
        by aggregation (:func:`_get_ancestor_chain`) and resolves each
        ancestor's level via the in-process entity-type → level map. The
        URN → label cache (:func:`_get_cached_label`) typically already
        holds labels for chain URNs as a side effect of materialization /
        prior :func:`get_node` calls; any gaps are filled with a single
        batch ``WHERE n.urn IN $urns RETURN n.urn, labels(n)[0]`` round-
        trip (no variable-length walk, no path sort).

        Falls back to the legacy variable-length Cypher only when the
        cache produces no chain AND the focus is not already at the
        requested level — preserves correctness on cold graphs while the
        common case becomes a Redis HGET + a small Python loop.
        """
        if not ctypes:
            return urn
        entity_levels: Dict[str, int] = getattr(self, "_entity_type_levels", None) or {}

        # Step 1: is the focus itself at the target level?
        focus_label = await self._get_cached_label(urn)
        if focus_label and entity_levels.get(focus_label) == level:
            return urn

        # Step 2: walk the cached ancestor chain.
        try:
            chain = await self._get_ancestor_chain(urn)
        except Exception:
            chain = []

        if chain and entity_levels:
            # Resolve labels for chain URNs (cache + one batch top-up).
            labels: Dict[str, Optional[str]] = {}
            missing: List[str] = []
            for u in chain:
                cached = await self._get_cached_label(u)
                labels[u] = cached
                if not cached:
                    missing.append(u)
            if missing:
                try:
                    res = await self._ro_query(
                        "MATCH (n) WHERE n.urn IN $urns "
                        "RETURN n.urn AS urn, labels(n)[0] AS label",
                        params={"urns": missing}, timeout=1.5,
                    )
                    bulk: Dict[str, str] = {}
                    for row in (res.result_set or []):
                        if row and row[0] and row[1]:
                            labels[row[0]] = row[1]
                            bulk[row[0]] = row[1]
                    if bulk:
                        await self._cache_urn_labels_bulk(bulk)
                except Exception as exc:
                    logger.warning(
                        "trace_at_level: anchor label batch fetch failed: %s", exc,
                    )

            for ancestor_urn in chain:
                lbl = labels.get(ancestor_urn)
                if lbl and entity_levels.get(lbl) == level:
                    return ancestor_urn
            # Chain authoritatively walked to root without a match.
            return urn

        # Step 3: cold-cache fallback. Bound the variable-length walk by
        # max-known hierarchy depth (or 10 when the level map is empty)
        # and cap the Cypher with a tight ``:timeout`` so a slow planner
        # cannot consume the trace deadline here.
        types = self._types_at_level(level)
        if not types:
            return urn
        max_depth = max(len(entity_levels), 10) if entity_levels else 10
        # NB: path-uniqueness predicate was attempted here but removed —
        # FalkorDB's planner doesn't always accept nested list-comprehension
        # `size(...)` inside path-bound ALL(), and the legacy form was
        # already cycle-safe via bounded max_depth + try/except. Cycle
        # protection for the new skeleton-first path lives in
        # _resolve_root_anchor (which itself falls back on failure).
        cypher = (
            "MATCH (focus {urn: $urn}) "
            f"OPTIONAL MATCH path = (focus)<-[c*0..{max_depth}]-(anc) "
            "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            "  AND labels(anc)[0] IN $types "
            "RETURN coalesce(anc.urn, focus.urn) AS anchorUrn "
            "ORDER BY length(path) ASC LIMIT 1"
        )
        try:
            result = await self._ro_query(
                cypher, params={"urn": urn, "ctypes": ctypes, "types": types},
                timeout=1.5,
            )
            rows = result.result_set or []
            if rows and rows[0]:
                return rows[0][0] or urn
        except Exception as exc:
            logger.warning("trace_at_level: anchor resolution fallback failed for %s: %s", urn, exc)
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
            # Tight ``:timeout`` — this is an existence check on the
            # trace hot path; if FalkorDB can't decide in ~1s the
            # planner is doing something wrong and we'd rather
            # fail-open (skip the inherited-lineage fallback) than
            # block the whole trace.
            result = await self._proj_ro_query(cypher, params=params, timeout=1.0)
            return bool(result.result_set)
        except Exception as exc:
            logger.warning("trace_at_level: has-lineage check failed for %s: %s", anchor_urn, exc)
            return True  # fail-open: skip the inherited-lineage fallback

    async def _find_ancestor_with_lineage(
        self, anchor_urn: str, level: int, ctypes: List[str],
        ltypes: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Find the nearest ancestor of ``anchor_urn`` that (a) is at the
        target ``level`` and (b) has at least one lineage edge there.

        Folds the previous "fetch 5 candidates + 1-5 ``_has_aggregated_at_level``
        round-trips" pattern into a single Cypher: the inner pattern
        predicate ``(parent)-[:AGGREGATED|...]-()`` filters candidates by
        edge existence directly in the planner, returning only the
        nearest ancestor that qualifies.

        The pattern predicate doesn't constrain the peer's level — a node
        that has AGGREGATED edges is overwhelmingly to peers at the same
        level (the materialiser pairs ancestors level-for-level), and a
        false positive just means the subsequent BFS finds an empty set
        for that anchor, which is cheaper than 5 extra existence
        checks per trace.
        """
        if not ctypes:
            return None
        types = self._types_at_level(level)
        if not types:
            return None

        # Relationship-type alternation: AGGREGATED rollup plus any raw
        # lineage types the caller declared. Sanitized to keep the
        # dynamic pattern injection-safe.
        rel_parts: List[str] = ["AGGREGATED"]
        if ltypes:
            rel_parts.extend(_sanitize_label(t) for t in ltypes)
        rel_alt = "|".join(rel_parts)

        max_depth = max(len(getattr(self, "_entity_type_levels", {}) or {}), 10)

        # NB: path-uniqueness predicate removed — legacy form, bounded by
        # max_depth + try/except. See note in _resolve_anchor_at_level.
        cypher = (
            "MATCH (a {urn: $anchor})"
            f"<-[c*1..{max_depth}]-(parent) "
            "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            "  AND labels(parent)[0] IN $types "
            "WITH parent, length(c) AS depth "
            "ORDER BY depth ASC LIMIT 5 "
            f"WITH parent, depth WHERE (parent)-[:{rel_alt}]-() "
            "RETURN parent.urn AS urn "
            "ORDER BY depth ASC LIMIT 1"
        )
        params = {"anchor": anchor_urn, "ctypes": ctypes, "types": types}
        try:
            result = await self._ro_query(cypher, params=params, timeout=1.5)
            rows = result.result_set or []
            if rows and rows[0] and rows[0][0]:
                return rows[0][0]
        except Exception as exc:
            logger.warning(
                "trace_at_level: find-ancestor-with-lineage failed for %s: %s",
                anchor_urn, exc,
            )
        return None

    async def _expand_aggregated_set(
        self,
        frontier: List[str],
        frontier_labels: Dict[str, str],
        direction: str,
        level: int,
        ltypes: Optional[List[str]],
        limit: int,
        timeout_secs: float,
        default_peer_label: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Per-hop expansion. Direction: 'incoming' (BFS upstream) or 'outgoing'
        (BFS downstream). Returns a list of dicts shaped for the BFS loop:
        {sourceUrn, targetUrn, edgeId, edgeType, edgeTypes, weight, node}.

        ``frontier_labels`` maps each URN to its entity-type label so each
        per-label sub-query can use the per-label ``(:Label).urn`` index for
        an index-seek instead of a full-graph property scan. URNs without a
        known label fall back to a label-less pattern (still correct, just
        slower).

        ``default_peer_label`` is the focus's sanitized entity-type label,
        used as the neighbour filter when no level-set and no per-bucket
        label can constrain the expansion. Without this, the query would
        walk to ANY neighbour and a Layer-focused trace would over-fetch
        into Attribute children (the original Solidatus bug). Pass the
        focus's entity_type so peer-rollup always has a fallback.

        Sub-queries per label bucket per direction:

        * AGGREGATED rollup — rel-typed pattern ``[r:AGGREGATED]`` filtered
          by ``r.sourceLevel`` / ``r.targetLevel`` (the level-pair fast
          path stamped by the materialiser + backfilled by
          ``backfill_aggregated_levels.py``). When the level map is
          missing or the AGGREGATED edge-property index hasn't been
          created, falls back to ``labels(other)[0] IN $types`` — the
          legacy neighbour-label scan.
        * Raw lineage — rel-type alternation ``[r:LTYPE1|LTYPE2|...]`` when
          ``ltypes`` is set, so fine-grained traces (schemaField /
          column) still walk TRANSFORMS / FLOWS_TO etc. Raw edges are
          not level-stamped, so this branch keeps the label filter.

        Each sub-query carries a Cypher ``:timeout`` capped at
        ``timeout_secs`` so a single bad sub-query cannot consume the
        whole BFS budget — FalkorDB cancels it server-side and the BFS
        loop logs and moves on with what it already has.
        """
        if not frontier or limit <= 0:
            return []

        types = self._types_at_level(level)
        # When the entity-type level map is available, prefer the
        # level-pair filter on AGGREGATED edges. The materialiser stamps
        # ``r.sourceLevel``/``r.targetLevel`` on new edges; legacy edges
        # are covered by ``backfill_aggregated_levels.py``.
        entity_levels: Dict[str, int] = getattr(self, "_entity_type_levels", None) or {}
        use_level_filter = bool(entity_levels) and level >= 0

        # Group frontier URNs by their entity-type label so each sub-query
        # uses the per-label ``urn`` index. URNs without a known label go
        # into the "" bucket and use a label-less fallback pattern.
        by_label: Dict[str, List[str]] = {}
        for urn in frontier:
            lbl = frontier_labels.get(urn) or ""
            by_label.setdefault(lbl, []).append(urn)

        # Direction shapes: ``f`` is the frontier-side variable, ``other`` is
        # the neighbour we're expanding into. Edge orientation in the returned
        # record is always (sourceUrn -> targetUrn).
        if direction == "incoming":
            arrow_template = "<-[r{rel}]-"
            source_var, target_var = "other", "f"
        else:
            arrow_template = "-[r{rel}]->"
            source_var, target_var = "f", "other"

        def _build(rel_clause: str, *, where_parts: List[str], order_by_weight: bool) -> str:
            where = ("WHERE " + " AND ".join(where_parts) + " ") if where_parts else ""
            # For AGGREGATED edges, ORDER BY r.weight DESC ensures the
            # per-source LIMIT keeps the highest-confidence edges first
            # (top-N by edge count). Without it, a super-hub Domain would
            # truncate arbitrarily. Raw lineage edges don't have weight,
            # so we skip the ORDER BY in that branch.
            order = "ORDER BY weight DESC " if order_by_weight else ""
            return (
                "UNWIND $frontier AS u "
                f"MATCH (f{{F_LABEL}} {{urn: u}}){arrow_template.format(rel=rel_clause)}(other) "
                + where
                + f"WITH {source_var}.urn AS sourceUrn, {target_var}.urn AS targetUrn, "
                "id(r) AS edgeId, type(r) AS edgeType, "
                "COALESCE(r.sourceEdgeTypes, [type(r)]) AS edgeTypes, "
                "COALESCE(r.weight, 1) AS weight, other AS otherNode "
                + order
                + "RETURN sourceUrn, targetUrn, edgeId, edgeType, edgeTypes, weight, otherNode "
                "LIMIT $limit"
            )

        # Per-query timeout. The wrapper subtracts 500ms for the DB-side
        # cancel; clamp the floor at 0.6s so a tight remaining-budget still
        # gives FalkorDB a useful slice (~100ms).
        per_query_timeout = max(0.6, min(1.5, timeout_secs))

        # Sanitize the focus's entity-type once — used as the fallback
        # neighbour filter when a frontier bucket has no per-URN label
        # (because get_node returned None, entity_type wasn't populated,
        # or labels(n)[0] didn't match the upsert convention).
        sanitized_default_peer = (
            _sanitize_label(default_peer_label) if default_peer_label else ""
        )

        queries: List[tuple[str, Dict[str, Any]]] = []
        for f_label, urns in by_label.items():
            sanitized_self_label = _sanitize_label(f_label) if f_label else ""
            label_clause = f":{sanitized_self_label}" if sanitized_self_label else ""

            # Peer-rollup neighbour filter. Order of preference:
            #   1. Per-bucket frontier label (sanitized_self_label)
            #   2. Caller-supplied default (focus entity_type)
            # If NEITHER is set, refuse to emit an unconstrained query —
            # the legacy "no filter at all" path is the over-fetch bug
            # that pulled Attributes into a Layer trace.
            effective_peer_label = sanitized_self_label or sanitized_default_peer
            peer_filter_clause: Optional[str] = None
            if effective_peer_label:
                peer_filter_clause = f"labels(other)[0] = '{effective_peer_label}'"
            else:
                logger.warning(
                    "trace expand: no peer label for bucket=%r and no default — "
                    "skipping sub-query to avoid unconstrained over-fetch",
                    f_label,
                )
                # Skip this bucket entirely. Better to return zero edges
                # than to return every neighbour in the graph.
                continue

            # AGGREGATED branch. The level-pair fast path is the primary
            # filter when available; otherwise label scan or peer fallback.
            agg_where: List[str] = []
            if use_level_filter:
                agg_where.append("r.sourceLevel = $level AND r.targetLevel = $level")
            elif types:
                agg_where.append("labels(other)[0] IN $types")
            elif peer_filter_clause:
                agg_where.append(peer_filter_clause)
            if ltypes:
                agg_where.append(
                    "(r.sourceEdgeTypes IS NULL "
                    "OR any(et IN r.sourceEdgeTypes WHERE et IN $ltypes))"
                )
            agg_cypher = _build(
                ":AGGREGATED", where_parts=agg_where, order_by_weight=True,
            ).replace("{F_LABEL}", label_clause)
            agg_params: Dict[str, Any] = {"frontier": urns, "limit": limit}
            if use_level_filter:
                agg_params["level"] = level
            elif types:
                agg_params["types"] = types
            if ltypes:
                agg_params["ltypes"] = ltypes
            queries.append((agg_cypher, agg_params))

            # Raw-lineage branch (only when ltypes provided). Raw edges
            # don't carry level props, so this branch uses the type-set
            # filter, or peer-label fallback when types is empty.
            if ltypes:
                rel_alt = "|".join(_sanitize_label(t) for t in ltypes)
                raw_where: List[str] = []
                if types:
                    raw_where.append("labels(other)[0] IN $types")
                elif peer_filter_clause:
                    raw_where.append(peer_filter_clause)
                raw_cypher = _build(
                    f":{rel_alt}", where_parts=raw_where, order_by_weight=False,
                ).replace("{F_LABEL}", label_clause)
                raw_params: Dict[str, Any] = {"frontier": urns, "limit": limit}
                if types:
                    raw_params["types"] = types
                queries.append((raw_cypher, raw_params))

        if not queries:
            return []

        async def _run(c: str, p: Dict[str, Any]):
            try:
                return await self._proj_ro_query(
                    c, params=p, timeout=per_query_timeout,
                )
            except Exception as exc:
                logger.warning(
                    "trace_at_level: expand sub-query (%s) failed: %s",
                    direction, exc,
                )
                return None

        results = await asyncio.gather(*(_run(c, p) for c, p in queries))

        out: List[Dict[str, Any]] = []
        seen_edge_ids: Set[str] = set()
        for result in results:
            if result is None:
                continue
            for row in (result.result_set or []):
                try:
                    edge_type = str(row[3]) if row[3] is not None else "AGGREGATED"
                    eid = str(row[2]) if row[2] is not None else (
                        f"{edge_type.lower()}-{row[0]}-{row[1]}"
                    )
                    # Dedupe across the AGGREGATED + raw-lineage sub-queries:
                    # a raw lineage edge might also appear in the AGGREGATED
                    # rollup (sourceEdgeTypes contains its type). Keep first.
                    if eid in seen_edge_ids:
                        continue
                    seen_edge_ids.add(eid)
                    rec = {
                        "sourceUrn": row[0],
                        "targetUrn": row[1],
                        "edgeId": eid,
                        "edgeType": edge_type,
                        "edgeTypes": row[4] if isinstance(row[4], list) else (
                            [row[4]] if row[4] else [edge_type]
                        ),
                        "weight": int(row[5]) if row[5] is not None else 1,
                        "node": self._extract_node_from_result([row[6]]) if row[6] is not None else None,
                    }
                    out.append(rec)
                    if len(out) >= limit:
                        return out
                except Exception:
                    continue
        return out

    async def _collect_ancestor_urns(
        self, urns: List[str], ctypes: List[str],
    ) -> List[str]:
        """Collect ALL containment ancestors of the given URNs.

        Foundational for trace responses: a trace returns lineage URNs at
        whatever level the user picked (e.g. column-level schemaFields), but
        the canvas needs the full ancestor chain (Dataset → Container →
        Domain) to position those URNs in the layered hierarchy. Without
        this, the trace nodes render as orphans or get filtered out by layer
        assignment.

        Reads from the Redis ancestor-chain cache populated by aggregation
        (:func:`_get_ancestor_chain` / :func:`_compute_and_store_ancestors_bulk`).
        On cache miss the bulk helper falls back to a per-URN typed Cypher
        with concurrency 4, then back-fills the cache for future trace
        requests. This replaces the previous single ``UNWIND $urns ...
        <-[c*1..10]-(ancestor)`` query that re-walked containment on every
        trace and was the second-biggest CPU consumer after the BFS itself.

        Raises on hard failure (Redis + Cypher both unavailable) so the
        caller can surface ``truncationReason="ancestors_failed"`` instead
        of silently dropping the containment chain (which produces canvas
        orphans).
        """
        if not urns or not ctypes:
            return []
        try:
            chains = await self._compute_and_store_ancestors_bulk(list(urns))
        except Exception as exc:
            logger.warning(
                "trace_at_level: ancestor collection failed for %d urns: %s",
                len(urns), exc,
            )
            raise

        # ``_compute_and_store_ancestors_bulk`` returns a {urn: chain} map.
        # Flatten + dedupe while preserving first-seen order so any caller
        # that depends on parent-before-grandparent ordering still gets it.
        seen: Set[str] = set()
        out: List[str] = []
        for chain in chains.values():
            for ancestor in chain or []:
                if ancestor and ancestor not in seen:
                    seen.add(ancestor)
                    out.append(ancestor)
        return out

    async def _collect_descendants_pair_at_level(
        self,
        source_urn: str,
        target_urn: str,
        target_level: int,
        ctypes: List[str],
        limit: int,
    ) -> Tuple[List[str], List[str]]:
        """Collect descendants of both anchors in a SINGLE Cypher round-trip.

        Bounded depth-10 containment descent; per-anchor row LIMIT applied
        before ``collect()`` so the slice form (which previously tripped
        FalkorDB's "expected List or Null but was Edge" planner error) is
        never used.

        Returns ``(source_urns, target_urns)``. Either side may be empty if
        the anchor's label does not match ``target_level``'s type set.
        """
        types = self._types_at_level(target_level)
        if not types:
            return [], []

        if not ctypes:
            # Empty containment — descendants of each anchor reduce to
            # the anchor itself, but only if its label matches.
            cypher = (
                "MATCH (a {urn: $source}) WHERE labels(a)[0] IN $types "
                "RETURN 's' AS side, [a.urn] AS urns "
                "UNION "
                "MATCH (b {urn: $target}) WHERE labels(b)[0] IN $types "
                "RETURN 't' AS side, [b.urn] AS urns"
            )
            params: Dict[str, Any] = {
                "source": source_urn, "target": target_urn, "types": types,
            }
        else:
            # UNION over per-anchor branches — same `WITH DISTINCT … LIMIT`
            # streaming pattern as the single-anchor helper used to (A1) so
            # the per-side `$limit` applies before ``collect()`` and the
            # path-alias never enters a slice context. One round-trip
            # instead of the prior two.
            #
            # Variable-length bound = max ontology depth (floor 10) so
            # very deep ontologies aren't truncated and shallow ones
            # don't pay for unused depth.
            max_depth = max(len(getattr(self, "_entity_type_levels", {}) or {}), 10)
            # NB: path-uniqueness predicate removed — legacy form, bounded by
            # max_depth. See note in _resolve_anchor_at_level.
            cypher = (
                f"MATCH (a {{urn: $source}})-[c*0..{max_depth}]->(child) "
                "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
                "  AND labels(child)[0] IN $types "
                "WITH DISTINCT child.urn AS urn "
                "LIMIT $limit "
                "RETURN 's' AS side, collect(urn) AS urns "
                "UNION "
                f"MATCH (b {{urn: $target}})-[c*0..{max_depth}]->(child) "
                "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
                "  AND labels(child)[0] IN $types "
                "WITH DISTINCT child.urn AS urn "
                "LIMIT $limit "
                "RETURN 't' AS side, collect(urn) AS urns"
            )
            params = {
                "source": source_urn, "target": target_urn,
                "ctypes": ctypes, "types": types, "limit": limit,
            }
        try:
            result = await self._ro_query(cypher, params=params, timeout=2.0)
        except Exception as exc:
            logger.warning(
                "trace_at_level: descendant pair collection failed for (%s, %s): %s",
                source_urn, target_urn, exc,
            )
            raise

        s_urns: List[str] = []
        t_urns: List[str] = []
        for row in (result.result_set or []):
            if not row or len(row) < 2:
                continue
            side = row[0]
            urns = row[1] if isinstance(row[1], list) else []
            urn_list = [u for u in urns if u]
            if side == 's':
                s_urns = urn_list
            elif side == 't':
                t_urns = urn_list
        return s_urns, t_urns

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
        """Containment edges where both endpoints are in ``urns``.

        Pair-list driven: builds the parent→child pairs we expect to exist
        from the cached ancestor chains (already populated by aggregation
        + the earlier :func:`_collect_ancestor_urns` call in
        :func:`trace_at_level`). Then issues ONE rel-typed Cypher to
        resolve the real edge type + id per pair.

        Replaces the previous ``UNWIND $urns ... MATCH (s)-[r]->(t)
        WHERE t.urn IN $urns AND type(r) IN $ctypes`` which scanned every
        outgoing edge from every URN before filtering — quadratic on
        wide trace results and a major contributor to the 8s timeout on
        100k-node graphs.

        Cold-cache fallback uses the same rel-typed alternation pattern
        so it's still faster than the legacy form.
        """
        if not urns or not ctypes:
            return []

        rel_alt = "|".join(_sanitize_label(c) for c in ctypes)
        urn_set = set(urns)

        # Build (parent, child) pair candidates from cached chains.
        try:
            chains = await self._compute_and_store_ancestors_bulk(list(urns))
        except Exception:
            chains = {}

        pairs: Set[tuple] = set()
        for child_urn, chain in (chains or {}).items():
            prev = child_urn
            for ancestor in chain or []:
                if ancestor in urn_set and prev in urn_set:
                    pairs.add((ancestor, prev))
                prev = ancestor

        if pairs:
            pair_list = [{"s": s, "t": t} for s, t in pairs]
            cypher = (
                "UNWIND $pairs AS p "
                f"MATCH (s {{urn: p.s}})-[r:{rel_alt}]->(t {{urn: p.t}}) "
                "RETURN p.s AS sUrn, p.t AS tUrn, "
                "type(r) AS edgeType, id(r) AS edgeId"
            )
            try:
                result = await self._ro_query(
                    cypher, params={"pairs": pair_list}, timeout=2.0,
                )
            except Exception as exc:
                logger.warning(
                    "trace_at_level: containment edge pair-fetch failed (%d pairs): %s",
                    len(pair_list), exc,
                )
                return []
        else:
            # Cold-cache fallback. Still rel-typed (avoids the OR-on-type
            # full edge scan of the legacy query).
            cypher = (
                "UNWIND $urns AS u "
                f"MATCH (s {{urn: u}})-[r:{rel_alt}]->(t) "
                "WHERE t.urn IN $urns "
                "RETURN s.urn AS sUrn, t.urn AS tUrn, "
                "type(r) AS edgeType, id(r) AS edgeId"
            )
            try:
                result = await self._ro_query(
                    cypher, params={"urns": list(urns)}, timeout=2.0,
                )
            except Exception as exc:
                logger.warning(
                    "trace_at_level: containment edge fallback fetch failed: %s",
                    exc,
                )
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
