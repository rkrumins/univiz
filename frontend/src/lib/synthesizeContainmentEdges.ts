/**
 * synthesizeContainmentEdges — emit parent→child containment edges for an
 * ancestor chain + a leaf node.
 *
 * Lifted from `ContextViewCanvas.onTraceComplete` (the trace flow has been
 * doing this inline) and `useTraceAncestorHydration` (which does the same
 * over a list of orphans). One small helper means there's a single
 * synthesis recipe, easy to test, and the navigation flow doesn't have to
 * know about the trace flow.
 *
 * Input contract:
 *   `ancestorChain` is ordered root → immediate parent (the same shape
 *   `EntitySearchHit.ancestorChain` carries from the backend). The leaf is
 *   the search target.
 *
 * Edge IDs are deterministic so re-emitting the same chain produces edges
 * that dedupe cleanly through the canvas store's idempotent `addEdges`.
 */
import type { GraphEdge, GraphNode } from '@/providers/GraphDataProvider'

export function synthesizeContainmentEdges(
  ancestorChain: GraphNode[],
  leaf: GraphNode,
  containmentEdgeType: string,
): GraphEdge[] {
  if (ancestorChain.length === 0) return []
  const edges: GraphEdge[] = []
  // Walk the chain pairwise: ancestor[i] → ancestor[i+1].
  for (let i = 0; i < ancestorChain.length - 1; i++) {
    const parent = ancestorChain[i]
    const child = ancestorChain[i + 1]
    edges.push({
      id: `synth-search-${parent.urn}-${child.urn}`,
      sourceUrn: parent.urn,
      targetUrn: child.urn,
      edgeType: containmentEdgeType,
    })
  }
  // Last hop: immediate parent → leaf.
  const parent = ancestorChain[ancestorChain.length - 1]
  edges.push({
    id: `synth-search-${parent.urn}-${leaf.urn}`,
    sourceUrn: parent.urn,
    targetUrn: leaf.urn,
    edgeType: containmentEdgeType,
  })
  return edges
}
