/**
 * useLineageTrace - Zustand store for the v2 lineage trace API.
 *
 * Holds trace state separately from `useCanvasStore` (architectural
 * inversion per plan §2.1) so the canvas store stays clean of trace
 * data and `useEdgeProjection` can early-out during trace mode without
 * silently dropping aggregated edges.
 *
 * Responsibilities:
 *  - Wire the v2 endpoints (postTrace / postTraceExpand) with full
 *    AbortController plumbing — every request is keyed and any prior
 *    in-flight request for the same key is aborted before a new one
 *    issues. AbortError is silently ignored (expected on rapid toggles).
 *  - Apply expand deltas via a pure `applyDelta` helper (dedupe by
 *    urn / edge id, drop removed edges, merge aggregatedChildCount,
 *    update expandableUrns).
 *  - Track per-action loading: `pendingExpansionUrns` (per-chevron
 *    spinner), `pendingFilterRetrace` (debounced filter retrace).
 *  - Local-only collapse: `collapse(urn)` hides a sub-tree without
 *    calling the backend; selectors filter the collapsed set out of
 *    visible nodes/edges.
 *  - Debounced edge-type filter retrace (350ms).
 *
 * The store does NOT push trace data into `useCanvasStore`. Canvases
 * read trace nodes/edges via the selectors exported from this module.
 *
 * Type re-exports (TraceConfig, TraceResult, TraceStatistics,
 * UseLineageTraceResult) keep TraceToolbar's import surface stable
 * during the migration window.
 */

import { create } from 'zustand'
import { useMemo } from 'react'

import {
  postTrace,
  postTraceExpand,
  TraceApiError,
  type TraceRequest,
  type TraceResultV2,
  type TraceData,
  type TraceMeta,
  type TraceEdge,
  type TraceDelta,
  type TraceExpandRequest,
} from '@/services/traceApi'
import type { GraphNode } from '@/providers/GraphDataProvider'

// ============================================
// Re-exports — keep external imports stable
// ============================================

export type {
  TraceRequest,
  TraceResultV2,
  TraceData,
  TraceMeta,
  TraceEdge,
  TraceDelta,
  TraceExpandRequest,
} from '@/services/traceApi'
export { TraceApiError } from '@/services/traceApi'

/**
 * Alias kept for backward compatibility with TraceToolbar's existing
 * type imports. The new shape is `TraceRequest`; callers should migrate
 * incrementally.
 */
export type TraceConfig = TraceRequest

/**
 * Alias kept for backward compatibility. `TraceResultV2` is the new
 * canonical envelope.
 */
export type TraceResult = TraceResultV2

// ============================================
// Internal store types
// ============================================

export type TraceStatus = 'idle' | 'loading' | 'success' | 'error'

export interface TraceErrorState {
  code: string
  message: string
}

type AbortKey = 'trace' | 'filter' | `expand:${string}`

interface TraceSliceState {
  /** Workspace id required to issue requests. Set by `bindWorkspace`. */
  wsId: string | null

  focusUrn: string | null
  /** Last request body sent to the server (used by `retrace`). */
  request: TraceRequest | null
  /** Current trace data + meta. */
  result: TraceResultV2 | null
  /** Chronological history of expand deltas. Useful for future undo. */
  delta: TraceDelta[]

  status: TraceStatus
  error: TraceErrorState | null

  showUpstream: boolean
  showDownstream: boolean

  /** AbortControllers indexed by action key. */
  abortControllers: Map<AbortKey, AbortController>
  /** URNs whose drill-down is in flight; drives per-chevron spinner. */
  pendingExpansionUrns: Set<string>
  /** True when a debounced edge-type-filter retrace is pending. */
  pendingFilterRetrace: boolean

  /** URNs the user has locally collapsed. Filtering only — no server call. */
  locallyCollapsedUrns: Set<string>

  /** Debounce timer for `setEdgeTypeFilter`. */
  _filterDebounceTimer: ReturnType<typeof setTimeout> | null

  // ---- Actions ----

  bindWorkspace(wsId: string | null): void
  start(focusUrn: string, request: Partial<TraceRequest>): Promise<void>
  retrace(): Promise<void>
  expand(urn: string): Promise<void>
  collapse(urn: string): void
  clear(): void
  setEdgeTypeFilter(types: string[]): void
  setShowUpstream(show: boolean): void
  setShowDownstream(show: boolean): void
}

// ============================================
// Constants
// ============================================

const FILTER_DEBOUNCE_MS = 350

const DEFAULT_REQUEST: TraceRequest = {
  urn: '',
  direction: 'both',
  upstreamDepth: 5,
  downstreamDepth: 5,
  targetLevel: null,
  targetLevelMode: null,
  lineageEdgeTypes: null,
  containmentEdgeTypes: null,
  includeContainment: true,
  limit: 2000,
  cursor: null,
  fields: 'default',
}

// ============================================
// Pure helpers
// ============================================

/**
 * Apply an expand delta to a TraceResultV2 in a pure / immutable way.
 *
 * - `addedNodes` are appended; nodes already in `result` (by urn) are
 *   not duplicated. Server-supplied data wins on conflict.
 * - `removedEdges` (edge ids) are dropped from `result.edges`.
 * - `addedEdges` are appended; existing edges with the same id are
 *   replaced by the new row (server is authoritative).
 * - `aggregatedChildCount` is merged; new entries override old.
 * - `expandableUrns` becomes (existing - {expanded URN itself}) ∪
 *   `newExpandableUrns`. The expanded URN cannot drill further at the
 *   current level once already expanded.
 *
 * Returns `null` when called with `null` result (defensive — should
 * never happen because the store gates expand on a non-null result).
 */
export function applyDelta(
  result: TraceResultV2 | null,
  delta: TraceDelta,
  expandedUrn: string,
): TraceResultV2 | null {
  if (!result) return null

  const data = result.data

  // Nodes — dedupe by urn, server wins on collision.
  const nodeIndex = new Map<string, GraphNode>()
  for (const n of data.nodes) nodeIndex.set(n.urn, n)
  for (const n of delta.data.addedNodes) nodeIndex.set(n.urn, n)
  const nodes = Array.from(nodeIndex.values())

  // Edges — drop removed first, then upsert added by id.
  const removedSet = new Set(delta.data.removedEdges)
  const edgeIndex = new Map<string, TraceEdge>()
  for (const e of data.edges) {
    if (!removedSet.has(e.id)) edgeIndex.set(e.id, e)
  }
  for (const e of delta.data.addedEdges) edgeIndex.set(e.id, e)
  const edges = Array.from(edgeIndex.values())

  // Aggregated child count — merge.
  const aggregatedChildCount: Record<string, number> = {
    ...data.aggregatedChildCount,
    ...delta.data.aggregatedChildCount,
  }

  // Expandable urns — the just-expanded URN drops out; new ones added.
  const expandable = new Set(data.expandableUrns)
  expandable.delete(expandedUrn)
  for (const u of delta.data.newExpandableUrns) expandable.add(u)

  const newData: TraceData = {
    ...data,
    nodes,
    edges,
    aggregatedChildCount,
    expandableUrns: Array.from(expandable),
  }

  return {
    data: newData,
    meta: delta.meta,
  }
}

function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === 'AbortError') return true
  if (err instanceof Error && err.name === 'AbortError') return true
  return false
}

function abortKey(key: AbortKey, controllers: Map<AbortKey, AbortController>): AbortController {
  const existing = controllers.get(key)
  if (existing) {
    try {
      existing.abort()
    } catch {
      // Ignore — abort on an already-aborted controller is a no-op.
    }
  }
  const next = new AbortController()
  controllers.set(key, next)
  return next
}

// ============================================
// Zustand store
// ============================================

export const useLineageTrace = create<TraceSliceState>((set, get) => ({
  wsId: null,
  focusUrn: null,
  request: null,
  result: null,
  delta: [],
  status: 'idle',
  error: null,
  showUpstream: true,
  showDownstream: true,
  abortControllers: new Map(),
  pendingExpansionUrns: new Set(),
  pendingFilterRetrace: false,
  locallyCollapsedUrns: new Set(),
  _filterDebounceTimer: null,

  bindWorkspace(wsId) {
    if (get().wsId === wsId) return
    // Workspace switch invalidates trace state — abort + clear.
    get().clear()
    set({ wsId })
  },

  async start(focusUrn, partial) {
    const wsId = get().wsId
    if (!wsId) {
      set({
        status: 'error',
        error: { code: 'trace_no_workspace', message: 'No workspace bound to trace store' },
      })
      return
    }

    // Build the full request body. Defaults pass through to the server
    // which applies its own resolution chain — nothing client-side
    // should infer targetLevel.
    const previous = get().request
    const request: TraceRequest = {
      ...DEFAULT_REQUEST,
      ...(previous ?? {}),
      ...partial,
      urn: focusUrn,
    }

    // Fresh AbortController; abort any prior 'trace' in flight.
    const controllers = new Map(get().abortControllers)
    const controller = abortKey('trace', controllers)

    set({
      focusUrn,
      request,
      status: 'loading',
      error: null,
      delta: [],
      // New trace invalidates locally-collapsed state from the previous trace.
      locallyCollapsedUrns: new Set(),
      abortControllers: controllers,
    })

    try {
      const result = await postTrace(wsId, request, { signal: controller.signal })

      // Stale-result guard: another start/clear may have replaced the
      // controller while this request was in flight. If our controller
      // is no longer the current one for 'trace', drop the result.
      if (get().abortControllers.get('trace') !== controller) return

      set({
        result,
        status: 'success',
        error: null,
      })
    } catch (err) {
      if (isAbortError(err)) return
      const apiErr =
        err instanceof TraceApiError
          ? err
          : new TraceApiError(
              'trace_request_failed',
              err instanceof Error ? err.message : 'Trace request failed',
              0,
            )
      // Only surface the error if we're still the active controller.
      if (get().abortControllers.get('trace') !== controller) return
      set({
        status: 'error',
        error: { code: apiErr.code, message: apiErr.message },
      })
    }
  },

  async retrace() {
    const { focusUrn, request } = get()
    if (!focusUrn || !request) return
    await get().start(focusUrn, request)
  },

  async expand(urn) {
    const { wsId, result } = get()
    if (!wsId || !result || !result.meta.traceSessionId) return

    const sessionId = result.meta.traceSessionId

    // Per-URN abort key so concurrent expands of different URNs don't
    // step on each other.
    const controllers = new Map(get().abortControllers)
    const key: AbortKey = `expand:${urn}`
    const controller = abortKey(key, controllers)

    const pending = new Set(get().pendingExpansionUrns)
    pending.add(urn)

    set({
      pendingExpansionUrns: pending,
      abortControllers: controllers,
    })

    const body: TraceExpandRequest = {
      traceSessionId: sessionId,
      expandUrn: urn,
      depthDelta: 1,
    }

    try {
      const delta = await postTraceExpand(wsId, body, { signal: controller.signal })

      // Stale-result guard.
      if (get().abortControllers.get(key) !== controller) return

      const merged = applyDelta(get().result, delta, urn)
      const nextPending = new Set(get().pendingExpansionUrns)
      nextPending.delete(urn)

      set({
        result: merged,
        delta: [...get().delta, delta],
        pendingExpansionUrns: nextPending,
      })
    } catch (err) {
      if (isAbortError(err)) {
        // Drop pending status for this URN even on abort so the spinner
        // doesn't stick around if the user re-toggled to a non-expand
        // state.
        if (get().abortControllers.get(key) === controller) {
          const nextPending = new Set(get().pendingExpansionUrns)
          nextPending.delete(urn)
          set({ pendingExpansionUrns: nextPending })
        }
        return
      }

      if (get().abortControllers.get(key) !== controller) return

      const apiErr =
        err instanceof TraceApiError
          ? err
          : new TraceApiError(
              'trace_expand_failed',
              err instanceof Error ? err.message : 'Trace expand failed',
              0,
            )

      const nextPending = new Set(get().pendingExpansionUrns)
      nextPending.delete(urn)

      set({
        error: { code: apiErr.code, message: apiErr.message },
        pendingExpansionUrns: nextPending,
      })
    }
  },

  collapse(urn) {
    const next = new Set(get().locallyCollapsedUrns)
    next.add(urn)
    set({ locallyCollapsedUrns: next })
  },

  clear() {
    // Abort every in-flight controller.
    const controllers = get().abortControllers
    for (const c of controllers.values()) {
      try {
        c.abort()
      } catch {
        // ignore
      }
    }
    // Clear any pending debounced retrace.
    const timer = get()._filterDebounceTimer
    if (timer) clearTimeout(timer)

    set({
      focusUrn: null,
      request: null,
      result: null,
      delta: [],
      status: 'idle',
      error: null,
      showUpstream: true,
      showDownstream: true,
      abortControllers: new Map(),
      pendingExpansionUrns: new Set(),
      pendingFilterRetrace: false,
      locallyCollapsedUrns: new Set(),
      _filterDebounceTimer: null,
    })
  },

  setEdgeTypeFilter(types) {
    const { request, focusUrn } = get()
    if (!focusUrn || !request) return

    // Update the request immediately so the toolbar reflects selection.
    set({
      request: { ...request, lineageEdgeTypes: types.length > 0 ? types : null },
      pendingFilterRetrace: true,
    })

    // Debounce the actual server call.
    const existingTimer = get()._filterDebounceTimer
    if (existingTimer) clearTimeout(existingTimer)

    const timer = setTimeout(() => {
      const state = get()
      // The user may have cleared the trace during the debounce window.
      if (!state.focusUrn || !state.request) {
        set({ pendingFilterRetrace: false, _filterDebounceTimer: null })
        return
      }
      set({ pendingFilterRetrace: false, _filterDebounceTimer: null })
      void state.start(state.focusUrn, state.request)
    }, FILTER_DEBOUNCE_MS)

    set({ _filterDebounceTimer: timer })
  },

  setShowUpstream(show) {
    set({ showUpstream: show })
  },

  setShowDownstream(show) {
    set({ showDownstream: show })
  },
}))

// ============================================
// Selectors
// ============================================

export const useTraceFocus = (): string | null => useLineageTrace((s) => s.focusUrn)
export const useTraceRequest = (): TraceRequest | null => useLineageTrace((s) => s.request)
export const useTraceResult = (): TraceResultV2 | null => useLineageTrace((s) => s.result)
export const useTraceMeta = (): TraceMeta | null =>
  useLineageTrace((s) => s.result?.meta ?? null)
export const useTraceStatus = (): TraceStatus => useLineageTrace((s) => s.status)
export const useTraceError = (): TraceErrorState | null => useLineageTrace((s) => s.error)
export const useIsTracing = (): boolean => useLineageTrace((s) => s.focusUrn !== null)
export const useIsTraceLoading = (): boolean => useLineageTrace((s) => s.status === 'loading')
export const useTraceShowUpstream = (): boolean => useLineageTrace((s) => s.showUpstream)
export const useTraceShowDownstream = (): boolean => useLineageTrace((s) => s.showDownstream)
export const useTraceLocallyCollapsed = (): Set<string> =>
  useLineageTrace((s) => s.locallyCollapsedUrns)

/**
 * URNs in the local containment-collapse set, expanded to also include
 * any URN whose containment ancestor (parent edge with isContainment=true)
 * is collapsed. This is the set of nodes that should be hidden from
 * `useTraceVisibleNodes` and `useTraceVisibleEdges`.
 */
function buildHiddenUrns(result: TraceResultV2 | null, locallyCollapsed: Set<string>): Set<string> {
  if (!result || locallyCollapsed.size === 0) return new Set()

  // Build child→parent map from containment edges only.
  const containmentParent = new Map<string, string>()
  for (const e of result.data.edges) {
    if (e.isContainment) {
      // Containment edges go parent -> child; child's hidden state
      // follows the parent's collapse.
      containmentParent.set(e.targetUrn, e.sourceUrn)
    }
  }

  const hidden = new Set<string>()
  for (const node of result.data.nodes) {
    if (locallyCollapsed.has(node.urn)) {
      // The collapsed URN itself stays visible (otherwise the user
      // can't re-expand). We hide its descendants.
      continue
    }
    // Walk ancestors; if any is collapsed, hide.
    let cursor: string | undefined = containmentParent.get(node.urn)
    while (cursor) {
      if (locallyCollapsed.has(cursor)) {
        hidden.add(node.urn)
        break
      }
      cursor = containmentParent.get(cursor)
    }
  }
  return hidden
}

/**
 * Visible trace nodes = result nodes minus locally-collapsed subtrees.
 * Memoized via the (result, collapse-set) tuple — both are stable
 * references between actions, so a new array is only built when one
 * actually changes.
 */
export function useTraceVisibleNodes(): GraphNode[] {
  const result = useTraceResult()
  const collapsed = useTraceLocallyCollapsed()
  return useMemo(() => {
    if (!result) return []
    if (collapsed.size === 0) return result.data.nodes
    const hidden = buildHiddenUrns(result, collapsed)
    if (hidden.size === 0) return result.data.nodes
    return result.data.nodes.filter((n) => !hidden.has(n.urn))
  }, [result, collapsed])
}

/**
 * Visible trace edges = result edges filtered by:
 *  - direction toggles (showUpstream / showDownstream),
 *  - locally-collapsed subtrees on either endpoint.
 *
 * Edges between the focus and its direct neighbors always render —
 * direction toggling is for everything *beyond* the focus.
 */
export function useTraceVisibleEdges(): TraceEdge[] {
  const result = useTraceResult()
  const showUpstream = useTraceShowUpstream()
  const showDownstream = useTraceShowDownstream()
  const collapsed = useTraceLocallyCollapsed()

  return useMemo(() => {
    if (!result) return []
    const upstream = new Set(result.data.upstreamUrns)
    const downstream = new Set(result.data.downstreamUrns)
    const hidden = collapsed.size > 0 ? buildHiddenUrns(result, collapsed) : new Set<string>()

    return result.data.edges.filter((e) => {
      if (hidden.has(e.sourceUrn) || hidden.has(e.targetUrn)) return false
      // Containment edges always render when their endpoints are visible.
      if (e.isContainment) return true

      const touchesUpstream = upstream.has(e.sourceUrn) || upstream.has(e.targetUrn)
      const touchesDownstream = downstream.has(e.sourceUrn) || downstream.has(e.targetUrn)

      if (touchesUpstream && !showUpstream && !touchesDownstream) return false
      if (touchesDownstream && !showDownstream && !touchesUpstream) return false

      return true
    })
  }, [result, showUpstream, showDownstream, collapsed])
}

export interface TraceStatistics {
  totalNodes: number
  upstreamCount: number
  downstreamCount: number
  totalEdges: number
  edgeTypes: string[]
  regime: TraceMeta['regime'] | null
  materializedHitRate: number
  queryMs: number
  isInherited: boolean
  inheritedFrom: string[]
}

export function useTraceStatistics(): TraceStatistics {
  const result = useTraceResult()
  return useMemo(() => {
    if (!result) {
      return {
        totalNodes: 0,
        upstreamCount: 0,
        downstreamCount: 0,
        totalEdges: 0,
        edgeTypes: [],
        regime: null,
        materializedHitRate: 0,
        queryMs: 0,
        isInherited: false,
        inheritedFrom: [],
      }
    }
    const data = result.data
    const meta = result.meta
    const edgeTypes = new Set<string>()
    for (const e of data.edges) edgeTypes.add(e.edgeType)
    return {
      totalNodes: data.nodes.length,
      upstreamCount: data.upstreamUrns.length,
      downstreamCount: data.downstreamUrns.length,
      totalEdges: data.edges.length,
      edgeTypes: Array.from(edgeTypes),
      regime: meta.regime,
      materializedHitRate: meta.materializedHitRate,
      queryMs: meta.queryMs,
      isInherited: data.inheritedFrom.length > 0,
      inheritedFrom: data.inheritedFrom,
    }
  }, [result])
}

/**
 * True iff the URN is in `expandableUrns` AND not currently expanding
 * AND not already locally collapsed (collapsed nodes can't be re-drilled
 * before they're re-expanded — local collapse is a UI affordance, not a
 * server state change).
 */
export const useIsExpandable = (urn: string): boolean =>
  useLineageTrace((s) => {
    if (!s.result) return false
    if (s.locallyCollapsedUrns.has(urn)) return false
    return s.result.data.expandableUrns.includes(urn)
  })

export const useIsExpansionPending = (urn: string): boolean =>
  useLineageTrace((s) => s.pendingExpansionUrns.has(urn))

// ============================================
// Public hook surface
// ============================================

export interface UseLineageTraceResult {
  // state
  status: TraceStatus
  error: TraceErrorState | null
  focusUrn: string | null
  request: TraceRequest | null
  result: TraceResultV2 | null
  isTracing: boolean
  isLoading: boolean
  showUpstream: boolean
  showDownstream: boolean
  // actions
  start: (focusUrn: string, request: Partial<TraceRequest>) => Promise<void>
  retrace: () => Promise<void>
  expand: (urn: string) => Promise<void>
  collapse: (urn: string) => void
  clear: () => void
  setEdgeTypeFilter: (types: string[]) => void
  setShowUpstream: (show: boolean) => void
  setShowDownstream: (show: boolean) => void
  // derived
  visibleNodes: GraphNode[]
  visibleEdges: TraceEdge[]
  statistics: TraceStatistics
}

/**
 * Convenience hook composing the most common selectors + actions.
 * Components that only need a subset should call the granular selectors
 * directly so they re-render only on the slices they consume.
 */
export function useLineageTraceState(): UseLineageTraceResult {
  const status = useTraceStatus()
  const error = useTraceError()
  const focusUrn = useTraceFocus()
  const request = useTraceRequest()
  const result = useTraceResult()
  const showUpstream = useTraceShowUpstream()
  const showDownstream = useTraceShowDownstream()

  const start = useLineageTrace((s) => s.start)
  const retrace = useLineageTrace((s) => s.retrace)
  const expand = useLineageTrace((s) => s.expand)
  const collapse = useLineageTrace((s) => s.collapse)
  const clear = useLineageTrace((s) => s.clear)
  const setEdgeTypeFilter = useLineageTrace((s) => s.setEdgeTypeFilter)
  const setShowUpstream = useLineageTrace((s) => s.setShowUpstream)
  const setShowDownstream = useLineageTrace((s) => s.setShowDownstream)

  const visibleNodes = useTraceVisibleNodes()
  const visibleEdges = useTraceVisibleEdges()
  const statistics = useTraceStatistics()

  return {
    status,
    error,
    focusUrn,
    request,
    result,
    isTracing: focusUrn !== null,
    isLoading: status === 'loading',
    showUpstream,
    showDownstream,
    start,
    retrace,
    expand,
    collapse,
    clear,
    setEdgeTypeFilter,
    setShowUpstream,
    setShowDownstream,
    visibleNodes,
    visibleEdges,
    statistics,
  }
}
