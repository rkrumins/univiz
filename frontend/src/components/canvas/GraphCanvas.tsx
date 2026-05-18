/**
 * GraphCanvas - Unified React Flow graph canvas
 *
 * Replaces both LineageCanvas and LayeredLineageCanvas as the main canvas
 * for all free-floating graph views. Composes shared hooks for:
 * - Containment hierarchy (useContainmentHierarchy)
 * - Trace with auto-merge + auto-expand (useCanvasTrace)
 * - Progressive loading (useGraphHydration)
 * - ELK.js layout (useElkLayout)
 * - Progressive edge disclosure (useAggregatedLineage)
 * - Edge roll-up to visible ancestors (useEdgeProjection)
 * - Click/hover highlighting (useHighlightState, useHoverHighlight)
 * - Edge filtering (useEdgeDetailPanel, useEdgeTypeFilters)
 * - Context menu, inline edit, quick create, command palette (useCanvasInteractions)
 * - Keyboard shortcuts (useCanvasKeyboard)
 */

import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  BackgroundVariant,
  type OnNodesChange,
  type OnEdgesChange,
  type OnConnect,
  type NodeMouseHandler,
  type EdgeMouseHandler,
  type ReactFlowInstance,
  applyNodeChanges,
  applyEdgeChanges,
  SelectionMode,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { AnimatePresence } from 'framer-motion'
import { ArrowRight, ArrowDown, Loader2, GitBranch, ZoomIn } from 'lucide-react'
import { cn } from '@/lib/utils'
import { generateColorFromType } from '@/lib/type-visuals'

// Node/Edge components
import { GhostNode } from './nodes/GhostNode'
import { GenericNode } from './nodes/GenericNode'
import { LineageEdge } from './edges/LineageEdge'
import { AggregatedEdge } from './edges/AggregatedEdge'
import { CanvasControls } from './CanvasControls'
import { EdgeLegend } from './EdgeLegend'
import { EntityDrawer } from '../panels/EntityDrawer'
import { EdgeDetailPanel, generateEdgeTypeFilters } from '../panels/EdgeDetailPanel'

// UX components
import { CanvasContextMenu } from './CanvasContextMenu'
import { InlineNodeEditor } from './InlineNodeEditor'
import { QuickCreateNode } from './QuickCreateNode'
import { CommandPalette } from './CommandPalette'
import { EditorToolbar } from './EditorToolbar'
import { TraceToolbar } from './TraceToolbar'
import { NodePalette } from './NodePalette'

// Hooks
import { useGraphHydration } from '@/hooks/useGraphHydration'
import { useRevealNode } from '@/hooks/useRevealNode'
import { useGraphProvider } from '@/providers/GraphProviderContext'
import { useElkLayout } from '@/hooks/useElkLayout'
import { useContainmentHierarchy } from '@/hooks/useContainmentHierarchy'
import { useCanvasTrace } from '@/hooks/useCanvasTrace'
import { useAggregatedLineage } from '@/hooks/useAggregatedLineage'
import { useHighlightState, useHoverHighlight, useHoveredNodeId } from '@/hooks/useHighlightState'
import { useEdgeDetailPanel, useEdgeTypeFilters, useEdgeFiltersStore } from '@/hooks/useEdgeFilters'
import { useSemanticZoom } from '@/hooks/useSemanticZoom'
import { useCanvasInteractions } from '@/hooks/useCanvasInteractions'
import { useCanvasKeyboard } from '@/hooks/useCanvasKeyboard'
import { useLoadingToast } from '@/components/ui/toast'

// Stores
import { useSchemaStore, normalizeEdgeType, useEdgeTypeMetadataMap } from '@/store/schema'
import {
  useViewContainmentEdgeTypes,
  useViewIsContainmentEdge,
  useViewLineageEdgeTypes,
  useViewRelationshipTypes,
  useViewEntityTypes,
  useViewSchemaIsReady,
} from '@/hooks/useViewSchema'
import { useCanvasStore, type LineageNode, type LineageEdge as LineageEdgeType } from '@/store/canvas'
import { fetchWithTimeout } from '@/services/fetchWithTimeout'
import { usePreferencesStore } from '@/store/preferences'

// Types
import type { HierarchyNode } from '@/types/hierarchy'

// ============================================
// Node/Edge type registrations
// ============================================

const nodeTypes = { ghost: GhostNode, generic: GenericNode }
const edgeTypes = { lineage: LineageEdge, aggregated: AggregatedEdge as any }

/** Maximum nodes rendered in the DOM before viewport culling kicks in */
const MAX_VISIBLE_NODES = 2000
/** Maximum children shown per parent before "Show More" */
const MAX_CHILDREN_PER_PARENT = 20

// ============================================
// Component
// ============================================

export function GraphCanvas({ className }: { className?: string }) {
  // 1. Schema readiness guard
  const isSchemaReady = useViewSchemaIsReady()

  // 2. Canvas store
  const { setNodes, setEdges, selectNode, selectEdge, clearSelection, addEdges, addNodes } = useCanvasStore()
  const setVisibleEdges = useCanvasStore((s) => s.setVisibleEdges)
  const rawNodes = useCanvasStore((s) => s.nodes)
  const rawEdges = useCanvasStore((s) => s.edges)
  const selectedNodeIds = useCanvasStore((s) => s.selectedNodeIds)
  const selectedNodeId = selectedNodeIds[0] ?? null
  const drawerNodeId = useCanvasStore((s) => s.drawerNodeId)
  // 3. Schema / ontology
  const schema = useSchemaStore((s) => s.schema)
  const containmentEdgeTypes = useViewContainmentEdgeTypes()
  const lineageEdgeTypes = useViewLineageEdgeTypes()
  const isContainmentEdge = useViewIsContainmentEdge()
  const relationshipTypes = useViewRelationshipTypes()
  const schemaEntityTypes = useViewEntityTypes()
  const edgeTypeMetadata = useEdgeTypeMetadataMap()
  const { showMinimap, showGrid } = usePreferencesStore()

  // 4. Local state
  const [showLineageFlow, setShowLineageFlow] = useState(true)
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())
  const [isPaletteOpen, setPaletteOpen] = useState(false)
  const [activeEdgeType, setActiveEdgeType] = useState<string>('manual')

  // Viewport-aware node filtering for large graphs
  const [viewportBounds, setViewportBounds] = useState<{ x: number; y: number; zoom: number } | null>(null)

  // Ref to hold semanticZoom.onViewportChange (defined later, avoids declaration order issue)
  const semanticZoomRef = useRef<((viewport: any) => void) | null>(null)

  const handleViewportChange = useCallback((viewport: { x: number; y: number; zoom: number }) => {
    setViewportBounds(viewport)
    semanticZoomRef.current?.(viewport)
  }, [])

  // 5. Containment hierarchy (shared hook)
  const { parentMap, childMap, nodeMap } = useContainmentHierarchy({
    nodes: rawNodes,
    edges: rawEdges,
    isContainmentEdge,
  })

  // 6. Compute VISIBLE nodes — the core expand/collapse logic.
  //
  // A node is visible if:
  //   (a) It's a root (no containment parent), OR
  //   (b) Every ancestor in its containment chain is expanded
  //       AND it's within the per-parent child cap (MAX_CHILDREN_PER_PARENT)
  //
  // hiddenChildCounts tracks how many children are hidden per parent for "Load More" UI.
  //
  // Tracks per-parent how many extra children to show beyond the default cap.
  const [childPageSize, setChildPageSize] = useState<Map<string, number>>(new Map())

  const { visibleNodeIds, displayMap, hiddenChildCounts } = useMemo(() => {
    const visible = new Set<string>()
    const dMap = new Map<string, HierarchyNode>()
    const hiddenCounts = new Map<string, number>()

    // Count visible children per parent (for capping)
    const visibleChildCount = new Map<string, number>()

    // Helper: check if a node's entire ancestor chain is expanded
    const isAncestorChainExpanded = (nodeId: string): boolean => {
      const parent = parentMap.get(nodeId)
      if (!parent) return true // Root — always visible
      if (!expandedNodes.has(parent)) return false // Parent collapsed — hidden
      return isAncestorChainExpanded(parent) // Check grandparent recursively
    }

    // First pass: determine which nodes WOULD be visible (ignoring cap)
    const wouldBeVisible = new Set<string>()
    rawNodes.forEach(n => {
      if (n.data.type === 'ghost') return
      if (isAncestorChainExpanded(n.id)) wouldBeVisible.add(n.id)
    })

    // Second pass: apply per-parent child cap
    rawNodes.forEach(n => {
      if (n.data.type === 'ghost') return
      if (!wouldBeVisible.has(n.id)) return

      const parent = parentMap.get(n.id)
      if (parent && expandedNodes.has(parent)) {
        // This is a child of an expanded parent — check cap
        const currentCount = visibleChildCount.get(parent) ?? 0
        const pageSize = childPageSize.get(parent) ?? MAX_CHILDREN_PER_PARENT
        if (currentCount >= pageSize) {
          // Over cap — track as hidden
          hiddenCounts.set(parent, (hiddenCounts.get(parent) ?? 0) + 1)
          return // Don't add to visible
        }
        visibleChildCount.set(parent, currentCount + 1)
      }

      visible.add(n.id)
    })

    // Build displayMap for ALL loaded nodes (needed by highlight/edge projection)
    rawNodes.forEach(n => {
      if (n.data.type === 'ghost') return
      const childIds = childMap.get(n.id) ?? []
      dMap.set(n.id, {
        id: n.id,
        typeId: (n.data.type as string) ?? '',
        name: (n.data.label as string) ?? n.id,
        data: n.data as Record<string, unknown>,
        children: childIds
          .map(cid => nodeMap.get(cid))
          .filter(Boolean)
          .map(cn => ({
            id: cn!.id,
            typeId: (cn!.data.type as string) ?? '',
            name: (cn!.data.label as string) ?? cn!.id,
            data: cn!.data as Record<string, unknown>,
            children: [],
            depth: 0,
            urn: (cn!.data.urn as string) ?? cn!.id,
            entityTypeOption: (cn!.data.type as string) ?? '',
            tags: (cn!.data.classifications as string[]) ?? [],
          })),
        depth: 0,
        urn: (n.data.urn as string) ?? n.id,
        entityTypeOption: (n.data.type as string) ?? '',
        tags: (n.data.classifications as string[]) ?? [],
      })
    })

    return { visibleNodeIds: visible, displayMap: dMap, hiddenChildCounts: hiddenCounts }
  }, [rawNodes, parentMap, expandedNodes, childMap, nodeMap, childPageSize])

  // Visible nodes and edges — filtered by expansion state
  const visibleNodes = useMemo(() =>
    rawNodes.filter(n => visibleNodeIds.has(n.id)),
    [rawNodes, visibleNodeIds]
  )

  // Note: visibleRawEdges removed — edge projection in allVisibleEdges
  // handles visibility by projecting to visible ancestors, not filtering.

  // 7. Progressive loading
  const { loadChildren, cancelChildLoad, isLoading: isLoadingChildren, loadingNodes } = useGraphHydration()
  useLoadingToast('graph-children', isLoadingChildren, 'Expanding hierarchy')

  // 8. Trace system (shared hook)
  const trace = useCanvasTrace({
    nodes: rawNodes,
    edges: rawEdges,
    isContainmentEdge,
    expandedNodes,
    setExpandedNodes,
    setShowLineageFlow,
  })

  // 9. Build traceContextSet
  const traceContextSet = useMemo(() => {
    const set = new Set<string>()
    if (!trace.isTracing) return set
    if (trace.focusId) set.add(trace.focusId)
    if (trace.focusId) {
      let curr = parentMap.get(trace.focusId)
      while (curr) {
        set.add(curr)
        curr = parentMap.get(curr)
      }
    }
    trace.visibleTraceNodes.forEach((id) => {
      set.add(id)
      let curr = parentMap.get(id)
      while (curr) {
        set.add(curr)
        curr = parentMap.get(curr)
      }
    })
    return set
  }, [trace.isTracing, trace.focusId, trace.visibleTraceNodes, parentMap])

  // 9b. Aggregated Lineage — fetches rolled-up lineage from the backend.
  // Lineage edges in the graph DB typically connect deep entities (tables, columns).
  // The aggregation service rolls them up to whatever level is currently visible.
  // This is WHY ContextViewCanvas shows lineage but raw edges alone don't.
  const {
    aggregatedEdges,
    fetchAggregated,
    isLoading: isLoadingAggEdges,
  } = useAggregatedLineage({ granularity: null })
  useLoadingToast('graph-agg', isLoadingAggEdges, 'Loading lineage')

  // Stable ref for nodes (avoids effect dependency on nodes array)
  const nodesRef = useRef(rawNodes)
  nodesRef.current = rawNodes

  // Track previous aggregation targets to avoid redundant fetches
  const prevAggregationKeyRef = useRef('')

  // Fetch aggregated lineage when visible node set changes.
  //
  // GATED ON !trace.isTracing: when a trace is active, the trace response
  // already carries the AGGREGATED edges at the right level (skeleton-first
  // contract — see backend/app/api/v2/endpoints/graph.py). Issuing a parallel
  // /aggregated-lineage fetch would double the network roundtrip and risk
  // racing the two results into the canvas. In browse mode (no active trace)
  // the hook fires as before to populate level-0 rollups for context.
  useEffect(() => {
    if (!showLineageFlow || rawNodes.length === 0) return
    if (trace.isTracing) return

    const fetchDebounced = setTimeout(() => {
      // Collect URNs of all visible nodes
      const visibleUrns = rawNodes
        .filter(n => visibleNodeIds.has(n.id))
        .map(n => (n.data?.urn as string) || n.id)
        .filter(Boolean)

      if (visibleUrns.length === 0) return

      // Only fetch for COLLAPSED visible nodes (expanded ones' children handle themselves)
      const urnToId = new Map(nodesRef.current.map(n => [(n.data?.urn as string) || n.id, n.id]))
      const aggregationTargets = visibleUrns.filter(urn => {
        const nodeId = urnToId.get(urn)
        return nodeId && !expandedNodes.has(nodeId)
      })

      // Skip if target set hasn't changed
      const aggregationKey = aggregationTargets.sort().join(',')
      if (aggregationKey === prevAggregationKeyRef.current) return
      prevAggregationKeyRef.current = aggregationKey

      if (aggregationTargets.length > 0) {
        fetchAggregated(aggregationTargets, aggregationTargets)
      }
    }, 500) // 500ms debounce

    return () => clearTimeout(fetchDebounced)
  }, [showLineageFlow, rawNodes.length, visibleNodeIds, expandedNodes, fetchAggregated, trace.isTracing])

  // 10. Edge projection — the core of multi-level lineage visualization.
  //
  // For every edge in rawEdges, we find the VISIBLE ancestor of each endpoint:
  // - If a node is visible, it maps to itself
  // - If a node is hidden (inside collapsed parent), walk up parentMap until
  //   we find a visible ancestor → project the edge to that ancestor
  //
  // This enables "inherited lineage": if Domain A → Domain B (lineage) and
  // Domain B is expanded, we show edges from Domain A to each of B's visible children.
  //
  // Containment edges: only shown when BOTH endpoints are directly visible (no projection)
  // Lineage edges: projected to visible ancestors, shown at all zoom levels

  // Helper: find the nearest visible ancestor of a node
  const findVisibleAncestor = useCallback((nodeId: string): string | null => {
    if (visibleNodeIds.has(nodeId)) return nodeId
    const parent = parentMap.get(nodeId)
    if (!parent) return null // Node not in tree at all
    return findVisibleAncestor(parent)
  }, [visibleNodeIds, parentMap])

  const allVisibleEdges = useMemo(() => {
    const result: Array<typeof rawEdges[0] & { _isContainment: boolean; _isProjected: boolean }> = []
    const seen = new Set<string>() // Deduplicate projected edges

    for (const edge of rawEdges) {
      const edgeType = normalizeEdgeType(edge)
      const isContainment = isContainmentEdge(edgeType)

      if (isContainment) {
        // Containment: only show when BOTH endpoints are directly visible
        if (visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)) {
          result.push({ ...edge, _isContainment: true, _isProjected: false })
        }
      } else {
        // Lineage: project to visible ancestors
        const visibleSource = findVisibleAncestor(edge.source)
        const visibleTarget = findVisibleAncestor(edge.target)

        if (visibleSource && visibleTarget && visibleSource !== visibleTarget) {
          // Deduplicate: multiple underlying edges may project to the same visible pair
          const projectedKey = `${visibleSource}->${visibleTarget}:${edgeType}`
          if (seen.has(projectedKey)) continue
          seen.add(projectedKey)

          const isProjected = visibleSource !== edge.source || visibleTarget !== edge.target
          result.push({
            ...edge,
            id: isProjected ? `proj:${edge.id}` : edge.id,
            source: visibleSource,
            target: visibleTarget,
            _isContainment: false,
            _isProjected: isProjected,
          })
        }
      }
    }

    // B. Aggregated lineage edges from the backend aggregation service.
    // These represent rolled-up lineage (e.g., Domain A → Domain B based on
    // underlying table-level TRANSFORMS edges). This is the primary source of
    // lineage visibility at the top level.
    aggregatedEdges.forEach((aggState, _key) => {
      if (aggState.state !== 'collapsed') return
      const agg = aggState.aggregated
      if (!agg?.sourceUrn || !agg?.targetUrn) return

      // Map URNs to visible node IDs
      const sourceId = findVisibleAncestor(agg.sourceUrn)
      const targetId = findVisibleAncestor(agg.targetUrn)
      if (!sourceId || !targetId || sourceId === targetId) return

      const aggKey = `agg:${sourceId}->${targetId}`
      if (seen.has(aggKey)) return
      seen.add(aggKey)

      result.push({
        id: agg.id || aggKey,
        source: sourceId,
        target: targetId,
        type: 'aggregated',
        data: {
          edgeType: 'AGGREGATED',
          relationship: 'aggregated',
          isAggregated: true,
          edgeCount: agg.edgeCount ?? 1,
          edgeTypes: agg.edgeTypes ?? [],
          confidence: agg.confidence ?? 1,
        },
        _isContainment: false,
        _isProjected: sourceId !== agg.sourceUrn || targetId !== agg.targetUrn,
      } as any)
    })

    return result
  }, [rawEdges, visibleNodeIds, isContainmentEdge, findVisibleAncestor, aggregatedEdges])

  // Lineage-only subset for highlight computation
  const lineageEdges = useMemo(() =>
    allVisibleEdges.filter(e => !e._isContainment),
    [allVisibleEdges]
  )

  // Publish the projected lineage edge set so EntityDrawer's Lineage section
  // can mirror what the user sees on canvas (see canvas.ts:visibleEdges).
  // Dedup by id-fingerprint so unstable upstream memos (allVisibleEdges
  // re-derives on every aggregatedEdges Map churn) don't cause repeated
  // store writes that feed back into a render loop.
  const lineageEdgesFingerprint = useMemo(
    () => lineageEdges.map((e) => e.id).join('|'),
    [lineageEdges],
  )
  const lineageEdgesRef = useRef(lineageEdges)
  lineageEdgesRef.current = lineageEdges
  useEffect(() => {
    setVisibleEdges(lineageEdgesRef.current as LineageEdgeType[])
    // No cleanup-reset: avoids a second store write per cycle that triggers
    // a re-render in any subscriber (LineageNeighbors) and feeds the loop.
    // Stale data on unmount is overwritten by the next canvas mount; if no
    // canvas is mounted, LineageNeighbors falls back to raw `edges`.
  }, [lineageEdgesFingerprint, setVisibleEdges])

  // 12. Highlight state — uses lineageEdges for click/hover highlighting
  const hoveredNodeId = useHoveredNodeId()
  const { highlightState, isHighlightActive: isClickHighlightActive } = useHighlightState({
    selectedNodeId,
    visibleLineageEdges: lineageEdges,
    isTracing: trace.isTracing,
    displayMap,
    childMap,
  })
  const { hoverHighlight, isHoverActive } = useHoverHighlight({
    hoveredNodeId,
    visibleLineageEdges: lineageEdges,
    isTracing: trace.isTracing,
    displayMap,
    childMap,
    isClickHighlightActive,
  })
  const isHighlightActive = isClickHighlightActive || isHoverActive
  const mergedHighlightNodes = isClickHighlightActive
    ? highlightState.nodes
    : hoverHighlight.nodes
  const mergedHighlightEdges = isClickHighlightActive
    ? highlightState.edges
    : hoverHighlight.edges

  // 13. Edge filters
  const { isOpen: isEdgePanelOpen, toggle: toggleEdgePanel, close: closeEdgePanel } =
    useEdgeDetailPanel()
  const { filters: edgeFilters, toggle: toggleEdgeFilter } = useEdgeTypeFilters()
  const ontologyMetadata = useMemo(() => ({ edgeTypeMetadata }), [edgeTypeMetadata])
  const dynamicEdgeFilters = useMemo(() => {
    if (rawEdges.length === 0) return edgeFilters
    return generateEdgeTypeFilters(
      rawEdges,
      relationshipTypes,
      containmentEdgeTypes,
      ontologyMetadata,
    )
  }, [rawEdges, relationshipTypes, containmentEdgeTypes, ontologyMetadata, edgeFilters])

  // 13a. Wire EdgeDetailPanel's filter state into actual canvas rendering.
  // The store has been driving the panel UI for a while — this is what makes
  // the toggles affect the graph the user sees.
  const directionFilter = useEdgeFiltersStore((s) => s.directionFilter)
  const focusedFilterNodeId = useEdgeFiltersStore((s) => s.focusedNodeId)
  const highlightedEdgeIds = useEdgeFiltersStore((s) => s.highlightedEdgeIds)
  const isolateMode = useEdgeFiltersStore((s) => s.isolateMode)

  // Set of normalized edge types the user has left enabled. `null` means "no
  // filters configured yet" (schema not loaded / no edges discovered) and we
  // pass everything through rather than nuking the graph.
  const enabledEdgeTypes = useMemo<Set<string> | null>(() => {
    if (!dynamicEdgeFilters || dynamicEdgeFilters.length === 0) return null
    return new Set(dynamicEdgeFilters.filter((f) => f.enabled).map((f) => f.type))
  }, [dynamicEdgeFilters])

  // Direction filter — when a node is focused via the panel, restrict the
  // displayed lineage edges to that node's incoming / outgoing / upstream /
  // downstream set. Transitive sets are computed via iterative DFS to avoid
  // recursion limits on large graphs.
  const directionEdgeIds = useMemo<Set<string> | null>(() => {
    if (!focusedFilterNodeId || directionFilter === 'all') return null
    if (directionFilter === 'incoming') {
      return new Set(
        allVisibleEdges.filter((e) => e.target === focusedFilterNodeId).map((e) => e.id),
      )
    }
    if (directionFilter === 'outgoing') {
      return new Set(
        allVisibleEdges.filter((e) => e.source === focusedFilterNodeId).map((e) => e.id),
      )
    }
    const ids = new Set<string>()
    const visited = new Set<string>()
    const stack = [focusedFilterNodeId]
    while (stack.length > 0) {
      const node = stack.pop()!
      if (visited.has(node)) continue
      visited.add(node)
      for (const e of allVisibleEdges) {
        if (directionFilter === 'upstream' && e.target === node) {
          ids.add(e.id)
          stack.push(e.source)
        } else if (directionFilter === 'downstream' && e.source === node) {
          ids.add(e.id)
          stack.push(e.target)
        }
      }
    }
    return ids
  }, [allVisibleEdges, focusedFilterNodeId, directionFilter])

  // 14. ELK Layout
  const { applyLayout, isLayouting, direction, toggleDirection } = useElkLayout()
  const [layoutedNodes, setLayoutedNodes] = useState<LineageNode[]>([])
  // Ref mirror so the reveal-focus adapter can read post-layout positions
  // without re-binding every render. Synced below on every render.
  const layoutedNodesRef = useRef<LineageNode[]>([])
  layoutedNodesRef.current = layoutedNodes
  const prevLayoutSig = useRef('')
  const hasAppliedInitialLayout = useRef(false)
  const fitViewTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null)

  const scheduleFitView = useCallback(() => {
    if (!rfInstance) return
    if (fitViewTimer.current) clearTimeout(fitViewTimer.current)
    fitViewTimer.current = setTimeout(() => {
      rfInstance.fitView({ padding: 0.2, duration: 300 })
      hasAppliedInitialLayout.current = true
      fitViewTimer.current = null
    }, 250)
  }, [rfInstance])

  // Reveal + center the viewport on a node by id. Used by EntityDrawer's
  // LineageNeighbors so clicking a neighbor pans the canvas to it — and,
  // when the neighbor is hidden inside collapsed parents, expands the
  // ancestor chain (lazy-loading from the backend if needed) before
  // panning. See [useRevealNode](../../hooks/useRevealNode.ts).
  //
  // The pan adapter reads from `layoutedNodes` (post-elk positions) via a
  // ref because canvas-store positions are pre-layout. Trace mode is
  // transparent here — visibility is governed by parentMap + expandedNodes,
  // not by trace state, so the cascade works under both browse and trace.
  const provider = useGraphProvider()
  const revealAndFocus = useRevealNode({
    parentMap,
    setExpandedNodes,
    loadChildren,
    provider,
    focus: (id: string) => {
      if (!rfInstance) return
      const node =
        layoutedNodesRef.current.find((n) => n.id === id) ??
        useCanvasStore.getState().nodes.find((n) => n.id === id)
      if (!node) return
      rfInstance.setCenter(node.position.x, node.position.y, {
        zoom: rfInstance.getZoom(),
        duration: 400,
      })
    },
  })

  // Multi-locate: reveal a batch of targets in parallel, then fit the
  // viewport so all of them are on screen. Each reveal runs the same
  // expand-ancestors cascade as the single-click path; using
  // Promise.allSettled lets a partial failure (e.g. one URN that
  // backend lookup rejects) still surface the successful subset rather
  // than abandoning the whole batch.
  const locateManyOnCanvas = useCallback(
    async (ids: string[]) => {
      // skipFocus: true on each reveal so the canvas doesn't run N
      // competing setCenter animations during the cascade. The trailing
      // fitView below produces a single coherent pan/zoom.
      await Promise.allSettled(
        ids.map((id) => revealAndFocus(id, { skipFocus: true })),
      )
      if (!rfInstance) return
      const targets = ids
        .map((id) => layoutedNodesRef.current.find((n) => n.id === id))
        .filter((n): n is LineageNode => !!n)
      if (targets.length === 0) return
      rfInstance.fitView({
        nodes: targets.map((n) => ({ id: n.id })),
        padding: 0.25,
        duration: 500,
      })
    },
    [rfInstance, revealAndFocus],
  )

  const scheduleFitViewRef = useRef(scheduleFitView)
  scheduleFitViewRef.current = scheduleFitView

  useEffect(() => {
    if (rfInstance && layoutedNodes.length > 0 && !hasAppliedInitialLayout.current) {
      scheduleFitView()
    }
  }, [rfInstance, layoutedNodes, scheduleFitView])

  // Edges for ELK: all edges where BOTH endpoints are visible (containment + lineage)
  // ELK uses these for positioning — containment edges create hierarchical structure
  const layoutEdges = useMemo(() =>
    rawEdges.filter(e => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target)),
    [rawEdges, visibleNodeIds]
  )

  // Layout signature — derived from visible nodes + their edges + direction
  const layoutSignature = useMemo(() => {
    if (visibleNodes.length === 0) return ''
    const nodeIds = visibleNodes.map((n) => n.id).sort().join(',')
    const edgeIds = layoutEdges.map((e) => e.id).sort().join(',')
    return `${nodeIds}|${edgeIds}|${direction}`
  }, [visibleNodes, layoutEdges, direction])

  // ELK layout — uses incremental layout when nodes are added (expand),
  // full layout on initial load or when direction changes.
  useEffect(() => {
    if (visibleNodes.length === 0) {
      setLayoutedNodes([])
      prevLayoutSig.current = ''
      hasAppliedInitialLayout.current = false
      return
    }
    if (layoutSignature === prevLayoutSig.current) return
    prevLayoutSig.current = layoutSignature

    // FLAT layout — pass ALL edges (containment + lineage) so ELK's layered
    // algorithm naturally creates hierarchical layers:
    //   Layer 0: roots (no incoming containment)
    //   Layer 1: children (incoming CONTAINS edge from layer 0)
    //   Layer 2: grandchildren, etc.
    // This is how DataHub/OpenMetadata do it — no compound nodes, just edge-driven layers.
    applyLayout(visibleNodes, layoutEdges, schemaEntityTypes as any)
      .then((positioned) => {
        setLayoutedNodes(positioned as LineageNode[])
        if (!hasAppliedInitialLayout.current) scheduleFitViewRef.current()
      })
      .catch((err) => {
        console.error('[GraphCanvas] Layout failed:', err)
        setLayoutedNodes(visibleNodes)
      })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutSignature, applyLayout])

  // 16. Semantic zoom — ontology-driven auto-expand/collapse on zoom
  const semanticZoom = useSemanticZoom({
    rfInstance,
    expandedNodes,
    setExpandedNodes,
    displayMap,
    parentMap,
    schemaEntityTypes: schemaEntityTypes as any,
    loadChildren,
    enabled: true,
  })
  semanticZoomRef.current = semanticZoom.onViewportChange

  // 17. Toggle node expansion with lazy loading
  // Stable callback refs for node data props (avoids new function refs on every render)
  const loadChildrenRef = useRef(loadChildren)
  loadChildrenRef.current = loadChildren

  const stableOnLoadMore = useCallback((parentId: string) => {
    // Increase visible cap for this parent (show more already-loaded children)
    setChildPageSize(prev => {
      const next = new Map(prev)
      const current = next.get(parentId) ?? MAX_CHILDREN_PER_PARENT
      next.set(parentId, current + MAX_CHILDREN_PER_PARENT)
      return next
    })
    // Also trigger backend fetch for more children (if not all loaded)
    loadChildrenRef.current(parentId)
  }, [])

  const pendingLoadRef = useRef<Set<string>>(new Set())
  const toggleNode = useCallback(
    async (nodeId: string) => {
      // Register manual override so semantic zoom doesn't undo this action
      semanticZoom.registerManualOverride(nodeId)

      // A child load for this node is already in flight. Ignore repeat
      // clicks until it settles: otherwise an impatient second click reads
      // committed state as expanded, collapses the node, and cancels the
      // in-flight fetch — forcing a third click to actually load. Collapse
      // works normally once the load completes (finally clears pendingLoadRef).
      if (pendingLoadRef.current.has(nodeId)) return

      let wasExpanded = false
      setExpandedNodes((prev) => {
        wasExpanded = prev.has(nodeId)
        const next = new Set(prev)
        if (wasExpanded) next.delete(nodeId)
        else next.add(nodeId)
        return next
      })
      if (!wasExpanded && !pendingLoadRef.current.has(nodeId)) {
        pendingLoadRef.current.add(nodeId)
        try {
          await loadChildren(nodeId)
        } finally {
          pendingLoadRef.current.delete(nodeId)
        }
      } else if (wasExpanded) {
        // User collapsed mid-load — drop the result so a slow response
        // doesn't repopulate a now-collapsed subtree.
        cancelChildLoad(nodeId)
      }
    },
    [loadChildren, cancelChildLoad, semanticZoom],
  )

  const toggleNodeRef = useRef(toggleNode)
  toggleNodeRef.current = toggleNode

  const stableOnToggle = useCallback((nodeId: string) => {
    toggleNodeRef.current(nodeId)
  }, [])

  // 15. Display edges — ALL visible edges with distinct styles per type
  //
  // Containment (parent→child): thin dashed gray, smooth step routing
  //   → Visible whenever the parent is expanded, clearly shows hierarchy
  // Lineage (data flow): thick solid colored, animated, bezier routing
  //   → Shows data dependencies, highlights on hover/click
  //
  // When lineage flow is off, hide lineage but still show containment structure.
  // Flow is the master switch — Trace mode respects it so the canvas can be
  // dialed back to "trace highlights on nodes only" when desired.
  const displayEdges = useMemo(() => {
    const matchesTypeFilter = (edge: typeof allVisibleEdges[number]): boolean => {
      if (!enabledEdgeTypes) return true
      const normalized = normalizeEdgeType(edge).toLowerCase()
      const original = (edge.data?.edgeType || edge.data?.relationship || 'unknown').toLowerCase()
      return enabledEdgeTypes.has(normalized) || enabledEdgeTypes.has(original)
    }
    return allVisibleEdges
      .filter(edge => {
        // Containment edges are subject to the type filter (so users can hide
        // structural edges) but ignore the lineage Flow toggle.
        if (edge._isContainment) {
          return matchesTypeFilter(edge)
        }
        // Lineage edges follow the Flow toggle, regardless of trace state
        if (!showLineageFlow) return false
        if (!matchesTypeFilter(edge)) return false
        // Direction filter (only active when a focus node is set)
        if (directionEdgeIds && !directionEdgeIds.has(edge.id)) return false
        // Isolate mode — only render highlighted edges. Requires at least one
        // highlight to avoid the surprise of "isolate mode hides everything".
        if (isolateMode && highlightedEdgeIds.size > 0 && !highlightedEdgeIds.has(edge.id)) return false
        return true
      })
      .map(edge => {
        if (edge._isContainment) {
          // Containment: thin dashed gray — shows hierarchy structure
          return {
            id: edge.id,
            source: edge.source,
            target: edge.target,
            type: 'smoothstep' as const,
            animated: false,
            style: {
              stroke: '#94a3b8',
              strokeDasharray: '6,4',
              strokeWidth: 1.5,
              opacity: isHighlightActive ? 0.3 : 0.6,
            },
            data: {
              edgeType: edge.data?.edgeType ?? 'CONTAINS',
              isContainment: true,
            },
          }
        }
        // Lineage: solid colored animated — shows data flow
        const isProjected = (edge as any)._isProjected
        const isAggregated = (edge.data as any)?.isAggregated === true
        return {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          // Use 'aggregated' edge component for rolled-up edges (shows edge count badge)
          type: isAggregated ? 'aggregated' as const : 'lineage' as const,
          animated: !isProjected && !isAggregated && (!isHighlightActive || mergedHighlightEdges.has(edge.id)),
          style: {
            opacity: isHighlightActive && !mergedHighlightEdges.has(edge.id) ? 0.15 : (isProjected ? 0.7 : 1),
            strokeDasharray: isProjected && !isAggregated ? '8,4' : undefined,
          },
          data: {
            edgeType: edge.data?.edgeType ?? edge.data?.relationship ?? '',
            confidence: edge.data?.confidence ?? 1,
            isTraced: trace.isTracing && trace.result?.traceEdges?.has(edge.id),
            isProjected,
            isAggregated,
            edgeCount: (edge.data as any)?.edgeCount,
            sourceEdgeCount: (edge.data as any)?.edgeCount,
          },
        }
      })
  }, [allVisibleEdges, showLineageFlow, trace.isTracing, trace.result, isHighlightActive, mergedHighlightEdges, enabledEdgeTypes, directionEdgeIds, isolateMode, highlightedEdgeIds])
  // (trace.isTracing/trace.result kept in deps because the map step inside reads them for isTraced flagging)

  // 16. Display nodes with visual state — only VISIBLE nodes (expand/collapse aware)
  const displayNodes = useMemo(() => {
    const base = layoutedNodes.length > 0 ? layoutedNodes : visibleNodes
    const allNodes = base.map((node) => ({
      ...node,
      data: {
        ...node.data,
        isLoading: loadingNodes.has(node.id),
        isTraced: trace.isInTrace(node.id),
        isDimmed:
          (trace.isTracing && !traceContextSet.has(node.id)) ||
          (isHighlightActive && !mergedHighlightNodes.has(node.id)),
        isUpstream: trace.isUpstream(node.id),
        isDownstream: trace.isDownstream(node.id),
        isFocus: trace.isFocus(node.id),
        isHighlighted: mergedHighlightNodes.has(node.id),
        isExpanded: expandedNodes.has(node.id),
        // Hidden child count — drives "Load More" badge on the node
        _hiddenCount: hiddenChildCounts.get(node.id) ?? 0,
        onLoadMore: stableOnLoadMore,
        onToggleExpanded: stableOnToggle,
      },
    }))

    // Viewport-aware filtering: only activate when node count exceeds threshold
    if (allNodes.length > MAX_VISIBLE_NODES && viewportBounds) {
      const { x: vx, y: vy, zoom } = viewportBounds
      const buffer = 500 // px buffer around viewport
      const viewWidth = (window.innerWidth || 1920) / zoom
      const viewHeight = (window.innerHeight || 1080) / zoom
      const viewLeft = -vx / zoom - buffer / zoom
      const viewTop = -vy / zoom - buffer / zoom
      const viewRight = viewLeft + viewWidth + 2 * buffer / zoom
      const viewBottom = viewTop + viewHeight + 2 * buffer / zoom

      allNodes.forEach((node) => {
        const inView =
          node.position.x < viewRight &&
          node.position.x + 200 > viewLeft &&
          node.position.y < viewBottom &&
          node.position.y + 80 > viewTop
        if (!inView) {
          node.hidden = true
        }
      })

      // Ensure visible count doesn't exceed cap
      const visible = allNodes.filter((n) => !n.hidden)
      if (visible.length > MAX_VISIBLE_NODES) {
        // Prioritize by depth (parents first) -- sort by depth ascending
        visible.sort(
          (a, b) =>
            (((a.data as any)?.depth as number) ?? 0) -
            (((b.data as any)?.depth as number) ?? 0),
        )
        visible.slice(MAX_VISIBLE_NODES).forEach((n) => {
          n.hidden = true
        })
      }
    }

    return allNodes
  }, [
    layoutedNodes,
    rawNodes,
    loadingNodes,
    trace,
    traceContextSet,
    isHighlightActive,
    mergedHighlightNodes,
    stableOnLoadMore,
    stableOnToggle,
    viewportBounds,
    expandedNodes,
    hiddenChildCounts,
  ])

  // 18. Handlers
  // Apply position/selection changes to layoutedNodes so drags are preserved.
  // We DON'T update rawNodes (the store) for position changes — those are layout-managed.
  // We DO update the store for selection changes.
  const onNodesChange: OnNodesChange = useCallback(
    (changes) => {
      // Update layouted nodes with position changes (drag support)
      setLayoutedNodes((prev) => {
        if (prev.length === 0) return prev
        return applyNodeChanges(changes, prev) as LineageNode[]
      })
      // Update the store for non-position changes (selection, etc.)
      const nonPositionChanges = changes.filter(c => c.type !== 'position')
      if (nonPositionChanges.length > 0) {
        setNodes(applyNodeChanges(nonPositionChanges, rawNodes) as LineageNode[])
      }
    },
    [rawNodes, setNodes],
  )

  const onEdgesChange: OnEdgesChange = useCallback(
    (changes) => {
      setEdges(applyEdgeChanges(changes, rawEdges) as LineageEdgeType[])
    },
    [rawEdges, setEdges],
  )

  const onNodeClick: NodeMouseHandler = useCallback(
    (_, node) => selectNode(node.id),
    [selectNode],
  )

  const onNodeDoubleClick: NodeMouseHandler = useCallback(
    (event, node) => {
      if (event.shiftKey) {
        // Shift+Double-click: trace
        trace.toggleTrace(node.id)
      } else {
        // Regular double-click: expand/collapse
        toggleNode(node.id)
      }
    },
    [trace, toggleNode],
  )

  const onPaneClick = useCallback(() => clearSelection(), [clearSelection])

  // Edge click
  const onEdgeClick: EdgeMouseHandler = useCallback(
    (_, edge) => selectEdge(edge.id),
    [selectEdge],
  )

  // Edge context menu — uses ref because interactions is defined later
  const interactionsRef = useRef<any>(null)
  const onEdgeContextMenu: EdgeMouseHandler = useCallback(
    (event, edge) => {
      event.preventDefault()
      interactionsRef.current?.openContextMenu(event as unknown as React.MouseEvent, {
        type: 'edge',
        id: edge.id,
        source: edge.source,
        target: edge.target,
      })
    },
    [],
  )

  // Helper: find all valid relationship types between two entity types (ontology-driven)
  // Uses containmentEdgeTypes and lineageEdgeTypes to classify each result.
  // Treats empty/missing sourceTypes or targetTypes as wildcards (any entity type allowed).
  const getValidEdgeTypes = useCallback(
    (sourceType: string, targetType: string) => {
      const containmentSet = new Set(containmentEdgeTypes.map(t => t.toUpperCase()))

      return relationshipTypes
        .filter(rt => {
          // Empty arrays or missing = wildcard (any type allowed)
          const srcOk = !rt.sourceTypes?.length || rt.sourceTypes.includes('*') || rt.sourceTypes.includes(sourceType)
          const tgtOk = !rt.targetTypes?.length || rt.targetTypes.includes('*') || rt.targetTypes.includes(targetType)
          return srcOk && tgtOk
        })
        .map(rt => ({
          ...rt,
          _isContainment: rt.isContainment ?? containmentSet.has(rt.id.toUpperCase()),
          _isLineage: rt.isLineage ?? !containmentSet.has(rt.id.toUpperCase()),
          _category: rt.isContainment ? 'Containment' : (rt.isLineage ? 'Lineage' : (rt.category ?? 'Association')),
        }))
    },
    [relationshipTypes, containmentEdgeTypes],
  )

  // Edge picker state — shown when user needs to choose between multiple valid edge types
  const [edgePicker, setEdgePicker] = useState<{
    isOpen: boolean
    position: { x: number; y: number }
    connection: { source: string; target: string; sourceName: string; targetName: string } | null
    validTypes: Array<{ id: string; name: string; description?: string; _isContainment: boolean; _isLineage: boolean; _category: string }>
  }>({ isOpen: false, position: { x: 0, y: 0 }, connection: null, validTypes: [] })

  // Create edge with a specific type
  const createEdgeWithType = useCallback(
    (source: string, target: string, edgeTypeId: string) => {
      const isContainment = containmentEdgeTypes.some(t => t.toUpperCase() === edgeTypeId.toUpperCase())
      addEdges([{
        id: `e-${source}-${target}-${edgeTypeId}-${Date.now()}`,
        source,
        target,
        type: isContainment ? 'smoothstep' : 'lineage',
        data: { edgeType: edgeTypeId, relationship: edgeTypeId },
        animated: !isContainment,
      }])
      setEdgePicker(prev => ({ ...prev, isOpen: false, connection: null }))
    },
    [addEdges, containmentEdgeTypes],
  )

  // Create edge by dragging between node handles — ontology-aware
  // Shows picker when multiple valid types exist; auto-creates when only one
  const onConnect: OnConnect = useCallback(
    (connection) => {
      if (!connection.source || !connection.target) return

      const sourceNode = rawNodes.find(n => n.id === connection.source)
      const targetNode = rawNodes.find(n => n.id === connection.target)
      if (!sourceNode || !targetNode) return

      const sourceType = (sourceNode.data.type as string) ?? ''
      const targetType = (targetNode.data.type as string) ?? ''
      const validTypes = getValidEdgeTypes(sourceType, targetType)

      if (validTypes.length === 0) return

      if (validTypes.length === 1) {
        createEdgeWithType(connection.source, connection.target, validTypes[0].id)
        return
      }

      // Multiple valid types — show picker
      const targetEl = document.querySelector(`[data-id="${connection.target}"]`)
      const rect = targetEl?.getBoundingClientRect()
      setEdgePicker({
        isOpen: true,
        position: rect
          ? { x: rect.left + rect.width / 2, y: rect.top }
          : { x: window.innerWidth / 2, y: window.innerHeight / 2 },
        connection: {
          source: connection.source,
          target: connection.target,
          sourceName: (sourceNode.data.label as string) ?? sourceNode.id,
          targetName: (targetNode.data.label as string) ?? targetNode.id,
        },
        validTypes,
      })
    },
    [rawNodes, getValidEdgeTypes, createEdgeWithType],
  )

  // Edge reconnection — ontology-aware
  const onReconnect = useCallback(
    (oldEdge: any, newConnection: any) => {
      if (!newConnection.source || !newConnection.target) return

      const sourceNode = rawNodes.find(n => n.id === newConnection.source)
      const targetNode = rawNodes.find(n => n.id === newConnection.target)
      if (!sourceNode || !targetNode) return

      const sourceType = (sourceNode.data.type as string) ?? ''
      const targetType = (targetNode.data.type as string) ?? ''
      const validTypes = getValidEdgeTypes(sourceType, targetType)

      const originalType = oldEdge.data?.edgeType ?? oldEdge.data?.relationship
      const edgeType = validTypes.find(rt => rt.id === originalType)?.id ?? validTypes[0]?.id
      if (!edgeType) return

      const { removeEdge, addEdges: addEdgesFresh } = useCanvasStore.getState()
      removeEdge(oldEdge.id)
      const isContainment = containmentEdgeTypes.some(t => t.toUpperCase() === edgeType.toUpperCase())
      addEdgesFresh([{
        id: `e-${newConnection.source}-${newConnection.target}-${edgeType}-${Date.now()}`,
        source: newConnection.source,
        target: newConnection.target,
        type: isContainment ? 'smoothstep' : 'lineage',
        data: { edgeType, relationship: edgeType },
        animated: !isContainment,
      }])
    },
    [rawNodes, getValidEdgeTypes, containmentEdgeTypes],
  )

  // Connection validation — checks ALL ontology relationship types
  // A connection is valid if ANY relationship type allows this source→target type combination
  const isValidConnection = useCallback(
    (connection: any) => {
      if (connection.source === connection.target) return false

      const sourceNode = rawNodes.find(n => n.id === connection.source)
      const targetNode = rawNodes.find(n => n.id === connection.target)
      if (!sourceNode || !targetNode) return false

      const sourceType = (sourceNode.data.type as string) ?? ''
      const targetType = (targetNode.data.type as string) ?? ''

      return getValidEdgeTypes(sourceType, targetType).length > 0
    },
    [rawNodes, getValidEdgeTypes],
  )

  // Drag-and-drop from NodePalette
  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData('application/reactflow')
      if (!type || !rfInstance) return

      const position = rfInstance.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      })

      addNodes([{
        id: `node-${Date.now()}`,
        type: 'generic',
        position,
        data: {
          type,
          label: `New ${type}`,
          urn: `urn:manual:${type}:${Date.now()}`,
        },
      }])
    },
    [rfInstance, addNodes],
  )

  // Save graph to backend
  const handleSave = useCallback(async () => {
    try {
      const response = await fetchWithTimeout('/api/v1/graph/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: rawNodes, edges: rawEdges }),
      })
      if (!response.ok) throw new Error('Failed to save graph')
      alert('Graph saved successfully!')
    } catch (error) {
      console.error('Error saving graph:', error)
      alert('Failed to save graph')
    }
  }, [rawNodes, rawEdges])

  // ESC-driven trace exit. Mirrors ContextViewCanvas: purges the edges the
  // trace merged into the canvas store, clears trace state, and reverts
  // ancestor-chain auto-expansion. Without this, ESC fell through to plain
  // selection-clear and the trace dock stayed open.
  const exitTrace = useCallback(() => {
    if (!trace.isTracing) return false
    const idsToRemove = Array.from(trace.addedEdgeIds)
    trace.clearTrace()
    if (idsToRemove.length > 0) {
      useCanvasStore.getState().removeEdges(idsToRemove)
    }
    trace.resetAddedEdgeIds()
    setExpandedNodes(new Set())
    return true
  }, [trace])

  // 19. Canvas interactions (context menu, inline edit, quick create, command palette)
  const interactions = useCanvasInteractions({
    onTraceNode: (nodeId) => trace.startTrace(nodeId),
    onNodeCreated: (nodeId) => selectNode(nodeId),
    onCloseEdgePanel: () => {
      if (isEdgePanelOpen) {
        closeEdgePanel()
        return true
      }
      return false
    },
    onCloseEntityDrawer: () => {
      if (selectedNodeId) {
        clearSelection()
        return true
      }
      return false
    },
    onExitTrace: exitTrace,
  })

  useCanvasKeyboard({ enabled: true, handlers: interactions.keyboardHandlers })
  interactionsRef.current = interactions

  // 20. Minimap color
  const minimapNodeColor = useCallback(
    (node: LineageNode) => {
      const entityType = schema?.entityTypes.find((et) => et.id === node.data.type)
      if (entityType) return entityType.visual.color
      return generateColorFromType(node.data.type as string)
    },
    [schema],
  )

  // 21. Hover detection for useHoveredNodeId
  const onNodeMouseEnter: NodeMouseHandler = useCallback((_, node) => {
    document.documentElement.dataset.hoveredNode = node.id
  }, [])
  const onNodeMouseLeave: NodeMouseHandler = useCallback(() => {
    delete document.documentElement.dataset.hoveredNode
  }, [])

  // Schema guard
  if (!isSchemaReady) {
    return (
      <div className="w-full h-full flex items-center justify-center bg-canvas">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="w-8 h-8 animate-spin text-accent-lineage" />
          <span className="text-sm text-ink-muted">Loading schema...</span>
        </div>
      </div>
    )
  }

  // RENDER
  return (
    <div className={cn('w-full h-full relative flex flex-col', className)}>
      {/* Editor Toolbar */}
      <div className="absolute top-4 left-4 z-30">
        <EditorToolbar
          onAddNode={() => setPaletteOpen(true)}
          onSave={handleSave}
          edgeTypes={relationshipTypes}
          activeEdgeType={activeEdgeType}
          onSelectEdgeType={setActiveEdgeType}
        />
      </div>

      {/* Node Palette */}
      <AnimatePresence>
        {isPaletteOpen && (
          <NodePalette isOpen={isPaletteOpen} onClose={() => setPaletteOpen(false)} />
        )}
      </AnimatePresence>

      {/* Header */}
      <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
        <div className="pointer-events-auto inline-flex items-center gap-3 bg-canvas-elevated/95 backdrop-blur rounded-xl border border-glass-border px-4 py-2 shadow-lg">
          <h2 className="text-sm font-display font-semibold text-ink">Graph View</h2>
          <span className="px-2 py-0.5 rounded-md bg-accent-lineage/10 text-accent-lineage text-2xs font-medium">
            {trace.isTracing ? 'Tracing' : 'Explore'}
          </span>
          <button
            onClick={() => setShowLineageFlow(!showLineageFlow)}
            className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all',
              showLineageFlow
                ? 'bg-accent-lineage/10 text-accent-lineage'
                : 'bg-black/5 dark:bg-white/10 text-ink-muted',
            )}
          >
            <GitBranch className="w-3.5 h-3.5" />
            {showLineageFlow ? 'Flow On' : 'Flow Off'}
          </button>
          <button
            onClick={semanticZoom.toggle}
            className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all',
              semanticZoom.isEnabled
                ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                : 'bg-black/5 dark:bg-white/10 text-ink-muted',
            )}
            title="Semantic Zoom: auto-expand/collapse entities based on zoom level"
          >
            <ZoomIn className="w-3.5 h-3.5" />
            {semanticZoom.isEnabled ? 'LOD On' : 'LOD Off'}
          </button>
        </div>
      </div>

      {/* Trace Toolbar — full controls, only when tracing (same as ContextViewCanvas/HierarchyCanvas) */}
      <AnimatePresence>
        {trace.isTracing && (
          <div className="absolute top-14 left-1/2 -translate-x-1/2 z-20 pointer-events-auto">
            <TraceToolbar
              focusNodeName={displayMap.get(trace.focusId || '')?.name || trace.focusId || 'Unknown'}
              upstreamCount={trace.upstreamCount}
              downstreamCount={trace.downstreamCount}
              showUpstream={trace.showUpstream}
              showDownstream={trace.showDownstream}
              onToggleUpstream={() => trace.setShowUpstream(!trace.showUpstream)}
              onToggleDownstream={() => trace.setShowDownstream(!trace.showDownstream)}
              onExitTrace={() => {
                trace.clearTrace()
                setExpandedNodes(new Set())
              }}
              onRetrace={trace.retrace}
              onTraceUpstream={() => trace.focusId && trace.traceUpstream(trace.focusId)}
              onTraceDownstream={() => trace.focusId && trace.traceDownstream(trace.focusId)}
              onTraceFullLineage={() => trace.focusId && trace.traceFullLineage(trace.focusId)}
              config={trace.config}
              onConfigChange={trace.setConfig}
              traceResult={trace.result}
              statistics={trace.statistics}
              isLoading={trace.isLoading}
              availableLineageEdgeTypes={lineageEdgeTypes}
              position="top"
            />
          </div>
        )}
      </AnimatePresence>

      {/* React Flow Canvas */}
      <div className="flex-1">
        <ReactFlow
          onInit={setRfInstance}
          onMoveEnd={(_, viewport) => handleViewportChange(viewport)}
          nodes={displayNodes}
          edges={displayEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          onNodeDoubleClick={onNodeDoubleClick}
          onNodeMouseEnter={onNodeMouseEnter}
          onNodeMouseLeave={onNodeMouseLeave}
          onPaneClick={onPaneClick}
          onConnect={onConnect}
          onReconnect={onReconnect}
          onEdgeClick={onEdgeClick}
          onEdgeContextMenu={onEdgeContextMenu}
          onDrop={onDrop}
          onDragOver={onDragOver}
          isValidConnection={isValidConnection}
          onNodeContextMenu={(event, node) => {
            event.preventDefault()
            interactions.openContextMenu(event as any, {
              type: 'node',
              id: node.id,
              data: node.data as Record<string, unknown>,
            })
          }}
          defaultEdgeOptions={{
            type: 'lineage',
            animated: true,
            interactionWidth: 20,
          }}
          selectionOnDrag
          multiSelectionKeyCode="Shift"
          selectionMode={SelectionMode.Partial}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.05}
          maxZoom={2}
          className="bg-canvas"
          proOptions={{ hideAttribution: true }}
        >
          {showGrid && (
            <Background
              variant={BackgroundVariant.Dots}
              gap={20}
              size={1}
              className="opacity-40"
            />
          )}
          <CanvasControls />
          {showMinimap && (
            <MiniMap
              nodeColor={minimapNodeColor}
              maskColor="rgba(0, 0, 0, 0.1)"
              className={cn(
                'glass-panel-subtle !rounded-xl !overflow-hidden',
                '!bottom-4 !right-4',
              )}
              pannable
              zoomable
            />
          )}
          <Controls
            className={cn(
              'glass-panel-subtle !rounded-xl !overflow-hidden !shadow-lg',
              '!bottom-4 !left-4',
            )}
            showInteractive={false}
          />
          {isLoadingChildren && (
            <div className="absolute top-20 right-4 glass-panel-subtle rounded-lg px-3 py-2 flex items-center gap-2">
              <div className="w-3 h-3 border-2 border-accent-lineage border-t-transparent rounded-full animate-spin" />
              <span className="text-xs text-ink-secondary">Loading children...</span>
            </div>
          )}
        </ReactFlow>
      </div>

      {/* Stats Bar */}
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 flex items-center gap-3">
        <button
          onClick={toggleDirection}
          className="glass-panel-subtle rounded-lg px-3 py-1.5 flex items-center gap-2 hover:bg-accent-lineage/10 transition-colors"
          title={`Layout: ${direction === 'LR' ? 'Left to Right' : 'Top to Bottom'}`}
        >
          {direction === 'LR' ? (
            <ArrowRight className="w-3.5 h-3.5 text-accent-lineage" />
          ) : (
            <ArrowDown className="w-3.5 h-3.5 text-accent-lineage" />
          )}
          <span className="text-2xs text-ink-muted">{direction}</span>
        </button>
        {isLayouting && (
          <div className="glass-panel-subtle rounded-lg px-3 py-1.5 flex items-center gap-2">
            <Loader2 className="w-3 h-3 text-accent-lineage animate-spin" />
            <span className="text-2xs text-ink-muted">Layouting...</span>
          </div>
        )}
        <div className="glass-panel-subtle rounded-lg px-3 py-1.5 flex items-center gap-2">
          <span className="text-2xs text-ink-muted">
            {(() => {
              const visibleCount = displayNodes.filter((n) => !n.hidden).length
              return visibleCount < rawNodes.length
                ? `${visibleCount} of ${rawNodes.length} entities`
                : `${rawNodes.length} entities`
            })()} &middot; {displayEdges.length} relationships
          </span>
        </div>
        <button
          onClick={toggleEdgePanel}
          className={cn(
            'glass-panel-subtle rounded-lg px-3 py-1.5 flex items-center gap-2 transition-colors',
            isEdgePanelOpen && 'bg-accent-lineage/10 border-accent-lineage',
          )}
        >
          <GitBranch className="w-3.5 h-3.5 text-accent-lineage" />
          <span className="text-2xs text-ink-muted">Edge Details</span>
        </button>
      </div>

      {/* Edge Legend */}
      <div
        className={cn(
          'absolute bottom-40 z-30 w-64 pointer-events-auto transition-all duration-300 ease-out',
          selectedNodeId ? 'right-[420px]' : 'right-4',
        )}
      >
        <EdgeLegend defaultExpanded={false} visibleEdges={allVisibleEdges} />
      </div>

      {/* Panels */}
      <AnimatePresence>
        {!drawerNodeId && isEdgePanelOpen && (
          <EdgeDetailPanel
            isOpen={isEdgePanelOpen}
            onClose={closeEdgePanel}
            edgeFilters={dynamicEdgeFilters}
            onToggleFilter={toggleEdgeFilter}
          />
        )}
      </AnimatePresence>
      <EntityDrawer
        onTraceUp={(nodeId) => trace.traceUpstream(nodeId)}
        onTraceDown={(nodeId) => trace.traceDownstream(nodeId)}
        onFullTrace={(nodeId) => trace.traceFullLineage(nodeId)}
        onFocusNode={revealAndFocus}
        onLocateMany={locateManyOnCanvas}
      />

      {/* UX Components */}
      <CanvasContextMenu
        isOpen={interactions.state.contextMenu.isOpen}
        position={interactions.state.contextMenu.position}
        target={interactions.state.contextMenu.target}
        onClose={interactions.closeContextMenu}
        onEditNode={interactions.editNode}
        onDuplicateNode={interactions.duplicateNode}
        onDeleteNode={interactions.deleteNode}
        onCreateChild={interactions.createChild}
        onTraceNode={(id) => trace.startTrace(id)}
        onCopyUrn={interactions.copyUrn}
        onEditEdge={interactions.editEdge}
        onDeleteEdge={interactions.deleteEdge}
        onReverseEdge={interactions.reverseEdge}
        onCreateNode={(pos) => interactions.openQuickCreate(pos)}
        onSelectAll={interactions.selectAll}
      />
      <InlineNodeEditor
        nodeId={interactions.state.inlineEdit.nodeId}
        value={interactions.state.inlineEdit.value}
        position={interactions.state.inlineEdit.position}
        onSave={interactions.saveInlineEdit}
        onCancel={interactions.cancelInlineEdit}
      />
      <QuickCreateNode
        isOpen={interactions.state.quickCreate.isOpen}
        position={interactions.state.quickCreate.position}
        parentUrn={interactions.state.quickCreate.parentUrn}
        onClose={interactions.closeQuickCreate}
        onCreated={(nodeId) => selectNode(nodeId)}
        variant="centered"
      />
      <CommandPalette
        isOpen={interactions.state.commandPalette.isOpen}
        onClose={interactions.closeCommandPalette}
        onCreateEntity={(_typeId) => {
          interactions.closeCommandPalette()
          interactions.openQuickCreate({
            x: window.innerWidth / 2,
            y: window.innerHeight / 2,
          })
        }}
        onSelectEntity={(entityId) => selectNode(entityId)}
      />

      {/* Edge Type Picker — ontology-driven relationship selection */}
      {edgePicker.isOpen && edgePicker.connection && (
        <>
          <div
            className="fixed inset-0 z-[60] bg-black/20 backdrop-blur-[1px]"
            onClick={() => setEdgePicker(prev => ({ ...prev, isOpen: false, connection: null }))}
          />
          <div
            className="fixed z-[61] bg-canvas-elevated border border-glass-border rounded-xl shadow-2xl min-w-[280px] max-w-[360px] overflow-hidden"
            style={{
              left: Math.min(edgePicker.position.x, window.innerWidth - 380),
              top: Math.max(edgePicker.position.y - 10, 40),
              transform: 'translateX(-50%)',
            }}
          >
            {/* Header */}
            <div className="px-4 py-3 border-b border-glass-border bg-canvas-elevated/80">
              <p className="text-sm font-semibold text-ink">Connect Entities</p>
              <p className="text-xs text-ink-muted mt-1">
                <span className="font-medium text-ink">{edgePicker.connection.sourceName}</span>
                <span className="mx-1.5 text-ink-muted/50">→</span>
                <span className="font-medium text-ink">{edgePicker.connection.targetName}</span>
              </p>
            </div>

            {/* Grouped by category */}
            <div className="py-1.5 px-1.5 max-h-[300px] overflow-y-auto">
              {/* Lineage edges first */}
              {edgePicker.validTypes.filter(rt => rt._isLineage).length > 0 && (
                <div className="mb-1">
                  <p className="px-2.5 py-1 text-2xs font-semibold text-accent-lineage uppercase tracking-wider">
                    Lineage
                  </p>
                  {edgePicker.validTypes.filter(rt => rt._isLineage).map(rt => (
                    <button
                      key={rt.id}
                      className="w-full text-left px-3 py-2 rounded-lg text-sm transition-colors hover:bg-accent-lineage/10"
                      onClick={() => edgePicker.connection && createEdgeWithType(edgePicker.connection.source, edgePicker.connection.target, rt.id)}
                    >
                      <div className="flex items-center gap-2">
                        <div className="w-1.5 h-1.5 rounded-full bg-accent-lineage" />
                        <span className="font-medium text-ink">{rt.name}</span>
                      </div>
                      {rt.description && (
                        <p className="text-2xs text-ink-muted mt-0.5 ml-3.5 line-clamp-2">{rt.description}</p>
                      )}
                    </button>
                  ))}
                </div>
              )}

              {/* Containment edges */}
              {edgePicker.validTypes.filter(rt => rt._isContainment).length > 0 && (
                <div className="mb-1">
                  <p className="px-2.5 py-1 text-2xs font-semibold text-slate-500 uppercase tracking-wider">
                    Containment
                  </p>
                  {edgePicker.validTypes.filter(rt => rt._isContainment).map(rt => (
                    <button
                      key={rt.id}
                      className="w-full text-left px-3 py-2 rounded-lg text-sm transition-colors hover:bg-slate-100 dark:hover:bg-slate-800"
                      onClick={() => edgePicker.connection && createEdgeWithType(edgePicker.connection.source, edgePicker.connection.target, rt.id)}
                    >
                      <div className="flex items-center gap-2">
                        <div className="w-1.5 h-1.5 rounded-full bg-slate-400" />
                        <span className="font-medium text-ink">{rt.name}</span>
                      </div>
                      {rt.description && (
                        <p className="text-2xs text-ink-muted mt-0.5 ml-3.5 line-clamp-2">{rt.description}</p>
                      )}
                    </button>
                  ))}
                </div>
              )}

              {/* Other/association edges */}
              {edgePicker.validTypes.filter(rt => !rt._isLineage && !rt._isContainment).length > 0 && (
                <div>
                  <p className="px-2.5 py-1 text-2xs font-semibold text-amber-600 dark:text-amber-400 uppercase tracking-wider">
                    Association
                  </p>
                  {edgePicker.validTypes.filter(rt => !rt._isLineage && !rt._isContainment).map(rt => (
                    <button
                      key={rt.id}
                      className="w-full text-left px-3 py-2 rounded-lg text-sm transition-colors hover:bg-amber-50 dark:hover:bg-amber-900/20"
                      onClick={() => edgePicker.connection && createEdgeWithType(edgePicker.connection.source, edgePicker.connection.target, rt.id)}
                    >
                      <div className="flex items-center gap-2">
                        <div className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                        <span className="font-medium text-ink">{rt.name}</span>
                      </div>
                      {rt.description && (
                        <p className="text-2xs text-ink-muted mt-0.5 ml-3.5 line-clamp-2">{rt.description}</p>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

export default GraphCanvas
