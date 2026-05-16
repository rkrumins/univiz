/**
 * useLineageStubs - Per-node lineage virtualization state machine.
 *
 * Sibling to useAggregatedLineage but keyed per (urn, direction) rather than
 * per (sourceUrns[], targetUrns[]). Backs the lineage-stub render mode where
 * every node with lineage shows a small dashed-gradient stub; hover materializes
 * the real edges; click pins them; mouseout collapses back (cache retained).
 *
 * Shared with useAggregatedLineage:
 *   - FNV-1a hashing for cache keys (hooks/lib/lineageCache)
 *   - LRU cache pattern (hooks/lib/lineageCache.LruCache)
 *   - collapsed/loading/expanded state machine semantics
 *
 * Stub-specific additions:
 *   - 'pinned' state (persists across mouseout)
 *   - per-(urn, direction) keying
 *   - 500-entry cache (vs 200) and 15 min TTL (vs 5) — per-node grain is finer
 *     and lineage rarely churns, so a larger / longer-lived cache pays back.
 *
 * Render layer integration is deferred — this hook only manages state and the
 * fetch pipeline. The consumer (ContextViewCanvas via LineageFlowOverlay, in
 * a follow-up PR) reads state per visible node and emits the appropriate SVG.
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import { useGraphProvider } from '@/providers/GraphProviderContext'
import { LruCache, fnv1a64 } from './lib/lineageCache'
import type { GraphEdge } from '@/providers/GraphDataProvider'

// ============================================
// Types
// ============================================

export type StubDirection = 'in' | 'out'

export type StubState = 'collapsed' | 'loading' | 'expanded' | 'pinned'

export interface LineageStubEntry {
    /** The node this stub belongs to. */
    urn: string
    /** Direction of lineage this stub represents. */
    direction: StubDirection
    /** Current interaction state. */
    state: StubState
    /** Count of real edges this stub represents (null until known). */
    count: number | null
    /** Distinct edge types represented. */
    edgeTypes: string[]
    /** Real edges, populated when expanded/pinned. */
    realEdges: GraphEdge[] | null
    /** Wall-clock of last successful fetch, ms. */
    fetchedAt: number | null
    /** Last error message, if any. */
    error: string | null
}

export interface UseLineageStubsOptions {
    /** LRU max entries. Default 500 — per-node grain warrants a larger budget. */
    cacheMaxEntries?: number
    /** Cache TTL in ms. Default 15 minutes — lineage rarely changes. */
    cacheTtlMs?: number
    /** Hover-enter debounce before fetch fires. Default 150ms. */
    hoverDebounceMs?: number
}

export interface UseLineageStubsResult {
    /** Map keyed `${urn}|${direction}` to current entry. */
    entries: Map<string, LineageStubEntry>

    /** True if any stub is currently fetching. */
    isLoading: boolean

    /** Get the state for a specific (urn, direction). */
    getEntry: (urn: string, direction: StubDirection) => LineageStubEntry | null

    /** Set the known count for a stub without fetching real edges. */
    setKnownCount: (urn: string, direction: StubDirection, count: number, edgeTypes?: string[]) => void

    /**
     * Schedule a hover-driven reveal. Returns the entry id (cacheKey) so the
     * caller can `cancelHover(id)` if the cursor exits before the debounce fires.
     */
    hoverEnter: (urn: string, direction: StubDirection) => string

    /** Cancel a pending hover-enter debounce (mouseout before 150ms elapsed). */
    cancelHover: (cacheKey: string) => void

    /** Begin the mouseout grace period; collapses to 'collapsed' after graceMs. */
    hoverLeave: (urn: string, direction: StubDirection, graceMs?: number) => void

    /** Pin a stub — persists past mouseout. */
    pin: (urn: string, direction: StubDirection) => Promise<void>

    /** Unpin (or unhover) a stub — return to collapsed. Real edges stay cached. */
    collapse: (urn: string, direction: StubDirection) => void

    /** Clear every cached stub. Use sparingly. */
    clearAll: () => void
}

// ============================================
// Module-level cache (survives hook unmount within session)
// ============================================

interface StubCacheValue {
    count: number
    edgeTypes: string[]
    realEdges: GraphEdge[]
}

const stubCache = new LruCache<StubCacheValue>({
    maxEntries: 500,
    ttlMs: 15 * 60 * 1000,
})

function cacheKey(urn: string, direction: StubDirection): string {
    // Short FNV-1a hash keeps the key compact regardless of URN length.
    return `${direction}:${fnv1a64(urn)}`
}

// ============================================
// Hook
// ============================================

export function useLineageStubs(options: UseLineageStubsOptions = {}): UseLineageStubsResult {
    const {
        hoverDebounceMs = 150,
    } = options

    const provider = useGraphProvider()

    const [entries, setEntries] = useState<Map<string, LineageStubEntry>>(new Map())
    const [loadingCount, setLoadingCount] = useState(0)

    // Per-key hover debounce timers. Cancelled on mouseout-before-fire.
    const hoverTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
    // Per-key mouseout grace timers. Cancelled on re-hover.
    const leaveTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
    // Per-key abort controllers — let mouseout cancel an in-flight fetch.
    const abortersRef = useRef<Map<string, AbortController>>(new Map())

    const upsert = useCallback((key: string, patch: Partial<LineageStubEntry>) => {
        setEntries(prev => {
            const next = new Map(prev)
            const existing = next.get(key)
            if (existing) {
                next.set(key, { ...existing, ...patch })
            } else {
                // patch must contain urn + direction for new entries — guarded
                // by the calling sites (getEntry/hoverEnter/pin/setKnownCount).
                next.set(key, {
                    urn: patch.urn ?? '',
                    direction: patch.direction ?? 'in',
                    state: patch.state ?? 'collapsed',
                    count: patch.count ?? null,
                    edgeTypes: patch.edgeTypes ?? [],
                    realEdges: patch.realEdges ?? null,
                    fetchedAt: patch.fetchedAt ?? null,
                    error: patch.error ?? null,
                })
            }
            return next
        })
    }, [])

    const fetchEdges = useCallback(async (urn: string, direction: StubDirection): Promise<void> => {
        if (!provider) return
        const key = cacheKey(urn, direction)

        // Cache hit fast path
        const cached = stubCache.get(key)
        if (cached) {
            upsert(key, {
                urn,
                direction,
                state: 'expanded',
                count: cached.count,
                edgeTypes: cached.edgeTypes,
                realEdges: cached.realEdges,
                fetchedAt: Date.now(),
                error: null,
            })
            return
        }

        // Cancel any in-flight fetch for this key (e.g. rapid hover-in / hover-out / hover-in).
        const prior = abortersRef.current.get(key)
        if (prior) prior.abort()
        const controller = new AbortController()
        abortersRef.current.set(key, controller)

        upsert(key, { urn, direction, state: 'loading', error: null })
        setLoadingCount(c => c + 1)

        try {
            const edges = await provider.getEdges(
                direction === 'in'
                    ? { targetUrns: [urn] }
                    : { sourceUrns: [urn] }
            )

            // Aborted mid-flight (user moved on) — drop the result silently.
            if (controller.signal.aborted) return

            const types = new Set<string>()
            for (const e of edges) types.add(e.edgeType)
            const edgeTypes = Array.from(types)

            stubCache.set(key, { count: edges.length, edgeTypes, realEdges: edges })

            upsert(key, {
                urn,
                direction,
                state: 'expanded',
                count: edges.length,
                edgeTypes,
                realEdges: edges,
                fetchedAt: Date.now(),
                error: null,
            })
        } catch (err: unknown) {
            if (controller.signal.aborted) return
            const message = err instanceof Error ? err.message : 'Failed to fetch lineage'
            upsert(key, { urn, direction, state: 'collapsed', error: message })
        } finally {
            if (abortersRef.current.get(key) === controller) {
                abortersRef.current.delete(key)
            }
            setLoadingCount(c => Math.max(0, c - 1))
        }
    }, [provider, upsert])

    const getEntry = useCallback((urn: string, direction: StubDirection): LineageStubEntry | null => {
        return entries.get(cacheKey(urn, direction)) ?? null
    }, [entries])

    const setKnownCount = useCallback((urn: string, direction: StubDirection, count: number, edgeTypes: string[] = []) => {
        const key = cacheKey(urn, direction)
        upsert(key, { urn, direction, count, edgeTypes })
    }, [upsert])

    const hoverEnter = useCallback((urn: string, direction: StubDirection): string => {
        const key = cacheKey(urn, direction)

        // Cancel an in-flight mouseout grace — user came back before it fired.
        const leave = leaveTimersRef.current.get(key)
        if (leave) {
            clearTimeout(leave)
            leaveTimersRef.current.delete(key)
        }

        // If already expanded/pinned/loading, no work needed.
        const current = entries.get(key)
        if (current && (current.state === 'expanded' || current.state === 'pinned' || current.state === 'loading')) {
            return key
        }

        // Schedule fetch after debounce — gives hover-through traversal time
        // to not trigger unintended fetches.
        const existing = hoverTimersRef.current.get(key)
        if (existing) clearTimeout(existing)
        const timer = setTimeout(() => {
            hoverTimersRef.current.delete(key)
            void fetchEdges(urn, direction)
        }, hoverDebounceMs)
        hoverTimersRef.current.set(key, timer)
        return key
    }, [entries, fetchEdges, hoverDebounceMs])

    const cancelHover = useCallback((key: string) => {
        const timer = hoverTimersRef.current.get(key)
        if (timer) {
            clearTimeout(timer)
            hoverTimersRef.current.delete(key)
        }
    }, [])

    const hoverLeave = useCallback((urn: string, direction: StubDirection, graceMs: number = 300) => {
        const key = cacheKey(urn, direction)

        // If the hover-enter debounce hasn't fired yet, cancel it — no fetch
        // ever needs to happen.
        const enter = hoverTimersRef.current.get(key)
        if (enter) {
            clearTimeout(enter)
            hoverTimersRef.current.delete(key)
        }

        // If pinned, leave alone — pin survives mouseout by definition.
        const current = entries.get(key)
        if (!current || current.state === 'pinned' || current.state === 'collapsed') return

        // Schedule collapse after grace period — gives the user time to move
        // cursor over revealed edges / target nodes without losing the reveal.
        const existing = leaveTimersRef.current.get(key)
        if (existing) clearTimeout(existing)
        const timer = setTimeout(() => {
            leaveTimersRef.current.delete(key)
            // Don't abort an in-flight fetch on collapse — let it complete
            // and warm the cache. State just goes back to 'collapsed' so the
            // stub re-renders as a stub. Real edges remain cached for re-hover.
            upsert(key, { state: 'collapsed' })
        }, graceMs)
        leaveTimersRef.current.set(key, timer)
    }, [entries, upsert])

    const pin = useCallback(async (urn: string, direction: StubDirection): Promise<void> => {
        const key = cacheKey(urn, direction)

        // Cancel pending hover timers — pin is an explicit commit.
        const enter = hoverTimersRef.current.get(key)
        if (enter) { clearTimeout(enter); hoverTimersRef.current.delete(key) }
        const leave = leaveTimersRef.current.get(key)
        if (leave) { clearTimeout(leave); leaveTimersRef.current.delete(key) }

        const current = entries.get(key)
        if (current?.state === 'expanded') {
            // Already loaded — just promote to pinned.
            upsert(key, { state: 'pinned' })
            return
        }

        // Cold pin — fetch first, then mark pinned. We mark loading immediately
        // so the UI can show feedback while the fetch runs.
        upsert(key, { urn, direction, state: 'loading' })
        await fetchEdges(urn, direction)
        // fetchEdges set state to 'expanded' on success; promote to 'pinned'.
        // On error it set to 'collapsed' — leave it there so the user can retry.
        setEntries(prev => {
            const existing = prev.get(key)
            if (!existing || existing.state !== 'expanded') return prev
            const next = new Map(prev)
            next.set(key, { ...existing, state: 'pinned' })
            return next
        })
    }, [entries, fetchEdges, upsert])

    const collapse = useCallback((urn: string, direction: StubDirection) => {
        const key = cacheKey(urn, direction)
        const enter = hoverTimersRef.current.get(key)
        if (enter) { clearTimeout(enter); hoverTimersRef.current.delete(key) }
        const leave = leaveTimersRef.current.get(key)
        if (leave) { clearTimeout(leave); leaveTimersRef.current.delete(key) }
        upsert(key, { state: 'collapsed' })
    }, [upsert])

    const clearAll = useCallback(() => {
        // Cancel every timer + aborter, then drop entries. Cache persists —
        // call stubCache.clear() externally if you need a hard wipe.
        for (const t of hoverTimersRef.current.values()) clearTimeout(t)
        hoverTimersRef.current.clear()
        for (const t of leaveTimersRef.current.values()) clearTimeout(t)
        leaveTimersRef.current.clear()
        for (const a of abortersRef.current.values()) a.abort()
        abortersRef.current.clear()
        setEntries(new Map())
    }, [])

    // Cleanup on unmount.
    useEffect(() => {
        return () => {
            for (const t of hoverTimersRef.current.values()) clearTimeout(t)
            for (const t of leaveTimersRef.current.values()) clearTimeout(t)
            for (const a of abortersRef.current.values()) a.abort()
        }
    }, [])

    return {
        entries,
        isLoading: loadingCount > 0,
        getEntry,
        setKnownCount,
        hoverEnter,
        cancelHover,
        hoverLeave,
        pin,
        collapse,
        clearAll,
    }
}
