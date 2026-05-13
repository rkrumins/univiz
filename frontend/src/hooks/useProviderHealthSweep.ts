/**
 * useProviderHealthSweep — user-gesture-only provider health probing.
 *
 * Post-P0.4: this hook NO LONGER auto-sweeps on mount. The auto-sweep
 * stampeded N unbounded /test calls on every page mount, which under
 * hostile-host conditions amplified BE per-provider slowness back into
 * a frozen UI. Baseline status now flows from the bounded, cache-only
 * ``/admin/providers/status`` endpoint via the global ``providerStatus``
 * store; per-provider /test calls fire ONLY on explicit user gesture.
 *
 * What this hook still provides:
 *  - ``testOne(id)`` — fire one /test for the per-row "Test" button.
 *  - ``refresh()`` — fire /test for every provider for "Re-test All".
 *    Bounded by ``concurrency`` (default 3), each call gets its own
 *    AbortController with ``perCallTimeoutMs``, dead providers are
 *    short-circuited by the FE circuit breaker.
 *  - ``healthMap`` — local sweep results keyed by provider id. Empty
 *    until a user gesture lands; consumers should fall back to the
 *    global ``providerStatus`` store for baseline state.
 *  - ``setHealth(id, health)`` — manual update path (used by the
 *    onboarding wizard after a successful test).
 *
 * Unmount aborts every in-flight probe.
 *
 * (Consider renaming to ``useTestableProviders`` in a follow-up — the
 * "sweep" semantics are gone but call sites pin the existing name.)
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { providerService, type ProviderResponse } from '@/services/providerService'
import { getCircuitBreaker } from '@/services/circuitBreaker'
import { TIMEOUTS } from '@/config/timeouts'

export type HealthStatus = 'checking' | 'healthy' | 'unhealthy' | 'unknown'

export interface ProviderHealth {
    status: HealthStatus
    latencyMs?: number
    error?: string
}

export interface UseProviderHealthSweepOptions {
    concurrency?: number
    perCallTimeoutMs?: number
}

const DEFAULT_CONCURRENCY = 3
const DEFAULT_PER_CALL_TIMEOUT_MS = TIMEOUTS.PROVIDER_HEALTH_MS

export function useProviderHealthSweep(
    providers: ProviderResponse[],
    options: UseProviderHealthSweepOptions = {},
) {
    const concurrency = options.concurrency ?? DEFAULT_CONCURRENCY
    const perCallTimeoutMs = options.perCallTimeoutMs ?? DEFAULT_PER_CALL_TIMEOUT_MS

    const [healthMap, setHealthMap] = useState<Record<string, ProviderHealth>>({})
    const inflightControllers = useRef<Map<string, AbortController>>(new Map())
    const initialSweepDone = useRef(false)

    const runProbe = useCallback(async (id: string, fresh: boolean = false): Promise<void> => {
        const breaker = getCircuitBreaker('provider', id)
        // Explicit user action (fresh=true) always gets through. Without
        // this reset, a breaker opened by 3 prior failures would silently
        // suppress the user's click for up to 15s, adding to the perceived
        // "UI is stuck" lag when the provider recovers.
        if (fresh) {
            breaker.reset()
        } else if (!breaker.canRequest()) {
            setHealthMap(prev => ({
                ...prev,
                [id]: { status: 'unhealthy', error: 'Circuit open — skipping probe until provider recovers' },
            }))
            return
        }

        // If an earlier probe for this provider is still in flight, cancel it
        // before starting a new one — the caller wants fresh data.
        const previous = inflightControllers.current.get(id)
        if (previous) previous.abort()

        const controller = new AbortController()
        inflightControllers.current.set(id, controller)
        const timer = setTimeout(() => controller.abort(), perCallTimeoutMs)

        setHealthMap(prev => ({ ...prev, [id]: { status: 'checking' } }))

        try {
            const result = await providerService.test(id, {
                signal: controller.signal,
                timeoutMs: perCallTimeoutMs,
                fresh,
            })
            if (controller.signal.aborted) return
            if (result.success) breaker.recordSuccess()
            else breaker.recordFailure()
            setHealthMap(prev => ({
                ...prev,
                [id]: {
                    status: result.success ? 'healthy' : 'unhealthy',
                    latencyMs: result.latencyMs,
                    error: result.error,
                },
            }))
        } catch (err) {
            if (controller.signal.aborted) return
            breaker.recordFailure()
            const message = err instanceof Error ? err.message : 'Provider health check failed'
            setHealthMap(prev => ({ ...prev, [id]: { status: 'unhealthy', error: message } }))
        } finally {
            clearTimeout(timer)
            if (inflightControllers.current.get(id) === controller) {
                inflightControllers.current.delete(id)
            }
        }
    }, [perCallTimeoutMs])

    const runSweep = useCallback(async (ids: string[], fresh: boolean = false): Promise<void> => {
        // Simple in-file semaphore — avoids pulling in p-limit for ~15 lines.
        const queue = [...ids]
        const workers: Promise<void>[] = []
        const next = async (): Promise<void> => {
            while (queue.length > 0) {
                const id = queue.shift()!
                await runProbe(id, fresh)
            }
        }
        for (let i = 0; i < Math.min(concurrency, queue.length); i++) {
            workers.push(next())
        }
        await Promise.allSettled(workers)
    }, [concurrency, runProbe])

    // refresh() = user clicked "Re-test All" → bypass server cache + breaker.
    const refresh = useCallback((): Promise<void> => {
        return runSweep(providers.map(p => p.id), true)
    }, [providers, runSweep])

    // testOne() exposed to callers is the manual Test button handler →
    // always fresh so the cached last result cannot mask the current truth.
    const testOne = useCallback((id: string): Promise<void> => {
        return runProbe(id, true)
    }, [runProbe])

    // P0.4: NO auto-mount sweep.
    //
    // The previous implementation fired ``concurrency``-bounded /test
    // calls for every provider on every mount. With 6 providers, 5 of
    // them DNS-unreachable, this storm hit the backend on every cold
    // boot and amplified any per-provider slowness back into a perceived
    // app freeze — even with the backend fully fixed.
    //
    // Baseline status now comes from the bounded ``/admin/providers/status``
    // and ``/api/v1/health/providers`` aggregate endpoints (polled by the
    // ``providerStatus`` and ``providerHealth`` stores). Per-provider
    // /test calls fire ONLY on explicit user gesture: ``testOne(id)`` for
    // the per-row Test button, ``refresh()`` for the "Re-test All" button.
    //
    // ``initialSweepDone`` is retained as a no-op anchor so existing
    // dependency arrays in callers don't break.
    void initialSweepDone

    // Cleanup — abort anything in flight on unmount.
    useEffect(() => {
        return () => {
            inflightControllers.current.forEach(c => c.abort())
            inflightControllers.current.clear()
        }
    }, [])

    const setHealth = useCallback((id: string, health: ProviderHealth): void => {
        setHealthMap(prev => ({ ...prev, [id]: health }))
    }, [])

    return {
        healthMap,
        testOne,
        refresh,
        setHealth,
    }
}
