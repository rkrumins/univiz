/**
 * useAggregatedLineage - Progressive edge disclosure hook
 * 
 * Manages aggregated lineage edges that show summarized connections
 * between containers (e.g., datasets, systems). Supports expanding
 * aggregated edges to reveal detailed connections on demand.
 */

import { useState, useCallback, useMemo, useRef, useEffect } from 'react'
import { useGraphProvider } from '@/providers/GraphProviderContext'
import { mapWithConcurrency } from '@/lib/concurrency'
import type {
    AggregatedEdgeInfo,
    AggregatedEdgeResult,
    GraphEdge
} from '@/providers/GraphDataProvider'

// ============================================
// Types
// ============================================

export type ExpansionState = 'collapsed' | 'expanded' | 'loading'

export interface AggregatedEdgeState {
    /** The aggregated edge info from backend */
    aggregated: AggregatedEdgeInfo
    /** Current expansion state */
    state: ExpansionState
    /** Detailed edges (populated when expanded) */
    detailedEdges: GraphEdge[]
}

export interface UseAggregatedLineageOptions {
    /**
     * Entity type ID to aggregate lineage to (e.g. "dataset", "term").
     * null = no aggregation, show all fine-grained edges.
     */
    granularity?: string | null
    /** Whether to automatically fetch aggregated edges */
    autoFetch?: boolean
    /** Cache TTL in milliseconds (default: 5 minutes) */
    cacheTtl?: number
}

export interface UseAggregatedLineageResult {
    /** Map of aggregated edge ID to its state */
    aggregatedEdges: Map<string, AggregatedEdgeState>

    /** Whether any aggregation request is loading */
    isLoading: boolean

    /** Last error encountered */
    error: string | null

    /**
     * Current granularity: entity type ID string, or null (no aggregation).
     */
    granularity: string | null

    /** Fetch aggregated edges for given source URNs */
    fetchAggregated: (sourceUrns: string[], targetUrns?: string[]) => Promise<void>

    /** Expand an aggregated edge to show detailed edges */
    expandEdge: (aggregatedEdgeId: string) => Promise<void>

    /** Collapse an expanded edge back to aggregated state */
    collapseEdge: (aggregatedEdgeId: string) => void

    /** Toggle expansion state of an edge */
    toggleEdge: (aggregatedEdgeId: string) => Promise<void>

    /** Check if an edge is expanded */
    isExpanded: (aggregatedEdgeId: string) => boolean

    /** Get all visible edges (both aggregated and detailed) */
    getVisibleEdges: () => Array<GraphEdge | AggregatedEdgeInfo>

    /** Change granularity (entity type ID string, or null for no aggregation) */
    setGranularity: (granularity: string | null) => void

    /** Clear all cached data */
    clearCache: () => void

    /**
     * Drop every aggregated-edge entry whose `sourceUrn` or `targetUrn` is
     * in the supplied URN set. Used on subtree collapse so stale child-level
     * aggregated edges disappear synchronously instead of waiting for the
     * 500 ms debounced refetch.
     */
    purgeEdgesIncidentToUrns: (urns: Iterable<string>) => void

    /** Get edge count for a specific aggregated edge */
    getEdgeCount: (aggregatedEdgeId: string) => number

    /** Get edge types summary for an aggregated edge */
    getEdgeTypes: (aggregatedEdgeId: string) => string[]

    /** True when the backend capped the aggregated-edge result set. */
    truncated: boolean

    /**
     * ISO-8601 timestamp of the last AGGREGATED materialisation, or null if
     * the projection has never been computed for this data source.
     */
    lastMaterializedAt: string | null

    /**
     * True when this response triggered a fire-and-forget materialise on
     * the backend — the canvas should re-poll shortly to pick up edges.
     */
    materializationTriggered: boolean
}

// ============================================
// Cache for aggregated edge results
// ============================================

interface CacheEntry {
    result: AggregatedEdgeResult
    timestamp: number
    sourceUrns: string[]
    targetUrns?: string[]
    granularity: string
}

const aggregatedEdgeCache = new Map<string, CacheEntry>()
const CACHE_MAX_ENTRIES = 200
const AGGREGATED_FETCH_BATCH_SIZE = 500

/**
 * Cap on parallel `/edges/aggregated` chunks. Aggregation is the
 * single most expensive endpoint — letting a 100k-URN canvas fire all
 * 200 chunks at once is a reliable way to saturate FalkorDB's single
 * Cypher thread. 4 concurrent chunks is the sweet spot per Phase 0 load
 * tests; tune via VITE_AGGREGATED_FETCH_CONCURRENCY.
 */
const AGGREGATED_FETCH_CONCURRENCY = (() => {
    const fromEnv = Number(import.meta.env?.VITE_AGGREGATED_FETCH_CONCURRENCY)
    return Number.isFinite(fromEnv) && fromEnv >= 1 ? fromEnv : 4
})()

// FNV-1a 64-bit, BigInt arithmetic. Returns a hex string. Avoids holding
// multi-MB joined-URN strings in the cache key across hundreds of entries.
const FNV_OFFSET_64 = 0xcbf29ce484222325n
const FNV_PRIME_64 = 0x100000001b3n
const FNV_MASK_64 = 0xffffffffffffffffn
function fnv1a64(input: string): string {
    let hash = FNV_OFFSET_64
    for (let i = 0; i < input.length; i++) {
        hash ^= BigInt(input.charCodeAt(i))
        hash = (hash * FNV_PRIME_64) & FNV_MASK_64
    }
    return hash.toString(16)
}

function getCacheKey(sourceUrns: string[], targetUrns: string[] | undefined, granularity: string): string {
    const srcHash = fnv1a64([...sourceUrns].sort().join(''))
    const tgtHash = targetUrns ? fnv1a64([...targetUrns].sort().join('')) : '0'
    return `${granularity}:${srcHash}:${tgtHash}`
}

// ============================================
// Hook Implementation
// ============================================

export function useAggregatedLineage(options: UseAggregatedLineageOptions = {}): UseAggregatedLineageResult {
    const {
        granularity: initialGranularity = null,
        cacheTtl = 5 * 60 * 1000, // 5 minutes
    } = options

    const provider = useGraphProvider()

    // State
    const [aggregatedEdges, setAggregatedEdges] = useState<Map<string, AggregatedEdgeState>>(new Map())
    const [isLoading, setIsLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [granularity, setGranularity] = useState(initialGranularity)
    const [truncated, setTruncated] = useState(false)
    const [lastMaterializedAt, setLastMaterializedAt] = useState<string | null>(null)
    const [materializationTriggered, setMaterializationTriggered] = useState(false)

    // Track current source URNs for refetch on granularity change
    const currentSourceUrnsRef = useRef<string[]>([])
    const currentTargetUrnsRef = useRef<string[] | undefined>(undefined)

    // Fetch aggregated edges from backend
    const fetchAggregated = useCallback(async (sourceUrns: string[], targetUrns?: string[]) => {
        if (!provider || sourceUrns.length === 0) return

        // Check cache first
        const cacheKey = getCacheKey(sourceUrns, targetUrns, granularity)
        const cached = aggregatedEdgeCache.get(cacheKey)

        if (cached && (Date.now() - cached.timestamp) < cacheTtl) {
            // Use cached result with functional update to avoid dependency on aggregatedEdges
            setAggregatedEdges(prev => {
                const edgeMap = new Map<string, AggregatedEdgeState>()
                for (const agg of cached.result.aggregatedEdges) {
                    const existing = prev.get(agg.id)
                    edgeMap.set(agg.id, {
                        aggregated: agg,
                        state: existing?.state ?? 'collapsed',
                        detailedEdges: existing?.detailedEdges ?? [],
                    })
                }
                return edgeMap
            })
            setTruncated(cached.result.truncated ?? false)
            setLastMaterializedAt(cached.result.lastMaterializedAt ?? null)
            setMaterializationTriggered(cached.result.materializationTriggered ?? false)
            return
        }

        setIsLoading(true)
        setError(null)

        try {
            // Chunk source URNs above the per-request budget so a 100k-node
            // canvas doesn't hand the backend a single 100k-URN payload.
            const chunks: string[][] = []
            if (sourceUrns.length > AGGREGATED_FETCH_BATCH_SIZE) {
                for (let i = 0; i < sourceUrns.length; i += AGGREGATED_FETCH_BATCH_SIZE) {
                    chunks.push(sourceUrns.slice(i, i + AGGREGATED_FETCH_BATCH_SIZE))
                }
            } else {
                chunks.push(sourceUrns)
            }

            // Bound parallel chunks so a 200-chunk fan-out doesn't blow
            // up the FalkorDB Cypher thread (single-threaded — every
            // chunk competes for the same slot).
            const settled = await mapWithConcurrency(
                chunks,
                AGGREGATED_FETCH_CONCURRENCY,
                (chunk) => provider.getAggregatedEdges({
                    sourceUrns: chunk,
                    targetUrns,
                    granularity,
                }),
            )

            const fulfilled = settled
                .filter((s): s is PromiseFulfilledResult<AggregatedEdgeResult> => s.status === 'fulfilled')
                .map(s => s.value)
            const rejected = settled.filter(s => s.status === 'rejected') as PromiseRejectedResult[]

            // Dedupe-merge by agg.id; later chunks with same id win (last-write).
            const mergedEdgesById = new Map<string, AggregatedEdgeInfo>()
            let mergedTotalSourceEdges = 0
            let mergedTruncated = false
            let mergedLastMaterializedAt: string | null | undefined = undefined
            let mergedMaterializationTriggered = false
            for (const r of fulfilled) {
                for (const agg of r.aggregatedEdges) mergedEdgesById.set(agg.id, agg)
                mergedTotalSourceEdges += r.totalSourceEdges ?? 0
                if (r.truncated) mergedTruncated = true
                if (r.materializationTriggered) mergedMaterializationTriggered = true
                if (r.lastMaterializedAt !== undefined) {
                    if (mergedLastMaterializedAt === undefined || mergedLastMaterializedAt === null) {
                        mergedLastMaterializedAt = r.lastMaterializedAt
                    } else if (r.lastMaterializedAt && r.lastMaterializedAt < mergedLastMaterializedAt) {
                        mergedLastMaterializedAt = r.lastMaterializedAt
                    }
                }
            }
            const mergedResult: AggregatedEdgeResult = {
                aggregatedEdges: Array.from(mergedEdgesById.values()),
                totalSourceEdges: mergedTotalSourceEdges,
                truncated: mergedTruncated,
                lastMaterializedAt: mergedLastMaterializedAt ?? null,
                materializationTriggered: mergedMaterializationTriggered,
            }

            // Cache the merged result (LRU eviction when full)
            if (aggregatedEdgeCache.size >= CACHE_MAX_ENTRIES) {
                const oldestKey = aggregatedEdgeCache.keys().next().value
                if (oldestKey !== undefined) aggregatedEdgeCache.delete(oldestKey)
            }
            aggregatedEdgeCache.set(cacheKey, {
                result: mergedResult,
                timestamp: Date.now(),
                sourceUrns,
                targetUrns,
                granularity,
            })

            // Update state with functional update to avoid dependency on aggregatedEdges
            setAggregatedEdges(prev => {
                const edgeMap = new Map<string, AggregatedEdgeState>()
                for (const agg of mergedResult.aggregatedEdges) {
                    const existing = prev.get(agg.id)
                    edgeMap.set(agg.id, {
                        aggregated: agg,
                        state: existing?.state ?? 'collapsed',
                        detailedEdges: existing?.detailedEdges ?? [],
                    })
                }
                return edgeMap
            })

            setTruncated(mergedResult.truncated ?? false)
            setLastMaterializedAt(mergedResult.lastMaterializedAt ?? null)
            setMaterializationTriggered(mergedResult.materializationTriggered ?? false)

            // Partial-success: surface the failure but keep applied chunks.
            if (rejected.length > 0) {
                const firstErr = rejected[0].reason
                setError(firstErr instanceof Error ? firstErr.message : 'Failed to fetch some aggregated edges')
            }

            // Track for refetch
            currentSourceUrnsRef.current = sourceUrns
            currentTargetUrnsRef.current = targetUrns

        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to fetch aggregated edges')
        } finally {
            setIsLoading(false)
        }
    }, [provider, granularity, cacheTtl])

    // Expand an aggregated edge to show detailed edges
    const expandEdge = useCallback(async (aggregatedEdgeId: string) => {
        const edgeState = aggregatedEdges.get(aggregatedEdgeId)
        if (!edgeState || !provider) return

        // Already expanded or loading
        if (edgeState.state === 'expanded' || edgeState.state === 'loading') return

        // Update state to loading
        setAggregatedEdges(prev => {
            const next = new Map(prev)
            const current = next.get(aggregatedEdgeId)
            if (current) {
                next.set(aggregatedEdgeId, { ...current, state: 'loading' })
            }
            return next
        })

        try {
            // Fetch detailed edges strictly between source and target
            // We optimized the backend to handle sourceUrns + targetUrns efficiently.
            const edges = await provider.getEdges({
                sourceUrns: [edgeState.aggregated.sourceUrn],
                targetUrns: [edgeState.aggregated.targetUrn],
            })

            // No need to filter extensively client-side if backend does its job,
            // but we keep a sanity check just in case.
            const relevantEdges = edges

            setAggregatedEdges(prev => {
                const next = new Map(prev)
                const current = next.get(aggregatedEdgeId)
                if (current) {
                    next.set(aggregatedEdgeId, {
                        ...current,
                        state: 'expanded',
                        detailedEdges: relevantEdges,
                    })
                }
                return next
            })
        } catch (err) {
            // Revert to collapsed on error
            setAggregatedEdges(prev => {
                const next = new Map(prev)
                const current = next.get(aggregatedEdgeId)
                if (current) {
                    next.set(aggregatedEdgeId, { ...current, state: 'collapsed' })
                }
                return next
            })
            setError(err instanceof Error ? err.message : 'Failed to expand edge')
        }
    }, [aggregatedEdges, provider])

    // Collapse an expanded edge
    const collapseEdge = useCallback((aggregatedEdgeId: string) => {
        setAggregatedEdges(prev => {
            const next = new Map(prev)
            const current = next.get(aggregatedEdgeId)
            if (current) {
                next.set(aggregatedEdgeId, {
                    ...current,
                    state: 'collapsed',
                    // Keep detailed edges cached for quick re-expand
                })
            }
            return next
        })
    }, [])

    // Toggle expansion
    const toggleEdge = useCallback(async (aggregatedEdgeId: string) => {
        const edgeState = aggregatedEdges.get(aggregatedEdgeId)
        if (!edgeState) return

        if (edgeState.state === 'expanded') {
            collapseEdge(aggregatedEdgeId)
        } else if (edgeState.state === 'collapsed') {
            await expandEdge(aggregatedEdgeId)
        }
    }, [aggregatedEdges, expandEdge, collapseEdge])

    // Check if expanded
    const isExpanded = useCallback((aggregatedEdgeId: string) => {
        return aggregatedEdges.get(aggregatedEdgeId)?.state === 'expanded'
    }, [aggregatedEdges])

    // Get all visible edges
    const getVisibleEdges = useCallback(() => {
        const visible: Array<GraphEdge | AggregatedEdgeInfo> = []

        for (const [, edgeState] of aggregatedEdges) {
            if (edgeState.state === 'expanded' && edgeState.detailedEdges.length > 0) {
                // Show detailed edges when expanded
                visible.push(...edgeState.detailedEdges)
            } else {
                // Show aggregated edge when collapsed
                visible.push(edgeState.aggregated)
            }
        }

        return visible
    }, [aggregatedEdges])

    // Change granularity and refetch
    const handleSetGranularity = useCallback((newGranularity: string | null) => {
        if (newGranularity === granularity) return

        setGranularity(newGranularity)

        // Refetch with new granularity if we have current sources
        if (currentSourceUrnsRef.current.length > 0) {
            // Clear cache for new granularity
            aggregatedEdgeCache.clear()
            fetchAggregated(currentSourceUrnsRef.current, currentTargetUrnsRef.current)
        }
    }, [granularity, fetchAggregated])

    // Clear cache
    const clearCache = useCallback(() => {
        aggregatedEdgeCache.clear()
        setAggregatedEdges(new Map())
        currentSourceUrnsRef.current = []
        currentTargetUrnsRef.current = undefined
        setTruncated(false)
        setLastMaterializedAt(null)
        setMaterializationTriggered(false)
    }, [])

    // Synchronous purge: drop entries incident to the supplied URN set.
    // Caller is the collapse path in ContextViewCanvas — it computes the
    // collapsed subtree's URNs and asks us to drop their aggregated edges
    // immediately, avoiding the 500 ms debounce flicker.
    const purgeEdgesIncidentToUrns = useCallback((urns: Iterable<string>) => {
        const urnSet = urns instanceof Set ? urns : new Set(urns)
        if (urnSet.size === 0) return
        setAggregatedEdges(prev => {
            let removed = 0
            const next = new Map(prev)
            for (const [id, entry] of prev) {
                if (urnSet.has(entry.aggregated.sourceUrn) || urnSet.has(entry.aggregated.targetUrn)) {
                    next.delete(id)
                    removed++
                }
            }
            return removed > 0 ? next : prev
        })
    }, [])

    // Get edge count
    const getEdgeCount = useCallback((aggregatedEdgeId: string) => {
        return aggregatedEdges.get(aggregatedEdgeId)?.aggregated.edgeCount ?? 0
    }, [aggregatedEdges])

    // Get edge types
    const getEdgeTypes = useCallback((aggregatedEdgeId: string) => {
        return aggregatedEdges.get(aggregatedEdgeId)?.aggregated.edgeTypes ?? []
    }, [aggregatedEdges])

    return {
        aggregatedEdges,
        isLoading,
        error,
        granularity,
        fetchAggregated,
        expandEdge,
        collapseEdge,
        toggleEdge,
        isExpanded,
        getVisibleEdges,
        setGranularity: handleSetGranularity,
        clearCache,
        purgeEdgesIncidentToUrns,
        getEdgeCount,
        getEdgeTypes,
        truncated,
        lastMaterializedAt,
        materializationTriggered,
    }
}

// ============================================
// Utility: Convert aggregated edge to React Flow edge
// ============================================

export function aggregatedEdgeToFlowEdge(
    agg: AggregatedEdgeInfo,
    options?: {
        animated?: boolean
        strokeWidth?: number
        showLabel?: boolean
    }
): {
    id: string
    source: string
    target: string
    type: string
    animated: boolean
    style: React.CSSProperties
    data: Record<string, unknown>
    label?: string
} {
    const { animated = true, strokeWidth = 2, showLabel = true } = options ?? {}

    // Scale stroke width based on edge count
    const scaledStrokeWidth = Math.min(strokeWidth + Math.log2(agg.edgeCount), 8)

    return {
        id: agg.id,
        source: agg.sourceUrn,
        target: agg.targetUrn,
        type: 'aggregated',
        animated,
        style: {
            strokeWidth: scaledStrokeWidth,
            opacity: agg.confidence,
        },
        data: {
            isAggregated: true,
            edgeCount: agg.edgeCount,
            edgeTypes: agg.edgeTypes,
            confidence: agg.confidence,
            sourceEdgeIds: agg.sourceEdgeIds,
        },
        label: showLabel ? `${agg.edgeCount} edges` : undefined,
    }
}

