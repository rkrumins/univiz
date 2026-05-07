"""
Neo4j Bolt adapter for GraphDataProvider.

Production-grade implementation connecting to any Neo4j database via the
official async driver.  A configurable SchemaMapping layer translates
foreign property names (e.g. ``uuid``, ``title``) to Synodic's canonical
model (``urn``, ``displayName``).

Key design decisions
--------------------
* ``execute_read`` / ``execute_write`` with **async** work functions for
  automatic transient-error retry (Neo4j 5.x async driver requirement).
* In-memory ``_TTLCache`` and LRU ``_URNLabelCache`` (no Redis requirement).
* Optional Redis for ancestor-chain caching when ``extra_config.redisUrl``
  is set; falls back to Cypher on-the-fly when absent.
* Batched BFS for ``get_trace_lineage`` — one Cypher per depth level.
* ``discover_schema`` introspects unknown databases and suggests mappings.
* Idempotent AGGREGATED edge materialization via ``sourceEdgeIds`` tracking.
"""

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict, defaultdict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from backend.common.interfaces.provider import GraphDataProvider
from backend.common.models.graph import (
    GraphNode, GraphEdge, NodeQuery, EdgeQuery,
    LineageResult, GraphSchemaStats,
    PropertyFilter, TagFilter, TextFilter, FilterOperator,
    EntityTypeSummary, EdgeTypeSummary, TagSummary,
    OntologyMetadata, EdgeTypeMetadata, EntityTypeHierarchy,
    AggregatedEdgeResult, AggregatedEdgeInfo,
    TraceResult, TraceFocus,
)
from .schema_mapping import SchemaMapping, map_node_props, map_edge_props

logger = logging.getLogger(__name__)


# ====================================================================
# Module-level helpers (zero driver dependency)
# ====================================================================

def _sanitize_label(s: str) -> str:
    """Alphanumeric + underscore only — safe for Cypher identifiers."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(s))


def _node_from_props(props: Dict[str, Any], entity_type_str: Optional[str] = None) -> Optional[GraphNode]:
    """Build GraphNode from a canonical property dict."""
    if not props or not props.get("urn"):
        return None
    entity_type = entity_type_str or props.get("entityType", "container")
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
        logger.warning("Failed to build GraphNode: %s", e)
        return None


def _edge_from_row(source_urn: str, target_urn: str, rel_type: str, props: Dict[str, Any]) -> GraphEdge:
    """Build GraphEdge from canonical edge data."""
    edge_id = props.get("id") or f"{source_urn}|{rel_type}|{target_urn}"
    return GraphEdge(
        id=edge_id,
        sourceUrn=source_urn,
        targetUrn=target_urn,
        edgeType=str(rel_type),
        confidence=props.get("confidence"),
        properties=json.loads(props["properties"]) if isinstance(props.get("properties"), str) else (props.get("properties") or {}),
    )


# ====================================================================
# In-memory caches
# ====================================================================

class _TTLCache:
    """Simple single-value TTL cache using monotonic clock."""

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._value: Any = None
        self._expires: float = 0.0

    def get(self) -> Any:
        if time.monotonic() < self._expires:
            return self._value
        return None

    def set(self, value: Any) -> None:
        self._value = value
        self._expires = time.monotonic() + self._ttl

    def invalidate(self) -> None:
        self._expires = 0.0


class _URNLabelCache:
    """Bounded LRU cache for URN -> label mappings using OrderedDict.

    On ``get()`` hit the entry is moved to the end (most-recently-used).
    On eviction the *least* recently used 10 % of entries are removed.
    """

    def __init__(self, max_size: int = 50_000):
        self._max = max_size
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, urn: str) -> Optional[str]:
        val = self._data.get(urn)
        if val is not None:
            self._data.move_to_end(urn)  # Mark as recently used
        return val

    def put(self, urn: str, label: str) -> None:
        if urn in self._data:
            self._data.move_to_end(urn)
            self._data[urn] = label
            return
        if len(self._data) >= self._max:
            # Evict least-recently-used ~10 %
            evict_count = self._max // 10
            for _ in range(evict_count):
                self._data.popitem(last=False)
        self._data[urn] = label

    def put_bulk(self, mapping: Dict[str, str]) -> None:
        for urn, label in mapping.items():
            self.put(urn, label)


# ====================================================================
# Neo4j Provider
# ====================================================================

class Neo4jProvider(GraphDataProvider):
    """
    GraphDataProvider backed by a Neo4j database via the Bolt protocol.

    Supports any Neo4j database through configurable SchemaMapping that
    translates foreign property names to Synodic's canonical model.

    Configuration via ``extra_config``:
      - ``schemaMapping``: property name translations (see SchemaMapping)
      - ``maxConnectionPoolSize``: driver pool size (default 50)
      - ``connectionTimeout``: connection timeout in seconds (default 30)
      - ``redisUrl``: optional Redis for ancestor-chain caching
    """

    def __init__(
        self,
        uri: str,
        username: str = "neo4j",
        password: str = "",
        database: str = "neo4j",
        extra_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._uri = uri
        self._username = username
        self._password = password
        self._database = database
        self._extra_config = extra_config or {}

        self._driver = None
        self._lock = asyncio.Lock()
        self._redis = None
        self._redis_available = False
        self._redis_lock = asyncio.Lock()

        # Schema mapping
        self._mapping = SchemaMapping.from_extra_config(self._extra_config)

        # Caches
        self._stats_cache = _TTLCache(60.0)
        self._ontology_cache = _TTLCache(60.0)
        self._urn_cache = _URNLabelCache(50_000)

    @property
    def name(self) -> str:
        return "neo4j"

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def preflight(self, *, deadline_s: float = 1.5):
        """Fast reachability probe — TCP connect to the Bolt port within
        ``deadline_s``. Does NOT instantiate the driver, does NOT touch
        the connection pool. Returns a ``PreflightResult``; never raises
        for network failure.

        Bolt's full handshake involves a 4-byte magic preamble + version
        negotiation, but for P0 a TCP-open is sufficient: we only need
        to distinguish "host unreachable" (fail fast) from "host alive,
        defer to connect()".
        """
        from urllib.parse import urlparse

        from backend.common.interfaces.preflight import (
            PreflightResult,
            tcp_preflight,
        )

        try:
            parsed = urlparse(self._uri)
        except Exception as exc:
            return PreflightResult.failure(
                reason=f"invalid_uri: {exc}", elapsed_ms=0,
            )
        host = parsed.hostname
        port = parsed.port or 7687
        if not host:
            return PreflightResult.failure(reason="no_host_in_uri", elapsed_ms=0)
        return await tcp_preflight(host, port, deadline_s=deadline_s)

    async def _get_driver(self):
        """Double-checked locking lazy driver init."""
        if self._driver is not None:
            return self._driver
        async with self._lock:
            if self._driver is not None:
                return self._driver
            from neo4j import AsyncGraphDatabase
            pool_size = self._extra_config.get("maxConnectionPoolSize", 50)
            conn_timeout = self._extra_config.get("connectionTimeout", 30)
            self._driver = AsyncGraphDatabase.driver(
                self._uri,
                auth=(self._username, self._password),
                max_connection_pool_size=pool_size,
                connection_acquisition_timeout=conn_timeout,
            )
            logger.info("Neo4j driver created for %s (db=%s)", self._uri, self._database)
        return self._driver

    async def _ensure_redis(self):
        """Lazily create Redis connection if redisUrl is configured.

        Uses double-checked locking to prevent duplicate connections.
        """
        if self._redis is not None:
            return
        async with self._redis_lock:
            if self._redis is not None:
                return
            redis_url = self._extra_config.get("redisUrl")
            if not redis_url:
                return
            try:
                import redis.asyncio as aioredis
                from backend.common.adapters import TimeoutRedis
                _redis_op_timeout = float(os.getenv("NEO4J_REDIS_OP_TIMEOUT", "3"))
                _raw_redis = aioredis.from_url(redis_url, decode_responses=True)
                await _raw_redis.ping()
                self._redis = TimeoutRedis(_raw_redis, timeout=_redis_op_timeout)
                self._redis_available = True
                logger.info("Neo4j provider: Redis connected at %s", redis_url)
            except Exception as e:
                logger.warning("Neo4j provider: Redis unavailable (%s), using Cypher fallback", e)
                self._redis = None
                self._redis_available = False

    async def close(self) -> None:
        """Release driver and optional Redis connections."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
            self._redis_available = False
        if self._driver is not None:
            try:
                await self._driver.close()
            except Exception:
                pass
            self._driver = None
            logger.info("Neo4j driver closed for %s", self._uri)

    # ------------------------------------------------------------------ #
    # Query execution helpers                                              #
    # ------------------------------------------------------------------ #

    async def _run_read(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> list:
        """Execute a read transaction with automatic transient-error retry.

        Uses an async work function — ``AsyncManagedTransaction.run()``
        and ``AsyncResult.data()`` are both coroutines in the Neo4j 5.x
        async driver and must be awaited.
        """
        driver = await self._get_driver()
        async with driver.session(database=self._database) as session:
            try:
                async def _work(tx):
                    result = await tx.run(cypher, params or {})
                    return await result.data()
                return await session.execute_read(_work)
            except Exception as e:
                if type(e).__name__ in ("ServiceUnavailable", "SessionExpired"):
                    self._driver = None
                raise

    async def _run_write(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> list:
        """Execute a write transaction with automatic transient-error retry."""
        driver = await self._get_driver()
        async with driver.session(database=self._database) as session:
            try:
                async def _work(tx):
                    result = await tx.run(cypher, params or {})
                    return await result.data()
                return await session.execute_write(_work)
            except Exception as e:
                if type(e).__name__ in ("ServiceUnavailable", "SessionExpired"):
                    self._driver = None
                raise

    # ------------------------------------------------------------------ #
    # Node / edge property serialization                                   #
    # ------------------------------------------------------------------ #

    def _node_to_write_props(self, node: GraphNode) -> Dict[str, Any]:
        """Serialize a GraphNode to a dict using mapped property names.

        When writing to a foreign Neo4j database, the property names must
        match the schema mapping so that subsequent reads (which use the
        mapped names) find the data.
        """
        m = self._mapping
        props: Dict[str, Any] = {
            m.identity_field: node.urn,
            m.display_name_field: node.display_name or "",
            m.qualified_name_field: node.qualified_name or "",
            m.description_field: node.description or "",
            m.tags_field: json.dumps(node.tags or []),
            m.layer_field: node.layer_assignment or "",
            m.source_system_field: node.source_system or "",
            m.last_synced_field: node.last_synced_at or "",
        }
        if m.properties_field:
            props[m.properties_field] = json.dumps(node.properties)
        # When entity type is stored as a property (not derived from labels),
        # write it explicitly so reads can find it.
        if m.entity_type_strategy == "property" and m.entity_type_field:
            props[m.entity_type_field] = str(node.entity_type)
        # Hierarchy level — written when the engine has injected the mapping.
        # Omitted otherwise so SET n += props doesn't overwrite an existing
        # level with null. Backfill script covers nodes upserted before this.
        level = self._get_node_level(node.entity_type)
        if level is not None:
            props["level"] = level
        return props

    # ------------------------------------------------------------------ #
    # Node extraction from Neo4j records                                   #
    # ------------------------------------------------------------------ #

    def _extract_node_from_record(self, record_value) -> Optional[GraphNode]:
        """Build GraphNode from a Neo4j node object or dict using schema mapping."""
        if record_value is None:
            return None
        # neo4j.graph.Node has .labels and dict() gives properties
        if hasattr(record_value, "labels"):
            raw_props = dict(record_value)
            labels = sorted(record_value.labels)  # frozenset -> sorted list
            mapped = map_node_props(raw_props, labels, self._mapping)
            node = _node_from_props(mapped)
            if node:
                self._urn_cache.put(node.urn, str(node.entity_type))
            return node
        # Plain dict (from .data() results)
        if isinstance(record_value, dict):
            return _node_from_props(record_value)
        return None

    # ------------------------------------------------------------------ #
    # Containment edge type resolution                                     #
    # ------------------------------------------------------------------ #

    def set_containment_edge_types(self, types: List[str]) -> None:
        """Called by ContextEngine after ontology resolution.

        An empty list is valid — it means no containment types (flat graph).
        """
        self._resolved_containment_types: Set[str] = {t.upper() for t in types}
        self._resolved_containment_types_set = True  # sentinel: distinguishes "set to empty" from "never set"

    def set_entity_type_levels(self, mapping: Dict[str, int]) -> None:
        """Inject entity-type → hierarchy.level mapping. Used both at write
        time (writes ``n.level`` for the level index) and at read time
        (resolves levels via labels/entityType so trace queries work without
        requiring backfill_node_levels.py to have run on existing nodes).
        """
        self._entity_type_levels: Dict[str, int] = dict(mapping)

    def _get_node_level(self, entity_type: Any) -> Optional[int]:
        mapping = getattr(self, "_entity_type_levels", None)
        if not mapping:
            return None
        return mapping.get(str(entity_type))

    def _types_at_level(self, level: int) -> List[str]:
        """Entity-type IDs whose ontology hierarchy.level == ``level``.
        Drives the trace's WHERE clause without requiring ``n.level`` backfill.
        """
        mapping = getattr(self, "_entity_type_levels", None) or {}
        return [t for t, lvl in mapping.items() if lvl == level]

    def _entity_type_filter(self, var: str) -> str:
        """Render a Cypher predicate that matches a node's entity type
        against ``$types``. Honours the schema mapping: ``label`` strategy
        uses ``labels(var)[0]``; ``property`` strategy uses
        ``var.<entity_type_field>``. Caller passes ``$types`` in params.
        """
        if self._mapping.entity_type_strategy == "property":
            field = self._mapping.entity_type_field or "entityType"
            return f"{var}.`{field}` IN $types"
        return f"labels({var})[0] IN $types"

    def _get_containment_edge_types(self) -> Set[str]:
        """Return the authoritative containment edge type set.

        Resolution chain (first match wins):
        1. Ontology-resolved types injected by ContextEngine (may be empty = no hierarchy)
        2. CONTAINMENT_EDGE_TYPES env var
        3. Hardcoded fallback {CONTAINS, BELONGS_TO} — only used before ontology resolves
        """
        if getattr(self, "_resolved_containment_types_set", False):
            return self._resolved_containment_types
        if not hasattr(self, "_containment_cache"):
            config = os.getenv("CONTAINMENT_EDGE_TYPES", "").strip()
            if config:
                self._containment_cache = {t.strip().upper() for t in config.split(",") if t.strip()}
            else:
                self._containment_cache = {"CONTAINS", "BELONGS_TO"}
        return self._containment_cache

    # ------------------------------------------------------------------ #
    # Cypher helpers                                                       #
    # ------------------------------------------------------------------ #

    def _id_prop(self) -> str:
        """Return backtick-escaped identity field for use in Cypher map literals.

        Ensures correctness when the identity field contains special
        characters (e.g. ``node-id``, ``my.uuid``).
        """
        return f"`{self._mapping.identity_field}`"

    # ------------------------------------------------------------------ #
    # Index management                                                     #
    # ------------------------------------------------------------------ #

    async def ensure_indices(self, entity_type_ids: Optional[List[str]] = None):
        """Create named indexes IF NOT EXISTS for common lookup properties."""
        default_labels = [
            "domain", "dataPlatform",
            "container", "dataset",
            "schemaField",
        ]
        extra = list(entity_type_ids) if entity_type_ids else []
        seen: set = set()
        labels: list = []
        for lbl in default_labels + extra:
            if lbl not in seen:
                seen.add(lbl)
                labels.append(lbl)

        id_field = self._mapping.identity_field
        name_field = self._mapping.display_name_field
        qname_field = self._mapping.qualified_name_field
        # `level` indexed for trace queries that filter by hierarchy level.
        # Not subject to schema mapping — we own this property and write it
        # under the literal name `level` (see _node_to_write_props).
        properties = [id_field, name_field, qname_field, "level"]

        for label in labels:
            safe_label = _sanitize_label(label)
            for prop in properties:
                safe_prop = _sanitize_label(prop)
                idx_name = f"idx_{safe_label}_{safe_prop}"
                try:
                    await self._run_write(
                        f"CREATE INDEX {idx_name} IF NOT EXISTS FOR (n:`{safe_label}`) ON (n.`{safe_prop}`)"
                    )
                except Exception:
                    pass  # Index may already exist or label may not exist yet

    # ------------------------------------------------------------------ #
    # Filter matching (Python-side post-filters)                           #
    # ------------------------------------------------------------------ #

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

    def _match_tag_filters(self, node: GraphNode, tag_filter: TagFilter) -> bool:
        node_tags = set(node.tags or [])
        target_tags = set(tag_filter.tags)
        if tag_filter.mode == "any":
            return not node_tags.isdisjoint(target_tags)
        if tag_filter.mode == "all":
            return target_tags.issubset(node_tags)
        if tag_filter.mode == "none":
            return node_tags.isdisjoint(target_tags)
        return True

    def _match_text_filter(self, text: str, text_filter: TextFilter) -> bool:
        t = text if text_filter.case_sensitive else text.lower()
        q = text_filter.text if text_filter.case_sensitive else text_filter.text.lower()
        if text_filter.operator == "equals":
            return t == q
        if text_filter.operator == "contains":
            return q in t
        if text_filter.operator == "startsWith":
            return t.startswith(q)
        if text_filter.operator == "endsWith":
            return t.endswith(q)
        return True

    # ================================================================== #
    # Node Operations                                                      #
    # ================================================================== #

    async def get_node(self, urn: str) -> Optional[GraphNode]:
        ip = self._id_prop()

        # Label-aware lookup via cache (index-assisted, much faster)
        label = self._urn_cache.get(urn)
        if label:
            safe_label = _sanitize_label(label)
            rows = await self._run_read(
                f"MATCH (n:`{safe_label}` {{{ip}: $urn}}) RETURN n",
                {"urn": urn},
            )
            if rows:
                return self._extract_node_from_record(rows[0]["n"])

        # Fallback: label-less scan
        rows = await self._run_read(
            f"MATCH (n) WHERE n.{ip} = $urn RETURN n",
            {"urn": urn},
        )
        if rows:
            node = self._extract_node_from_record(rows[0]["n"])
            if node:
                self._urn_cache.put(urn, str(node.entity_type))
            return node
        return None

    async def get_nodes(self, query: NodeQuery) -> List[GraphNode]:
        ip = self._id_prop()
        name_field = self._mapping.display_name_field
        params: Dict[str, Any] = {}
        conditions: List[str] = []

        if query.entity_types:
            types_lower = [str(t).lower() for t in query.entity_types]
            params["entityTypesLower"] = types_lower
            if self._mapping.entity_type_strategy == "label":
                conditions.append("toLower(labels(n)[0]) IN $entityTypesLower")
            else:
                et_field = self._mapping.entity_type_field or "entityType"
                conditions.append(f"toLower(n.`{et_field}`) IN $entityTypesLower")

        if query.urns:
            if len(query.urns) == 1:
                conditions.append(f"n.{ip} = $urn0")
                params["urn0"] = query.urns[0]
            else:
                params["urnList"] = query.urns
                conditions.append(f"n.{ip} IN $urnList")

        if query.tags:
            tags_field = self._mapping.tags_field
            params["tagVal"] = json.dumps(query.tags[0])
            conditions.append(f"(n.`{tags_field}` IS NOT NULL AND n.`{tags_field}` CONTAINS $tagVal)")

        if query.search_query:
            params["search"] = query.search_query.lower()
            conditions.append(
                f"(toLower(toString(n.`{name_field}`)) CONTAINS $search "
                f"OR toLower(toString(n.{ip})) CONTAINS $search)"
            )

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = int(query.offset or 0)
        limit = query.limit or 100
        params["skip"] = offset
        params["limit"] = limit

        include_child_count = query.include_child_count

        if include_child_count:
            containment = list(self._get_containment_edge_types())
            if containment:
                containment_rel = "|".join(f"`{_sanitize_label(t)}`" for t in containment)
                cypher = (
                    f"MATCH (n) {where} "
                    f"WITH n SKIP $skip LIMIT $limit "
                    f"OPTIONAL MATCH (n)-[:{containment_rel}]->(child) "
                    f"RETURN n, count(child) as childCount"
                )
            else:
                # No containment types — childCount is always 0
                cypher = f"MATCH (n) {where} WITH n SKIP $skip LIMIT $limit RETURN n, 0 as childCount"
        else:
            cypher = f"MATCH (n) {where} RETURN n SKIP $skip LIMIT $limit"

        try:
            rows = await self._run_read(cypher, params)
        except Exception as e:
            logger.warning("get_nodes query failed: %s", e)
            return []

        nodes = []
        for row in rows:
            n = self._extract_node_from_record(row["n"])
            child_count = row.get("childCount") if include_child_count else None

            if not n:
                continue
            if query.property_filters and not self._match_property_filters(n, query.property_filters):
                continue
            if query.tag_filters and not self._match_tag_filters(n, query.tag_filters):
                continue
            if query.name_filter and not self._match_text_filter(n.display_name, query.name_filter):
                continue

            if child_count is not None:
                n.child_count = int(child_count)
                if n.properties:
                    n.properties["childCount"] = int(child_count)

            nodes.append(n)
            if len(nodes) >= limit:
                break
        return nodes

    async def search_nodes(self, query: str, limit: int = 10) -> List[GraphNode]:
        return await self.get_nodes(NodeQuery(search_query=query, limit=limit))

    # ================================================================== #
    # Edge Operations                                                      #
    # ================================================================== #

    async def get_edges(self, query: EdgeQuery) -> List[GraphEdge]:
        ip = self._id_prop()
        params: Dict[str, Any] = {}
        conditions: List[str] = []

        if query.source_urns:
            params["sourceUrns"] = query.source_urns
            conditions.append(f"a.{ip} IN $sourceUrns")
        if query.target_urns:
            params["targetUrns"] = query.target_urns
            conditions.append(f"b.{ip} IN $targetUrns")
        if query.any_urns:
            params["anyUrns"] = query.any_urns
            conditions.append(f"(a.{ip} IN $anyUrns OR b.{ip} IN $anyUrns)")
        if query.edge_types:
            types = [t.value if hasattr(t, "value") else str(t) for t in query.edge_types]
            params["edgeTypes"] = types
            conditions.append("type(r) IN $edgeTypes")
        if query.min_confidence is not None:
            conf_field = self._mapping.edge_confidence_field or "confidence"
            params["minConf"] = query.min_confidence
            conditions.append(f"r.`{conf_field}` >= $minConf")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = query.offset or 0
        limit = query.limit or 100
        params["skip"] = offset
        params["limit"] = limit

        cypher = (
            f"MATCH (a)-[r]->(b) {where} "
            f"RETURN a.{ip} AS src, b.{ip} AS tgt, "
            f"type(r) AS relType, properties(r) AS rprops "
            f"SKIP $skip LIMIT $limit"
        )

        try:
            rows = await self._run_read(cypher, params)
        except Exception as e:
            logger.warning("get_edges query failed: %s", e)
            return []

        edges = []
        for row in rows:
            edge_props = map_edge_props(row["rprops"] or {}, self._mapping)
            edges.append(_edge_from_row(row["src"], row["tgt"], row["relType"], edge_props))
        return edges

    # ================================================================== #
    # Containment Hierarchy                                                #
    # ================================================================== #

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
        ip = self._id_prop()
        name_field = self._mapping.display_name_field
        # None = caller didn't specify, use ontology/fallback; [] = explicitly no containment
        target_edge_types = list(set(edge_types) if edge_types is not None else self._get_containment_edge_types())
        if not target_edge_types:
            # No containment types — flat graph, no children
            return []
        params: Dict[str, Any] = {
            "parent": parent_urn, "skip": offset, "lim": limit,
            "relTypes": target_edge_types,
        }

        search_where = ""
        if search_query:
            search_where = (
                f"AND (toLower(c.`{name_field}`) CONTAINS toLower($searchQuery) "
                f"OR toLower(c.{ip}) CONTAINS toLower($searchQuery)) "
            )
            params["searchQuery"] = search_query

        if len(target_edge_types) == 1:
            rel = _sanitize_label(target_edge_types[0])
            cypher = (
                f"MATCH (p)-[r:`{rel}`]->(c) "
                f"WHERE p.{ip} = $parent {search_where}"
                f"WITH c SKIP $skip LIMIT $lim "
                f"OPTIONAL MATCH (c)-[rc]->(gc) WHERE type(rc) IN $relTypes "
                f"RETURN c, count(gc) as childCount"
            )
        else:
            cypher = (
                f"MATCH (p)-[r]->(c) "
                f"WHERE p.{ip} = $parent AND type(r) IN $relTypes {search_where}"
                f"WITH c SKIP $skip LIMIT $lim "
                f"OPTIONAL MATCH (c)-[rc]->(gc) WHERE type(rc) IN $relTypes "
                f"RETURN c, count(gc) as childCount"
            )

        try:
            rows = await self._run_read(cypher, params)
        except Exception as e:
            logger.warning("get_children query failed: %s", e)
            return []

        nodes = []
        for row in rows:
            n = self._extract_node_from_record(row["c"])
            child_count = row["childCount"]
            if n and (not entity_types or n.entity_type in entity_types):
                if child_count is not None:
                    n.child_count = int(child_count)
                    if n.properties:
                        n.properties["childCount"] = int(child_count)
                nodes.append(n)
        nodes.sort(key=lambda x: x.display_name)
        return nodes

    async def get_parent(self, child_urn: str) -> Optional[GraphNode]:
        ip = self._id_prop()
        containment = list(self._get_containment_edge_types())
        if not containment:
            # No containment types — flat graph, no parent
            return None
        rows = await self._run_read(
            f"MATCH (p)-[r]->(c) WHERE c.{ip} = $child AND type(r) IN $ctypes RETURN p",
            {"child": child_urn, "ctypes": containment},
        )
        if rows:
            return self._extract_node_from_record(rows[0]["p"])
        return None

    # ================================================================== #
    # Lineage Traversal                                                    #
    # ================================================================== #

    async def _traverse_lineage(
        self,
        start_urn: str,
        direction: str,
        depth: int,
        descendant_types: Optional[List[str]] = None,
    ) -> Set[str]:
        """Bounded variable-length Cypher paths excluding containment edges."""
        ip = self._id_prop()
        containment = list(self._get_containment_edge_types())
        safe_depth = max(1, min(int(depth), 20))
        params: Dict[str, Any] = {
            "startUrn": start_urn,
            "containmentTypes": containment,
        }

        type_clause = ""
        if descendant_types:
            allowed = [t.value if hasattr(t, "value") else str(t) for t in descendant_types]
            params["allowedTypes"] = [a.lower() for a in allowed]
            if self._mapping.entity_type_strategy == "label":
                type_clause = "AND toLower(labels(neighbor)[0]) IN $allowedTypes "
            else:
                et_field = self._mapping.entity_type_field or "entityType"
                type_clause = f"AND toLower(neighbor.`{et_field}`) IN $allowedTypes "

        if direction == "upstream":
            cypher = (
                f"MATCH (start) WHERE start.{ip} = $startUrn "
                f"MATCH path = (neighbor)-[*1..{safe_depth}]->(start) "
                f"WHERE ALL(r IN relationships(path) WHERE NOT type(r) IN $containmentTypes) "
                f"{type_clause}"
                f"RETURN DISTINCT neighbor.{ip} AS urn"
            )
        else:
            cypher = (
                f"MATCH (start) WHERE start.{ip} = $startUrn "
                f"MATCH path = (start)-[*1..{safe_depth}]->(neighbor) "
                f"WHERE ALL(r IN relationships(path) WHERE NOT type(r) IN $containmentTypes) "
                f"{type_clause}"
                f"RETURN DISTINCT neighbor.{ip} AS urn"
            )

        try:
            rows = await self._run_read(cypher, params)
        except Exception as e:
            logger.warning("_traverse_lineage failed: %s", e)
            return set()

        return {row["urn"] for row in rows if row["urn"] and row["urn"] != start_urn}

    async def _build_lineage_result(
        self,
        center_urn: str,
        upstream_urns: Set[str],
        downstream_urns: Set[str],
    ) -> LineageResult:
        """Shared helper: fetch nodes + edges for a set of lineage URNs,
        filter to the connected subgraph, and return a LineageResult."""
        all_urns = upstream_urns | downstream_urns | {center_urn}
        nodes = await self.get_nodes(
            NodeQuery(urns=list(all_urns), limit=len(all_urns), include_child_count=False)
        )
        node_ids = {n.urn for n in nodes}
        edges = await self.get_edges(
            EdgeQuery(any_urns=list(all_urns), limit=len(all_urns) * 10)
        )
        edges = [e for e in edges if e.source_urn in node_ids and e.target_urn in node_ids]
        return LineageResult(
            nodes=nodes, edges=edges,
            upstreamUrns=upstream_urns, downstreamUrns=downstream_urns,
            totalCount=len(nodes), hasMore=False,
        )

    async def get_upstream(
        self, urn: str, depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        upstream_urns = await self._traverse_lineage(urn, "upstream", depth, descendant_types)
        return await self._build_lineage_result(urn, upstream_urns, set())

    async def get_downstream(
        self, urn: str, depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        downstream_urns = await self._traverse_lineage(urn, "downstream", depth, descendant_types)
        return await self._build_lineage_result(urn, set(), downstream_urns)

    async def get_full_lineage(
        self, urn: str, upstream_depth: int, downstream_depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        # Run upstream and downstream traversals concurrently
        up, down = await asyncio.gather(
            self._traverse_lineage(urn, "upstream", upstream_depth, descendant_types),
            self._traverse_lineage(urn, "downstream", downstream_depth, descendant_types),
        )
        return await self._build_lineage_result(urn, up, down)

    async def get_trace_lineage(
        self, urn: str, direction: str, depth: int,
        containment_edges: List[str], lineage_edges: List[str],
    ) -> LineageResult:
        """Batched BFS trace lineage: one Cypher per depth level.

        1. Expand scope: target + children via containment
        2. BFS across lineage edges per depth level
        3. Climb containment for structural context
        """
        ip = self._id_prop()
        safe_containment = [_sanitize_label(t) for t in containment_edges]
        safe_lineage = [_sanitize_label(t) for t in lineage_edges]

        if not safe_lineage:
            node = await self.get_node(urn)
            return LineageResult(
                nodes=[node] if node else [], edges=[],
                upstreamUrns=set(), downstreamUrns=set(),
                totalCount=1 if node else 0, hasMore=False,
            )

        # 1. Expand: target + children
        start_urns = {urn}
        if safe_containment:
            rows = await self._run_read(
                f"MATCH (p)-[r]->(c) "
                f"WHERE p.{ip} = $urn AND type(r) IN $containment "
                f"RETURN c.{ip} AS curn",
                {"urn": urn, "containment": safe_containment},
            )
            for row in rows:
                if row["curn"]:
                    start_urns.add(row["curn"])

        collected_nodes: Dict[str, GraphNode] = {}
        collected_edges: Dict[str, GraphEdge] = {}
        upstream_urns: Set[str] = set()
        downstream_urns: Set[str] = set()

        # 2. Batched BFS across lineage edges
        visited_lineage = set(start_urns)
        current_frontier = list(start_urns)

        for _ in range(depth):
            if not current_frontier:
                break

            next_frontier_up: List[str] = []
            next_frontier_down: List[str] = []

            dir_queries = []
            if direction in ("upstream", "both"):
                dir_queries.append((
                    "upstream",
                    f"MATCH (src)-[r]->(tgt) "
                    f"WHERE tgt.{ip} IN $frontier AND type(r) IN $lineage "
                    f"RETURN src, r, tgt",
                ))
            if direction in ("downstream", "both"):
                dir_queries.append((
                    "downstream",
                    f"MATCH (src)-[r]->(tgt) "
                    f"WHERE src.{ip} IN $frontier AND type(r) IN $lineage "
                    f"RETURN src, r, tgt",
                ))

            for dir_label, cypher_q in dir_queries:
                try:
                    rows = await self._run_read(
                        cypher_q,
                        {"frontier": current_frontier, "lineage": safe_lineage},
                    )
                except Exception as e:
                    logger.warning("Trace lineage BFS query failed: %s", e)
                    continue

                for row in rows:
                    src_node = self._extract_node_from_record(row["src"])
                    tgt_node = self._extract_node_from_record(row["tgt"])
                    if not src_node or not tgt_node:
                        continue

                    rel = row["r"]
                    r_type = rel.type if hasattr(rel, "type") else "RELATED_TO"
                    r_props = dict(rel) if hasattr(rel, "items") else {}
                    edge_props = map_edge_props(r_props, self._mapping)
                    edge = _edge_from_row(src_node.urn, tgt_node.urn, r_type, edge_props)

                    if edge.id not in collected_edges:
                        collected_edges[edge.id] = edge
                        collected_nodes[src_node.urn] = src_node
                        collected_nodes[tgt_node.urn] = tgt_node

                        if dir_label == "upstream":
                            if src_node.urn not in visited_lineage:
                                visited_lineage.add(src_node.urn)
                                upstream_urns.add(src_node.urn)
                                next_frontier_up.append(src_node.urn)
                        else:
                            if tgt_node.urn not in visited_lineage:
                                visited_lineage.add(tgt_node.urn)
                                downstream_urns.add(tgt_node.urn)
                                next_frontier_down.append(tgt_node.urn)

            current_frontier = next_frontier_up + next_frontier_down

        # 3. Structural context: climb containment
        all_lineage_urns = list(collected_nodes.keys())
        if all_lineage_urns and safe_containment:
            cypher_structure = (
                f"MATCH (parent)-[r]->(child) "
                f"WHERE child.{ip} IN $urns AND type(r) IN $containment "
                f"RETURN parent, r, child"
            )
            current_level_urns = all_lineage_urns
            seen_parents: Set[str] = set(collected_nodes.keys())

            for _ in range(5):
                if not current_level_urns:
                    break
                try:
                    rows = await self._run_read(
                        cypher_structure,
                        {"urns": current_level_urns, "containment": safe_containment},
                    )
                except Exception:
                    break

                next_level_urns = []
                for row in rows:
                    parent = self._extract_node_from_record(row["parent"])
                    child = self._extract_node_from_record(row["child"])
                    if parent and child:
                        collected_nodes[child.urn] = child
                        rel = row["r"]
                        r_type = rel.type if hasattr(rel, "type") else "CONTAINS"
                        r_props = dict(rel) if hasattr(rel, "items") else {}
                        edge_props = map_edge_props(r_props, self._mapping)
                        edge = _edge_from_row(parent.urn, child.urn, r_type, edge_props)
                        collected_edges[edge.id] = edge

                        if parent.urn not in seen_parents:
                            seen_parents.add(parent.urn)
                            collected_nodes[parent.urn] = parent
                            next_level_urns.append(parent.urn)

                current_level_urns = next_level_urns

        # Ensure origin node is present
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
            hasMore=False,
        )

    async def get_aggregated_edges_between(
        self, source_urns: List[str], target_urns: Optional[List[str]],
        granularity: Any, containment_edges: List[str], lineage_edges: List[str],
        *, timeout: Optional[float] = None,
    ) -> AggregatedEdgeResult:
        """Read pre-materialized AGGREGATED edges."""
        ip = self._id_prop()

        if target_urns:
            cypher = (
                f"MATCH (s)-[r:AGGREGATED]->(t) "
                f"WHERE s.{ip} IN $sourceUrns AND t.{ip} IN $targetUrns "
                f"AND s.{ip} <> t.{ip} "
                f"RETURN s.{ip} AS sUrn, t.{ip} AS tUrn, "
                f"r.weight AS weight, r.sourceEdgeTypes AS types "
                f"ORDER BY r.weight DESC"
            )
            params: Dict[str, Any] = {"sourceUrns": source_urns, "targetUrns": target_urns}
        else:
            cypher = (
                f"MATCH (s)-[r:AGGREGATED]->(t) "
                f"WHERE s.{ip} IN $sourceUrns "
                f"AND s.{ip} <> t.{ip} "
                f"RETURN s.{ip} AS sUrn, t.{ip} AS tUrn, "
                f"r.weight AS weight, r.sourceEdgeTypes AS types "
                f"ORDER BY r.weight DESC"
            )
            params = {"sourceUrns": source_urns}

        try:
            rows = await self._run_read(cypher, params)
        except Exception as e:
            logger.warning("AGGREGATED edge read failed: %s", e)
            rows = []

        return self._rows_to_aggregated_result(rows)

    def _rows_to_aggregated_result(self, rows: list) -> AggregatedEdgeResult:
        aggregated = []
        total_edges = 0
        for row in rows:
            w = int(row["weight"]) if row.get("weight") else 1
            types = row.get("types")
            edge_types = types if isinstance(types, list) else [str(types)] if types else []
            aggregated.append(AggregatedEdgeInfo(
                id=f"agg-{row['sUrn']}-{row['tUrn']}",
                sourceUrn=row["sUrn"],
                targetUrn=row["tUrn"],
                edgeCount=w,
                edgeTypes=edge_types,
                confidence=1.0,
                sourceEdgeIds=[],
            ))
            total_edges += w
        return AggregatedEdgeResult(aggregatedEdges=aggregated, totalSourceEdges=total_edges)

    # ================================================================== #
    # Trace v2 — Cypher-native, ontology-aware lineage                   #
    #                                                                    #
    # Filters AGGREGATED edges by node-level (s.level/t.level) at the   #
    # database layer. Per-hop set-based BFS orchestrated in Python — the #
    # hot path is a single UNWIND $frontier MATCH per hop, capped by    #
    # LIMIT. Cost is proportional to result size, not graph size.       #
    # ================================================================== #

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
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        ctypes = [t.upper() for t in (containment_edge_types or [])]
        ltypes = [t.upper() for t in (lineage_edge_types or [])] if lineage_edge_types else None

        focus_node = await self.get_node(urn)
        focus_level = self._get_node_level(focus_node.entity_type) if focus_node else level
        focus_entity_type = str(focus_node.entity_type) if focus_node else "unknown"

        anchor_urn = await self._resolve_anchor_at_level(urn, level, ctypes)

        is_inherited = False
        inherited_from = None
        if include_inherited_lineage and not await self._has_aggregated_at_level(anchor_urn, level):
            parent = await self._find_ancestor_with_lineage(anchor_urn, level, ctypes)
            if parent and parent != anchor_urn:
                inherited_from = anchor_urn
                anchor_urn = parent
                is_inherited = True

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

        for hop in range(max(upstream_depth, downstream_depth)):
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
                        edges_by_id[edge_id] = GraphEdge(
                            id=edge_id,
                            sourceUrn=rec["sourceUrn"],
                            targetUrn=rec["targetUrn"],
                            edgeType="AGGREGATED",
                            properties={
                                "sourceEdgeTypes": rec.get("edgeTypes") or [],
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

        # Always hydrate the containment chain (ancestor nodes + parent-child
        # edges) so the canvas can position trace nodes in the layered
        # hierarchy. Hierarchy context is non-optional for trace responses;
        # the `include_containment_edges` flag is intentionally ignored. See
        # the FalkorDB implementation for the rationale.
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
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        ctypes = [t.upper() for t in (containment_edge_types or [])]
        ltypes = [t.upper() for t in (lineage_edge_types or [])] if lineage_edge_types else None

        s_task = self._collect_descendants_at_level(source_urn, next_level, ctypes, max_nodes)
        t_task = self._collect_descendants_at_level(target_urn, next_level, ctypes, max_nodes)
        s_urns, t_urns = await asyncio.gather(s_task, t_task)

        truncation_reason: Optional[str] = None
        if time.monotonic() > deadline:
            truncation_reason = "timeout"

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

        all_urns = (set(s_urns) | set(t_urns)) & node_urns_in_edges if edges else (set(s_urns) | set(t_urns))
        if len(all_urns) > max_nodes:
            in_edges = list(node_urns_in_edges)[:max_nodes]
            all_urns = set(in_edges)
            truncation_reason = truncation_reason or "max_nodes"

        nodes = await self.get_nodes_batch(list(all_urns)) if all_urns else []
        nodes_by_urn = {n.urn: n for n in nodes if n}

        # Always hydrate ancestors + containment edges so drilled-into nodes
        # have hierarchy context in the canvas. See trace_at_level rationale.
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

        anchor_node = nodes_by_urn.get(source_urn) or await self.get_node(source_urn)
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
        """Walk UP containment to the nearest ancestor at ``level``. Filters
        by entity-type (label or property per schema mapping) so the trace
        works without ``n.level`` backfill having run.
        """
        ip = self._id_prop()
        if not ctypes:
            return urn
        types = self._types_at_level(level)
        if not types:
            return urn
        cypher = (
            f"MATCH (focus {{{ip}: $urn}}) "
            f"OPTIONAL MATCH path = (focus)<-[c*0..10]-(anc) "
            f"WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            f"  AND {self._entity_type_filter('anc')} "
            f"RETURN coalesce(anc.{ip}, focus.{ip}) AS anchorUrn "
            f"ORDER BY length(path) ASC LIMIT 1"
        )
        try:
            rows = await self._run_read(
                cypher, {"urn": urn, "ctypes": ctypes, "types": types},
            )
            if rows:
                value = rows[0].get("anchorUrn") if isinstance(rows[0], dict) else None
                return value or urn
        except Exception as exc:
            logger.warning("trace_at_level: anchor resolution failed for %s: %s", urn, exc)
        return urn

    async def _has_aggregated_at_level(self, anchor_urn: str, level: int) -> bool:
        types = self._types_at_level(level)
        if not types:
            return True  # fail-open: skip the inherited-lineage fallback
        ip = self._id_prop()
        cypher = (
            f"MATCH (a {{{ip}: $anchor}})-[r:AGGREGATED]-(peer) "
            f"WHERE {self._entity_type_filter('peer')} "
            f"RETURN 1 LIMIT 1"
        )
        try:
            rows = await self._run_read(cypher, {"anchor": anchor_urn, "types": types})
            return bool(rows)
        except Exception as exc:
            logger.warning("trace_at_level: has-aggregated check failed for %s: %s", anchor_urn, exc)
            return True

    async def _find_ancestor_with_lineage(
        self, anchor_urn: str, level: int, ctypes: List[str],
    ) -> Optional[str]:
        if not ctypes:
            return None
        types = self._types_at_level(level)
        if not types:
            return None
        ip = self._id_prop()
        cypher = (
            f"MATCH (a {{{ip}: $anchor}})<-[c*1..10]-(parent) "
            f"WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            f"  AND {self._entity_type_filter('parent')} "
            f"RETURN parent.{ip} AS urn, length(c) AS depth "
            f"ORDER BY length(c) ASC LIMIT 5"
        )
        try:
            rows = await self._run_read(
                cypher, {"anchor": anchor_urn, "ctypes": ctypes, "types": types},
            )
            for row in rows:
                candidate = row.get("urn") if isinstance(row, dict) else None
                if candidate and await self._has_aggregated_at_level(candidate, level):
                    return candidate
        except Exception as exc:
            logger.warning("trace_at_level: find-ancestor-with-lineage failed for %s: %s", anchor_urn, exc)
        return None

    async def _expand_aggregated_set(
        self, frontier: List[str], direction: str, level: int,
        ltypes: Optional[List[str]], limit: int,
    ) -> List[Dict[str, Any]]:
        """Per-hop expansion. Filters by entity-type (label or property per
        schema mapping) so the trace works without ``n.level`` backfill.
        Drops the type filter when the ontology has no types at this level
        — degrades to "all AGGREGATED neighbours" rather than empty.
        """
        if not frontier or limit <= 0:
            return []
        ip = self._id_prop()

        types = self._types_at_level(level)
        type_filter = ""
        if types:
            other = "s" if direction == "incoming" else "t"
            type_filter = f"AND {self._entity_type_filter(other)} "
        ltype_filter = "AND any(et IN r.sourceEdgeTypes WHERE et IN $ltypes) " if ltypes else ""

        if direction == "incoming":
            cypher = (
                f"UNWIND $frontier AS srcUrn "
                f"MATCH (s)-[r:AGGREGATED]->(t) "
                f"WHERE t.{ip} = srcUrn "
                + type_filter
                + ltype_filter
                + f"RETURN s.{ip} AS sourceUrn, t.{ip} AS targetUrn, "
                f"id(r) AS edgeId, r.sourceEdgeTypes AS edgeTypes, "
                f"r.weight AS weight, s AS otherNode "
                f"LIMIT $limit"
            )
        else:
            cypher = (
                f"UNWIND $frontier AS srcUrn "
                f"MATCH (s)-[r:AGGREGATED]->(t) "
                f"WHERE s.{ip} = srcUrn "
                + type_filter
                + ltype_filter
                + f"RETURN s.{ip} AS sourceUrn, t.{ip} AS targetUrn, "
                f"id(r) AS edgeId, r.sourceEdgeTypes AS edgeTypes, "
                f"r.weight AS weight, t AS otherNode "
                f"LIMIT $limit"
            )

        params: Dict[str, Any] = {"frontier": frontier, "limit": limit}
        if types:
            params["types"] = types
        if ltypes:
            params["ltypes"] = ltypes

        try:
            rows = await self._run_read(cypher, params)
        except Exception as exc:
            logger.warning("trace_at_level: expand (%s) failed: %s", direction, exc)
            return []

        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                rec = {
                    "sourceUrn": row.get("sourceUrn"),
                    "targetUrn": row.get("targetUrn"),
                    "edgeId": str(row.get("edgeId")) if row.get("edgeId") is not None
                              else f"agg-{row.get('sourceUrn')}-{row.get('targetUrn')}",
                    "edgeTypes": row.get("edgeTypes") if isinstance(row.get("edgeTypes"), list)
                                 else ([row.get("edgeTypes")] if row.get("edgeTypes") else []),
                    "weight": int(row.get("weight")) if row.get("weight") is not None else 1,
                    "node": self._extract_node_from_record(row.get("otherNode"))
                            if row.get("otherNode") is not None else None,
                }
                out.append(rec)
            except Exception:
                continue
        return out

    async def _collect_ancestor_urns(
        self, urns: List[str], ctypes: List[str],
    ) -> List[str]:
        """Collect ALL containment ancestors of the given URNs in one query.

        Foundational for trace responses: a trace returns lineage URNs at the
        requested level (often columns), but the canvas needs the full
        ancestor chain (Dataset → Container → Domain) to position those URNs
        in the layered hierarchy. Without this, trace nodes become orphans
        and never reach the rendered tree.
        """
        if not urns or not ctypes:
            return []
        ip = self._id_prop()
        cypher = (
            f"UNWIND $urns AS u "
            f"MATCH (n {{{ip}: u}})<-[c*1..10]-(ancestor) "
            f"WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
            f"RETURN DISTINCT ancestor.{ip} AS ancestorUrn"
        )
        try:
            rows = await self._run_read(cypher, {"urns": urns, "ctypes": ctypes})
            out: List[str] = []
            for row in rows:
                if isinstance(row, dict):
                    val = row.get("ancestorUrn")
                    if val:
                        out.append(val)
            return out
        except Exception as exc:
            logger.warning("trace_at_level: ancestor collection failed for %d urns: %s", len(urns), exc)
            return []

    async def _collect_descendants_at_level(
        self, anchor_urn: str, target_level: int, ctypes: List[str], limit: int,
    ) -> List[str]:
        """Descendants of ``anchor_urn`` whose entity type sits at
        ``target_level``. Filters by labels/entityType — no ``n.level``
        dependency.
        """
        ip = self._id_prop()
        types = self._types_at_level(target_level)
        if not types:
            return []

        if not ctypes:
            cypher = (
                f"MATCH (a {{{ip}: $anchor}}) WHERE {self._entity_type_filter('a')} "
                f"RETURN [a.{ip}] AS urns"
            )
            params = {"anchor": anchor_urn, "types": types}
        else:
            cypher = (
                f"MATCH (a {{{ip}: $anchor}})-[c*0..10]->(child) "
                f"WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
                f"  AND {self._entity_type_filter('child')} "
                f"RETURN collect(DISTINCT child.{ip})[..$limit] AS urns"
            )
            params = {"anchor": anchor_urn, "ctypes": ctypes, "types": types, "limit": limit}
        try:
            rows = await self._run_read(cypher, params)
            if rows and isinstance(rows[0], dict):
                value = rows[0].get("urns")
                if isinstance(value, list):
                    return [u for u in value if u]
        except Exception as exc:
            logger.warning("trace_at_level: descendant collection failed for %s: %s", anchor_urn, exc)
        return []

    async def _edges_between_sets(
        self, s_urns: List[str], t_urns: List[str], level: int,
        ltypes: Optional[List[str]], use_raw: bool, limit: int,
    ) -> List[GraphEdge]:
        if not s_urns or not t_urns:
            return []
        ip = self._id_prop()

        if use_raw:
            ltypes_eff = ltypes or []
            if not ltypes_eff:
                return []
            cypher = (
                f"UNWIND $sUrns AS srcUrn "
                f"MATCH (s {{{ip}: srcUrn}})-[r]->(t) "
                f"WHERE t.{ip} IN $tUrns AND type(r) IN $ltypes "
                f"RETURN s.{ip} AS sUrn, t.{ip} AS tUrn, type(r) AS edgeType, "
                f"id(r) AS edgeId, properties(r) AS props "
                f"LIMIT $limit"
            )
            params = {"sUrns": s_urns, "tUrns": t_urns, "ltypes": ltypes_eff, "limit": limit}
        else:
            type_filter = "AND any(et IN r.sourceEdgeTypes WHERE et IN $ltypes) " if ltypes else ""
            cypher = (
                f"UNWIND $sUrns AS srcUrn "
                f"MATCH (s {{{ip}: srcUrn}})-[r:AGGREGATED]->(t) "
                f"WHERE t.{ip} IN $tUrns " + type_filter
                + f"RETURN s.{ip} AS sUrn, t.{ip} AS tUrn, 'AGGREGATED' AS edgeType, "
                f"id(r) AS edgeId, "
                f"{{sourceEdgeTypes: r.sourceEdgeTypes, weight: r.weight}} AS props "
                f"LIMIT $limit"
            )
            params = {"sUrns": s_urns, "tUrns": t_urns, "limit": limit}
            if ltypes:
                params["ltypes"] = ltypes

        try:
            rows = await self._run_read(cypher, params)
        except Exception as exc:
            logger.warning("expand_aggregated: edge fetch failed: %s", exc)
            return []

        out: List[GraphEdge] = []
        seen: Set[str] = set()
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                edge_id = str(row.get("edgeId")) if row.get("edgeId") is not None \
                          else f"{row.get('edgeType')}-{row.get('sUrn')}-{row.get('tUrn')}"
                if edge_id in seen:
                    continue
                seen.add(edge_id)
                props = row.get("props") if isinstance(row.get("props"), dict) else {}
                out.append(GraphEdge(
                    id=edge_id,
                    sourceUrn=row.get("sUrn"),
                    targetUrn=row.get("tUrn"),
                    edgeType=str(row.get("edgeType")),
                    properties=props or {},
                ))
            except Exception:
                continue
        return out

    async def _fetch_containment_edges(
        self, urns: List[str], ctypes: List[str],
    ) -> List[GraphEdge]:
        if not urns or not ctypes:
            return []
        ip = self._id_prop()
        cypher = (
            f"UNWIND $urns AS u "
            f"MATCH (s {{{ip}: u}})-[r]->(t) "
            f"WHERE t.{ip} IN $urns AND type(r) IN $ctypes "
            f"RETURN s.{ip} AS sUrn, t.{ip} AS tUrn, type(r) AS edgeType, "
            f"id(r) AS edgeId"
        )
        try:
            rows = await self._run_read(cypher, {"urns": urns, "ctypes": ctypes})
        except Exception as exc:
            logger.warning("trace_at_level: containment edge fetch failed: %s", exc)
            return []
        out: List[GraphEdge] = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                out.append(GraphEdge(
                    id=str(row.get("edgeId")),
                    sourceUrn=row.get("sUrn"),
                    targetUrn=row.get("tUrn"),
                    edgeType=str(row.get("edgeType")),
                    properties={},
                ))
            except Exception:
                continue
        return out

    async def get_nodes_batch(self, urns: List[str]) -> List[GraphNode]:
        if not urns:
            return []
        ip = self._id_prop()
        try:
            rows = await self._run_read(
                f"MATCH (n) WHERE n.{ip} IN $urns RETURN n",
                {"urns": urns},
            )
            out: List[GraphNode] = []
            for row in rows:
                if isinstance(row, dict):
                    n = self._extract_node_from_record(row.get("n"))
                    if n:
                        out.append(n)
            return out
        except Exception as exc:
            logger.warning("get_nodes_batch failed: %s", exc)
            return []

    # ================================================================== #
    # Metadata Operations                                                  #
    # ================================================================== #

    async def get_stats(self) -> Dict[str, Any]:
        cached = self._stats_cache.get()
        if cached:
            return cached

        type_res = await self._run_read(
            "MATCH (n) RETURN labels(n)[0] AS lbl, count(*) AS c"
        )
        entity_type_counts = {}
        node_count = 0
        for row in type_res:
            lbl = row["lbl"] or "unknown"
            cnt = row["c"]
            entity_type_counts[lbl] = cnt
            node_count += cnt

        edge_type_res = await self._run_read(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c"
        )
        edge_type_counts = {}
        edge_count = 0
        for row in edge_type_res:
            t = row["t"] or "UNKNOWN"
            cnt = row["c"]
            edge_type_counts[t] = cnt
            edge_count += cnt

        result = {
            "provider": "neo4j",
            "database": self._database,
            "nodeCount": node_count,
            "edgeCount": edge_count,
            "entityTypeCounts": entity_type_counts,
            "edgeTypeCounts": edge_type_counts,
        }
        self._stats_cache.set(result)
        return result

    async def get_schema_stats(self) -> GraphSchemaStats:
        name_field = self._mapping.display_name_field
        tags_field = self._mapping.tags_field

        type_res = await self._run_read(
            f"MATCH (n) "
            f"WITH labels(n)[0] AS lbl, n.`{name_field}` AS name "
            f"WITH lbl, count(*) AS c, collect(name)[0..3] AS samples "
            f"RETURN lbl, c, samples"
        )

        entity_stats = []
        total_nodes = 0
        for row in type_res:
            lbl = row["lbl"] or "unknown"
            cnt = row["c"]
            samples = [s for s in (row["samples"] or []) if s]
            total_nodes += cnt
            entity_stats.append(EntityTypeSummary(id=lbl, name=lbl, count=cnt, sampleNames=samples))

        edge_type_res = await self._run_read(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c"
        )
        edge_stats = []
        total_edges = 0
        for row in edge_type_res:
            t = row["t"] or "UNKNOWN"
            cnt = row["c"]
            edge_stats.append(EdgeTypeSummary(id=t, name=t, count=cnt))
            total_edges += cnt

        # Tag stats
        try:
            tag_res = await self._run_read(
                f"MATCH (n) WHERE n.`{tags_field}` IS NOT NULL AND n.`{tags_field}` <> '[]' "
                f"RETURN n.`{tags_field}` AS tags"
            )
            tag_counts: Dict[str, int] = {}
            tag_types: Dict[str, Set[str]] = {}
            for row in tag_res:
                tags_raw = row["tags"]
                try:
                    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
                except Exception:
                    continue
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if tag not in tag_types:
                        tag_types[tag] = set()
                    tag_types[tag].add("entity")
            tag_stats = [
                TagSummary(tag=t, count=c, entityTypes=list(tag_types.get(t, {"entity"})))
                for t, c in tag_counts.items()
            ]
        except Exception as e:
            logger.warning("Failed to fetch tag stats: %s", e)
            tag_stats = []

        return GraphSchemaStats(
            totalNodes=total_nodes, totalEdges=total_edges,
            entityTypeStats=entity_stats, edgeTypeStats=edge_stats, tagStats=tag_stats,
        )

    async def get_ontology_metadata(self) -> OntologyMetadata:
        """Dynamic introspection — queries actual relationship types and builds
        ontology metadata from the live database."""
        cached = self._ontology_cache.get()
        if cached:
            return cached

        containment = list(self._get_containment_edge_types())
        containment_upper = {t.upper() for t in containment}

        # 1. Distinct relationship types
        type_res = await self._run_read("MATCH ()-[r]->() RETURN DISTINCT type(r) AS t")
        all_types = [row["t"] for row in type_res]

        config_lineage = os.getenv("LINEAGE_EDGE_TYPES", "").strip()
        if config_lineage:
            lineage_types = [t.strip() for t in config_lineage.split(",") if t.strip()]
        else:
            config_metadata = os.getenv("METADATA_EDGE_TYPES", "").strip()
            metadata_types = (
                {t.strip().upper() for t in config_metadata.split(",") if t.strip()}
                if config_metadata else {"TAGGED_WITH"}
            )
            lineage_types = [
                t for t in all_types
                if t.upper() not in containment_upper
                and t.upper() not in metadata_types
                and t.upper() != "AGGREGATED"
            ]

        lineage_upper = {t.upper() for t in lineage_types}

        # 2. Edge metadata
        edge_type_metadata: Dict[str, EdgeTypeMetadata] = {}
        for et in all_types:
            is_containment = et.upper() in containment_upper
            is_lineage = et.upper() in lineage_upper
            if is_containment:
                category = "structural"
                direction = "child-to-parent" if et.upper() == "BELONGS_TO" else "parent-to-child"
            elif is_lineage:
                category = "flow"
                direction = "source-to-target"
            elif et.upper() == "TAGGED_WITH":
                category = "metadata"
                direction = "bidirectional"
            else:
                category = "association"
                direction = "bidirectional"
            edge_type_metadata[et] = EdgeTypeMetadata(
                isContainment=is_containment, isLineage=is_lineage,
                direction=direction, category=category,
                description=f"{category} relationship: {et}",
            )

        # 3. Entity hierarchy from containment edges
        hierarchy_res = await self._run_read(
            "MATCH (p)-[r]->(c) WHERE type(r) IN $containment "
            "RETURN DISTINCT labels(p)[0] AS pType, labels(c)[0] AS cType, type(r) AS rType",
            {"containment": containment},
        )

        entity_type_hierarchy: Dict[str, EntityTypeHierarchy] = {}
        found_parent_types: Set[str] = set()
        found_child_types: Set[str] = set()

        for row in hierarchy_res:
            p_type, c_type, r_type = row["pType"], row["cType"], row["rType"]
            if not p_type or not c_type:
                continue
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
        self._ontology_cache.set(result)
        return result

    async def get_distinct_values(self, property_name: str) -> List[Any]:
        if property_name in ("entityType", "entitytype"):
            if self._mapping.entity_type_strategy == "label":
                rows = await self._run_read("MATCH (n) RETURN DISTINCT labels(n)[0] AS lbl")
                return [row["lbl"] for row in rows if row["lbl"]]
            else:
                et_field = self._mapping.entity_type_field or "entityType"
                rows = await self._run_read(
                    f"MATCH (n) WHERE n.`{et_field}` IS NOT NULL "
                    f"RETURN DISTINCT n.`{et_field}` AS v LIMIT 100"
                )
                return [row["v"] for row in rows]

        if property_name == "tags":
            tags_field = self._mapping.tags_field
            rows = await self._run_read(
                f"MATCH (n) WHERE n.`{tags_field}` IS NOT NULL RETURN n.`{tags_field}` AS tags"
            )
            seen: Set[str] = set()
            for row in rows:
                raw = row["tags"]
                try:
                    tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
                    for t in tags:
                        seen.add(t)
                except Exception:
                    pass
            return list(seen)

        mapped = self._mapping.cypher_field(property_name)
        safe_prop = _sanitize_label(mapped) or "urn"
        try:
            rows = await self._run_read(
                f"MATCH (n) WHERE n.`{safe_prop}` IS NOT NULL "
                f"RETURN DISTINCT n.`{safe_prop}` AS v LIMIT 100"
            )
            return [row["v"] for row in rows]
        except Exception:
            return []

    # ================================================================== #
    # Ancestor / Descendant Chains                                         #
    # ================================================================== #

    async def _compute_ancestor_chain(self, urn: str) -> List[str]:
        """Single Cypher to walk containment edges upward."""
        ip = self._id_prop()
        containment = list(self._get_containment_edge_types())
        if not containment:
            # No containment types — flat graph, no ancestors
            return []
        containment_cypher = "|".join(f"`{_sanitize_label(t)}`" for t in containment)

        rows = await self._run_read(
            f"MATCH path = (child)<-[:{containment_cypher}*1..10]-(ancestor) "
            f"WHERE child.{ip} = $urn "
            f"WITH path ORDER BY length(path) DESC LIMIT 1 "
            f"RETURN [n IN nodes(path)[1..] | n.{ip}] AS chain",
            {"urn": urn},
        )
        if rows and rows[0].get("chain"):
            return rows[0]["chain"]
        return []

    async def _get_ancestor_chain(self, urn: str) -> List[str]:
        """Get ancestor chain with optional Redis caching."""
        await self._ensure_redis()
        if self._redis_available and self._redis:
            cache_key = f"{self._database}:ancestors"
            try:
                raw = await self._redis.hget(cache_key, urn)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass

            ancestors = await self._compute_ancestor_chain(urn)
            try:
                await self._redis.hset(cache_key, urn, json.dumps(ancestors))
            except Exception:
                pass
            return ancestors

        # No Redis — compute on the fly
        return await self._compute_ancestor_chain(urn)

    async def get_ancestors(self, urn: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        chain = await self._get_ancestor_chain(urn)
        chain = chain[offset:offset + limit]
        if not chain:
            return []
        nodes = await self.get_nodes(NodeQuery(urns=chain, limit=len(chain), include_child_count=False))
        urn_to_node = {n.urn: n for n in nodes}
        return [urn_to_node[u] for u in chain if u in urn_to_node]

    async def get_descendants(
        self, urn: str, depth: int = 5,
        entity_types: Optional[List[str]] = None,
        limit: int = 100, offset: int = 0,
    ) -> List[GraphNode]:
        ip = self._id_prop()
        containment = list(self._get_containment_edge_types())
        if not containment:
            # No containment types — flat graph, no descendants
            return []
        containment_cypher = "|".join(f"`{_sanitize_label(t)}`" for t in containment)

        conditions = [f"root.{ip} = $urn"]
        params: Dict[str, Any] = {"urn": urn, "skip": offset, "lim": limit}

        if entity_types:
            types = [t.value if hasattr(t, "value") else str(t) for t in entity_types]
            params["entityTypes"] = [t.lower() for t in types]
            if self._mapping.entity_type_strategy == "label":
                conditions.append("toLower(labels(desc)[0]) IN $entityTypes")
            else:
                et_field = self._mapping.entity_type_field or "entityType"
                conditions.append(f"toLower(desc.`{et_field}`) IN $entityTypes")

        where = " AND ".join(conditions)
        cypher = (
            f"MATCH (root)-[:{containment_cypher}*1..{depth}]->(desc) "
            f"WHERE {where} "
            f"RETURN DISTINCT desc SKIP $skip LIMIT $lim"
        )

        try:
            rows = await self._run_read(cypher, params)
        except Exception as e:
            logger.warning("get_descendants query failed: %s", e)
            return []

        nodes = []
        for row in rows:
            n = self._extract_node_from_record(row["desc"])
            if n:
                nodes.append(n)
        return nodes

    # ================================================================== #
    # Tag / Layer Filtering                                                #
    # ================================================================== #

    async def get_nodes_by_tag(self, tag: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        tags_field = self._mapping.tags_field
        tag_pattern = json.dumps(tag)
        rows = await self._run_read(
            f"MATCH (n) WHERE n.`{tags_field}` IS NOT NULL AND n.`{tags_field}` CONTAINS $tag "
            f"RETURN n SKIP $skip LIMIT $limit",
            {"tag": tag_pattern, "skip": offset, "limit": limit},
        )
        nodes = []
        for row in rows:
            n = self._extract_node_from_record(row["n"])
            if n and tag in (n.tags or []):
                nodes.append(n)
        return nodes

    async def get_nodes_by_layer(self, layer_id: str, limit: int = 100, offset: int = 0) -> List[GraphNode]:
        layer_field = self._mapping.layer_field
        rows = await self._run_read(
            f"MATCH (n) WHERE n.`{layer_field}` = $lid RETURN n SKIP $skip LIMIT $limit",
            {"lid": layer_id, "skip": offset, "limit": limit},
        )
        nodes = []
        for row in rows:
            n = self._extract_node_from_record(row["n"])
            if n:
                nodes.append(n)
        return nodes

    # ================================================================== #
    # Write Operations                                                     #
    # ================================================================== #

    async def save_custom_graph(self, nodes: List[GraphNode], edges: List[GraphEdge]) -> bool:
        """Batch-save nodes and edges using UNWIND, grouped by label.

        Uses mapped property names so data round-trips correctly through
        the schema mapping layer.
        """
        batch_size = 500
        ip = self._id_prop()

        # Group nodes by label
        nodes_by_label: Dict[str, list] = defaultdict(list)
        for node in nodes:
            label = _sanitize_label(str(node.entity_type))
            nodes_by_label[label].append(self._node_to_write_props(node))

        label_mapping = {}
        for label, items in nodes_by_label.items():
            for item in items:
                label_mapping[item[self._mapping.identity_field]] = label

            # Build SET clause dynamically from mapped field names
            # All fields in the item dict except the identity field
            set_fields = [
                f"n.`{k}` = item.`{k}`"
                for k in items[0].keys()
                if k != self._mapping.identity_field
            ]
            set_clause = ", ".join(set_fields)

            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                try:
                    await self._run_write(
                        f"UNWIND $batch AS item "
                        f"MERGE (n:`{label}` {{{ip}: item.{ip}}}) "
                        f"SET {set_clause}",
                        {"batch": batch},
                    )
                except Exception as e:
                    logger.warning("Batch node merge failed for label %s: %s", label, e)
        self._urn_cache.put_bulk(label_mapping)

        # Group edges by relationship type
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
                batch = items[i:i + batch_size]
                try:
                    await self._run_write(
                        f"UNWIND $batch AS item "
                        f"MATCH (a {{{ip}: item.src}}) "
                        f"MATCH (b {{{ip}: item.tgt}}) "
                        f"MERGE (a)-[r:`{rel_type}`]->(b) "
                        f"SET r.id = item.eid, r.confidence = item.conf, "
                        f"r.properties = item.props",
                        {"batch": batch},
                    )
                except Exception as e:
                    logger.warning("Batch edge merge failed for type %s: %s", rel_type, e)

        return True

    async def create_node(self, node: GraphNode, containment_edge: Optional[GraphEdge] = None) -> bool:
        ip = self._id_prop()
        try:
            label = _sanitize_label(str(node.entity_type))
            props = self._node_to_write_props(node)
            await self._run_write(
                f"MERGE (n:`{label}` {{{ip}: $urn}}) SET n += $p",
                {"urn": node.urn, "p": props},
            )
            self._urn_cache.put(node.urn, label)

            if containment_edge:
                rel_type = _sanitize_label(str(containment_edge.edge_type))
                await self._run_write(
                    f"MATCH (a {{{ip}: $src}}) MATCH (b {{{ip}: $tgt}}) "
                    f"MERGE (a)-[r:`{rel_type}`]->(b) "
                    f"SET r.id = $eid, r.confidence = $conf",
                    {
                        "src": containment_edge.source_urn,
                        "tgt": containment_edge.target_urn,
                        "eid": containment_edge.id,
                        "conf": containment_edge.confidence,
                    },
                )
            return True
        except Exception as e:
            logger.error("create_node failed: %s", e)
            return False

    async def create_edge(self, edge: GraphEdge) -> bool:
        """Create a single edge in Neo4j."""
        ip = self._id_prop()
        try:
            rel_type = _sanitize_label(str(edge.edge_type))
            await self._run_write(
                f"MATCH (a {{{ip}: $src}}) MATCH (b {{{ip}: $tgt}}) "
                f"MERGE (a)-[r:`{rel_type}`]->(b) "
                f"SET r.id = $eid, r.confidence = $conf, r.properties = $props",
                {
                    "src": edge.source_urn,
                    "tgt": edge.target_urn,
                    "eid": edge.id,
                    "conf": edge.confidence or 1.0,
                    "props": json.dumps(edge.properties or {}),
                },
            )
            return True
        except Exception as e:
            logger.error("create_edge failed: %s", e)
            return False

    async def update_edge(self, edge_id: str, properties: Dict[str, Any]) -> Optional[GraphEdge]:
        """Update edge properties by edge ID."""
        raise NotImplementedError("Neo4j update_edge not yet implemented")

    async def delete_edge(self, edge_id: str) -> bool:
        """Delete an edge by its ID property."""
        raise NotImplementedError("Neo4j delete_edge not yet implemented")

    # ================================================================== #
    # Projection / Materialization Lifecycle Hooks                         #
    # ================================================================== #

    async def ensure_projections(self) -> None:
        """Create index for AGGREGATED edge lookups."""
        ip = self._id_prop()
        safe_id = _sanitize_label(self._mapping.identity_field)
        try:
            await self._run_write(
                f"CREATE INDEX idx_projection_{safe_id} IF NOT EXISTS "
                f"FOR (n:_Projection) ON (n.{ip})"
            )
        except Exception:
            pass

    async def on_lineage_edge_written(
        self, source_urn: str, target_urn: str, edge_id: str, edge_type: str,
    ) -> None:
        """Materialize AGGREGATED edges using ancestor chains.

        Idempotent: tracks contributing leaf edges in ``r.sourceEdgeIds``.
        Calling twice with the same ``edge_id`` does not change the weight.
        Weight is always ``size(r.sourceEdgeIds)`` — the true count of
        distinct contributing edges.
        """
        ip = self._id_prop()
        s_ancestors = await self._get_ancestor_chain(source_urn)
        t_ancestors = await self._get_ancestor_chain(target_urn)

        s_chain = [source_urn] + s_ancestors
        t_chain = [target_urn] + t_ancestors

        merge_batch = []
        for s_urn in s_chain:
            for t_urn in t_chain:
                if s_urn != t_urn:
                    merge_batch.append({"s": s_urn, "t": t_urn, "eid": edge_id})

        if not merge_batch:
            return

        try:
            await self._run_write(
                f"UNWIND $batch AS item "
                f"MERGE (s {{{ip}: item.s}}) "
                f"MERGE (t {{{ip}: item.t}}) "
                f"MERGE (s)-[r:AGGREGATED]->(t) "
                f"ON CREATE SET "
                f"  r.sourceEdgeIds = [item.eid], "
                f"  r.weight = 1, "
                f"  r.sourceEdgeTypes = [$edgeType], "
                f"  r.latestUpdate = timestamp() "
                f"ON MATCH SET "
                f"  r.sourceEdgeIds = CASE "
                f"    WHEN item.eid IN coalesce(r.sourceEdgeIds, []) THEN r.sourceEdgeIds "
                f"    ELSE coalesce(r.sourceEdgeIds, []) + item.eid END, "
                f"  r.weight = size(CASE "
                f"    WHEN item.eid IN coalesce(r.sourceEdgeIds, []) THEN r.sourceEdgeIds "
                f"    ELSE coalesce(r.sourceEdgeIds, []) + item.eid END), "
                f"  r.sourceEdgeTypes = CASE "
                f"    WHEN $edgeType IN coalesce(r.sourceEdgeTypes, []) THEN r.sourceEdgeTypes "
                f"    ELSE coalesce(r.sourceEdgeTypes, []) + $edgeType END, "
                f"  r.latestUpdate = timestamp()",
                {"batch": merge_batch, "edgeType": edge_type},
            )
        except Exception as e:
            logger.error("Batched AGGREGATED MERGE failed: %s", e)

    async def on_lineage_edge_deleted(
        self, source_urn: str, target_urn: str, edge_id: str,
    ) -> None:
        """Remove a contributing edge from AGGREGATED relationships.

        Scoped to affected pairs only — no full-graph scan. Removes the
        ``edge_id`` from ``r.sourceEdgeIds``, recomputes weight, and
        deletes the AGGREGATED edge when no contributing edges remain.
        """
        ip = self._id_prop()
        s_ancestors = await self._get_ancestor_chain(source_urn)
        t_ancestors = await self._get_ancestor_chain(target_urn)

        s_chain = [source_urn] + s_ancestors
        t_chain = [target_urn] + t_ancestors

        pairs = [{"s": s, "t": t} for s in s_chain for t in t_chain if s != t]
        if not pairs:
            return

        try:
            await self._run_write(
                f"UNWIND $batch AS item "
                f"MATCH (s {{{ip}: item.s}})-[r:AGGREGATED]->(t {{{ip}: item.t}}) "
                f"SET r.sourceEdgeIds = [eid IN coalesce(r.sourceEdgeIds, []) WHERE eid <> $edgeId], "
                f"    r.weight = size([eid IN coalesce(r.sourceEdgeIds, []) WHERE eid <> $edgeId]), "
                f"    r.latestUpdate = timestamp() "
                f"WITH r WHERE r.weight <= 0 "
                f"DELETE r",
                {"batch": pairs, "edgeId": edge_id},
            )
        except Exception as e:
            logger.error("Batched AGGREGATED decrement failed: %s", e)

    async def on_containment_changed(self, urn: str) -> None:
        """Invalidate ancestor cache for a node and its descendants."""
        await self._ensure_redis()
        if not self._redis_available or not self._redis:
            return  # No cache to invalidate

        ip = self._id_prop()
        cache_key = f"{self._database}:ancestors"
        containment = list(self._get_containment_edge_types())

        if not containment:
            # No containment types — only invalidate the node itself
            try:
                await self._redis.hdel(cache_key, urn)
            except Exception:
                pass
            logger.info("Invalidated ancestor cache for 1 node (no containment types): %s", urn)
            return

        # Single query to find all descendants instead of N+1 BFS
        containment_cypher = "|".join(f"`{_sanitize_label(t)}`" for t in containment)
        try:
            rows = await self._run_read(
                f"MATCH (root)-[:{containment_cypher}*0..10]->(desc) "
                f"WHERE root.{ip} = $urn "
                f"RETURN DISTINCT desc.{ip} AS durn",
                {"urn": urn},
            )
            urns_to_invalidate = [row["durn"] for row in rows if row["durn"]]
        except Exception:
            urns_to_invalidate = [urn]

        # Batch invalidate in Redis
        try:
            if urns_to_invalidate:
                await self._redis.hdel(cache_key, *urns_to_invalidate)
        except Exception:
            pass

        logger.info("Invalidated ancestor cache for %d nodes under %s", len(urns_to_invalidate), urn)

    async def count_aggregated_edges(self) -> int:
        """Cheap COUNT used as the denominator for purge progress
        reporting — see :meth:`purge_aggregated_edges`."""
        rows = await self._run_read(
            "MATCH ()-[r:AGGREGATED]->() RETURN count(r) AS total", {},
        )
        return int(rows[0]["total"]) if rows else 0

    async def purge_aggregated_edges(
        self,
        *,
        batch_size: int = 10_000,
        progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Remove ALL materialized AGGREGATED edges from the graph.

        Iterates the deletion in chunks of at most ``batch_size`` so a
        multi-million-edge purge produces visible progress (callers
        provide ``progress_callback`` to checkpoint each batch's running
        total) and cannot silently truncate at the previous one-shot
        ``LIMIT 100000``. The ``should_cancel`` predicate is checked
        between batches; raising ``JobCancelled`` from there exits the
        loop cleanly without orphaning the in-flight Cypher.
        """
        if batch_size <= 0:
            batch_size = 10_000
        batch_size = min(batch_size, 100_000)

        try:
            total_deleted = 0
            while True:
                if should_cancel is not None and should_cancel():
                    from backend.app.services.aggregation.cancel import JobCancelled
                    from datetime import datetime, timezone
                    raise JobCancelled(
                        job_id="<provider-cancel>",
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )

                rows = await self._run_write(
                    f"MATCH ()-[r:AGGREGATED]->() "
                    f"WITH r LIMIT {int(batch_size)} "
                    f"DELETE r "
                    f"RETURN count(r) AS deleted",
                    {},
                )
                deleted_in_batch = int(rows[0]["deleted"]) if rows else 0
                total_deleted += deleted_in_batch

                if progress_callback is not None:
                    try:
                        await progress_callback(total_deleted)
                    except Exception as cb_exc:
                        logger.warning(
                            "purge_aggregated_edges progress_callback raised: %s",
                            cb_exc,
                        )

                if deleted_in_batch < batch_size:
                    break

            # Clean up Redis tracking keys after all DELETEs land. Crash
            # mid-purge would otherwise leave the tracker cleared while
            # AGGREGATED edges still exist, silently no-op'ing the next
            # materialize run.
            if self._redis_available and self._redis:
                pattern = f"{self._database}:agg:sourceEdgeIds:*"
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(cursor, match=pattern, count=500)
                    if keys:
                        await self._redis.delete(*keys)
                    if cursor == 0:
                        break

            logger.info("Purged %d AGGREGATED edges from %s", total_deleted, self._database)
            return total_deleted
        except Exception as e:
            logger.error("Failed to purge AGGREGATED edges: %s", e)
            raise

    # ================================================================== #
    # Provider Registry Lifecycle                                          #
    # ================================================================== #

    async def list_graphs(self) -> List[str]:
        """Return available Neo4j database names (excludes system DB)."""
        driver = await self._get_driver()
        try:
            async with driver.session(database="system") as session:
                result = await session.run("SHOW DATABASES YIELD name")
                records = await result.data()
            return [r["name"] for r in records if r["name"] != "system"]
        except Exception as e:
            logger.warning("list_graphs (SHOW DATABASES) failed: %s", e)
            return []

    # ================================================================== #
    # Schema Discovery                                                     #
    # ================================================================== #

    async def discover_schema(self) -> Dict[str, Any]:
        """Introspect the Neo4j database and return labels, relationship types,
        property keys, sample data, and a suggested schema mapping.

        Uses aggregation queries to avoid N+1 per-label round-trips.
        """
        try:
            # Labels
            label_rows = await self._run_read("CALL db.labels() YIELD label RETURN label")
            labels = [row["label"] for row in label_rows]

            # Relationship types
            rel_rows = await self._run_read(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
            )
            rel_types = [row["relationshipType"] for row in rel_rows]

            # Count + samples per label in a single aggregation query
            label_stats_rows = await self._run_read(
                "MATCH (n) "
                "WITH labels(n)[0] AS lbl, properties(n) AS props "
                "WITH lbl, count(*) AS cnt, collect(props)[0..3] AS samples "
                "RETURN lbl, cnt, samples"
            )

            label_details: Dict[str, Any] = {}
            for row in label_stats_rows:
                lbl = row["lbl"]
                if not lbl:
                    continue
                sample_list = row["samples"] or []
                all_keys: Set[str] = set()
                for s in sample_list:
                    if isinstance(s, dict):
                        all_keys.update(s.keys())
                label_details[lbl] = {
                    "count": row["cnt"],
                    "propertyKeys": sorted(all_keys),
                    "samples": sample_list,
                }

            # Fill in any labels that had no nodes
            for lbl in labels:
                if lbl not in label_details:
                    label_details[lbl] = {"count": 0, "propertyKeys": [], "samples": []}

            suggested = self._suggest_mapping(label_details)

            return {
                "labels": labels,
                "relationshipTypes": rel_types,
                "labelDetails": label_details,
                "suggestedMapping": suggested,
            }
        except Exception as e:
            logger.error("discover_schema failed: %s", e)
            return {}

    @staticmethod
    def _suggest_mapping(label_details: Dict[str, Any]) -> Dict[str, Any]:
        """Heuristic mapping suggestion based on observed property keys."""
        all_keys: Set[str] = set()
        for detail in label_details.values():
            all_keys.update(detail.get("propertyKeys", []))

        mapping: Dict[str, str] = {}

        # Identity field
        for candidate in ("urn", "uuid", "id", "uri", "identifier", "nodeId"):
            if candidate in all_keys:
                mapping["identity_field"] = candidate
                break

        # Display name
        for candidate in ("displayName", "name", "title", "label", "display_name"):
            if candidate in all_keys:
                mapping["display_name_field"] = candidate
                break

        # Qualified name
        for candidate in ("qualifiedName", "qualified_name", "fqn", "fullName", "full_name"):
            if candidate in all_keys:
                mapping["qualified_name_field"] = candidate
                break

        # Description
        for candidate in ("description", "desc", "summary", "about"):
            if candidate in all_keys:
                mapping["description_field"] = candidate
                break

        # Tags
        for candidate in ("tags", "labels", "categories"):
            if candidate in all_keys:
                mapping["tags_field"] = candidate
                break

        return mapping
