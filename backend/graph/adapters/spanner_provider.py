"""Google Spanner Graph implementation of GraphDataProvider.

Schemaless model: a single ``GraphNode(urn, label, properties JSON)``
table and a single ``GraphEdge(urn, dest_urn, edge_id, label,
properties JSON)`` table, declared as a property graph with
``DYNAMIC LABEL (label) DYNAMIC PROPERTIES (properties)``. This matches
Synodic's open-string ``entity_type``/``edge_type`` taxonomy without
schema migrations per new type.

Async transport: the official sync ``google-cloud-spanner``
``Database``/``Snapshot``/``Batch`` API wrapped in
``anyio.to_thread.run_sync``. Streaming reassembly, session pool, and
``Aborted``-retry are battle-tested in the sync client; reimplementing
them on top of ``SpannerAsyncClient`` is high-risk for low gain at our
bounded concurrency. See ``backend/graph/adapters/spanner_async_seam.py``.

Trace v2, AGGREGATED edge materialisation, ancestor caching, and
schema-discovery suggestions delegate to ``backend/common/providers/``
shared modules.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from backend.common.adapters import CircuitBreakerProxy  # noqa: F401  (re-exported via registry)
from backend.common.interfaces.preflight import (
    PreflightResult,
    _classify as _classify_preflight_exc,
    tcp_preflight,
)
from backend.common.interfaces.provider import (
    GraphDataProvider,
    ProviderConfigurationError,
    ProviderInputError,
)
from backend.common.models.graph import (
    AggregatedEdgeInfo,
    AggregatedEdgeRequest,
    AggregatedEdgeResult,
    ChildrenWithEdgesResult,
    EdgeQuery,
    EdgeTypeMetadata,
    EdgeTypeSummary,
    EntityTypeHierarchy,
    EntityTypeSummary,
    GraphEdge,
    GraphNode,
    GraphSchemaStats,
    LineageResult,
    NodeQuery,
    OntologyMetadata,
    TagSummary,
    TopLevelNodesResult,
    TraceResult,
)
# Note: this provider implements aggregation natively against the sidecar
# table for performance reasons. The shared ``AggregatedEdgeMaterializer``
# in ``backend/common/providers/aggregation.py`` is kept for FalkorDB and
# Neo4j (where Redis SADD / Cypher property accumulators are sub-ms per
# pair); Spanner cannot afford one Spanner round-trip per ancestor pair.
from backend.common.providers.ancestor_cache import AncestorChainCache
from backend.common.providers.config import ProviderEnvBudget
from backend.common.providers.deadlines import DeadlineGuard
from backend.common.providers.schema_introspection import SchemaIntrospector
from backend.common.providers.trace_orchestrator import (
    ExpandRecord,
    FrontierRecord,
    TraceCallbacks,
    TraceOrchestrator,
)
from backend.graph.adapters.schema_mapping import SchemaMapping
from backend.graph.adapters.spanner_async_seam import to_thread

logger = logging.getLogger(__name__)


# Configurable, conservative defaults for Spanner.
_DEFAULT_GRAPH_NAME = "UniViz"
# Aggregated-edge label. Spanner Graph guidance is that dynamic labels
# should be lowercase + namespaced; the previous "AGGREGATED" both broke
# convention and could collide with a customer ontology that legitimately
# declared an edge type called "aggregated" (case-insensitive matching
# in GQL would have made `purge_aggregated_edges` delete real lineage).
# The underscore prefix puts the sentinel firmly outside any reasonable
# customer namespace. Audit M11.
_AGG_LABEL = "_synodic_aggregated"
_ENTITY_LABEL = "Entity"
_DEFAULT_MAX_QUANTIFIER_DEPTH = int(os.getenv("SPANNER_MAX_QUANTIFIER_DEPTH", "5"))
_DEFAULT_MERGE_BATCH = int(os.getenv("SPANNER_MERGE_SUB_BATCH_SIZE", "1000"))

# JSON cell-size guard. Spanner enforces a hard 10 MiB cell limit for
# JSON columns. The 8 MiB threshold leaves headroom for Spanner's
# row-overhead accounting and avoids the cliff where a single oversized
# property bag fails the entire batched mutation atomically — poisoning
# every adjacent row. Audit B9.
_SPANNER_JSON_MAX_BYTES = int(os.getenv(
    "SPANNER_JSON_MAX_BYTES", str(8 * 1024 * 1024),
))


def _safe_json_dumps(
    obj: Any,
    *,
    field: str,
    owner_id: str,
    max_bytes: int = _SPANNER_JSON_MAX_BYTES,
) -> str:
    """Serialise ``obj`` to JSON; raise ``ProviderInputError`` if the
    encoded payload exceeds ``max_bytes``. Always uses compact
    separators so the size check matches what Spanner stores.

    ``field`` and ``owner_id`` are surfaced in the error message so the
    operator can find the offending row. Audit B9.
    """
    encoded = json.dumps(obj, separators=(",", ":"))
    n = len(encoded.encode("utf-8"))
    if n > max_bytes:
        raise ProviderInputError(
            f"Spanner JSON {field} for {owner_id!r} is {n} bytes "
            f"(limit {max_bytes}). Spanner enforces a 10 MiB cell cap; "
            "shrink the payload or split the entity into multiple rows."
        )
    return encoded

# Spanner identifier rules: starts with a letter or underscore; remaining
# characters letters/digits/underscores; max 128 chars. Used to validate
# graph_name (interpolated unparameterised into ~17 GQL prefixes + the
# CREATE PROPERTY GRAPH DDL) and any other identifier that flows from
# extra_config or schema_mapping into a query string. Audit B6/B7.
_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _validate_identifier(name: str, *, what: str) -> str:
    """Reject anything that isn't a Spanner-safe bare identifier.

    Spanner DDL accepts no parameter binding for identifiers; the only
    safe substitution is a whitelist regex applied at ingest time. Used
    in __init__ for graph_name and (in Phase 5) for every column name
    that flows from a customer-supplied SchemaMapping.
    """
    if not isinstance(name, str) or not _VALID_IDENTIFIER_RE.match(name):
        raise ProviderConfigurationError(
            f"Invalid Spanner identifier for {what}: {name!r}. "
            "Must match ^[A-Za-z_][A-Za-z0-9_]{0,127}$ — no spaces, "
            "no SQL/GQL syntax, max 128 characters."
        )
    return name


class SpannerEditionError(RuntimeError):
    """Raised when the connected Spanner instance is not on Enterprise edition.

    The wizard catches this on ``preflight``/``_ensure_connected`` and renders a
    dedicated error card that distinguishes "wrong edition" from "wrong creds".
    """


# ===========================================================================
# Public provider
# ===========================================================================

class SpannerProvider(GraphDataProvider):
    """Google Spanner Graph provider.

    Configuration is supplied through the ProviderORM row (host/port are
    unused for managed Spanner) plus ``extra_config``:

    extra_config = {
        "projectId":   "my-gcp-project",
        "instanceId":  "uniViz-instance",
        "databaseId":  "uniViz",                  # the spanner DB
        "graphName":   "UniViz",                  # the property graph name
        "useEmulator": false,
        "schemaMapping": {...}                    # standard mapping overrides
    }

    credentials = {
        "service_account_json": "{...}"           # optional in emulator mode
    }
    """

    # ----- Construction & lifecycle ---------------------------------------

    def __init__(
        self,
        *,
        project_id: str,
        instance_id: str,
        database_id: str,
        graph_name: str = _DEFAULT_GRAPH_NAME,
        credentials_json: Optional[str] = None,
        use_emulator: bool = False,
        extra_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not project_id or not instance_id or not database_id:
            raise ValueError(
                "SpannerProvider requires project_id, instance_id, database_id"
            )
        self._project_id = project_id
        self._instance_id = instance_id
        self._database_id = database_id
        # graph_name is interpolated unparameterised into the
        # CREATE PROPERTY GRAPH DDL and into every GRAPH <name> GQL
        # prefix; reject anything that could break out of the identifier
        # position. Audit B6/B7.
        self._graph_name = _validate_identifier(graph_name, what="graph_name")
        self._credentials_json = credentials_json
        self._use_emulator = use_emulator
        self._extra_config = extra_config or {}

        self._schema_mapping = SchemaMapping.from_extra_config(self._extra_config)
        self._budget = ProviderEnvBudget.from_env("spanner")
        self._guard = DeadlineGuard(provider_name="spanner")

        self._client: Any = None
        self._instance: Any = None
        self._database: Any = None
        self._db_admin_client: Any = None
        self._connect_lock = asyncio.Lock()
        self._connected = False
        self._schema_bootstrapped = False
        # True iff CREATE PROPERTY GRAPH succeeded. The emulator and
        # Standard-edition Spanner refuse the DDL — we still bootstrap the
        # underlying tables and serve SQL paths, but every GQL-using ABC
        # method must call ``_require_gql`` and surface a clear error.
        self._has_property_graph: bool = False

        # Ontology injection state — populated by ContextEngine.
        self._resolved_containment_types: Set[str] = set()
        self._resolved_containment_types_set = False
        self._containment_fingerprint: str = "default"
        self._entity_type_levels: Dict[str, int] = {}
        self._resolved_edge_metadata: Dict[str, Any] = {}
        self._resolved_lineage_types: List[str] = []

        # Shared-base components.
        self._ancestor_cache = AncestorChainCache(
            namespace=f"spanner:{project_id}:{instance_id}:{database_id}",
            redis_client=None,
            in_memory_capacity=int(os.getenv("SPANNER_ANCESTOR_CACHE_CAPACITY", "50000")),
            ttl_s=int(os.getenv("SPANNER_ANCESTOR_CACHE_TTL", "3600")),
        )
        self._trace = TraceOrchestrator(_SpannerTraceCallbacks(self))
        self._introspector = _SpannerSchemaIntrospector(self)

    @property
    def name(self) -> str:
        return "spanner"

    async def preflight(self, *, deadline_s: float = 1.5) -> PreflightResult:
        """Fast reachability probe; never raises for connectivity outcomes.

        Returns the canonical ``PreflightResult`` from
        ``backend.common.interfaces.preflight`` so the manager's preflight
        gate, the ``/providers/test-connection`` endpoint, and the
        wizard's friendly-error map all read the same fields
        (``ok`` / ``reason`` / ``elapsed_ms`` / ``peer``).
        """
        # Emulator: pure TCP probe of localhost:9010. Reuses the shared
        # tcp_preflight building block — already returns the canonical type.
        if self._use_emulator:
            host, _, port = (
                os.getenv("SPANNER_EMULATOR_HOST", "localhost:9010").partition(":")
            )
            return await tcp_preflight(
                host or "localhost",
                int(port or 9010),
                deadline_s=deadline_s,
            )

        peer = (
            f"projects/{self._project_id}/instances/{self._instance_id}"
            f"/databases/{self._database_id}"
        )
        t0 = time.monotonic()

        def _residual(floor: float = 0.05) -> float:
            """Per-step residual: total deadline minus elapsed wall time.
            Floored so each step gets at least 50ms of forward progress
            even when the budget is nearly exhausted (avoids 0s timeouts
            that would surface as 'cancelled' rather than 'slow')."""
            return max(floor, deadline_s - (time.monotonic() - t0))

        try:
            await asyncio.wait_for(self._ensure_client(), timeout=_residual())
            await asyncio.wait_for(
                to_thread(
                    lambda: self._instance.exists(),
                    op_name="spanner.instance_exists",
                    read_only=True,
                ),
                timeout=_residual(),
            )

            # GQL probe — confirm the configured property graph actually
            # exists. Without this, a typo in ``extra_config.graphName``
            # surfaces only on the first user query, well after the
            # provider has already passed its connectivity check. The
            # ``information_schema.property_graphs`` view is unavailable
            # on the emulator (handled above) and on databases without
            # any property graph defined; we treat the latter as a
            # configuration failure at registration time.
            try:
                graph_rows = await self._execute_query(
                    "SELECT 1 FROM information_schema.property_graphs "
                    "WHERE property_graph_name = @name LIMIT 1",
                    op_name="preflight.property_graph_probe",
                    timeout_s=_residual(),
                    params={"name": self._graph_name},
                    param_types_={"name": _ParamTypes.STRING},
                )
            except Exception as exc:
                # The view itself may be unavailable (Standard edition,
                # pre-Graph Spanner). Fall back to instance.exists() as
                # the only signal — log for triage.
                logger.debug(
                    "spanner: property_graphs probe failed (%s); accepting "
                    "instance.exists() as the only preflight signal.", exc,
                )
                graph_rows = None

            if graph_rows is not None and not graph_rows:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                return PreflightResult.failure(
                    reason=f"graph_not_found: {self._graph_name}"[:120],
                    elapsed_ms=elapsed_ms,
                )

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return PreflightResult.success(peer=peer, elapsed_ms=elapsed_ms)
        except asyncio.CancelledError:
            raise
        except SpannerEditionError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return PreflightResult.failure(
                reason=f"edition_error: {exc}"[:120],
                elapsed_ms=elapsed_ms,
            )
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return PreflightResult.failure(
                reason=_classify_preflight_exc(exc),
                elapsed_ms=elapsed_ms,
            )

    async def _ensure_client(self) -> None:
        """Build the spanner.Client + Instance + Database (no DDL).

        Idempotent. The DDL bootstrap runs separately in ``_ensure_connected``
        because some callers (preflight) want connectivity-only verification.
        """
        if self._client is not None:
            return
        # Deferred import so ``import backend.graph.adapters.spanner_provider``
        # works in environments without google-cloud-spanner installed (tests
        # that don't actually touch Spanner).
        from google.cloud import spanner  # type: ignore
        from google.oauth2 import service_account  # type: ignore

        if self._use_emulator:
            os.environ["SPANNER_EMULATOR_HOST"] = os.getenv(
                "SPANNER_EMULATOR_HOST", "localhost:9010",
            )
            credentials = None
        elif self._credentials_json:
            info = json.loads(self._credentials_json)
            credentials = service_account.Credentials.from_service_account_info(info)
        else:
            credentials = None  # ADC fallback

        def _build():
            client = spanner.Client(project=self._project_id, credentials=credentials)
            instance = client.instance(self._instance_id)
            database = instance.database(self._database_id)
            return client, instance, database

        self._client, self._instance, self._database = await to_thread(
            _build,
            op_name="spanner.client_build",
            read_only=False,  # constructor; not idempotent across attempts
        )

    async def _ensure_connected(self) -> None:
        """Connect + bootstrap schema if missing. Idempotent under lock."""
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            await self._ensure_client()
            if not self._schema_bootstrapped:
                await self._ensure_schema_bootstrap()
                self._schema_bootstrapped = True
            self._connected = True

    async def close(self) -> None:
        if self._database is not None:
            try:
                # Sync Database holds a session pool; release it cleanly.
                # Wrapped in DeadlineGuard so a stuck pool clear cannot hang
                # ProviderRegistry.evict_provider — Phase 5/M22 expands this
                # to also close the gRPC channel + admin client transport.
                await self._guard.run(
                    to_thread(
                        lambda: getattr(self._database, "session_pool", None) and self._database.session_pool.clear(),
                        op_name="spanner.session_pool.clear",
                        read_only=False,
                    ),
                    op_name="close.session_pool",
                    timeout_s=self._budget.init,
                )
            except Exception as exc:
                logger.warning(
                    "spanner: pool clear failed: %s", exc,
                    extra={"provider": "spanner", "op": "close", "outcome": "error"},
                )
        self._client = None
        self._instance = None
        self._database = None
        self._db_admin_client = None
        self._connected = False
        self._schema_bootstrapped = False

    # ----- Schema bootstrap -----------------------------------------------

    async def _ensure_schema_bootstrap(self) -> None:
        """Create GraphNode / GraphEdge tables + property graph if absent.

        Idempotent: each step is gated by an ``INFORMATION_SCHEMA`` check.
        Edition errors are translated to ``SpannerEditionError``.
        """
        await self._guard.run(
            self._bootstrap_tables(), op_name="bootstrap_tables", timeout_s=self._budget.init * 5,
        )
        await self._guard.run(
            self._bootstrap_property_graph(),
            op_name="bootstrap_property_graph",
            timeout_s=self._budget.init * 5,
        )

    async def _bootstrap_tables(self) -> None:
        ddl: List[str] = []
        if not await self._table_exists("GraphNode"):
            ddl.append(_DDL_CREATE_GRAPH_NODE)
        if not await self._table_exists("GraphEdge"):
            ddl.append(_DDL_CREATE_GRAPH_EDGE)
        if not await self._table_exists("GraphEdgeContribution"):
            ddl.append(_DDL_CREATE_GRAPH_EDGE_CONTRIBUTION)

        # Index DDL is filtered through information_schema.indexes so we
        # never re-send a CREATE INDEX for one that already exists. Cloud
        # Spanner supports ``IF NOT EXISTS`` on CREATE INDEX as of 2024,
        # but the emulator's parser is uneven and earlier Spanner releases
        # rejected the form outright. Pre-filtering is correct on every
        # supported configuration.
        existing_indexes = await self._existing_index_names()
        for stmt in (*_DDL_CREATE_INDEXES, *_DDL_CREATE_CONTRIBUTION_INDEXES):
            name = _index_name_from_ddl(stmt)
            if name and name not in existing_indexes:
                ddl.append(stmt)

        if ddl:
            await self._apply_ddl(ddl)

    async def _existing_index_names(self) -> Set[str]:
        try:
            rows = await self._execute_query(
                "SELECT index_name FROM information_schema.indexes "
                "WHERE table_schema = '' "
                "AND table_name IN ('GraphNode', 'GraphEdge', 'GraphEdgeContribution')",
                op_name="existing_index_names",
                timeout_s=self._budget.init,
            )
        except Exception as exc:
            # information_schema.indexes is universally available on real
            # Spanner; the emulator may lag. Treat lookup failure as
            # "unknown" — fall back to sending the DDL and let
            # IF NOT EXISTS / driver semantics handle it.
            logger.debug("spanner: index introspection failed (%s); will send all DDL", exc)
            return set()
        return {str(r["index_name"]) for r in rows if r.get("index_name")}

    async def _bootstrap_property_graph(self) -> None:
        # Two environments cannot host a property graph:
        #   * cloud-spanner-emulator (no CREATE PROPERTY GRAPH support).
        #   * Standard-edition Spanner (Graph requires Enterprise+).
        # In both cases the relational tables are still usable, so the
        # provider stays functional for ingestion + SQL paths and only
        # GQL-using methods raise via ``_require_gql``.
        try:
            if await self._property_graph_exists(self._graph_name):
                self._has_property_graph = True
                return
        except Exception as exc:
            # ``information_schema.property_graphs`` itself isn't available
            # on the emulator. Fall through and try the DDL; the failure
            # below is handled identically.
            logger.debug(
                "spanner: property_graphs view unavailable (%s); "
                "trying CREATE PROPERTY GRAPH directly", exc,
            )

        try:
            await self._apply_ddl([_DDL_CREATE_PROPERTY_GRAPH(self._graph_name)])
            self._has_property_graph = True
        except Exception as exc:
            msg = str(exc).lower()
            if (
                "edition" in msg
                or "enterprise" in msg
                or "feature is not enabled" in msg
            ) and not self._use_emulator:
                # Real Spanner instance refused the DDL on edition grounds —
                # surface as a typed error so the wizard can render its
                # dedicated edition card.
                raise SpannerEditionError(
                    "Spanner Graph requires Enterprise or Enterprise Plus edition; "
                    f"this instance refused CREATE PROPERTY GRAPH: {exc}"
                ) from exc
            # Emulator path, or any other failure that we treat as "GQL
            # unavailable here". Keep the provider usable for SQL.
            self._has_property_graph = False
            logger.warning(
                "spanner: CREATE PROPERTY GRAPH failed (%s). "
                "Continuing with SQL-only mode; GQL-using methods will raise.",
                exc,
            )

    def _require_gql(self) -> None:
        """Raise for methods that depend on the property graph being live.

        Called at the head of every GQL-using public method. Lets the
        emulator-backed test suite (and any Standard-edition instance)
        run the SQL surface without blowing up at import / connect time.
        """
        if not self._has_property_graph:
            raise RuntimeError(
                "Spanner Graph (GQL) is not available on this connection. "
                "This is expected on the cloud-spanner-emulator (no GQL "
                "support) and on Standard-edition Spanner (Graph is an "
                "Enterprise+ feature). SQL paths (ingestion, schema "
                "discovery, stats, count_aggregated_edges) still work."
            )

    async def _table_exists(self, table_name: str) -> bool:
        rows = await self._execute_query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = '' AND table_name = @name",
            op_name="table_exists",
            timeout_s=self._budget.init,
            params={"name": table_name},
            param_types_={"name": _ParamTypes.STRING},
        )
        return bool(rows)

    async def _property_graph_exists(self, graph_name: str) -> bool:
        try:
            rows = await self._execute_query(
                "SELECT 1 FROM information_schema.property_graphs "
                "WHERE property_graph_name = @name",
                op_name="property_graph_exists",
                timeout_s=self._budget.init,
                params={"name": graph_name},
                param_types_={"name": _ParamTypes.STRING},
            )
            return bool(rows)
        except Exception as exc:
            # The view is unavailable on pre-Graph editions — translate.
            msg = str(exc).lower()
            if "property_graphs" in msg and ("not found" in msg or "unknown" in msg):
                return False
            raise

    async def _apply_ddl(self, ddl: List[str]) -> None:
        if not ddl:
            return
        # Defer the admin client until a DDL is actually needed.
        if self._db_admin_client is None:
            from google.cloud import spanner_admin_database_v1  # type: ignore
            self._db_admin_client = await to_thread(
                spanner_admin_database_v1.DatabaseAdminClient,
                op_name="spanner.admin_client_build",
                read_only=False,
            )

        database_name = (
            f"projects/{self._project_id}/instances/{self._instance_id}"
            f"/databases/{self._database_id}"
        )

        # The DDL apply itself is bounded server-side by op.result(timeout=);
        # match that timeout to the same outer DeadlineGuard budget so a
        # cancelled bootstrap doesn't leave the worker thread sleeping for
        # a minute while the outer wait_for has already returned.
        ddl_budget = max(self._budget.init * 5, 1.0)

        def _apply():
            op = self._db_admin_client.update_database_ddl(
                database=database_name, statements=ddl,
            )
            op.result(timeout=ddl_budget)

        await self._guard.run(
            to_thread(
                _apply,
                op_name="spanner.update_database_ddl",
                read_only=False,
                attributes=self._otel_attrs("apply_ddl", "ddl"),
            ),
            op_name="apply_ddl",
            timeout_s=ddl_budget,
        )

    # ----- Ontology injection (called by ContextEngine) -------------------

    def _reject_aggregated_collision(self, edge_types: Iterable[str]) -> None:
        """Refuse to accept an ontology that declares an edge type whose
        lowercase form collides with the AGGREGATED sentinel.

        ``_AGG_LABEL`` is reserved by the provider for materialised
        aggregated edges; if a customer ontology re-used the same name
        (any case), every GQL filter that compares ``e.label`` lower-cased
        would over-match — ``purge_aggregated_edges`` would then delete
        the customer's real lineage. Audit M11.
        """
        sentinel = _AGG_LABEL.lower()
        offenders = sorted({
            t for t in edge_types
            if isinstance(t, str) and t.lower() == sentinel
        })
        if offenders:
            raise ProviderConfigurationError(
                f"Ontology declares edge type(s) {offenders!r} that collide "
                f"with the reserved AGGREGATED sentinel {_AGG_LABEL!r} "
                "(case-insensitive). Rename the ontology edge type — the "
                "sentinel is namespaced precisely so this collision is "
                "detected at registration time rather than after data loss."
            )

    def set_containment_edge_types(
        self, types: List[str], from_ontology: bool = True,
    ) -> None:
        # Spanner schemaless treats GraphEdge.label as case-sensitive data.
        # Store edge types verbatim; do NOT normalise case here or in the
        # query-side filters. ContextEngine is the single source of truth
        # for which casing the ontology actually uses.
        self._reject_aggregated_collision(types or [])
        self._resolved_containment_types = set(types or [])
        self._resolved_containment_types_set = True
        digest_input = ",".join(sorted(self._resolved_containment_types))
        self._containment_fingerprint = hashlib.sha256(digest_input.encode()).hexdigest()[:16]

    def set_entity_type_levels(self, mapping: Dict[str, int]) -> None:
        self._entity_type_levels = dict(mapping or {})

    def set_resolved_edge_metadata(
        self,
        edge_type_metadata: Dict[str, Any],
        lineage_edge_types: List[str],
    ) -> None:
        # Same collision check on both metadata keys + lineage list — the
        # ontology can declare an edge type via either path.
        self._reject_aggregated_collision(
            list((edge_type_metadata or {}).keys()) + list(lineage_edge_types or [])
        )
        self._resolved_edge_metadata = dict(edge_type_metadata or {})
        self._resolved_lineage_types = list(lineage_edge_types or [])

    def _containment_types(self) -> List[str]:
        if self._resolved_containment_types_set:
            return list(self._resolved_containment_types)
        env = os.getenv("CONTAINMENT_EDGE_TYPES", "")
        if env:
            return [t.strip() for t in env.split(",") if t.strip()]
        raise ProviderConfigurationError(
            "Spanner provider has no containment edge types configured. "
            "ContextEngine must call set_containment_edge_types() after "
            "ontology resolution. Translate to HTTP 400 with a clear "
            "ontology-configuration message; do not silently default."
        )

    # =====================================================================
    # GraphDataProvider implementation
    # =====================================================================

    # ----- Node operations -------------------------------------------------

    async def get_node(self, urn: str) -> Optional[GraphNode]:
        await self._ensure_connected()
        self._require_gql()
        rows = await self._execute_query(
            f"GRAPH {self._graph_name}\n"
            "MATCH (n:Entity {urn: @urn})\n"
            "RETURN n.urn AS urn, n.label AS label, "
            "       TO_JSON(n.properties) AS properties",
            op_name="get_node",
            params={"urn": urn},
            param_types_={"urn": _ParamTypes.STRING},
        )
        if not rows:
            return None
        return self._row_to_node(rows[0])

    async def get_nodes(self, query: NodeQuery) -> List[GraphNode]:
        await self._ensure_connected()
        self._require_gql()
        params: Dict[str, Any] = {
            "labels": [t for t in (query.entity_types or [])] or None,
            "search": query.search_query or None,
        }
        param_types_ = {
            "labels": _ParamTypes.array(_ParamTypes.STRING),
            "search": _ParamTypes.STRING,
        }
        limit = _safe_int(query.limit, default=100, max_value=10_000)
        offset = _safe_int(query.offset, default=0, max_value=1_000_000)
        gql = (
            f"GRAPH {self._graph_name}\n"
            "MATCH (n:Entity)\n"
            "WHERE (@labels IS NULL OR n.label IN UNNEST(@labels))\n"
            "  AND (@search IS NULL OR REGEXP_CONTAINS("
            "       LAX_STRING(n.properties.displayName), @search))\n"
            "RETURN n.urn AS urn, n.label AS label, "
            "       TO_JSON(n.properties) AS properties\n"
            "ORDER BY n.urn\n"
            f"LIMIT {limit} OFFSET {offset}"
        )
        rows = await self._execute_query(
            gql,
            op_name="get_nodes",
            params=params,
            param_types_=param_types_,
        )
        return [self._row_to_node(r) for r in rows]

    async def search_nodes(self, query: str, limit: int = 10) -> List[GraphNode]:
        return await self.get_nodes(NodeQuery(search_query=query, limit=limit))

    # ----- Edge operations -------------------------------------------------

    async def get_edges(self, query: EdgeQuery) -> List[GraphEdge]:
        await self._ensure_connected()
        self._require_gql()
        src_list = list(query.source_urns) if query.source_urns else None
        dst_list = list(query.target_urns) if query.target_urns else None
        type_list = list(query.edge_types) if query.edge_types else None
        limit = _safe_int(query.limit, default=100, max_value=10_000)
        gql = (
            f"GRAPH {self._graph_name}\n"
            "MATCH (s:Entity)-[e]->(t:Entity)\n"
            "WHERE (@src IS NULL OR s.urn IN UNNEST(@src))\n"
            "  AND (@dst IS NULL OR t.urn IN UNNEST(@dst))\n"
            "  AND (@types IS NULL OR e.label IN UNNEST(@types))\n"
            "RETURN s.urn AS source_urn, t.urn AS target_urn,\n"
            "       e.edge_id AS id, e.label AS edge_type,\n"
            "       TO_JSON(e.properties) AS properties\n"
            "ORDER BY s.urn, t.urn, e.edge_id\n"
            f"LIMIT {limit}"
        )

        def _params(src_chunk: Optional[List[str]]) -> tuple[Dict[str, Any], Dict[str, Any]]:
            return (
                {"src": src_chunk, "dst": dst_list, "types": type_list},
                {
                    "src": _ParamTypes.array(_ParamTypes.STRING),
                    "dst": _ParamTypes.array(_ParamTypes.STRING),
                    "types": _ParamTypes.array(_ParamTypes.STRING),
                },
            )

        # Chunk on src when the caller supplied a list; if src is None
        # we run a single call (filter is open). dst/types are typically
        # bounded (ontology / explicit set) so passing whole is safe.
        all_rows: List[Dict[str, Any]] = []
        if src_list:
            for src_chunk in self._chunk_array(src_list):
                params, param_types_ = _params(src_chunk)
                all_rows.extend(await self._execute_query(
                    gql, op_name="get_edges",
                    params=params, param_types_=param_types_,
                ))
        else:
            params, param_types_ = _params(None)
            all_rows = await self._execute_query(
                gql, op_name="get_edges",
                params=params, param_types_=param_types_,
            )
        # Re-apply the caller's LIMIT after merging chunks.
        return [self._row_to_edge(r) for r in all_rows[:limit]]

    # ----- Containment hierarchy ------------------------------------------

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
        self._require_gql()
        ctypes = list(edge_types) if edge_types else self._containment_types()
        params: Dict[str, Any] = {
            "parent": parent_urn,
            "cont_types": ctypes,
            "types": [t for t in (entity_types or [])] or None,
            "search": search_query or None,
            "cursor": cursor,
        }
        param_types_ = {
            "parent": _ParamTypes.STRING,
            "cont_types": _ParamTypes.array(_ParamTypes.STRING),
            "types": _ParamTypes.array(_ParamTypes.STRING),
            "search": _ParamTypes.STRING,
            "cursor": _ParamTypes.STRING,
        }
        safe_limit = _safe_int(limit, default=100, max_value=1_000)
        gql = (
            f"GRAPH {self._graph_name}\n"
            "MATCH (p:Entity {urn: @parent})-[c]->(child:Entity)\n"
            "WHERE c.label IN UNNEST(@cont_types)\n"
            "  AND (@types IS NULL OR child.label IN UNNEST(@types))\n"
            "  AND (@search IS NULL OR REGEXP_CONTAINS("
            "       LAX_STRING(child.properties.displayName), @search))\n"
            "  AND (@cursor IS NULL OR "
            "       LAX_STRING(child.properties.displayName) > @cursor)\n"
            "RETURN child.urn AS urn, child.label AS label,\n"
            "       TO_JSON(child.properties) AS properties\n"
            "ORDER BY LAX_STRING(child.properties.displayName)\n"
            f"LIMIT {safe_limit}"
        )
        rows = await self._execute_query(
            gql,
            op_name="get_children",
            params=params,
            param_types_=param_types_,
        )
        return [self._row_to_node(r) for r in rows]

    async def get_parent(self, child_urn: str) -> Optional[GraphNode]:
        await self._ensure_connected()
        self._require_gql()
        ctypes = self._containment_types()
        rows = await self._execute_query(
            f"GRAPH {self._graph_name}\n"
            "MATCH (parent:Entity)-[c]->(child:Entity {urn: @urn})\n"
            "WHERE c.label IN UNNEST(@cont_types)\n"
            "RETURN parent.urn AS urn, parent.label AS label,\n"
            "       TO_JSON(parent.properties) AS properties\n"
            "LIMIT 1",
            op_name="get_parent",
            params={"urn": child_urn, "cont_types": ctypes},
            param_types_={
                "urn": _ParamTypes.STRING,
                "cont_types": _ParamTypes.array(_ParamTypes.STRING),
            },
        )
        if not rows:
            return None
        return self._row_to_node(rows[0])

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
        await self._ensure_connected()
        self._require_gql()
        ctypes = self._containment_types()
        types_filter = entity_types or root_entity_types or None
        safe_limit = _safe_int(limit, default=100, max_value=1_000)
        gql = (
            f"GRAPH {self._graph_name}\n"
            "MATCH (n:Entity)\n"
            "WHERE NOT EXISTS {\n"
            "  MATCH (parent:Entity)-[c]->(n)\n"
            "  WHERE c.label IN UNNEST(@cont_types)\n"
            "}\n"
            "  AND (@types IS NULL OR n.label IN UNNEST(@types))\n"
            "  AND (@search IS NULL OR REGEXP_CONTAINS("
            "       LAX_STRING(n.properties.displayName), @search))\n"
            "  AND (@cursor IS NULL OR "
            "       LAX_STRING(n.properties.displayName) > @cursor)\n"
            "RETURN n.urn AS urn, n.label AS label,\n"
            "       TO_JSON(n.properties) AS properties\n"
            "ORDER BY LAX_STRING(n.properties.displayName)\n"
            f"LIMIT {safe_limit}"
        )
        rows = await self._execute_query(
            gql,
            op_name="get_top_level_or_orphan_nodes",
            params={
                "cont_types": ctypes,
                "types": types_filter,
                "search": search_query,
                "cursor": cursor,
            },
            param_types_={
                "cont_types": _ParamTypes.array(_ParamTypes.STRING),
                "types": _ParamTypes.array(_ParamTypes.STRING),
                "search": _ParamTypes.STRING,
                "cursor": _ParamTypes.STRING,
            },
        )
        nodes = [self._row_to_node(r) for r in rows]
        roots = set(t.lower() for t in (root_entity_types or []))
        root_count = sum(1 for n in nodes if n.entity_type.lower() in roots)
        next_cursor = nodes[-1].display_name if nodes and len(nodes) >= safe_limit else None
        return TopLevelNodesResult(
            nodes=nodes,
            totalCount=len(nodes) + (1 if next_cursor else 0),
            hasMore=next_cursor is not None,
            nextCursor=next_cursor,
            rootTypeCount=root_count,
            orphanCount=max(0, len(nodes) - root_count),
        )

    # ----- Lineage --------------------------------------------------------

    async def get_upstream(
        self,
        urn: str,
        depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        return await self._directional_lineage(urn, depth, direction="upstream")

    async def get_downstream(
        self,
        urn: str,
        depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        return await self._directional_lineage(urn, depth, direction="downstream")

    async def get_full_lineage(
        self,
        urn: str,
        upstream_depth: int,
        downstream_depth: int,
        include_column_lineage: bool = False,
        descendant_types: Optional[List[str]] = None,
    ) -> LineageResult:
        # TaskGroup (over plain gather) ensures that if one branch raises,
        # the surviving sibling is awaited / cancelled cleanly before the
        # exception propagates — combined with the seam's read_only=True
        # cancellation contract, no worker thread is left holding a session.
        async with asyncio.TaskGroup() as tg:
            up_task = tg.create_task(
                self._directional_lineage(urn, upstream_depth, direction="upstream")
            )
            down_task = tg.create_task(
                self._directional_lineage(urn, downstream_depth, direction="downstream")
            )
        up, down = up_task.result(), down_task.result()
        nodes_by_urn: Dict[str, GraphNode] = {n.urn: n for n in up.nodes}
        for n in down.nodes:
            nodes_by_urn.setdefault(n.urn, n)
        edges_by_id: Dict[str, GraphEdge] = {e.id: e for e in up.edges}
        for e in down.edges:
            edges_by_id.setdefault(e.id, e)
        return LineageResult(
            nodes=list(nodes_by_urn.values()),
            edges=list(edges_by_id.values()),
            upstreamUrns=up.upstream_urns,
            downstreamUrns=down.downstream_urns,
            totalCount=len(nodes_by_urn),
            hasMore=False,
        )

    async def _directional_lineage(
        self, urn: str, depth: int, direction: str,
    ) -> LineageResult:
        await self._ensure_connected()
        self._require_gql()
        safe_depth = max(1, min(_DEFAULT_MAX_QUANTIFIER_DEPTH, _safe_int(depth, default=3, max_value=_DEFAULT_MAX_QUANTIFIER_DEPTH)))
        ltypes = self._resolved_lineage_types or []
        # Direction shapes the GQL pattern arrows.
        if direction == "upstream":
            pattern = "(focus:Entity {urn: @urn})<-[e]-{1," + str(safe_depth) + "}(other:Entity)"
        else:
            pattern = "(focus:Entity {urn: @urn})-[e]->{1," + str(safe_depth) + "}(other:Entity)"
        gql = (
            f"GRAPH {self._graph_name}\n"
            # ``ANY SHORTEST`` short-circuits BFS once the first shortest
            # path between focus and ``other`` is found. The path variable
            # binding is intentionally omitted -- we use the edge sequence
            # ``e`` directly. Spanner GQL accepts ``MATCH ANY SHORTEST
            # <pattern>`` without a path variable.
            f"MATCH ANY SHORTEST {pattern}\n"
            "WHERE (@ltypes IS NULL OR ALL(edge IN e WHERE edge.label IN UNNEST(@ltypes)))\n"
            "RETURN other.urn AS urn, other.label AS label,\n"
            "       TO_JSON(other.properties) AS properties,\n"
            "       ARRAY(SELECT AS STRUCT edge.urn AS source_urn, "
            "             edge.dest_urn AS target_urn, edge.edge_id AS id, "
            "             edge.label AS edge_type, "
            "             TO_JSON(edge.properties) AS properties FROM UNNEST(e) edge"
            "       ) AS edges"
        )
        rows = await self._execute_query(
            gql,
            op_name=f"get_{direction}",
            params={"urn": urn, "ltypes": ltypes or None},
            param_types_={
                "urn": _ParamTypes.STRING,
                "ltypes": _ParamTypes.array(_ParamTypes.STRING),
            },
        )
        nodes_by_urn: Dict[str, GraphNode] = {}
        edges_by_id: Dict[str, GraphEdge] = {}
        side: Set[str] = set()
        for r in rows:
            n = self._row_to_node(r)
            nodes_by_urn[n.urn] = n
            side.add(n.urn)
            for e_struct in r.get("edges") or []:
                e = self._row_to_edge(e_struct)
                edges_by_id[e.id] = e
        return LineageResult(
            nodes=list(nodes_by_urn.values()),
            edges=list(edges_by_id.values()),
            upstreamUrns=side if direction == "upstream" else set(),
            downstreamUrns=side if direction == "downstream" else set(),
            totalCount=len(nodes_by_urn),
            hasMore=False,
        )

    # ----- Trace v2 (delegated) -------------------------------------------

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
        self._require_gql()
        return await self._trace.trace_at_level(
            urn=urn, level=level,
            upstream_depth=upstream_depth, downstream_depth=downstream_depth,
            lineage_edge_types=lineage_edge_types,
            containment_edge_types=containment_edge_types,
            max_nodes=max_nodes, timeout_ms=timeout_ms,
            include_containment_edges=include_containment_edges,
            include_inherited_lineage=include_inherited_lineage,
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
        self._require_gql()
        return await self._trace.expand_aggregated(
            source_urn=source_urn, target_urn=target_urn,
            next_level=next_level,
            lineage_edge_types=lineage_edge_types,
            containment_edge_types=containment_edge_types,
            max_nodes=max_nodes, timeout_ms=timeout_ms,
            use_raw_edges=use_raw_edges,
            include_containment_edges=include_containment_edges,
        )

    async def get_trace_lineage(
        self,
        urn: str,
        direction: str,
        depth: int,
        containment_edges: List[str],
        lineage_edges: List[str],
    ) -> LineageResult:
        # Legacy adapter — delegate to direction-based fetch.
        if direction == "upstream":
            return await self.get_upstream(urn, depth)
        if direction == "downstream":
            return await self.get_downstream(urn, depth)
        return await self.get_full_lineage(urn, depth, depth)

    # ----- Aggregated edges ------------------------------------------------

    async def get_aggregated_edges_between(
        self,
        source_urns: List[str],
        target_urns: Optional[List[str]],
        granularity: Any,
        containment_edges: List[str],
        lineage_edges: List[str],
    ) -> AggregatedEdgeResult:
        await self._ensure_connected()
        self._require_gql()
        if not source_urns:
            return AggregatedEdgeResult(aggregatedEdges=[], totalSourceEdges=0)

        # Two GQL shapes — with/without target filter — built so params and
        # param_types_ stay key-symmetric (audit M9). Chunk source_urns
        # since it's user-supplied and could exceed the per-array
        # practical cap (~few thousand elements in Spanner GQL).
        has_targets = bool(target_urns)
        if has_targets:
            gql = (
                f"GRAPH {self._graph_name}\n"
                "MATCH (s:Entity)-[e]->(t:Entity)\n"
                "WHERE s.urn IN UNNEST(@srcs) AND t.urn IN UNNEST(@dsts)\n"
                "  AND e.label = @agg_label\n"
                "RETURN s.urn AS source_urn, t.urn AS target_urn,\n"
                "       e.edge_id AS id, TO_JSON(e.properties) AS properties\n"
                "ORDER BY LAX_INT64(e.properties.weight) DESC"
            )
        else:
            gql = (
                f"GRAPH {self._graph_name}\n"
                "MATCH (s:Entity)-[e]->(t:Entity)\n"
                "WHERE s.urn IN UNNEST(@srcs)\n"
                "  AND e.label = @agg_label\n"
                "RETURN s.urn AS source_urn, t.urn AS target_urn,\n"
                "       e.edge_id AS id, TO_JSON(e.properties) AS properties\n"
                "ORDER BY LAX_INT64(e.properties.weight) DESC"
            )

        all_rows: List[Dict[str, Any]] = []
        for srcs_chunk in self._chunk_array(list(source_urns)):
            params: Dict[str, Any] = {"srcs": srcs_chunk, "agg_label": _AGG_LABEL}
            param_types_: Dict[str, Any] = {
                "srcs": _ParamTypes.array(_ParamTypes.STRING),
                "agg_label": _ParamTypes.STRING,
            }
            if has_targets:
                params["dsts"] = list(target_urns or [])
                param_types_["dsts"] = _ParamTypes.array(_ParamTypes.STRING)
            chunk_rows = await self._execute_query(
                gql,
                op_name="get_aggregated_edges_between",
                params=params,
                param_types_=param_types_,
            )
            all_rows.extend(chunk_rows)

        infos: List[AggregatedEdgeInfo] = []
        total = 0
        for r in all_rows:
            props = _decode_json(r.get("properties"))
            weight = int(props.get("weight", 1)) if isinstance(props, dict) else 1
            edge_types = props.get("source_edge_types", []) if isinstance(props, dict) else []
            infos.append(AggregatedEdgeInfo(
                id=str(r.get("id") or f"agg-{r['source_urn']}-{r['target_urn']}"),
                sourceUrn=r["source_urn"],
                targetUrn=r["target_urn"],
                edgeCount=weight,
                edgeTypes=list(edge_types or []),
                confidence=1.0,
                sourceEdgeIds=list(props.get("source_edge_ids", []) or []) if isinstance(props, dict) else [],
            ))
            total += weight
        return AggregatedEdgeResult(aggregatedEdges=infos, totalSourceEdges=total)

    # ----- Materialisation hooks ------------------------------------------

    async def on_lineage_edge_written(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
        edge_type: str,
    ) -> None:
        """Materialise AGGREGATED edges for one new leaf lineage edge.

        Native batched implementation: read ancestor chains (cached),
        compute the cross-product of pairs, then run one read-write
        transaction that (a) inserts contribution rows, (b) recomputes
        weights/types for affected pairs, (c) upserts AGGREGATED edges.
        Cost: O(1) Spanner round-trips per leaf edge regardless of
        ancestor depth.
        """
        await self._ensure_connected()
        # Ancestor-chain fetch goes via GQL. Without a property graph
        # we cannot materialise reliably; fail loudly so a misconfigured
        # production deploy doesn't silently lose AGGREGATED edges.
        self._require_gql()

        # TaskGroup so a failure on one ancestor-chain fetch cancels the
        # sibling cleanly instead of leaking a worker thread.
        async with asyncio.TaskGroup() as tg:
            s_task = tg.create_task(self._fetch_ancestor_chain(source_urn))
            t_task = tg.create_task(self._fetch_ancestor_chain(target_urn))
        s_chain, t_chain = s_task.result(), t_task.result()
        pairs = _ancestor_pairs_for_leaf(s_chain, t_chain, source_urn, target_urn)
        if not pairs:
            return

        # Distinct ancestor URN sets — used to scope the recompute query
        # so we only touch the cells we actually changed.
        s_urns = sorted({s for s, _ in pairs})
        t_urns = sorted({t for _, t in pairs})

        contribution_rows = [
            (s, t, edge_id, edge_type or "")
            for s, t in pairs
        ]

        def _txn(transaction):
            # 1. Idempotent contribution insert. INSERT OR UPDATE on the
            #    (source_urn, target_urn, contributor_id) PK is naturally
            #    a no-op if the contribution is already recorded.
            transaction.insert_or_update(
                "GraphEdgeContribution",
                columns=("source_urn", "target_urn", "contributor_id", "contributor_type"),
                values=contribution_rows,
            )

            # 2. Recompute weight + type aggregates for every pair we
            #    just touched. ARRAY_AGG(DISTINCT) collapses repeats.
            cursor = transaction.execute_sql(
                "SELECT source_urn, target_urn, COUNT(*) AS weight, "
                "       ARRAY_AGG(DISTINCT contributor_type "
                "                 IGNORE NULLS) AS types "
                "FROM GraphEdgeContribution "
                "WHERE source_urn IN UNNEST(@s) AND target_urn IN UNNEST(@t) "
                "GROUP BY source_urn, target_urn",
                params={"s": s_urns, "t": t_urns},
                param_types={
                    "s": _ParamTypes.array(_ParamTypes.STRING),
                    "t": _ParamTypes.array(_ParamTypes.STRING),
                },
            )

            # 3. Upsert AGGREGATED GraphEdge rows from the recompute.
            agg_rows: List[Tuple[str, str, str, str, str]] = []
            for row in cursor:
                s, t, weight, types = row[0], row[1], int(row[2]), list(row[3] or [])
                agg_rows.append((
                    s, t, _agg_edge_id(s, t), _AGG_LABEL,
                    _safe_json_dumps(
                        {
                            "weight": weight,
                            "source_edge_types": sorted(t_ for t_ in types if t_),
                        },
                        field="GraphEdge.properties",
                        owner_id=_agg_edge_id(s, t),
                    ),
                ))
            if agg_rows:
                transaction.insert_or_update(
                    "GraphEdge",
                    columns=("urn", "dest_urn", "edge_id", "label", "properties"),
                    values=agg_rows,
                )

        await self._execute_write(_txn, op_name="on_lineage_edge_written")

    async def on_lineage_edge_deleted(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
    ) -> None:
        """Symmetric to ``on_lineage_edge_written``: drop the contribution,
        then recompute / delete affected AGGREGATED edges in one transaction.
        """
        await self._ensure_connected()
        self._require_gql()

        async with asyncio.TaskGroup() as tg:
            s_task = tg.create_task(self._fetch_ancestor_chain(source_urn))
            t_task = tg.create_task(self._fetch_ancestor_chain(target_urn))
        s_chain, t_chain = s_task.result(), t_task.result()
        pairs = _ancestor_pairs_for_leaf(s_chain, t_chain, source_urn, target_urn)
        if not pairs:
            return

        s_urns = sorted({s for s, _ in pairs})
        t_urns = sorted({t for _, t in pairs})

        def _txn(transaction):
            # 1. Drop every contribution this leaf-edge produced. DML
            #    DELETE returns the affected row count for telemetry.
            transaction.execute_update(
                "DELETE FROM GraphEdgeContribution "
                "WHERE source_urn IN UNNEST(@s) "
                "  AND target_urn IN UNNEST(@t) "
                "  AND contributor_id = @c",
                params={"s": s_urns, "t": t_urns, "c": edge_id},
                param_types={
                    "s": _ParamTypes.array(_ParamTypes.STRING),
                    "t": _ParamTypes.array(_ParamTypes.STRING),
                    "c": _ParamTypes.STRING,
                },
            )

            # 2. Recompute remaining aggregates for the affected pairs.
            cursor = transaction.execute_sql(
                "SELECT source_urn, target_urn, COUNT(*) AS weight, "
                "       ARRAY_AGG(DISTINCT contributor_type "
                "                 IGNORE NULLS) AS types "
                "FROM GraphEdgeContribution "
                "WHERE source_urn IN UNNEST(@s) AND target_urn IN UNNEST(@t) "
                "GROUP BY source_urn, target_urn",
                params={"s": s_urns, "t": t_urns},
                param_types={
                    "s": _ParamTypes.array(_ParamTypes.STRING),
                    "t": _ParamTypes.array(_ParamTypes.STRING),
                },
            )

            surviving: Dict[Tuple[str, str], Tuple[int, List[str]]] = {}
            for row in cursor:
                s, t, weight, types = row[0], row[1], int(row[2]), list(row[3] or [])
                surviving[(s, t)] = (weight, types)

            # 3a. Upsert AGGREGATED rows that still have contributors.
            keep_rows: List[Tuple[str, str, str, str, str]] = []
            drop_pairs: List[Tuple[str, str]] = []
            for pair in pairs:
                s, t = pair
                if pair in surviving:
                    weight, types = surviving[pair]
                    keep_rows.append((
                        s, t, _agg_edge_id(s, t), _AGG_LABEL,
                        _safe_json_dumps(
                            {
                                "weight": weight,
                                "source_edge_types": sorted(t_ for t_ in types if t_),
                            },
                            field="GraphEdge.properties",
                            owner_id=_agg_edge_id(s, t),
                        ),
                    ))
                else:
                    drop_pairs.append(pair)

            if keep_rows:
                transaction.insert_or_update(
                    "GraphEdge",
                    columns=("urn", "dest_urn", "edge_id", "label", "properties"),
                    values=keep_rows,
                )

            # 3b. Delete AGGREGATED rows whose count fell to zero. DML
            #     because Spanner mutations don't accept WHERE clauses
            #     beyond the PK, and we want to guard on label = AGGREGATED.
            if drop_pairs:
                drop_s = [p[0] for p in drop_pairs]
                drop_t = [p[1] for p in drop_pairs]
                drop_ids = [_agg_edge_id(s, t) for s, t in drop_pairs]
                transaction.execute_update(
                    "DELETE FROM GraphEdge "
                    "WHERE label = @agg "
                    "  AND urn IN UNNEST(@s) "
                    "  AND dest_urn IN UNNEST(@t) "
                    "  AND edge_id IN UNNEST(@ids)",
                    params={
                        "agg": _AGG_LABEL,
                        "s": drop_s, "t": drop_t, "ids": drop_ids,
                    },
                    param_types={
                        "agg": _ParamTypes.STRING,
                        "s": _ParamTypes.array(_ParamTypes.STRING),
                        "t": _ParamTypes.array(_ParamTypes.STRING),
                        "ids": _ParamTypes.array(_ParamTypes.STRING),
                    },
                )

        await self._execute_write(_txn, op_name="on_lineage_edge_deleted")

    async def on_containment_changed(self, urn: str) -> None:
        await self._ancestor_cache.invalidate(urn, fingerprint=self._containment_fingerprint)

    async def count_aggregated_edges(self) -> int:
        await self._ensure_connected()
        rows = await self._execute_query(
            "SELECT COUNT(*) AS n FROM GraphEdge WHERE label = @agg",
            op_name="count_aggregated_edges",
            params={"agg": _AGG_LABEL},
            param_types_={"agg": _ParamTypes.STRING},
        )
        return int(rows[0]["n"]) if rows else 0

    async def purge_aggregated_edges(
        self,
        *,
        batch_size: int = 10_000,  # noqa: ARG002 — see below
        progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> int:
        """Delete every AGGREGATED edge + contribution row.

        Drops the sidecar table contents AFTER the GraphEdge purge so
        a crash mid-purge cannot leave the sidecar pointing at deleted
        AGGREGATED rows (the next ``on_lineage_edge_written`` would
        otherwise no-op against missing edges).

        ``batch_size`` is part of the ABC signature; Spanner DML accepts
        a single bulk ``DELETE WHERE TRUE`` so we don't chunk on the
        client side. Kept for cross-provider call-site compatibility.
        """
        del batch_size  # explicitly unused on this path
        await self._ensure_connected()
        deleted = 0

        def _purge_edges_txn(transaction):
            return int(transaction.execute_update(
                "DELETE FROM GraphEdge WHERE label = @agg",
                params={"agg": _AGG_LABEL},
                param_types={"agg": _ParamTypes.STRING},
            ) or 0)

        def _purge_contrib_txn(transaction):
            return int(transaction.execute_update(
                "DELETE FROM GraphEdgeContribution WHERE TRUE",
            ) or 0)

        deleted += await self._execute_write(
            _purge_edges_txn,
            op_name="purge_aggregated_edges_main",
            timeout_s=self._budget.purge_batch,
        )
        # Sidecar cleanup; failure here is logged but doesn't roll back the
        # GraphEdge purge that the user asked for. (Phase 4 will replace
        # this with a single-transaction merged delete to close the orphan
        # window — see audit BLOCKER B11.)
        try:
            await self._execute_write(
                _purge_contrib_txn,
                op_name="purge_aggregated_edges_sidecar",
                timeout_s=self._budget.purge_batch,
            )
        except Exception as exc:
            logger.warning("spanner: GraphEdgeContribution cleanup failed: %s", exc)

        if progress_callback is not None:
            try:
                await progress_callback(deleted)
            except Exception:
                pass
        return deleted

    # ----- Metadata --------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        await self._ensure_connected()
        rows_n = await self._execute_query(
            "SELECT COUNT(*) AS n FROM GraphNode",
            op_name="get_stats.nodes",
        )
        rows_e = await self._execute_query(
            "SELECT COUNT(*) AS n FROM GraphEdge",
            op_name="get_stats.edges",
        )
        return {
            "nodeCount": int(rows_n[0]["n"]) if rows_n else 0,
            "edgeCount": int(rows_e[0]["n"]) if rows_e else 0,
            "provider": "spanner",
            "graph": self._graph_name,
        }

    async def get_schema_stats(self) -> GraphSchemaStats:
        await self._ensure_connected()
        labels = await self._execute_query(
            "SELECT label, COUNT(*) AS n FROM GraphNode GROUP BY label ORDER BY n DESC",
            op_name="get_schema_stats.node_labels",
        )
        edge_labels = await self._execute_query(
            "SELECT label, COUNT(*) AS n FROM GraphEdge GROUP BY label ORDER BY n DESC",
            op_name="get_schema_stats.edge_labels",
        )
        total_nodes = sum(int(r["n"]) for r in labels)
        total_edges = sum(int(r["n"]) for r in edge_labels)
        return GraphSchemaStats(
            totalNodes=total_nodes,
            totalEdges=total_edges,
            entityTypeStats=[
                EntityTypeSummary(id=r["label"], name=r["label"], count=int(r["n"]))
                for r in labels
            ],
            edgeTypeStats=[
                EdgeTypeSummary(id=r["label"], name=r["label"], count=int(r["n"]))
                for r in edge_labels
            ],
            tagStats=[],
        )

    async def get_ontology_metadata(self) -> OntologyMetadata:
        # Return what was injected by ContextEngine (single source of truth).
        edge_meta: Dict[str, EdgeTypeMetadata] = {}
        for ty, raw in (self._resolved_edge_metadata or {}).items():
            if isinstance(raw, EdgeTypeMetadata):
                edge_meta[ty] = raw
            elif isinstance(raw, dict):
                edge_meta[ty] = EdgeTypeMetadata(**raw)
        return OntologyMetadata(
            containmentEdgeTypes=list(self._resolved_containment_types),
            lineageEdgeTypes=list(self._resolved_lineage_types),
            edgeTypeMetadata=edge_meta,
            entityTypeHierarchy={},
            rootEntityTypes=[],
        )

    async def get_distinct_values(self, property_name: str) -> List[Any]:
        await self._ensure_connected()
        # Property lookups go through the JSON column. Use a generated STORED
        # column name when present (e.g. ``level``); otherwise use JSON.
        if property_name in {"level", "qualified_name", "layer_assignment"}:
            col = property_name
            sql = f"SELECT DISTINCT {col} AS v FROM GraphNode WHERE {col} IS NOT NULL"
        else:
            # Bind property name as a path. Spanner JSON access uses dot path.
            sql = (
                "SELECT DISTINCT LAX_STRING(JSON_QUERY(properties, @path)) AS v "
                "FROM GraphNode WHERE LAX_STRING(JSON_QUERY(properties, @path)) IS NOT NULL"
            )
        params = {"path": f"$.{property_name}"} if "@path" in sql else {}
        param_types_ = {"path": _ParamTypes.STRING} if params else {}
        rows = await self._execute_query(
            sql,
            op_name="get_distinct_values",
            params=params,
            param_types_=param_types_,
        )
        return [r["v"] for r in rows if r.get("v") is not None]

    async def get_ancestors(
        self, urn: str, limit: int = 100, offset: int = 0,
    ) -> List[GraphNode]:
        await self._ensure_connected()
        self._require_gql()
        chain = await self._fetch_ancestor_chain(urn)
        if not chain:
            return []
        sliced = chain[offset : offset + limit]
        if not sliced:
            return []
        return await self.get_nodes_batch(sliced)

    async def get_descendants(
        self,
        urn: str,
        depth: int = 5,
        entity_types: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[GraphNode]:
        await self._ensure_connected()
        self._require_gql()
        ctypes = self._containment_types()
        safe_depth = max(1, min(_DEFAULT_MAX_QUANTIFIER_DEPTH, depth))
        gql = (
            f"GRAPH {self._graph_name}\n"
            f"MATCH ANY SHORTEST (root:Entity {{urn: @urn}})-[c]->{{1,{safe_depth}}}(d:Entity)\n"
            "WHERE ALL(edge IN c WHERE edge.label IN UNNEST(@ctypes))\n"
            "  AND (@types IS NULL OR d.label IN UNNEST(@types))\n"
            "RETURN DISTINCT d.urn AS urn, d.label AS label, "
            "       TO_JSON(d.properties) AS properties\n"
            f"LIMIT {_safe_int(limit, default=100, max_value=1000)} "
            f"OFFSET {_safe_int(offset, default=0, max_value=1_000_000)}"
        )
        rows = await self._execute_query(
            gql,
            op_name="get_descendants",
            params={
                "urn": urn, "ctypes": ctypes,
                "types": entity_types or None,
            },
            param_types_={
                "urn": _ParamTypes.STRING,
                "ctypes": _ParamTypes.array(_ParamTypes.STRING),
                "types": _ParamTypes.array(_ParamTypes.STRING),
            },
        )
        return [self._row_to_node(r) for r in rows]

    async def get_nodes_by_tag(
        self, tag: str, limit: int = 100, offset: int = 0,
    ) -> List[GraphNode]:
        await self._ensure_connected()
        sql = (
            "SELECT urn, label, TO_JSON(properties) AS properties "
            "FROM GraphNode "
            "WHERE EXISTS (SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(properties, @path)) t "
            "              WHERE LAX_STRING(t) = @tag) "
            f"ORDER BY urn LIMIT {_safe_int(limit, 100, 1000)} OFFSET {_safe_int(offset, 0, 1_000_000)}"
        )
        rows = await self._execute_query(
            sql,
            op_name="get_nodes_by_tag",
            params={"path": "$.tags", "tag": tag},
            param_types_={"path": _ParamTypes.STRING, "tag": _ParamTypes.STRING},
        )
        return [self._row_to_node(r) for r in rows]

    async def get_nodes_by_layer(
        self, layer_id: str, limit: int = 100, offset: int = 0,
    ) -> List[GraphNode]:
        await self._ensure_connected()
        sql = (
            "SELECT urn, label, TO_JSON(properties) AS properties "
            "FROM GraphNode "
            "WHERE layer_assignment = @layer "
            f"ORDER BY urn LIMIT {_safe_int(limit, 100, 1000)} OFFSET {_safe_int(offset, 0, 1_000_000)}"
        )
        rows = await self._execute_query(
            sql,
            op_name="get_nodes_by_layer",
            params={"layer": layer_id},
            param_types_={"layer": _ParamTypes.STRING},
        )
        return [self._row_to_node(r) for r in rows]

    # ----- Write operations ------------------------------------------------

    async def save_custom_graph(
        self, nodes: List[GraphNode], edges: List[GraphEdge],
    ) -> bool:
        await self._ensure_connected()
        if nodes:
            await self._upsert_nodes(nodes)
        if edges:
            await self._upsert_edges(edges)
        return True

    async def create_node(
        self, node: GraphNode, containment_edge: Optional[GraphEdge] = None,
    ) -> bool:
        await self._ensure_connected()
        await self._upsert_nodes([node])
        if containment_edge is not None:
            await self._upsert_edges([containment_edge])
        return True

    async def create_edge(self, edge: GraphEdge) -> bool:
        await self._ensure_connected()
        await self._upsert_edges([edge])
        return True

    async def update_edge(
        self, edge_id: str, properties: Dict[str, Any],
    ) -> Optional[GraphEdge]:
        await self._ensure_connected()

        # Read-modify-write the JSON properties column under one read-write
        # transaction so two concurrent ``update_edge`` calls cannot race
        # and lose intermediate writes. The SELECT must run on the same
        # transaction object as the UPDATE.
        patch = dict(properties or {})

        def _txn(transaction):
            cursor = transaction.execute_sql(
                "SELECT urn, dest_urn, edge_id, label, TO_JSON(properties) AS properties "
                "FROM GraphEdge WHERE edge_id = @id LIMIT 1",
                params={"id": edge_id},
                param_types={"id": _ParamTypes.STRING},
            )
            fields = [f.name for f in cursor.fields]
            row_tuple = next(iter(cursor), None)
            if row_tuple is None:
                return None
            row = {fields[i]: row_tuple[i] for i in range(len(fields))}
            existing = _decode_json(row.get("properties")) or {}
            merged = {**existing, **patch}
            transaction.execute_update(
                "UPDATE GraphEdge SET properties = JSON @props WHERE edge_id = @id",
                params={"props": merged, "id": edge_id},
                param_types={"props": _ParamTypes.JSON, "id": _ParamTypes.STRING},
            )
            return row, merged

        result = await self._execute_write(_txn, op_name="update_edge")
        if result is None:
            return None
        row, merged = result
        # The merged JSON is what we just persisted; size-bound here too
        # so a giant patch surfaces as a 400 to the API rather than as a
        # silently-truncated row in the model layer.
        return self._row_to_edge({
            **row,
            "properties": _safe_json_dumps(
                merged, field="GraphEdge.properties", owner_id=edge_id,
            ),
        })

    async def delete_edge(self, edge_id: str) -> bool:
        await self._ensure_connected()

        def _txn(transaction):
            transaction.execute_update(
                "DELETE FROM GraphEdge WHERE edge_id = @id",
                params={"id": edge_id},
                param_types={"id": _ParamTypes.STRING},
            )
        await self._execute_write(_txn, op_name="delete_edge")
        return True

    # ----- Schema discovery ------------------------------------------------

    async def discover_schema(self) -> Dict[str, Any]:
        await self._ensure_connected()
        return await self._introspector.discover()

    async def list_graphs(self) -> List[str]:
        await self._ensure_connected()
        try:
            rows = await self._execute_query(
                "SELECT property_graph_name FROM information_schema.property_graphs",
                op_name="list_graphs",
            )
            return [str(r["property_graph_name"]) for r in rows]
        except Exception:
            return []

    async def ensure_indices(
        self, entity_type_ids: Optional[List[str]] = None,
    ) -> None:
        # Baseline indexes were created in _ensure_schema_bootstrap.
        # Hook reserved for per-entity-type secondary indexes when hot
        # reads warrant them. No-op for now.
        return

    # =====================================================================
    # Internal helpers
    # =====================================================================

    async def get_nodes_batch(self, urns: List[str]) -> List[GraphNode]:
        """Batch fetch — used by Trace orchestrator. Chunks the URN list
        per ``_DEFAULT_MERGE_BATCH`` so a 50K-URN trace expansion doesn't
        slam Spanner with a single oversized array param. Audit M8."""
        if not urns:
            return []
        all_rows: List[Dict[str, Any]] = []
        for chunk in self._chunk_array(list(urns)):
            chunk_rows = await self._execute_query(
                "SELECT urn, label, TO_JSON(properties) AS properties "
                "FROM GraphNode WHERE urn IN UNNEST(@urns)",
                op_name="get_nodes_batch",
                params={"urns": chunk},
                param_types_={"urns": _ParamTypes.array(_ParamTypes.STRING)},
            )
            all_rows.extend(chunk_rows)
        return [self._row_to_node(r) for r in all_rows]

    async def _upsert_nodes(self, nodes: List[GraphNode]) -> None:
        if not nodes:
            return
        # Mutation API is fastest for bulk inserts — INSERT OR UPDATE matches MERGE.
        rows: List[Tuple[str, str, str]] = []
        for n in nodes:
            props = dict(n.properties or {})
            if n.display_name:
                props.setdefault("displayName", n.display_name)
            if n.qualified_name:
                props.setdefault("qualifiedName", n.qualified_name)
            if n.description:
                props.setdefault("description", n.description)
            if n.tags:
                props.setdefault("tags", list(n.tags))
            if n.layer_assignment:
                props.setdefault("layerAssignment", n.layer_assignment)
            if n.source_system:
                props.setdefault("sourceSystem", n.source_system)
            if n.last_synced_at:
                props.setdefault("lastSyncedAt", n.last_synced_at)
            level = self._entity_type_levels.get(n.entity_type)
            if level is not None:
                props.setdefault("level", level)
            rows.append((
                n.urn,
                n.entity_type,
                _safe_json_dumps(
                    props, field="GraphNode.properties", owner_id=n.urn,
                ),
            ))

        # Spanner accepts JSON via the .insert_or_update mutation by typing
        # the column as JSON; the sync client serialises a Python str.
        def _do():
            with self._database.batch() as batch:
                batch.insert_or_update(
                    "GraphNode",
                    columns=("urn", "label", "properties"),
                    values=rows,
                )
        await self._guard.run(
            to_thread(
                _do,
                op_name="spanner.upsert_nodes",
                read_only=False,
                attributes=self._otel_attrs("upsert_nodes", "batch"),
            ),
            op_name="upsert_nodes",
            timeout_s=self._budget.write,
        )

    async def _upsert_edges(self, edges: List[GraphEdge]) -> None:
        if not edges:
            return
        rows: List[Tuple[str, str, str, str, str]] = []
        for e in edges:
            props = dict(e.properties or {})
            if e.confidence is not None:
                props.setdefault("confidence", e.confidence)
            edge_id = e.id or f"{e.source_urn}|{e.edge_type}|{e.target_urn}"
            rows.append((
                e.source_urn, e.target_urn, edge_id,
                e.edge_type,
                _safe_json_dumps(
                    props, field="GraphEdge.properties", owner_id=edge_id,
                ),
            ))

        def _do():
            with self._database.batch() as batch:
                batch.insert_or_update(
                    "GraphEdge",
                    columns=("urn", "dest_urn", "edge_id", "label", "properties"),
                    values=rows,
                )
        await self._guard.run(
            to_thread(
                _do,
                op_name="spanner.upsert_edges",
                read_only=False,
                attributes=self._otel_attrs("upsert_edges", "batch"),
            ),
            op_name="upsert_edges",
            timeout_s=self._budget.write,
        )

    async def _fetch_ancestor_chain(self, urn: str) -> List[str]:
        """Compute or fetch from cache the containment ancestor chain."""
        cached = await self._ancestor_cache.get(urn, fingerprint=self._containment_fingerprint)
        if cached is not None:
            return cached
        ctypes = self._containment_types()
        safe_depth = _DEFAULT_MAX_QUANTIFIER_DEPTH * 2  # ancestors can be deeper
        gql = (
            f"GRAPH {self._graph_name}\n"
            f"MATCH (start:Entity {{urn: @urn}})<-[c]-{{1,{safe_depth}}}(anc:Entity)\n"
            "WHERE ALL(edge IN c WHERE edge.label IN UNNEST(@ctypes))\n"
            "RETURN DISTINCT anc.urn AS urn"
        )
        rows = await self._execute_query(
            gql,
            op_name="fetch_ancestor_chain",
            params={"urn": urn, "ctypes": ctypes},
            param_types_={
                "urn": _ParamTypes.STRING,
                "ctypes": _ParamTypes.array(_ParamTypes.STRING),
            },
        )
        chain = [str(r["urn"]) for r in rows]
        await self._ancestor_cache.set(urn, chain, fingerprint=self._containment_fingerprint)
        return chain

    # ----- Row marshalling -------------------------------------------------

    def _row_to_node(self, row: Dict[str, Any]) -> GraphNode:
        props = _decode_json(row.get("properties")) or {}
        return GraphNode(
            urn=str(row["urn"]),
            entityType=str(row.get("label") or props.get("entityType") or "unknown"),
            displayName=str(props.get("displayName") or row["urn"]),
            qualifiedName=props.get("qualifiedName"),
            description=props.get("description"),
            properties={
                k: v for k, v in props.items()
                if k not in {
                    "displayName", "qualifiedName", "description",
                    "tags", "layerAssignment", "sourceSystem",
                    "lastSyncedAt", "level",
                }
            },
            tags=list(props.get("tags") or []),
            layerAssignment=props.get("layerAssignment"),
            sourceSystem=props.get("sourceSystem"),
            lastSyncedAt=props.get("lastSyncedAt"),
        )

    def _row_to_edge(self, row: Dict[str, Any]) -> GraphEdge:
        props = _decode_json(row.get("properties")) or {}
        edge_id = row.get("id") or row.get("edge_id") or (
            f"{row['source_urn']}|{row.get('edge_type', '')}|{row['target_urn']}"
        )
        confidence = row.get("confidence")
        if confidence is None:
            confidence = props.get("confidence")
        return GraphEdge(
            id=str(edge_id),
            sourceUrn=str(row["source_urn"]),
            targetUrn=str(row["target_urn"]),
            edgeType=str(row.get("edge_type") or row.get("label") or "RELATED"),
            confidence=float(confidence) if confidence is not None else None,
            properties={k: v for k, v in props.items() if k != "confidence"},
        )

    # ----- Spanner execution ----------------------------------------------

    # ------------------------------------------------------------------
    # Substrate — the deadline boundary
    # ------------------------------------------------------------------
    # Both helpers ENFORCE a per-operation deadline; callers cannot opt
    # out. This is what makes the SpannerProvider compliant with the
    # GraphDataProvider MUST clause ("Implementations MUST bound every
    # async I/O call with a per-operation deadline"). The deadline is
    # applied in two layers:
    #   • client-side via DeadlineGuard.run (asyncio.wait_for)
    #   • server-side via the Spanner SDK timeout= kwarg (gRPC deadline)
    # so cancellation on one side bounds the work on the other.

    @staticmethod
    def _chunk_array(items: List[Any], chunk_size: int = _DEFAULT_MERGE_BATCH):
        """Yield ``items`` in fixed-size chunks for ``IN UNNEST(@arr)`` calls.

        Spanner caps query payload at ~100 MiB and array parameters at a
        practical few thousand elements; passing a single 50K-URN array
        fails at runtime with INVALID_ARGUMENT instead of degrading
        gracefully. Every user-supplied or trace-frontier-supplied URN
        list flows through this helper. Callers concatenate the per-chunk
        result lists. Audit M8.
        """
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")
        if not items:
            yield []
            return
        for i in range(0, len(items), chunk_size):
            yield items[i:i + chunk_size]

    @staticmethod
    def _assert_param_types_match(
        params: Optional[Dict[str, Any]],
        param_types_: Optional[Dict[str, Any]],
    ) -> None:
        """Reject silent param/type-key drift before it reaches Spanner.

        google-cloud-spanner has historically tolerated extra param_types
        entries; newer client versions reject them with ``Param ... not
        found``. The audit (M9) flagged ``get_aggregated_edges_between``
        as having declared ``dsts`` types even when ``params`` omitted it.
        Catching the asymmetry at the substrate boundary means the bug
        surfaces in CI rather than at the first runtime call.
        """
        p_keys = set((params or {}).keys())
        t_keys = set((param_types_ or {}).keys())
        if p_keys != t_keys:
            extra_in_types = sorted(t_keys - p_keys)
            extra_in_params = sorted(p_keys - t_keys)
            details = []
            if extra_in_types:
                details.append(f"types-only: {extra_in_types}")
            if extra_in_params:
                details.append(f"params-only: {extra_in_params}")
            raise ProviderConfigurationError(
                "Spanner param/param_types_ key sets disagree — "
                + "; ".join(details)
                + ". Build both dicts from the same source to avoid silent "
                "type-coercion bugs across client-library upgrades."
            )

    async def _execute_query(
        self,
        sql: str,
        *,
        op_name: str,
        timeout_s: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
        param_types_: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Run a SELECT SQL/GQL against a single-use snapshot.

        Both SQL and GQL go through the same ``execute_sql`` call — the
        Spanner parser detects the leading ``GRAPH <name>`` clause and
        switches modes. ``op_name`` is required so every call site has a
        distinct OTel span name and structured-log identifier; ``timeout_s``
        defaults to the configured ``query`` budget but may be overridden
        for sub-operations of a larger orchestrator.
        """
        self._assert_param_types_match(params, param_types_)
        await self._ensure_client()
        budget = timeout_s if timeout_s is not None else self._budget.query

        def _do():
            with self._database.snapshot() as snap:
                cursor = snap.execute_sql(
                    sql,
                    params=params or None,
                    param_types=param_types_ or None,
                    # Server-side gRPC deadline mirrors the asyncio budget.
                    # If wait_for fires and the seam abandons the worker,
                    # the underlying gRPC call still terminates within
                    # ``budget`` seconds — bounded thread leak.
                    timeout=budget,
                )
                fields = [f.name for f in cursor.fields]
                out: List[Dict[str, Any]] = []
                for row in cursor:
                    out.append({fields[i]: row[i] for i in range(len(fields))})
                return out

        return await self._guard.run(
            to_thread(
                _do,
                op_name=f"spanner.{op_name}",
                read_only=True,  # snapshot reads are safe to abandon
                attributes=self._otel_attrs(op_name, "query"),
            ),
            op_name=op_name,
            timeout_s=budget,
        )

    async def _execute_write(
        self,
        txn_fn: Callable[[Any], Any],
        *,
        op_name: str,
        timeout_s: Optional[float] = None,
    ) -> Any:
        """Run a read-write transaction with bounded deadline + retry.

        ``run_in_transaction`` retries Aborted internally; ``timeout_secs``
        bounds the retry budget so the inner loop cannot run longer than
        the asyncio side. Reads abandon on cancel; writes do NOT —
        committing after we've returned would be a phantom commit. The
        server-side ``timeout_secs`` therefore sets the upper bound on
        thread-leak duration in the cancelled-write case.
        """
        await self._ensure_client()
        budget = timeout_s if timeout_s is not None else self._budget.write
        return await self._guard.run(
            to_thread(
                self._database.run_in_transaction,
                txn_fn,
                # Spanner SDK kwarg name: timeout_secs (not timeout_s).
                timeout_secs=budget,
                op_name=f"spanner.{op_name}",
                read_only=False,
                attributes=self._otel_attrs(op_name, "txn"),
            ),
            op_name=op_name,
            timeout_s=budget,
        )

    def _otel_attrs(self, op_name: str, kind: str) -> Dict[str, Any]:
        """Span attributes attached to every Spanner seam call.

        Lets operators filter Cloud Trace by tenant/database without
        parsing the span name. Cheap (one dict alloc); ignored when otel
        isn't installed.
        """
        return {
            "spanner.project_id": self._project_id,
            "spanner.instance_id": self._instance_id,
            "spanner.database_id": self._database_id,
            "spanner.graph_name": self._graph_name,
            "spanner.op": op_name,
            "spanner.statement_kind": kind,
        }


# ===========================================================================
# Trace callbacks
# ===========================================================================

class _SpannerTraceCallbacks(TraceCallbacks):
    def __init__(self, provider: SpannerProvider) -> None:
        self._p = provider

    async def get_node(self, urn: str) -> Optional[GraphNode]:
        return await self._p.get_node(urn)

    async def get_nodes_batch(self, urns: List[str]) -> List[GraphNode]:
        return await self._p.get_nodes_batch(urns)

    async def get_node_level(self, entity_type: str) -> Optional[int]:
        return self._p._entity_type_levels.get(entity_type)

    async def resolve_anchor_at_level(
        self, urn: str, level: int, containment_edge_types: List[str],
    ) -> str:
        # Fast path: focus URN is already at the requested level. The
        # common case for level="auto" requests is the focus IS the
        # anchor — so resolving the chain + batch-hydrating ancestors
        # would be wasted work. One get_node call covers it.
        focus = await self._p.get_node(urn)
        if focus is not None:
            focus_level = self._p._entity_type_levels.get(focus.entity_type)
            if focus_level == level:
                return urn

        # Climb path: fetch ancestor chain, then a single batch-fetch for
        # their levels. We exclude ``urn`` from the batch since we already
        # have its node above.
        chain = await self._p._fetch_ancestor_chain(urn)
        if not chain:
            return urn
        nodes = await self._p.get_nodes_batch(chain)
        for n in nodes:
            if self._p._entity_type_levels.get(n.entity_type) == level:
                return n.urn
        return urn

    async def has_aggregated_at_level(self, urn: str, level: int) -> bool:
        # Per-hop deadline via _execute_query — the orchestrator's outer
        # wall-clock check covers the trace-wide budget; the substrate
        # ensures no individual hop hangs beyond `query` seconds.
        rows = await self._p._execute_query(
            f"GRAPH {self._p._graph_name}\n"
            "MATCH (focus:Entity {urn: @urn})-[e]->(other:Entity)\n"
            "WHERE e.label = @agg\n"
            "RETURN 1 AS found\n"
            "LIMIT 1",
            op_name="trace.has_aggregated_at_level",
            params={"urn": urn, "agg": _AGG_LABEL},
            param_types_={"urn": _ParamTypes.STRING, "agg": _ParamTypes.STRING},
        )
        return bool(rows)

    async def find_ancestor_with_lineage(
        self, urn: str, level: int, containment_edge_types: List[str],
    ) -> Optional[str]:
        chain = await self._p._fetch_ancestor_chain(urn)
        for u in chain or []:
            if await self.has_aggregated_at_level(u, level):
                return u
        return None

    async def expand_frontier(
        self,
        urns: List[str],
        *,
        direction: str,
        level: int,
        lineage_edge_types: Optional[List[str]],
        budget: int,
    ) -> List[FrontierRecord]:
        if not urns:
            return []
        if direction == "incoming":
            pattern = "(other:Entity)-[e]->(focus:Entity)"
            new_var, focus_var = "other", "focus"
        else:
            pattern = "(focus:Entity)-[e]->(other:Entity)"
            new_var, focus_var = "other", "focus"
        ltype_filter = (
            "AND e.label IN UNNEST(@ltypes)"
            if lineage_edge_types
            else "AND e.label = @agg"
        )
        safe_budget = _safe_int(budget, 1000, 50000)
        gql = (
            f"GRAPH {self._p._graph_name}\n"
            f"MATCH {pattern}\n"
            f"WHERE {focus_var}.urn IN UNNEST(@frontier)\n"
            f"  {ltype_filter}\n"
            "RETURN e.urn AS source_urn, e.dest_urn AS target_urn,\n"
            f"       e.edge_id AS edge_id, e.label AS edge_type,\n"
            "       TO_JSON(e.properties) AS properties,\n"
            f"       {new_var}.urn AS new_urn, {new_var}.label AS new_label,\n"
            f"       TO_JSON({new_var}.properties) AS new_properties\n"
            f"LIMIT {safe_budget}"
        )

        # Chunk the frontier — trace expansion can produce thousands of
        # URNs per hop. Each chunk respects the LIMIT independently; we
        # truncate the merged result back to ``safe_budget`` to preserve
        # the orchestrator's per-call contract.
        all_rows: List[Dict[str, Any]] = []
        for frontier_chunk in self._p._chunk_array(list(urns)):
            params: Dict[str, Any] = {"frontier": frontier_chunk, "agg": _AGG_LABEL}
            param_types_ = {
                "frontier": _ParamTypes.array(_ParamTypes.STRING),
                "agg": _ParamTypes.STRING,
            }
            if lineage_edge_types:
                params["ltypes"] = list(lineage_edge_types)
                param_types_["ltypes"] = _ParamTypes.array(_ParamTypes.STRING)
            chunk_rows = await self._p._execute_query(
                gql,
                op_name=f"trace.expand_frontier.{direction}",
                params=params,
                param_types_=param_types_,
            )
            all_rows.extend(chunk_rows)
            if len(all_rows) >= safe_budget:
                break
        rows = all_rows[:safe_budget]
        out: List[FrontierRecord] = []
        for r in rows:
            props = _decode_json(r.get("properties")) or {}
            out.append(FrontierRecord(
                edge_id=str(r["edge_id"]),
                source_urn=str(r["source_urn"]),
                target_urn=str(r["target_urn"]),
                new_urn=str(r["new_urn"]),
                edge_type=str(r.get("edge_type") or _AGG_LABEL),
                weight=int(props.get("weight", 1)),
                source_edge_types=list(props.get("source_edge_types") or []),
                new_node=self._p._row_to_node({
                    "urn": r["new_urn"],
                    "label": r.get("new_label"),
                    "properties": r.get("new_properties"),
                }),
            ))
        return out

    async def collect_ancestor_urns(
        self, urns: List[str], containment_edge_types: List[str],
    ) -> List[str]:
        if not urns:
            return []
        # Chunk the urn set; dedupe across chunks. Audit M8.
        seen: Set[str] = set()
        out: List[str] = []
        for chunk in self._p._chunk_array(list(urns)):
            rows = await self._p._execute_query(
                f"GRAPH {self._p._graph_name}\n"
                f"MATCH (start:Entity)<-[c]-{{1,10}}(anc:Entity)\n"
                "WHERE start.urn IN UNNEST(@urns)\n"
                "  AND ALL(edge IN c WHERE edge.label IN UNNEST(@ctypes))\n"
                "RETURN DISTINCT anc.urn AS urn",
                op_name="trace.collect_ancestor_urns",
                params={"urns": chunk, "ctypes": containment_edge_types},
                param_types_={
                    "urns": _ParamTypes.array(_ParamTypes.STRING),
                    "ctypes": _ParamTypes.array(_ParamTypes.STRING),
                },
            )
            for r in rows:
                u = str(r["urn"])
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    async def fetch_containment_edges(
        self, node_urns: List[str], containment_edge_types: List[str],
    ) -> List[GraphEdge]:
        if not node_urns:
            return []
        # Chunk the urn set. Both filter clauses (s.urn IN, t.urn IN)
        # use the same parameter, so chunking once covers both sides.
        # Edges where source and target straddle the chunk boundary are
        # returned by neither chunk; for trace use this is acceptable
        # because the orchestrator only asks for edges within a known
        # subgraph URN set, and the chunks together cover the same set.
        # Note: a single param appears in both filters; passing the
        # full node_urns array as the matching set per chunk preserves
        # straddle-edge visibility.
        all_rows: List[Dict[str, Any]] = []
        for chunk in self._p._chunk_array(list(node_urns)):
            rows = await self._p._execute_query(
                f"GRAPH {self._p._graph_name}\n"
                "MATCH (s:Entity)-[e]->(t:Entity)\n"
                "WHERE s.urn IN UNNEST(@chunk) AND t.urn IN UNNEST(@all_urns)\n"
                "  AND e.label IN UNNEST(@ctypes)\n"
                "RETURN s.urn AS source_urn, t.urn AS target_urn,\n"
                "       e.edge_id AS id, e.label AS edge_type,\n"
                "       TO_JSON(e.properties) AS properties",
                op_name="trace.fetch_containment_edges",
                params={
                    "chunk": chunk,
                    "all_urns": list(node_urns),
                    "ctypes": containment_edge_types,
                },
                param_types_={
                    "chunk": _ParamTypes.array(_ParamTypes.STRING),
                    "all_urns": _ParamTypes.array(_ParamTypes.STRING),
                    "ctypes": _ParamTypes.array(_ParamTypes.STRING),
                },
            )
            all_rows.extend(rows)
        return [self._p._row_to_edge(r) for r in all_rows]

    async def descendants_at_level(
        self,
        anchor_urn: str,
        level: int,
        containment_edge_types: List[str],
    ) -> Set[str]:
        rows = await self._p._execute_query(
            f"GRAPH {self._p._graph_name}\n"
            f"MATCH (root:Entity {{urn: @urn}})-[c]->{{1,{_DEFAULT_MAX_QUANTIFIER_DEPTH * 2}}}(d:Entity)\n"
            "WHERE ALL(edge IN c WHERE edge.label IN UNNEST(@ctypes))\n"
            "  AND d.level = @level\n"
            "RETURN DISTINCT d.urn AS urn",
            op_name="trace.descendants_at_level",
            params={"urn": anchor_urn, "ctypes": containment_edge_types, "level": level},
            param_types_={
                "urn": _ParamTypes.STRING,
                "ctypes": _ParamTypes.array(_ParamTypes.STRING),
                "level": _ParamTypes.INT64,
            },
        )
        return {str(r["urn"]) for r in rows}

    async def edges_between(
        self,
        source_urns: List[str],
        target_urns: List[str],
        edge_types: Optional[List[str]],
        *,
        use_raw_edges: bool = False,
    ) -> List[ExpandRecord]:
        if not source_urns or not target_urns:
            return []
        ltype_filter = (
            "AND e.label IN UNNEST(@types)" if edge_types
            else ("AND e.label != @agg" if use_raw_edges else "AND e.label = @agg")
        )
        gql = (
            f"GRAPH {self._p._graph_name}\n"
            "MATCH (s:Entity)-[e]->(t:Entity)\n"
            "WHERE s.urn IN UNNEST(@srcs) AND t.urn IN UNNEST(@dsts)\n"
            f"  {ltype_filter}\n"
            "RETURN e.urn AS source_urn, e.dest_urn AS target_urn,\n"
            "       e.edge_id AS edge_id, e.label AS edge_type,\n"
            "       TO_JSON(e.properties) AS properties"
        )
        # Chunk srcs; pass full target_urns per chunk. Cross-chunk edges
        # are not lost because the dst filter remains complete each call.
        rows: List[Dict[str, Any]] = []
        for srcs_chunk in self._p._chunk_array(list(source_urns)):
            params: Dict[str, Any] = {
                "srcs": srcs_chunk,
                "dsts": list(target_urns),
                "agg": _AGG_LABEL,
            }
            param_types_: Dict[str, Any] = {
                "srcs": _ParamTypes.array(_ParamTypes.STRING),
                "dsts": _ParamTypes.array(_ParamTypes.STRING),
                "agg": _ParamTypes.STRING,
            }
            if edge_types:
                params["types"] = list(edge_types)
                param_types_["types"] = _ParamTypes.array(_ParamTypes.STRING)
            rows.extend(await self._p._execute_query(
                gql,
                op_name="trace.edges_between" + (".raw" if use_raw_edges else ".agg"),
                params=params,
                param_types_=param_types_,
            ))
        out: List[ExpandRecord] = []
        for r in rows:
            props = _decode_json(r.get("properties")) or {}
            out.append(ExpandRecord(
                edge_id=str(r["edge_id"]),
                source_urn=str(r["source_urn"]),
                target_urn=str(r["target_urn"]),
                edge_type=str(r.get("edge_type") or _AGG_LABEL),
                weight=int(props.get("weight", 1)),
                source_edge_types=list(props.get("source_edge_types") or []),
            ))
        return out


# ===========================================================================
# Aggregation — Spanner-native batched implementation
# ===========================================================================
#
# Architecture (replaces the prior JSON-array-on-edge model):
#
# * ``GraphEdgeContribution(source_urn, target_urn, contributor_id,
#   contributor_type)`` is the source of truth for who contributes to
#   each AGGREGATED pair. Every row is one (leaf-edge, AGGREGATED-pair)
#   contribution. INSERT OR UPDATE on the (s, t, c) primary key is
#   naturally idempotent.
#
# * ``GraphEdge`` rows with ``label = 'AGGREGATED'`` are the user-visible
#   AGGREGATED edges. ``properties.weight`` and
#   ``properties.source_edge_types`` are recomputed from the sidecar
#   after every contribution write. The edge_id is deterministic
#   (``agg:<s>|<t>``) so the upsert key is stable.
#
# * ``on_lineage_edge_written`` and ``on_lineage_edge_deleted`` drive
#   the lifecycle in ONE read-write transaction:
#     1. Read source + target ancestor chains (cached, one batched read).
#     2. INSERT OR UPDATE / DELETE all sidecar contribution rows in
#        a single batched mutation.
#     3. SELECT the new aggregated counts + types for affected pairs in
#        one SQL.
#     4. INSERT OR UPDATE / DELETE the AGGREGATED GraphEdge rows in
#        one batched mutation.
#   Net cost: O(1) round-trips per leaf edge regardless of ancestor depth.


def _agg_edge_id(s: str, t: str) -> str:
    """Deterministic edge_id for the AGGREGATED edge between (s, t)."""
    return f"agg:{s}|{t}"


def _ancestor_pairs_for_leaf(
    s_chain: List[str],
    t_chain: List[str],
    source_urn: str,
    target_urn: str,
) -> List[Tuple[str, str]]:
    """Cross-product of ancestor chains, excluding self-loops.

    Always includes the leaf endpoints themselves (chains may be empty
    if the leaf has no ancestors yet).
    """
    s_list = list(s_chain or []) or [source_urn]
    if source_urn not in s_list:
        s_list = [source_urn] + s_list
    t_list = list(t_chain or []) or [target_urn]
    if target_urn not in t_list:
        t_list = [target_urn] + t_list
    seen: Set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    for s in s_list:
        for t in t_list:
            if s == t:
                continue
            pair = (s, t)
            if pair in seen:
                continue
            seen.add(pair)
            out.append(pair)
    return out


# ===========================================================================
# Schema introspector
# ===========================================================================

class _SpannerSchemaIntrospector(SchemaIntrospector):
    def __init__(self, provider: SpannerProvider) -> None:
        self._p = provider

    async def labels(self) -> List[str]:
        rows = await self._p._execute_query(
            "SELECT DISTINCT label FROM GraphNode",
            op_name="introspect.labels",
        )
        return [str(r["label"]) for r in rows if r.get("label")]

    async def edge_types(self) -> List[str]:
        rows = await self._p._execute_query(
            "SELECT DISTINCT label FROM GraphEdge",
            op_name="introspect.edge_types",
        )
        return [str(r["label"]) for r in rows if r.get("label")]

    async def label_property_keys(self, label: str) -> List[str]:
        # Sample a small number of nodes per label and union their JSON keys.
        rows = await self._p._execute_query(
            "SELECT properties FROM GraphNode WHERE label = @label LIMIT 50",
            op_name="introspect.label_property_keys",
            params={"label": label},
            param_types_={"label": _ParamTypes.STRING},
        )
        keys: Set[str] = set()
        for r in rows:
            p = _decode_json(r.get("properties"))
            if isinstance(p, dict):
                keys.update(p.keys())
        return sorted(keys)

    async def raw_metadata(self) -> Dict[str, Any]:
        try:
            rows = await self._p._execute_query(
                "SELECT property_graph_name, property_graph_metadata_json "
                "FROM information_schema.property_graphs "
                "WHERE property_graph_name = @name",
                op_name="introspect.raw_metadata",
                params={"name": self._p._graph_name},
                param_types_={"name": _ParamTypes.STRING},
            )
            if not rows:
                return {"propertyGraphName": self._p._graph_name, "metadata": None}
            return {
                "propertyGraphName": rows[0]["property_graph_name"],
                "metadata": _decode_json(rows[0].get("property_graph_metadata_json")),
            }
        except Exception:
            return {}


# ===========================================================================
# Helpers
# ===========================================================================

class _ParamTypesMeta(type):
    """Metaclass enabling ``_ParamTypes.STRING`` / ``_ParamTypes.INT64`` etc.
    to lazily resolve to ``spanner.param_types.STRING``.

    Importing this module must not require ``google-cloud-spanner`` to
    be installed (so tests that touch only DDL/template helpers can run
    in a minimal env). The lazy ``__getattr__`` is the entire mechanism;
    when ``spanner.param_types`` is unavailable the attribute access
    raises ``ImportError`` at the point of use, exactly when a query
    parameter type is genuinely needed.
    """

    _cache: Dict[str, Any] = {}

    def __getattr__(cls, name: str) -> Any:
        cached = cls._cache.get(name)
        if cached is not None:
            return cached
        from google.cloud import spanner  # type: ignore
        try:
            value = getattr(spanner.param_types, name)
        except AttributeError as exc:
            raise AttributeError(
                f"spanner.param_types has no attribute {name!r}"
            ) from exc
        cls._cache[name] = value
        return value


class _ParamTypes(metaclass=_ParamTypesMeta):
    """Namespace for Spanner parameter type constructors.

    Use ``_ParamTypes.STRING``, ``_ParamTypes.INT64``, ``_ParamTypes.JSON``
    etc. to reference scalar types; ``_ParamTypes.array(element_type)`` for
    homogeneous array parameters.
    """

    @classmethod
    def array(cls, element_type: Any) -> Any:
        from google.cloud import spanner  # type: ignore
        return spanner.param_types.Array(element_type)


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------

_INDEX_NAME_RE = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+|NULL_FILTERED\s+)*INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)


def _index_name_from_ddl(stmt: str) -> Optional[str]:
    """Extract the index name from a ``CREATE INDEX`` DDL statement.

    Used by the bootstrap path to filter out indexes that already exist,
    so we don't depend on the database parser accepting
    ``IF NOT EXISTS`` on every supported version. Returns None if the
    statement isn't recognisable as a CREATE INDEX.
    """
    m = _INDEX_NAME_RE.search(stmt or "")
    return m.group(1) if m else None


def _decode_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw


def _safe_int(v: Any, default: int, max_value: int) -> int:
    """Validate an integer for safe inline-formatting into a SQL/GQL string.

    Spanner's GQL cannot bind LIMIT/OFFSET parameters, so we format them
    inline. Always validate before formatting; never accept arbitrary
    callers' integers without bounds.
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    if n < 0:
        n = default
    if n > max_value:
        n = max_value
    return n


# ===========================================================================
# DDL templates
# ===========================================================================

# Owned-schema v2 (Phase 3 of the production-ready plan).
#
# Two non-trivial changes versus v1:
#
#   * ``shard INT64 AS (MOD(FARM_FINGERPRINT(urn), 256)) STORED`` is the
#     leading PK column. Spanner range-shards by PK prefix; URN
#     namespaces like ``urn:dataset:big_table:*`` cluster lexicographically
#     and funnel writes to one split. The hash-shard breaks the prefix
#     so a single-dataset ingest distributes across 256 splits. URN-by-
#     URN reads still use the unique secondary ``IDX_GraphNode_URN``
#     so lookups remain a single key seek. Audit B8.
#   * ``LABEL Entity PROPERTIES ALL COLUMNS EXCEPT (properties, shard)``
#     in the property graph DDL stops the JSON bag from being exposed
#     twice (once typed as ``properties``, once dynamic via DYNAMIC
#     PROPERTIES) and excludes the synthetic shard column from the
#     property-graph surface — it's a distribution-only column. Audit M10.
_DDL_CREATE_GRAPH_NODE = """
CREATE TABLE GraphNode (
  urn STRING(MAX) NOT NULL,
  shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(urn), 256)) STORED,
  label STRING(MAX) NOT NULL,
  properties JSON,
  level INT64 AS (LAX_INT64(properties.level)) STORED,
  qualified_name STRING(MAX) AS (LAX_STRING(properties.qualifiedName)) STORED,
  layer_assignment STRING(MAX) AS (LAX_STRING(properties.layerAssignment)) STORED,
) PRIMARY KEY (shard, urn)
""".strip()

_DDL_CREATE_GRAPH_EDGE = """
CREATE TABLE GraphEdge (
  urn STRING(MAX) NOT NULL,
  shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(urn), 256)) STORED,
  dest_urn STRING(MAX) NOT NULL,
  edge_id STRING(MAX) NOT NULL,
  label STRING(MAX) NOT NULL,
  properties JSON,
  confidence FLOAT64 AS (LAX_FLOAT64(properties.confidence)) STORED,
) PRIMARY KEY (shard, urn, dest_urn, edge_id),
  INTERLEAVE IN PARENT GraphNode ON DELETE CASCADE
""".strip()

_DDL_CREATE_INDEXES = [
    # Unique on urn alone — supports KEY(urn) in the property graph + lets
    # every WHERE urn = @u / urn IN UNNEST(@urns) lookup take a single
    # key seek instead of a 256-shard scan. STORING covers the columns
    # the hot read paths project.
    "CREATE UNIQUE INDEX IF NOT EXISTS IDX_GraphNode_URN ON GraphNode (urn) STORING (label, properties, level, qualified_name, layer_assignment)",
    "CREATE INDEX IF NOT EXISTS R_EDGE ON GraphEdge (dest_urn, urn, edge_id) STORING (label, properties)",
    "CREATE INDEX IF NOT EXISTS IDX_NODE_LABEL ON GraphNode (label)",
    "CREATE INDEX IF NOT EXISTS IDX_NODE_LEVEL ON GraphNode (level, label)",
    "CREATE INDEX IF NOT EXISTS IDX_NODE_QUALIFIED ON GraphNode (qualified_name)",
    "CREATE INDEX IF NOT EXISTS IDX_NODE_LAYER ON GraphNode (layer_assignment)",
]

# Sidecar bookkeeping table for AGGREGATED edge contributions.
# Each row records that ``contributor_id`` (a leaf-level lineage edge) is
# one of the source edges aggregated into the AGGREGATED edge between
# ``source_urn`` and ``target_urn``. INSERT OR UPDATE on the (s,t,c)
# triple is naturally idempotent; counting and edge-type aggregation
# happen with normal SQL. This replaces the prior JSON-array-on-edge
# read-modify-write model that incurred 3-5 round-trips per ancestor pair.
# v2: leading source_shard column to avoid hotspot on the source URN
# prefix; the recompute SQL constrains on (source_urn, target_urn) which
# Spanner can resolve via the secondary IDX_CONTRIB_BY_PAIR index.
_DDL_CREATE_GRAPH_EDGE_CONTRIBUTION = """
CREATE TABLE GraphEdgeContribution (
  source_urn STRING(MAX) NOT NULL,
  source_shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(source_urn), 256)) STORED,
  target_urn STRING(MAX) NOT NULL,
  contributor_id STRING(MAX) NOT NULL,
  contributor_type STRING(MAX) NOT NULL,
) PRIMARY KEY (source_shard, source_urn, target_urn, contributor_id)
""".strip()

_DDL_CREATE_CONTRIBUTION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS IDX_CONTRIB_BY_PAIR ON GraphEdgeContribution (source_urn, target_urn)",
    "CREATE INDEX IF NOT EXISTS IDX_CONTRIB_BY_CONTRIBUTOR ON GraphEdgeContribution (contributor_id)",
]


def _DDL_CREATE_PROPERTY_GRAPH(graph_name: str) -> str:
    """``EXCEPT (properties, shard)`` is the load-bearing change here:
    without EXCEPT, the JSON ``properties`` column is exposed twice (once
    as a typed property, once via ``DYNAMIC PROPERTIES``), violating the
    Spanner schemaless-graph guidance and producing duplicated keys in
    GQL ``RETURN n.properties``. Excluding ``shard`` keeps the synthetic
    distribution column out of the user-visible property surface. Audit M10.
    """
    return f"""
CREATE PROPERTY GRAPH {graph_name}
  NODE TABLES (
    GraphNode AS Entity
      KEY (urn)
      LABEL Entity PROPERTIES ALL COLUMNS EXCEPT (properties, shard)
      DYNAMIC LABEL (label)
      DYNAMIC PROPERTIES (properties)
  )
  EDGE TABLES (
    GraphEdge
      SOURCE KEY (urn) REFERENCES GraphNode (urn)
      DESTINATION KEY (dest_urn) REFERENCES GraphNode (urn)
      LABEL EntityEdge PROPERTIES ALL COLUMNS EXCEPT (properties, shard)
      DYNAMIC LABEL (label)
      DYNAMIC PROPERTIES (properties)
  )
""".strip()
