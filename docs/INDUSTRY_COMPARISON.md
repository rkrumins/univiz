# Synodic — Industry Comparison: Data Lineage, Visualization & Catalog Activation

> **Audience:** Product, GTM, and executive leadership
> **Purpose:** Position Synodic against the data lineage / catalog / governance
> landscape, with focus on licensing cost, openness, scalability, and
> AI/agent readiness.
> **Status:** Internal analysis. Competitor pricing fields marked
> `[PLACEHOLDER: verify]` must be confirmed before any external use.

---

## 1. Executive Summary

**Synodic is not another data catalog.** The market is saturated with tools
that *store* metadata — OpenMetadata, DataHub, Collibra, Unity Catalog,
Dataplex. Synodic does something the catalogs do not: it sits **on top of
the best-in-class open-source catalogs** and makes the data already inside
them **consumable** — explorable, investigable, and genuinely useful — for
*business users*, *AI agents*, and *every persona*, each with the context
appropriate to them.

Catalogs answer "what metadata exists?" Synodic answers "what does this data
mean, where did it come from, what breaks if it changes, and how do I — or
my agent — act on it?" We ingest catalog metadata into a **knowledge graph**,
overlay **versioned business ontologies**, and expose it through an
**interactive canvas** and a **clean API surface** built from the ground up
for **scale, performance, and integration** with any downstream application.

Against **Solidatus** specifically — the closest visualization/lineage
competitor — Synodic is materially superior: open-source-leveraged (no
proprietary modeling lock-in), engineered for billion-node scale, and
architected so the lineage graph is directly consumable by AI agents rather
than trapped in a manual modeling tool.

---

## 2. Market Landscape — Two Categories, One Gap

The competitive set splits into two categories. Conflating them is the most
common analytical mistake; separating them makes Synodic's position obvious.

**Category A — Catalogs & Governance Platforms (store metadata):**
OpenMetadata, DataHub, Collibra, Unity Catalog, Dataplex. Their job is
ingestion, cataloging, governance, and policy. They are *sources of truth
for metadata*.

**Category B — Lineage / Modeling Tools (draw lineage):** Solidatus. Its job
is to model and visualize data flow, largely through manual or
semi-automated modeling.

**The gap both categories leave:** none of them turns the metadata they hold
into something a *business user* can navigate intuitively, a *data engineer*
can trace to column level, and an *AI agent* can consume programmatically —
from the same graph, with per-persona context.

**Synodic occupies that gap.** It is a **consumption and activation layer**
that sits *above* Category A (complement, not replacement — keep your
catalog) and *outclasses* Category B on scale, openness, and AI readiness.

```
        Business users · Analysts · Engineers · GRC · AI Agents
                              ▲
                              │  per-persona context, interactive,
                              │  API + agent-consumable
                   ┌──────────────────────┐
                   │       SYNODIC         │  ← consumption / activation layer
                   │  Knowledge graph +    │
                   │  versioned ontology   │
                   └──────────────────────┘
                              ▲
        ┌──────────┬──────────┼──────────┬──────────┐
   OpenMetadata  DataHub   Collibra   Unity Cat.  Dataplex   ← catalogs (Category A)
        (we ingest from / sit on top of these — we do not replace them)
```

---

## 3. Per-Competitor Deep Dive

> Cost figures are model descriptions, not quotes. Hard numbers are marked
> `[PLACEHOLDER: verify]` and must be confirmed before external use.

### 3.1 OpenMetadata

- **What it is:** Open-source (Apache 2.0) metadata catalog — ingestion,
  discovery, governance, basic column-level lineage.
- **Cost & licensing:** Free OSS self-hosted; managed offering via Collate
  at `[PLACEHOLDER: verify Collate pricing]`. Cost is infrastructure +
  operations, not license.
- **Capabilities & limits:** Strong, broad ingestion connectors and a solid
  governance model. Lineage UI is functional but static and shallow for deep
  investigation; not designed for business-user exploration or programmatic
  agent consumption at graph scale.
- **Synodic's UVP vs it:** *Complement, not compete.* OpenMetadata is an
  excellent best-in-class open-source catalog to **sit on top of**. Synodic
  ingests its metadata into a knowledge graph and makes it explorable,
  contextual per persona, and agent-consumable — the activation layer
  OpenMetadata itself does not provide.

### 3.2 DataHub

- **What it is:** Open-source (Apache 2.0) metadata platform; rich entity
  model, column-level lineage, GraphQL API. Managed via Acryl Data.
- **Cost & licensing:** Free OSS self-hosted (non-trivial ops: Kafka, search,
  graph store); Acryl managed at `[PLACEHOLDER: verify Acryl pricing]`.
- **Capabilities & limits:** Best-in-class ingestion breadth and metadata
  model. Visualization is technical and DAG-oriented; multi-persona business
  context and interactive large-graph exploration are not its focus.
- **Synodic's UVP vs it:** *Complement, not compete.* Synodic has a
  **first-class DataHub provider** (`backend/graph/adapters/datahub_provider.py`)
  and normalizes to DataHub-aligned URNs (`urn:li:dataset:...`). We make the
  DataHub graph consumable for business users and agents — without asking
  anyone to leave DataHub.

### 3.3 Collibra

- **What it is:** Proprietary enterprise data governance/catalog suite —
  governance workflows, stewardship, policy, business glossary.
- **Cost & licensing:** Proprietary, enterprise/per-seat + platform fees,
  typically six-/seven-figure annual contracts.
  `[PLACEHOLDER: verify Collibra list/seat pricing]`. High TCO; significant
  lock-in.
- **Capabilities & limits:** Deep governance and stewardship. Lineage and
  interactive visualization are weaker relative to its governance strength;
  closed model limits programmatic/agent consumption and forces a single
  expensive platform.
- **Synodic's UVP vs it:** Synodic does not replicate Collibra's governance
  bureaucracy and does not carry its licensing weight. We deliver the
  exploration/activation layer at a fraction of TCO, on open-source
  foundations, and remain consumable by AI agents — where Collibra's closed
  stack is not.

### 3.4 Solidatus  *(primary head-to-head competitor)*

- **What it is:** Proprietary data lineage **modeling and visualization**
  tool. Lineage is largely built/maintained through manual or
  semi-automated modeling.
- **Cost & licensing:** Proprietary, commercial enterprise licensing
  (per-seat / subscription). `[PLACEHOLDER: verify Solidatus list/seat
  pricing]`. Closed source; modeling lock-in.
- **Capabilities & limits:** Polished lineage modeling and visualization for
  governance/regulatory mapping. But: (1) closed and proprietary — no
  open-source leverage; (2) modeling-centric — effort scales with manual
  upkeep; (3) not architected as a knowledge graph for programmatic AI/agent
  consumption; (4) scalability and performance are bounded by a tool built
  for modeling, not billion-node graph activation.
- **Synodic's UVP vs it (decisive):**
  - **Openness/cost:** Synodic leverages best-in-class OSS catalogs and graph
    engines (FalkorDB/Neo4j/DataHub) — no proprietary modeling license, no
    lock-in.
  - **Scale & performance, ground-up:** skeleton-first Trace v2 (<100ms
    domain skeleton), set-based BFS Trace Orchestrator, Redis + in-process
    singleflight caching, ancestor-chain cache, viewport virtualization,
    billion-node design. Solidatus is not engineered for this class of
    graph scale.
  - **AI/agent consumption:** Synodic's knowledge-graph + normalized API is
    *architected to be consumed by AI agents and any downstream app*.
    Solidatus's modeled diagrams are not an agent-consumable substrate.
  - **Multi-persona:** one graph, business + technical context (and
    agent-facing API). Solidatus is a specialist modeling tool, not a
    multi-persona activation layer.
  - **Conclusion:** For data visualization, lineage, and making data
    accessible to AI agents, **Synodic is materially superior to
    Solidatus.**

### 3.5 Unity Catalog (Databricks)

- **What it is:** Governance/catalog layer for the Databricks lakehouse
  (also an open-sourced core). Lineage and governance scoped to the
  Databricks ecosystem.
- **Cost & licensing:** OSS core available; the valuable, managed experience
  is consumption-priced and effectively coupled to Databricks spend.
  `[PLACEHOLDER: verify Databricks/Unity commercial terms]`. Ecosystem
  lock-in.
- **Capabilities & limits:** Strong inside Databricks; weak as a
  cross-platform, vendor-neutral lineage/visualization/activation layer.
  Not built for heterogeneous estates or business-user exploration.
- **Synodic's UVP vs it:** Synodic is **backend-agnostic and
  cross-platform** — it spans estates Unity cannot, and turns that whole
  picture into a consumable, per-persona, agent-ready graph without
  Databricks lock-in.

### 3.6 Dataplex (Google Cloud)

- **What it is:** GCP's data governance/catalog/quality service; lineage and
  governance centered on the Google Cloud estate.
- **Cost & licensing:** Cloud consumption pricing; cost scales with usage
  and is tied to GCP. `[PLACEHOLDER: verify Dataplex pricing]`. Cloud
  lock-in.
- **Capabilities & limits:** Good within GCP; not a vendor-neutral,
  cross-cloud, multi-persona exploration/activation layer; not designed as
  an agent-consumable knowledge graph across heterogeneous sources.
- **Synodic's UVP vs it:** Same as Unity — Synodic is the **neutral
  activation layer** across clouds and catalogs, not bound to one provider's
  consumption meter.

---

## 4. Licensing & Total Cost of Ownership

| Tool | Licensing model | Cost driver | Lock-in |
|---|---|---|---|
| **Synodic** | App layer on **OSS** substrate (FalkorDB/Neo4j/DataHub) | Infra + ops only | **None** — pluggable backends |
| OpenMetadata | OSS (Apache 2.0) / managed | Ops; managed `[PLACEHOLDER]` | Low |
| DataHub | OSS (Apache 2.0) / managed | Ops; Acryl `[PLACEHOLDER]` | Low |
| Collibra | Proprietary enterprise | Per-seat + platform `[PLACEHOLDER]` | **High** |
| Solidatus | Proprietary enterprise | Per-seat / subscription `[PLACEHOLDER]` | **High** |
| Unity Catalog | OSS core / Databricks-coupled | Databricks consumption `[PLACEHOLDER]` | **High (Databricks)** |
| Dataplex | Cloud consumption | GCP usage `[PLACEHOLDER]` | **High (GCP)** |

**TCO narrative.** Synodic's cost advantage is structural, not promotional:

- **No catalog re-platform.** We sit on the catalog you already run. No
  migration project, no parallel system, no duplicated catalog license.
- **No proprietary modeling/governance license** (vs Solidatus, Collibra).
- **Runs on existing infrastructure** via the pluggable provider model — no
  forced backend, no cloud-meter lock-in (vs Unity, Dataplex).
- **Open-source leverage** means our cost floor is infrastructure +
  operations, while Category B competitors carry per-seat license cost that
  grows with adoption — exactly when value should compound, not cost.

---

## 5. Architecture Differentiation

Synodic is a **consumption layer on top of best-in-class open-source
catalogs**, not another catalog. Concretely (grounded in the codebase):

- **Pluggable provider model** — one `GraphDataProvider` interface; FalkorDB,
  Neo4j, DataHub, Spanner, Mock (`backend/common/interfaces/provider.py`,
  `backend/graph/adapters/`). Swap backends without rewriting lineage logic.
- **Catalog activation, not catalog storage** — CatalogItem/DataSource
  abstraction binds external catalogs into governed, explorable data products
  (`docs/ARCHITECTURE.md`).
- **Knowledge graph core** — URN normalization (`urn:li:dataset:...`,
  DataHub-aligned) unifies heterogeneous sources into one graph.
- **Versioned ontologies** with evolution policies (reject / deprecate /
  migrate) and three-layer resolution (`docs/DATA_ARCHITECTURE.md`) —
  semantic governance without code changes.
- **Context per persona** — the same graph is rendered with business labels,
  technical column-level URNs, or served raw via API to an agent.

---

## 6. Scalability & Performance (built ground-up)

| Capability | How Synodic does it (in code) | Typical competitor approach |
|---|---|---|
| Fast first paint | Trace v2 skeleton-first, <100ms domain skeleton | Full DAG computation before render |
| Deep lineage | Set-based BFS Trace Orchestrator, depth-99, per-hop parallel `asyncio.gather` | Bounded depth / precomputed views |
| Large graphs | Viewport virtualization (`MAX_VISIBLE_NODES`, ghost nodes), ELK layout in Web Worker | Static images / pagination-only |
| Repeat queries | Redis + in-process singleflight hybrid cache, generation-based invalidation | Per-request recompute |
| Hot-path traversal | Ancestor-chain cache (Redis hash, O(1) batch reads) | Repeated graph queries |
| Billion-node target | Sparse-matrix / GraphBLAS design (`SPEC.md`) | Not a design goal for modeling tools |

This is the decisive contrast with Solidatus: Synodic was engineered from
the ground up for graph scale, performance, and *serving data to other
systems* — not for hand-built modeling diagrams.

---

## 7. Visualization & Lineage UX

- **Interactive Figma-like canvas** (`frontend/src/components/canvas/`) —
  pan, zoom, trace, filter in real time vs static reports/DAGs.
- **Multi-granularity zoom** — column → table → domain, server-side edge
  roll-up (aggregated edges with badge counts).
- **Per-persona context** — business view, technical view, and agent/API
  view from one graph. Competitors are single-audience (technical catalogs)
  or single-purpose (Solidatus modeling).

---

## 8. AI / Agent Readiness

**Honest framing:** Synodic does not ship an LLM/agent feature *today*. It is
**architected to be the ideal substrate for AI agents** — and that is the
strategic differentiator:

- A **knowledge graph** is the representation agents need — entities,
  relationships, lineage, and meaning, not rows or static diagrams.
- **URN normalization + versioned ontology** give agents stable identifiers
  and semantics across heterogeneous catalogs.
- **Clean, stateless API surface** (Graph Service, port 8001 discovery;
  REST graph/trace endpoints) is directly callable by an agent or any app.

**Roadmap (forward-looking, not shipped):** retrieval/embeddings over the
graph, MCP-style agent endpoints, natural-language lineage Q&A, automated
impact summaries. None of the catalog incumbents — and certainly not a
manual modeling tool like Solidatus — is structurally positioned to deliver
agent-native lineage the way a knowledge-graph-first platform is.

---

## 9. Integration & Extensibility

Synodic is built so the data is **available for any app, persona, or agent
you want to integrate**: backend-agnostic providers, a stateless discovery
service, and a clean REST surface over the graph and trace engine. The
lineage graph is an asset other systems consume — not a closed diagram
trapped in a proprietary tool.

---

## 10. Consolidated Comparison Matrix

| | Category | Licensing / cost | Lineage depth | Visualization / UX | Multi-persona context | AI/agent consumable | Openness / lock-in |
|---|---|---|---|---|---|---|---|
| **Synodic** | Activation layer | OSS substrate; infra+ops only | Column→domain, depth-99 | Interactive canvas | **Business + technical + API/agent** | **Architected for it** | **Open, pluggable** |
| OpenMetadata | Catalog (OSS) | Free / managed `[PH]` | Column (functional) | Static | Technical | Limited | Open |
| DataHub | Catalog (OSS) | Free / managed `[PH]` | Column (strong) | Technical DAG | Technical | API, not agent-tuned | Open |
| Collibra | Governance (proprietary) | Enterprise `[PH]` | Moderate | Governance UI | Steward-focused | Limited (closed) | High |
| **Solidatus** | Lineage modeling (proprietary) | Per-seat `[PH]` | Modeled | Polished, manual | Single-purpose | **No** | High |
| Unity Catalog | Catalog (Databricks) | Consumption `[PH]` | Databricks-scoped | Basic | Technical | Limited | High (Databricks) |
| Dataplex | Catalog (GCP) | Consumption `[PH]` | GCP-scoped | Basic | Technical | Limited | High (GCP) |

`[PH]` = `[PLACEHOLDER: verify]`.

---

## 11. Head-to-Head: Synodic vs Solidatus

| Dimension | Solidatus | **Synodic** |
|---|---|---|
| Foundation | Proprietary modeling tool | OSS-leveraged, knowledge-graph platform |
| Lineage | Manual / semi-automated modeling | Ingested from catalogs into a graph, traced to column level |
| Visualization | Polished but modeling-bound | Interactive canvas, multi-granularity, per-persona |
| Scale/performance | Bounded by modeling design | Ground-up: skeleton-first, billion-node design |
| AI/agent consumption | Not a substrate for agents | Architected to be agent-consumable |
| Integration | Closed | Open API; data available for any app |
| Cost/lock-in | Per-seat license, lock-in `[PH]` | Infra+ops; no modeling license; no lock-in |

**Bottom line:** For data visualization, lineage, and making data accessible
for AI-agent consumption, Synodic is **far more capable and far less
encumbered** than Solidatus — because it was built ground-up for scale,
performance, and serving data to any application, while Solidatus is a
closed, manually-modeled, single-purpose tool.

---

## 12. Recommendation

Position and sell Synodic as the **catalog activation layer**: "Keep your
best-in-class open-source catalog. Synodic makes the data in it explorable,
investigable, and useful — for business, for every persona, and for AI
agents." Lead competitive displacement efforts against **Solidatus** on
openness, ground-up scalability, and AI-agent readiness; position as a
**complement** (not a rip-and-replace) alongside OpenMetadata/DataHub and a
**neutral, cross-platform alternative** to Collibra/Unity/Dataplex lock-in.

---

## Appendix — Source of Claims & Verification List

**Synodic claims are grounded in the repository:**

- Pluggable providers: `backend/common/interfaces/provider.py`,
  `backend/graph/adapters/`, `backend/app/providers/`
- Catalog abstraction: `docs/ARCHITECTURE.md`, `docs/OVERVIEW.md`
- Knowledge graph / URN / ontology: `docs/DATA_ARCHITECTURE.md`
- Scalability: `backend/common/providers/trace_orchestrator.py`,
  `backend/app/services/graph_cache.py`, `SPEC.md`
- Canvas/persona UX: `frontend/src/components/canvas/`
- AI status: **no AI/LLM/agent code present today** — framed as
  architected-for-AI and roadmap only.

**`[PLACEHOLDER: verify]` items to confirm before any external use:**

1. OpenMetadata / Collate managed pricing
2. DataHub / Acryl managed pricing
3. Collibra list & per-seat pricing
4. Solidatus list & per-seat / subscription pricing
5. Unity Catalog / Databricks commercial terms
6. Dataplex (GCP) pricing
