/**
 * View API Service — First-class view persistence (de-coupled from Context Models).
 *
 * Views are visual renderings stored in the `views` table.
 * `View.config` stores the FULL `ViewConfiguration` — no lossy conversion needed.
 *
 * API scope: /api/v1/views (top-level, cross-workspace)
 */
import type { ViewConfiguration } from '@/types/schema'
import { authFetch } from './apiClient'

// ============================================
// Types
// ============================================

export interface View {
    id: string
    name: string
    description?: string
    contextModelId?: string
    contextModelName?: string
    workspaceId: string
    workspaceName?: string
    dataSourceId?: string
    dataSourceName?: string
    viewType: string
    /**
     * Top-level projection of `config.layoutType` — lets metadata-only
     * consumers (ViewWizard scope resolver) decide which schema scope to
     * fetch WITHOUT parsing the full config blob.
     */
    layoutType?: string
    config: Record<string, any>    // Full ViewConfiguration shape
    visibility: 'private' | 'workspace' | 'enterprise'
    createdBy?: string
    /** Human-readable creator name resolved server-side from the users table. */
    createdByName?: string
    /** Creator's email, surfaced for tooltip / hover detail. */
    createdByEmail?: string
    tags?: string[]
    isPinned: boolean
    favouriteCount: number
    isFavourited: boolean
    createdAt: string
    updatedAt: string
    deletedAt?: string | null
    /**
     * Ontology digest captured when the view was last saved. Used by the
     * wizard to detect ontology drift: if this differs from the current
     * workspace ontology digest, the UI surfaces a non-blocking warning.
     * Nullable for views created before drift tracking landed.
     */
    ontologyDigest?: string | null
}

export interface ViewCreateRequest {
    name: string
    description?: string
    contextModelId?: string
    workspaceId: string
    dataSourceId?: string
    viewType?: string
    config?: Record<string, any>
    visibility?: string
    tags?: string[]
    isPinned?: boolean
}

export interface ViewUpdateRequest {
    name?: string
    description?: string
    contextModelId?: string
    viewType?: string
    config?: Record<string, any>
    visibility?: string
    tags?: string[]
    isPinned?: boolean
}

export interface ViewListParams {
    visibility?: string
    /** Multi-visibility filter; wins over single ``visibility`` when both are set. */
    visibilityIn?: string[]
    workspaceId?: string
    /** Multi-workspace filter; wins over single ``workspaceId`` when both are set. */
    workspaceIds?: string[]
    contextModelId?: string
    dataSourceId?: string
    viewType?: string
    /** Multi-viewType filter; wins over single ``viewType`` when both are set. */
    viewTypes?: string[]
    /** Restrict to views authored by a specific user (used by "My Views"). */
    createdBy?: string
    /** Multi-creator filter; wins over single ``createdBy`` when both are set. */
    createdByIn?: string[]
    /** ISO timestamp — returns views created on or after this time. */
    createdAfter?: string
    search?: string
    /** OR semantics: match views whose tags array contains ANY of these. */
    tags?: string[]
    limit?: number
    offset?: number
    /** Return only views the current user has bookmarked/favourited. */
    favouritedOnly?: boolean
    /** Include soft-deleted views in the results. */
    includeDeleted?: boolean
    /** Return only soft-deleted views. */
    deletedOnly?: boolean
    /** Return only views that need attention (stale, broken, or inactive). */
    attentionOnly?: boolean
    /**
     * Embedded resources the server should fold into the response.
     * ``"popular"`` makes the response carry ``popular`` (the trending
     * strip) so the Explorer needs one round-trip instead of two.
     */
    include?: ('popular')[]
    /** Cap on the embedded ``popular`` list. Only honoured with ``include: ['popular']``. */
    popularLimit?: number
}

/**
 * Paginated envelope returned by ``GET /api/v1/views/``.
 *
 * ``total`` is the authoritative count of matches; ``hasMore`` and
 * ``nextOffset`` are pre-computed on the server so callers don't have
 * to infer pagination state from ``items.length >= limit``.
 */
export interface ViewListResponse {
    items: View[]
    total: number
    hasMore: boolean
    nextOffset: number | null
    /**
     * Embedded trending strip — present iff the caller passed
     * ``include: ['popular']``. Lets the Explorer get its list + popular
     * in one round-trip instead of two.
     */
    popular?: View[]
}

/** A single facet value with its row count. */
export interface ViewFacetValue {
    value: string
    count: number
}

/** A creator facet row with display metadata. */
export interface ViewFacetCreator {
    userId: string
    displayName: string
    email?: string
    count: number
}

/**
 * Catalog stats consumed by the Explorer stats bar.
 *
 * Returned by ``GET /api/v1/views/stats`` which accepts the same
 * filter params as the list endpoint — the numbers always describe
 * the currently-filtered population so the stats bar stays in sync
 * as users narrow their query.
 */
export interface ViewCatalogStats {
    total: number
    recentlyAdded: number
    needsAttention: number
    lastActivityAt: string | null
}

/**
 * Aggregate facets returned by ``GET /api/v1/views/facets``.
 *
 * Used by the Explorer to populate the Tag / View Type / Creator
 * dropdowns from the DB-wide set of values (not just the current page).
 * Facets are intentionally global so the dropdowns always show the
 * full option space — see ``getViewStats`` for filter-aware counts.
 */
export interface ViewFacetsResponse {
    tags: ViewFacetValue[]
    viewTypes: ViewFacetValue[]
    creators: ViewFacetCreator[]
}

// ============================================
// API Client
// ============================================

// Use authFetch so the JWT access token is attached to every view API
// call. Without this, the backend treats every request as anonymous,
// which breaks created_by attribution and per-user favourite flags.
const apiFetch = authFetch

// ============================================
// CRUD
// ============================================

/**
 * List views matching the given filters.
 *
 * Returns a paginated envelope with ``items`` + ``total`` + ``hasMore`` +
 * ``nextOffset``. Callers that don't need pagination metadata should
 * read ``.items`` directly rather than guessing from array length.
 *
 * Pass ``include: ['popular']`` to fold the trending strip into the
 * response (under ``popular``) so the Explorer page only makes one
 * round-trip instead of two.
 *
 * ``signal`` lets callers cancel in-flight requests on rapid filter
 * changes / unmount via a native ``AbortController`` — replaces the
 * legacy "set a cancelled flag and ignore the response" idiom.
 */
export async function listViews(
    params?: ViewListParams,
    signal?: AbortSignal,
): Promise<ViewListResponse> {
    const sp = new URLSearchParams()
    if (params?.visibilityIn && params.visibilityIn.length > 0) {
        params.visibilityIn.forEach(v => sp.append('visibilityIn', v))
    } else if (params?.visibility) {
        sp.set('visibility', params.visibility)
    }
    if (params?.workspaceIds && params.workspaceIds.length > 0) {
        params.workspaceIds.forEach(id => sp.append('workspaceIds', id))
    } else if (params?.workspaceId) {
        sp.set('workspaceId', params.workspaceId)
    }
    if (params?.contextModelId) sp.set('contextModelId', params.contextModelId)
    if (params?.dataSourceId) sp.set('dataSourceId', params.dataSourceId)
    if (params?.viewTypes && params.viewTypes.length > 0) {
        params.viewTypes.forEach(v => sp.append('viewTypes', v))
    } else if (params?.viewType) {
        sp.set('viewType', params.viewType)
    }
    if (params?.createdByIn && params.createdByIn.length > 0) {
        params.createdByIn.forEach(id => sp.append('createdByIn', id))
    } else if (params?.createdBy) {
        sp.set('createdBy', params.createdBy)
    }
    if (params?.createdAfter) sp.set('createdAfter', params.createdAfter)
    if (params?.search) sp.set('search', params.search)
    if (params?.tags) params.tags.forEach(t => sp.append('tags', t))
    if (params?.limit != null) sp.set('limit', String(params.limit))
    if (params?.offset != null) sp.set('offset', String(params.offset))
    if (params?.favouritedOnly) sp.set('favouritedOnly', 'true')
    if (params?.includeDeleted) sp.set('includeDeleted', 'true')
    if (params?.deletedOnly) sp.set('deletedOnly', 'true')
    if (params?.attentionOnly) sp.set('attentionOnly', 'true')
    if (params?.include) params.include.forEach(v => sp.append('include', v))
    if (params?.popularLimit != null) sp.set('popularLimit', String(params.popularLimit))
    const qs = sp.toString()
    return apiFetch<ViewListResponse>(
        `/api/v1/views/${qs ? `?${qs}` : ''}`,
        signal ? { signal } : undefined,
    )
}

/** List the most-favourited enterprise-visible views */
export async function listPopularViews(limit = 20): Promise<View[]> {
    return apiFetch<View[]>(`/api/v1/views/popular?limit=${limit}`)
}

/**
 * Aggregate distinct tags, view types, and creators across non-deleted views.
 *
 * Used to populate Explorer filter dropdowns from the authoritative
 * DB-wide set rather than deriving from the currently-loaded page.
 */
export async function getViewFacets(): Promise<ViewFacetsResponse> {
    return apiFetch<ViewFacetsResponse>('/api/v1/views/facets')
}

/** Subset of ``ViewListParams`` that applies to the stats endpoint. */
export type ViewStatsParams = Omit<ViewListParams, 'limit' | 'offset' | 'contextModelId'>

/**
 * Fetch the Explorer stats-bar numbers for a given filter set.
 *
 * The endpoint accepts the same filter params as ``listViews`` so the
 * stats always describe the currently-filtered population.
 */
export async function getViewStats(params?: ViewStatsParams): Promise<ViewCatalogStats> {
    const sp = new URLSearchParams()
    if (params?.visibilityIn && params.visibilityIn.length > 0) {
        params.visibilityIn.forEach(v => sp.append('visibilityIn', v))
    } else if (params?.visibility) {
        sp.set('visibility', params.visibility)
    }
    if (params?.workspaceIds && params.workspaceIds.length > 0) {
        params.workspaceIds.forEach(id => sp.append('workspaceIds', id))
    } else if (params?.workspaceId) {
        sp.set('workspaceId', params.workspaceId)
    }
    if (params?.dataSourceId) sp.set('dataSourceId', params.dataSourceId)
    if (params?.viewTypes && params.viewTypes.length > 0) {
        params.viewTypes.forEach(v => sp.append('viewTypes', v))
    } else if (params?.viewType) {
        sp.set('viewType', params.viewType)
    }
    if (params?.createdByIn && params.createdByIn.length > 0) {
        params.createdByIn.forEach(id => sp.append('createdByIn', id))
    } else if (params?.createdBy) {
        sp.set('createdBy', params.createdBy)
    }
    if (params?.createdAfter) sp.set('createdAfter', params.createdAfter)
    if (params?.search) sp.set('search', params.search)
    if (params?.tags) params.tags.forEach(t => sp.append('tags', t))
    if (params?.favouritedOnly) sp.set('favouritedOnly', 'true')
    if (params?.includeDeleted) sp.set('includeDeleted', 'true')
    if (params?.deletedOnly) sp.set('deletedOnly', 'true')
    if (params?.attentionOnly) sp.set('attentionOnly', 'true')
    const qs = sp.toString()
    return apiFetch<ViewCatalogStats>(`/api/v1/views/stats${qs ? `?${qs}` : ''}`)
}

/** Create a new view (workspaceId required) */
export async function createView(data: ViewCreateRequest): Promise<View> {
    return apiFetch<View>('/api/v1/views/', {
        method: 'POST',
        body: JSON.stringify(data),
    })
}

/** Get a single view by ID (enriched with workspace name + favourite data) */
export async function getView(viewId: string): Promise<View> {
    return apiFetch<View>(`/api/v1/views/${viewId}`)
}

/** Update an existing view */
export async function updateView(viewId: string, data: ViewUpdateRequest): Promise<View> {
    return apiFetch<View>(`/api/v1/views/${viewId}`, {
        method: 'PUT',
        body: JSON.stringify(data),
    })
}

/** Delete a view. Soft-deletes by default; pass permanent=true to remove from DB. */
export async function deleteView(viewId: string, permanent = false): Promise<void> {
    const qs = permanent ? '?permanent=true' : ''
    return apiFetch<void>(`/api/v1/views/${viewId}${qs}`, { method: 'DELETE' })
}

/** Restore a soft-deleted view */
export async function restoreView(viewId: string): Promise<View> {
    return apiFetch<View>(`/api/v1/views/${viewId}/restore`, { method: 'POST' })
}

/** Change the visibility of a view */
export async function updateViewVisibility(viewId: string, visibility: string): Promise<View> {
    return apiFetch<View>(`/api/v1/views/${viewId}/visibility`, {
        method: 'PUT',
        body: JSON.stringify({ visibility }),
    })
}

/** Favourite a view */
export async function favouriteView(viewId: string): Promise<void> {
    return apiFetch<void>(`/api/v1/views/${viewId}/favourite`, { method: 'POST' })
}

/** Unfavourite a view */
export async function unfavouriteView(viewId: string): Promise<void> {
    return apiFetch<void>(`/api/v1/views/${viewId}/favourite`, { method: 'DELETE' })
}

// ============================================
// View → ViewConfiguration converter
// ============================================

/**
 * Convert a View API response to the ViewConfiguration type
 * consumed by CanvasRouter, ViewSelector, and SidebarNav.
 *
 * View.config stores the FULL ViewConfiguration shape — we just
 * overlay the top-level identity fields.
 */
export function viewToViewConfig(view: View): ViewConfiguration {
    const cfg = view.config ?? {}
    // Derive scopeKey from the authoritative workspaceId + dataSourceId on the API
    // response, matching the format used by setActiveScopeKey() in the schema store
    // (`${wsId}/${dsId}` or `${wsId}/default`). The stored cfg.scopeKey is unreliable
    // because it was a frontend-only field and is absent on server-created views.
    const scopeKey = view.workspaceId
        ? view.dataSourceId
            ? `${view.workspaceId}/${view.dataSourceId}`
            : `${view.workspaceId}/default`
        : cfg.scopeKey ?? null
    return {
        id: view.id,
        name: view.name,
        description: view.description,
        icon: cfg.icon ?? 'Layout',
        scopeKey,
        workspaceId: view.workspaceId,
        dataSourceId: view.dataSourceId ?? null,
        workspaceName: view.workspaceName,
        isFavourited: view.isFavourited,
        content: cfg.content ?? {
            visibleEntityTypes: [],
            visibleRelationshipTypes: [],
            defaultDepth: 5,
            maxDepth: 10,
            rootEntityTypes: ['domain'],
        },
        layout: cfg.layout ?? {
            type: (view.viewType ?? 'graph') as any,
            lod: { enabled: false, levels: [] },
        },
        filters: cfg.filters ?? {
            entityTypeFilters: [],
            fieldFilters: [],
            searchableFields: [],
            quickFilters: [],
        },
        entityOverrides: cfg.entityOverrides ?? {},
        grouping: cfg.grouping,
        isDefault: false,
        isPublic: view.visibility !== 'private',
        createdBy: view.createdBy ?? 'user',
        createdAt: view.createdAt,
        updatedAt: view.updatedAt,
    }
}
