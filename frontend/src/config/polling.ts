/**
 * Centralised polling configuration.
 *
 * Two responsibilities:
 *
 * 1. **One place to tune intervals.** Every poll across the app reads
 *    its baseline from here so an operator can re-balance the load
 *    floor (announcements every 30s instead of 15s, etc.) without
 *    grepping for `setInterval(`.
 *
 * 2. **Jitter helper.** Adds up to ±`frac` random noise to a base
 *    interval so 1000 clients don't all poll at the same instant.
 *    Without jitter, the backend sees a `users / interval` spike at
 *    every multiple of the interval; with jitter, the load is spread
 *    flat. For 1000 users at 15s baseline that's `~67 req/s spike → ~67
 *    req/s flat` — same average but no thundering herd.
 *
 * The values here are baselines. The announcements poll is also
 * admin-configurable via the backend's announcement config (overrides
 * the constant below) so ops can dial it remotely without a deploy.
 */

/** Baseline interval constants. All values in milliseconds. */
export const POLLING_INTERVALS = {
  /**
   * Active announcements (banner). Backend's admin config can
   * override this per-deployment via `/announcements/config`; this is
   * the fallback used until the config response lands.
   */
  announcements: 15_000,
  /**
   * Provider health/status. Background poll for the connection status
   * indicator across all configured providers.
   */
  providerStatus: 30_000,
  /**
   * Aggregation job history — only ticks while a job is actually
   * pending/running. Bumped from 3s to 5s because below 5s the UI
   * gains nothing (humans perceive ~2s as "real time") and the cost
   * scales linearly with how many users are watching.
   */
  aggregationHistoryActive: 5_000,
} as const

/**
 * Add bounded jitter to a base interval. Used to prevent lockstep
 * polling across many clients — without this, every client that
 * started near the same instant would fire at the same wall-clock
 * times forever.
 *
 * Returns an integer milliseconds value in the range
 * `[baseMs, baseMs * (1 + frac)]`. Default `frac = 0.3` matches what
 * exponential-backoff libraries typically use; tune lower (e.g.
 * `0.1`) if a tight cadence matters, higher if smoothing is more
 * important than predictability.
 *
 * Pure function: easy to unit-test, no DOM / timer dependencies.
 */
export function withJitter(baseMs: number, frac = 0.3): number {
  if (baseMs <= 0) return 0
  const spread = Math.max(0, Math.min(1, frac))
  return Math.floor(baseMs + Math.random() * baseMs * spread)
}
