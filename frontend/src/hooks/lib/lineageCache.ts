/**
 * lineageCache - shared substrate for FNV-1a hashing and LRU cache
 * patterns used across the lineage hooks (useAggregatedLineage, useLineageStubs).
 *
 * Extracted from useAggregatedLineage so per-node-direction and per-pair flows
 * share the same hashing + eviction implementation without duplicating it.
 *
 * No state-machine logic lives here — that stays in each consumer hook because
 * the lifecycle semantics differ (collapsed/loading/expanded for aggregated,
 * collapsed/loading/expanded/pinned for stubs).
 */

// FNV-1a 64-bit hash. BigInt arithmetic. Returns a hex string.
// Used to keep cache keys compact even when the input is a multi-MB joined-URN string.
const FNV_OFFSET_64 = 0xcbf29ce484222325n
const FNV_PRIME_64 = 0x100000001b3n
const FNV_MASK_64 = 0xffffffffffffffffn

export function fnv1a64(input: string): string {
  let hash = FNV_OFFSET_64
  for (let i = 0; i < input.length; i++) {
    hash ^= BigInt(input.charCodeAt(i))
    hash = (hash * FNV_PRIME_64) & FNV_MASK_64
  }
  return hash.toString(16)
}

/**
 * LRU cache with FIFO eviction on overflow, TTL-aware lookup.
 *
 * Insertion order is preserved by Map; the oldest key gets evicted when size
 * exceeds maxEntries. `get` returns null if the entry's age exceeds ttlMs.
 *
 * This mirrors the inline pattern at useAggregatedLineage.ts:126-169,277-279
 * — extracting it lets both hooks share identical semantics without copy-paste.
 */
export interface LruCacheOptions {
  maxEntries: number
  ttlMs: number
}

interface CacheEntry<V> {
  value: V
  timestamp: number
}

export class LruCache<V> {
  private store: Map<string, CacheEntry<V>>
  private readonly maxEntries: number
  private readonly ttlMs: number

  constructor({ maxEntries, ttlMs }: LruCacheOptions) {
    this.store = new Map()
    this.maxEntries = maxEntries
    this.ttlMs = ttlMs
  }

  get(key: string): V | null {
    const entry = this.store.get(key)
    if (!entry) return null
    if (Date.now() - entry.timestamp > this.ttlMs) {
      // Stale — drop it so future lookups don't keep returning expired data.
      this.store.delete(key)
      return null
    }
    return entry.value
  }

  /** Set a value. Evicts the oldest entry if the cache is at capacity. */
  set(key: string, value: V): void {
    if (this.store.size >= this.maxEntries && !this.store.has(key)) {
      const oldestKey = this.store.keys().next().value
      if (oldestKey !== undefined) this.store.delete(oldestKey)
    }
    this.store.set(key, { value, timestamp: Date.now() })
  }

  delete(key: string): boolean {
    return this.store.delete(key)
  }

  /** Clear every entry. Use sparingly — generally prefer per-key invalidation. */
  clear(): void {
    this.store.clear()
  }

  get size(): number {
    return this.store.size
  }

  /** Iterate entries without exposing the internal Map. */
  entries(): IterableIterator<[string, V]> {
    const iter = this.store.entries()
    return (function* () {
      for (const [key, entry] of iter) {
        yield [key, entry.value]
      }
    })()
  }
}
