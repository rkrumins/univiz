/**
 * usePinnedLineagePath - isolate the sub-lineage between a trace focus and
 * one or more pinned nodes.
 *
 * A trace can return a 99-hop skeleton with thousands of nodes. "Pin
 * Lineage" lets the user pick the endpoints they actually care about; this
 * hook reduces the graph to exactly the nodes/edges that lie on a directed
 * lineage path between the trace focus and any pinned node.
 *
 * Everything needed is already in the canvas store (a node can only be
 * pinned once it is on screen, i.e. already part of the merged trace
 * result), so this is pure client-side graph traversal — no API call.
 *
 * Canvas invariant (useGraphHydration.toCanvasNode/Edge): a node's `id`
 * equals its URN and an edge's `source`/`target` are URNs. So pinned URNs,
 * the focus URN, node ids and edge endpoints are all the same key space.
 */

import { useMemo } from 'react'
import { normalizeEdgeType } from '@/store/schema'
import type { LineageEdge } from '@/store/canvas'

/** Minimal edge shape the pure algorithm needs. */
export interface PathEdge {
  id: string
  source: string
  target: string
  /** True for containment/hierarchy edges (excluded from lineage paths). */
  isContainment: boolean
}

export interface PinnedPathInput {
  edges: PathEdge[]
  focusUrn: string | null
  pinnedUrns: string[]
  /** child urn -> parent urn (containment), for layout-ancestor retention. */
  containmentParent: Map<string, string>
}

export interface PinnedPathResult {
  /** Nodes lying on a directed lineage path between focus and a pin. */
  pathNodeUrns: Set<string>
  /** Lineage edge ids whose both endpoints are path nodes. */
  pathEdgeIds: Set<string>
  /**
   * Containment ancestors of path nodes. Not part of the lineage path but
   * must stay rendered so the layout engine can position the isolated
   * sub-lineage within its hierarchy.
   */
  keepForLayoutUrns: Set<string>
  /** True only when there is a focus AND at least one pin. */
  active: boolean
}

const EMPTY: PinnedPathResult = {
  pathNodeUrns: new Set(),
  pathEdgeIds: new Set(),
  keepForLayoutUrns: new Set(),
  active: false,
}

/** BFS over an adjacency map, returning every reachable node (incl. start). */
function reachable(starts: Iterable<string>, adj: Map<string, string[]>): Set<string> {
  const seen = new Set<string>()
  const queue: string[] = []
  for (const s of starts) {
    if (!seen.has(s)) {
      seen.add(s)
      queue.push(s)
    }
  }
  while (queue.length > 0) {
    const cur = queue.shift() as string
    const next = adj.get(cur)
    if (!next) continue
    for (const n of next) {
      if (!seen.has(n)) {
        seen.add(n)
        queue.push(n)
      }
    }
  }
  return seen
}

/**
 * Pure path-isolation algorithm. Keeps every node on ANY directed lineage
 * route connecting focus and a pinned node, orientation-agnostic so it
 * works whether the pins are downstream OR upstream of the focus.
 */
export function computePinnedPath(input: PinnedPathInput): PinnedPathResult {
  const { edges, focusUrn, pinnedUrns, containmentParent } = input
  if (!focusUrn || pinnedUrns.length === 0) return EMPTY

  // Directed lineage adjacency (forward) + reverse adjacency (backward).
  const fwd = new Map<string, string[]>()
  const bwd = new Map<string, string[]>()
  for (const e of edges) {
    if (e.isContainment) continue
    if (!fwd.has(e.source)) fwd.set(e.source, [])
    fwd.get(e.source)!.push(e.target)
    if (!bwd.has(e.target)) bwd.set(e.target, [])
    bwd.get(e.target)!.push(e.source)
  }

  const df = reachable([focusUrn], fwd) // focus → downstream
  const bf = reachable([focusUrn], bwd) // focus → upstream
  const dp = reachable(pinnedUrns, fwd) // pins  → downstream
  const bp = reachable(pinnedUrns, bwd) // pins  → upstream

  // A node is on a focus↔pin path if it is reachable from focus going one
  // way AND can reach a pin going the same way. Union of both orientations
  // covers downstream-pinned and upstream-pinned cases without asking the
  // caller which trace direction was used.
  const pathNodeUrns = new Set<string>()
  for (const u of df) if (bp.has(u)) pathNodeUrns.add(u)
  for (const u of bf) if (dp.has(u)) pathNodeUrns.add(u)
  pathNodeUrns.add(focusUrn)
  for (const p of pinnedUrns) pathNodeUrns.add(p)

  const pathEdgeIds = new Set<string>()
  for (const e of edges) {
    if (e.isContainment) continue
    if (pathNodeUrns.has(e.source) && pathNodeUrns.has(e.target)) {
      pathEdgeIds.add(e.id)
    }
  }

  // Retain containment ancestors of every path node so ELK can place the
  // isolated sub-lineage inside its hierarchy.
  const keepForLayoutUrns = new Set<string>()
  for (const node of pathNodeUrns) {
    let cur = containmentParent.get(node)
    while (cur && !pathNodeUrns.has(cur) && !keepForLayoutUrns.has(cur)) {
      keepForLayoutUrns.add(cur)
      cur = containmentParent.get(cur)
    }
  }

  return { pathNodeUrns, pathEdgeIds, keepForLayoutUrns, active: true }
}

/**
 * React adapter: maps canvas edges to the pure algorithm's shape (resolving
 * containment via the schema store's normalizeEdgeType + the view's
 * isContainmentEdge predicate) and memoizes on the inputs that matter.
 */
export function usePinnedLineagePath(params: {
  edges: LineageEdge[]
  isContainmentEdge: (normalizedEdgeType: string) => boolean
  focusUrn: string | null
  pinnedUrns: string[]
  containmentParent: Map<string, string>
}): PinnedPathResult {
  const { edges, isContainmentEdge, focusUrn, pinnedUrns, containmentParent } = params
  return useMemo(() => {
    if (!focusUrn || pinnedUrns.length === 0) return EMPTY
    const pathEdges: PathEdge[] = edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      isContainment: isContainmentEdge(normalizeEdgeType(e as any)),
    }))
    return computePinnedPath({
      edges: pathEdges,
      focusUrn,
      pinnedUrns,
      containmentParent,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edges, focusUrn, pinnedUrns, containmentParent, isContainmentEdge])
}
