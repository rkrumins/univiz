# Strategy: User-Authored, Versioned Graphs at Enterprise Scale

> Status: Approved design. Drives the phased implementation tracked in
> "Phased delivery & verification" below. Authored from a four-team
> architecture review (storage, version-control engine, backend platform,
> frontend) reconciled against confirmed product decisions.

## Context

Today `dataviz` is a read-only data-lineage/canvas product (Solidatus-class): graphs come
from external providers (FalkorDB primary; Neo4j/Spanner/DataHub adapters), the React
frontend visualizes them, and the only "edit" affordances are skeletons
(`stagedChangesStore` is in-memory, Context-View-only, never persisted; `GraphCanvas`
`handleSave` POSTs to a throwaway endpoint).

We are adding a **fundamental, enterprise-grade capability**: users create their own
graphs **from scratch**, persisted, with a **full audit trail** (CRUD on nodes/edges
*and* per-attribute changes), **Git-like version control** (commits, branches, parallel
multi-user editing with independent three-way merge, history/diff/blame), and the
ability to derive **any number of views** from a single source graph. It must scale to
**100s of source graphs and 1000s of views**, with individual graphs reaching millions
of nodes/edges. Version history is stored at the **SQL (CloudSQL/Postgres) level as the
system-of-record / cold store**; FalkorDB (Spanner-Graph optional) is a **disposable hot
projection** for visualization. This was designed by four independent architect teams
(storage, version-control engine, backend platform, frontend) and reconciled below.

## Confirmed product decisions

1. **Git-style collaboration** — per-(user,branch) isolated working copy, explicit
   commits, three-way merge across branches. No shared live working set.
2. **Schemaless by default** — a new graph has `ontology_id = NULL` and structural-only
   validation (valid URNs, no dangling edges); user can opt-in to promote to an
   ontology-enforced `strict` graph (reusing the existing ontology gate).
3. **FalkorDB is the default hot provider**; Spanner-Graph supported via the existing
   adapter. SQL/CloudSQL is the system-of-record cold store regardless of provider.
4. **Node layout/position is versioned in the graph** — `position` is part of node
   content (hashed, diffed, merged like any attribute), with an auto-layout escape hatch.
5. **Dedicated, decoupled Graph Store DB** — the relational store for all
   nodes/edges/versions/commits/audit is a **separate database instance**
   (`GRAPH_STORE_DB_URL`), independently scaled, **NOT** the existing management DB
   (`MANAGEMENT_DB_URL`). Every write goes to this SQL store **first and durably**; the
   graph provider is a strictly downstream, eventually-consistent read projection. All
   reads/queries for graph content go via the graph provider.

## Core architecture (reconciled)

- **Three decoupled stores.** (1) **Management DB** (`MANAGEMENT_DB_URL`, unchanged) —
  users/workspaces/views/RBAC/ontologies. (2) **Graph Store DB** (`GRAPH_STORE_DB_URL`,
  NEW, separate CloudSQL instance, independently scaled) — the authoritative,
  append-only, content-addressed system-of-record for graphs/branches/commits, every
  node/edge version, audit, working-sets, **and its own outbox**. (3) **Hot graph
  provider** (FalkorDB) — a per-ref **materialized projection**, never authoritative,
  never written by users directly, rebuilt entirely from the Graph Store DB. Write path:
  API → durable write to Graph Store DB (one txn) → relay → materialization worker →
  hot provider → all graph reads go via the provider. The existing live-provider
  `ContextEngine` path for *connected/imported* graphs is left untouched; authored
  graphs use a new parallel engine. This contains blast radius and lets the graph store
  scale (partitioning, read replicas, later Spanner) without touching management OLTP.
- **Cross-DB boundary = soft references, no DB-level FKs.** `workspace_id`,
  `ontology_id`, `created_by` etc. are id strings validated at the service layer, not
  Postgres FKs (same discipline the existing `aggregation` schema already uses across
  the schema boundary). The single-transaction guarantee is preserved by **co-locating
  the outbox, audit, working-set, commit and version tables all in the Graph Store DB**
  — the durable write + audit + outbox event commit atomically there. A relay drains the
  Graph Store DB outbox to Redis; management-side projections (e.g. a view's freshness
  flag) update reactively from those events and are themselves eventually consistent.
- **Editing is strongly consistent; projected reads are eventually consistent.**
  Working-set and uncommitted reads are served from the cold store directly (bounded by
  working-set size). Only committed history is projected to the hot store; staleness is
  surfaced via the existing `DerivedResponse`/`DerivedMeta` envelope (extended with
  `ref`, `materialized_commit`, `head_commit`; status `fresh|stale|computing`), never a
  504. This is the same staleness contract already shipped for aggregated edges.
- **Git-like storage = content-addressed blobs + a partitioned 2-level Merkle manifest.**
  Adopt the manifest model (root → fixed-N partition manifests → object), backed by
  content-addressed version tables + `LIST(graph_id)` partitioning + cold
  tiering. This makes commit, diff, and checkout **O(changed objects)**, never O(graph),
  at million-node scale, with structural sharing of unchanged partitions across commits
  and branches. (We deliberately avoid a snapshot+delta-replay scheme in favor of
  the Merkle manifest — no snapshot-cadence tuning, cleaner point-in-time reads.)
- **Projection & jobs reuse the aggregation framework.** Materialization is a structural
  clone of `AggregationService`/worker: Redis Streams dispatch, checkpointed
  crash-recovery (`last_cursor`), per-graph concurrency cap, advisory-lock reservation,
  SSE progress with monotonic sequence. Per-ref hot namespace
  `g_{graph_id}__b_{branch}` / `g_{graph_id}__c_{commit}` via the existing
  `ProviderManager` cache key `(provider_id, graph_name)` — per-branch circuit breakers
  and semaphores come for free.

## Data model (new — all in the dedicated Graph Store DB)

All tables below live in the **Graph Store DB** (`GRAPH_STORE_DB_URL`), not the
management DB. A new SQLAlchemy engine + role-based pools bound to this URL are added in
`engine.py` (separate `WEB`/`JOBS`/`READONLY` pools, independent sizing); the Graph
Store DB has its **own Alembic migration lineage** (separate version table / metadata)
so it can be provisioned, migrated and scaled independently. It includes its **own**
`outbox_events` table (mirroring `OutboxEventORM`) so writes + audit + events are one
atomic local transaction.

ID = type-prefixed `uuid4().hex[:12]`; soft-delete `deleted_at/by`; immutable tables
have no `updated_at/deleted_at` (mirror `OntologyAuditLogORM` / a published
`OntologyORM`). **Deviations flagged for review:** use `JSONB` (not house Text-JSON;
Postgres-only is already enforced) for property/diff queries, and a real total-order
commit sequence (not `_now` Text) for ordering columns — both are load-bearing for
diff/partition-pruning correctness.

- `user_graphs` — `id(g_)`, `workspace_id` FK, `ontology_id` FK **nullable**,
  `schema_mode` (`schemaless|strict`), `default_branch`, `partition_count` (frozen at
  create, default 4096), `version` (optimistic lock), soft-delete + audit cols.
- `graph_refs` — `id(gref_)`, `graph_id` FK, `name`, `ref_type` (`branch|tag`),
  `commit_id`, `revision` (optimistic lock, mirrors `OntologyORM.revision`),
  `is_protected`. `UNIQUE(graph_id, name)`.
- `graph_commits` — immutable; `id(gcmt_)`, `graph_id`, `parent_ids` JSON (0/1/2),
  `merge_base_id?`, `root_manifest_hash`, `author`, `message`, `ontology_digest`,
  `delta_summary` JSONB, `committed_at`. `UNIQUE(graph_id, commit_hash)`.
- `graph_node_versions` / `graph_edge_versions` — content-addressed immutable blobs.
  `UNIQUE(graph_id, content_hash)`; node content hash **includes `position`**;
  `properties`/`tags` JSONB; `PARTITION BY LIST(graph_id)` + `HASH(node_key)`
  sub-partition for huge graphs; cold-tier flag.
- `graph_partition_manifest` — content-addressed (`manifest_hash` PK), `graph_id`,
  `partition_index` (`-1` = root), `entries` BYTEA (gzip canonical
  `(id,kind,content_hash)[]`), `entry_count`. Structural sharing across commits/branches.
- `graph_change_event` — **the single immutable audit stream** (mirrors
  `OntologyAuditLogORM` immutability + indices). Per-object **and per-attribute**
  (`attribute_path`, `old_value`, `new_value`, content-hash before/after), written at
  **stage time** (uncommitted enterprise edits are still trailed), `commit_id` stamped on
  commit. Action enum covers CRUD **and** lifecycle (`committed|branched|merged|...`).
  Indices for blame `(graph_id,object_id,created_at)`, commit, branch activity, actor.
- `graph_working_set` — Git-style per user/branch. `UNIQUE(graph_id, branch, user_id)`,
  `base_commit_id`, `status`, `ws_change_version` (coarse guard).
- `graph_working_change` — staged ops mirroring `StagedChange`: `change_type`,
  `object_kind`, `object_id` (or `staged_` temp id), `base_content_hash` (lost-update
  guard), `before_blob`/`after_blob` JSONB, `summary`, `seq`.
- `graph_merge` + `graph_merge_conflict` — merge attempt + per-conflict rows
  (`conflict_class ∈ attr|add_add|edit_delete|dangling_edge|edge_endpoint|structural`,
  base/ours/theirs, resolution).
- In the **`aggregation` schema** (decoupled like `AggregationJobORM`):
  `graph_projection_jobs` + `graph_projection_state` (mirror `AggregationJobORM`:
  `status`, `last_cursor`, `progress`, `idempotency_key`, `current_phase`,
  `last_sequence`; plus `graph_id`, `branch`, `target_commit_id`, `provider_id`,
  `target_graph_name`, `last_projected_commit_id`, `lag_seconds`);
  `graph_materializations` (`graph_id`, `commit_id`, `hot_namespace`, `status`,
  `materialized_at`).
- `views` — **extend** `ViewORM` with nullable `source_graph_id`, `source_ref`. Legacy
  views leave them NULL and behave exactly as today (surgical, no behavior migration).

## Backend services & API

New router `graphs.py` mounted `/api/v1/{ws_id}/graphs` (sibling to `graph.router`);
cursor pagination + `idempotencyKey` per existing conventions; DTOs in
`backend/common/models/graph_authoring.py` reusing `GraphNode`/`GraphEdge`.

- **`GraphAuthoringEngine`** (`graph_authoring_engine.py`) — `for_graph(ws_id, graph_id,
  branch, user, session, storage, vc)`. Working-set CRUD, `/commands/batch` apply,
  validation hook. Extract **`OntologyMutationValidator`** from `ContextEngine`
  (`_validate_node/_validate_edge`) so strict graphs reuse the exact existing gate
  *without* a live provider (ontology lives in Postgres); schemaless = structural-only.
- **`VersionControlOrchestrator`** (`vc_orchestrator.py`) — `commit/branch/merge/diff/
  blame` wrapping VC primitives in the transactional + outbox + audit shell. **Per-graph
  commit/merge serialization** via the aggregation reservation primitive
  (`pg_try_advisory_xact_lock(hashtextextended(graph_id,0))`) — working-set CRUD is NOT
  serialized (fully concurrent), only the cheap commit/ref-advance is.
- **Concurrency:** per-entity optimistic `revision` on working-set rows
  (`409 stale_entity` → FE re-fetch+replay); commit carries `expectedHeadCommitId`
  (`409 head_moved`); a conflicting concurrent commit is routed through the **same
  three-way merge flow** as a branch merge (one mechanism, not two).
- **Three-way merge:** LCA merge base over the commit DAG; Merkle-pruned per-object then
  per-attribute three-way merge; **mandatory post-merge referential-integrity +
  containment-cycle pass, re-run after human conflict resolution**. `dangling_edge` is
  **never auto-resolved**. No silent rename coalescing (urn is identity; surface a hint
  only). This is the sharpest correctness risk and is non-negotiable.
- **Eventing/audit:** the Graph Store DB write, the `graph_change_event` audit row, and
  an outbox event are committed **atomically in the Graph Store DB's own transaction**
  (its own `outbox_events` table + an `emit`-style helper reusing the existing
  `<domain>.<entity>.<verb>` contract under the already-whitelisted **`visualization`**
  domain — no `_VALID_DOMAINS` change). A dedicated **Graph Store relay**
  (`graph_outbox_relay.py`, `FOR UPDATE SKIP LOCKED` on the Graph Store JOBS pool) drains
  it to Redis Streams. This is the structural fix for cross-DB atomicity: nothing relies
  on a single txn spanning two databases. **Phase-0 critical-path dependency** for
  materialization + collaboration.
- **Materialization:** `GraphMaterializationService` + worker (clone of aggregation
  worker); consumes `visualization.*` commit events → projects commit into hot namespace
  → emits completion → views flip freshness. GC reaper drops orphaned namespaces (keep
  last-N + view-pinned).
- **RBAC:** extend `grant_repo.VALID_RESOURCE_TYPES` to `{"view","graph"}` (table
  already polymorphic — additive); creator gets implicit `editor` grant; seed
  `workspace:graph:{create,read,edit,delete,commit,branch,merge}`; endpoints use the
  existing `requires(perm, workspace="ws_id")` dependency unchanged.
- **API:** graph lifecycle; per-(user,branch) working-set node/edge CRUD + `/commands/
  batch`; `commits` create/list/get; `diff?from&to`; `nodes/{id}/blame`; `branches`
  CRUD; `merges` + `/resolution`; views extended with `sourceGraphId`/`sourceRef` +
  `PUT /views/{id}/ref`; SSE `/branches/{branch}/events` (presence ephemeral in Redis,
  history/working-set events from the durable outbox stream).

## Frontend

- Routes under existing `CanvasLayout`: `graphs`, `graphs/new`, `graphs/:graphId`,
  `graphs/:graphId/:ref` (`branch~`/`commit~` encoded). `GraphsGalleryPage` models on
  `ViewsGallery`.
- **Extend, do not fork** `GraphCanvas` (1467 lines) into an authoring mode gated by a
  new store. Reuse `GenericNode`, `EditorToolbar`, `InlineNodeEditor`, `NodePalette`,
  `useSemanticZoom`, ELK/dagre.
- New `store/graphEditorStore.ts` — persisted, per-branch, server-draft-backed
  working-set generalizing the proven `stagedChangesStore` patterns (per-target
  coalescing, undo/redo, temp-ID resolution). `stagedChangesStore` left untouched
  (still used by Context View).
- Reuse the existing `data.isPending: 'create'|'delete'|'modify'` badge contract for
  **both** editing and diff visualization (diff = synthetic working-set → zero new
  node-render code). `DiffSummaryPanel`/`MergeView` are list-driven primary;
  canvas overlay secondary with "diff-only culling" so diff is O(changes) on a
  virtualized 100k-node canvas.
- `components/versioning/*`: `VersioningBar`, `BranchPicker`, `CommitDialog`,
  `HistoryTimeline`, `DiffSummaryPanel`, `BlameInspector`, `MergeView`,
  `ConflictResolver`, `RefMovedBanner`.
- Views-from-graph: **reuse** `ViewWizard`/`ViewEditor` with additive
  `sourceGraphId`/`refBinding` (one new `SourceRefStep`: pin-to-commit vs
  float-on-branch); `viewToViewConfig` passthrough (~3 lines); `ViewConfiguration` type
  gains the two optional fields.
- Optimistic-concurrency: backend returns a **structured 409**
  (`detail:{code:'ref_moved',...}` — the shape `authFetch` already special-cases);
  `graphEditorStore.onRefMoved` → soft-locked banner → "Review & rebase" (non-destructive;
  local ops retained until commit succeeds). SSE presence via `useGraphPresence`;
  remote commits notify (toast/pill, reuse degraded-pill pattern), never silently
  rewrite mid-edit.
- **Position-versioned consequence:** `onNodeDragStop` emits debounced `move_node` ops
  into the working set; `move_node` writes `layoutedNodes` directly and **must not**
  change the ELK `layoutSignature` (else every drag relayouts 100k nodes — the single
  biggest perf footgun; covered by a regression test).

## Phased delivery & verification

Tests use existing pytest + httpx async fixtures (mirror `test_api_graph.py`,
`test_api_rbac_phase2.py`) and Vitest/RTL (`npm run test`, lint max-warnings 0).

- **Phase 0 — Foundations.** Provision the **decoupled Graph Store DB**: add
  `GRAPH_STORE_DB_URL` config, a separate engine + role pools in `engine.py`, its own
  Alembic lineage, its own `outbox_events` table + `graph_outbox_relay.py` drainer;
  extract `OntologyMutationValidator`; seed `workspace:graph:*` perms + `grant_repo`
  resource type; core-table migrations (+ dynamic per-graph partition DDL on graph
  create). *Verify:* `test_graph_store_engine.py` (writes hit Graph Store DB, isolated
  from management pool), `test_graph_outbox_relay.py` (atomic write+audit+event →
  drained → `processed` flips), `test_graph_rbac.py` grant/deny matrix,
  `test_no_new_cross_domain_joins.py` guard (no cross-DB FK/join).
- **Phase 1 — MVP (single-branch authoring).** Create graph (schemaless+strict),
  per-user working-set CRUD + `/commands/batch`, content-addressed blobs + Merkle
  manifest, `commit` on `main`, history, per-attribute `graph_change_event` audit +
  blame. Editor reads cold store (strongly consistent); views-from-graph deferred.
  Frontend editor + `CommitDialog`/`HistoryTimeline`/`BlameInspector`.
  *Verify:* create→stage→batch→commit→history roundtrip; two concurrent PATCH → one
  `409 stale_entity`; blame correctness; graphEditorStore unit tests
  (coalesce/undo/redo/temp-ID/onRefMoved); the ELK-no-relayout perf-guard test.
- **Phase 2 — Branching, merge, materialization, views-at-ref.** Branch CRUD; three-way
  merge (clean + conflict + resolution + post-merge integrity/cycle pass);
  diff; `GraphMaterializationWorker`; view `sourceRef` pinning with
  `DerivedResponse` fresh/stale/computing; SSE presence + "review & rebase".
  *Verify:* `test_graph_merge.py` (clean, conflict payload, dangling_edge never
  auto-resolved, integrity re-run after resolution); `test_graph_materialization.py`
  (commit → projection → namespace, checkpoint resume after simulated crash);
  `test_views_at_ref.py` (freshness transitions); SSE replay test; RTL `MergeView`/
  `ConflictResolver`/`RefMovedBanner`.
- **Phase 3 — Scale hardening.** Partition-DDL automation, manifest/blob GC + cold
  tiering, hot-namespace GC reaper, working-set caps + force-commit, rate limits,
  `(graph_id,ref)→namespace` resolver LRU. *Verify:* load test 100s graphs × 1000s
  views (mirror `seed_large_lineage.py`); bulkhead regression (materialization storm
  cannot drain WEB pool); staleness SLO.

## Key risks

1. **Cross-DB atomicity / no dual-write** — resolved by co-locating the write, audit
   and outbox in the Graph Store DB (one local txn); the hot provider and any
   management-side projection are eventually consistent off the relay. No code path may
   attempt a transaction spanning the two databases, and there must be no DB-level FK or
   join across the boundary (guarded by the cross-domain lint).
2. **Outbox relay gap** — the Graph Store relay is Phase-0 critical path; nothing
   downstream (materialization, collaboration) works without durable delivery.
3. **Referential integrity across merge** — object-correct merge can still yield a
   globally invalid graph; mandatory post-merge integrity + containment-cycle pass,
   re-run after resolution; never auto-resolve `dangling_edge`.
4. **Position as a versioned property** — continuous-value conflicts (accept ours/theirs
   only + auto-layout escape hatch); must not trigger ELK relayout per drag.
5. **Bulk-rewrite commit** is irreducibly O(graph) for that one commit — mitigate with
   `COPY` batch insert + forced manifest + coalesced full-rebuild projection.
6. **Hot-namespace explosion** (1000s views × commits) — GC reaper + default views to
   branch-HEAD unless explicitly pinned.
7. **JSONB / real-timestamp deviation** from house Text-JSON/`_now` style — intentional
   and load-bearing for diff/partition correctness; flag for reviewer.

## Critical files

- DB decoupling: `backend/app/db/engine.py` (add a second engine + role pools bound to
  `GRAPH_STORE_DB_URL`; pool-role template 52-90), `.env.example` /
  `docker-compose*.yml` (new graph-store instance + URL), a separate Alembic env/lineage
  for the Graph Store DB.
- Models/migrations: `backend/app/db/models.py` (audit template ~345-376;
  `OutboxEventORM` ~1436 as the graph-store outbox template; new `models_graph.py`
  bound to the Graph Store metadata), `backend/alembic/versions/`,
  `backend/app/services/aggregation/models.py`.
- Reuse: `backend/app/db/repositories/outbox_event_repo.py` (`emit`, `_VALID_DOMAINS`
  41-51), `backend/app/services/context_engine.py` (validator extraction,
  `get_ontology_digest` 175-203), `backend/app/providers/manager.py` (cache key 152,
  semaphore 122), `backend/app/services/aggregation/worker.py` + `reservation.py`
  (job/checkpoint/advisory-lock template), `backend/app/db/engine.py` (pool roles
  52-90), `backend/app/db/repositories/grant_repo.py` (22), `backend/app/api/v1/api.py`
  (router mount 151-165), `backend/app/schemas/derived.py` (envelope).
- Frontend: `frontend/src/routes.tsx`, `components/canvas/GraphCanvas.tsx`
  (onNodesChange 770-784, getValidEdgeTypes 837, handleSave 1006-1019),
  `store/canvas.ts` (`isPending` 22-23), `store/stagedChangesStore.ts` (pattern source,
  do not modify), `services/viewApiService.ts` (346-392), `types/schema.ts` (154-197),
  `components/views/ViewWizard/`.
