/**
 * ``authFetch`` — convenience wrapper that calls ``fetchWithTimeout``
 * and parses the response as JSON (or returns ``undefined`` on 204).
 *
 * All of the interesting behaviour — credentialed cookies, CSRF header
 * injection, silent refresh on 401 — lives in ``fetchWithTimeout`` so
 * every service (authFetch callers or not) inherits it uniformly. This
 * module is only here so existing call sites that return parsed JSON
 * don't have to each repeat the ``res.ok`` / ``res.json()`` boilerplate.
 */

import { fetchWithTimeout } from './fetchWithTimeout'
import { useHealthStore } from '@/store/health'

export async function authFetch<T>(url: string, init?: RequestInit): Promise<T> {
    let res: Response
    try {
        res = await fetchWithTimeout(url, init)
    } catch (err) {
        // Network / timeout failures should surface to the health store
        // the same way they did previously, so banner + retry UI continue
        // to work unchanged.
        useHealthStore.getState().reportFailure(err)
        throw err
    }

    if (!res.ok) {
        const text = await res.text()
        let detail: string = res.statusText
        try {
            const body = JSON.parse(text)
            // Some endpoints (e.g. the aggregation trigger gate) return a
            // structured error body of the form ``{detail: {code, message,
            // resolution}}`` so the UI can render context. Stringify those
            // safely instead of letting `new Error(dict)` silently coerce
            // the object into "[object Object]".
            const raw = body?.detail
            if (typeof raw === 'string') {
                detail = raw
            } else if (Array.isArray(raw)) {
                // FastAPI 422 validation errors come through as
                // ``detail: [{loc, msg, type}, ...]``. Render the
                // first item's ``msg`` if available, falling back to
                // the JSON dump — never let the array coerce via
                // ``Array.prototype.toString`` into the cursed
                // "[object Object],[object Object]" string.
                const first = raw.find((e: any) => e && typeof e.msg === 'string') as any
                detail = first ? first.msg : JSON.stringify(raw)
            } else if (raw && typeof raw === 'object') {
                if (typeof (raw as any).message === 'string') {
                    detail = (raw as any).message
                } else {
                    detail = JSON.stringify(raw)
                }
            } else {
                detail = JSON.stringify(body)
            }
        } catch {
            detail = text || res.statusText
        }
        if (res.status === 401) throw new Error('Session expired')
        throw new Error(detail)
    }

    if (res.status === 204) return undefined as T
    return res.json()
}
