/**
 * traceApi - Client for the v2 lineage trace endpoints.
 *
 * Wire format is camelCase end-to-end (the backend uses
 * `populate_by_name=True` + Field aliases to emit camelCase to clients).
 * These types match the Pydantic models in
 * `backend/common/models/graph.py` (TraceRequest, TraceResultV2, TraceData,
 * TraceMeta, TraceEdge, TraceExpandRequest, TraceDelta, TraceDeltaData,
 * TraceErrorBody) verified at the field level.
 *
 * Endpoints:
 *   POST /api/v2/{ws_id}/graph/trace        — initial trace
 *   POST /api/v2/{ws_id}/graph/trace/expand — drill-down delta
 *
 * Both return a `{data, meta}` envelope on success and `{error: {code,
 * message, details}, meta}` on 4xx/5xx. Non-2xx responses are translated
 * into a `TraceApiError` so callers can branch on `code`.
 */

import { fetchWithTimeout } from './fetchWithTimeout'
import type { GraphNode } from '@/providers/GraphDataProvider'

// ============================================
// Wire types — camelCase, matching backend aliases
// ============================================

export type TraceDirection = 'upstream' | 'downstream' | 'both'

export type TargetLevelMode =
  | 'top'
  | 'top_minus_1'
  | 'top_minus_2'
  | 'focus_level'
  | 'focus_minus_1'
  | 'focus_plus_1'

export type TraceRegime = 'materialized' | 'runtime' | 'demoted'

export type TargetLevelSource = 'request' | 'request_mode' | 'workspace' | 'ontology_default'

/**
 * Initial trace request body. The only required field is `urn`; all other
 * fields fall back to server / workspace / ontology defaults via the
 * priority chain documented in plan §1.5.
 */
export interface TraceRequest {
  urn: string
  direction?: TraceDirection
  upstreamDepth?: number
  downstreamDepth?: number
  /** Absolute ontology level. Mutually exclusive with `targetLevelMode`. */
  targetLevel?: number | null
  /** Semantic shortcut. Mutually exclusive with `targetLevel`. */
  targetLevelMode?: TargetLevelMode | null
  /** Whitelist of lineage edge types to include. `null` / omitted = all ontology lineage types. */
  lineageEdgeTypes?: string[] | null
  /** Whitelist of containment edge types. `null` / omitted = ontology default. UI passes null. */
  containmentEdgeTypes?: string[] | null
  includeContainment?: boolean
  limit?: number
  cursor?: string | null
  fields?: 'default' | 'full'
}

/**
 * Edge in a trace response. Extends GraphEdge with embedded aggregation
 * metadata (no separate edgeMeta map). Containment edges are emitted
 * inline and distinguished by `isContainment=true`.
 */
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

/** Top-level data block of a TraceResultV2 envelope. */
export interface TraceData {
  focusUrn: string
  focusLevel: number
  targetLevel: number
  nodes: GraphNode[]
  edges: TraceEdge[]
  upstreamUrns: string[]
  downstreamUrns: string[]
  expandableUrns: string[]
  /** urn -> child count at next-deeper ontology level; drives the +N badge. */
  aggregatedChildCount: Record<string, number>
  /** Ancestors whose lineage was inherited (ordered nearest-first). Empty when not inherited. */
  inheritedFrom: string[]
  hasMore: boolean
  nextCursor: string | null
}

/** Sidecar metadata for trace responses. */
export interface TraceMeta {
  regime: TraceRegime
  /** 'hit' | 'miss' | 'bypass' */
  cacheStatus: string
  ontologyDigest: string
  traceSessionId: string | null
  targetLevel: number
  targetLevelSource: TargetLevelSource
  queryMs: number
  materializedHitRate: number
  warnings: string[]
  notices: string[]
}

/** Canonical envelope returned by POST /trace. */
export interface TraceResultV2 {
  data: TraceData
  meta: TraceMeta
}

export interface TraceExpandRequest {
  traceSessionId: string
  expandUrn: string
  depthDelta?: number
  newTargetLevel?: number
}

export interface TraceDeltaData {
  addedNodes: GraphNode[]
  removedEdges: string[]
  addedEdges: TraceEdge[]
  newExpandableUrns: string[]
  aggregatedChildCount: Record<string, number>
}

export interface TraceDelta {
  data: TraceDeltaData
  meta: TraceMeta
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
