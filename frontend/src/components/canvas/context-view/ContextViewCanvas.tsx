/**
 * ContextViewCanvas - Enterprise-grade Context View with User-Defined Layers
 *
 * Displays entities in a horizontal left-to-right flow with:
 * - User-defined layer columns (Source → Staging → Refinery → Report)
 * - Collapsible containers within each layer
 * - Entities flow from left (sources) to right (consumers)
 * - Configurable layer definitions via schema
 * - Lineage flow overlay support
 * - Backend-persisted blueprints (Save / Load / Quick Start Templates)
 *
 * Orchestrator component — delegates layer assignment, edge projection,
 * highlight state, and rendering to extracted hooks and components.
 */

import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { AnimatePresence } from 'framer-motion'
import { cn } from '@/lib/utils'
import { fetchWithTimeout } from '@/services/fetchWithTimeout'
import {
  useSchemaStore,
  normalizeEdgeType,
  useEdgeTypeMetadataMap,
} from '@/store/schema'
import {
  useViewContainmentEdgeTypes,
  useViewLineageEdgeTypes,
  useViewIsContainmentEdge,
  useViewRelationshipTypes,
  useViewEntityTypes,
} from '@/hooks/useViewSchema'
import { useCanvasStore, useCanvasVersion } from '@/store/canvas'
import { useInstanceAssignments, useReferenceModelStore } from '@/store/referenceModelStore'
import { useWorkspacesStore } from '@/store/workspaces'
import { useGraphProvider } from '@/providers'
import type { TraceV2Result } from '@/providers/GraphDataProvider'
import { useGraphHydration } from '@/hooks/useGraphHydration'
import { useAggregatedLineage } from '@/hooks/useAggregatedLineage'
import { EdgeDetailPanel, generateEdgeTypeFilters } from '../../panels/EdgeDetailPanel'
import { EntityDrawer } from '../../panels/EntityDrawer'
import { EntityCreationPanel } from '../../panels/EntityCreationPanel'
import { EdgeLegend } from '../EdgeLegend'

import { useUnifiedTrace } from '@/hooks/useUnifiedTrace'
import { useEdgeDetailPanel, useEdgeTypeFilters } from '@/hooks/useEdgeFilters'
import { getEdgeTypeDefinition } from '@/utils/edgeTypeUtils'

// UX-first interaction components
import { CanvasContextMenu } from '../CanvasContextMenu'
import { InlineNodeEditor } from '../InlineNodeEditor'
import { QuickCreateNode } from '../QuickCreateNode'
import { CommandPalette } from '../CommandPalette'
import { useCanvasInteractions } from '@/hooks/useCanvasInteractions'
import { useCanvasKeyboard } from '@/hooks/useCanvasKeyboard'

// Editor components (shared across canvases)
import { EditorToolbar } from '../EditorToolbar'
import { NodePalette } from '../NodePalette'

import type { ViewLayerConfig, LogicalNodeConfig } from '@/types/schema'

// Extracted types, constants, hooks, and components
import { defaultReferenceModelLayers } from './constants'
import { useLayerAssignment } from '@/hooks/useLayerAssignment'
import { useContainmentHierarchy } from '@/hooks/useContainmentHierarchy'
import { useEdgeProjection } from '@/hooks/useEdgeProjection'
import { useHighlightState, useHoverHighlight, useHoveredNodeId } from '@/hooks/useHighlightState'
import { useTraceFilteredHierarchy } from '@/hooks/useTraceFilteredHierarchy'
import { computeTraceMergeSpine } from '@/hooks/lib/traceMergeSpine'
import { LayerColumn } from './LayerColumn'
import { LineageFlowOverlay, EXTREMITY_EDGE_GUTTER_PX } from './LineageFlowOverlay'
import { ContextViewHeader } from './ContextViewHeader'
import { useLoadingToast } from '@/components/ui/toast'
import { useStagedChangesStore } from '@/store/stagedChangesStore'
import { StagedChangesPanel } from './StagedChangesPanel'
import { TraceBottomDock } from '../trace/TraceBottomDock'

// Re-export for backward compatibility
export { defaultReferenceModelLayers } from './constants'

export interface ContextViewCanvasProps {
  className?: string
  layers?: ViewLayerConfig[]
  showLineageFlow?: boolean
}

export function ContextViewCanvas({
  className,
  layers = defaultReferenceModelLayers,
  showLineageFlow: initialShowLineageFlow = true
}: ContextViewCanvasProps) {
  const nodes = useCanvasStore((s) => s.nodes)
  const edges = useCanvasStore((s) => s.edges)
  const addNodes = useCanvasStore((s) => s.addNodes)
  const addEdges = useCanvasStore((s) => s.addEdges)
  const removeEdgesByNodeIds = useCanvasStore((s) => s.removeEdgesByNodeIds)
  const removeStoreEdges = useCanvasStore((s) => s.removeEdges)
  const selectNode = useCanvasStore((s) => s.selectNode)
  const selectedNodeIds = useCanvasStore((s) => s.selectedNodeIds)
  const selectedNodeId = selectedNodeIds[0] ?? null
  const drawerNodeId = useCanvasStore((s) => s.drawerNodeId)
  const closeNodeDrawer = useCanvasStore((s) => s.closeNodeDrawer)
  const schema = useSchemaStore((s) => s.schema)
  const activeView = useSchemaStore((s) => s.getActiveView())
  const provider = useGraphProvider()
  const containmentEdgeTypes = useViewContainmentEdgeTypes()
  const lineageEdgeTypes = useViewLineageEdgeTypes()
  const isContainmentEdge = useViewIsContainmentEdge()
  const edgeTypeMetadata = useEdgeTypeMetadataMap()

  // URN resolver for trace
  const urnResolver = useCallback((nodeId: string) => {
    const node = nodes.find(n => n.id === nodeId)
    return (node?.data?.urn as string) || nodeId
  }, [nodes])

  // Unified Trace System - replaces local trace state
  const trace = useUnifiedTrace({
    provider,
    urnResolver,
    onTraceComplete: async (result) => {
      console.log('[ReferenceModelCanvas] Trace complete:', result.traceNodes.size, 'nodes')

      // Auto-enable lineage flow so edges are visible
      setShowLineageFlow(true)

      // Merge trace result into the canvas store using a spine-based strategy
      // (see frontend/src/hooks/lib/traceMergeSpine.ts).
      //
      // Why not "all" or "nothing":
      //   - Blanket-merging every node the backend returned re-parents existing
      //     canvas nodes under alien ancestors (e.g. Snowflake stealing
      //     REPORTING) via useLayerAssignment's "children inherit parent's
      //     layer" HARD RULE — destroys legitimate placements.
      //   - Blanket-dropping every ancestor (the previous approach) leaves new
      //     lineage participants floating with no path to a layer root, so
      //     useLayerAssignment marks them unassigned and useEdgeProjection
      //     silently drops their edges (the "trace returns nodes but UI shows
      //     no lineage" bug).
      //
      // The spine helper returns the minimum ancestor chain needed to route
      // each new participant up to a node the canvas already places. We merge
      // participants + spine, then attach an `assignmentHint` to spine roots
      // whose chain never reached a known anchor — useLayerAssignment honours
      // that hint as a last-resort fallback so the lineage stays visible.
      if (result.lineageResult) {
        const lr = result.lineageResult

        const participantUrns = new Set<string>()
        result.traceNodes.forEach(u => participantUrns.add(u))
        lr.upstreamUrns.forEach(u => participantUrns.add(u))
        lr.downstreamUrns.forEach(u => participantUrns.add(u))

        const knownAssignedUrns = new Set<string>(displayMap.keys())
        const { spineUrns, unreachableRoots } = computeTraceMergeSpine({
          participantUrns,
          containmentEdges: result.containmentEdges ?? [],
          knownAssignedUrns,
        })

        // Determine the focus's effective layer for the assignmentHint
        // fallback. In ContextViewCanvas the canvas node id == urn, so we
        // can look it up directly. Outside trace mode this map is rebuilt
        // every render; reading it here is O(1).
        const focusLayerId = result.focusId ? nodeLayerMap.get(result.focusId) : undefined

        const shouldMergeNode = (urn: string): boolean =>
          (participantUrns.has(urn) || spineUrns.has(urn)) && !knownAssignedUrns.has(urn)

        const newCanvasNodes = lr.nodes
          .filter(gn => shouldMergeNode(gn.urn))
          .map(gn => {
            const metadata: Record<string, unknown> = {
              ...gn.properties,
              childCount: gn.childCount,
              sourceSystem: gn.sourceSystem,
            }
            if (unreachableRoots.has(gn.urn) && focusLayerId) {
              metadata.assignmentHint = focusLayerId
            }
            return {
              id: gn.urn,
              type: 'default' as const,
              position: { x: 0, y: 0 },
              data: {
                label: gn.displayName,
                urn: gn.urn,
                type: gn.entityType,
                classifications: gn.tags ?? [],
                metadata,
              },
            }
          })
        if (newCanvasNodes.length > 0) {
          addNodes(newCanvasNodes as any[])
        }

        // Lineage edges: both endpoints must be either newly-merged or
        // already on the canvas. Drops only the rare edge whose endpoint
        // is an ancestor the spine excluded — those dangle.
        const isResolvableEndpoint = (urn: string): boolean =>
          shouldMergeNode(urn) || knownAssignedUrns.has(urn)
        const newCanvasEdges = lr.edges
          .filter(ge => isResolvableEndpoint(ge.sourceUrn) && isResolvableEndpoint(ge.targetUrn))
          .map(ge => ({
            id: ge.id,
            source: ge.sourceUrn,
            target: ge.targetUrn,
            data: {
              edgeType: ge.edgeType,
              relationship: ge.edgeType,
              confidence: ge.confidence,
            },
          }))
        if (newCanvasEdges.length > 0) {
          addEdges(newCanvasEdges as any[])
          trace.recordAddedEdgeIds(newCanvasEdges.map(e => e.id))
        }

        // Containment edges: only when the TARGET (child) is a newly-merged
        // node. Never add an edge whose target is already on the canvas —
        // that would re-parent an existing node under an alien ancestor and
        // collapse its layer assignment via the HARD RULE.
        const newContainmentEdges = (result.containmentEdges ?? [])
          .filter(ge => shouldMergeNode(ge.targetUrn) && isResolvableEndpoint(ge.sourceUrn))
          .map(ge => ({
            id: ge.id,
            source: ge.sourceUrn,
            target: ge.targetUrn,
            data: {
              edgeType: ge.edgeType,
              relationship: ge.edgeType,
              confidence: ge.confidence,
            },
          }))
        if (newContainmentEdges.length > 0) {
          addEdges(newContainmentEdges as any[])
          trace.recordAddedEdgeIds(newContainmentEdges.map(e => e.id))
        }

        // Auto-expand only the focus's containment chain. Expanding every
        // participant's container would unfold the entire layered canvas for
        // a hub focus (a Campaigns object with 216 downstream participants
        // would expand 17 entity types × 4 layers worth of containers,
        // rendering tens of thousands of edges on initial trace). Leaving
        // other participants rolled up means the user sees AGGREGATED
        // edges between containers; expanding a container drills via
        // autoDrillOnExpand to reveal its lineage participants on demand.
        const nodesToExpand = new Set(expandedNodes)
        const allCurrentEdges = [...edges, ...newCanvasEdges, ...newContainmentEdges]
        const traceParentMap = new Map<string, string>()
        allCurrentEdges.forEach(e => {
          if (isContainmentEdge(normalizeEdgeType(e))) {
            traceParentMap.set(e.target ?? (e as any).targetUrn, e.source ?? (e as any).sourceUrn)
          }
        })

        if (result.focusId) {
          let curr = traceParentMap.get(result.focusId)
          while (curr) {
            if (nodesToExpand.has(curr)) break  // already on the walk
            nodesToExpand.add(curr)
            curr = traceParentMap.get(curr)
          }
        }

        setExpandedNodes(nodesToExpand)
      }
    }
  })

  // Forward-declared ref to the smart-level trace handler — defined further
  // down where granularityOptions is in scope. Used by hooks that fire
  // before that declaration (useCanvasInteractions options) so the
  // closure dereferences lazily.
  const startTraceRef = useRef<(nodeId: string) => void>(() => {})
  const toggleTraceRef = useRef<(nodeId: string) => void>(() => {})

  // Exit-trace cleanup. Purges the edges the trace merged into the canvas
  // store so the ambient edge mesh doesn't permanently inherit them.
  const exitTrace = useCallback(() => {
    if (!trace.isTracing) return false
    const idsToRemove = Array.from(trace.addedEdgeIds)
    trace.clearTrace()
    if (idsToRemove.length > 0) removeStoreEdges(idsToRemove)
    trace.resetAddedEdgeIds()
    setExpandedNodes(new Set())
    return true
  }, [trace, removeStoreEdges])

  // UX-first Canvas Interactions (context menu, inline edit, quick create, command palette)
  const interactions = useCanvasInteractions({
    onTraceNode: (nodeId) => startTraceRef.current(nodeId),
    onNodeCreated: (nodeId) => selectNode(nodeId),
    layers: layers,
    onMoveToLayer: (_nodeId, _layerId) => {
      // Implementation handled by the existing moveToLayer function
    },
    onCloseEdgePanel: () => {
      if (isEdgePanelOpen) { closeEdgePanel(); return true }
      return false
    },
    onCloseEntityDrawer: () => {
      if (isStagedPanelOpen) { closeStagedChangesPanel(); return true }
      if (drawerNodeId) { closeNodeDrawer(); clearSelection(); return true }
      return false
    },
    // ESC exits an active trace before any other panel close — gives the
    // user a single, predictable escape from a busy trace view.
    onExitTrace: exitTrace,
  })

  // Keyboard shortcuts
  useCanvasKeyboard({
    enabled: true,
    handlers: interactions.keyboardHandlers,
  })

  // Aggregated lineage for progressive edge disclosure
  const {
    aggregatedEdges,
    fetchAggregated,
    clearCache: clearAggregationCache,
    isLoading: isLoadingAggregatedEdges,
    granularity: lineageGranularity,
    setGranularity: setLineageGranularity,
    truncated: aggregationTruncated,
    purgeEdgesIncidentToUrns: purgeAggregatedEdgesIncidentToUrns,
  } = useAggregatedLineage({ granularity: null })

  // Instance-level assignments from store (user drag-and-drop)
  const instanceAssignments = useInstanceAssignments()
  const effectiveAssignments = useReferenceModelStore(s => s.effectiveAssignments)
  const computeAssignments = useReferenceModelStore(s => s.computeAssignments)
  const assignmentStatus = useReferenceModelStore(s => s.assignmentStatus)
  const setLayers = useReferenceModelStore(s => s.setLayers)
  const storeLayers = useReferenceModelStore(s => s.layers)
  const syncStatus = useReferenceModelStore(s => s.syncStatus)
  const activeContextModelName = useReferenceModelStore(s => s.activeContextModelName)
  const saveToBackend = useReferenceModelStore(s => s.saveToBackend)
  const assignEntityToLayer = useReferenceModelStore(s => s.assignEntityToLayer)
  const activeWorkspaceId = useWorkspacesStore(s => s.activeWorkspaceId)

  // Step 1: Sync view layers to store when activeView changes
  useEffect(() => {
    if (!activeView) return

    const viewLayers = activeView.layout?.referenceLayout?.layers
    if (!viewLayers || viewLayers.length === 0) return

    // Only sync if layers have changed (avoid unnecessary updates)
    const layersChanged =
      storeLayers.length !== viewLayers.length ||
      storeLayers.some((layer, idx) => {
        const viewLayer = viewLayers[idx]
        return !viewLayer ||
          layer.id !== viewLayer.id ||
          JSON.stringify(layer.entityAssignments) !== JSON.stringify(viewLayer.entityAssignments)
      })

    if (layersChanged) {
      setLayers(viewLayers)
    }
  }, [activeView?.id, activeView?.layout?.referenceLayout?.layers, setLayers, storeLayers])

  // Step 2: Load assignments from backend when layers are synced and nodes are available
  // Uses a ref to track what we've computed for, preventing cascading re-fetches.
  const assignmentComputedRef = useRef<string | null>(null)

  // Reset the assignment guard when the active view changes so recomputation
  // always happens for the new view (even if layer IDs happen to match).
  useEffect(() => {
    assignmentComputedRef.current = null
  }, [activeView?.id])

  useEffect(() => {
    if (nodes.length === 0 || !provider || storeLayers.length === 0) return
    if (assignmentStatus !== 'idle') return

    // Include activeView ID so switching between views with identical layer IDs
    // still triggers recomputation.
    const layerFingerprint = `${activeView?.id ?? ''}:${storeLayers.map(l => l.id).join(',')}`

    // Only compute once per unique view+layer configuration
    if (assignmentComputedRef.current === layerFingerprint) return
    assignmentComputedRef.current = layerFingerprint

    computeAssignments(provider)
  }, [nodes.length, provider, computeAssignments, assignmentStatus, storeLayers, activeView?.id])

  // Search state
  const [searchQuery, setSearchQuery] = useState('')

  // Entity creation state
  const [isCreatingEntity, setIsCreatingEntity] = useState(false)
  const [creationParentId, setCreationParentId] = useState<string | null>(null)
  const [creationLayerId, setCreationLayerId] = useState<string | null>(null)

  // Assignment warning state (shown when user tries to assign child to different layer)
  const [assignmentWarning, setAssignmentWarning] = useState<string | null>(null)
  const assignmentWarningTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handleAssignToLayer = useCallback((entityId: string, layerId: string) => {
    // Capture the previous layer for diff display before mutation.
    const prevAssignment = useReferenceModelStore.getState().effectiveAssignments.get(entityId)
    const prevLayerId = prevAssignment?.layerId
    const prevLayer = storeLayers.find(l => l.id === prevLayerId)
    const targetLayer = storeLayers.find(l => l.id === layerId)
    const entity = nodes.find(n => n.id === entityId || (n.data?.urn as string) === entityId)
    const entityName = (entity?.data?.label as string) ?? entityId

    const result = assignEntityToLayer(entityId, layerId)
    if (!result.success && result.conflict?.type === 'containment_locked') {
      setAssignmentWarning(result.conflict.message)
      // Auto-dismiss after 5 seconds
      if (assignmentWarningTimer.current) clearTimeout(assignmentWarningTimer.current)
      assignmentWarningTimer.current = setTimeout(() => setAssignmentWarning(null), 5000)
      return
    }

    // Surface the assignment in the staged-changes review panel.
    // Apply is a no-op because saveToBackend (referenceModelStore) is what
    // actually flushes layer assignments — calling it here would double-write.
    const stagedChanges = useStagedChangesStore.getState()
    stagedChanges.stageOrReplace(
      (c) => c.type === 'assign_layer' && c.targetId === entityId,
      {
        type: 'assign_layer',
        targetId: entityId,
        before: { layerId: prevLayerId, layerName: prevLayer?.name },
        after: { layerId, layerName: targetLayer?.name },
        summary: `Move '${entityName}' → ${targetLayer?.name ?? 'layer'}`,
        discard: () => {
          if (prevLayerId) {
            useReferenceModelStore.getState().assignEntityToLayer(entityId, prevLayerId)
          } else {
            useReferenceModelStore.getState().removeEntityAssignment(entityId)
          }
        },
      },
    )
  }, [assignEntityToLayer, storeLayers, nodes])

  // Expanded nodes state (for hierarchy expansion, not trace)
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())

  // Per-view expanded state: save/restore on view switch to prevent stale data
  const expandedByViewRef = useRef<Map<string, Set<string>>>(new Map())
  const prevViewIdRef = useRef<string | null>(null)

  useEffect(() => {
    const currentViewId = activeView?.id ?? null
    // Save current expanded state for the previous view
    if (prevViewIdRef.current && prevViewIdRef.current !== currentViewId) {
      expandedByViewRef.current.set(prevViewIdRef.current, new Set(expandedNodes))
    }
    // Restore or reset for the new view
    if (currentViewId !== prevViewIdRef.current) {
      const restored = expandedByViewRef.current.get(currentViewId ?? '') ?? new Set<string>()
      setExpandedNodes(restored)
      // Reset aggregation cache so stale data doesn't bleed into the new view
      prevAggregationKeyRef.current = ''
      clearAggregationCache()
    }
    prevViewIdRef.current = currentViewId
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeView?.id])

  // Edit Mode State (shared across canvases)
  const [isPaletteOpen, setPaletteOpen] = useState(false)
  const [activeEdgeType, setActiveEdgeType] = useState<string>('manual')
  const relationshipTypes = useViewRelationshipTypes()

  // Granularity options for the lineage aggregation selector — driven by the
  // active ontology's entity types, sorted coarsest-first (lowest level first).
  // Filtered to types that are valid lineage anchors (behavior.traceable=true)
  // — matches the trace v2 contract where only traceable entities can be the
  // level a trace runs at. Tags / glossary terms are excluded.
  const schemaEntityTypes = useViewEntityTypes()
  const granularityOptions = useMemo(
    () => schemaEntityTypes
      .filter(et => et.hierarchy?.level !== undefined)
      .filter(et => et.behavior?.traceable !== false)
      .map(et => ({ id: et.id, name: et.name, level: et.hierarchy.level })),
    [schemaEntityTypes]
  )

  // Auto-select the coarsest (lowest-level) granularity once options are
  // available. The toolbar no longer exposes a "no aggregation" option, so
  // null is not a valid resting state.
  useEffect(() => {
    if (lineageGranularity == null && granularityOptions.length > 0) {
      const coarsest = [...granularityOptions].sort((a, b) => a.level - b.level)[0]
      setLineageGranularity(coarsest.id)
    }
  }, [lineageGranularity, granularityOptions, setLineageGranularity])

  // Handle save graph
  const handleSave = useCallback(async () => {
    try {
      const response = await fetchWithTimeout('/api/v1/graph/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes, edges })
      })
      if (!response.ok) throw new Error('Failed to save graph')
      alert('Graph saved successfully!')
    } catch (error) {
      console.error('Error saving graph:', error)
      alert('Failed to save graph')
    }
  }, [nodes, edges])

  // Handle right click - now uses unified CanvasContextMenu
  const handleContextMenu = useCallback((e: React.MouseEvent, nodeId: string) => {
    e.preventDefault()
    e.stopPropagation()
    const node = nodes.find(n => n.id === nodeId)
    interactions.openContextMenu(e, {
      type: 'node',
      id: nodeId,
      data: node?.data as Record<string, unknown> || {},
    })
  }, [nodes, interactions])



  // Edge details
  const { isOpen: isEdgePanelOpen, toggle: toggleEdgePanel, close: closeEdgePanel } = useEdgeDetailPanel()
  const { filters: edgeFilters, toggle: toggleEdgeFilter } = useEdgeTypeFilters()
  const ontologyMetadata = useMemo(() => ({ edgeTypeMetadata }), [edgeTypeMetadata])
  const selectEdge = useCanvasStore((s) => s.selectEdge)

  // Generate dynamic edge filters from actual edges and schema
  const dynamicEdgeFilters = useMemo(() => {
    if (edges.length === 0) return edgeFilters
    return generateEdgeTypeFilters(
      edges,
      relationshipTypes,
      containmentEdgeTypes,
      ontologyMetadata
    )
  }, [edges, relationshipTypes, containmentEdgeTypes, ontologyMetadata, edgeFilters])

  // Schema-driven edge color resolver — used by LineageFlowOverlay
  // Resolves edge type → color from backend schema, falling back to defaults
  const resolveEdgeColor = useCallback((edgeType: string) => {
    return getEdgeTypeDefinition(
      edgeType,
      relationshipTypes,
      containmentEdgeTypes,
      ontologyMetadata ? { edgeTypeMetadata: ontologyMetadata.edgeTypeMetadata } : undefined
    ).color
  }, [relationshipTypes, containmentEdgeTypes, ontologyMetadata])

  // Double-click handler: inline edit (default) or trace (shift+double-click)
  const handleDoubleClick = useCallback(async (nodeId: string, event?: React.MouseEvent) => {
    // UX-first: Double-click = inline edit (modern approach)
    // Use Shift+Double-click for trace (power user feature)
    if (event && !event.shiftKey) {
      // Find the node element to get its position
      const element = document.getElementById(`layer-node-${nodeId}`)
      if (element) {
        const rect = element.getBoundingClientRect()
        const targetNode = nodes.find(n => n.id === nodeId)
        interactions.startInlineEdit(
          nodeId,
          (targetNode?.data?.label as string) || nodeId,
          { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
        )
        return
      }
    }

    // TRACE MODE: Toggle trace using unified trace hook + smart level
    toggleTraceRef.current(nodeId)
  }, [nodes, interactions])


  // Lineage flow toggle
  const [showLineageFlow, setShowLineageFlow] = useState(initialShowLineageFlow)

  // Edge direction toggle — controls arrowheads + animated mid-edge chevron
  const [showEdgeDirection, setShowEdgeDirection] = useState(true)

  // Trace bottom dock — expanded vs compact. Lifted to the canvas so a
  // global Cmd/Ctrl+I shortcut can toggle it from anywhere.
  const [dockExpanded, setDockExpanded] = useState(false)
  // Auto-collapse the dock when trace exits so a stale open state doesn't
  // immediately reappear next time the user starts a trace.
  useEffect(() => {
    if (!trace.isTracing && dockExpanded) setDockExpanded(false)
  }, [trace.isTracing, dockExpanded])
  // Cmd/Ctrl+I toggles the dock's expanded state while a trace is active.
  useEffect(() => {
    if (!trace.isTracing) return
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      const isMod = e.metaKey || e.ctrlKey
      if (!isMod || e.shiftKey) return
      if (e.key.toLowerCase() === 'i') {
        e.preventDefault()
        setDockExpanded(v => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [trace.isTracing])

  // Sync ontology-derived lineage edge types into trace config so the trace
  // backend traverses TRANSFORMS, AGGREGATED, and any other ontology-classified
  // lineage edges — not just AGGREGATED. (Issue #3)
  useEffect(() => {
    if (lineageEdgeTypes.length > 0) {
      trace.setConfig({ lineageEdgeTypes })
    }
  }, [lineageEdgeTypes, trace.setConfig])

  // Trace ALWAYS runs at the focus node's own level — `level: 'auto'` resolves
  // server-side to the focus's hierarchy.level, so a column-level focus traces
  // column-level lineage (TRANSFORMS, AGGREGATED, or any other ontology-
  // classified lineage edge type). The previous "auto-coarsen" hack broke
  // fine-grained TRANSFORMS lineage; removed.
  const startTraceWithSmartLevel = useCallback((nodeId: string) => {
    trace.setConfig({ level: 'auto', lineageEdgeTypes })
    return trace.startTrace(nodeId)
  }, [trace, lineageEdgeTypes])

  const toggleTraceWithSmartLevel = useCallback((nodeId: string) => {
    trace.setConfig({ level: 'auto', lineageEdgeTypes })
    return trace.toggleTrace(nodeId)
  }, [trace, lineageEdgeTypes])

  const traceUpstreamWithSmartLevel = useCallback((nodeId: string) => {
    trace.setConfig({ level: 'auto', lineageEdgeTypes })
    return trace.traceUpstream(nodeId)
  }, [trace, lineageEdgeTypes])

  const traceDownstreamWithSmartLevel = useCallback((nodeId: string) => {
    trace.setConfig({ level: 'auto', lineageEdgeTypes })
    return trace.traceDownstream(nodeId)
  }, [trace, lineageEdgeTypes])

  const traceFullLineageWithSmartLevel = useCallback((nodeId: string) => {
    trace.setConfig({ level: 'auto', lineageEdgeTypes })
    return trace.traceFullLineage(nodeId)
  }, [trace, lineageEdgeTypes])

  // Wire up the forward-declared refs (used by hooks that fire earlier in
  // render order, before granularityOptions is in scope).
  startTraceRef.current = startTraceWithSmartLevel
  toggleTraceRef.current = toggleTraceWithSmartLevel

  // Staged changes — review-before-save layer for all canvas edits
  const stagedChangeList = useStagedChangesStore(s => s.changes)
  const stagedRedoStack = useStagedChangesStore(s => s.redoStack)
  const isStagedPanelOpen = useStagedChangesStore(s => s.isReviewPanelOpen)
  const openStagedChangesPanel = useStagedChangesStore(s => s.openReviewPanel)
  const closeStagedChangesPanel = useStagedChangesStore(s => s.closeReviewPanel)
  const applyStagedChanges = useStagedChangesStore(s => s.applyAll)
  const undoStagedChange = useStagedChangesStore(s => s.undo)
  const redoStagedChange = useStagedChangesStore(s => s.redo)

  // Keyboard shortcuts for Undo/Redo — works anywhere on the canvas.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ignore when the user is typing in an input/textarea
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      const isMod = e.metaKey || e.ctrlKey
      if (!isMod) return
      const key = e.key.toLowerCase()
      if (key === 'z' && !e.shiftKey) {
        e.preventDefault()
        undoStagedChange()
      } else if ((key === 'z' && e.shiftKey) || key === 'y') {
        e.preventDefault()
        redoStagedChange()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undoStagedChange, redoStagedChange])

  // Save Blueprint button now OPENS the review modal first — the user
  // confirms inside the modal which then performs the actual apply + save.
  // This makes the save flow self-documenting: every save shows what's about
  // to happen, and the modal doubles as a "view pending changes" panel.
  const handleSaveAll = useCallback(() => {
    if (!activeWorkspaceId) return
    openStagedChangesPanel()
  }, [activeWorkspaceId, openStagedChangesPanel])

  // Ref to trigger edge redraw from child components
  const triggerEdgeRedrawRef = useRef<(() => void) | null>(null)

  // Horizontal scroll container — used by the drawer-aware autoscroll effect
  // below to keep the selected column in the un-occluded region whenever a
  // side panel (EntityDrawer / EdgeDetailPanel) is open.
  const horizontalScrollRef = useRef<HTMLDivElement | null>(null)
  const lastAutoScrolledForSelectionRef = useRef<string | null>(null)

  // Drawer-aware horizontal autoscroll: when a side panel opens (EntityDrawer
  // for selected nodes or EdgeDetailPanel for selected edges) the right edge
  // of the canvas is reserved via padding, but a node already sitting on the
  // far right would still be visually clipped. This effect smoothly slides the
  // selected node's column into the un-occluded region. One-shot per
  // selection so the user retains scroll control afterwards (mirrors the
  // trace-focus auto-scroll guard in LayerColumn).
  useEffect(() => {
    const drawerOpen = !!selectedNodeId
    if (!drawerOpen && !isEdgePanelOpen) {
      lastAutoScrolledForSelectionRef.current = null
      return
    }
    if (!selectedNodeId) return
    if (lastAutoScrolledForSelectionRef.current === selectedNodeId) return

    const layerId = effectiveAssignments.get(selectedNodeId)?.layerId
    if (!layerId) return

    // Defer two frames: first to let React commit the padding change, second
    // to let layout settle so getBoundingClientRect reads the new geometry.
    let cancelRaf2: number | null = null
    const raf1 = requestAnimationFrame(() => {
      cancelRaf2 = requestAnimationFrame(() => {
        const container = horizontalScrollRef.current
        if (!container) return
        const column = container.querySelector(
          `[data-layer-id="${CSS.escape(layerId)}"]`,
        ) as HTMLElement | null
        if (!column) return

        // Read actual rendered panel widths (responsive clamp() values) so the
        // math doesn't over- or under-shift on different viewport sizes.
        const drawerEl = document.querySelector('[data-panel="entity-drawer"]') as HTMLElement | null
        const edgePanelEl = document.querySelector('[data-panel="edge-detail-panel"]') as HTMLElement | null
        const reservedRight = drawerEl?.offsetWidth ?? edgePanelEl?.offsetWidth ?? 0

        const cRect = container.getBoundingClientRect()
        const colRect = column.getBoundingClientRect()
        const margin = 24

        const viewportLeft = cRect.left
        const viewportRight = cRect.right - reservedRight

        let delta = 0
        if (colRect.right > viewportRight) {
          delta = colRect.right - viewportRight + margin
        } else if (colRect.left < viewportLeft) {
          delta = colRect.left - viewportLeft - margin
        }

        if (delta !== 0) {
          container.scrollTo({
            left: container.scrollLeft + delta,
            behavior: 'smooth',
          })
          // Lineage edges measure node positions from the DOM; redraw once the
          // smooth scroll has settled so trace edges follow the column.
          setTimeout(() => triggerEdgeRedrawRef.current?.(), 350)
        }
        lastAutoScrolledForSelectionRef.current = selectedNodeId
      })
    })
    return () => {
      cancelAnimationFrame(raf1)
      if (cancelRaf2 != null) cancelAnimationFrame(cancelRaf2)
    }
  }, [selectedNodeId, isEdgePanelOpen, effectiveAssignments])

  const handleLayerScroll = useCallback(() => {
    if (triggerEdgeRedrawRef.current) {
      triggerEdgeRedrawRef.current()
    }
  }, [])

  // Callback for animation completion to trigger edge redraw
  const handleAnimationComplete = useCallback(() => {
    // Small delay to ensure DOM is fully updated after animation
    requestAnimationFrame(() => {
      if (triggerEdgeRedrawRef.current) {
        triggerEdgeRedrawRef.current()
      }
    })
  }, [])

  // Sort layers by order
  const activeLayers = useMemo(() => {
    if (layers && layers !== defaultReferenceModelLayers && layers.length > 0) return layers
    if (activeView?.layout?.referenceLayout?.layers?.length) return activeView.layout.referenceLayout.layers
    return defaultReferenceModelLayers
  }, [layers, activeView])

  const sortedLayers = useMemo(() =>
    [...activeLayers].sort((a, b) => a.order - b.order),
    [activeLayers]
  )

  // Monotonic version counter — replaces brittle fingerprint sampling.
  // Incremented automatically by canvas store middleware on every node/edge mutation.
  const canvasVersion = useCanvasVersion()
  const nodeEdgeFingerprint = `${activeView?.id ?? ''}:${canvasVersion}`

  // Build containment hierarchy using shared hook (incremental updates).
  const { nodeMap, childMap, parentMap } = useContainmentHierarchy({
    nodes, edges, isContainmentEdge, fingerprint: nodeEdgeFingerprint,
  })

  // Helper: Calculate currently visible top-level nodes (containers)
  const getVisibleContainerUrns = useCallback(() => {
    return nodes
      .filter(n => {
        const parentId = parentMap.get(n.id)
        if (!parentId) return true // Root
        return expandedNodes.has(parentId)
      })
      .map(n => (n.data?.urn as string) || n.id)
      .filter(Boolean)
  }, [nodes, parentMap, expandedNodes])

  // Track previous aggregation target fingerprint to avoid redundant fetches
  const prevAggregationKeyRef = useRef<string>('')

  // Stable node URN-to-ID map (updated via ref to avoid effect dependency on nodes)
  const nodesRef = useRef(nodes)
  nodesRef.current = nodes

  // Optimized Effect: Fetch aggregated edges only when the visible set actually changes
  // Uses expandedNodes (user-driven) as the primary trigger, not nodes array reference.
  // A 500ms debounce coalesces rapid expand/collapse actions.
  //
  // GATED ON !trace.isTracing: skeleton-first /trace v2 returns AGGREGATED
  // edges at the trace's effective level, so the parallel /aggregated-lineage
  // fetch is redundant + racy when a trace is active. In browse mode the
  // hook fires as before.
  useEffect(() => {
    if (!showLineageFlow || nodes.length === 0) return
    if (trace.isTracing) return

    const fetchDebounced = setTimeout(() => {
      const currentVisibleList = getVisibleContainerUrns()

      // Exclude expanded nodes from aggregation targets.
      // When a node is expanded, its children are already in the visible list and will
      // represent it. Including BOTH parent and children causes the Cypher CONTAINS*0..5
      // traversal to find the same TRANSFORMS edges at multiple hierarchy levels,
      // producing duplicate/inflated aggregated edge counts.
      // (Earlier this caused missing lineage because orphan nodes like Snowflake weren't
      // loaded — that's now fixed by the initial graph load fetching orphan nodes.)
      const urnToIdMap = new Map(nodesRef.current.map(n => [(n.data?.urn as string) || n.id, n.id]))
      const aggregationTargets = currentVisibleList.filter(urn => {
        const nodeId = urnToIdMap.get(urn)
        return nodeId && !expandedNodes.has(nodeId)
      })

      // Only fetch if the target set actually changed
      const aggregationKey = aggregationTargets.sort().join(',')
      if (aggregationKey === prevAggregationKeyRef.current) return
      prevAggregationKeyRef.current = aggregationKey

      if (aggregationTargets.length > 0) {
        fetchAggregated(aggregationTargets, aggregationTargets)
      }
    }, 150) // Snappy refetch on expand/collapse — old 500ms felt laggy
            // when iteratively drilling. 150ms is still long enough to
            // coalesce a rapid sequence of clicks but feels live.

    return () => clearTimeout(fetchDebounced)
  }, [showLineageFlow, getVisibleContainerUrns, fetchAggregated, nodes.length, expandedNodes, trace.isTracing])

  // === Extracted Hooks ===

  // Layer assignment: rules, nodesByLayer, displayFlat, displayMap, urnToIdMap, nodeLayerMap
  const { nodesByLayer, displayFlat, displayMap, urnToIdMap, nodeLayerMap } = useLayerAssignment({
    nodes, sortedLayers, nodeEdgeFingerprint,
    instanceAssignments, effectiveAssignments,
    nodeMap, childMap, parentMap,
  })

  // Trace filter — when a trace is active, hides everything outside the trace
  // context (traced URNs + drilldown URNs + their containment ancestors).
  // When trace is off, returns the inputs unchanged with no allocation.
  // Used by LayerColumn / edge projection so expansion reveals only traced
  // descendants, recursively to any depth.
  const {
    filteredByLayer, filteredFlat, filteredMap, contextSet: traceContextSet,
  } = useTraceFilteredHierarchy({
    nodesByLayer, displayFlat, displayMap,
    isTracing: trace.isTracing,
    traceNodes: trace.result?.traceNodes ?? new Set<string>(),
    drilldowns: trace.drilldowns,
    parentMap,
    childMap,
    expandedNodes,
  })

  // The hook returns the inputs unchanged when !isTracing, so these
  // assignments are effectively a no-op outside trace mode.
  const renderByLayer = trace.isTracing ? filteredByLayer : nodesByLayer
  const renderFlat = trace.isTracing ? filteredFlat : displayFlat
  const renderMap = trace.isTracing ? filteredMap : displayMap

  // Suppress parent AGGREGATED edges whose drill currently has at least one
  // finer-level edge visible. Without this the canvas renders the same
  // lineage twice — once at the parent level (e.g. Dataset↔Dataset AGG) and
  // once at the child level (Column↔Column). Keying on the URN pair lets
  // useEdgeProjection skip both Section A (`aggregatedEdges`-derived) and
  // Section B (canvas-store) AGG edges in one pass. Restoration is
  // automatic: when either endpoint collapses, no drilled edge has both
  // endpoints in renderMap, the key drops out of the set, and the AGG edge
  // re-appears next render.
  const suppressedAggEdgeKeys = useMemo(() => {
    const keys = new Set<string>()
    if (!trace.isTracing) return keys
    trace.drilldowns.forEach((result, key) => {
      const at = key.indexOf('@')
      const pair = at >= 0 ? key.slice(0, at) : key
      const arrow = pair.indexOf('->')
      if (arrow < 0) return
      const s = pair.slice(0, arrow)
      const t = pair.slice(arrow + 2)
      const anyVisible = result.edges.some(
        e => renderMap.has(e.sourceUrn) && renderMap.has(e.targetUrn),
      )
      if (anyVisible) keys.add(`${s}->${t}`)
    })
    return keys
  }, [trace.isTracing, trace.drilldowns, renderMap])


  // Search results
  const searchResults = useMemo(() => {
    if (!searchQuery.trim()) return []
    const query = searchQuery.toLowerCase()
    return displayFlat.filter((node) =>
      node.name.toLowerCase().includes(query) ||
      node.typeId.toLowerCase().includes(query)
    )
  }, [searchQuery, displayFlat])

  // Action: Move entity to layer (updated for unified context menu)
  // Stages a `move_to_layer` change instead of immediately persisting via
  // updateView — the actual schema mutation happens during applyAll.
  const moveToLayer = useCallback((nodeId: string, layerId: string) => {
    if (!activeView || !activeView.id) return

    const entity = displayMap.get(nodeId)
    if (!entity) return

    if (entity.isLogical) {
      console.warn("Moving logical nodes not yet supported via context menu")
      return
    }

    const layers = activeView.layout.referenceLayout?.layers || defaultReferenceModelLayers
    const targetLayer = layers.find(l => l.id === layerId)

    const addRuleToNode = (nodes: LogicalNodeConfig[], targetId: string): LogicalNodeConfig[] => {
      return nodes.map(node => {
        if (node.id === targetId) {
          return {
            ...node,
            rules: [
              ...(node.rules || []),
              { id: `rule-${Date.now()}`, priority: 100, urnPattern: entity.urn }
            ]
          }
        }
        if (node.children) {
          return { ...node, children: addRuleToNode(node.children, targetId) }
        }
        return node
      })
    }

    const buildUpdatedLayers = () => layers.map(l => {
      if (l.id === layerId) {
        return {
          ...l,
          rules: [
            ...(l.rules || []),
            { id: `rule-${Date.now()}`, priority: 100, urnPattern: entity.urn }
          ]
        }
      }
      if (l.logicalNodes) {
        const updatedLogicalNodes = addRuleToNode(l.logicalNodes, layerId)
        if (updatedLogicalNodes !== l.logicalNodes) {
          return { ...l, logicalNodes: updatedLogicalNodes }
        }
      }
      return l
    })

    const previousLayout = activeView.layout

    useStagedChangesStore.getState().stage({
      type: 'move_to_layer',
      targetId: nodeId,
      targetUrn: entity.urn,
      before: { layout: previousLayout },
      after: { layerId, layerName: targetLayer?.name },
      summary: `Move-to-layer rule: '${entity.name}' → ${targetLayer?.name ?? layerId}`,
      apply: async () => {
        const updatedLayers = buildUpdatedLayers()
        useSchemaStore.getState().updateView(activeView.id, {
          layout: {
            ...activeView.layout,
            referenceLayout: {
              ...activeView.layout.referenceLayout,
              layers: updatedLayers
            }
          }
        })
      },
      discard: () => {
        // No mutation occurred yet — discard is a no-op.
      },
    })

    interactions.closeContextMenu()
  }, [activeView, displayMap, interactions])

  // Handler for adding child entities
  const handleAddChildEntity = useCallback((parentId: string) => {
    setCreationParentId(parentId)
    setIsCreatingEntity(true)
  }, [])

  // Toggle node expansion with Lazy Loading
  const { loadChildren, searchChildren, cancelChildLoad, isLoading: isLoadingChildren, loadingNodes, failedNodes } = useGraphHydration()

  // Floating loading toasts
  useLoadingToast('ctx-assignments', assignmentStatus === 'loading', 'Computing layer assignments')
  useLoadingToast('ctx-agg-edges', isLoadingAggregatedEdges, 'Loading aggregated edges')
  useLoadingToast('ctx-children', isLoadingChildren, 'Expanding hierarchy')

  // Tracks nodes currently being fetched — prevents duplicate fetches on rapid clicks.
  // A ref (not state) because we need synchronous reads inside the toggle callback.
  const pendingLoadRef = useRef<Set<string>>(new Set())

  // Merge a drill-down result into the canvas store: adds new nodes/edges
  // (idempotent — addNodes/addEdges merge by ID), then auto-expands every
  // containment ancestor so the new finer-level nodes are revealed within
  // their hosts. Used by both manual edge double-click and auto-drill on
  // node expansion.
  const mergeDrilldownIntoCanvas = useCallback((expanded: TraceV2Result) => {
    // Same spine strategy as onTraceComplete — see the comment there for the
    // full rationale. Drilldowns rarely include alien ancestors (the server
    // returns lineage between two already-visible subtrees), but applying the
    // same filter keeps the two merge paths consistent and protects against
    // the Snowflake-style re-parenting if the server response widens.
    const participantUrns = new Set<string>()
    expanded.nodes.forEach(n => participantUrns.add(n.urn))
    expanded.upstreamUrns.forEach(u => participantUrns.add(u))
    expanded.downstreamUrns.forEach(u => participantUrns.add(u))

    const knownAssignedUrns = new Set<string>(displayMap.keys())
    const { spineUrns, unreachableRoots } = computeTraceMergeSpine({
      participantUrns,
      containmentEdges: expanded.containmentEdges ?? [],
      knownAssignedUrns,
    })

    // For drilldowns we don't have a single focus, so derive a hint from
    // expanded.focus.urn (the trace anchor). If unavailable, leave
    // unreachable participants without a hint — they still fall through
    // useLayerAssignment's existing chain.
    const drillAnchor = expanded.focus?.urn
    const focusLayerId = drillAnchor ? nodeLayerMap.get(drillAnchor) : undefined

    const shouldMergeNode = (urn: string): boolean =>
      (participantUrns.has(urn) || spineUrns.has(urn)) && !knownAssignedUrns.has(urn)
    const isResolvableEndpoint = (urn: string): boolean =>
      shouldMergeNode(urn) || knownAssignedUrns.has(urn)

    const newCanvasNodes = expanded.nodes
      .filter(gn => shouldMergeNode(gn.urn))
      .map(gn => {
        const metadata: Record<string, unknown> = {
          ...gn.properties,
          childCount: gn.childCount,
          sourceSystem: gn.sourceSystem,
        }
        if (unreachableRoots.has(gn.urn) && focusLayerId) {
          metadata.assignmentHint = focusLayerId
        }
        return {
          id: gn.urn,
          type: 'default' as const,
          position: { x: 0, y: 0 },
          data: {
            label: gn.displayName,
            urn: gn.urn,
            type: gn.entityType,
            classifications: gn.tags ?? [],
            metadata,
          },
        }
      })
    if (newCanvasNodes.length > 0) addNodes(newCanvasNodes as any[])

    const newCanvasEdges = expanded.edges
      .filter(ge => isResolvableEndpoint(ge.sourceUrn) && isResolvableEndpoint(ge.targetUrn))
      .map(ge => ({
        id: ge.id,
        source: ge.sourceUrn,
        target: ge.targetUrn,
        data: {
          edgeType: ge.edgeType,
          relationship: ge.edgeType,
          confidence: ge.confidence,
        },
      }))
    if (newCanvasEdges.length > 0) {
      addEdges(newCanvasEdges as any[])
      trace.recordAddedEdgeIds(newCanvasEdges.map(e => e.id))
    }

    // Containment edges: only when target is newly-merged. Never re-parent
    // existing canvas nodes (the HARD RULE would steal their layer).
    const newContainmentCanvasEdges = (expanded.containmentEdges ?? [])
      .filter(ge => shouldMergeNode(ge.targetUrn) && isResolvableEndpoint(ge.sourceUrn))
      .map(ge => ({
        id: ge.id,
        source: ge.sourceUrn,
        target: ge.targetUrn,
        data: {
          edgeType: ge.edgeType,
          relationship: ge.edgeType,
          confidence: ge.confidence,
        },
      }))
    if (newContainmentCanvasEdges.length > 0) {
      addEdges(newContainmentCanvasEdges as any[])
      trace.recordAddedEdgeIds(newContainmentCanvasEdges.map(e => e.id))
    }

    const drillContainmentMap = new Map<string, string>()
    expanded.containmentEdges?.forEach(ce => {
      drillContainmentMap.set(ce.targetUrn, ce.sourceUrn)
    })
    setExpandedNodes(prev => {
      const next = new Set(prev)
      expanded.nodes.forEach(n => {
        let p = drillContainmentMap.get(n.urn) ?? parentMap.get(n.urn)
        while (p) {
          next.add(p)
          p = drillContainmentMap.get(p) ?? parentMap.get(p)
        }
      })
      return next
    })
  }, [addNodes, addEdges, parentMap, displayMap, nodeLayerMap, trace])

  // Entity-type → hierarchy.level lookup for auto-drill. The drill-down
  // RPC takes the *current* level and returns one level finer; we derive
  // the current level from the expanded node's entity type via the schema.
  const entityTypeLevels = useMemo(() => {
    const map = new Map<string, number>()
    schemaEntityTypes.forEach(et => {
      if (typeof et.hierarchy?.level === 'number') map.set(et.id, et.hierarchy.level)
    })
    return map
  }, [schemaEntityTypes])

  // Auto-drill on expand: when a traced node is expanded, drill into every
  // AGGREGATED edge incident to it. Each drill returns the next-finer level
  // of nodes/edges between this node's subtree and the peer's subtree;
  // mergeDrilldownIntoCanvas merges them, and `useTraceFilteredHierarchy`
  // (which reads `trace.drilldowns`) reveals them in the canvas — recursively
  // at any depth.
  //
  // Single batched call: previously this fired one /trace/expand request per
  // incident aggregated edge (concurrency-6 worker pool). A hub node with
  // 30 edges produced 30 HTTP requests. Now we collect every pair into one
  // /trace/expand-batch call; the server fans out internally and returns a
  // single merged result. The drilldowns cache still keys per (s, t, lvl)
  // so re-expanding a previously-drilled node remains a no-op.
  const autoDrillOnExpand = useCallback(async (nodeId: string) => {
    if (!trace.isTracing) return
    const node = nodes.find(n => n.id === nodeId)
    if (!node) return
    const nodeUrn = (node.data?.urn as string) ?? nodeId
    const entityType = (node.data?.type as string) ?? ''
    const currentLevel = entityTypeLevels.get(entityType)
    if (currentLevel === undefined) return  // not a leveled entity (logical/tag/etc)

    // Find AGGREGATED edges incident to this node (canvas-store edges already
    // carry the trace result's edges via the post-trace merge).
    const incidentEdges = edges.filter(e => {
      const isAgg = String(((e as any).data?.edgeType) ?? '').toUpperCase() === 'AGGREGATED'
      if (!isAgg) return false
      const s = (e as any).source ?? (e as any).sourceUrn
      const t = (e as any).target ?? (e as any).targetUrn
      return s === nodeUrn || t === nodeUrn
    })
    if (incidentEdges.length === 0) return

    const pairs = incidentEdges.map(edge => ({
      sourceUrn: (edge as any).source ?? (edge as any).sourceUrn,
      targetUrn: (edge as any).target ?? (edge as any).targetUrn,
      currentLevel,
    })).filter(p => p.sourceUrn && p.targetUrn)
    if (pairs.length === 0) return

    const merged = await trace.expandAggregatedEdgesBatch(pairs)
    if (merged) mergeDrilldownIntoCanvas(merged)
  }, [trace, nodes, edges, entityTypeLevels, mergeDrilldownIntoCanvas])

  const toggleNode = useCallback(async (nodeId: string) => {
    const node = displayMap.get(nodeId)

    if (node?.isLogical) {
      setExpandedNodes((prev) => {
        const next = new Set(prev)
        if (next.has(nodeId)) next.delete(nodeId)
        else next.add(nodeId)
        return next
      })
      return
    }

    // A child load for this node is already in flight (expand started by an
    // earlier click). Ignore repeat clicks until it settles: without this,
    // an impatient second click would read committed state as expanded,
    // collapse the node, and cancelChildLoad() the in-flight fetch — forcing
    // a third click to actually load. The loading spinner provides feedback
    // meanwhile; collapse works normally once the load completes (finally
    // clears pendingLoadRef).
    if (pendingLoadRef.current.has(nodeId)) return

    // Determine action from committed state via updater function — avoids stale closure read.
    let wasExpanded = false
    setExpandedNodes((prev) => {
      wasExpanded = prev.has(nodeId)
      const next = new Set(prev)
      if (wasExpanded) next.delete(nodeId)
      else next.add(nodeId)
      return next
    })

    // Trigger fetch only when expanding, and only once per node (guard against rapid clicks).
    if (!wasExpanded && !pendingLoadRef.current.has(nodeId)) {
      pendingLoadRef.current.add(nodeId)
      try {
        // Browse path: loadChildren fetches the children + their lineage
        // edges (via getChildrenWithEdges with includeLineageEdges:true).
        // Trace path: also batch-drill the AGGREGATED edges incident to
        // this node so the server returns the next-finer level of trace
        // edges between this node's subtree and its peers' subtrees. The
        // density-tier renderer + browse-mode bundling now absorb the
        // result; the historical reason this was disabled (canvas
        // overload) no longer applies.
        await loadChildren(nodeId)
        if (trace.isTracing) {
          // Fire-and-forget: drill runs in the background and merges into
          // the canvas as it returns. No await — the children are already
          // visible from loadChildren above.
          void autoDrillOnExpand(nodeId)
        }
      } finally {
        pendingLoadRef.current.delete(nodeId)
      }
    } else if (wasExpanded) {
      // User collapsed — drop any pending/in-flight child load so a
      // slow response doesn't repopulate a now-collapsed subtree.
      cancelChildLoad(nodeId)

      // Collapse: drop every edge with an endpoint inside the collapsed
      // subtree (the node itself + all descendants). Runs in BOTH browse
      // mode and trace mode — `loadChildren` (browse) and trace drilldowns
      // both add edges to the canvas store on expand, so collapse must
      // unconditionally release them. Re-expanding refetches via
      // loadChildren / drill paths (cached by the trace store).
      const subtreeIds = new Set<string>()
      subtreeIds.add(nodeId)
      const stack: string[] = [nodeId]
      while (stack.length > 0) {
        const id = stack.pop()!
        const children = childMap.get(id)
        if (!children) continue
        for (const cid of children) {
          if (!subtreeIds.has(cid)) { subtreeIds.add(cid); stack.push(cid) }
        }
      }
      // Edges where the collapsed node is one endpoint should also drop so the
      // node's children-level lineage doesn't linger as orphan edges. The
      // collapsed parent re-acquires its aggregated edges via fetchAggregated
      // on the next render tick.
      removeEdgesByNodeIds(subtreeIds)

      // Synchronous companion: drop matching entries in the aggregated-edge
      // map too. Otherwise stale child-level aggregated edges linger for up
      // to 500 ms (until the debounced fetchAggregated refreshes), producing
      // a flicker after collapse. Resolve subtree URNs from displayMap so
      // we don't depend on id == urn invariants.
      const subtreeUrns = new Set<string>()
      for (const id of subtreeIds) {
        const u = (displayMap.get(id)?.data?.urn as string | undefined) ?? id
        if (u) subtreeUrns.add(u)
      }
      if (subtreeUrns.size > 0) purgeAggregatedEdgesIncidentToUrns(subtreeUrns)
    }
  }, [displayMap, loadChildren, cancelChildLoad, childMap, removeEdgesByNodeIds, purgeAggregatedEdgesIncidentToUrns, trace.isTracing, autoDrillOnExpand])




  // `traceContextSet` now comes directly from useTraceFilteredHierarchy above
  // (single source of truth for both filtering and edge projection).

  // Hovered node — needed by both edge projection (delegation) and hover highlight
  const hoveredNodeId = useHoveredNodeId()

  // Layer-index map: nodeId → layer ordinal (Source=0, Staging=1, …).
  // Drives reverse-flow detection — projected edges where target.layerIdx <
  // source.layerIdx get `isReverseFlow:true` so the renderer can route them
  // through the dedicated lane below the columns. Lazily-cheap: O(N) once
  // per layer assignment change.
  const nodeLayerIndexMap = useMemo(() => {
    const layerOrdinal = new Map<string, number>()
    sortedLayers.forEach((l, i) => layerOrdinal.set(l.id, i))
    const byNode = new Map<string, number>()
    nodeLayerMap.forEach((layerId, nodeId) => {
      const idx = layerOrdinal.get(layerId)
      if (typeof idx === 'number') byNode.set(nodeId, idx)
    })
    return byNode
  }, [sortedLayers, nodeLayerMap])

  // Browse-mode bundling threshold. The pre-bundle edge count above which
  // the projection rolls leaf-pair edges up to their containment parents.
  // 800 is empirically where SVG-edge density crosses from "readable" into
  // "fog" on a typical layered canvas — chosen to match the renderer's
  // density-tier thresholds.
  const BROWSE_BUNDLE_THRESHOLD = 800
  const browseBundleEnabled = !trace.isTracing && edges.length > BROWSE_BUNDLE_THRESHOLD

  // Edge projection: lineageEdges, visibleLineageEdges
  // Pass the trace-filtered views so projected edges only reference visible
  // nodes; outside trace mode these are pass-through to the originals.
  const { visibleLineageEdges } = useEdgeProjection({
    edges, aggregatedEdges, nodesByLayer: renderByLayer, expandedNodes,
    displayFlat: renderFlat, displayMap: renderMap, urnToIdMap,
    showLineageFlow, isTracing: trace.isTracing,
    traceContextSet, isContainmentEdge,
    hoveredNodeId,
    suppressedAggEdgeKeys,
    traceAddedEdgeIds: trace.addedEdgeIds,
    // Trace-mode edge bundling: roll every leaf endpoint up to the focus's
    // hierarchy level so per-pair grouping collapses thousands of
    // column-to-column edges into a handful of container-to-container
    // bundles. parentMap is the canvas containment hierarchy; entityTypeLevels
    // is the ontology level map; result.effectiveLevel is what the trace
    // actually ran at.
    traceBundleParentMap: parentMap,
    entityTypeLevels,
    traceFocusLevel: trace.result?.effectiveLevel,
    // Browse-mode bundling: kicks in only outside trace mode and only when
    // edge density would otherwise overload the canvas. Walks endpoints up
    // the containment chain in passes; collapses parent-pairs whose fan-in
    // exceeds the threshold.
    browseBundleEnabled,
    browseBundleParentMap: parentMap,
    browseBundleFanInThreshold: 1,
    nodeLayerIndexMap,
  })

  // Highlight state: connected nodes/edges for selected node
  const { highlightState, isHighlightActive: isClickHighlightActive } = useHighlightState({
    selectedNodeId, visibleLineageEdges,
    isTracing: trace.isTracing, displayMap, childMap,
  })

  // Hover highlight: same visual effect on hover (lighter), defers to click-highlight
  const { hoverHighlight, isHoverActive } = useHoverHighlight({
    hoveredNodeId,
    visibleLineageEdges,
    isTracing: trace.isTracing,
    displayMap, childMap,
    isClickHighlightActive,
  })

  // Merge: click takes priority, hover used when no click selection
  const isHighlightActive = isClickHighlightActive || isHoverActive
  const mergedHighlightNodes = isClickHighlightActive ? highlightState.nodes : hoverHighlight.nodes
  const mergedHighlightEdges = isClickHighlightActive ? highlightState.edges : hoverHighlight.edges

  const clearSelection = useCanvasStore((s) => s.clearSelection)

  // Drill-down: double-click an AGGREGATED edge to fetch finer-level lineage
  // between the two ancestors and merge it into the canvas. The trace store
  // tracks each drilldown by `${sourceUrn}->${targetUrn}@${atLevel}` so collapse
  // can revert. Single-click still selects/opens the EdgeDetailPanel.
  const handleEdgeDoubleClick = useCallback(async (edgeId: string) => {
    // Resolve the bundle from the projected edges first — bundle ids look
    // like `bundle-${sourceId}->${targetId}` and are not in the canvas
    // store. Falling back to the store lookup keeps the legacy AGGREGATED
    // drill path working when callers pass a raw store edge id.
    const bundle = visibleLineageEdges.find(e => e.id === edgeId)
    const storeEdge = edges.find(e => e.id === edgeId)

    // ── Path 1: browse-mode bundle drill ─────────────────────────────────
    //
    // Iterative reveal: expand whichever endpoint has unrevealed children
    // (priority: source first, then target if source had nothing to expand).
    // Each double-click peels one layer; the projection re-bundles at the
    // next-finer level, so the user can keep drilling. Works in BOTH
    // browse and trace mode for client-side bundles — the trace AGGREGATED
    // server drill (Path 2) only kicks in when the bundle itself is the
    // server-returned AGG edge.
    if (bundle && (bundle.isBrowseBundle || bundle.isBundled)) {
      const isServerAgg = bundle.isAggregated  // backed by server AGGREGATED edge
      if (!isServerAgg || !trace.isTracing) {
        const trySource = displayMap.get(bundle.source)
        const tryTarget = displayMap.get(bundle.target)
        const sourceHasChildren = !!trySource && !expandedNodes.has(bundle.source)
          && (((trySource.data?.childCount as number) ?? trySource.children?.length ?? 0) > 0)
        const targetHasChildren = !!tryTarget && !expandedNodes.has(bundle.target)
          && (((tryTarget.data?.childCount as number) ?? tryTarget.children?.length ?? 0) > 0)
        if (sourceHasChildren) await toggleNode(bundle.source)
        if (targetHasChildren) await toggleNode(bundle.target)
        // If neither side had unrevealed children, fall through and let the
        // server-AGG drill (Path 2) try below — for nested trace structures
        // the same bundle can be both client-collapsed AND a server AGG
        // edge underneath. No-op if not in trace mode / not aggregated.
        if (sourceHasChildren || targetHasChildren) return
      }
    }

    // ── Path 2: server AGGREGATED drill (trace mode only) ────────────────
    if (!trace.isTracing) return
    const edgeForDrill: any = storeEdge ?? bundle
    if (!edgeForDrill) return
    const isAggregated =
      String((edgeForDrill?.data?.edgeType) ?? '').toUpperCase() === 'AGGREGATED'
      || edgeForDrill?.isAggregated
    if (!isAggregated) return

    // The server drill needs URNs, not visible node IDs. For server-edge
    // ids the source/target are already URNs; for projected bundles the
    // source/target are node IDs that we resolve through displayMap.
    const resolveUrn = (id: string): string | undefined => {
      if (!id) return undefined
      const node = displayMap.get(id)
      return (node?.urn as string | undefined) ?? id
    }
    const sourceUrn = resolveUrn(edgeForDrill.source ?? edgeForDrill.sourceUrn)
    const targetUrn = resolveUrn(edgeForDrill.target ?? edgeForDrill.targetUrn)
    if (!sourceUrn || !targetUrn) return

    const currentLevel = trace.result?.effectiveLevel ?? 0
    const expanded = await trace.expandAggregatedEdge(sourceUrn, targetUrn, currentLevel)
    if (expanded) mergeDrilldownIntoCanvas(expanded)
  }, [trace, edges, visibleLineageEdges, displayMap, expandedNodes, toggleNode, mergeDrilldownIntoCanvas])

  // Background click handler to clear selection/highlight
  const handleBackgroundClick = useCallback((e: React.MouseEvent) => {
    // Skip if clicking on an interactive element (tree items, edges, search boxes, etc.)
    if ((e.target as HTMLElement).closest('[data-canvas-interactive]')) return
    clearSelection()
  }, [clearSelection])

  return (
    <div
      data-trace-active={trace.isTracing ? 'true' : 'false'}
      className={cn("h-full w-full flex flex-col overflow-hidden bg-gradient-to-br from-canvas via-canvas to-canvas-elevated/30", className)}
    >
      {/* Node Palette - Drag and drop entity creation */}
      <AnimatePresence>
        {isPaletteOpen && (
          <NodePalette
            isOpen={isPaletteOpen}
            onClose={() => setPaletteOpen(false)}
          />
        )}
      </AnimatePresence>

      {/* Row layout: canvas column + right-rail panels.
          When a panel opens it joins the row as a flex sibling so the entire
          canvas (header + body) shrinks horizontally rather than being
          overlaid. Only one right-rail panel is mounted at a time. */}
      <div className="flex-1 flex flex-row min-h-0 overflow-hidden">
      <div className="flex-1 min-w-0 flex flex-col overflow-hidden relative">
      {/* Editor Toolbar - Unified with LineageCanvas */}
      <div className="absolute top-4 left-4 z-30">
        <EditorToolbar
          onAddNode={() => setPaletteOpen(true)}
          onSave={handleSave}
          edgeTypes={relationshipTypes}
          activeEdgeType={activeEdgeType}
          onSelectEdgeType={setActiveEdgeType}
        />
      </div>

      <ContextViewHeader
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        searchResults={searchResults}
        onSearchResultClick={(node) => {
          selectNode(node.id)
          setExpandedNodes((prev) => new Set([...prev, node.id]))
        }}
        showLineageFlow={showLineageFlow}
        onToggleLineageFlow={() => setShowLineageFlow(!showLineageFlow)}
        showEdgeDirection={showEdgeDirection}
        onToggleEdgeDirection={() => setShowEdgeDirection(v => !v)}
        traceActive={trace.isTracing}
        canTrace={selectedNodeIds.length === 1 && !selectedNodeIds[0].startsWith('logical:')}
        onStartTrace={() => { if (selectedNodeIds[0]) startTraceWithSmartLevel(selectedNodeIds[0]) }}
        onExitTrace={exitTrace}
        onAddEntity={() => { setIsCreatingEntity(true); setCreationParentId(null); setCreationLayerId(null) }}
        viewName={activeView?.name}
        entityTypeCount={activeView?.content.visibleEntityTypes.length}
        activeWorkspaceId={activeWorkspaceId}
        activeContextModelName={activeContextModelName}
        syncStatus={syncStatus}
        onSave={handleSaveAll}
        pendingChangeCount={stagedChangeList.length}
        onOpenStagedChanges={openStagedChangesPanel}
        canUndo={stagedChangeList.length > 0}
        canRedo={stagedRedoStack.length > 0}
        onUndo={undoStagedChange}
        onRedo={redoStagedChange}
      />

      <div data-canvas-body className="flex-1 w-full h-full relative overflow-hidden bg-canvas flex flex-col">
        {/* Trace UI lives in TraceBottomDock at the bottom of canvas-body.
            EntityDrawer keeps the right rail. Both surfaces are independent. */}
        <AnimatePresence>
          {trace.isTracing && (
            <TraceBottomDock
              trace={trace}
              displayMap={displayMap}
              availableEdgeTypes={lineageEdgeTypes}
              granularityOptions={granularityOptions}
              resolveEdgeColor={resolveEdgeColor}
              expanded={dockExpanded}
              onToggleExpanded={() => setDockExpanded(v => !v)}
              onExit={exitTrace}
              onJumpToUrn={(urn) => {
                const id = urnToIdMap.get(urn) ?? urn
                startTraceWithSmartLevel(id)
              }}
            />
          )}
        </AnimatePresence>

        {/* Aggregation truncation banner — backend signal that the visible
            edge set was capped. The "computing" and "last computed Xh ago"
            banners were removed: the materialization-triggered flag was
            sticky after first paint and the staleness banner fired even
            for fresh aggregations. Trust the data already on canvas. */}
        {aggregationTruncated && (
          <div
            data-canvas-interactive
            className="mx-4 mt-2 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/40 text-amber-700 text-xs flex items-center gap-2 z-20"
          >
            <span className="font-medium">Showing the largest connections — narrow the selection to see more.</span>
          </div>
        )}
        {/* Warning: missing ontology configuration */}
        {schema && containmentEdgeTypes.length === 0 && edges.length > 0 && (
          <div className="mx-4 mt-2 px-3 py-2 rounded-md bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 text-amber-700 dark:text-amber-400 text-xs flex items-center gap-2 z-20">
            <span className="font-medium">No containment types configured.</span>
            <span className="text-amber-600 dark:text-amber-500">Hierarchy is disabled — all nodes appear flat. Configure your ontology to enable parent-child nesting.</span>
          </div>
        )}
        {/* Warning: containment inheritance violation attempt */}
        {assignmentWarning && (
          <div className="mx-4 mt-2 px-3 py-2 rounded-md bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400 text-xs flex items-center gap-2 z-20">
            <span className="font-medium">Assignment blocked.</span>
            <span className="text-red-600 dark:text-red-500">{assignmentWarning}</span>
            <button
              className="ml-auto text-red-400 hover:text-red-600 dark:hover:text-red-300"
              onClick={() => setAssignmentWarning(null)}
            >
              &times;
            </button>
          </div>
        )}
        {/* Save Confirmation Modal — opens when the user clicks Save Blueprint
             or the pending-changes badge. Single source of truth for reviewing
             and confirming a batch of staged edits before they hit the backend. */}
        <StagedChangesPanel onConfirm={async () => {
          if (!activeWorkspaceId) return
          const result = stagedChangeList.length > 0
            ? await applyStagedChanges(provider, activeWorkspaceId)
            : { ok: 0, failed: 0 }
          if (result.failed === 0) {
            await saveToBackend(activeWorkspaceId)
            closeStagedChangesPanel()
          }
        }} />

        {/* Edge Legend — sits at the bottom-right of the (possibly shrunken)
            canvas. Right-rail panels are now flex siblings, so the canvas
            itself shrinks when one opens — the legend doesn't need its own
            offset logic. Lifts above TraceBottomDock via --trace-dock-height. */}
        <div
          className="absolute z-30 w-64 pointer-events-auto transition-all duration-300 ease-out"
          style={{
            bottom: 'calc(160px + var(--trace-dock-height, 0px))',
            right: '1rem',
          }}
        >
          <EdgeLegend defaultExpanded={false} visibleEdges={visibleLineageEdges} />
        </div>


        {/* Layer Columns. */}
        <div
          ref={horizontalScrollRef}
          className="flex-1 overflow-auto relative scroll-smooth"
          onClick={handleBackgroundClick}
        >
          {/* Lineage Flow Overlay - Render BEFORE columns to be behind them
              (z-index managed in component to 0, cols should be higher).
              Flow is the master switch — Trace mode respects it so the user
              can dial back ambient edge noise while keeping trace highlights
              on the nodes and trace panels open. */}
          {showLineageFlow && (
            <LineageFlowOverlay
              nodes={renderFlat}
              edges={visibleLineageEdges}
              expandedNodes={expandedNodes}
              selectEdge={selectEdge}
              isEdgePanelOpen={isEdgePanelOpen}
              toggleEdgePanel={toggleEdgePanel}
              triggerRedrawRef={triggerEdgeRedrawRef}
              isTracing={trace.isTracing}
              traceResult={trace.result}
              highlightedEdges={mergedHighlightEdges}
              isHighlightActive={isHighlightActive}
              resolveEdgeColor={resolveEdgeColor}
              onEdgeDoubleClick={handleEdgeDoubleClick}
              showDirection={showEdgeDirection}
            />
          )}

          {/*
            z-30 + pointer-events-none on the columns wrapper:
            - z-30 puts the columns ABOVE the hit-test layer (z-20 in
              LineageFlowOverlay), so node cards win pointer events when the
              cursor is over them — even when an edge stroke passes
              geometrically over the same pixel.
            - pointer-events-none on the wrapper itself means the wrapper
              doesn't capture clicks in the inter-column gaps; events fall
              through to the hit layer below for edge interaction. The child
              LayerColumn / FlatTreeItem elements default to pointer-events:
              auto and continue to receive their own hover/click events.
          */}
          {/* Left/right gutters inside the scroll content so edges that bow
              into the leftmost column or leave the rightmost column aren't
              clipped by the overflow-auto scroll container. The width is
              derived from LineageFlowOverlay's same-column lane math
              (EXTREMITY_EDGE_GUTTER_PX) so the two stay in sync. The overlay
              SVG spans the full viewport, so insetting the columns keeps
              those curves within the visible box at the scroll extremes. */}
          <div
            className="flex h-full min-h-0 relative z-30 gap-12 pointer-events-none"
            style={{ paddingLeft: EXTREMITY_EDGE_GUTTER_PX, paddingRight: EXTREMITY_EDGE_GUTTER_PX }}
          >
            {sortedLayers.map((layer) => (
              <LayerColumn
                key={layer.id}
                layer={layer}
                nodes={renderByLayer.get(layer.id) ?? []}
                schema={schema}
                selectedNodeId={selectedNodeId}
                expandedNodes={expandedNodes}
                searchResults={searchResults.map((n) => n.id)}
                onSelect={selectNode}
                onToggle={toggleNode}
                onContextMenu={handleContextMenu}
                onDoubleClick={handleDoubleClick}
                onAddChild={handleAddChildEntity}
                onAddToLayer={(layerId) => {
                  setCreationLayerId(layerId)
                  setCreationParentId(null)
                  setIsCreatingEntity(true)
                }}
                traceFocusId={trace.focusId}
                traceNodes={trace.visibleTraceNodes}
                traceContextSet={traceContextSet}
                isTracing={trace.isTracing}
                highlightedNodes={mergedHighlightNodes}
                isHighlightActive={isHighlightActive}
                isHoverHighlight={isHoverActive && !isClickHighlightActive}
                onAnimationComplete={handleAnimationComplete}
                onLoadMore={loadChildren}
                onSearchChildren={searchChildren}
                isLoadingChildren={isLoadingChildren}
                loadingNodes={loadingNodes}
                failedNodes={failedNodes}
                onScroll={handleLayerScroll}
                onAssignToLayer={(entityId) => handleAssignToLayer(entityId, layer.id)}
              />
            ))}
          </div>


        </div>
      </div>
      </div>{/* end canvas column */}

      {/* Right-rail panels — flex siblings of the canvas column.
          Mutual exclusion: selection > edge-panel > creation. Only one is
          ever mounted at a time, so the canvas shrinks by exactly one
          panel's width whenever any of them opens. */}
      <AnimatePresence>
        {drawerNodeId && (
          <EntityDrawer
            key="entity-drawer"
            onTraceUp={(nodeId) => traceUpstreamWithSmartLevel(nodeId)}
            onTraceDown={(nodeId) => traceDownstreamWithSmartLevel(nodeId)}
            onFullTrace={(nodeId) => traceFullLineageWithSmartLevel(nodeId)}
          />
        )}
        {!drawerNodeId && isEdgePanelOpen && (
          <EdgeDetailPanel
            key="edge-detail-panel"
            isOpen={isEdgePanelOpen}
            onClose={closeEdgePanel}
            edgeFilters={dynamicEdgeFilters}
            onToggleFilter={toggleEdgeFilter}
          />
        )}
        {!drawerNodeId && !isEdgePanelOpen && isCreatingEntity && (
          <EntityCreationPanel
            key="entity-creation-panel"
            isOpen={isCreatingEntity}
            onClose={() => {
              setIsCreatingEntity(false)
              setCreationParentId(null)
              setCreationLayerId(null)
            }}
            parentId={creationParentId}
            layerId={creationLayerId}
            onEntityCreated={(_nodeId, parentUrn) => {
              if (parentUrn) {
                setExpandedNodes(prev => new Set([...prev, parentUrn]))
              }
            }}
          />
        )}
      </AnimatePresence>
      </div>{/* end flex-row wrapper */}

      {/* === UX-FIRST INTERACTION COMPONENTS === */}

      {/* Modern Context Menu - Full CRUD operations */}
      <CanvasContextMenu
        isOpen={interactions.state.contextMenu.isOpen}
        position={interactions.state.contextMenu.position}
        target={interactions.state.contextMenu.target}
        onClose={interactions.closeContextMenu}
        onEditNode={interactions.editNode}
        onDuplicateNode={interactions.duplicateNode}
        onDeleteNode={interactions.deleteNode}
        onCreateChild={interactions.createChild}
        onTraceNode={(id) => startTraceWithSmartLevel(id)}
        onCopyUrn={interactions.copyUrn}
        onEditEdge={interactions.editEdge}
        onDeleteEdge={interactions.deleteEdge}
        onReverseEdge={interactions.reverseEdge}
        onCreateNode={(pos) => interactions.openQuickCreate(pos)}
        onSelectAll={interactions.selectAll}
        layers={sortedLayers}
        onMoveToLayer={(nodeId, layerId) => moveToLayer(nodeId, layerId)}
      />

      {/* Inline Node Editor - Double-click to edit names */}
      <InlineNodeEditor
        nodeId={interactions.state.inlineEdit.nodeId}
        value={interactions.state.inlineEdit.value}
        position={interactions.state.inlineEdit.position}
        onSave={interactions.saveInlineEdit}
        onCancel={interactions.cancelInlineEdit}
      />

      {/* Quick Create - Press 'N' or use context menu */}
      <QuickCreateNode
        isOpen={interactions.state.quickCreate.isOpen}
        position={interactions.state.quickCreate.position}
        parentUrn={interactions.state.quickCreate.parentUrn}
        onClose={interactions.closeQuickCreate}
        onCreated={(nodeId) => selectNode(nodeId)}
        variant="centered"
      />

      {/* Command Palette - Press Cmd+K */}
      <CommandPalette
        isOpen={interactions.state.commandPalette.isOpen}
        onClose={interactions.closeCommandPalette}
        onCreateEntity={(_typeId) => {
          interactions.closeCommandPalette()
          interactions.openQuickCreate({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
        }}
        onSelectEntity={(entityId) => selectNode(entityId)}
      />
    </div>
  )
}
