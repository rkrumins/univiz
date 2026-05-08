import { unwrapEnvelope } from '@/services/cacheEnvelope'
import { getCircuitBreaker, type CircuitBreaker } from '@/services/circuitBreaker'
import { fetchWithTimeout } from '@/services/fetchWithTimeout'

import type {
    GraphDataProvider,
    GraphNode,
    GraphEdge,
    EntityType,
    URN,
    NodeQuery,
    EdgeQuery,
    LineageResult,
    ContainmentResult,
    TraceOptions,
    TraceV2Request,
    TraceV2Result,
    ExpandAggregatedRequest,
    LayerAssignmentRequest,
    LayerAssignmentResult,
    GraphSchemaStats,
    OntologyMetadata,
    GraphSchema,
    AggregatedEdgeRequest,
    AggregatedEdgeResult,
    CreateNodeRequest,
    CreateNodeResult,
    TopLevelNodesQuery,
    TopLevelNodesResult,
} from './GraphDataProvider'
import type { TraceMeta } from '@/services/traceApi'

// Wire shape from POST /trace/v2 — `upstreamUrns`/`downstreamUrns` arrive as
// JSON arrays (Pydantic serializes Set as list); we re-hydrate to Set on read.
interface RawTraceV2Result {
    nodes: GraphNode[]
    edges: GraphEdge[]
    containmentEdges: GraphEdge[]
    upstreamUrns: URN[]
    downstreamUrns: URN[]
    focus: { urn: URN; level: number; entityType: string }
    effectiveLevel: number
    isInherited: boolean
    inheritedFromUrn?: string | null
    truncated: boolean
    truncationReason?: string | null
    /** Optional sidecar metadata — only present when the v2 envelope emits it. */
    meta?: TraceMeta
}

function normalizeTraceV2(raw: RawTraceV2Result): TraceV2Result {
    return {
        ...raw,
        upstreamUrns: new Set(raw.upstreamUrns ?? []),
        downstreamUrns: new Set(raw.downstreamUrns ?? []),
        containmentEdges: raw.containmentEdges ?? [],
        meta: raw.meta,
    }
}

const API_BASE = '/api/v1'

export interface RemoteGraphProviderOptions {
    /** Workspace ID. When set, routes through /v1/{ws_id}/graph/... */
    workspaceId?: string
    /** Data source ID. When set, appended as ?dataSourceId= to workspace-scoped routes. */
    dataSourceId?: string
    /** @deprecated Legacy connection ID. Use workspaceId instead. */
    connectionId?: string
}

export class RemoteGraphProvider implements GraphDataProvider {
    readonly name = 'RemoteGraphProvider'

    private readonly workspaceId?: string
    private readonly dataSourceId?: string
    private readonly connectionId?: string
    private readonly circuitBreaker: CircuitBreaker

    /** In-flight request deduplication: identical concurrent requests share one Promise */
    private _inflight = new Map<string, Promise<unknown>>()

    /** Short-lived response cache for GET requests (prevents rapid re-fetches during re-renders) */
    private _responseCache = new Map<string, { data: unknown; ts: number }>()
    private static RESPONSE_CACHE_TTL = 2000 // 2 seconds

    constructor(options?: RemoteGraphProviderOptions) {
        this.workspaceId = options?.workspaceId
        this.dataSourceId = options?.dataSourceId
        this.connectionId = options?.connectionId
        this.circuitBreaker = getCircuitBreaker(this.workspaceId, this.dataSourceId)
    }

    // ==========================================
    // URL builder — workspace path or legacy query param
    // ==========================================

    private buildUrl(path: string, extraParams?: Record<string, string>): string {
        // Workspace-scoped: /api/v1/{ws_id}/graph/...
        const base = this.workspaceId
            ? `/api/v1/${this.workspaceId}/graph`
            : API_BASE

        const url = new URL(`${base}${path}`, window.location.origin)

        // Data source targeting within a workspace
        if (this.workspaceId && this.dataSourceId) {
            url.searchParams.set('dataSourceId', this.dataSourceId)
        }

        // Legacy fallback: append connectionId as query param
        if (!this.workspaceId && this.connectionId) {
            url.searchParams.set('connectionId', this.connectionId)
        }

        if (extraParams) {
            Object.entries(extraParams).forEach(([k, v]) => url.searchParams.set(k, v))
        }
        return url.pathname + url.search
    }

    // ==========================================
    // Internal Fetch Helper
    // ==========================================

    private async fetch<T>(path: string, options?: RequestInit & { extraParams?: Record<string, string>, timeoutMs?: number }): Promise<T> {
        const { extraParams, timeoutMs, ...fetchOptions } = options ?? {}
        const method = (fetchOptions.method ?? 'GET').toUpperCase()
        const url = this.buildUrl(path, extraParams)
        const cacheKey = `${method}:${url}:${fetchOptions.body ?? ''}`

        // Check short-lived response cache for GET requests
        if (method === 'GET') {
            const cached = this._responseCache.get(cacheKey)
            if (cached && Date.now() - cached.ts < RemoteGraphProvider.RESPONSE_CACHE_TTL) {
                return cached.data as T
            }
        }

        // Deduplicate identical in-flight requests
        const existing = this._inflight.get(cacheKey)
        if (existing) return existing as Promise<T>

        const promise = this._doFetch<T>(url, fetchOptions, method, cacheKey, timeoutMs)
        this._inflight.set(cacheKey, promise)
        return promise
    }

    private async _doFetch<T>(url: string, fetchOptions: RequestInit, method: string, cacheKey: string, timeoutMs?: number): Promise<T> {
        // Circuit breaker: fail fast if provider is known-dead
        if (!this.circuitBreaker.canRequest()) {
            this._inflight.delete(cacheKey)
            throw new Error('Provider unavailable (circuit open)')
        }

        try {
            // Use the global default timeout (5s). The graph endpoints
            // are all cache-only post-insights-refactor — they read from
            // Postgres and respond in <100ms; an empty/computing cache
            // surfaces as `meta.status="computing"` in the body, never
            // as a timeout. The legacy 12s window was sized for live
            // provider calls that no longer happen here.
            const response = await fetchWithTimeout(url, {
                ...fetchOptions,
                ...(timeoutMs !== undefined ? { timeoutMs } : {}),
                headers: {
                    'Content-Type': 'application/json',
                    ...fetchOptions?.headers,
                },
            })

            if (!response.ok) {
                const errorText = await response.text()
                const error = new Error(`API Error ${response.status}: ${errorText || response.statusText}`)
                // 5xx errors indicate provider/backend failure — feed circuit breaker
                if (response.status >= 500) {
                    // Honor Retry-After header from backend (sent on 503 ProviderUnavailable)
                    const retryAfter = response.headers.get('Retry-After')
                    const retryAfterMs = retryAfter ? parseInt(retryAfter, 10) * 1000 : undefined
                    this.circuitBreaker.recordFailure(
                        retryAfterMs && !isNaN(retryAfterMs) ? retryAfterMs : undefined,
                    )
                }
                throw error
            }

            const data = await response.json() as T

            // Cache GET responses briefly to handle rapid re-renders
            if (method === 'GET') {
                this._responseCache.set(cacheKey, { data, ts: Date.now() })
            }

            this.circuitBreaker.recordSuccess()
            return data
        } catch (err) {
            if (err instanceof TypeError) {
                this.circuitBreaker.recordFailure()
                if (err.message.includes('timed out')) {
                    throw new Error(`Request timed out: ${method} ${url}`)
                }
            }
            throw err
        } finally {
            this._inflight.delete(cacheKey)
        }
    }

    // ==========================================
    // Node Operations
    // ==========================================

    async getNode(urn: URN): Promise<GraphNode | null> {
        try {
            return await this.fetch<GraphNode>(`/nodes/${encodeURIComponent(urn)}`)
        } catch (error) {
            if (error instanceof Error && error.message.includes('404')) {
                return null
            }
            throw error
        }
    }

    async getNodes(query: NodeQuery): Promise<GraphNode[]> {
        // Use POST for complex queries
        return await this.fetch<GraphNode[]>('/nodes/query', {
            method: 'POST',
            body: JSON.stringify({ query }),
        })
    }

    async searchNodes(query: string, limit = 10): Promise<GraphNode[]> {
        return await this.fetch<GraphNode[]>('/search', {
            method: 'POST',
            body: JSON.stringify({ query, limit }),
        })
    }

    // ==========================================
    // Edge Operations
    // ==========================================

    async getEdges(query: EdgeQuery): Promise<GraphEdge[]> {
        // Use POST for complex queries (especially multiple URNs)
        return await this.fetch<GraphEdge[]>('/edges/query', {
            method: 'POST',
            body: JSON.stringify({ query }),
        })
    }

    async getEdgesBetween(urns: URN[], edgeTypes?: string[], limit?: number): Promise<GraphEdge[]> {
        if (urns.length === 0) return []
        return await this.fetch<GraphEdge[]>('/edges/between', {
            method: 'POST',
            body: JSON.stringify({ urns, edgeTypes, limit }),
        })
    }

    // ==========================================
    // Containment Hierarchy
    // ==========================================

    async getChildren(
        parentUrn: URN,
        options?: {
            entityTypes?: EntityType[]
            edgeTypes?: string[]
            searchQuery?: string
            offset?: number
            limit?: number
            sortProperty?: string | null
            cursor?: string | null
        }
    ): Promise<GraphNode[]> {
        const params = new URLSearchParams()
        if (options?.offset) params.append('offset', String(options.offset))
        if (options?.limit) params.append('limit', String(options.limit))
        if (options?.searchQuery) params.append('searchQuery', options.searchQuery)
        if (options?.sortProperty !== undefined) params.append('sortProperty', options.sortProperty ?? '')
        if (options?.cursor) params.append('cursor', options.cursor)

        if (options?.edgeTypes?.length) {
            options.edgeTypes.forEach(t => params.append('edgeTypes', t))
        }

        return await this.fetch<GraphNode[]>(`/nodes/${encodeURIComponent(parentUrn)}/children?${params.toString()}`)
    }

    async getChildrenWithEdges(
        parentUrn: URN,
        options?: {
            edgeTypes?: string[]
            lineageEdgeTypes?: string[]
            searchQuery?: string
            offset?: number
            limit?: number
            includeLineageEdges?: boolean
            sortProperty?: string | null
            cursor?: string | null
        }
    ): Promise<{
        children: GraphNode[]
        containmentEdges: GraphEdge[]
        lineageEdges: GraphEdge[]
        totalChildren: number
        hasMore: boolean
        nextCursor?: string | null
    }> {
        const params = new URLSearchParams()
        if (options?.offset) params.append('offset', String(options.offset))
        if (options?.limit) params.append('limit', String(options.limit))
        if (options?.searchQuery) params.append('searchQuery', options.searchQuery)
        if (options?.includeLineageEdges === false) params.append('includeLineageEdges', 'false')
        if (options?.sortProperty !== undefined) params.append('sortProperty', options.sortProperty ?? '')
        if (options?.cursor) params.append('cursor', options.cursor)

        if (options?.edgeTypes?.length) {
            options.edgeTypes.forEach(t => params.append('edgeTypes', t))
        }
        if (options?.lineageEdgeTypes?.length) {
            options.lineageEdgeTypes.forEach(t => params.append('lineageEdgeTypes', t))
        }

        return await this.fetch(`/nodes/${encodeURIComponent(parentUrn)}/children-with-edges?${params.toString()}`)
    }

    async getParent(childUrn: URN): Promise<GraphNode | null> {
        return await this.fetch<GraphNode | null>(`/nodes/${encodeURIComponent(childUrn)}/parent`)
    }

    async getAncestors(urn: URN): Promise<GraphNode[]> {
        return await this.fetch<GraphNode[]>(`/nodes/${encodeURIComponent(urn)}/ancestors`)
    }

    async getDescendants(urn: URN, depth = 10): Promise<GraphNode[]> {
        return await this.fetch<GraphNode[]>(`/nodes/${encodeURIComponent(urn)}/descendants?depth=${depth}`)
    }

    async getTopLevelNodes(query: TopLevelNodesQuery): Promise<TopLevelNodesResult> {
        const params = new URLSearchParams()
        // Don't swallow an explicit limit of 0 — backend clamps to [1,1000].
        if (query.limit !== undefined) params.append('limit', String(query.limit))
        if (query.searchQuery) params.append('searchQuery', query.searchQuery)
        if (query.cursor) params.append('cursor', query.cursor)
        if (query.includeChildCount === false) params.append('includeChildCount', 'false')
        if (query.entityTypes?.length) {
            query.entityTypes.forEach(t => params.append('entityTypes', t))
        }
        // Backend returns camelCase via response_model_by_alias=True, so the
        // wire shape already matches TopLevelNodesResult one-to-one.
        return await this.fetch<TopLevelNodesResult>(
            `/nodes/top-level?${params.toString()}`,
        )
    }

    async getContainment(params: { parentUrn: URN; searchQuery?: string; limit?: number }): Promise<ContainmentResult> {
        const { parentUrn, searchQuery, limit = 50 } = params
        const [parent, children] = await Promise.all([
            this.getNode(parentUrn),
            this.getChildren(parentUrn, { limit }),
        ])
        const filtered = searchQuery?.trim()
            ? children.filter(
                (c) =>
                    c.displayName?.toLowerCase().includes(searchQuery.toLowerCase()) ||
                    c.urn?.toLowerCase().includes(searchQuery.toLowerCase())
            )
            : children
        return {
            parent,
            children: filtered.slice(0, limit),
            hasNestedChildren: filtered.some((c) => (c.childCount ?? 0) > 0),
        }
    }

    // ==========================================
    // Lineage Traversal
    // ==========================================

    async getUpstream(
        urn: URN,
        depth: number,
        options?: TraceOptions
    ): Promise<LineageResult> {
        return this.fetch<LineageResult>('/trace', {
            method: 'POST',
            body: JSON.stringify({
                urn,
                direction: 'upstream',
                upstreamDepth: depth,
                downstreamDepth: 0,
                granularity: options?.granularity ?? 'table',
                aggregateEdges: options?.aggregateEdges ?? true,
                excludeContainmentEdges: options?.excludeContainmentEdges ?? true,
                includeInheritedLineage: options?.includeInheritedLineage ?? true,
            })
        })
    }

    async getDownstream(
        urn: URN,
        depth: number,
        options?: TraceOptions
    ): Promise<LineageResult> {
        return this.fetch<LineageResult>('/trace', {
            method: 'POST',
            body: JSON.stringify({
                urn,
                direction: 'downstream',
                upstreamDepth: 0,
                downstreamDepth: depth,
                granularity: options?.granularity ?? 'table',
                aggregateEdges: options?.aggregateEdges ?? true,
                excludeContainmentEdges: options?.excludeContainmentEdges ?? true,
                includeInheritedLineage: options?.includeInheritedLineage ?? true,
            })
        })
    }

    async getFullLineage(
        urn: URN,
        upstreamDepth: number,
        downstreamDepth: number,
        options?: TraceOptions
    ): Promise<LineageResult> {
        return this.fetch<LineageResult>('/trace', {
            method: 'POST',
            body: JSON.stringify({
                urn,
                direction: 'both',
                upstreamDepth,
                downstreamDepth,
                granularity: options?.granularity ?? 'table',
                aggregateEdges: options?.aggregateEdges ?? true,
                excludeContainmentEdges: options?.excludeContainmentEdges ?? true,
                includeInheritedLineage: options?.includeInheritedLineage ?? true,
                // Ontology-driven: pass lineage edge type filter to backend
                ...(options?.lineageEdgeTypes?.length ? { lineageEdgeTypes: options.lineageEdgeTypes } : {}),
            })
        })
    }

    /**
     * Trace v2 — POST /trace/v2. Server-side level filter via n.level index;
     * per-hop set-based BFS in Cypher. See plan: trace refactor.
     *
     * Hard caps (max_nodes/timeout_ms) live on the server; truncation surfaces
     * as `truncated: true` in the response. Always HTTP 200 unless input is
     * malformed — clients render partial results without retrying.
     */
    async traceAtLevel(request: TraceV2Request): Promise<TraceV2Result> {
        const raw = await this.fetch<RawTraceV2Result>('/trace/v2', {
            method: 'POST',
            body: JSON.stringify(request),
        })
        return normalizeTraceV2(raw)
    }

    async expandAggregated(request: ExpandAggregatedRequest): Promise<TraceV2Result> {
        const raw = await this.fetch<RawTraceV2Result>('/trace/expand', {
            method: 'POST',
            body: JSON.stringify(request),
        })
        return normalizeTraceV2(raw)
    }

    // ==========================================
    // Layer/Classification Queries
    // ==========================================

    async getNodesByLayer(layerId: string): Promise<GraphNode[]> {
        return await this.fetch<GraphNode[]>(`/nodes/by-layer/${encodeURIComponent(layerId)}`)
    }

    async getNodesByTag(tag: string): Promise<GraphNode[]> {
        return await this.fetch<GraphNode[]>(`/nodes/by-tag/${encodeURIComponent(tag)}`)
    }

    // ==========================================
    // Metadata Operations
    // ==========================================

    async getEntityTypes(): Promise<EntityType[]> {
        return await this.fetch<EntityType[]>('/metadata/entity-types')
    }

    async getTags(): Promise<string[]> {
        return await this.fetch<string[]>('/metadata/tags')
    }

    /**
     * The four cache-only endpoints below (`/stats`, `/introspection`,
     * `/metadata/ontology`, `/metadata/schema`) return the canonical
     * `{data, meta}` envelope. We unwrap to the raw payload here so
     * callers stay envelope-unaware. Consumers that need cache
     * freshness for UI banners should hit a separate helper that
     * preserves the envelope.
     *
     * `unwrapEnvelope` returns `null` when `meta.status === 'error'`,
     * which we let propagate so the circuit breaker / retry logic
     * upstream can react. For `computing` / `partial` states the
     * payload is genuinely null/synthetic; downstream code already
     * handles that path (e.g. SchemaScope error UI).
     */
    async getStats(): Promise<{
        nodeCount: number
        edgeCount: number
        entityTypeCounts: Record<EntityType, number>
    }> {
        const raw = await this.fetch<unknown>('/stats')
        const data = unwrapEnvelope<{
            nodeCount: number
            edgeCount: number
            entityTypeCounts: Record<EntityType, number>
        }>(raw)
        if (!data) {
            throw new Error('Stats unavailable: cache miss or backend error')
        }
        return data
    }

    async getSchemaStats(): Promise<GraphSchemaStats> {
        const raw = await this.fetch<unknown>('/introspection')
        const data = unwrapEnvelope<GraphSchemaStats>(raw)
        if (!data) {
            throw new Error('Schema stats unavailable: cache miss or backend error')
        }
        return data
    }

    async getOntologyMetadata(): Promise<OntologyMetadata> {
        const raw = await this.fetch<unknown>('/metadata/ontology')
        const data = unwrapEnvelope<OntologyMetadata>(raw)
        if (!data) {
            throw new Error('Ontology metadata unavailable: cache miss or backend error')
        }
        return data
    }

    // ==========================================
    // Assignment Operations
    // ==========================================

    async computeLayerAssignments(request: LayerAssignmentRequest): Promise<LayerAssignmentResult> {
        return await this.fetch<LayerAssignmentResult>('/assignments/compute', {
            method: 'POST',
            body: JSON.stringify(request)
        })
    }

    // ==========================================
    // Schema Operations (Dynamic Schema Loading)
    // ==========================================

    async getFullSchema(dataSourceId?: string): Promise<GraphSchema> {
        const raw = await this.fetch<unknown>('/metadata/schema', {
            extraParams: dataSourceId ? { dataSourceId } : undefined,
        })
        const data = unwrapEnvelope<GraphSchema>(raw)
        if (!data) {
            throw new Error('Graph schema unavailable: cache miss or backend error')
        }
        return data
    }

    // ==========================================
    // Aggregated Edge Operations
    // ==========================================

    async getAggregatedEdges(request: AggregatedEdgeRequest): Promise<AggregatedEdgeResult> {
        // Aligns with backend HTTP_TIMEOUT_AGGREGATION_SECS (45s) for the
        // aggregated-edges route — the 8s default is sized for cache-hit
        // graph endpoints and aborts legitimately-slow Cypher reads.
        return await this.fetch<AggregatedEdgeResult>('/edges/aggregated', {
            method: 'POST',
            body: JSON.stringify(request),
            timeoutMs: 45_000,
        })
    }

    // ==========================================
    // Node Creation
    // ==========================================

    async createNode(request: CreateNodeRequest): Promise<CreateNodeResult> {
        return await this.fetch<CreateNodeResult>('/nodes/create', {
            method: 'POST',
            body: JSON.stringify(request)
        })
    }
}
