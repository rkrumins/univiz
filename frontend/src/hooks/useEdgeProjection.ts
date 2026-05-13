/**
 * useEdgeProjection - Extracted from ReferenceModelCanvas.tsx
 *
 * Encapsulates:
 * - lineageEdges: aggregated + expanded detailed + trace/regular edges
 * - visibleLineageEdges: edge projection/roll-up to visible ancestors
 *
 * Phase 5.1: ancestorMap is built incrementally — on expand/collapse only
 * the changed subtree is patched, avoiding a full O(N) traversal on every
 * user interaction. Full rebuild only happens when nodesByLayer changes
 * (layer re-assignment, initial load).
 */

import { useMemo, useRef } from 'react'
import { normalizeEdgeType } from '@/store/schema'
import type { HierarchyNode } from '@/types/hierarchy'

// ============================================
// Types
// ============================================

export interface UseEdgeProjectionOptions {
  edges: any[]
  aggregatedEdges: Map<string, any>
  nodesByLayer: Map<string, HierarchyNode[]>
  expandedNodes: Set<string>
  displayFlat: HierarchyNode[]
  displayMap: Map<string, HierarchyNode>
  urnToIdMap: Map<string, string>
  showLineageFlow: boolean
  isTracing: boolean
  traceContextSet: Set<string>
  isContainmentEdge: (edgeType: string) => boolean
  /** Currently hovered node — expanded parents show edges on hover */
  hoveredNodeId?: string | null
  /**
   * URN-pair keys (`${sourceUrn}->${targetUrn}`) for parent AGGREGATED edges
   * that have been drilled into and currently have at least one finer-level
   * edge visible. Suppresses the parent AGG so the canvas doesn't render the
   * same lineage twice (rolled-up + detailed). Restored automatically when
   * either endpoint is collapsed. Only consulted in trace mode.
   */
  suppressedAggEdgeKeys?: Set<string>
}

// ============================================
// Tree helpers for incremental ancestorMap updates
// ============================================

/** Build a flat id→node lookup from the full tree. O(N) once, then O(1) per lookup. */
function buildNodeIndex(nodesByLayer: Map<string, HierarchyNode[]>): Map<string, HierarchyNode> {
  const index = new Map<string, HierarchyNode>()
  const stack: HierarchyNode[] = []
  nodesByLayer.forEach(roots => { for (let i = roots.length - 1; i >= 0; i--) stack.push(roots[i]) })
  while (stack.length > 0) {
    const node = stack.pop()!
    index.set(node.id, node)
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i])
  }
  return index
}

/** Map a node and all its descendants to `anchor` in the given map. Iterative. */
function collapseSubtreeInMap(root: HierarchyNode, anchor: string, map: Map<string, string>) {
  const stack: HierarchyNode[] = [root]
  while (stack.length > 0) {
    const node = stack.pop()!
    if (node.urn) map.set(node.urn, anchor)
    map.set(node.id, anchor)
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i])
  }
}

/**
 * When `node` is expanded, each direct child becomes visible and maps to
 * itself. If a child is already expanded, process its children too.
 * If a child is collapsed, all its descendants roll up to it. Iterative.
 */
function expandNodeInMap(node: HierarchyNode, expandedNodes: Set<string>, map: Map<string, string>) {
  const stack: HierarchyNode[] = [node]
  while (stack.length > 0) {
    const current = stack.pop()!
    for (const child of current.children) {
      if (child.urn) map.set(child.urn, child.id)
      map.set(child.id, child.id)
      if (expandedNodes.has(child.id)) {
        stack.push(child)
      } else {
        for (const gc of child.children) collapseSubtreeInMap(gc, child.id, map)
      }
    }
  }
}

/** Full O(N) build. Called on initial load and whenever nodesByLayer changes. Iterative. */
function buildFullAncestorMap(
  nodesByLayer: Map<string, HierarchyNode[]>,
  expandedNodes: Set<string>,
  displayFlat: HierarchyNode[],
): Map<string, string> {
  const map = new Map<string, string>()

  const stack: Array<{ node: HierarchyNode; anchor: string }> = []
  nodesByLayer.forEach(roots => {
    for (let i = roots.length - 1; i >= 0; i--) stack.push({ node: roots[i], anchor: roots[i].id })
  })

  while (stack.length > 0) {
    const { node, anchor } = stack.pop()!
    if (node.urn) map.set(node.urn, anchor)
    map.set(node.id, anchor)

    let childAnchor = anchor
    if (node.id === anchor) {
      childAnchor = expandedNodes.has(node.id) ? 'USE_CHILD_ID' : node.id
    }

    for (let i = node.children.length - 1; i >= 0; i--) {
      const child = node.children[i]
      stack.push({ node: child, anchor: childAnchor === 'USE_CHILD_ID' ? child.id : childAnchor })
    }
  }

  // Safety pass: visible nodes always map to themselves
  displayFlat.forEach(node => {
    if (!map.has(node.id)) map.set(node.id, node.id)
    if (node.urn && !map.has(node.urn)) map.set(node.urn, node.id)
  })

  return map
}

// ============================================
// Hook
// ============================================

export function useEdgeProjection({
  edges,
  aggregatedEdges,
  nodesByLayer,
  expandedNodes,
  displayFlat,
  displayMap,
  urnToIdMap,
  showLineageFlow,
  isTracing,
  traceContextSet,
  isContainmentEdge,
  hoveredNodeId,
  suppressedAggEdgeKeys,
}: UseEdgeProjectionOptions): { lineageEdges: any[], visibleLineageEdges: any[], unresolvedAggregatedCount: number } {

  // Telemetry for silently-dropped aggregated edges whose endpoints can't be
  // resolved through displayMap/ancestorMap/urnToIdMap. Surfacing this lets
  // callers render a "X edges hidden — expand parents to reveal" badge.
  const unresolvedAggregatedRef = useRef(0)
  const lastWarnAtRef = useRef(0)

  // ── Flat node index — O(1) lookup replacing O(N) tree search ──────────
  const nodeIndex = useMemo(() => buildNodeIndex(nodesByLayer), [nodesByLayer])

  // ── Incremental ancestorMap state ──────────────────────────────────────
  const ancestorMapRef = useRef<Map<string, string>>(new Map())
  const prevNodesByLayerRef = useRef<Map<string, HierarchyNode[]> | null>(null)
  const prevExpandedNodesRef = useRef<Set<string>>(new Set())

  // ── lineageEdges ───────────────────────────────────────────────────────
  // Flow is the master switch for edge rendering. Trace mode keeps its node
  // highlights and side panels but respects Flow off — the canvas stays clean
  // when the user wants to inspect a trace path without ambient mesh noise.
  const lineageEdges = useMemo(() => {
    if (!showLineageFlow) return []

    // 1. Aggregated Edges
    const aggEdges = Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'collapsed')
      .map(e => ({
        id: e.aggregated.id,
        source: e.aggregated.sourceUrn,
        target: e.aggregated.targetUrn,
        data: {
          edgeType: 'AGGREGATED',
          relationship: 'aggregated',
          isAggregated: true,
          edgeCount: e.aggregated.edgeCount,
          edgeTypes: e.aggregated.edgeTypes,
          confidence: e.aggregated.confidence,
        }
      }))

    // 2. Expanded Detailed Edges
    const expandedDetailedEdges = Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'expanded')
      .flatMap(e => e.detailedEdges
        .filter((de: any) => !isContainmentEdge(de.edgeType))
        .map((de: any) => ({
          id: de.id,
          source: de.sourceUrn,
          target: de.targetUrn,
          data: {
            edgeType: de.edgeType,
            relationship: de.edgeType,
            confidence: de.confidence,
          }
        })))

    // 3. Regular canvas edges — performance guard: only include edges where
    // at least one endpoint is in displayMap.
    const regularEdges = edges.filter(edge => {
      if (isContainmentEdge(normalizeEdgeType(edge))) return false
      return displayMap.has(edge.source) || displayMap.has(edge.target)
    })

    return [...aggEdges, ...expandedDetailedEdges, ...regularEdges]
  }, [edges, showLineageFlow, aggregatedEdges, isContainmentEdge, displayMap])

  // ── ancestorMap (Phase 5.1 — incremental) ─────────────────────────────
  //
  // Full rebuild: when nodesByLayer reference changes (layer re-assignment,
  // initial data load). This is infrequent.
  //
  // Incremental patch: when only expandedNodes changes (user expands /
  // collapses a tree node). We diff the previous/current Set and only
  // traverse the affected subtrees — O(subtree) instead of O(N).
  const ancestorMap = useMemo(() => {
    const needsFullRebuild = prevNodesByLayerRef.current !== nodesByLayer

    if (needsFullRebuild) {
      const map = buildFullAncestorMap(nodesByLayer, expandedNodes, displayFlat)
      prevNodesByLayerRef.current = nodesByLayer
      prevExpandedNodesRef.current = expandedNodes
      ancestorMapRef.current = map
      return map
    }

    // Same nodesByLayer — check if expandedNodes changed
    const prev = prevExpandedNodesRef.current
    if (prev === expandedNodes) {
      return ancestorMapRef.current
    }

    // Diff
    const expanded: string[] = []
    const collapsed: string[] = []
    expandedNodes.forEach(id => { if (!prev.has(id)) expanded.push(id) })
    prev.forEach(id => { if (!expandedNodes.has(id)) collapsed.push(id) })

    if (expanded.length === 0 && collapsed.length === 0) {
      prevExpandedNodesRef.current = expandedNodes
      return ancestorMapRef.current
    }

    // Shallow copy then patch only changed subtrees
    const map = new Map(ancestorMapRef.current)

    // Collapses first: all descendants → collapsed node
    collapsed.forEach(id => {
      const node = nodeIndex.get(id)
      if (node) {
        node.children.forEach(child => collapseSubtreeInMap(child, id, map))
      }
    })

    // Expansions: children become individually visible
    expanded.forEach(id => {
      const node = nodeIndex.get(id)
      if (node) {
        expandNodeInMap(node, expandedNodes, map)
      }
    })

    prevExpandedNodesRef.current = expandedNodes
    ancestorMapRef.current = map
    return map
  }, [nodesByLayer, expandedNodes, displayFlat, nodeIndex])

  // ── Edge projection ────────────────────────────────────────────────────
  //
  // Now depends on the stable `ancestorMap` instead of rebuilding it here.
  // This memo only re-runs when edges or the ancestorMap actually change.
  const projectedEdges = useMemo(() => {
    if (!showLineageFlow) return []

    const edgeGroups = new Map<string, any[]>()

    const addEdgeToGroup = (sourceId: string, targetId: string, edge: any, type: string) => {
      const groupKey = `${sourceId}->${targetId}`
      if (!edgeGroups.has(groupKey)) edgeGroups.set(groupKey, [])
      edgeGroups.get(groupKey)!.push({ ...edge, source: sourceId, target: targetId, originalType: type })
    }

    // A. Aggregated Edges
    let unresolvedThisPass = 0
    Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'collapsed')
      .forEach(e => {
        const agg = e.aggregated
        // Suppress parent AGG when its drill is producing visible finer-level edges.
        if (isTracing && suppressedAggEdgeKeys?.has(`${agg.sourceUrn}->${agg.targetUrn}`)) return
        let sId = displayMap.has(agg.sourceUrn) ? agg.sourceUrn : ancestorMap.get(agg.sourceUrn)
        let tId = displayMap.has(agg.targetUrn) ? agg.targetUrn : ancestorMap.get(agg.targetUrn)
        if (!sId) sId = urnToIdMap.get(agg.sourceUrn)
        if (!tId) tId = urnToIdMap.get(agg.targetUrn)
        if (sId && tId && sId !== tId) {
          addEdgeToGroup(sId, tId, {
            id: agg.id,
            data: {
              edgeType: 'AGGREGATED',
              relationship: 'aggregated',
              isAggregated: true,
              edgeCount: agg.edgeCount,
              edgeTypes: agg.edgeTypes,
              confidence: agg.confidence,
              sourceEdgeIds: agg.sourceEdgeIds,
            }
          }, 'AGGREGATED')
        } else if (!sId && !tId) {
          unresolvedThisPass++
        }
      })
    unresolvedAggregatedRef.current = unresolvedThisPass
    if (unresolvedThisPass > 0) {
      const now = Date.now()
      if (now - lastWarnAtRef.current > 1000) {
        lastWarnAtRef.current = now
        console.warn(`[useEdgeProjection] ${unresolvedThisPass} aggregated edges hidden — endpoints unresolvable via displayMap/ancestorMap/urnToIdMap`)
      }
    }

    // B. Regular / Trace Edges
    edges
      .filter(edge => !isContainmentEdge(normalizeEdgeType(edge)))
      .forEach(edge => {
        const sId = ancestorMap.get(edge.source) || (displayMap.has(edge.source) ? edge.source : null)
        const tId = ancestorMap.get(edge.target) || (displayMap.has(edge.target) ? edge.target : null)
        if (sId && tId && sId !== tId) {
          if (isTracing && (!traceContextSet.has(sId) || !traceContextSet.has(tId))) return
          // Suppress drilled parent AGG edges (URN-pair match on the original endpoints).
          if (
            isTracing
            && String((edge.data?.edgeType) ?? '').toUpperCase() === 'AGGREGATED'
            && suppressedAggEdgeKeys?.has(`${edge.source}->${edge.target}`)
          ) return
          addEdgeToGroup(sId, tId, { ...edge, data: edge.data || {} }, normalizeEdgeType(edge))
        }
      })

    // C. Expanded Detailed Edges
    Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'expanded')
      .flatMap(e => e.detailedEdges)
      .forEach(edge => {
        const sId = ancestorMap.get(edge.sourceUrn)
        const tId = ancestorMap.get(edge.targetUrn)
        if (sId && tId && sId !== tId) {
          addEdgeToGroup(sId, tId, {
            id: edge.id,
            data: { edgeType: edge.edgeType, relationship: edge.edgeType, confidence: edge.confidence }
          }, edge.edgeType)
        }
      })

    // Finalize: bundle groups into projected edges (without delegation — applied in separate memo)
    const projected: any[] = []
    edgeGroups.forEach((groupEdges, key) => {
      const distinctTypes = new Set<string>()
      let isGhost = false
      let isAggregated = false
      let maxConfidence = 0

      const sourceId = groupEdges[0].source
      const targetId = groupEdges[0].target

      if (groupEdges.some((e: any) => e.target !== e.originalTargetId || e.source !== e.originalSourceId)) {
        isGhost = true
      }

      groupEdges.forEach(e => {
        if (e.data?.isAggregated) isAggregated = true
        if (e.data?.edgeTypes) {
          e.data.edgeTypes.forEach((et: string) => distinctTypes.add(et))
        } else if (e.originalType) {
          distinctTypes.add(e.originalType)
        }
        maxConfidence = Math.max(maxConfidence, e.data?.confidence ?? 1)
      })

      const edgeCount = groupEdges.length
      const typesArray = Array.from(distinctTypes)

      projected.push({
        id: `bundle-${key}`,
        source: sourceId,
        target: targetId,
        isBundled: edgeCount > 1,
        isGhost,
        edgeCount,
        types: typesArray,
        confidence: maxConfidence,
        isAggregated,
        isDelegated: false,
        isResidual: false,
        data: { edgeTypes: typesArray, confidence: maxConfidence, edgeCount }
      })
    })

    return projected
  }, [ancestorMap, lineageEdges, edges, aggregatedEdges, displayMap, urnToIdMap, showLineageFlow, isTracing, traceContextSet, isContainmentEdge, expandedNodes, suppressedAggEdgeKeys])

  // ── Edge delegation — separate memo so hoveredNodeId changes are O(E) not O(expensive) ──
  //
  // The heavy edge projection above doesn't re-run on hover. This cheap pass
  // stamps isDelegated/isResidual on the already-projected edges.
  const visibleLineageEdgesWithDelegation = useMemo(() => {
    if (projectedEdges.length === 0) return projectedEdges

    // Build expanded parent info
    const expandedParentInfo = new Map<string, { isPartiallyLoaded: boolean }>()
    expandedNodes.forEach(nodeId => {
      const node = displayMap.get(nodeId)
      if (!node) return
      const totalChildCount = (node.data?.childCount as number) || (node.data?._collapsedChildCount as number) || 0
      const loadedChildCount = node.children?.length ?? 0
      if (loadedChildCount > 0) {
        expandedParentInfo.set(nodeId, {
          isPartiallyLoaded: totalChildCount > 0 && loadedChildCount < totalChildCount,
        })
      }
    })

    // If no expanded parents with children, skip the mapping
    if (expandedParentInfo.size === 0) return projectedEdges

    return projectedEdges.map(edge => {
      const sourceExpanded = expandedParentInfo.get(edge.source)
      const targetExpanded = expandedParentInfo.get(edge.target)

      if (!sourceExpanded && !targetExpanded) return edge

      const isEndpointHovered = hoveredNodeId === edge.source || hoveredNodeId === edge.target
      const anyPartial = sourceExpanded?.isPartiallyLoaded || targetExpanded?.isPartiallyLoaded

      return {
        ...edge,
        isDelegated: anyPartial ? false : !isEndpointHovered,
        isResidual: anyPartial ? !isEndpointHovered : false,
      }
    })
  }, [projectedEdges, expandedNodes, displayMap, hoveredNodeId])

  return {
    lineageEdges,
    visibleLineageEdges: visibleLineageEdgesWithDelegation,
    unresolvedAggregatedCount: unresolvedAggregatedRef.current,
  }
}
