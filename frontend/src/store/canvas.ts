import { create } from 'zustand'
import type { Node, Edge, Viewport } from '@xyflow/react'
import type { HydrationPhase } from '@/hooks/useGraphHydration'

export interface LineageNode extends Node {
  data: {
    label: string
    businessLabel?: string
    technicalLabel?: string
    urn: string
    type: string // Allow any entity type
    lensId?: string
    classifications?: string[]
    confidence?: number
    metadata?: Record<string, unknown>
    // Hierarchy
    childIds?: string[]
    parentId?: string
    isExpanded?: boolean
    // Roll-up data
    _collapsedChildCount?: number
    _rollupData?: Record<string, unknown>
    /** Pending change marker — drives the visual badge on the canvas. */
    isPending?: 'create' | 'delete' | 'modify'
  }
}

export interface LineageEdge extends Edge {
  data?: {
    confidence?: number
    edgeType?: string
    relationship?: string
    animated?: boolean
    label?: string
    // For aggregated edges
    isAggregated?: boolean
    sourceEdgeCount?: number
    sourceEdges?: string[]
  }
}

interface CanvasState {
  // Nodes and Edges
  nodes: LineageNode[]
  edges: LineageEdge[]
  _nodeIndex: Set<string>
  _edgeIndex: Set<string>
  /** Monotonic counter — incremented on every node/edge mutation. */
  _version: number
  setNodes: (nodes: LineageNode[]) => void
  setEdges: (edges: LineageEdge[]) => void
  addNodes: (nodes: LineageNode[]) => void
  addEdges: (edges: LineageEdge[]) => void
  /** Atomic set of both nodes and edges (1 re-render, prevents flash-of-no-edges) */
  setGraph: (nodes: LineageNode[], edges: LineageEdge[]) => void
  /** Atomic add of both nodes and edges with dedup (1 re-render) */
  addGraph: (nodes: LineageNode[], edges: LineageEdge[]) => void

  // Visible edges — the projected + aggregated lineage edge set currently
  // rendered on the canvas. Published by whichever canvas component owns
  // edge projection (ContextViewCanvas via useEdgeProjection; GraphCanvas via
  // its allVisibleEdges memo). Read by panels that need to mirror what the
  // user actually sees on canvas (e.g. EntityDrawer's Lineage section),
  // which raw `edges` alone cannot represent because raw edges live at
  // leaf level while the canvas shows them rolled up to visible ancestors
  // and merged with backend-aggregated edges. Empty when no canvas is
  // mounted — readers must fall back to `edges`.
  visibleEdges: LineageEdge[]
  setVisibleEdges: (edges: LineageEdge[]) => void

  // Selection
  selectedNodeIds: string[]
  selectedEdgeIds: string[]
  selectNode: (id: string, multi?: boolean) => void
  selectEdge: (id: string, multi?: boolean) => void
  clearSelection: () => void

  // Sticky entity drawer — which entity the drawer currently shows.
  // Decoupled from selection so background clicks / selection changes
  // don't close it; only an explicit close (X) does.
  drawerNodeId: string | null
  openNodeDrawer: (id: string) => void
  closeNodeDrawer: () => void

  // Viewport
  viewport: Viewport
  setViewport: (viewport: Viewport) => void

  // Loading State
  isLoading: boolean
  loadingRegions: Set<string>
  setLoading: (loading: boolean) => void
  addLoadingRegion: (region: string) => void
  removeLoadingRegion: (region: string) => void

  // Hydration phase — mirrored from useGraphHydration({hydrate:true}) in
  // CanvasRouter so downstream canvas components can drive ghost-loading UI
  // without each owning their own hydration hook.
  hydrationPhase: HydrationPhase
  setHydrationPhase: (phase: HydrationPhase) => void

  // Active Lens
  activeLensId: string | null
  setActiveLens: (lensId: string | null) => void

  // Trace State
  traceOrigin: string | null
  traceDirection: 'upstream' | 'downstream' | 'both'
  traceDepth: number
  setTraceOrigin: (nodeId: string | null) => void
  setTraceDirection: (direction: 'upstream' | 'downstream' | 'both') => void
  setTraceDepth: (depth: number) => void

  // Cache
  cachedRegions: Map<string, LineageNode[]>
  cacheRegion: (key: string, nodes: LineageNode[]) => void
  getCachedRegion: (key: string) => LineageNode[] | undefined
  clearCache: () => void

  // Editing Mode
  isEditing: boolean
  setEditing: (isEditing: boolean) => void

  // Node/Edge CRUD (Manual)
  updateNode: (id: string, data: Partial<LineageNode['data']>) => void
  removeNode: (id: string) => void
  removeEdge: (id: string) => void
  removeNodes: (ids: string[]) => void
  removeEdges: (ids: string[]) => void
  /**
   * Remove every edge whose source OR target is in the supplied set of
   * node ids. Used on subtree collapse to drop edges that only existed
   * because that subtree was expanded — without this, edges accumulate
   * monotonically across expand/collapse cycles.
   *
   * Note: this removes edges between the collapsed subtree and *visible*
   * peers too. That matches the intent — those edges represented the
   * subtree's relationships at its expanded granularity. Re-expanding
   * refetches them via loadChildren/drill paths.
   */
  removeEdgesByNodeIds: (nodeIds: Iterable<string>) => void
}

import { persist, createJSONStorage } from 'zustand/middleware'
import type { StateCreator } from 'zustand'

/**
 * Middleware: auto-increment `_version` whenever nodes or edges change.
 * Replaces brittle fingerprint sampling with a monotonic counter.
 */
const withVersion: (
  config: StateCreator<CanvasState, [], []>,
) => StateCreator<CanvasState, [], []> =
  (config) => (rawSet, get, api) => {
    const wrappedSet: typeof rawSet = (...args: any[]) => {
      const [partial, replace] = args
      const update: Record<string, unknown> =
        typeof partial === 'function' ? partial(get()) : partial
      const touchesGraph = 'nodes' in update || 'edges' in update
        || '_nodeIndex' in update || '_edgeIndex' in update
      if (touchesGraph) {
        return (rawSet as any)(
          { ...update, _version: get()._version + 1 } as Partial<CanvasState>,
          replace,
        )
      }
      return (rawSet as any)(partial, replace)
    }
    return config(wrappedSet, get, api)
  }

export const useCanvasStore = create<CanvasState>()(
  persist(
    withVersion(
    (set, get) => ({
      // Nodes and Edges
      nodes: [],
      edges: [],
      _nodeIndex: new Set(),
      _edgeIndex: new Set(),
      _version: 0,
      setNodes: (nodes) => set({ nodes, _nodeIndex: new Set(nodes.map((n) => n.id)) }),
      setEdges: (edges) => set({ edges, _edgeIndex: new Set(edges.map((e) => e.id)) }),
      visibleEdges: [],
      setVisibleEdges: (visibleEdges) => set({ visibleEdges }),
      addNodes: (newNodes) => set((state) => {
        const existingIds = state._nodeIndex
        const uniqueNodes = newNodes.filter((n) => !existingIds.has(n.id))
        if (uniqueNodes.length === 0) return state // No-op: prevent unnecessary re-render
        const nextIndex = new Set(existingIds)
        uniqueNodes.forEach((n) => nextIndex.add(n.id))
        return { nodes: [...state.nodes, ...uniqueNodes], _nodeIndex: nextIndex }
      }),
      addEdges: (newEdges) => set((state) => {
        const existingIds = state._edgeIndex
        const uniqueEdges = newEdges.filter((e) => !existingIds.has(e.id))
        if (uniqueEdges.length === 0) return state // No-op: prevent unnecessary re-render
        const nextIndex = new Set(existingIds)
        uniqueEdges.forEach((e) => nextIndex.add(e.id))
        return { edges: [...state.edges, ...uniqueEdges], _edgeIndex: nextIndex }
      }),
      setGraph: (nodes, edges) => set(() => {
        // Dedup by id to prevent React duplicate-key warnings when callers
        // pass arrays with overlapping entries (e.g. assigned + child nodes).
        const seenNodes = new Set<string>()
        const dedupedNodes: LineageNode[] = []
        for (const n of nodes) {
          if (!seenNodes.has(n.id)) {
            seenNodes.add(n.id)
            dedupedNodes.push(n)
          }
        }
        const seenEdges = new Set<string>()
        const dedupedEdges: LineageEdge[] = []
        for (const e of edges) {
          if (!seenEdges.has(e.id)) {
            seenEdges.add(e.id)
            dedupedEdges.push(e)
          }
        }
        return {
          nodes: dedupedNodes,
          edges: dedupedEdges,
          _nodeIndex: seenNodes,
          _edgeIndex: seenEdges,
        }
      }),
      addGraph: (newNodes, newEdges) => set((state) => {
        const uniqueNodes = newNodes.filter((n) => !state._nodeIndex.has(n.id))
        const uniqueEdges = newEdges.filter((e) => !state._edgeIndex.has(e.id))
        if (uniqueNodes.length === 0 && uniqueEdges.length === 0) return state
        const nodeIndex = new Set(state._nodeIndex)
        const edgeIndex = new Set(state._edgeIndex)
        uniqueNodes.forEach((n) => nodeIndex.add(n.id))
        uniqueEdges.forEach((e) => edgeIndex.add(e.id))
        return {
          nodes: [...state.nodes, ...uniqueNodes],
          edges: [...state.edges, ...uniqueEdges],
          _nodeIndex: nodeIndex,
          _edgeIndex: edgeIndex,
        }
      }),

      // Selection
      selectedNodeIds: [],
      selectedEdgeIds: [],
      selectNode: (id, multi = false) => set((state) => ({
        selectedNodeIds: multi
          ? state.selectedNodeIds.includes(id)
            ? state.selectedNodeIds.filter((nid) => nid !== id)
            : [...state.selectedNodeIds, id]
          : state.selectedNodeIds.length === 1 && state.selectedNodeIds[0] === id
            ? [] // Toggle off: clicking the already-selected node deselects it
            : [id],
        selectedEdgeIds: multi ? state.selectedEdgeIds : [],
        // Single-select of a real entity opens (or swaps) the sticky drawer.
        // Toggle-off keeps it open — only the X button closes it. Logical
        // groupings and multi-select never touch the drawer.
        ...(!multi && !id.startsWith('logical:') ? { drawerNodeId: id } : {}),
      })),
      selectEdge: (id, multi = false) => set((state) => ({
        selectedEdgeIds: multi
          ? state.selectedEdgeIds.includes(id)
            ? state.selectedEdgeIds.filter((eid) => eid !== id)
            : [...state.selectedEdgeIds, id]
          : [id],
        selectedNodeIds: multi ? state.selectedNodeIds : [],
        // Mutual exclusion: selecting an edge swaps the right rail to the
        // edge drawer.
        drawerNodeId: null,
      })),
      clearSelection: () => set({ selectedNodeIds: [], selectedEdgeIds: [] }),

      // Sticky entity drawer
      drawerNodeId: null,
      openNodeDrawer: (id) => set({ drawerNodeId: id }),
      closeNodeDrawer: () => set({ drawerNodeId: null }),

      // Viewport
      viewport: { x: 0, y: 0, zoom: 1 },
      setViewport: (viewport) => set({ viewport }),

      // Loading
      isLoading: false,
      loadingRegions: new Set(),
      hydrationPhase: 'idle',
      setHydrationPhase: (hydrationPhase) => set({ hydrationPhase }),
      setLoading: (isLoading) => set({ isLoading }),
      addLoadingRegion: (region) => set((state) => {
        const newRegions = new Set(state.loadingRegions)
        newRegions.add(region)
        return { loadingRegions: newRegions, isLoading: true }
      }),
      removeLoadingRegion: (region) => set((state) => {
        const newRegions = new Set(state.loadingRegions)
        newRegions.delete(region)
        return {
          loadingRegions: newRegions,
          isLoading: newRegions.size > 0
        }
      }),

      // Active Lens
      activeLensId: null,
      setActiveLens: (activeLensId) => set({ activeLensId }),

      // Trace
      traceOrigin: null,
      traceDirection: 'both',
      traceDepth: 10,
      setTraceOrigin: (traceOrigin) => set({ traceOrigin }),
      setTraceDirection: (traceDirection) => set({ traceDirection }),
      setTraceDepth: (traceDepth) => set({ traceDepth }),

      // Cache
      cachedRegions: new Map(),
      cacheRegion: (key, nodes) => set((state) => {
        const newCache = new Map(state.cachedRegions)
        newCache.set(key, nodes)
        return { cachedRegions: newCache }
      }),
      getCachedRegion: (key) => get().cachedRegions.get(key),
      clearCache: () => set({ cachedRegions: new Map() }),

      // Editing Mode
      isEditing: false,
      setEditing: (isEditing) => set({ isEditing }),

      // Node/Edge CRUD (Manual)
      updateNode: (id, data) => set((state) => ({
        nodes: state.nodes.map((n) =>
          n.id === id ? { ...n, data: { ...n.data, ...data } } : n
        )
      })),
      removeNode: (id) => set((state) => {
        const nextNodeIndex = new Set(state._nodeIndex)
        nextNodeIndex.delete(id)
        const remainingEdges = state.edges.filter((e) => e.source !== id && e.target !== id)
        const nextEdgeIndex = new Set(remainingEdges.map((e) => e.id))
        return {
          nodes: state.nodes.filter((n) => n.id !== id),
          edges: remainingEdges,
          _nodeIndex: nextNodeIndex,
          _edgeIndex: nextEdgeIndex,
        }
      }),
      removeNodes: (ids) => set((state) => {
        if (ids.length === 0) return state
        const idSet = new Set(ids)
        const nextNodeIndex = new Set(state._nodeIndex)
        ids.forEach(id => nextNodeIndex.delete(id))
        const remainingEdges = state.edges.filter((e) => !idSet.has(e.source) && !idSet.has(e.target))
        const nextEdgeIndex = new Set(remainingEdges.map((e) => e.id))
        return {
          nodes: state.nodes.filter((n) => !idSet.has(n.id)),
          edges: remainingEdges,
          _nodeIndex: nextNodeIndex,
          _edgeIndex: nextEdgeIndex,
        }
      }),
      removeEdge: (id) => set((state) => {
        const nextEdgeIndex = new Set(state._edgeIndex)
        nextEdgeIndex.delete(id)
        return {
          edges: state.edges.filter((e) => e.id !== id),
          _edgeIndex: nextEdgeIndex,
        }
      }),
      removeEdges: (ids) => set((state) => {
        if (ids.length === 0) return state
        const idSet = new Set(ids)
        const nextEdgeIndex = new Set(state._edgeIndex)
        ids.forEach(id => nextEdgeIndex.delete(id))
        return {
          edges: state.edges.filter((e) => !idSet.has(e.id)),
          _edgeIndex: nextEdgeIndex,
        }
      }),
      removeEdgesByNodeIds: (nodeIds) => set((state) => {
        const nodeIdSet = nodeIds instanceof Set ? nodeIds : new Set(nodeIds)
        if (nodeIdSet.size === 0) return state
        const nextEdgeIndex = new Set(state._edgeIndex)
        const remainingEdges: LineageEdge[] = []
        for (const e of state.edges) {
          if (nodeIdSet.has(e.source) || nodeIdSet.has(e.target)) {
            nextEdgeIndex.delete(e.id)
          } else {
            remainingEdges.push(e)
          }
        }
        if (remainingEdges.length === state.edges.length) return state
        return { edges: remainingEdges, _edgeIndex: nextEdgeIndex }
      }),
    })),
    {
      name: 'canvas-storage',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        viewport: state.viewport,
        activeLensId: state.activeLensId,
      }),
    }
  )
)

// Selector hooks
export const useNodes = () => useCanvasStore((s) => s.nodes)
export const useEdges = () => useCanvasStore((s) => s.edges)
export const useSelectedNodes = () => useCanvasStore((s) => s.selectedNodeIds)
export const useIsLoading = () => useCanvasStore((s) => s.isLoading)
export const useCanvasVersion = () => useCanvasStore((s) => s._version)

