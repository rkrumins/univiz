/**
 * useEdgeFilters - Hook for managing edge visibility, filtering, and highlighting
 * 
 * Provides state and actions for:
 * - Toggling edge detail panel visibility
 * - Filtering edges by type and direction
 * - Highlighted edges on canvas
 * - Node-centric edge exploration
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { useMemo } from 'react'
import { useCanvasStore, type LineageEdge } from '@/store/canvas'
import { useContainmentEdgeTypes, normalizeEdgeType, isContainmentEdgeType } from '@/store/schema'
import type { EdgeTypeFilter } from '@/components/panels/EdgeDetailPanel'

// ============================================
// Types
// ============================================

export type EdgeDirection = 'all' | 'incoming' | 'outgoing' | 'upstream' | 'downstream'

export interface EdgeDirectionFilter {
    id: EdgeDirection
    label: string
    description: string
    icon: string
}

export const EDGE_DIRECTION_FILTERS: EdgeDirectionFilter[] = [
    { id: 'all', label: 'All', description: 'All edges in the graph', icon: 'Network' },
    { id: 'incoming', label: 'Direct Incoming', description: 'Edges pointing to the selected node', icon: 'ArrowDownLeft' },
    { id: 'outgoing', label: 'Direct Outgoing', description: 'Edges from the selected node', icon: 'ArrowUpRight' },
    { id: 'upstream', label: 'All Upstream', description: 'All edges leading to the selected node (transitive)', icon: 'GitMerge' },
    { id: 'downstream', label: 'All Downstream', description: 'All edges from the selected node (transitive)', icon: 'GitBranch' },
]

// ============================================
// Default Filters (fallback when no edges exist)
// ============================================

// Empty by default — filters are populated from actual edges discovered in the graph
// via generateEdgeTypeFiltersFromEdges(). No hardcoded edge types.
export const DEFAULT_EDGE_FILTERS: EdgeTypeFilter[] = []

/**
 * Generate edge type filters from discovered edge types
 * This is used to initialize filters when edges are first loaded
 */
export function generateEdgeTypeFiltersFromEdges(
    edges: LineageEdge[],
    relationshipTypes: any[],
    containmentEdgeTypes: string[],
    ontologyMetadata?: any
): EdgeTypeFilter[] {
    // Import here to avoid circular dependency
    const { getAllEdgeTypeDefinitions } = require('@/utils/edgeTypeUtils')
    const definitions = getAllEdgeTypeDefinitions(
        edges,
        relationshipTypes,
        containmentEdgeTypes,
        ontologyMetadata ? { edgeTypeMetadata: ontologyMetadata.edgeTypeMetadata } : undefined
    )

    return definitions.map(def => ({
        type: def.type.toLowerCase(), // Use lowercase for filter matching
        label: def.label,
        color: def.color,
        enabled: true, // Default to enabled
    }))
}

// ============================================
// Store
// ============================================

interface EdgeFiltersState {
    // Panel visibility
    isDetailPanelOpen: boolean
    toggleDetailPanel: () => void
    openDetailPanel: () => void
    closeDetailPanel: () => void

    // Edge type filters
    filters: EdgeTypeFilter[]
    toggleFilter: (type: string) => void
    enableAllFilters: () => void
    disableAllFilters: () => void
    resetFilters: () => void
    setFilterEnabled: (type: string, enabled: boolean) => void

    // Direction filter
    directionFilter: EdgeDirection
    setDirectionFilter: (direction: EdgeDirection) => void

    // Focused node for edge exploration
    focusedNodeId: string | null
    setFocusedNode: (nodeId: string | null) => void

    // Highlighted edges (shown prominently on canvas)
    highlightedEdgeIds: Set<string>
    highlightEdge: (edgeId: string) => void
    unhighlightEdge: (edgeId: string) => void
    toggleHighlightEdge: (edgeId: string) => void
    setHighlightedEdges: (edgeIds: string[]) => void
    clearHighlightedEdges: () => void
    highlightMode: 'pulse' | 'glow' | 'bold'
    setHighlightMode: (mode: 'pulse' | 'glow' | 'bold') => void

    // Isolate mode - only show highlighted edges
    isolateMode: boolean
    toggleIsolateMode: () => void
    setIsolateMode: (enabled: boolean) => void

    // Show edge labels on canvas
    showEdgeLabels: boolean
    toggleEdgeLabels: () => void

    // Confidence threshold
    confidenceThreshold: number
    setConfidenceThreshold: (threshold: number) => void
}

export const useEdgeFiltersStore = create<EdgeFiltersState>()(
    persist(
        (set) => ({
            // Panel visibility
            isDetailPanelOpen: false,
            toggleDetailPanel: () => set((s) => ({ isDetailPanelOpen: !s.isDetailPanelOpen })),
            openDetailPanel: () => set({ isDetailPanelOpen: true }),
            closeDetailPanel: () => set({ isDetailPanelOpen: false }),

            // Edge type filters
            filters: DEFAULT_EDGE_FILTERS,
            toggleFilter: (type) => set((s) => ({
                filters: s.filters.map((f) =>
                    f.type === type ? { ...f, enabled: !f.enabled } : f
                ),
            })),
            enableAllFilters: () => set((s) => ({
                filters: s.filters.map((f) => ({ ...f, enabled: true })),
            })),
            disableAllFilters: () => set((s) => ({
                filters: s.filters.map((f) => ({ ...f, enabled: false })),
            })),
            resetFilters: () => set({ filters: DEFAULT_EDGE_FILTERS }),
            setFilterEnabled: (type, enabled) => set((s) => ({
                filters: s.filters.map((f) =>
                    f.type === type ? { ...f, enabled } : f
                ),
            })),

            // Direction filter
            directionFilter: 'all',
            setDirectionFilter: (direction) => set({ directionFilter: direction }),

            // Focused node
            focusedNodeId: null,
            setFocusedNode: (nodeId) => set({ focusedNodeId: nodeId }),

            // Highlighted edges
            highlightedEdgeIds: new Set(),
            highlightEdge: (edgeId) => set((s) => {
                const newSet = new Set(s.highlightedEdgeIds)
                newSet.add(edgeId)
                return { highlightedEdgeIds: newSet }
            }),
            unhighlightEdge: (edgeId) => set((s) => {
                const newSet = new Set(s.highlightedEdgeIds)
                newSet.delete(edgeId)
                return { highlightedEdgeIds: newSet }
            }),
            toggleHighlightEdge: (edgeId) => set((s) => {
                const newSet = new Set(s.highlightedEdgeIds)
                if (newSet.has(edgeId)) {
                    newSet.delete(edgeId)
                } else {
                    newSet.add(edgeId)
                }
                return { highlightedEdgeIds: newSet }
            }),
            setHighlightedEdges: (edgeIds) => set({ highlightedEdgeIds: new Set(edgeIds) }),
            clearHighlightedEdges: () => set({ highlightedEdgeIds: new Set() }),
            highlightMode: 'glow',
            setHighlightMode: (mode) => set({ highlightMode: mode }),

            // Isolate mode
            isolateMode: false,
            toggleIsolateMode: () => set((s) => ({ isolateMode: !s.isolateMode })),
            setIsolateMode: (enabled) => set({ isolateMode: enabled }),

            // Edge labels
            showEdgeLabels: false,
            toggleEdgeLabels: () => set((s) => ({ showEdgeLabels: !s.showEdgeLabels })),

            // Confidence threshold
            confidenceThreshold: 0,
            setConfidenceThreshold: (threshold) => set({ confidenceThreshold: threshold }),
        }),
        {
            name: 'edge-filters-storage',
            partialize: (state) => ({
                filters: state.filters,
                showEdgeLabels: state.showEdgeLabels,
                confidenceThreshold: state.confidenceThreshold,
                highlightMode: state.highlightMode,
            }),
        }
    )
)

// ============================================
// Traversal helpers for upstream/downstream
// ============================================

function findUpstreamEdges(
    nodeId: string,
    edges: LineageEdge[],
    visited = new Set<string>()
): LineageEdge[] {
    const result: LineageEdge[] = []
    const incomingEdges = edges.filter(e => e.target === nodeId && !visited.has(e.id))

    incomingEdges.forEach(edge => {
        visited.add(edge.id)
        result.push(edge)
        // Recurse upstream
        result.push(...findUpstreamEdges(edge.source, edges, visited))
    })

    return result
}

function findDownstreamEdges(
    nodeId: string,
    edges: LineageEdge[],
    visited = new Set<string>()
): LineageEdge[] {
    const result: LineageEdge[] = []
    const outgoingEdges = edges.filter(e => e.source === nodeId && !visited.has(e.id))

    outgoingEdges.forEach(edge => {
        visited.add(edge.id)
        result.push(edge)
        // Recurse downstream
        result.push(...findDownstreamEdges(edge.target, edges, visited))
    })

    return result
}

// ============================================
// Hook for filtered edges with direction
// ============================================

export function useFilteredEdges(): {
    allEdges: LineageEdge[]
    filteredEdges: LineageEdge[]
    containmentEdges: LineageEdge[]
    lineageEdges: LineageEdge[]
    directionFilteredEdges: LineageEdge[]
    highlightedEdges: LineageEdge[]
} {
    const edges = useCanvasStore((s) => s.edges)
    const filters = useEdgeFiltersStore((s) => s.filters)
    const confidenceThreshold = useEdgeFiltersStore((s) => s.confidenceThreshold)
    const directionFilter = useEdgeFiltersStore((s) => s.directionFilter)
    const focusedNodeId = useEdgeFiltersStore((s) => s.focusedNodeId)
    const highlightedEdgeIds = useEdgeFiltersStore((s) => s.highlightedEdgeIds)
    const isolateMode = useEdgeFiltersStore((s) => s.isolateMode)
    const containmentEdgeTypes = useContainmentEdgeTypes()

    return useMemo(() => {
        const enabledTypes = new Set(
            filters.filter((f) => f.enabled).map((f) => f.type)
        )

        // When no filters are defined yet (schema not loaded, no edges discovered),
        // pass all edges through rather than filtering everything out.
        const hasFilters = filters.length > 0

        // Type-filtered edges (case-insensitive matching)
        const typeFiltered = edges.filter((edge) => {
            const confidence = edge.data?.confidence ?? 1
            if (confidence < confidenceThreshold) return false
            // If no filters defined, show all edges
            if (!hasFilters) return true
            const normalized = normalizeEdgeType(edge).toLowerCase()
            const originalType = (edge.data?.edgeType || edge.data?.relationship || 'unknown').toLowerCase()
            // Match against normalized or original type (case-insensitive)
            return enabledTypes.has(normalized) || enabledTypes.has(originalType)
        })

        const containment = edges.filter((e) =>
            isContainmentEdgeType(normalizeEdgeType(e), containmentEdgeTypes)
        )

        const lineage = edges.filter((e) =>
            !isContainmentEdgeType(normalizeEdgeType(e), containmentEdgeTypes)
        )

        // Apply direction filter if we have a focused node
        let directionFiltered = typeFiltered
        if (focusedNodeId && directionFilter !== 'all') {
            switch (directionFilter) {
                case 'incoming':
                    directionFiltered = typeFiltered.filter(e => e.target === focusedNodeId)
                    break
                case 'outgoing':
                    directionFiltered = typeFiltered.filter(e => e.source === focusedNodeId)
                    break
                case 'upstream':
                    directionFiltered = findUpstreamEdges(focusedNodeId, typeFiltered)
                    break
                case 'downstream':
                    directionFiltered = findDownstreamEdges(focusedNodeId, typeFiltered)
                    break
            }
        }

        // Get highlighted edges
        const highlighted = edges.filter(e => highlightedEdgeIds.has(e.id))

        // In isolate mode, only show highlighted edges — composed AFTER type +
        // direction filters so all three stack predictably for canvas rendering.
        const finalFiltered = isolateMode && highlightedEdgeIds.size > 0
            ? directionFiltered.filter(e => highlightedEdgeIds.has(e.id))
            : directionFiltered

        return {
            allEdges: edges,
            filteredEdges: finalFiltered,
            containmentEdges: containment,
            lineageEdges: lineage,
            directionFilteredEdges: directionFiltered,
            highlightedEdges: highlighted,
        }
    }, [edges, filters, confidenceThreshold, directionFilter, focusedNodeId, highlightedEdgeIds, isolateMode, containmentEdgeTypes])
}

// ============================================
// Hook for node-centric edge exploration
// ============================================

export function useNodeEdges(nodeId: string | null): {
    incomingEdges: LineageEdge[]
    outgoingEdges: LineageEdge[]
    upstreamEdges: LineageEdge[]
    downstreamEdges: LineageEdge[]
    allNodeEdges: LineageEdge[]
    edgeStats: {
        incomingCount: number
        outgoingCount: number
        upstreamCount: number
        downstreamCount: number
    }
} {
    const edges = useCanvasStore((s) => s.edges)
    const filters = useEdgeFiltersStore((s) => s.filters)

    return useMemo(() => {
        if (!nodeId) {
            return {
                incomingEdges: [],
                outgoingEdges: [],
                upstreamEdges: [],
                downstreamEdges: [],
                allNodeEdges: [],
                edgeStats: { incomingCount: 0, outgoingCount: 0, upstreamCount: 0, downstreamCount: 0 }
            }
        }

        const enabledTypes = new Set(
            filters.filter((f) => f.enabled).map((f) => f.type)
        )

        // When no filters are defined yet (schema not loaded, no edges discovered),
        // pass all edges through rather than filtering everything out.
        const hasFilters = filters.length > 0

        // Filter by type first (case-insensitive matching)
        const typeFiltered = edges.filter((edge) => {
            // If no filters defined, show all edges
            if (!hasFilters) return true
            const normalized = normalizeEdgeType(edge).toLowerCase()
            const originalType = (edge.data?.edgeType || edge.data?.relationship || 'unknown').toLowerCase()
            // Match against normalized or original type (case-insensitive)
            return enabledTypes.has(normalized) || enabledTypes.has(originalType)
        })

        const incoming = typeFiltered.filter(e => e.target === nodeId)
        const outgoing = typeFiltered.filter(e => e.source === nodeId)
        const upstream = findUpstreamEdges(nodeId, typeFiltered)
        const downstream = findDownstreamEdges(nodeId, typeFiltered)

        return {
            incomingEdges: incoming,
            outgoingEdges: outgoing,
            upstreamEdges: upstream,
            downstreamEdges: downstream,
            allNodeEdges: [...new Set([...incoming, ...outgoing])],
            edgeStats: {
                incomingCount: incoming.length,
                outgoingCount: outgoing.length,
                upstreamCount: upstream.length,
                downstreamCount: downstream.length,
            }
        }
    }, [nodeId, edges, filters])
}

// ============================================
// Convenience Hooks
// ============================================

export function useEdgeDetailPanel() {
    const isOpen = useEdgeFiltersStore((s) => s.isDetailPanelOpen)
    const toggle = useEdgeFiltersStore((s) => s.toggleDetailPanel)
    const open = useEdgeFiltersStore((s) => s.openDetailPanel)
    const close = useEdgeFiltersStore((s) => s.closeDetailPanel)

    return { isOpen, toggle, open, close }
}

export function useEdgeTypeFilters() {
    const filters = useEdgeFiltersStore((s) => s.filters)
    const toggle = useEdgeFiltersStore((s) => s.toggleFilter)
    const enableAll = useEdgeFiltersStore((s) => s.enableAllFilters)
    const disableAll = useEdgeFiltersStore((s) => s.disableAllFilters)
    const reset = useEdgeFiltersStore((s) => s.resetFilters)
    const setEnabled = useEdgeFiltersStore((s) => s.setFilterEnabled)

    return { filters, toggle, enableAll, disableAll, reset, setEnabled }
}

export function useEdgeHighlighting() {
    const highlightedEdgeIds = useEdgeFiltersStore((s) => s.highlightedEdgeIds)
    const highlightEdge = useEdgeFiltersStore((s) => s.highlightEdge)
    const unhighlightEdge = useEdgeFiltersStore((s) => s.unhighlightEdge)
    const toggleHighlight = useEdgeFiltersStore((s) => s.toggleHighlightEdge)
    const setHighlighted = useEdgeFiltersStore((s) => s.setHighlightedEdges)
    const clearHighlighted = useEdgeFiltersStore((s) => s.clearHighlightedEdges)
    const highlightMode = useEdgeFiltersStore((s) => s.highlightMode)
    const setHighlightMode = useEdgeFiltersStore((s) => s.setHighlightMode)
    const isolateMode = useEdgeFiltersStore((s) => s.isolateMode)
    const toggleIsolate = useEdgeFiltersStore((s) => s.toggleIsolateMode)

    return {
        highlightedEdgeIds,
        highlightEdge,
        unhighlightEdge,
        toggleHighlight,
        setHighlighted,
        clearHighlighted,
        highlightMode,
        setHighlightMode,
        isolateMode,
        toggleIsolate,
        isHighlighted: (edgeId: string) => highlightedEdgeIds.has(edgeId),
    }
}

export function useEdgeDirectionFilter() {
    const direction = useEdgeFiltersStore((s) => s.directionFilter)
    const setDirection = useEdgeFiltersStore((s) => s.setDirectionFilter)
    const focusedNodeId = useEdgeFiltersStore((s) => s.focusedNodeId)
    const setFocusedNode = useEdgeFiltersStore((s) => s.setFocusedNode)

    return { direction, setDirection, focusedNodeId, setFocusedNode }
}
