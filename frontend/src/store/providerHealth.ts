/**
 * Per-provider health store — tracks which workspace/datasource providers
 * are healthy vs unhealthy.
 *
 * Polls /api/v1/health/providers every 30s (only when tab visible).
 * Used by sidebar, workspace cards, and GraphProviderContext to show
 * health indicators and warn users before switching to broken providers.
 */
import { create } from 'zustand'
import { fetchWithTimeout } from '@/services/fetchWithTimeout'
import { TIMEOUTS } from '@/config/timeouts'

export type ProviderStatus = 'healthy' | 'unhealthy' | 'unknown'

interface ProviderHealthEntry {
  status: ProviderStatus
  error?: string
  lastChecked: number
}

interface ProviderHealthState {
  /** Map of "workspaceId:dataSourceId" → health entry */
  providers: Map<string, ProviderHealthEntry>

  /** Get status for a specific provider scope */
  getStatus: (workspaceId?: string, dataSourceId?: string) => ProviderStatus

  /** Refresh from backend */
  refresh: () => Promise<void>
}

export const useProviderHealthStore = create<ProviderHealthState>((set, get) => ({
  providers: new Map(),

  getStatus: (workspaceId?: string, dataSourceId?: string) => {
    if (!workspaceId || !dataSourceId) return 'unknown'
    const key = `${workspaceId}:${dataSourceId}`
    return get().providers.get(key)?.status ?? 'unknown'
  },

  refresh: async () => {
    try {
      const res = await fetchWithTimeout('/api/v1/health/providers', { timeoutMs: TIMEOUTS.PROVIDER_HEALTH_MS })
      if (!res.ok) return

      const data = await res.json() as { providers: Record<string, { status: string; error?: string }> }
      const now = Date.now()
      const newMap = new Map<string, ProviderHealthEntry>()

      for (const [key, entry] of Object.entries(data.providers)) {
        newMap.set(key, {
          status: entry.status === 'healthy' ? 'healthy' : 'unhealthy',
          error: entry.error,
          lastChecked: now,
        })
      }

      set({ providers: newMap })
    } catch {
      // Poll failure — don't clear existing data, just skip this cycle
    }
  },
}))

// ─── Polling ──────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 30_000
let pollTimer: ReturnType<typeof setTimeout> | null = null
let authReady = false

function startPolling() {
  if (pollTimer || !authReady) return
  const poll = async () => {
    await useProviderHealthStore.getState().refresh()
    const jitter = Math.random() * 5_000
    pollTimer = setTimeout(poll, POLL_INTERVAL_MS + jitter)
  }
  poll()
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

/** Call once after auth resolves to enable polling. */
export function enableProviderHealthPolling() {
  authReady = true
  if (typeof document !== 'undefined' && !document.hidden) {
    startPolling()
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopPolling()
    } else {
      startPolling()
    }
  })
}
