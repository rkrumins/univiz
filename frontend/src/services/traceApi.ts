/**
 * traceApi - Client for the v2 lineage trace endpoints.
 *
 * Wire format is camelCase end-to-end (the backend uses
 * `populate_by_name=True` + Field aliases to emit camelCase to clients).
 * These types match the Pydantic models in
 * `backend/common/models/graph.py` — TraceRequest, TraceResultV2,
 * TraceMeta, TraceExpandRequest, TraceDelta — at the field level.
 *
 * Endpoints:
 *   POST /api/v2/{ws_id}/graph/trace        — initial trace
 *   POST /api/v2/{ws_id}/graph/trace/expand — drill-down delta
 *
 * Both return a flat TraceResultV2 / TraceDelta shape (TraceResult fields
 * inline + a `meta` sidecar). On 4xx/5xx the body is `{error: {code,
 * message, details}, meta: {...}}` and is translated into a TraceApiError
 * so callers can branch on `code`.
 */

import { fetchWithTimeout } from './fetchWithTimeout'
import type { GraphNode, GraphEdge } from '@/providers/GraphDataProvider'

// ============================================
// Wire types — camelCase, matching backend aliases
// ============================================

export type TraceDirection = 'upstream' | 'downstream' | 'both'

/**
 * Server-side trace regime. "skeleton" is the initial /trace call;
 * "expand" is the response to /trace/expand drill-down.
 *
 * The remaining members ("materialized"/"runtime"/"demoted") describe a
 * Phase 1.5 regime that was never emitted by the backend; kept in the
 * union so the dead `useLineageTrace` test compiles.
 */
export type TraceRegime =
  | 'skeleton'
  | 'expand'
  /** @deprecated Never emitted by backend. */
  | 'materialized'
  /** @deprecated Never emitted by backend. */
  | 'runtime'
  /** @deprecated Never emitted by backend. */
  | 'demoted'

/**
 * Initial trace request body. The only required field is `urn`; all other
 * fields fall back to server / workspace / ontology defaults via the
 * resolution chain in the backend `ContextEngine.get_trace_v2`.
 *
 * `level` accepts:
 *   - 0 (DEFAULT) — top-level Domain skeleton
 *   - integer N — literal ontology level
 *   - entity-type-id string — resolved to that type's level server-side
 *   - "auto" — clamped to 0 in V2
 */
export interface TraceRequest {
  urn: string
  direction?: TraceDirection
  upstreamDepth?: number
  downstreamDepth?: number
  level?: number | string
  lineageEdgeTypes?: string[] | null
  includeContainmentEdges?: boolean
  includeInheritedLineage?: boolean
  includeAncestorChain?: boolean
}

/** Focus of a trace — the URN the user clicked, with its resolved level
 *  and entity type. */
export interface TraceFocus {
  urn: string
  level: number
  entityType: string
}

/** A node whose AGGREGATED out-degree exceeded TRACE_DEGREE_CAP. The
 *  frontend uses `total - shown` to render a "+N more" chip. */
export interface MegaNodeInfo {
  urn: string
  shown: number
  total: number
  direction: 'upstream' | 'downstream'
}

/**
 * Sidecar metadata. Mirrors backend `TraceMeta` field-for-field.
 *
 * The block at the bottom (`cacheStatus`, `targetLevel`, etc.) describes a
 * Phase 1.5 envelope the backend never shipped — kept as optional/deprecated
 * so the dead `useLineageTrace.ts` module still compiles. They are
 * `undefined` at runtime.
 */
export interface TraceMeta {
  regime: TraceRegime
  effectiveLevel: number
  /** "max_nodes" | "timeout" | "degree_cap" | "cycle_detected" | "orphan" | null */
  truncationReason?: string | null
  cypherMs: number
  nodeCount: number
  edgeCount: number
  /** Set when orphan-fallback fires — the highest level actually reached. */
  fallbackLevel?: number | null
  megaNodes: MegaNodeInfo[]
  /** Informational correlation ID; not used to look up server state. */
  traceSessionId?: string | null
  ontologyDigest?: string | null

  // === Legacy (Phase 1.5 envelope; never set by backend) ===
  /** @deprecated Never set by backend; reads as undefined. */
  cacheStatus?: string
  /** @deprecated Never set by backend. Use `effectiveLevel`. */
  targetLevel?: number
  /** @deprecated Never set by backend. */
  targetLevelSource?: TargetLevelSource
  /** @deprecated Never set by backend. Use `cypherMs`. */
  queryMs?: number
  /** @deprecated Never set by backend. */
  materializedHitRate?: number
  /** @deprecated Never set by backend. */
  warnings?: string[]
  /** @deprecated Never set by backend. */
  notices?: string[]
}

/**
 * Canonical envelope returned by POST /trace. FLAT shape — TraceResult
 * fields inline + a `meta` sidecar. Matches backend `TraceResultV2`.
 *
 * The optional `data` field is a Phase 1.5 wrapper that was never delivered
 * by the backend; it's typed only so `useLineageTrace.ts` (dead code)
 * compiles.
 */
export interface TraceResultV2 {
  nodes: GraphNode[]
  edges: GraphEdge[]
  containmentEdges: GraphEdge[]
  upstreamUrns: string[]
  downstreamUrns: string[]
  focus: TraceFocus
  effectiveLevel: number
  isInherited: boolean
  inheritedFromUrn?: string | null
  truncated: boolean
  /** "max_nodes" | "timeout" | "degree_cap" | "cycle_detected" | "orphan" | null */
  truncationReason?: string | null
  meta: TraceMeta

  /**
   * @deprecated Phase 1.5 envelope; never populated at runtime. Typed as
   * required so the dead `useLineageTrace.ts` module can dereference it
   * without strict-null-check errors. At runtime this field is undefined.
   */
  data: TraceData
}

/**
 * Request body for POST /trace/expand. Stateless — `traceSessionId` is
 * informational only. The `expandUrn`/`depthDelta`/`newTargetLevel` fields
 * describe a Phase 1.5 surface that was never wired; retained as optional
 * so dead `useLineageTrace.ts` keeps compiling.
 */
export interface TraceExpandRequest {
  sourceUrn?: string
  targetUrn?: string
  nextLevel?: number | string
  lineageEdgeTypes?: string[] | null
  includeContainmentEdges?: boolean
  traceSessionId?: string | null

  /** @deprecated Phase 1.5; never honored. */
  expandUrn?: string
  /** @deprecated Phase 1.5; never honored. */
  depthDelta?: number
  /** @deprecated Phase 1.5; never honored. */
  newTargetLevel?: number
}

/**
 * Response to POST /trace/expand. Same shape as TraceResultV2 with
 * `meta.regime == "expand"`. Additionally carries an optional `data`
 * (Phase 1.5; not used) for the dead-code module.
 */
export interface TraceDelta extends Omit<TraceResultV2, 'data'> {
  /**
   * @deprecated Phase 1.5 delta wrapper; never populated at runtime. Typed
   * as required (with both TraceData and TraceDeltaData fields) so the
   * dead `useLineageTrace.ts` module's `applyDelta` compiles.
   */
  data: TraceData & TraceDeltaData
}

export interface TraceErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
}

/** Envelope returned for any 4xx/5xx response from trace endpoints. */
export interface TraceErrorEnvelope {
  error: TraceErrorBody
  meta?: Record<string, unknown>
}

// ============================================
// Legacy types — Phase 1.5 envelope that never shipped.
// Retained ONLY so `useLineageTrace.ts` (dead code, zero production
// consumers) keeps compiling. Do not use in new code. Slated for
// deletion together with `useLineageTrace.ts`.
// ============================================

/** @deprecated Phase 1.5 envelope; the wire shape is flat. Use TraceResultV2. */
export interface TraceEdge {
  id: string
  sourceUrn: string
  targetUrn: string
  edgeType: string
  confidence?: number | null
  properties?: Record<string, unknown>
  isAggregated: boolean
  weight: number
  sourceEdgeTypes: string[]
  underlyingPairs: number
  /** 'materialized' | 'trace_time' */
  source: string
  isContainment: boolean
}

/** @deprecated Phase 1.5 envelope; the wire shape is flat. */
export interface TraceData {
  focusUrn: string
  focusLevel: number
  targetLevel: number
  nodes: GraphNode[]
  edges: TraceEdge[]
  upstreamUrns: string[]
  downstreamUrns: string[]
  expandableUrns: string[]
  aggregatedChildCount: Record<string, number>
  inheritedFrom: string[]
  hasMore: boolean
  nextCursor: string | null
}

/** @deprecated Phase 1.5 envelope; the wire shape is flat. */
export interface TraceDeltaData {
  addedNodes: GraphNode[]
  removedEdges: string[]
  addedEdges: TraceEdge[]
  newExpandableUrns: string[]
  aggregatedChildCount: Record<string, number>
}

/** @deprecated Never delivered by the backend. */
export type TargetLevelMode =
  | 'top'
  | 'top_minus_1'
  | 'top_minus_2'
  | 'focus_level'
  | 'focus_minus_1'
  | 'focus_plus_1'

/** @deprecated Never delivered by the backend. */
export type TargetLevelSource = 'request' | 'request_mode' | 'workspace' | 'ontology_default'

// ============================================
// Error type
// ============================================

/**
 * Thrown when a trace endpoint returns a non-2xx response. Preserves the
 * error envelope's `code/message/details` so callers can branch on the
 * machine-readable code (e.g. `trace_session_expired` → re-trace) rather
 * than string-matching the message.
 */
export class TraceApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details: Record<string, unknown>

  constructor(code: string, message: string, status: number, details?: Record<string, unknown>) {
    super(message)
    this.name = 'TraceApiError'
    this.code = code
    this.status = status
    this.details = details ?? {}
  }
}

// ============================================
// Internal helpers
// ============================================

const API_BASE = '/api/v2'

function tracePath(wsId: string): string {
  return `${API_BASE}/${encodeURIComponent(wsId)}/graph/trace`
}

function expandPath(wsId: string): string {
  return `${API_BASE}/${encodeURIComponent(wsId)}/graph/trace/expand`
}

async function parseError(res: Response): Promise<TraceApiError> {
  // Best-effort parse of the standardized error envelope. If the body
  // isn't JSON or doesn't match the envelope shape, fall back to a
  // generic code/message so callers always get a TraceApiError.
  let code = 'trace_unknown_error'
  let message = res.statusText || `HTTP ${res.status}`
  let details: Record<string, unknown> = {}

  try {
    const body = (await res.json()) as TraceErrorEnvelope | { detail?: string }
    if (body && typeof body === 'object' && 'error' in body && body.error) {
      code = body.error.code || code
      message = body.error.message || message
      details = body.error.details ?? {}
    } else if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
      // FastAPI default error shape — keep as fallback.
      message = body.detail
    }
  } catch {
    // Non-JSON body. Leave defaults.
  }

  return new TraceApiError(code, message, res.status, details)
}

interface RequestOptions {
  signal?: AbortSignal
  idempotencyKey?: string
}

async function postJson<TBody, TResult>(
  url: string,
  body: TBody,
  options?: RequestOptions,
): Promise<TResult> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }
  if (options?.idempotencyKey) {
    headers['Idempotency-Key'] = options.idempotencyKey
  }

  const res = await fetchWithTimeout(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: options?.signal,
  })

  if (!res.ok) {
    throw await parseError(res)
  }

  return (await res.json()) as TResult
}

// ============================================
// Public API
// ============================================

/**
 * POST /api/v2/{ws_id}/graph/trace — initial trace.
 *
 * The minimum valid body is `{urn}`; the server resolves direction,
 * depths, target level, and lineage edge types from request defaults +
 * workspace settings + ontology fallbacks.
 */
export async function postTrace(
  wsId: string,
  body: TraceRequest,
  options?: RequestOptions,
): Promise<TraceResultV2> {
  return postJson<TraceRequest, TraceResultV2>(tracePath(wsId), body, options)
}

/**
 * POST /api/v2/{ws_id}/graph/trace/expand — drill-down delta.
 *
 * The session-stored original request body (held server-side, keyed by
 * traceSessionId) provides the trace context. Returns the delta to merge
 * into the existing TraceResultV2 via `applyDelta`.
 */
export async function postTraceExpand(
  wsId: string,
  body: TraceExpandRequest,
  options?: RequestOptions,
): Promise<TraceDelta> {
  return postJson<TraceExpandRequest, TraceDelta>(expandPath(wsId), body, options)
}
