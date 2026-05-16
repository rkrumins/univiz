/**
 * perfMarkers - lightweight wrappers around the Performance API for
 * before/after measurement of canvas mount, first paint, and edge counts.
 *
 * Markers appear in Chrome DevTools Performance panel and can be queried
 * programmatically via performance.getEntriesByType('measure').
 *
 * Used to establish a baseline of the 45,840-edge trace scene so the
 * impact of the lineage stub work can be quantified, not estimated.
 */

const PREFIX = 'synodic'

function mark(name: string): void {
  if (typeof performance === 'undefined' || !performance.mark) return
  try {
    performance.mark(`${PREFIX}.${name}`)
  } catch {
    // Silently ignore — perf markers are diagnostic, never required.
  }
}

function measure(name: string, startMark: string, endMark?: string): number | null {
  if (typeof performance === 'undefined' || !performance.measure) return null
  try {
    const fullStart = `${PREFIX}.${startMark}`
    const fullEnd = endMark ? `${PREFIX}.${endMark}` : undefined
    performance.measure(`${PREFIX}.${name}`, fullStart, fullEnd)
    const entries = performance.getEntriesByName(`${PREFIX}.${name}`, 'measure')
    const last = entries[entries.length - 1]
    return last ? last.duration : null
  } catch {
    return null
  }
}

/** Canvas hydration started — every consumer should call this at mount. */
export function markCanvasMountStart(): void {
  mark('canvas.mount.start')
}

/** First meaningful paint — when nodes and (stub or real) edges first render. */
export function markCanvasFirstPaint(): void {
  mark('canvas.first-paint')
  measure('canvas.mount-to-first-paint', 'canvas.mount.start', 'canvas.first-paint')
}

/** Trace started — call from useUnifiedTrace.startTrace. */
export function markTraceStart(focusId: string): void {
  mark(`trace.start.${focusId}`)
  mark('trace.start')
}

/** Trace skeleton response merged into canvas. */
export function markTraceSkeletonMerged(edgeCount: number): void {
  mark('trace.skeleton.merged')
  measure('trace.start-to-skeleton', 'trace.start', 'trace.skeleton.merged')
  reportCount('trace.skeleton.edgeCount', edgeCount)
}

/** Trace drill-down expand returned. */
export function markTraceDrillMerged(level: number, edgeCount: number): void {
  mark(`trace.drill.${level}.merged`)
  reportCount(`trace.drill.${level}.edgeCount`, edgeCount)
}

/** A point-in-time snapshot of how many SVG edge elements are in the DOM. */
export function snapshotEdgeCount(label: string = 'idle'): number {
  if (typeof document === 'undefined') return 0
  const count = document.querySelectorAll('[data-edge-id], .react-flow__edge, [data-lineage-edge]').length
  reportCount(`edges.dom.${label}`, count)
  return count
}

/** Count of edges currently in the canvas store. Pure data layer. */
export function reportStoreEdgeCount(count: number): void {
  reportCount('edges.store.count', count)
}

/** Generic numeric reporter — emits a zero-duration mark with detail. */
function reportCount(name: string, value: number): void {
  if (typeof performance === 'undefined' || !performance.mark) return
  try {
    // `performance.mark` supports a `detail` option in modern browsers.
    // Falls back silently on older runtimes.
    performance.mark(`${PREFIX}.${name}`, { detail: { value } } as PerformanceMarkOptions)
  } catch {
    /* noop */
  }
}

/**
 * Dump all synodic markers as a JSON-friendly summary for console-paste
 * comparison between before/after runs.
 *
 * Usage from DevTools console:
 *   import('@/utils/perfMarkers').then(m => console.table(m.summary()))
 */
export function summary(): Array<{ name: string; type: string; duration?: number; detail?: unknown }> {
  if (typeof performance === 'undefined') return []
  const entries = performance.getEntries()
    .filter((e) => e.name.startsWith(`${PREFIX}.`))
    .map((e) => ({
      name: e.name.replace(`${PREFIX}.`, ''),
      type: e.entryType,
      duration: e.duration > 0 ? Math.round(e.duration * 100) / 100 : undefined,
      detail: (e as PerformanceMark).detail,
    }))
  return entries
}

/** Clear all synodic markers — useful before a fresh measurement run. */
export function clearMarkers(): void {
  if (typeof performance === 'undefined') return
  performance.getEntriesByType('mark')
    .filter((e) => e.name.startsWith(`${PREFIX}.`))
    .forEach((e) => performance.clearMarks(e.name))
  performance.getEntriesByType('measure')
    .filter((e) => e.name.startsWith(`${PREFIX}.`))
    .forEach((e) => performance.clearMeasures(e.name))
}
