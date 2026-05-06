/**
 * useEntitySearch — backend-driven graph-entity search with debounce + abort.
 *
 * Distinct from `useGlobalSearch` (which finds workspaces/views/templates) —
 * this hook finds *graph entities inside the current view's data*. Backed by
 * `provider.searchEntities()` (POST /api/v1/graph/search/entities) which
 * returns hits with their full containment ancestor chain so the canvas can
 * lazy-expand to a result that lives many levels deep.
 *
 * Behaviour:
 *   - 250ms debounce on the query before hitting the network.
 *   - Each new search aborts the previous in-flight request — only the most
 *     recent query's results land in state.
 *   - Empty / whitespace queries return an idle empty state without firing.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useGraphProvider } from '@/providers'
import type { EntitySearchHit, EntitySearchRequest } from '@/providers/GraphDataProvider'

export type EntitySearchScope = 'view' | 'global'

export interface UseEntitySearchOptions {
  /** Default scope. Each canvas decides what's right for its mental model. */
  initialScope?: EntitySearchScope
  /** Optional view id used when scope === 'view' (passes through as `viewId`). */
  viewId?: string | null
  /** Per-page hit count. Default 20. */
  limit?: number
  /** Restrict matching to specific entity types. */
  entityTypes?: string[]
}

export interface UseEntitySearchResult {
  query: string
  setQuery: (q: string) => void
  scope: EntitySearchScope
  setScope: (s: EntitySearchScope) => void
  results: EntitySearchHit[]
  isLoading: boolean
  error: Error | null
  total: number
  hasMore: boolean
  tookMs: number
  /** Append the next page to `results`. No-op when `hasMore` is false. */
  fetchMore: () => void
  /** Reset query + results. */
  clear: () => void
}

const DEBOUNCE_MS = 250

export function useEntitySearch(opts: UseEntitySearchOptions = {}): UseEntitySearchResult {
  const provider = useGraphProvider()
  const limit = opts.limit ?? 20

  const [query, setQuery] = useState('')
  const [scope, setScope] = useState<EntitySearchScope>(opts.initialScope ?? 'view')
  const [results, setResults] = useState<EntitySearchHit[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [total, setTotal] = useState(-1)
  const [hasMore, setHasMore] = useState(false)
  const [tookMs, setTookMs] = useState(0)

  // Refs survive re-renders without retriggering effects.
  const abortRef = useRef<AbortController | null>(null)
  const offsetRef = useRef(0)
  // Tracks the query that produced the current `results` so fetchMore appends
  // to the right buffer even if the user has typed since the last fetch.
  const committedQueryRef = useRef('')

  const runSearch = useCallback(
    async (q: string, offset: number, append: boolean) => {
      if (!provider.searchEntities) {
        setError(new Error('Provider does not support entity search.'))
        return
      }

      // Cancel any in-flight request — only the latest keystroke matters.
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl

      setIsLoading(true)
      setError(null)

      const req: EntitySearchRequest = {
        query: q,
        limit,
        offset,
        includeAncestors: true,
        viewId: scope === 'view' ? (opts.viewId ?? null) : null,
        entityTypes: opts.entityTypes,
      }

      try {
        const response = await provider.searchEntities(req)
        if (ctrl.signal.aborted) return
        setResults(prev => (append ? [...prev, ...response.hits] : response.hits))
        setTotal(response.total)
        setHasMore(response.hasMore)
        setTookMs(response.tookMs)
        offsetRef.current = offset + response.hits.length
        committedQueryRef.current = q
      } catch (err) {
        if (ctrl.signal.aborted) return
        // AbortError is the expected outcome of a superseded request — never
        // surface it to the UI.
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err : new Error(String(err)))
        if (!append) setResults([])
      } finally {
        if (!ctrl.signal.aborted) setIsLoading(false)
      }
    },
    [provider, limit, scope, opts.viewId, opts.entityTypes],
  )

  // Debounced fetch on query / scope change.
  useEffect(() => {
    const trimmed = query.trim()
    if (!trimmed) {
      abortRef.current?.abort()
      setResults([])
      setTotal(-1)
      setHasMore(false)
      setIsLoading(false)
      setError(null)
      offsetRef.current = 0
      committedQueryRef.current = ''
      return
    }

    const timer = setTimeout(() => {
      runSearch(trimmed, 0, false)
    }, DEBOUNCE_MS)

    return () => clearTimeout(timer)
  }, [query, scope, runSearch])

  // Cancel on unmount.
  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  const fetchMore = useCallback(() => {
    if (!hasMore || isLoading) return
    const q = committedQueryRef.current
    if (!q) return
    runSearch(q, offsetRef.current, true)
  }, [hasMore, isLoading, runSearch])

  const clear = useCallback(() => {
    abortRef.current?.abort()
    setQuery('')
    setResults([])
    setTotal(-1)
    setHasMore(false)
    setError(null)
    setIsLoading(false)
    offsetRef.current = 0
    committedQueryRef.current = ''
  }, [])

  return {
    query, setQuery,
    scope, setScope,
    results, isLoading, error, total, hasMore, tookMs,
    fetchMore, clear,
  }
}
