/**
 * useEntityBrowser — API-authoritative entity browsing hook for the ViewWizard.
 *
 * Loads entities that sit at the TOP of the containment hierarchy (ontology
 * roots + orphan instances of non-root types), then lazy-expands children
 * via containment edges. Every browse action hits the API — no stale caches.
 *
 * Why "top-level" and not "root types":
 *   The old implementation derived a list of "root entity types" from the
 *   ontology and then queried `getNodes({ entityTypes: rootTypes })`. That
 *   was both ontology-specific (custom ontologies may have no single root)
 *   AND silently ignored orphans — a Platform ingested without a Domain
 *   would just disappear from the wizard. The new `getTopLevelNodes`
 *   endpoint is a *structural* query: "nodes with no incoming containment
 *   edge", which makes the wizard correct on any ontology and surfaces
 *   orphans explicitly (diagnostic `rootTypeCount` / `orphanCount` fields).
 *
 * Designed for million-node scale:
 * - Cursor-based pagination for both top-level AND child loads
 * - Strictly lazy: ONE level per expand, never recursive
 * - Type filter is pure frontend ontology computation (no API call) when
 *   filtering what the user sees; when the filter is active AND no search
 *   is running, we re-query with `entityTypes=[typeId]` so the top-level
 *   page is pre-narrowed by the server instead of fetching N pages and
 *   filtering them down client-side.
 * - Scoped search within expanded subtrees via `/nodes/top-level?searchQuery`
 */

import { useState, useCallback, useRef, useMemo, useEffect } from 'react'
import type {
    GraphDataProvider,
    GraphNode,
    EntityTypeDefinition,
    DescendantPreviewQuery,
    DescendantPreviewResult,
} from '@/providers/GraphDataProvider'

// ─── Constants ──────────────────────────────────────────────────────────────

const PAGE_SIZE = 50

// ─── Types ──────────────────────────────────────────────────────────────────

export interface BrowserNode {
    /** Raw API response — always authoritative */
    node: GraphNode
    /** URNs of direct children (from containment edges in the API response) */
    childIds: string[]
    /** Approximate total children count (from API childCount or totalChildren) */
    totalChildren: number
    /** Whether more children exist beyond what's loaded */
    hasMore: boolean
    /** Cursor for the next page of children */
    nextCursor: string | null
    /** Whether children have been fetched at least once */
    loaded: boolean
}

export interface TopLevelMetadata {
    /** How many top-level nodes are ontology-root instances. */
    rootTypeCount: number
    /** How many are orphans of non-root types (missing containment in-edge). */
    orphanCount: number
}

export interface UseEntityBrowserOptions {
    provider: GraphDataProvider
    /** Containment edge types from the ontology (is_containment=true).
     *  Still required for child-expansion calls. */
    containmentEdgeTypes: string[]
    /**
     * Full entity type definitions from the ontology — kept ONLY so the
     * type-filter UI can compute `canContain` chains and show/hide
     * intermediate branches in the tree. NOT used for data loading.
     */
    entityTypeDefinitions: EntityTypeDefinition[]
    /** Set to false while schema is still loading. */
    enabled: boolean
}

export interface UseEntityBrowserResult {
    // ─── Data (from API responses) ───
    nodes: Map<string, BrowserNode>
    topLevelIds: string[]
    topLevelHasMore: boolean
    topLevelTotalCount: number
    topLevelMetadata: TopLevelMetadata
    parentMap: Map<string, string>

    // ─── Ontology-derived ───
    canTransitivelyContain: (ancestorType: string, targetType: string) => boolean
    typesOnPathTo: (targetType: string) => Set<string>

    // ─── State ───
    isLoading: boolean
    loadingNodes: Set<string>
    searchQuery: string
    typeFilter: string | null
    error: string | null

    // ─── Actions ───
    loadTopLevel: () => Promise<void>
    loadMoreTopLevel: () => Promise<void>
    expandNode: (urn: string) => Promise<void>
    loadMoreChildren: (parentUrn: string) => Promise<void>
    setSearch: (query: string) => void
    setTypeFilter: (typeId: string | null) => void
    refresh: () => Promise<void>

    /**
     * Server-side preview of descendants under `parentId` matching the
     * filter. Used by the wizard's bulk-assign panel to show a live
     * "matches N entities" count before the user saves a scoped rule.
     * Does not touch selection or the lazy-loaded tree.
     */
    previewDescendants: (parentId: string, query: DescendantPreviewQuery) => Promise<DescendantPreviewResult>
}

// ─── Hook ───────────────────────────────────────────────────────────────────

export function useEntityBrowser(options: UseEntityBrowserOptions): UseEntityBrowserResult {
    const { provider, containmentEdgeTypes, entityTypeDefinitions, enabled } = options

    // ─── State ───
    const [nodes, setNodes] = useState<Map<string, BrowserNode>>(new Map())
    const [topLevelIds, setTopLevelIds] = useState<string[]>([])
    const [topLevelHasMore, setTopLevelHasMore] = useState(false)
    const [topLevelCursor, setTopLevelCursor] = useState<string | null>(null)
    const [topLevelTotalCount, setTopLevelTotalCount] = useState(0)
    const [topLevelMetadata, setTopLevelMetadata] = useState<TopLevelMetadata>({
        rootTypeCount: 0,
        orphanCount: 0,
    })
    const [parentMap, setParentMap] = useState<Map<string, string>>(new Map())
    const [loadingNodes, setLoadingNodes] = useState<Set<string>>(new Set())
    const [isLoading, setIsLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [searchQuery, setSearchQueryState] = useState('')
    const [typeFilter, setTypeFilterState] = useState<string | null>(null)

    // Refs — mutable, no re-render, no stale closure issues
    const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
    const providerRef = useRef<GraphDataProvider | null>(null)
    // Use refs for state that callbacks need to read without re-creating closures
    const nodesRef = useRef(nodes)
    nodesRef.current = nodes
    // Capture current filter/search inside loaders without retriggering the
    // callback identity every time the user types — the callback reads the
    // ref at call-time instead.
    const typeFilterRef = useRef(typeFilter)
    typeFilterRef.current = typeFilter

    // Reset when provider changes (workspace/datasource switch)
    useEffect(() => {
        if (providerRef.current !== provider) {
            providerRef.current = provider
            setNodes(new Map())
            setTopLevelIds([])
            setTopLevelHasMore(false)
            setTopLevelCursor(null)
            setTopLevelTotalCount(0)
            setTopLevelMetadata({ rootTypeCount: 0, orphanCount: 0 })
            setParentMap(new Map())
            setError(null)
        }
    }, [provider])

    // ─── Ontology computations (memoized, zero API calls) ───

    const canContainMap = useMemo(() => {
        const map = new Map<string, string[]>()
        for (const et of entityTypeDefinitions) {
            map.set(et.id, et.hierarchy?.canContain ?? [])
        }
        return map
    }, [entityTypeDefinitions])

    const canTransitivelyContain = useCallback((ancestorType: string, targetType: string): boolean => {
        if (ancestorType === targetType) return true
        const visited = new Set<string>()
        const queue = [ancestorType]
        while (queue.length > 0) {
            const current = queue.shift()!
            if (visited.has(current)) continue
            visited.add(current)
            const children = canContainMap.get(current) ?? []
            for (const child of children) {
                if (child === targetType) return true
                queue.push(child)
            }
        }
        return false
    }, [canContainMap])

    const typesOnPathTo = useCallback((targetType: string): Set<string> => {
        const result = new Set<string>()
        for (const et of entityTypeDefinitions) {
            if (et.id === targetType) continue
            if (canTransitivelyContain(et.id, targetType)) {
                result.add(et.id)
            }
        }
        return result
    }, [entityTypeDefinitions, canTransitivelyContain])

    // ─── Helpers ───

    const addLoading = useCallback((id: string) => {
        setLoadingNodes(prev => { const next = new Set(prev); next.add(id); return next })
    }, [])

    const removeLoading = useCallback((id: string) => {
        setLoadingNodes(prev => { const next = new Set(prev); next.delete(id); return next })
    }, [])

    const mergeTopLevelResult = useCallback(
        (
            result: Awaited<ReturnType<GraphDataProvider['getTopLevelNodes']>>,
            mode: 'replace' | 'append',
        ) => {
            if (mode === 'replace') {
                const newNodes = new Map<string, BrowserNode>()
                const newIds: string[] = []
                for (const node of result.nodes) {
                    newNodes.set(node.urn, {
                        node,
                        childIds: [],
                        totalChildren: node.childCount ?? 0,
                        hasMore: false,
                        nextCursor: null,
                        loaded: false,
                    })
                    newIds.push(node.urn)
                }
                setNodes(newNodes)
                setTopLevelIds(newIds)
                setParentMap(new Map())
            } else {
                setNodes(prev => {
                    const next = new Map(prev)
                    for (const node of result.nodes) {
                        if (!next.has(node.urn)) {
                            next.set(node.urn, {
                                node,
                                childIds: [],
                                totalChildren: node.childCount ?? 0,
                                hasMore: false,
                                nextCursor: null,
                                loaded: false,
                            })
                        }
                    }
                    return next
                })
                setTopLevelIds(prev => {
                    const existing = new Set(prev)
                    const newIds = result.nodes
                        .filter(n => !existing.has(n.urn))
                        .map(n => n.urn)
                    return [...prev, ...newIds]
                })
            }
            setTopLevelHasMore(Boolean(result.hasMore))
            setTopLevelCursor(result.nextCursor ?? null)
            setTopLevelTotalCount(result.totalCount ?? 0)
            setTopLevelMetadata({
                rootTypeCount: result.rootTypeCount ?? 0,
                orphanCount: result.orphanCount ?? 0,
            })
        },
        [],
    )

    // ─── loadTopLevel ───

    const loadTopLevel = useCallback(async () => {
        if (!enabled) return

        setIsLoading(true)
        setError(null)

        try {
            const activeFilter = typeFilterRef.current
            const result = await provider.getTopLevelNodes({
                entityTypes: activeFilter ? [activeFilter] : undefined,
                limit: PAGE_SIZE,
                cursor: null,
                includeChildCount: true,
            })
            mergeTopLevelResult(result, 'replace')
        } catch (err) {
            console.error('[useEntityBrowser] Failed to load top-level nodes:', err)
            setError(err instanceof Error ? err.message : 'Failed to load entities')
        } finally {
            setIsLoading(false)
        }
    }, [enabled, provider, mergeTopLevelResult])

    // ─── loadMoreTopLevel ───

    const loadMoreTopLevel = useCallback(async () => {
        if (!topLevelHasMore) return
        addLoading('__top-level')

        try {
            const activeFilter = typeFilterRef.current
            const result = await provider.getTopLevelNodes({
                entityTypes: activeFilter ? [activeFilter] : undefined,
                limit: PAGE_SIZE,
                cursor: topLevelCursor,
                includeChildCount: true,
            })
            mergeTopLevelResult(result, 'append')
        } catch (err) {
            console.error('[useEntityBrowser] Failed to load more top-level nodes:', err)
        } finally {
            removeLoading('__top-level')
        }
    }, [topLevelHasMore, topLevelCursor, provider, mergeTopLevelResult, addLoading, removeLoading])

    // ─── expandNode: lazy-load direct children (ONE level only) ───
    // Uses nodesRef to avoid re-creating this callback when nodes change.

    const expandNode = useCallback(async (urn: string) => {
        // Read from ref to avoid stale closure — no nodes in dependency array
        const existing = nodesRef.current.get(urn)
        if (existing?.loaded) return

        addLoading(urn)

        try {
            const result = await provider.getChildrenWithEdges(urn, {
                edgeTypes: containmentEdgeTypes.length > 0 ? containmentEdgeTypes : undefined,
                limit: PAGE_SIZE,
                offset: 0,
                includeLineageEdges: false,
            })

            setNodes(prev => {
                const next = new Map(prev)

                // Collect child IDs inside the updater to avoid closure issues
                const childIds: string[] = []
                for (const child of result.children) {
                    childIds.push(child.urn)
                    // Always update the child node data (fresh from API)
                    const existingChild = next.get(child.urn)
                    next.set(child.urn, {
                        node: child,
                        childIds: existingChild?.childIds ?? [],
                        totalChildren: child.childCount ?? 0,
                        hasMore: existingChild?.hasMore ?? false,
                        nextCursor: existingChild?.nextCursor ?? null,
                        loaded: existingChild?.loaded ?? false,
                    })
                }

                // Update parent with children info
                const parentEntry = next.get(urn)
                if (parentEntry) {
                    next.set(urn, {
                        ...parentEntry,
                        childIds,
                        totalChildren: result.totalChildren,
                        hasMore: result.hasMore,
                        nextCursor: result.nextCursor ?? null,
                        loaded: true,
                    })
                }
                return next
            })

            // Update parent map from containment edges
            setParentMap(prev => {
                const next = new Map(prev)
                for (const edge of result.containmentEdges) {
                    next.set(edge.targetUrn, edge.sourceUrn)
                }
                return next
            })
        } catch (err) {
            console.error(`[useEntityBrowser] Failed to expand ${urn}:`, err)
        } finally {
            removeLoading(urn)
        }
    }, [provider, containmentEdgeTypes, addLoading, removeLoading])
    // NOTE: no `nodes` in deps — uses nodesRef instead to prevent infinite re-creation

    // ─── loadMoreChildren ───

    const loadMoreChildren = useCallback(async (parentUrn: string) => {
        const parentEntry = nodesRef.current.get(parentUrn)
        if (!parentEntry?.hasMore) return

        addLoading(parentUrn)

        try {
            const result = await provider.getChildrenWithEdges(parentUrn, {
                edgeTypes: containmentEdgeTypes.length > 0 ? containmentEdgeTypes : undefined,
                limit: PAGE_SIZE,
                cursor: parentEntry.nextCursor,
                includeLineageEdges: false,
            })

            setNodes(prev => {
                const next = new Map(prev)
                const newChildIds: string[] = []

                for (const child of result.children) {
                    newChildIds.push(child.urn)
                    if (!next.has(child.urn)) {
                        next.set(child.urn, {
                            node: child,
                            childIds: [],
                            totalChildren: child.childCount ?? 0,
                            hasMore: false,
                            nextCursor: null,
                            loaded: false,
                        })
                    }
                }

                const entry = next.get(parentUrn)
                if (entry) {
                    const existingSet = new Set(entry.childIds)
                    const appendIds = newChildIds.filter(id => !existingSet.has(id))
                    next.set(parentUrn, {
                        ...entry,
                        childIds: [...entry.childIds, ...appendIds],
                        totalChildren: result.totalChildren,
                        hasMore: result.hasMore,
                        nextCursor: result.nextCursor ?? null,
                    })
                }
                return next
            })

            setParentMap(prev => {
                const next = new Map(prev)
                for (const edge of result.containmentEdges) {
                    next.set(edge.targetUrn, edge.sourceUrn)
                }
                return next
            })
        } catch (err) {
            console.error(`[useEntityBrowser] Failed to load more children for ${parentUrn}:`, err)
        } finally {
            removeLoading(parentUrn)
        }
    }, [provider, containmentEdgeTypes, addLoading, removeLoading])

    // ─── setSearch: debounced server-side search ───

    const setSearch = useCallback((query: string) => {
        setSearchQueryState(query)

        if (searchTimerRef.current) {
            clearTimeout(searchTimerRef.current)
        }

        if (!query.trim()) {
            loadTopLevel()
            return
        }

        searchTimerRef.current = setTimeout(async () => {
            setIsLoading(true)
            try {
                const activeFilter = typeFilterRef.current
                const result = await provider.getTopLevelNodes({
                    entityTypes: activeFilter ? [activeFilter] : undefined,
                    searchQuery: query,
                    limit: PAGE_SIZE,
                    cursor: null,
                    includeChildCount: true,
                })
                mergeTopLevelResult(result, 'replace')
            } catch (err) {
                console.error('[useEntityBrowser] Search failed:', err)
                setError(err instanceof Error ? err.message : 'Search failed')
            } finally {
                setIsLoading(false)
            }
        }, 300)
    }, [provider, mergeTopLevelResult, loadTopLevel])

    // ─── setTypeFilter ───

    const setTypeFilter = useCallback((typeId: string | null) => {
        setTypeFilterState(typeId)
        // Re-query top-level with the new filter. If the user has an active
        // search, preserve it; otherwise fall through to a plain top-level
        // refresh. Update the ref immediately so the async callbacks see the
        // new filter without waiting for the next render.
        typeFilterRef.current = typeId
        if (searchQuery.trim()) {
            setSearch(searchQuery)
        } else {
            loadTopLevel()
        }
    }, [searchQuery, setSearch, loadTopLevel])

    // ─── previewDescendants ───

    const previewDescendants = useCallback(
        async (parentId: string, query: DescendantPreviewQuery): Promise<DescendantPreviewResult> => {
            return provider.getDescendantsPreview(parentId, query, {
                edgeTypes: containmentEdgeTypes.length > 0 ? containmentEdgeTypes : undefined,
            })
        },
        [provider, containmentEdgeTypes],
    )

    // ─── refresh ───

    const refresh = useCallback(async () => {
        if (searchQuery.trim()) {
            setSearch(searchQuery)
        } else {
            await loadTopLevel()
        }
    }, [searchQuery, setSearch, loadTopLevel])

    // Cleanup
    useEffect(() => {
        return () => {
            if (searchTimerRef.current) clearTimeout(searchTimerRef.current)
        }
    }, [])

    return {
        nodes,
        topLevelIds,
        topLevelHasMore,
        topLevelTotalCount,
        topLevelMetadata,
        parentMap,
        canTransitivelyContain,
        typesOnPathTo,
        isLoading,
        loadingNodes,
        searchQuery,
        typeFilter,
        error,
        loadTopLevel,
        loadMoreTopLevel,
        expandNode,
        loadMoreChildren,
        setSearch,
        setTypeFilter,
        refresh,
        previewDescendants,
    }
}
