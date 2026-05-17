import { useMemo } from 'react'
import { create } from 'zustand'
import { POLLING_INTERVALS, withJitter } from '@/config/polling'
import { providerService, type ProviderStatusResponse } from '@/services/providerService'

export interface ProviderStatusEntry extends ProviderStatusResponse {}

interface ProviderStatusState {
  statuses: Record<string, ProviderStatusEntry>
  lastUpdatedAt: number | null
  /** P4.2 — true when statuses came from localStorage hydration, not a
   * fresh poll. Components can render a subtle "cached" indicator until
   * the first real refresh lands. */
  fromCache: boolean
  refresh: () => Promise<void>
}

const POLL_INTERVAL_MS = POLLING_INTERVALS.providerStatus

// P4.2 — persist last-known status to localStorage so a returning visit
// renders the previous truth instantly while the first poll completes.
// Bypasses the cold-start "Computing…" window for typical 30s warmup
// cycles. TTL keeps stale-on-disk entries from masking real changes
// across longer absences (e.g. overnight tab restore).
const PERSIST_KEY = 'providerStatusV1'
const PERSIST_TTL_MS = 5 * 60_000   // 5 minutes

interface PersistedSnapshot {
  statuses: Record<string, ProviderStatusEntry>
  ts: number
}

function hydrateFromStorage(): { statuses: Record<string, ProviderStatusEntry>; ts: number } | null {
  if (typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(PERSIST_KEY)
    if (!raw) return null
    const snap = JSON.parse(raw) as PersistedSnapshot
    if (!snap || typeof snap.ts !== 'number') return null
    if (Date.now() - snap.ts > PERSIST_TTL_MS) return null
    return { statuses: snap.statuses ?? {}, ts: snap.ts }
  } catch {
    return null
  }
}

function persistToStorage(statuses: Record<string, ProviderStatusEntry>): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(PERSIST_KEY, JSON.stringify({
      statuses,
      ts: Date.now(),
    } satisfies PersistedSnapshot))
  } catch {
    // localStorage may be full or disabled; non-fatal.
  }
}

const initialHydrated = hydrateFromStorage()

export const useProviderStatusStore = create<ProviderStatusState>((set) => ({
  statuses: initialHydrated?.statuses ?? {},
  lastUpdatedAt: initialHydrated?.ts ?? null,
  fromCache: initialHydrated !== null,

  refresh: async () => {
    try {
      const statuses = await providerService.listStatus()
      const map = Object.fromEntries(statuses.map((status) => [status.id, status]))
      set({
        statuses: map,
        lastUpdatedAt: Date.now(),
        fromCache: false,
      })
      persistToStorage(map)
    } catch {
      // Keep the previous snapshot. Provider status should never blank the UI.
    }
  },
}))

export function useProviderStatus(providerId: string | null | undefined): ProviderStatusEntry | null {
  return useProviderStatusStore((state) => {
    if (!providerId) return null
    return state.statuses[providerId] ?? null
  })
}

export function useAllProviderStatuses(): ProviderStatusEntry[] {
  const statuses = useProviderStatusStore((state) => state.statuses)
  return useMemo(() => Object.values(statuses), [statuses])
}

let pollTimer: ReturnType<typeof setTimeout> | null = null
let authReady = false

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function startPolling() {
  if (pollTimer || !authReady || typeof document === 'undefined' || document.hidden) return

  const poll = async () => {
    await useProviderStatusStore.getState().refresh()
    // Jitter every reschedule so 1000 clients that mounted near the
    // same instant don't fire in lockstep forever. Same flat-load
    // motivation as the announcements + aggregation-history pollers.
    pollTimer = setTimeout(poll, withJitter(POLL_INTERVAL_MS))
  }

  void poll()
}

/** Call once after auth resolves to enable polling. */
export function enableProviderStatusPolling() {
  authReady = true
  startPolling()
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
