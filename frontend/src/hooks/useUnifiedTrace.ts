/**
 * useUnifiedTrace - Unified trace functionality across all canvas views
 * 
 * Consolidates trace logic across all canvas views (GraphCanvas, ContextViewCanvas, HierarchyCanvas)
 * into a single reusable hook with configurable depth and direction.
 * 
 * Features:
 * - Server-side driven trace (calls backend /trace API)
 * - Auto-sync traced nodes/edges to canvas store
 * - Configurable upstream/downstream depths
 * - Direction filtering (show/hide upstream/downstream)
 * - Re-trace on config change
 * - URN-to-ID mapping for store synchronization
 */

import { create } from 'zustand'
import { useCallback, useMemo, useEffect, useRef } from 'react'
import type {
    GraphDataProvider, GraphEdge, LineageResult, TraceOptions,
    TraceV2Result, TraceV2Request,
} from '@/providers/GraphDataProvider'
import type { TraceMeta } from '@/services/traceApi'
import { useCanvasStore } from '@/store/canvas'

// ============================================
// Types
// ============================================

export type TraceDirection = 'upstream' | 'downstream' | 'both'
export type TraceStatus = 'idle' | 'loading' | 'success' | 'error'

export interface TraceConfig {
    /** Maximum depth for upstream traversal (1-99) */
    upstreamDepth: number
    /** Maximum depth for downstream traversal (1-99) */
    downstreamDepth: number
    /** Include column-level lineage in trace (legacy /trace path) */
    includeColumnLineage: boolean
    /** Exclude containment edges for pure data lineage (default: true) */
    excludeContainmentEdges: boolean
    /** Include inherited lineage from parent if no direct lineage */
    includeInheritedLineage: boolean
    /** Auto-expand ancestors when tracing */
    autoExpandAncestors: boolean
    /** Show only the traced path vs show with context */
    pathOnly: boolean
    /** Auto-sync traced nodes to canvas store */
    autoSyncToStore: boolean
    /** Legacy granularity override — superseded by `level` for /trace/v2 */
    granularity?: 'column' | 'table' | 'schema' | 'system' | 'domain'
    /**
     * Trace v2 hierarchy level.
     * - "auto" = peer rollup at source's own hierarchy.level (default)
     * - integer = literal level (0 = coarsest)
     * - string = entity-type-id ("dataset"); resolved to that type's level
     */
    level: 'auto' | number | string
    /** Optional whitelist of lineage edge types to trace (empty = all ontology lineage types) */
    lineageEdgeTypes: string[]
    /** Also fetch parent-child edges between returned nodes (single round-trip) */
    includeContainmentEdges: boolean
}

export interface TraceResult {
    /** The node being traced from */
    focusId: string
    /** All nodes in the trace */
    traceNodes: Set<string>
    /** Upstream nodes only */
    upstreamNodes: Set<string>
    /** Downstream nodes only */
    downstreamNodes: Set<string>
    /** All edges in the trace */
    traceEdges: Set<string>
    /** Raw lineage result from backend (legacy shape — synthesized from v2 result for back-compat) */
    lineageResult: LineageResult | null
    // ---- v2 fields (server-resolved, populated when /trace/v2 is used) ----
    /** Hierarchy level the trace ran at */
    effectiveLevel?: number
    /** True if focus had no direct lineage and an ancestor was used as anchor */
    isInherited?: boolean
    /** Ancestor URN that was used when isInherited=true */
    inheritedFromUrn?: string
    /** True if hard caps tripped */
    truncated?: boolean
    /** "max_nodes" | "timeout" | undefined */
    truncationReason?: string | null
    /**
     * Containment edges (parent → child) returned by the backend. Always
     * populated by /trace/v2 — needed so the canvas can position trace
     * nodes (especially deep ones like columns) in the layered hierarchy.
     * Without these, deep trace nodes render as orphans.
     */
    containmentEdges?: GraphEdge[]
    /**
     * Ancestor nodes (Domain → Container → Dataset …) that the backend
     * hydrated alongside the lineage participants. They're already in
     * `lineageResult.nodes` for back-compat; this set lets consumers tell
     * "lineage participant" from "ancestor for hierarchy context" when it
     * matters (e.g. for a "Show only direct lineage" toggle).
     */
    ancestorUrns?: Set<string>
    /**
     * Sidecar metadata: cache hit/miss, regime, query latency, materialised
     * hit rate. Populated when the v2 envelope emits a `meta` block. Used by
     * the Performance tab in the trace panel.
     */
    meta?: TraceMeta
}

/** Recent-focus entry for the trace history breadcrumb. */
export interface TraceHistoryEntry {
    /** Internal node ID at the time the trace was started. */
    focusId: string
    /** URN at the time the trace was started — display name resolved at render time. */
    focusUrn: string
    /** Effective hierarchy level the trace ran at (from result.effectiveLevel). */
    level?: number
    /** Wall-clock timestamp of the push. */
    timestamp: number
    /** Snapshot of the trace config used — applied when the user jumps back. */
    config: TraceConfig
}

/** Drill-down state — keyed by `${sourceUrn}->${targetUrn}@${atLevel}`. */
export type DrilldownKey = string
export const drilldownKey = (sourceUrn: string, targetUrn: string, atLevel: number): DrilldownKey =>
    `${sourceUrn}->${targetUrn}@${atLevel}`

export interface TraceState {
    /** Current trace status */
    status: TraceStatus
    /** Error message if failed */
    error: string | null
    /** Current focus node ID (if tracing) */
    focusId: string | null
    /** Current trace result */
    result: TraceResult | null
    /** Trace configuration */
    config: TraceConfig
    /** Direction toggle states */
    showUpstream: boolean
    showDownstream: boolean
    /** Drill-down results keyed by `${sourceUrn}->${targetUrn}@${atLevel}` */
    drilldowns: Map<DrilldownKey, TraceV2Result>
    /** Recent-focus breadcrumb (newest first, capped at 5). Session-scoped. */
    traceHistory: TraceHistoryEntry[]

    // Actions
    setFocus: (nodeId: string | null) => void
    setConfig: (config: Partial<TraceConfig>) => void
    setShowUpstream: (show: boolean) => void
    setShowDownstream: (show: boolean) => void
    fetchTrace: (nodeId: string, provider: GraphDataProvider, urnResolver?: (id: string) => string) => Promise<TraceResult | null>
    /** Drill into an AGGREGATED edge — fetch finer-level lineage between subtrees. */
    expandAggregatedEdge: (
        sourceUrn: string,
        targetUrn: string,
        currentLevel: number,
        provider: GraphDataProvider,
    ) => Promise<TraceV2Result | null>
    /** Collapse a previously-opened drilldown (reverts to the original AGGREGATED edge). */
    collapseDrilldown: (key: DrilldownKey) => void
    /** Drop the trace-history breadcrumb. */
    clearTraceHistory: () => void
    clearTrace: () => void
    reset: () => void
}

/** History capacity. Older entries are evicted FIFO. */
const TRACE_HISTORY_LIMIT = 5

// ============================================
// Default Configuration
// ============================================

const DEFAULT_CONFIG: TraceConfig = {
    // Default hop depth — set high enough that a typical end-to-end pipeline
    // (raw → staging → refined → consumption → reporting) plus a couple of
    // intermediate transforms doesn't get truncated. Backend caps further
    // if needed.
    upstreamDepth: 25,
    downstreamDepth: 25,
    includeColumnLineage: true,
    excludeContainmentEdges: true,
    includeInheritedLineage: true,
    autoExpandAncestors: true,
    pathOnly: false,
    autoSyncToStore: true,
    lineageEdgeTypes: [],  // Empty = use all ontology-classified lineage types
    level: 'auto',         // v2: peer rollup at source's own hierarchy.level
    // ContextView positions every node by walking containment from a layer
    // root to its descendants. /trace/v2 returning lineage participants but
    // NOT their containment chains makes deep participants (e.g. schemaField
    // upstream of focus) orphans — invisible in the canvas. Default to true
    // so trace results are always positionable in the hierarchy.
    includeContainmentEdges: true,
    granularity: 'column', // legacy /trace path only — superseded by `level` for v2
}

// ============================================
// Zustand Store
// ============================================

export const useTraceStore = create<TraceState>((set, get) => ({
    status: 'idle',
    error: null,
    focusId: null,
    result: null,
    config: DEFAULT_CONFIG,
    showUpstream: true,
    showDownstream: true,
    drilldowns: new Map(),
    traceHistory: [],

    setFocus: (nodeId) => {
        if (nodeId === null) {
            set({ focusId: null, result: null, status: 'idle', drilldowns: new Map() })
        } else {
            set({ focusId: nodeId })
        }
    },

    setConfig: (config) => {
        set(state => ({
            config: { ...state.config, ...config }
        }))
    },

    setShowUpstream: (show) => set({ showUpstream: show }),
    setShowDownstream: (show) => set({ showDownstream: show }),

    fetchTrace: async (nodeId, provider, urnResolver) => {
        const { config } = get()

        // New trace clears any drilldowns from a previous focus.
        set({ status: 'loading', error: null, focusId: nodeId, drilldowns: new Map() })

        try {
            const urn = urnResolver ? urnResolver(nodeId) : nodeId

            // Direction-aware depths: when a preset (traceUpstream/traceDownstream)
            // sets one depth to 0, the request should reflect that.
            const upDepth = config.upstreamDepth
            const downDepth = config.downstreamDepth
            const direction: 'upstream' | 'downstream' | 'both' =
                upDepth > 0 && downDepth === 0 ? 'upstream' :
                downDepth > 0 && upDepth === 0 ? 'downstream' : 'both'

            // Prefer /trace/v2 — Cypher-native, ontology-aware peer-level rollup.
            let traceResult: TraceResult
            if (typeof provider.traceAtLevel === 'function') {
                const req: TraceV2Request = {
                    urn,
                    direction,
                    upstreamDepth: upDepth,
                    downstreamDepth: downDepth,
                    level: config.level,
                    lineageEdgeTypes: config.lineageEdgeTypes.length > 0 ? config.lineageEdgeTypes : null,
                    includeContainmentEdges: config.includeContainmentEdges,
                    includeInheritedLineage: config.includeInheritedLineage,
                }
                const v2 = await provider.traceAtLevel(req)
                traceResult = traceResultFromV2(nodeId, urn, v2)
            } else {
                // Legacy /trace path — only providers that don't implement v2.
                const traceOptions: TraceOptions = {
                    includeColumnLineage: config.includeColumnLineage,
                    excludeContainmentEdges: config.excludeContainmentEdges,
                    includeInheritedLineage: config.includeInheritedLineage,
                    ...(config.lineageEdgeTypes.length > 0 ? { lineageEdgeTypes: config.lineageEdgeTypes } : {}),
                    granularity: config.granularity ?? 'column',
                }
                const lineage = await provider.getFullLineage(urn, upDepth, downDepth, traceOptions)
                traceResult = traceResultFromLegacy(nodeId, urn, lineage)
            }

            // Push the new focus into the history breadcrumb. Skip the push
            // when the user re-traces the same focus (e.g. config tweak +
            // retrace) so history shows distinct focus nodes, not a wall of
            // duplicates. Cap at TRACE_HISTORY_LIMIT (FIFO).
            const { traceHistory } = get()
            const head = traceHistory[0]
            const isSameFocus = head?.focusId === nodeId
            const nextHistory: TraceHistoryEntry[] = isSameFocus
                ? traceHistory
                : [
                      {
                          focusId: nodeId,
                          focusUrn: urn,
                          level: traceResult.effectiveLevel,
                          timestamp: Date.now(),
                          config,
                      },
                      ...traceHistory,
                  ].slice(0, TRACE_HISTORY_LIMIT)

            set({ status: 'success', result: traceResult, traceHistory: nextHistory })
            return traceResult
        } catch (err) {
            set({
                status: 'error',
                error: err instanceof Error ? err.message : 'Failed to fetch trace',
            })
            return null
        }
    },

    expandAggregatedEdge: async (sourceUrn, targetUrn, currentLevel, provider) => {
        if (typeof provider.expandAggregated !== 'function') {
            // Provider doesn't support drill-down — caller should disable the UI affordance.
            return null
        }
        const { config, drilldowns } = get()
        const nextLevel = currentLevel + 1
        const key = drilldownKey(sourceUrn, targetUrn, nextLevel)
        if (drilldowns.has(key)) return drilldowns.get(key) ?? null

        try {
            const v2 = await provider.expandAggregated({
                sourceUrn,
                targetUrn,
                nextLevel,
                lineageEdgeTypes: config.lineageEdgeTypes.length > 0 ? config.lineageEdgeTypes : null,
                includeContainmentEdges: config.includeContainmentEdges,
            })
            // Merge into the drilldowns map (immutable replacement so React selectors notice).
            const next = new Map(drilldowns)
            next.set(key, v2)
            set({ drilldowns: next })
            return v2
        } catch (err) {
            // Drill-down failure is non-fatal — keep the trace state, surface error.
            set({ error: err instanceof Error ? err.message : 'Failed to expand aggregated edge' })
            return null
        }
    },

    collapseDrilldown: (key) => {
        const { drilldowns } = get()
        if (!drilldowns.has(key)) return
        const next = new Map(drilldowns)
        next.delete(key)
        set({ drilldowns: next })
    },

    clearTraceHistory: () => set({ traceHistory: [] }),

    clearTrace: () => {
        set({
            focusId: null,
            result: null,
            status: 'idle',
            error: null,
            showUpstream: true,
            showDownstream: true,
            drilldowns: new Map(),
        })
    },

    reset: () => {
        set({
            status: 'idle',
            error: null,
            focusId: null,
            result: null,
            config: DEFAULT_CONFIG,
            showUpstream: true,
            showDownstream: true,
            drilldowns: new Map(),
            traceHistory: [],
        })
    },
}))

// ----- Result builders ------------------------------------------------------

function traceResultFromV2(focusId: string, focusUrn: string, v2: TraceV2Result): TraceResult {
    const traceNodes = new Set<string>()
    const upstreamNodes = new Set<string>(v2.upstreamUrns)
    const downstreamNodes = new Set<string>(v2.downstreamUrns)
    const traceEdges = new Set<string>()

    traceNodes.add(focusId)
    traceNodes.add(focusUrn)
    v2.nodes.forEach(n => traceNodes.add(n.urn))
    v2.upstreamUrns.forEach(u => traceNodes.add(u))
    v2.downstreamUrns.forEach(u => traceNodes.add(u))
    v2.edges.forEach(e => traceEdges.add(e.id))

    // Identify ancestor-only nodes: those returned by the backend purely for
    // hierarchy positioning (Domain/Container/Dataset chains around deep
    // lineage participants). They aren't lineage members themselves, so we
    // exclude them from upstream/downstream sets but keep them in the
    // canvas merge so the layered hierarchy can host the trace nodes.
    const lineageMembers = new Set<string>([focusUrn])
    v2.upstreamUrns.forEach(u => lineageMembers.add(u))
    v2.downstreamUrns.forEach(u => lineageMembers.add(u))
    const ancestorUrns = new Set<string>()
    v2.nodes.forEach(n => {
        if (!lineageMembers.has(n.urn)) ancestorUrns.add(n.urn)
    })

    // Synthesize a legacy-shape LineageResult so existing consumers
    // (ContextViewCanvas merge, EdgeLegend, etc.) keep working unchanged.
    const lineageResult: LineageResult = {
        nodes: v2.nodes,
        edges: v2.edges,
        upstreamUrns: v2.upstreamUrns,
        downstreamUrns: v2.downstreamUrns,
        totalCount: v2.nodes.length,
        hasMore: false,
        ...(v2.isInherited && v2.inheritedFromUrn ? { inheritedFrom: v2.inheritedFromUrn } : {}),
    }

    return {
        focusId,
        traceNodes,
        upstreamNodes,
        downstreamNodes,
        traceEdges,
        lineageResult,
        effectiveLevel: v2.effectiveLevel,
        isInherited: v2.isInherited,
        inheritedFromUrn: v2.inheritedFromUrn ?? undefined,
        truncated: v2.truncated,
        truncationReason: v2.truncationReason ?? undefined,
        containmentEdges: v2.containmentEdges,
        ancestorUrns,
        meta: v2.meta,
    }
}

function traceResultFromLegacy(focusId: string, focusUrn: string, lineage: LineageResult): TraceResult {
    const traceNodes = new Set<string>([focusId, focusUrn])
    const upstreamNodes = new Set<string>()
    const downstreamNodes = new Set<string>()
    const traceEdges = new Set<string>()

    lineage.nodes.forEach(n => traceNodes.add(n.urn))
    lineage.upstreamUrns.forEach(u => { traceNodes.add(u); upstreamNodes.add(u) })
    lineage.downstreamUrns.forEach(u => { traceNodes.add(u); downstreamNodes.add(u) })
    lineage.edges.forEach(e => traceEdges.add(e.id))

    return {
        focusId,
        traceNodes,
        upstreamNodes,
        downstreamNodes,
        traceEdges,
        lineageResult: lineage,
    }
}

// ============================================
// Hook
// ============================================

export interface UseUnifiedTraceOptions {
    /** Graph data provider */
    provider: GraphDataProvider | null
    /** Function to resolve node ID to URN */
    urnResolver?: (nodeId: string) => string
    /** Callback when trace is completed */
    onTraceComplete?: (result: TraceResult) => void
}

export interface TraceStatistics {
    /** Total nodes in trace */
    totalNodes: number
    /** Upstream node count */
    upstreamCount: number
    /** Downstream node count */
    downstreamCount: number
    /** Total edges in trace */
    totalEdges: number
    /** Edge types in trace */
    edgeTypes: string[]
    /** Whether lineage was inherited from parent */
    isInherited: boolean
    /** Parent URN if inherited */
    inheritedFrom?: string
}

export interface UseUnifiedTraceResult {
    /** Current trace status */
    status: TraceStatus
    /** Error message if failed */
    error: string | null
    /** Current focus node ID */
    focusId: string | null
    /** Current trace result */
    result: TraceResult | null
    /** Is trace active */
    isTracing: boolean
    /** Is loading */
    isLoading: boolean

    /** Trace configuration */
    config: TraceConfig
    /** Update configuration */
    setConfig: (config: Partial<TraceConfig>) => void

    /** Direction visibility */
    showUpstream: boolean
    showDownstream: boolean
    setShowUpstream: (show: boolean) => void
    setShowDownstream: (show: boolean) => void

    /** Start trace from a node */
    startTrace: (nodeId: string) => Promise<void>
    /** Toggle trace on a node (start if not active, clear if same node) */
    toggleTrace: (nodeId: string) => Promise<void>
    /** Clear current trace */
    clearTrace: () => void
    /** Re-trace with current focus and updated config */
    retrace: () => Promise<void>

    // Preset actions
    /** Trace upstream only (root cause analysis) */
    traceUpstream: (nodeId: string) => Promise<void>
    /** Trace downstream only (impact analysis) */
    traceDownstream: (nodeId: string) => Promise<void>
    /** Full trace (both directions) */
    traceFullLineage: (nodeId: string) => Promise<void>

    /** Check if a node is in the trace */
    isInTrace: (nodeId: string) => boolean
    /** Check if a node is upstream */
    isUpstream: (nodeId: string) => boolean
    /** Check if a node is downstream */
    isDownstream: (nodeId: string) => boolean
    /** Check if a node is the focus */
    isFocus: (nodeId: string) => boolean

    /** Get visible trace nodes (filtered by direction toggles) */
    visibleTraceNodes: Set<string>
    /** Get trace context (includes ancestors for dimming logic) */
    traceContextSet: Set<string>

    /** Upstream count */
    upstreamCount: number
    /** Downstream count */
    downstreamCount: number

    /** Full trace statistics */
    statistics: TraceStatistics

    /** Drill-down state — keyed by `${sourceUrn}->${targetUrn}@${atLevel}` */
    drilldowns: Map<DrilldownKey, TraceV2Result>
    /** Drill into an AGGREGATED edge */
    expandAggregatedEdge: (sourceUrn: string, targetUrn: string, currentLevel: number) => Promise<TraceV2Result | null>
    /** Collapse a drilldown by key */
    collapseDrilldown: (key: DrilldownKey) => void

    /** Recent-focus breadcrumb (newest first, max 5). Persists across `clearTrace`; cleared on `reset` or explicit `clearTraceHistory`. */
    traceHistory: TraceHistoryEntry[]
    /** Restore a previous focus from history — applies its config and re-traces. */
    jumpToHistoryEntry: (entry: TraceHistoryEntry) => Promise<void>
    /** Drop the entire trace-history breadcrumb. */
    clearTraceHistory: () => void
}

export function useUnifiedTrace(options: UseUnifiedTraceOptions): UseUnifiedTraceResult {
    const { provider, urnResolver, onTraceComplete } = options

    // Get store state
    const status = useTraceStore(s => s.status)
    const error = useTraceStore(s => s.error)
    const focusId = useTraceStore(s => s.focusId)
    const result = useTraceStore(s => s.result)
    const config = useTraceStore(s => s.config)
    const showUpstream = useTraceStore(s => s.showUpstream)
    const showDownstream = useTraceStore(s => s.showDownstream)
    const drilldowns = useTraceStore(s => s.drilldowns)
    const traceHistory = useTraceStore(s => s.traceHistory)

    // Actions
    const setConfig = useTraceStore(s => s.setConfig)
    const setShowUpstream = useTraceStore(s => s.setShowUpstream)
    const setShowDownstream = useTraceStore(s => s.setShowDownstream)
    const fetchTrace = useTraceStore(s => s.fetchTrace)
    const clearTrace = useTraceStore(s => s.clearTrace)
    const setFocus = useTraceStore(s => s.setFocus)
    const expandAggregatedEdgeAction = useTraceStore(s => s.expandAggregatedEdge)
    const collapseDrilldown = useTraceStore(s => s.collapseDrilldown)
    const clearTraceHistory = useTraceStore(s => s.clearTraceHistory)

    // Canvas store for auto-sync
    const { nodes: canvasNodes } = useCanvasStore()

    // Track previous config for re-trace detection
    const prevConfigRef = useRef(config)

    // Derived state
    const isTracing = focusId !== null
    const isLoading = status === 'loading'

    // Start trace
    const startTrace = useCallback(async (nodeId: string) => {
        if (!provider) return

        const traceResult = await fetchTrace(nodeId, provider, urnResolver)

        if (traceResult && onTraceComplete) {
            onTraceComplete(traceResult)
        }
    }, [provider, urnResolver, fetchTrace, onTraceComplete])

    // Re-trace with current focus and updated config
    const retrace = useCallback(async () => {
        if (!focusId || !provider) return
        await startTrace(focusId)
    }, [focusId, provider, startTrace])

    // Preset: Trace upstream only (root cause analysis) — deep walk so
    // multi-hop pipelines reach origin systems without truncation.
    const traceUpstream = useCallback(async (nodeId: string) => {
        setConfig({ upstreamDepth: 50, downstreamDepth: 0 })
        setShowUpstream(true)
        setShowDownstream(false)
        await startTrace(nodeId)
    }, [setConfig, setShowUpstream, setShowDownstream, startTrace])

    // Preset: Trace downstream only (impact analysis) — same deep-walk depth.
    const traceDownstream = useCallback(async (nodeId: string) => {
        setConfig({ upstreamDepth: 0, downstreamDepth: 50 })
        setShowUpstream(false)
        setShowDownstream(true)
        await startTrace(nodeId)
    }, [setConfig, setShowUpstream, setShowDownstream, startTrace])

    // Preset: Full trace (both directions). 25+25 covers typical end-to-end
    // lineage chains (source → staging → refined → mart → reporting +
    // intermediate transforms) without explosively expanding the graph.
    const traceFullLineage = useCallback(async (nodeId: string) => {
        setConfig({ upstreamDepth: 25, downstreamDepth: 25 })
        setShowUpstream(true)
        setShowDownstream(true)
        await startTrace(nodeId)
    }, [setConfig, setShowUpstream, setShowDownstream, startTrace])

    // Toggle trace
    const toggleTrace = useCallback(async (nodeId: string) => {
        if (focusId === nodeId) {
            clearTrace()
        } else {
            await startTrace(nodeId)
        }
    }, [focusId, clearTrace, startTrace])

    // Jump back to a previous focus from the history breadcrumb. Restores the
    // entry's config snapshot first so the same trace shape is reproduced,
    // then runs the trace. The fetchTrace handler will see the duplicate
    // focus check and re-use the existing top-of-history entry rather than
    // pushing a new one.
    const jumpToHistoryEntry = useCallback(async (entry: TraceHistoryEntry) => {
        setConfig(entry.config)
        await startTrace(entry.focusId)
    }, [setConfig, startTrace])

    // Drill into an AGGREGATED edge — provider-bound wrapper
    const expandAggregatedEdge = useCallback(async (
        sourceUrn: string, targetUrn: string, currentLevel: number,
    ): Promise<TraceV2Result | null> => {
        if (!provider) return null
        return expandAggregatedEdgeAction(sourceUrn, targetUrn, currentLevel, provider)
    }, [provider, expandAggregatedEdgeAction])

    // Check functions - support both node ID and URN matching
    const isInTrace = useCallback((nodeId: string) => {
        if (!result) return false
        // Check direct match
        if (result.traceNodes.has(nodeId)) return true
        // Check via canvas node URN
        const node = canvasNodes.find(n => n.id === nodeId)
        if (node?.data?.urn && result.traceNodes.has(node.data.urn)) return true
        return false
    }, [result, canvasNodes])

    const isUpstream = useCallback((nodeId: string) => {
        if (!result) return false
        if (result.upstreamNodes.has(nodeId)) return true
        const node = canvasNodes.find(n => n.id === nodeId)
        if (node?.data?.urn && result.upstreamNodes.has(node.data.urn)) return true
        return false
    }, [result, canvasNodes])

    const isDownstream = useCallback((nodeId: string) => {
        if (!result) return false
        if (result.downstreamNodes.has(nodeId)) return true
        const node = canvasNodes.find(n => n.id === nodeId)
        if (node?.data?.urn && result.downstreamNodes.has(node.data.urn)) return true
        return false
    }, [result, canvasNodes])

    const isFocus = useCallback((nodeId: string) => {
        if (focusId === nodeId) return true
        // Also check URN match
        const node = canvasNodes.find(n => n.id === nodeId)
        if (node?.data?.urn && focusId === node.data.urn) return true
        return false
    }, [focusId, canvasNodes])

    // Visible trace nodes (filtered by direction)
    const visibleTraceNodes = useMemo(() => {
        if (!result) return new Set<string>()

        const visible = new Set<string>()

        // Always include focus
        if (focusId) visible.add(focusId)

        result.traceNodes.forEach(nodeId => {
            const isUp = result.upstreamNodes.has(nodeId)
            const isDown = result.downstreamNodes.has(nodeId)
            const isFocusNode = nodeId === focusId

            // Include if focus, or if direction is enabled
            if (isFocusNode) {
                visible.add(nodeId)
            } else if (isUp && showUpstream) {
                visible.add(nodeId)
            } else if (isDown && showDownstream) {
                visible.add(nodeId)
            } else if (!isUp && !isDown) {
                // Nodes that are neither upstream nor downstream but in trace
                // (e.g., intermediate nodes) - show if either direction is on
                if (showUpstream || showDownstream) {
                    visible.add(nodeId)
                }
            }
        })

        return visible
    }, [result, focusId, showUpstream, showDownstream])

    // Trace context set (includes ancestors for proper highlighting)
    const traceContextSet = useMemo(() => {
        // For now, same as visible trace nodes
        // Could be extended to include ancestors for container highlighting
        return visibleTraceNodes
    }, [visibleTraceNodes])

    // Counts
    const upstreamCount = result?.upstreamNodes.size ?? 0
    const downstreamCount = result?.downstreamNodes.size ?? 0

    // Full statistics
    const statistics: TraceStatistics = useMemo(() => {
        if (!result?.lineageResult) {
            return {
                totalNodes: 0,
                upstreamCount: 0,
                downstreamCount: 0,
                totalEdges: 0,
                edgeTypes: [],
                isInherited: false,
            }
        }

        const lineageResult = result.lineageResult
        const edgeTypeSet = new Set<string>()
        lineageResult.edges.forEach(e => edgeTypeSet.add(e.edgeType))
        // Drill-down edges count toward stats too
        drilldowns.forEach(d => d.edges.forEach(e => edgeTypeSet.add(e.edgeType)))

        // Prefer v2 first-class fields; fall back to legacy aggregatedEdges sentinel.
        const isInherited = result.isInherited
            ?? !!lineageResult.aggregatedEdges?.['_inheritedFrom']
        const inheritedFrom = result.inheritedFromUrn
            ?? (lineageResult.aggregatedEdges?.['_inheritedFrom'] as string | undefined)

        return {
            totalNodes: result.traceNodes.size,
            upstreamCount,
            downstreamCount,
            totalEdges: result.traceEdges.size,
            edgeTypes: Array.from(edgeTypeSet),
            isInherited,
            inheritedFrom,
        }
    }, [result, upstreamCount, downstreamCount, drilldowns])

    return {
        status,
        error,
        focusId,
        result,
        isTracing,
        isLoading,
        config,
        setConfig,
        showUpstream,
        showDownstream,
        setShowUpstream,
        setShowDownstream,
        startTrace,
        toggleTrace,
        clearTrace,
        retrace,
        traceUpstream,
        traceDownstream,
        traceFullLineage,
        isInTrace,
        isUpstream,
        isDownstream,
        isFocus,
        visibleTraceNodes,
        traceContextSet,
        upstreamCount,
        downstreamCount,
        statistics,
        drilldowns,
        expandAggregatedEdge,
        collapseDrilldown,
        traceHistory,
        jumpToHistoryEntry,
        clearTraceHistory,
    }
}

// ============================================
// Utility Selectors
// ============================================

export const useTraceConfig = () => useTraceStore(s => s.config)
export const useTraceFocusId = () => useTraceStore(s => s.focusId)
export const useTraceStatus = () => useTraceStore(s => s.status)
export const useIsTracing = () => useTraceStore(s => s.focusId !== null)

