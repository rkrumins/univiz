"""Centralized resilience & timeout configuration.

Every timeout, retry, and circuit-breaker tunable lives here so operators
can reason about the system's failure behaviour from a single file.
All values are configurable via environment variables with sensible defaults.

Timeout layering (outermost to innermost):
    HTTP middleware                       →  CircuitBreakerProxy
    (15-45s)                                 (no deadline; gate + observer)
                                          →  Provider per-operation deadline
                                             (5-15s, owned by provider)
The innermost timeout fires first by design. The proxy does not impose
a deadline because only the provider knows the right granularity (a
single query vs. an orchestration of many).
"""

import os

# ── Circuit Breaker (applies to ALL provider types) ─────────────────
# Number of consecutive failures before the breaker opens.
BREAKER_FAIL_MAX: int = int(os.getenv("PROVIDER_BREAKER_FAIL_MAX", "3"))
# Seconds the breaker stays open before allowing a single probe request.
BREAKER_RESET_TIMEOUT_SECS: int = int(os.getenv("PROVIDER_BREAKER_RESET_TIMEOUT_SECS", "30"))

# ── FalkorDB-specific query timeouts ────────────────────────────────
# Read-only Cypher queries (MATCH ... RETURN).
FALKORDB_QUERY_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_QUERY_TIMEOUT", "5"))
# get_children / get_children_with_edges per-query timeout. Larger than
# the generic 5s read default because wide containers with many lineage
# cross-edges legitimately exceed it; aligns with HTTP_TIMEOUT_GRAPH_SECS.
FALKORDB_CHILDREN_QUERY_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_CHILDREN_QUERY_TIMEOUT", "15"))
# /edges/between resolves edges among a (potentially large) URN set. The
# generic 5s read default times out on big graphs; this sits just under
# the 45s ASGI/client budgets so the DB cancels first with a clean error.
FALKORDB_EDGES_BETWEEN_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_EDGES_BETWEEN_TIMEOUT", "40"))
# Aggregated-edge projection reads can scan large URN sets; the generic
# 5s read timeout kills these on graphs with hundreds of containers.
FALKORDB_AGGREGATED_READ_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_AGGREGATED_READ_TIMEOUT_SECS", "30"))
# Soft cap on aggregated-edge result rows. When a response reaches this
# count it is flagged ``truncated=true`` so the caller can render a
# "narrow your selection" hint instead of silently showing partial data.
AGGREGATED_EDGE_RESULT_CAP: int = int(os.getenv("AGGREGATED_EDGE_RESULT_CAP", "100000"))
# Max source URNs sent to a single aggregated-edge Cypher; oversized
# requests are split and gathered. Hard upper bound at 100k is enforced
# by the provider with a 413 response.
AGGREGATED_SOURCE_URN_BATCH_SIZE: int = int(os.getenv("AGGREGATED_SOURCE_URN_BATCH_SIZE", "5000"))
# Write Cypher queries (CREATE, MERGE, UNWIND+MERGE batch ops).
# Generous default because batch MERGE operations in the aggregation
# worker can legitimately take 10-15s on large graphs.
FALKORDB_WRITE_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_WRITE_TIMEOUT", "15"))
# Startup-time operations: seed check, index creation.
# Short because these run during _ensure_connected() on the critical path.
FALKORDB_INIT_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_INIT_TIMEOUT", "3"))

# ── HTTP request timeouts (ASGI middleware, per-path) ───────────────
# Health/readiness probes — must respond fast for K8s.
HTTP_TIMEOUT_HEALTH_SECS: float = float(os.getenv("HTTP_TIMEOUT_HEALTH_SECS", "5"))
# Read-only graph queries — bounded by per-query timeouts below.
HTTP_TIMEOUT_GRAPH_SECS: float = float(os.getenv("HTTP_TIMEOUT_GRAPH_SECS", "15"))
# Trace routes (/trace/v2, /trace/expand on v1; /trace, /trace/expand on
# v2). Deep BFS traversals legitimately exceed the 15s graph budget;
# matches the frontend TRACE_MS default so neither side aborts first.
HTTP_TIMEOUT_TRACE_SECS: float = float(os.getenv("HTTP_TIMEOUT_TRACE_SECS", "60"))
# Write-heavy aggregation operations.
HTTP_TIMEOUT_AGGREGATION_SECS: float = float(os.getenv("HTTP_TIMEOUT_AGGREGATION_SECS", "45"))
# Default for all other endpoints.
HTTP_TIMEOUT_DEFAULT_SECS: float = float(os.getenv("HTTP_TIMEOUT_DEFAULT_SECS", "30"))

# ── Redis data-structure operations (caching, materialization state) ─
# Per-operation timeout for ALL Redis calls (HGET, SADD, pipeline, etc.)
# in FalkorDB provider. These are separate from graph (Cypher) queries.
FALKORDB_REDIS_OP_TIMEOUT_SECS: float = float(os.getenv("FALKORDB_REDIS_OP_TIMEOUT", "3"))
# Same for Neo4j provider's optional Redis (ancestor chain caching).
NEO4J_REDIS_OP_TIMEOUT_SECS: float = float(os.getenv("NEO4J_REDIS_OP_TIMEOUT", "3"))

# ── Dedicated Redis for non-graph operations ────────────────────────
# When set, ALL Redis caching + materialization state in FalkorDB
# provider uses this dedicated instance instead of the FalkorDB server.
# This cleanly separates graph queries (FalkorDB) from caching/state
# (dedicated Redis). Unset = falls back to FalkorDB instance (dev compat).
# CACHE_REDIS_URL: str (e.g. "redis://cache-redis:6379/0")

# ── Trace outer budget ──────────────────────────────────────────────
# Outermost deadline for a single ContextEngine.trace_* call. Per-hop
# budgets inside the FalkorDB provider derive from the remaining
# share of this number, so bumping it gives genuinely deeper traces
# more headroom. Was previously read directly as TRACE_TIMEOUT_MS in
# context_engine.py — moved here for central visibility.
TRACE_TIMEOUT_SECS: float = float(os.getenv("TRACE_TIMEOUT_SECS", "60"))

# ── Ontology introspection ──────────────────────────────────────────
# Outer timeout for the aggregate get_ontology_metadata() call (which
# issues 4-5 internal Cypher queries). Per-query timeouts fire first;
# this is a defense-in-depth backstop.
ONTOLOGY_INTROSPECTION_TIMEOUT_SECS: float = float(os.getenv("ONTOLOGY_INTROSPECTION_TIMEOUT", "8"))

# ── Scheduler & drift checks ───────────────────────────────────────
# Per-provider timeout during periodic fingerprint comparison.
# Also used by AggregationService.check_drift() and readiness check.
SCHEDULER_DRIFT_CHECK_TIMEOUT_SECS: float = float(os.getenv("SCHEDULER_DRIFT_CHECK_TIMEOUT", "5"))

# ── Event listener ──────────────────────────────────────────────────
# Timeout for Redis subscribe and per-message poll in the aggregation
# event listener background task.
EVENT_LISTENER_TIMEOUT_SECS: float = float(os.getenv("EVENT_LISTENER_TIMEOUT", "10"))

# ── Stats service (background schema/ontology refresh) ─────────────
# Per-data-source polling timeout. Size-adaptive: default for small
# graphs, extended for graphs past the large-node threshold (since
# full-graph MATCH scans on 1M+ node graphs legitimately need minutes).
STATS_POLL_TIMEOUT_SECS: float = float(os.getenv("STATS_POLL_TIMEOUT_SECS", "30"))
STATS_POLL_TIMEOUT_LARGE_SECS: float = float(os.getenv("STATS_POLL_TIMEOUT_LARGE_SECS", "600"))
STATS_POLL_LARGE_THRESHOLD: int = int(os.getenv("STATS_POLL_LARGE_THRESHOLD", "100000"))

# ── Schema / ontology in-memory Redis cache ─────────────────────────
# Short-term memoization layer for get_stats / get_ontology_metadata.
# Postgres (DataSourceStatsORM, populated by the stats service) is the
# durable source of truth. Set to 0 to disable the Redis memoization.
FALKORDB_SCHEMA_CACHE_TTL: int = int(os.getenv("FALKORDB_SCHEMA_CACHE_TTL", "300"))

# ── Cache-only read path for graph introspection endpoints ──────────
# HTTP handlers (/graph/stats, /graph/metadata/schema, /introspection,
# /metadata/ontology) read exclusively from data_source_stats when
# STATS_CACHE_STRICT_MODE=true. The stats service owns all provider
# introspection; the web tier never runs a MATCH on the critical path.
# Set to "false" to restore the legacy try-cache-then-provider fallback
# as a one-release rollback escape hatch.
STATS_CACHE_STRICT_MODE: bool = os.getenv("STATS_CACHE_STRICT_MODE", "true").lower() == "true"

# Freshness classification — fed to X-Cache-* headers and the frontend
# staleness banner. A cache entry is "fresh" when polled within this
# window (default aligns with the scheduler's 5-min default interval).
STATS_CACHE_FRESH_SECS: int = int(os.getenv("STATS_CACHE_FRESH_SECS", "300"))
# Absolute expiry: a cache row older than this is treated as missing —
# the handler refuses to serve it and falls through to synthetic-or-202.
# 7 days survives weekend outages of the stats service while ensuring
# abandoned data sources don't surface year-old numbers.
STATS_CACHE_ABSOLUTE_EXPIRY_SECS: int = int(os.getenv("STATS_CACHE_ABSOLUTE_EXPIRY_SECS", "604800"))

# Stats-service health classification — compared against
# data_source_polling_configs.last_polled_at. Emitted as
# X-Stats-Service-Status so the frontend can show a "updates paused"
# banner without needing a separate health endpoint.
STATS_SERVICE_LAGGING_THRESHOLD_SECS: int = int(os.getenv("STATS_SERVICE_LAGGING_THRESHOLD_SECS", "60"))
STATS_SERVICE_UNREACHABLE_THRESHOLD_SECS: int = int(os.getenv("STATS_SERVICE_UNREACHABLE_THRESHOLD_SECS", "600"))

# ── Discovery (pre-registration asset cache) ────────────────────────
# Cadence for the background ``run_discovery_scheduler`` coroutine.
# Each tick fans out enqueue calls for every active provider's
# list-all sentinel + every cached asset row. Lower = fresher cache,
# higher = less load. 30 minutes is the right default for a system
# whose UI users rarely need second-by-second accuracy.
DISCOVERY_REFRESH_INTERVAL_SECS: int = int(os.getenv("DISCOVERY_REFRESH_INTERVAL_SECS", "1800"))

# Dedup-claim TTL for discovery jobs. Discovery handlers complete in
# seconds (list_graphs / get_stats); the stats-poll TTL of 1200s is
# wildly oversized here and was the root cause of the "Stale for X
# minutes" regression — a stalled worker held the claim for 20 min
# before re-enqueue could happen. 90s is enough headroom for a
# legitimately slow provider call but recovers fast on stalls.
DISCOVERY_DEDUP_TTL_SECS: int = int(os.getenv("DISCOVERY_DEDUP_TTL_SECS", "90"))

# ── Insights frontend / job-poll knobs (surfaced via /admin/insights/config) ─
# Frontend reads these once at app mount via ``useInsightsConfig``;
# all values are env-driven on the backend. Changing requires a
# backend restart but no frontend rebuild.
INSIGHTS_FRONTEND_POLL_INTERVAL_MS: int = int(os.getenv("INSIGHTS_FRONTEND_POLL_INTERVAL_MS", "5000"))
INSIGHTS_FRONTEND_STALE_TIME_MS: int = int(os.getenv("INSIGHTS_FRONTEND_STALE_TIME_MS", "60000"))
INSIGHTS_JOB_POLL_INTERVAL_MS: int = int(os.getenv("INSIGHTS_JOB_POLL_INTERVAL_MS", "2000"))
INSIGHTS_JOB_MAX_RETRIES: int = int(os.getenv("INSIGHTS_JOB_MAX_RETRIES", "4"))
# UI-only "Stale" presentation threshold. The backend still classifies
# rows past STATS_CACHE_FRESH_SECS as ``stale`` (it's the read-path
# enqueue trigger), but the frontend's StatusChip suppresses the amber
# warning until ``staleness_secs`` exceeds this threshold. Default 24h
# avoids the "Stale 4m ago" false-alarm UX with the 30-min discovery
# scheduler cadence; ops can lower it for environments that need tighter
# freshness signalling.
INSIGHTS_UI_STALE_THRESHOLD_SECS: int = int(os.getenv("INSIGHTS_UI_STALE_THRESHOLD_SECS", "86400"))

# ── Insights worker / DLQ knobs ─────────────────────────────────────
# Cap on the per-provider Refresh button's fan-out — protects against
# a single click firing thousands of jobs when a provider has a long
# tail of cached assets.
INSIGHTS_MAX_PROVIDER_REFRESH: int = int(os.getenv("INSIGHTS_MAX_PROVIDER_REFRESH", "200"))

# Worker XAUTOCLAIM idle-time threshold. A pending entry must be at
# least this old before another consumer reclaims it for redelivery.
# Same value used by the periodic trim's PEL-freshness gate.
XAUTOCLAIM_MIN_IDLE_MS: int = int(os.getenv("XAUTOCLAIM_MIN_IDLE_MS", "60000"))

# Maximum redrive attempts per DLQ entry. Each successful redrive
# increments ``redrive_count`` on the new envelope; the admin endpoint
# refuses redrive past this limit so a poisoned envelope doesn't loop.
DLQ_REDRIVE_LIMIT: int = int(os.getenv("DLQ_REDRIVE_LIMIT", "3"))
