"""Shared building blocks for GraphDataProvider implementations.

Modules in this package are consumed by FalkorDB, Neo4j, and Spanner
provider adapters to avoid triplicating algorithmic concerns:

* deadlines.DeadlineGuard       -- per-op asyncio.wait_for with structured logs
* config.ProviderEnvBudget      -- per-provider timeout env vars with defaults
* ancestor_cache.AncestorChainCache -- containment-ancestor cache (Redis + LRU)
* trace_orchestrator.TraceOrchestrator -- set-based BFS for trace_at_level / expand
* aggregation.AggregatedEdgeMaterializer -- idempotent AGGREGATED edge writer
* schema_introspection.SchemaIntrospector -- shared discover_schema heuristic

Provider adapters supply the database-specific query bodies via small
callback protocols; the algorithms live here once.
"""
