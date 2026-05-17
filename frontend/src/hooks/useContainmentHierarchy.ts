/**
 * useContainmentHierarchy - Shared hook for building containment hierarchy
 * from canvas nodes and edges using ontology-driven containment edge types.
 *
 * Extracted from HierarchyCanvas (lines 232-285) and ContextViewCanvas (lines 476-522).
 * Uses incremental updates: only processes new edges when edges are appended
 * (the common path via addGraph). Full rebuild when edges shrink or
 * containmentEdgeTypes change.
 *
 * Consumed by: HierarchyCanvas, ContextViewCanvas, GraphCanvas
 */

import { useMemo, useRef } from 'react'
import { normalizeEdgeType } from '@/store/schema'
import type { LineageNode, LineageEdge } from '@/store/canvas'

// ============================================
// Types
// ============================================

export interface UseContainmentHierarchyOptions {
  nodes: LineageNode[]
  edges: LineageEdge[]
  /**
   * Predicate that returns true if the given normalized edge type
   * represents a containment relationship. Typically from useViewIsContainmentEdge().
   */
  isContainmentEdge: (normalizedEdgeType: string) => boolean
  /**
   * Optional fingerprint that forces a full rebuild when it changes.
   * ContextViewCanvas passes `nodeEdgeFingerprint` (view ID + canvas version).
   * If not provided, the hook derives change detection from edges.length.
   */
  fingerprint?: string
}

export interface UseContainmentHierarchyResult {
  /** Map from node ID to its parent node ID (containment only). */
  parentMap: Map<string, string>
  /** Map from parent ID to array of child IDs (containment only). */
  childMap: Map<string, string[]>
  /** Nodes with no containment parent (roots in the hierarchy). */
  rootNodes: LineageNode[]
  /** O(1) node lookup by ID. */
  nodeMap: Map<string, LineageNode>
}

// ============================================
// Hook
// ============================================

export function useContainmentHierarchy({
  nodes,
  edges,
  isContainmentEdge,
  fingerprint,
}: UseContainmentHierarchyOptions): UseContainmentHierarchyResult {
  // Refs for incremental update tracking
  const prevEdgeLenRef = useRef(0)
  const prevIsContainmentRef = useRef(isContainmentEdge)
  const prevFingerprintRef = useRef(fingerprint)
  const childSetsRef = useRef(new Map<string, Set<string>>())
  const parentMapRef = useRef(new Map<string, string>())
  const childMapRef = useRef(new Map<string, string[]>())
  const childMapSourceRef = useRef<Map<string, Set<string>> | null>(null)
  const childMapKeyRef = useRef<string>('')

  const { nodeMap, childMap, parentMap } = useMemo(() => {
    const nMap = new Map(nodes.map((n) => [n.id, n]))

    // Determine whether a full rebuild is needed
    const predicateChanged = prevIsContainmentRef.current !== isContainmentEdge
    const edgesShrank = edges.length < prevEdgeLenRef.current
    const fingerprintChanged = fingerprint !== undefined && fingerprint !== prevFingerprintRef.current
    const needsFullRebuild = predicateChanged || edgesShrank || fingerprintChanged

    let cSets: Map<string, Set<string>>
    let pMap: Map<string, string>

    if (needsFullRebuild) {
      // Full rebuild into fresh maps. After building, compare content with
      // the previous parentMap and reuse the old reference if equal — the
      // canvas-version fingerprint flips on every store mutation (including
      // lineage-edge merges that don't touch containment), so a content-
      // equal rebuild would otherwise invalidate downstream memos that
      // consume `parentMap` (e.g. useEdgeProjection's bundling pass).
      const newCSets = new Map<string, Set<string>>()
      const newPMap = new Map<string, string>()
      for (const edge of edges) {
        if (!edge.source || !edge.target) continue
        if (!isContainmentEdge(normalizeEdgeType(edge))) continue
        if (!newCSets.has(edge.source)) newCSets.set(edge.source, new Set())
        newCSets.get(edge.source)!.add(edge.target)
        newPMap.set(edge.target, edge.source)
      }

      const prevPMap = parentMapRef.current
      let parentMapEqual = prevPMap.size === newPMap.size
      if (parentMapEqual) {
        for (const [k, v] of newPMap) {
          if (prevPMap.get(k) !== v) { parentMapEqual = false; break }
        }
      }

      if (parentMapEqual) {
        // Content unchanged — keep the prior refs so downstream memos
        // (parentMap-keyed bundling, layer assignment, trace filter) see
        // stable references and skip re-projection.
        pMap = prevPMap
        cSets = childSetsRef.current
      } else {
        pMap = newPMap
        cSets = newCSets
      }
    } else {
      // Incremental: reuse previous maps, only process new edges
      cSets = childSetsRef.current
      pMap = parentMapRef.current
      const startIdx = prevEdgeLenRef.current
      for (let i = startIdx; i < edges.length; i++) {
        const edge = edges[i]
        if (!edge.source || !edge.target) continue
        if (!isContainmentEdge(normalizeEdgeType(edge))) continue
        if (!cSets.has(edge.source)) cSets.set(edge.source, new Set())
        cSets.get(edge.source)!.add(edge.target)
        pMap.set(edge.target, edge.source)
      }
    }

    // Convert Sets to arrays for downstream consumers. Cache the derived
    // cMap and reuse its reference when the underlying cSets hasn't changed
    // structurally — keyed by (reference, parent-count, total-children).
    // Incremental rebuilds mutate cSets in place, so a reference match alone
    // is insufficient; we also compare the total child count to detect adds.
    let totalChildren = 0
    cSets.forEach(set => { totalChildren += set.size })
    const cMapKey = `${cSets.size}:${totalChildren}`
    let cMap: Map<string, string[]>
    if (
      childMapSourceRef.current === cSets
      && childMapKeyRef.current === cMapKey
    ) {
      cMap = childMapRef.current
    } else {
      cMap = new Map<string, string[]>()
      cSets.forEach((children, parent) => cMap.set(parent, Array.from(children)))
      childMapRef.current = cMap
      childMapSourceRef.current = cSets
      childMapKeyRef.current = cMapKey
    }

    // Update refs for next incremental pass
    prevEdgeLenRef.current = edges.length
    prevIsContainmentRef.current = isContainmentEdge
    prevFingerprintRef.current = fingerprint
    childSetsRef.current = cSets
    parentMapRef.current = pMap

    return { nodeMap: nMap, childMap: cMap, parentMap: pMap }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, isContainmentEdge, fingerprint])

  // Root nodes: those with no containment parent and not ghost type
  const rootNodes = useMemo(() => {
    return nodes.filter((n) =>
      n.id && !parentMap.has(n.id) && n.data.type !== 'ghost'
    )
  }, [nodes, parentMap])

  return { parentMap, childMap, rootNodes, nodeMap }
}
