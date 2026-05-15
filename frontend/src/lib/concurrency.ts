/**
 * Concurrency primitives for bounding parallel network fan-out.
 *
 * Used by graph hydration and aggregated-edge fetches to keep a single
 * "expand all" or large-view-open from issuing hundreds of parallel
 * requests against the graph data provider — the failure mode that
 * spikes FalkorDB CPU to 300–400% and freezes the app for everyone.
 */

/**
 * Run `fn` over `items` with at most `limit` concurrent invocations.
 *
 * Returns results in input order. Failures propagate per-item via
 * Promise.allSettled-style — callers wanting bulk failure semantics
 * should map their own try/catch into `fn`.
 */
export async function mapWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<PromiseSettledResult<R>[]> {
  if (items.length === 0) return []
  const cap = Math.max(1, Math.min(limit, items.length))
  const results: PromiseSettledResult<R>[] = new Array(items.length)
  let cursor = 0

  const worker = async (): Promise<void> => {
    while (true) {
      const idx = cursor++
      if (idx >= items.length) return
      try {
        const value = await fn(items[idx], idx)
        results[idx] = { status: 'fulfilled', value }
      } catch (reason) {
        results[idx] = { status: 'rejected', reason }
      }
    }
  }

  const workers: Promise<void>[] = []
  for (let i = 0; i < cap; i++) workers.push(worker())
  await Promise.all(workers)
  return results
}

/**
 * Stateful FIFO queue that caps active tasks at `limit`. Used by
 * components that fire one-off requests independently (e.g. a hierarchy
 * tree where the user clicks several nodes to expand) and need them
 * smoothed into a bounded fan-out.
 *
 * Each task is keyed; duplicate keys collapse to the in-flight promise
 * instead of enqueuing a second copy. Use `cancel(key)` to drop a
 * pending or in-flight task (the underlying AbortController is signaled
 * if the task registered one via `signal`).
 */
interface QueueEntry {
  key: string
  controller: AbortController
  /** Invoked by drain() when a slot opens — runs the task. */
  start: () => void
  /** Invoked by cancel() when the entry is still pending — resolves the outer promise without running the task. */
  abandonPending: () => void
  /** Set true once start() has fired so cancel() routes to in-flight semantics. */
  started: boolean
}

export class BoundedQueue {
  private readonly limit: number
  private active = 0
  private readonly pending: QueueEntry[] = []
  private readonly entries = new Map<string, QueueEntry>()
  private readonly inflight = new Map<string, Promise<void>>()

  constructor(limit: number) {
    this.limit = Math.max(1, limit)
  }

  /**
   * Submit a task. If a task with the same `key` is already pending or
   * in-flight, returns the existing promise — caller's `task` is dropped.
   * The AbortSignal passed to `task` is aborted by `cancel(key)`.
   *
   * The returned promise resolves when the task finishes OR when it is
   * cancelled while still pending (it never ran). It rejects only when
   * the task itself throws.
   */
  submit(key: string, task: (signal: AbortSignal) => Promise<void>): Promise<void> {
    const existing = this.inflight.get(key)
    if (existing) return existing

    const controller = new AbortController()
    let entry!: QueueEntry

    const promise = new Promise<void>((resolve, reject) => {
      entry = {
        key,
        controller,
        started: false,
        start: () => {
          entry.started = true
          ;(async () => {
            try {
              await task(controller.signal)
              resolve()
            } catch (err) {
              reject(err)
            } finally {
              this.entries.delete(key)
              this.inflight.delete(key)
              this.active--
              this.drain()
            }
          })()
        },
        abandonPending: () => {
          this.entries.delete(key)
          this.inflight.delete(key)
          resolve()
        },
      }
      this.pending.push(entry)
      this.entries.set(key, entry)
      this.drain()
    })

    this.inflight.set(key, promise)
    return promise
  }

  /**
   * Cancel a queued or in-flight task by key. If pending, the task is
   * removed and its promise resolves without running. If in-flight, the
   * task's AbortSignal is aborted — the task itself must observe the
   * signal to actually stop work; the promise resolves/rejects per the
   * task's own behavior.
   */
  cancel(key: string): void {
    const entry = this.entries.get(key)
    if (!entry) return
    entry.controller.abort()
    if (!entry.started) {
      const idx = this.pending.indexOf(entry)
      if (idx >= 0) this.pending.splice(idx, 1)
      entry.abandonPending()
    }
  }

  /** Cancel everything in the queue. Used on provider/view switch. */
  cancelAll(): void {
    for (const key of [...this.entries.keys()]) {
      this.cancel(key)
    }
  }

  size(): number {
    return this.active + this.pending.length
  }

  private drain(): void {
    while (this.active < this.limit && this.pending.length > 0) {
      const next = this.pending.shift()
      if (!next) break
      this.active++
      next.start()
    }
  }
}
