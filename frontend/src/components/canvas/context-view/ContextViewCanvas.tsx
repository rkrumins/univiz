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
import { LayerColumn } from './LayerColumn'
import { LineageFlowOverlay } from './LineageFlowOverlay'
import { ContextViewHeader } from './ContextViewHeader'
import { useLoadingToast } from '@/components/ui/toast'
import { useStagedChangesStore } from '@/store/stagedChangesStore'
import { StagedChangesPanel } from './StagedChangesPanel'

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
  const selectNode = useCanvasStore((s) => s.selectNode)
  const selectedNodeIds = useCanvasStore((s) => s.selectedNodeIds)
  const selectedNodeId = selectedNodeIds[0] ?? null
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

      // CRITICAL: Merge trace result nodes/edges into canvas store
      // Without this, LineageFlowOverlay can't draw trace edges
      if (result.lineageResult) {
        const lr = result.lineageResult

        // Convert GraphNode[] → LineageNode[] and add to canvas
        const newCanvasNodes = lr.nodes.map(gn => ({
          id: gn.urn,
          type: 'default' as const,
          position: { x: 0, y: 0 },
          data: {
            label: gn.displayName,
            urn: gn.urn,
            type: gn.entityType,
            classifications: gn.tags ?? [],
            metadata: {
              ...gn.properties,
              childCount: gn.childCount,
              sourceSystem: gn.sourceSystem,
            },
          },
        }))
        if (newCanvasNodes.length > 0) {
          addNodes(newCanvasNodes as any[])
        }

        // Convert GraphEdge[] → LineageEdge[] and add to canvas
        const newCanvasEdges = lr.edges.map(ge => ({
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
        }

        // Containment edges hydrated by /trace/v2 — these are the parent→child
        // edges that link returned trace nodes (and their ancestors) into the
        // canvas hierarchy. Without them deep lineage URNs (e.g. column-level
        // schemaFields whose Datasets aren't loaded) render as orphans.
        const newContainmentEdges = (result.containmentEdges ?? []).map(ge => ({
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
        }

        // Auto-expand ancestors of traced nodes
        const nodesToExpand = new Set(expandedNodes)

        // Build parent map from ALL edges (including newly added — both
        // lineage edges and the freshly-hydrated containment edges).
        const allCurrentEdges = [...edges, ...newCanvasEdges, ...newContainmentEdges]
        const traceParentMap = new Map<string, string>()
        allCurrentEdges.forEach(e => {
          if (isContainmentEdge(normalizeEdgeType(e))) {
            traceParentMap.set(e.target ?? (e as any).targetUrn, e.source ?? (e as any).sourceUrn)
          }
        })

        // For each traced node, expand its ancestors
        result.traceNodes.forEach(id => {
          let curr = traceParentMap.get(id)
          while (curr) {
            nodesToExpand.add(curr)
            curr = traceParentMap.get(curr)
          }
        })

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
      if (selectedNodeId) { clearSelection(); return true }
      return false
    },
    // ESC exits an active trace before any other panel close — gives the
    // user a single, predictable escape from a busy trace view.
    onExitTrace: () => {
      if (trace.isTracing) { trace.clearTrace(); setExpandedNodes(new Set()); return true }
      return false
    },
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
    lastMaterializedAt: aggregationLastMaterializedAt,
    materializationTriggered: aggregationMaterializationTriggered,
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
  useEffect(() => {
    if (!showLineageFlow || nodes.length === 0) return

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
    }, 500) // 500ms debounce — coalesces rapid expand/collapse

    return () => clearTimeout(fetchDebounced)
  }, [showLineageFlow, getVisibleContainerUrns, fetchAggregated, nodes.length, expandedNodes])

  // === Extracted Hooks ===

  // Layer assignment: rules, nodesByLayer, displayFlat, displayMap, urnToIdMap
  const { nodesByLayer, displayFlat, displayMap, urnToIdMap } = useLayerAssignment({
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
  const { loadChildren, searchChildren, isLoading: isLoadingChildren, loadingNodes, failedNodes } = useGraphHydration()

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
    const newCanvasNodes = expanded.nodes.map(gn => ({
      id: gn.urn,
      type: 'default' as const,
      position: { x: 0, y: 0 },
      data: {
        label: gn.displayName,
        urn: gn.urn,
        type: gn.entityType,
        classifications: gn.tags ?? [],
        metadata: {
          ...gn.properties,
          childCount: gn.childCount,
          sourceSystem: gn.sourceSystem,
        },
      },
    }))
    if (newCanvasNodes.length > 0) addNodes(newCanvasNodes as any[])

    const newCanvasEdges = expanded.edges.map(ge => ({
      id: ge.id,
      source: ge.sourceUrn,
      target: ge.targetUrn,
      data: {
        edgeType: ge.edgeType,
        relationship: ge.edgeType,
        confidence: ge.confidence,
      },
    }))
    if (newCanvasEdges.length > 0) addEdges(newCanvasEdges as any[])

    // Hydrated containment edges from /trace/expand — link new nodes into
    // the canvas hierarchy alongside the lineage edges. Without these the
    // drilled-into nodes are floating; useContainmentHierarchy can't put
    // them under their parents and they end up filtered out by layer
    // assignment.
    const newContainmentCanvasEdges = (expanded.containmentEdges ?? []).map(ge => ({
      id: ge.id,
      source: ge.sourceUrn,
      target: ge.targetUrn,
      data: {
        edgeType: ge.edgeType,
        relationship: ge.edgeType,
        confidence: ge.confidence,
      },
    }))
    if (newContainmentCanvasEdges.length > 0) addEdges(newContainmentCanvasEdges as any[])

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
  }, [addNodes, addEdges, parentMap])

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
  // Idempotent: `trace.expandAggregatedEdge` caches results by
  // `${s}->${t}@${nextLevel}`, so re-expanding a previously-drilled node
  // is a no-op against the network.
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

    // Drill all incident edges in parallel; merge each result.
    const results = await Promise.all(incidentEdges.map(edge => {
      const s = (edge as any).source ?? (edge as any).sourceUrn
      const t = (edge as any).target ?? (edge as any).targetUrn
      return trace.expandAggregatedEdge(s, t, currentLevel)
    }))
    results.forEach(r => { if (r) mergeDrilldownIntoCanvas(r) })
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
        // In trace mode, auto-drill so the next-finer level of trace nodes
        // becomes visible inside the expanded container. Outside trace mode
        // this is a no-op. Both run in parallel — they touch independent
        // pieces of state.
        await Promise.all([
          loadChildren(nodeId),
          trace.isTracing ? autoDrillOnExpand(nodeId) : Promise.resolve(),
        ])
      } finally {
        pendingLoadRef.current.delete(nodeId)
      }
    }
  }, [displayMap, loadChildren, trace.isTracing, autoDrillOnExpand])




  // `traceContextSet` now comes directly from useTraceFilteredHierarchy above
  // (single source of truth for both filtering and edge projection).

  // Hovered node — needed by both edge projection (delegation) and hover highlight
  const hoveredNodeId = useHoveredNodeId()

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
    if (!trace.isTracing) return
    const edge = edges.find(e => e.id === edgeId)
    if (!edge) return
    const isAggregated = String(((edge as any).data?.edgeType) ?? '').toUpperCase() === 'AGGREGATED'
    if (!isAggregated) return

    const sourceUrn = (edge as any).source ?? (edge as any).sourceUrn
    const targetUrn = (edge as any).target ?? (edge as any).targetUrn
    if (!sourceUrn || !targetUrn) return

    const currentLevel = trace.result?.effectiveLevel ?? 0
    const expanded = await trace.expandAggregatedEdge(sourceUrn, targetUrn, currentLevel)
    if (expanded) mergeDrilldownIntoCanvas(expanded)
  }, [trace, edges, mergeDrilldownIntoCanvas])

  // Background click handler to clear selection/highlight
  const handleBackgroundClick = useCallback((e: React.MouseEvent) => {
    // Skip if clicking on an interactive element (tree items, edges, search boxes, etc.)
    if ((e.target as HTMLElement).closest('[data-canvas-interactive]')) return
    clearSelection()
  }, [clearSelection])

  return (
    <div className={cn("h-full w-full flex flex-col overflow-hidden bg-gradient-to-br from-canvas via-canvas to-canvas-elevated/30", className)}>
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

      {/* Node Palette - Drag and drop entity creation */}
      <AnimatePresence>
        {isPaletteOpen && (
          <NodePalette
            isOpen={isPaletteOpen}
            onClose={() => setPaletteOpen(false)}
          />
        )}
      </AnimatePresence>

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
        lineageGranularity={lineageGranularity}
        onGranularityChange={setLineageGranularity}
        granularityOptions={granularityOptions}
        showEdgeDirection={showEdgeDirection}
        onToggleEdgeDirection={() => setShowEdgeDirection(v => !v)}
        onAddEntity={() => { setIsCreatingEntity(true); setCreationParentId(null); setCreationLayerId(null) }}
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
        trace={trace}
        focusNodeName={displayMap.get(trace.focusId || '')?.name || trace.focusId || 'Unknown Node'}
        lineageEdgeTypes={lineageEdgeTypes}
        onExitTrace={() => { trace.clearTrace(); setExpandedNodes(new Set()) }}
      />

      <div className="flex-1 w-full h-full relative overflow-hidden bg-canvas flex flex-col">
        {/* Trace mode banner — persistent, always-visible exit affordance.
            Sits above the layer columns (not floating like TraceToolbar) so
            the user can never lose it via scroll/pan. ESC also exits via
            useCanvasInteractions.onExitTrace. */}
        {trace.isTracing && (
          <div
            data-canvas-interactive
            className="mx-4 mt-2 px-3 py-2 rounded-md bg-accent-lineage/10 border border-accent-lineage/40 text-accent-lineage text-xs flex items-center gap-2 z-20"
          >
            <span className="inline-block w-2 h-2 rounded-full bg-accent-lineage animate-pulse" aria-hidden="true" />
            <span className="font-medium">Tracing</span>
            <span className="text-accent-lineage/80 truncate" title={trace.focusId ?? undefined}>
              {displayMap.get(trace.focusId || '')?.name || trace.focusId || 'Unknown'}
            </span>
            {(() => {
              const focusNode = trace.focusId ? displayMap.get(trace.focusId) : null
              const focusType = focusNode?.typeId
              const level = trace.result?.effectiveLevel
              if (!focusType && typeof level !== 'number') return null
              return (
                <span className="text-accent-lineage/60">
                  · {focusType ?? ''}{typeof level === 'number' ? ` (L${level})` : ''}
                </span>
              )
            })()}
            <span className="ml-auto text-[10px] text-accent-lineage/50 hidden md:inline">Press ESC to exit</span>
            <button
              type="button"
              onClick={() => { trace.clearTrace(); setExpandedNodes(new Set()) }}
              className="ml-2 px-2 py-0.5 rounded border border-accent-lineage/40 hover:bg-accent-lineage/20 transition-colors duration-150 font-medium"
              title="Exit trace (ESC)"
            >
              Exit Trace ✕
            </button>
          </div>
        )}
        {/* Aggregation state banner — surfaces backend signals so an empty
            canvas isn't ambiguous between "no lineage" and "still computing".
            Priority: computing > truncated > stale-materialised. */}
        {(() => {
          if (aggregationMaterializationTriggered) {
            return (
              <div
                data-canvas-interactive
                className="mx-4 mt-2 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/40 text-amber-700 text-xs flex items-center gap-2 z-20"
              >
                <span className="inline-block w-2 h-2 rounded-full bg-amber-500 animate-pulse" aria-hidden="true" />
                <span className="font-medium">Aggregations computing — this view will refresh in a moment.</span>
              </div>
            )
          }
          if (aggregationTruncated) {
            return (
              <div
                data-canvas-interactive
                className="mx-4 mt-2 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/40 text-amber-700 text-xs flex items-center gap-2 z-20"
              >
                <span className="font-medium">Showing the largest connections — narrow the selection to see more.</span>
              </div>
            )
          }
          if (aggregationLastMaterializedAt) {
            const ageMs = Date.now() - new Date(aggregationLastMaterializedAt).getTime()
            const ageMin = Math.floor(ageMs / 60_000)
            if (ageMin > 60) {
              const ageHours = Math.floor(ageMin / 60)
              return (
                <div
                  data-canvas-interactive
                  className="mx-4 mt-2 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/40 text-amber-700 text-xs flex items-center gap-2 z-20"
                >
                  <span className="font-medium">Aggregations last computed {ageHours}h ago.</span>
                </div>
              )
            }
          }
          return null
        })()}
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
        {/* Edge Panel */}
        <AnimatePresence>
          {isEdgePanelOpen && (
            <EdgeDetailPanel
              isOpen={isEdgePanelOpen}
              onClose={closeEdgePanel}
              edgeFilters={dynamicEdgeFilters}
              onToggleFilter={toggleEdgeFilter}
            />
          )}

          {/* Entity Drawer - Unified view & edit */}
          <EntityDrawer
            onTraceUp={(nodeId) => traceUpstreamWithSmartLevel(nodeId)}
            onTraceDown={(nodeId) => traceDownstreamWithSmartLevel(nodeId)}
            onFullTrace={(nodeId) => traceFullLineageWithSmartLevel(nodeId)}
          />

          {/* Entity Creation Panel */}
          <EntityCreationPanel
            isOpen={isCreatingEntity}
            onClose={() => {
              setIsCreatingEntity(false)
              setCreationParentId(null)
              setCreationLayerId(null)
            }}
            parentId={creationParentId}
            layerId={creationLayerId}
            onEntityCreated={(_nodeId, parentUrn) => {
              // Auto-expand parent if a child was created
              if (parentUrn) {
                setExpandedNodes(prev => new Set([...prev, parentUrn]))
              }
            }}
          />
        </AnimatePresence>

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

        {/* Edge Legend — shifts left when EntityDrawer is open to avoid overlap (3.3)
             receives only the projected visible edges, not all canvas edges (3.2) */}
        <div className={cn(
          "absolute bottom-40 z-30 w-64 pointer-events-auto transition-all duration-300 ease-out",
          selectedNodeId ? "right-[420px]" : "right-4"
        )}>
          <EdgeLegend defaultExpanded={false} visibleEdges={visibleLineageEdges} />
        </div>

        {/* Layer Columns */}
        <div
          className={cn(
            "flex-1 overflow-auto relative scroll-smooth transition-[padding] duration-300 ease-out",
            selectedNodeId ? "pr-[420px]" : ""
          )}
          onClick={handleBackgroundClick}
        >
          {/* Lineage Flow Overlay - Render BEFORE columns to be behind them (z-index managed in component to 0, cols should be higher) */}
          {(showLineageFlow || trace.isTracing) && (
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

          <div className="flex h-full min-h-0 relative z-10 gap-12">
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
