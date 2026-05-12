/**
 * Fetch wrapper with AbortController timeout, session cookies, CSRF,
 * and transparent refresh-on-401.
 *
 * One wrapper carries four cross-cutting concerns because the codebase
 * has ~20 service modules that each call ``fetchWithTimeout`` directly.
 * Centralising here means every service inherits the full session
 * behaviour; the alternative is patching all of them.
 *
 * Behaviour:
 *   * ``credentials: 'include'`` — HttpOnly session cookies on every call.
 *   * On non-GET/HEAD/OPTIONS methods the value of the ``nx_csrf``
 *     cookie is mirrored into the ``X-CSRF-Token`` header.
 *   * On 401 for non-auth routes, a single silent ``POST /auth/refresh``
 *     is attempted; on success the original request is retried once
 *     with the rotated cookies. Concurrent 401s share the same in-flight
 *     refresh. If refresh fails we dispatch ``'auth:session-lost'`` on
 *     ``window`` so the auth store can transition to unauthenticated.
 *   * Default timeout via AbortController, sourced from
 *     ``TIMEOUTS.DEFAULT_MS`` in ``src/config/timeouts.ts`` (30 s out of
 *     the box, overridable via ``VITE_TIMEOUT_DEFAULT_MS``). Per-call
 *     override remains supported via the ``timeoutMs`` option — see
 *     ``RemoteGraphProvider`` for trace / children / aggregated-edges
 *     overrides. Earlier 8 s default was aborting legitimately slow BE
 *     responses on deep trace traversals.
 *
 * /auth/* URLs are exempt from the refresh-on-401 dance — /auth/refresh
 * itself returning 401 means the session really is gone, and /auth/me
 * returning 401 is handled by the bootstrap flow directly.
 */

import { TIMEOUTS } from '../config/timeouts'

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])
const CSRF_COOKIE = 'nx_csrf'
const CSRF_HEADER = 'X-CSRF-Token'
const REFRESH_URL = '/api/v1/auth/refresh'
const SESSION_LOST_EVENT = 'auth:session-lost'
const ACCESS_DENIED_EVENT = 'auth:access-denied'

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${name}=`
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim()
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length))
    }
  }
  return null
}

function urlPath(input: RequestInfo | URL): string {
  const raw =
    typeof input === 'string'
      ? input
      : input instanceof URL
      ? input.toString()
      : input.url
  try {
    return new URL(raw, 'http://local').pathname
  } catch {
    return raw
  }
}

function isRefreshEndpoint(input: RequestInfo | URL): boolean {
  // Only /auth/refresh is exempt from the silent-refresh retry loop —
  // bouncing it off itself would just recurse. Every other endpoint,
  // including /auth/me called on boot, benefits from the silent refresh
  // so a still-valid refresh cookie can resurrect an expired access
  // cookie without ever logging the user out.
  return urlPath(input) === REFRESH_URL
}

/**
 * Single in-flight refresh promise — concurrent 401s share one network
 * call instead of each spawning its own. Cleared on the next microtask
 * after resolution so subsequent unrelated 401s can start a new one.
 */
let refreshInFlight: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = (async () => {
    try {
      // Bare fetch — avoids the circular import that would exist if this
      // module pulled in authService, and avoids recursing through the
      // refresh-on-401 logic above (the isAuthEndpoint guard would catch
      // it anyway, but going direct is cheaper).
      const res = await fetch(REFRESH_URL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
      })
      return res.ok
    } catch {
      return false
    } finally {
      queueMicrotask(() => {
        refreshInFlight = null
      })
    }
  })()
  return refreshInFlight
}

function notifySessionLost(): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(SESSION_LOST_EVENT))
}


/**
 * Notify the AppLayout-mounted access-denied modal that a request was
 * 403'd. The detail object carries enough for the modal to render a
 * useful message; the missing-permission name comes from the backend's
 * ``detail`` field which is shaped by the ``requires(...)`` factory as
 * ``"Missing permission: <perm>"``.
 *
 * The event handler is fire-and-forget — calling code still receives
 * the underlying ``Response`` (or thrown error from ``apiClient``) so
 * per-call error handling can remain in place.
 */
async function notifyAccessDenied(res: Response, requestPath: string): Promise<void> {
  if (typeof window === 'undefined') return
  let detail: string | null = null
  // Clone before reading so the caller can still consume the body.
  try {
    const clone = res.clone()
    const text = await clone.text()
    if (text) {
      try {
        const body = JSON.parse(text) as { detail?: string }
        detail = body.detail ?? null
      } catch {
        detail = text
      }
    }
  } catch {
    // ignore — we'll dispatch with detail=null
  }
  window.dispatchEvent(
    new CustomEvent(ACCESS_DENIED_EVENT, {
      detail: { detail, path: requestPath, status: res.status },
    }),
  )
}

/**
 * Build the Headers object for a request, injecting CSRF on writes and
 * defaulting ``Content-Type: application/json`` when the body is a
 * JSON-stringified payload.
 *
 * Why the Content-Type default: every service module here calls
 * ``authFetch(url, { method: 'POST', body: JSON.stringify(data) })`` —
 * i.e., the body is already a JSON string — and none of them set the
 * header explicitly. Without this default, ``fetch`` infers
 * ``text/plain;charset=UTF-8`` from the string body, FastAPI parses the
 * entire body as a single string field, and every Pydantic model in the
 * backend rejects it with *"Input should be a valid dictionary or object
 * to extract fields from"*. Defaulting here fixes every POST/PUT/PATCH
 * at once. FormData / Blob / URLSearchParams bodies skip this branch so
 * the browser's auto-detected multipart/octet-stream boundaries are
 * preserved.
 *
 * Factored out so both the original attempt and the post-refresh retry
 * re-read a possibly-rotated ``nx_csrf`` cookie.
 */
function buildHeaders(
  method: string,
  raw: HeadersInit | undefined,
  body: BodyInit | null | undefined,
): Headers {
  const headers = new Headers(raw)
  if (!SAFE_METHODS.has(method) && !headers.has(CSRF_HEADER)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) headers.set(CSRF_HEADER, csrf)
  }
  if (typeof body === 'string' && body.length > 0 && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  return headers
}

async function runOnce(
  input: RequestInfo | URL,
  fetchInit: RequestInit,
  method: string,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController()
  if (fetchInit.signal) {
    fetchInit.signal.addEventListener('abort', () =>
      controller.abort((fetchInit.signal as AbortSignal).reason),
    )
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(input, {
      credentials: 'include',
      ...fetchInit,
      headers: buildHeaders(method, fetchInit.headers, fetchInit.body),
      signal: controller.signal,
    })
  } finally {
    clearTimeout(timer)
  }
}

export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init?: RequestInit & { timeoutMs?: number },
): Promise<Response> {
  const { timeoutMs = TIMEOUTS.DEFAULT_MS, ...fetchInit } = init ?? {}
  const method = (fetchInit.method ?? 'GET').toUpperCase()

  let res: Response
  try {
    res = await runOnce(input, fetchInit, method, timeoutMs)
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new TypeError('Request timed out (backend may be unavailable)')
    }
    throw err
  }

  // Silent refresh: access cookie expired (or missing) but the refresh
  // cookie may still be good. Attempt exactly one refresh + retry before
  // giving up. /auth/refresh itself is exempt so its own 401 doesn't
  // recurse into another refresh.
  if (res.status === 401 && !isRefreshEndpoint(input)) {
    const refreshed = await tryRefresh()
    if (refreshed) {
      try {
        return await runOnce(input, fetchInit, method, timeoutMs)
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          throw new TypeError('Request timed out (backend may be unavailable)')
        }
        throw err
      }
    }
    notifySessionLost()
  }

  // 403 surfaces as a non-blocking modal mounted by AppLayout. We do
  // NOT short-circuit the request — the calling service still gets the
  // Response and can shape its own error handling — we just announce
  // the denial centrally so the user sees a clear "you don't have X"
  // message instead of a generic toast.
  if (res.status === 403) {
    void notifyAccessDenied(res, urlPath(input))
  }

  return res
}
