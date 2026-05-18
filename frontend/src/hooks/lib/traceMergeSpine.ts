/**
 * traceMergeSpine — pure helper for the ContextViewCanvas trace merge step.
 *
 * Background: /trace/v2 returns lineage participants plus containment ancestors
 * the server hydrated for hierarchy positioning. Naively adding every returned
 * node into the canvas re-parents existing canvas nodes under alien domain
 * roots (e.g. Snowflake stealing REPORTING/GOLD via the layer-assignment HARD
 * RULE). Naively dropping every ancestor leaves new lineage participants
 * floating with no path to a layer root, which makes them unassigned and
 * causes useEdgeProjection to silently drop their edges.
 *
 * The spine is the **minimum set of ancestors** we must merge so every NEW
 * lineage participant can route up to a node already on the canvas. Anything
 * outside the spine is dropped. Containment edges whose target is an existing
 * canvas node are dropped too — re-parenting existing nodes is the exact bug
 * the original code was guarding against.
 *
 * When a participant's spine cannot reach a known anchor (the entire ancestor
 * chain is novel), the topmost spine node is flagged as `unreachable`. The
 * caller is expected to merge such participants only if they have a
 * legitimate layer claim via useLayerAssignment's normal priority chain
 * (explicit assignment, instance, view config, rules, inheritance);
 * otherwise they fall out of `nodesByLayer` and don't render. This is the
 * desired behaviour — preventing trace from parking unassigned entities in
 * the focus's layer.
 */

export interface SpineInput {
  /** URN of every lineage participant (focus + upstream + downstream). */
  participantUrns: Iterable<string>
  /** parent→child containment edges from /trace/v2. */
  containmentEdges: ReadonlyArray<{ sourceUrn: string; targetUrn: string }>
  /** URNs the canvas already places (typically `displayMap` keys). */
  knownAssignedUrns: ReadonlySet<string>
}

export interface SpineResult {
  /** Ancestor URNs that must be merged to route new participants to a known anchor. */
  spineUrns: Set<string>
  /** Spine roots whose chain never reached a known anchor — informational; callers no longer use these as an assignment fallback. */
  unreachableRoots: Set<string>
}

/**
 * Walks each participant's ancestor chain (via `containmentEdges`) toward a
 * known canvas node. Intermediate ancestors join the spine; the chain stops
 * at the first known anchor. When no anchor is reached, the topmost reached
 * ancestor is flagged as unreachable.
 */
export function computeTraceMergeSpine({
  participantUrns,
  containmentEdges,
  knownAssignedUrns,
}: SpineInput): SpineResult {
  const childToParent = new Map<string, string>()
  for (const ce of containmentEdges) {
    if (!ce.sourceUrn || !ce.targetUrn) continue
    childToParent.set(ce.targetUrn, ce.sourceUrn)
  }

  const spineUrns = new Set<string>()
  const unreachableRoots = new Set<string>()
  const visitedInWalk = new Set<string>()

  for (const urn of participantUrns) {
    if (!urn || knownAssignedUrns.has(urn)) continue

    visitedInWalk.clear()
    // Seed the visited set with the participant itself so a containment cycle
    // (e.g. a↔b) terminates rather than letting the participant slip into the
    // spine as its own ancestor.
    visitedInWalk.add(urn)

    const chain: string[] = []
    let cursor: string | undefined = childToParent.get(urn)

    while (cursor && !knownAssignedUrns.has(cursor) && !visitedInWalk.has(cursor)) {
      visitedInWalk.add(cursor)
      chain.push(cursor)
      cursor = childToParent.get(cursor)
    }

    chain.forEach(u => spineUrns.add(u))

    // Reached anchor iff we exited because cursor pointed at a known node.
    // Loop exits via undefined parent or cycle hit → chain is unreachable.
    const reachedAnchor = cursor !== undefined && knownAssignedUrns.has(cursor)
    if (!reachedAnchor) {
      const topmost = chain.length > 0 ? chain[chain.length - 1] : urn
      unreachableRoots.add(topmost)
    }
  }

  return { spineUrns, unreachableRoots }
}
