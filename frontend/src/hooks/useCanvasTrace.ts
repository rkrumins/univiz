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

    // Convert and merge trace nodes into canvas store
    const newCanvasNodes = lr.nodes.map((gn: any) => toCanvasNode(gn))
    if (newCanvasNodes.length > 0) {
      addNodes(newCanvasNodes as any[])
    }

    // Convert and merge trace edges into canvas store
    const newCanvasEdges = lr.edges.map((ge: any) => toCanvasEdge(ge))
    if (newCanvasEdges.length > 0) {
      addEdges(newCanvasEdges as any[])
    }

    // Auto-expand ancestors of traced nodes
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
        nodesToExpand.add(curr)
        curr = traceParentMap.get(curr)
      }
    })

    setExpandedNodes(nodesToExpand)
  }, [edges, expandedNodes, isContainmentEdge, addNodes, addEdges, setExpandedNodes, setShowLineageFlow])

  return useUnifiedTrace({
    provider,
    urnResolver,
    onTraceComplete,
  })
}
