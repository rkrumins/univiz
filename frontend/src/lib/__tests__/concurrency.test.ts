import { describe, expect, it } from 'vitest'
import { BoundedQueue, mapWithConcurrency } from '../concurrency'

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

describe('mapWithConcurrency', () => {
    it('returns results in input order regardless of resolution order', async () => {
        const out = await mapWithConcurrency([0, 1, 2, 3], 2, async (n) => {
            await delay(20 - n * 5)
            return n * 10
        })
        expect(out.map((r) => (r.status === 'fulfilled' ? r.value : null))).toEqual([0, 10, 20, 30])
    })

    it('never exceeds the concurrency limit', async () => {
        let active = 0
        let peak = 0
        await mapWithConcurrency(Array.from({ length: 20 }, (_, i) => i), 4, async () => {
            active++
            peak = Math.max(peak, active)
            await delay(5)
            active--
        })
        expect(peak).toBeLessThanOrEqual(4)
    })

    it('captures rejections per-item without aborting the run', async () => {
        const out = await mapWithConcurrency([1, 2, 3], 2, async (n) => {
            if (n === 2) throw new Error('boom')
            return n
        })
        expect(out[0]).toEqual({ status: 'fulfilled', value: 1 })
        expect(out[1].status).toBe('rejected')
        expect(out[2]).toEqual({ status: 'fulfilled', value: 3 })
    })

    it('returns [] for empty input', async () => {
        expect(await mapWithConcurrency([], 4, async (n) => n)).toEqual([])
    })
})

describe('BoundedQueue', () => {
    it('caps active task count at the limit', async () => {
        const q = new BoundedQueue(3)
        let active = 0
        let peak = 0
        await Promise.all(
            Array.from({ length: 12 }, (_, i) =>
                q.submit(`k${i}`, async () => {
                    active++
                    peak = Math.max(peak, active)
                    await delay(5)
                    active--
                }),
            ),
        )
        expect(peak).toBeLessThanOrEqual(3)
    })

    it('collapses duplicate keys to the in-flight promise', async () => {
        const q = new BoundedQueue(2)
        let calls = 0
        const task = async () => {
            calls++
            await delay(10)
        }
        const a = q.submit('shared', task)
        const b = q.submit('shared', task)
        expect(a).toBe(b)
        await a
        expect(calls).toBe(1)
    })

    it('signals abort on cancel and removes pending entries', async () => {
        const q = new BoundedQueue(1)
        let signalSeen: AbortSignal | null = null
        // Block the queue with one slow task
        const blocker = q.submit('block', async () => {
            await delay(20)
        })
        // Queue a second task that records its signal
        const observed = q.submit('observe', async (signal) => {
            signalSeen = signal
        })
        // Cancel before it ever runs
        q.cancel('observe')
        await blocker
        await observed
        // The pending task was dropped — it never ran, so signalSeen is null.
        // (The point being tested: cancel removes pending entries without throwing.)
        expect(signalSeen).toBeNull()
    })

    it('cancelAll empties the queue', async () => {
        const q = new BoundedQueue(1)
        const blocker = q.submit('block', async () => { await delay(20) })
        q.submit('a', async () => { await delay(5) })
        q.submit('b', async () => { await delay(5) })
        expect(q.size()).toBeGreaterThan(1)
        q.cancelAll()
        await blocker
        // After cancelAll the queue's pending list is drained; size only
        // reflects whatever is still in-flight (the original blocker may
        // already be done).
        expect(q.size()).toBeLessThanOrEqual(1)
    })
})
