# Executive Email — Synodic Competitive Positioning

> Draft for leadership. Replace every `[PLACEHOLDER: verify]` with a
> confirmed figure before sending externally. AI capabilities are framed as
> architected-for / roadmap — not shipping today — keep that framing intact.

---

**Subject:** Why Synodic wins — we don't build another catalog, we make the
catalog usable (and we beat Solidatus)

**To:** Executive Leadership
**From:** Product

---

Team,

A clear summary of where Synodic stands against the data lineage and catalog
market, and why we are positioned to win — particularly against **Solidatus**.

**The core message: we are not building a data catalog.** The market already
has excellent catalogs — OpenMetadata, DataHub, Collibra, Unity Catalog,
Dataplex. They *store* metadata. None of them makes that metadata genuinely
**consumable** — explorable for business users, traceable to column level
for engineers, and directly usable by AI agents — all from one graph, each
with the right context.

**That gap is Synodic.** We sit on top of best-in-class open-source catalogs,
ingest their metadata into a **knowledge graph**, overlay versioned business
ontologies, and make the data explorable, investigable, and useful for every
persona — and, by design, for AI agents. We don't replace your catalog. We
make it pay off.

**Why we beat Solidatus — decisively — on the things that matter:**

- **Visualization & lineage:** Solidatus is a closed, largely *manually
  modeled* lineage tool. Synodic ingests lineage from the catalogs you
  already run and renders it on an interactive canvas — column to domain,
  per persona — with no manual modeling treadmill.
- **AI-agent consumption:** Synodic's knowledge graph + normalized API is
  *architected to be consumed by AI agents and any downstream app*.
  Solidatus's modeled diagrams are not an agent substrate. This is the
  strategic differentiator of the next five years, and we own it.
- **Scalability & performance — built ground-up:** skeleton-first tracing
  (sub-100ms domain skeleton), set-based deep traversal, multi-layer
  caching, viewport virtualization, billion-node design. Solidatus is
  bounded by a tool built for modeling, not graph-scale activation.
- **Openness & cost:** Synodic leverages open-source catalogs and graph
  engines — no proprietary modeling license, no lock-in. Solidatus is
  proprietary, per-seat, and closed `[PLACEHOLDER: verify Solidatus
  pricing]`.
- **Integration:** our lineage graph is an asset other applications consume
  through a clean API. Solidatus traps lineage inside a proprietary tool.

**Cost & positioning across the field (model, not quotes — verify before
external use):**

- **OpenMetadata / DataHub** — open source; we *complement* them (we ingest
  from them, we don't compete). Managed tiers `[PLACEHOLDER: verify]`.
- **Collibra** — heavyweight proprietary governance; high per-seat TCO
  `[PLACEHOLDER: verify]`. We deliver the activation layer at a fraction of
  cost, on open foundations.
- **Solidatus** — proprietary, per-seat, closed `[PLACEHOLDER: verify]`. Our
  primary displacement target.
- **Unity Catalog / Dataplex** — strong inside Databricks / GCP, but
  consumption-priced and locked to one vendor `[PLACEHOLDER: verify]`. We
  are the neutral, cross-platform layer.

**The one-line positioning:** *"Keep your best-in-class open-source catalog.
Synodic makes the data in it explorable, investigable, and useful — for
business, for every persona, and for AI agents."*

One honest note for internal accuracy: agent-native features (NL lineage
Q&A, embeddings, MCP-style endpoints) are on the **roadmap**. What ships
today is the architecture that makes them inevitable for us and structurally
hard for catalog incumbents and a manual modeling tool like Solidatus to
match.

Happy to walk through the full industry comparison
(`docs/INDUSTRY_COMPARISON.md`) in detail.

Best,
Product
