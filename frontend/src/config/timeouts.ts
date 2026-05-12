/**
 * Central frontend timeout configuration.
 *
 * Every fetch/AbortController deadline used by the app comes from here.
 * Defaults are intentionally generous so legitimately slow backend
 * responses (deep trace traversals, wide containment fanout) are not
 * aborted before the backend's own per-operation budget fires.
 *
 * Override at build/run time via Vite env vars:
 *   VITE_TIMEOUT_DEFAULT_MS
 *   VITE_TIMEOUT_TRACE_MS
 *   VITE_TIMEOUT_GET_CHILDREN_MS
 *   VITE_TIMEOUT_AGGREGATED_EDGES_MS
 *
 * Companion backend constants live in
 * backend/app/config/resilience.py.
 */

function readMs(key: string, fallback: number): number {
  const raw = (import.meta.env as Record<string, string | undefined>)[key]
  const n = raw === undefined ? NaN : Number(raw)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

export const TIMEOUTS = {
  DEFAULT_MS:          readMs('VITE_TIMEOUT_DEFAULT_MS',          30_000),
  TRACE_MS:            readMs('VITE_TIMEOUT_TRACE_MS',            60_000),
  GET_CHILDREN_MS:     readMs('VITE_TIMEOUT_GET_CHILDREN_MS',     30_000),
  AGGREGATED_EDGES_MS: readMs('VITE_TIMEOUT_AGGREGATED_EDGES_MS', 45_000),
  EDGES_BETWEEN_MS:    readMs('VITE_TIMEOUT_EDGES_BETWEEN_MS',    45_000),
  PROVIDER_HEALTH_MS:  readMs('VITE_TIMEOUT_PROVIDER_HEALTH_MS',  30_000),
} as const
