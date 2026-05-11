/**
 * useCanvasTrace - Shared trace wrapper for all canvas types
 *
 * Wraps useUnifiedTrace with the standard canvas-level onTraceComplete logic
 * that was duplicated in HierarchyCanvas (lines 103-172) and
 * ContextViewCanvas (lines 108-182):
 *
 * 1. Merge trace result nodes/edges into canvas store
 * 2. Auto-expand ancestors of traced nodes
 * 3. Enable lineage flow visibility
 *
 * Consumed by: HierarchyCanvas, ContextViewCanvas, GraphCanvas
 */

import { useCallback } from 'react'
import { useCanvasStore } from '@/store/canvas'
import { useGraphProvider } from '@/providers/GraphProviderContext'
import { normalizeEdgeType } from '@/store/schema'
import { useUnifiedTrace, type UseUnifiedTraceResult, type TraceResult } from './useUnifiedTrace'
import type { TraceV2Result } from '@/providers/GraphDataProvider'
import { toCanvasNode, toCanvasEdge } from './useGraphHydration'

// ============================================
// Types
// ============================================

export interface UseCanvasTraceOptions {
  /** All canvas nodes (for URN resolution and parent map building). */
  nodes: any[]
  /** All canvas edges (for parent map building). */
  edges: any[]
  /** Predicate: is this a containment edge? */
  isContainmentEdge: (normalizedEdgeType: string) => boolean
  /** Current expanded nodes set. */
  expandedNodes: Set<string>
  /** Setter to update expanded nodes. */
  setExpandedNodes: (updater: Set<string> | ((prev: Set<string>) => Set<string>)) => void
  /** Optional: enable lineage flow when trace completes. */
  setShowLineageFlow?: (show: boolean) => void
  /**
   * When true, exclude the backend-supplied containment ancestor chain
   * from canvas-store insertion. Set this on ContextViewCanvas, where
   * the view's effectiveAssignments are authoritative — adding an
   * ancestor (e.g. Snowflake as parent of REPORTING/GOLD/...) causes
   * useLayerAssignment's "children inherit parent's layer" HARD RULE
   * to override existing per-URN layer assignments and collapse every
   * descendant into the ancestor's layer.
   *
   * Default false (free-flow canvases like GraphCanvas still need the
   * ancestor chain for hierarchy positioning).
   */
  excludeAncestors?: boolean
}

// ============================================
// Hook
// ============================================

export function useCanvasTrace({
  nodes,
  edges,
  isContainmentEdge,
  expandedNodes,
  setExpandedNodes,
  setShowLineageFlow,
  excludeAncestors = false,
}: UseCanvasTraceOptions): UseUnifiedTraceResult {
  const provider = useGraphProvider()
  const addNodes = useCanvasStore((s) => s.addNodes)
  const addEdges = useCanvasStore((s) => s.addEdges)

  // URN resolver: find node by ID, return its URN
  const urnResolver = useCallback((nodeId: string) => {
    const node = nodes.find((n: any) => n.id === nodeId)
    return (node?.data?.urn as string) || nodeId
  }, [nodes])

  // Standard onTraceComplete: merge results + auto-expand ancestors
  const onTraceComplete = useCallback((result: TraceResult) => {
    // Enable lineage flow so edges are visible
    setShowLineageFlow?.(true)

    if (!result.lineageResult) return
    const lr = result.lineageResult

    // Discriminate lineage participants from the containment ancestor
    // chain. The backend response packs both:
    //   • Lineage participants — focus + upstreamUrns + downstreamUrns
    //   • Containment ancestors — added by the server invariant so
    //     free-flow canvases can resolve hierarchy positioning
    // ContextViewCanvas (excludeAncestors=true) must NOT receive the
    // ancestors, because useLayerAssignment's "children inherit parent's
    // layer" HARD RULE would otherwise collapse every descendant into
    // the ancestor's layer and break the view's effectiveAssignments.
    const lineageUrns = new Set<string>([
      ...((lr as any).upstreamUrns ?? []),
      ...((lr as any).downstreamUrns ?? []),
    ])
    // The focus URN — try a few common shapes safely
    const focusUrn: string | undefined =
      (lr as any).focus?.urn ??
      (result as any).focusUrn ??
      ((typeof result.focusId === 'string' && result.focusId.startsWith('urn:')) ? result.focusId : undefined)
    if (focusUrn) lineageUrns.add(focusUrn)

    const acceptUrn = (urn: string | undefined): boolean => {
      if (!excludeAncestors) return true
      if (!urn) return false
      // In excludeAncestors mode, only accept URNs that participate in
      // the lineage path. Ancestors (URNs in `nodes` but not in the
      // upstream/downstream/focus sets) are dropped — the view already
      // owns their layer placement.
      return lineageUrns.has(urn)
    }

    // Convert and merge trace nodes into canvas store
    const newCanvasNodes = lr.nodes
      .filter((gn: any) => acceptUrn(gn.urn))
      .map((gn: any) => toCanvasNode(gn))
    if (newCanvasNodes.length > 0) {
      addNodes(newCanvasNodes as any[])
    }

    // Convert and merge trace edges into canvas store. When
    // excludeAncestors is on, drop edges whose endpoints aren't in the
    // lineage set so we don't leave dangling references to the
    // skipped ancestor nodes.
    const newCanvasEdges = lr.edges
      .filter((ge: any) => {
        if (!excludeAncestors) return true
        const s = ge.source ?? ge.sourceUrn
        const t = ge.target ?? ge.targetUrn
        return lineageUrns.has(s) && lineageUrns.has(t)
      })
      .map((ge: any) => toCanvasEdge(ge))
    if (newCanvasEdges.length > 0) {
      addEdges(newCanvasEdges as any[])
    }

    // Auto-expand ancestors of traced nodes. When excludeAncestors is
    // on, we still walk the existing canvas edges to expand parents
    // that are part of the view — so a traced container's parent
    // layer-root expands to reveal it — but we DON'T expand any
    // ancestor URN that wasn't allowed onto the canvas (it isn't
    // there, so expanding it does nothing useful and would re-trigger
    // the layer cascade if processed elsewhere).
    const nodesToExpand = new Set(expandedNodes)

    // Build parent map from ALL edges (including newly added trace edges)
    const allCurrentEdges = [...edges, ...newCanvasEdges]
    const traceParentMap = new Map<string, string>()
    allCurrentEdges.forEach((e: any) => {
      if (isContainmentEdge(normalizeEdgeType(e))) {
        traceParentMap.set(
          e.target ?? (e as any).targetUrn,
          e.source ?? (e as any).sourceUrn,
        )
      }
    })

    // Walk up ancestor chain for each traced node
    result.traceNodes.forEach((id) => {
      let curr = traceParentMap.get(id)
      while (curr) {
        if (excludeAncestors && !lineageUrns.has(curr)) {
          // Don't auto-expand a node we filtered out of the canvas.
          break
        }
        nodesToExpand.add(curr)
        curr = traceParentMap.get(curr)
      }
    })

    setExpandedNodes(nodesToExpand)
  }, [edges, expandedNodes, isContainmentEdge, addNodes, addEdges, setExpandedNodes, setShowLineageFlow, excludeAncestors])

  const traceApi = useUnifiedTrace({
    provider,
    urnResolver,
    onTraceComplete,
  })

  // Canvas-aware drill-down wrapper. The underlying expandAggregatedEdge
  // stashes the response in a drilldowns Map but doesn't merge into the
  // canvas store — so the new finer-level nodes never render. Wrap it to
  // merge incrementally: existing skeleton nodes stay put (preserved by
  // canvasStore.addNodes' id-keyed dedup), new ones slide in next to
  // their parents. The server guarantees ancestor chains are present in
  // the response, so useLayerAssignment's children-inherit-parent rule
  // resolves layer placement without any frontend climb.
  const expandTraceEdge = useCallback(async (
    sourceUrn: string,
    targetUrn: string,
    currentLevel: number,
  ): Promise<TraceV2Result | null> => {
    const v2 = await traceApi.expandAggregatedEdge(sourceUrn, targetUrn, currentLevel)
    if (!v2) return null

    // Same lineage-vs-ancestor discrimination as onTraceComplete. In
    // excludeAncestors mode, the response's containmentEdges are
    // intentionally dropped — the view's layer rules already place
    // every legitimate node, and merging the ancestors would re-trigger
    // the children-inherit-parent cascade that collapses descendants
    // into the ancestor's layer.
    const lineageUrns = new Set<string>([
      ...((v2 as any).upstreamUrns ?? []),
      ...((v2 as any).downstreamUrns ?? []),
    ])
    const focusUrn = (v2 as any).focus?.urn
    if (focusUrn) lineageUrns.add(focusUrn)

    const acceptUrn = (urn: string | undefined): boolean => {
      if (!excludeAncestors) return true
      if (!urn) return false
      return lineageUrns.has(urn)
    }

    const newNodes = (v2.nodes ?? [])
      .filter((gn: any) => acceptUrn(gn.urn))
      .map((gn: any) => toCanvasNode(gn))
    if (newNodes.length > 0) addNodes(newNodes as any[])

    // Lineage edges first — these connect lineage participants and
    // ride along unconditionally when accepted. Containment edges only
    // when excludeAncestors is off (they connect to ancestor nodes we
    // intentionally skipped otherwise).
    const lineage = (v2.edges ?? [])
      .filter((ge: any) => {
        if (!excludeAncestors) return true
        const s = ge.source ?? ge.sourceUrn
        const t = ge.target ?? ge.targetUrn
        return lineageUrns.has(s) && lineageUrns.has(t)
      })
      .map((ge: any) => toCanvasEdge(ge))
    const containment = excludeAncestors
      ? []
      : ((v2 as any).containmentEdges ?? []).map((ge: any) => toCanvasEdge(ge))
    const newEdges = [...lineage, ...containment]
    if (newEdges.length > 0) addEdges(newEdges as any[])

    return v2
  }, [traceApi, addNodes, addEdges, excludeAncestors])

  return { ...traceApi, expandAggregatedEdge: expandTraceEdge }
}
