/**
 * useExplorerViews — Data fetching hook for the Explorer page.
 *
 * Strategic server-driven design:
 * - Every filter is a query param on ``GET /api/v1/views/`` — the backend
 *   is the single source of truth and returns a ``{ items, total, hasMore,
 *   nextOffset }`` envelope. No client-side residual filtering.
 * - ``total`` is the authoritative count shown in the UI; ``hasMore``
 *   drives the infinite-scroll sentinel.
 * - ``loadMore()`` fetches the server-advertised ``nextOffset`` and
 *   appends (deduped) to the loaded page.
 * - Filter changes reset pagination and refetch from offset 0.
 * - Search is debounced 300ms before it hits the wire.
 * - Sort is client-side over the assembled list; we keep the API in
 *   ``updated_at desc`` order so pagination stays deterministic.
 * - Optimistic favourite toggles.
 */
import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import {
  listViews, favouriteView, unfavouriteView,
  type View, type ViewListParams,
} from '@/services/viewApiService'

/** Stable JSON key for array deps — prevents infinite re-render loops from new array refs. */
function useStableKey(value: unknown): string {
  const key = JSON.stringify(value)
  const ref = useRef(key)
  if (ref.current !== key) ref.current = key
  return ref.current
}

// ─── Sort Options ───────────────────────────────────────────────────────────

/**
 * Sort options. The first six are the user-selectable global sorts
 * shown in the sort dropdown; the column-sorts below are applied when
 * the user clicks a header in the list view. Every option has a single
 * canonical string so the URL (``?sort=az``) stays stable and
 * human-readable.
 */
export type SortOption =
  | 'newest'
  | 'oldest'
  | 'popular'
  | 'updated'
  | 'az'
  | 'za'
  // Column-sorts (list view header clicks)
  | 'updated-asc'
  | 'likes-asc'
  | 'type-az'
  | 'type-za'
  | 'owner-az'
  | 'owner-za'

function sortViews(views: View[], sort: SortOption): View[] {
  const sorted = [...views]
  const byStr = (a: string, b: string) => a.localeCompare(b)
  switch (sort) {
    case 'newest':
      return sorted.sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime())
    case 'oldest':
      return sorted.sort((a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime())
    case 'popular':
      return sorted.sort((a, b) => b.favouriteCount - a.favouriteCount || new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    case 'updated':
      return sorted.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    case 'updated-asc':
      return sorted.sort((a, b) => new Date(a.updatedAt).getTime() - new Date(b.updatedAt).getTime())
    case 'likes-asc':
      return sorted.sort((a, b) => a.favouriteCount - b.favouriteCount || new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    case 'az':
      return sorted.sort((a, b) => byStr(a.name, b.name))
    case 'za':
      return sorted.sort((a, b) => byStr(b.name, a.name))
    case 'type-az':
      return sorted.sort((a, b) => byStr(a.viewType ?? '', b.viewType ?? '') || byStr(a.name, b.name))
    case 'type-za':
      return sorted.sort((a, b) => byStr(b.viewType ?? '', a.viewType ?? '') || byStr(a.name, b.name))
    case 'owner-az':
      return sorted.sort((a, b) => byStr(a.createdByName ?? a.createdBy ?? '', b.createdByName ?? b.createdBy ?? '') || byStr(a.name, b.name))
    case 'owner-za':
      return sorted.sort((a, b) => byStr(b.createdByName ?? b.createdBy ?? '', a.createdByName ?? a.createdBy ?? '') || byStr(a.name, b.name))
    default:
      return sorted
  }
}

// ─── Filter Params ──────────────────────────────────────────────────────────

export interface ExplorerFilters {
  search: string
  visibility: string | null         // 'enterprise' | 'workspace' | 'private' | null (all)
  workspaceIds: string[]            // Multi-select — sent as ``workspaceIds`` on API
  dataSourceId: string | null
  viewTypes: string[]               // Multi-select — sent as ``viewTypes`` on API
  tags: string[]                    // Multi-select — sent as ``tags`` on API (OR semantics)
  creatorIds: string[]              // Multi-select — sent as ``createdByIn`` on API
  sort: SortOption
  favouritedOnly: boolean
  /** 'my-views' | 'my-favourites' | 'recently-added' | 'shared-with-me' | 'needs-attention' | 'deleted' | null */
  category: string | null
  currentUserId: string | null      // For 'my-views' — sent as ``createdBy`` on API
  limit: number                     // Page size
  offset: number                    // Unused externally; hook manages offset internally
}

export interface UseExplorerViewsResult {
  views: View[]                     // Sorted + paginated display slice
  totalCount: number                // Authoritative server total across all pages
  popularViews: View[]
  isLoading: boolean                // Initial fetch
  isLoadingMore: boolean            // Subsequent page fetch
  error: string | null
  toggleFavourite: (viewId: string) => void
  removeView: (viewId: string) => void
  refetch: () => void
  loadMore: () => void
  hasMore: boolean
}

// ─── Category → server params resolver ─────────────────────────────────────

/**
 * Translate a user-selected category to the concrete API params the backend
 * understands. Centralising this here keeps the hook body simple and makes
 * the client/server contract explicit — no hidden client-side filtering.
 *
 * Exported so the stats bar and any future catalog-level consumers can
 * translate the same way without reimplementing the mapping.
 */
export function resolveCategoryParams(
  category: string | null,
  currentUserId: string | null,
): Partial<ViewListParams> {
  switch (category) {
    case 'my-views':
      // If we have no user id the Explorer page falls back to showing
      // nothing (not "all") so the filter label stays truthful.
      return currentUserId ? { createdBy: currentUserId } : { createdBy: '__no_user__' }
    case 'my-favourites':
      return { favouritedOnly: true }
    case 'recently-added': {
      // IMPORTANT: quantise the cutoff to a 5-minute bucket so the
      // string value is stable across re-renders. Without this, every
      // render produces a fresh ``Date.now()`` → fresh ISO string →
      // fresh React Query key → refetch → state update → re-render →
      // infinite fetch loop. The 5-minute window means the "last 7
      // days" cutoff can drift by at most 5 minutes, which is fine
      // for a human-facing "recent" filter.
      const QUANTUM_MS = 5 * 60 * 1000
      const now = Math.floor(Date.now() / QUANTUM_MS) * QUANTUM_MS
      const sevenDaysAgo = new Date(now - 7 * 24 * 60 * 60 * 1000)
      return { createdAfter: sevenDaysAgo.toISOString() }
    }
    case 'shared-with-me':
      return { visibilityIn: ['workspace', 'enterprise'] }
    case 'needs-attention':
      return { attentionOnly: true }
    case 'deleted':
      return { deletedOnly: true }
    default:
      return {}
  }
}

// ─── Hook ───────────────────────────────────────────────────────────────────

export function useExplorerViews(filters: ExplorerFilters): UseExplorerViewsResult {
  const [allViews, setAllViews] = useState<View[]>([])
  const [popularViews, setPopularViews] = useState<View[]>([])
  const [total, setTotal] = useState(0)
  const [nextOffset, setNextOffset] = useState<number | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refetchKey, setRefetchKey] = useState(0)

  // Debounce search — only make API call after 300ms of no typing
  const [debouncedSearch, setDebouncedSearch] = useState(filters.search)
  const debounceTimer = useRef<ReturnType<typeof setTimeout>>(null)

  useEffect(() => {
    debounceTimer.current = setTimeout(() => {
      setDebouncedSearch(filters.search)
    }, 300)
    return () => { if (debounceTimer.current) clearTimeout(debounceTimer.current) }
  }, [filters.search])

  // Stabilise array deps so the fetch effect doesn't loop on identical values.
  const workspaceIdsKey = useStableKey(filters.workspaceIds)
  const viewTypesKey = useStableKey(filters.viewTypes)
  const tagsKey = useStableKey(filters.tags)
  const creatorIdsKey = useStableKey(filters.creatorIds)
  const stableVisibility = filters.visibility
  const stableDataSourceId = filters.dataSourceId
  const stableFavouritedOnly = filters.favouritedOnly
  const stableCategory = filters.category
  const stableCurrentUserId = filters.currentUserId
  const stableLimit = filters.limit

  // Build the full set of API params for a given page offset. Category
  // params are merged last so they can override defaults (e.g. the
  // ``deleted`` category sets ``deletedOnly: true``).
  const buildParams = useCallback((pageOffset: number): ViewListParams => {
    const wsIds: string[] = JSON.parse(workspaceIdsKey)
    const vTypes: string[] = JSON.parse(viewTypesKey)
    const tagList: string[] = JSON.parse(tagsKey)
    const creators: string[] = JSON.parse(creatorIdsKey)
    const categoryParams = resolveCategoryParams(stableCategory, stableCurrentUserId)

    const params: ViewListParams = {
      search: debouncedSearch || undefined,
      visibility: stableVisibility || undefined,
      favouritedOnly: stableFavouritedOnly || undefined,
      dataSourceId: stableDataSourceId || undefined,
      workspaceIds: wsIds.length > 0 ? wsIds : undefined,
      viewTypes: vTypes.length > 0 ? vTypes : undefined,
      tags: tagList.length > 0 ? tagList : undefined,
      createdByIn: creators.length > 0 ? creators : undefined,
      limit: stableLimit,
      offset: pageOffset,
      ...categoryParams,
    }

    return params
  }, [
    debouncedSearch, stableVisibility, stableFavouritedOnly,
    stableDataSourceId, workspaceIdsKey, viewTypesKey, tagsKey,
    creatorIdsKey, stableLimit, stableCategory, stableCurrentUserId,
  ])

  // ─── Initial fetch + filter-change reload ───────────────────────────

  useEffect(() => {
    // Native AbortController: the cleanup aborts the in-flight HTTP
    // request itself (rapid filter changes / unmount no longer waste
    // bandwidth on responses we'll discard). Replaces the previous
    // ``cancelled`` flag idiom which only suppressed setState.
    const controller = new AbortController()

    const fetchInitial = async () => {
      setIsLoading(true)
      setError(null)

      try {
        // Single request: ``?include=popular`` makes the server fold the
        // trending strip into the same response, killing the second
        // round-trip the page used to make.
        const envelope = await listViews(
          { ...buildParams(0), include: ['popular'], popularLimit: 10 },
          controller.signal,
        )

        setAllViews(envelope.items)
        setPopularViews(envelope.popular ?? [])
        setTotal(envelope.total)
        setNextOffset(envelope.hasMore ? envelope.nextOffset : null)
      } catch (err) {
        // AbortError fires when the controller is aborted — that's the
        // happy-path cleanup, not a real failure, so don't surface it.
        if ((err as DOMException)?.name === 'AbortError') return
        console.error('[useExplorerViews] Failed to load views:', err)
        setError(err instanceof Error ? err.message : 'Failed to load views')
      } finally {
        if (!controller.signal.aborted) {
          setIsLoading(false)
        }
      }
    }

    fetchInitial()
    return () => { controller.abort() }
  }, [buildParams, refetchKey])

  // ─── Load next page (append) ───────────────────────────────────────

  const loadMore = useCallback(async () => {
    if (isLoading || isLoadingMore || nextOffset == null) return

    setIsLoadingMore(true)
    try {
      const envelope = await listViews(buildParams(nextOffset))
      setAllViews(prev => {
        // Defensive de-dupe in case rows moved between pages.
        const seen = new Set(prev.map(v => v.id))
        const fresh = envelope.items.filter(v => !seen.has(v.id))
        return [...prev, ...fresh]
      })
      setTotal(envelope.total)
      setNextOffset(envelope.hasMore ? envelope.nextOffset : null)
    } catch (err) {
      console.error('[useExplorerViews] loadMore failed:', err)
    } finally {
      setIsLoadingMore(false)
    }
  }, [isLoading, isLoadingMore, nextOffset, buildParams])

  // ─── Sort ──────────────────────────────────────────────────────────
  // All filtering happens server-side; we only re-sort the loaded slice.

  const views = useMemo(() => sortViews(allViews, filters.sort), [allViews, filters.sort])

  // ─── Optimistic favourite toggle ────────────────────────────────────

  const toggleFavourite = useCallback((viewId: string) => {
    const view = allViews.find(v => v.id === viewId)
    if (!view) return

    const wasFavourited = view.isFavourited

    setAllViews(prev => prev.map(v =>
      v.id === viewId
        ? { ...v, isFavourited: !wasFavourited, favouriteCount: v.favouriteCount + (wasFavourited ? -1 : 1) }
        : v
    ))
    setPopularViews(prev => prev.map(v =>
      v.id === viewId
        ? { ...v, isFavourited: !wasFavourited, favouriteCount: v.favouriteCount + (wasFavourited ? -1 : 1) }
        : v
    ))

    const apiCall = wasFavourited ? unfavouriteView(viewId) : favouriteView(viewId)
    apiCall.catch(() => {
      setAllViews(prev => prev.map(v =>
        v.id === viewId
          ? { ...v, isFavourited: wasFavourited, favouriteCount: v.favouriteCount + (wasFavourited ? 1 : -1) }
          : v
      ))
      setPopularViews(prev => prev.map(v =>
        v.id === viewId
          ? { ...v, isFavourited: wasFavourited, favouriteCount: v.favouriteCount + (wasFavourited ? 1 : -1) }
          : v
      ))
    })
  }, [allViews])

  const removeView = useCallback((viewId: string) => {
    setAllViews(prev => prev.filter(v => v.id !== viewId))
    setPopularViews(prev => prev.filter(v => v.id !== viewId))
    setTotal(t => Math.max(0, t - 1))
  }, [])

  const refetch = useCallback(() => {
    setRefetchKey(k => k + 1)
  }, [])

  return {
    views,
    totalCount: total,
    popularViews,
    isLoading,
    isLoadingMore,
    error,
    toggleFavourite,
    removeView,
    refetch,
    loadMore,
    hasMore: nextOffset != null,
  }
}
