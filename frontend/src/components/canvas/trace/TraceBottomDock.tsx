import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { cn } from '@/lib/utils'
import { MOTION } from '@/lib/motion'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { TraceDockTitleBar } from './TraceDockTitleBar'
import { TraceDockNoticeStrip } from './TraceDockNoticeStrip'
import { TraceDockMetricStrip } from './TraceDockMetricStrip'
import { TraceDockDrilldownList } from './TraceDockDrilldownList'
import { TraceDockControls, type GranularityOption } from './TraceDockControls'
import { TraceDockPerformance } from './TraceDockPerformance'
import { useTraceEscStack } from './useTraceEscStack'

export interface TraceBottomDockProps {
  trace: UseUnifiedTraceResult
  displayMap: Map<string, HierarchyNode>
  availableEdgeTypes: string[]
  granularityOptions: GranularityOption[]
  resolveEdgeColor: (edgeType: string) => string
  expanded: boolean
  onToggleExpanded: () => void
  onExit: () => void
  onJumpToUrn: (urn: string) => void
}

const COMPACT_HEIGHT = 52
const MIN_EXPANDED_HEIGHT = 180
const DEFAULT_EXPANDED_HEIGHT = 260
const MAX_VH_FRACTION = 0.6

// Module-level so the user's preferred expanded height persists across
// open/close within the session. Resets on page reload (deliberate; no
// localStorage thrash).
let lastExpandedHeight = DEFAULT_EXPANDED_HEIGHT

/**
 * The bottom Trace Dock — a floating shelf at the bottom of canvas-body.
 *
 * Compact mode (52px): just the title bar with focus + counts + direction
 * + Recent + Expand + Exit controls.
 *
 * Expanded mode (~260px default, drag-resize between 180px and 60vh):
 * title bar stays at top, then dense inline content sections beneath —
 * notice (when triggered), metric strip, drilldown list, controls,
 * performance. No tabs; everything visible at once.
 *
 * The dock sits inside canvas-body's relative-positioned container with
 * `left-3 right-3 bottom-3` insets — a 12px floating-shelf gap from each
 * edge. z-index 30 keeps it above EdgeLegend at z-30 (which is lifted
 * above the dock by the parent).
 */
export function TraceBottomDock({
  trace,
  displayMap,
  availableEdgeTypes,
  granularityOptions,
  resolveEdgeColor,
  expanded,
  onToggleExpanded,
  onExit,
  onJumpToUrn,
}: TraceBottomDockProps) {
  const [expandedHeight, setExpandedHeight] = useState(lastExpandedHeight)
  const dockRef = useRef<HTMLDivElement>(null)

  // ESC closes the expanded dock first, then exits trace via the parent.
  useTraceEscStack(expanded, onToggleExpanded, 50)

  // Persist height across the session.
  useEffect(() => () => { lastExpandedHeight = expandedHeight }, [expandedHeight])

  // Re-clamp height when the viewport shrinks so the dock never crops the
  // canvas down to nothing.
  useLayoutEffect(() => {
    const onResize = () => {
      const max = Math.floor(window.innerHeight * MAX_VH_FRACTION)
      setExpandedHeight(h => Math.min(h, max))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Drag-to-resize on the top edge of the dock. The dock extends UPWARD
  // when grown, so dragging UP increases height (negative deltaY).
  const onResizeStart = (e: React.PointerEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startHeight = expandedHeight
    const maxHeight = Math.floor(window.innerHeight * MAX_VH_FRACTION)
    const onMove = (ev: PointerEvent) => {
      const delta = startY - ev.clientY  // up = positive growth
      const next = Math.min(maxHeight, Math.max(MIN_EXPANDED_HEIGHT, startHeight + delta))
      setExpandedHeight(next)
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  const dockHeight = expanded ? expandedHeight : COMPACT_HEIGHT

  // Publish dock height as a CSS variable on the parent canvas-body so
  // EdgeLegend (and any other bottom-anchored chrome) can lift above it.
  // The CSS var is read by the canvas-body root (set by parent) — we just
  // emit it from here when the dock changes height.
  useEffect(() => {
    const parent = dockRef.current?.closest<HTMLElement>('[data-canvas-body]')
    if (!parent) return
    parent.style.setProperty('--trace-dock-height', `${dockHeight + 12}px`) // +12px floating-shelf gap
    return () => {
      parent.style.removeProperty('--trace-dock-height')
    }
  }, [dockHeight])

  const handleReduceDepth = useCallback(() => {
    trace.setConfig({
      upstreamDepth: Math.max(1, Math.floor(trace.config.upstreamDepth / 2)),
      downstreamDepth: Math.max(1, Math.floor(trace.config.downstreamDepth / 2)),
    })
    trace.retrace()
  }, [trace])

  const focusNode = trace.focusId ? displayMap.get(trace.focusId) ?? null : null

  return (
    <motion.div
      ref={dockRef}
      data-canvas-interactive
      id="trace-bottom-dock"
      role="region"
      aria-label="Trace dock"
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0, height: dockHeight }}
      exit={{ opacity: 0, y: 24 }}
      transition={MOTION.modalSpring}
      style={{ height: dockHeight }}
      className={cn(
        'absolute left-3 right-3 bottom-3 z-30',
        'rounded-2xl overflow-hidden',
        'bg-canvas-elevated/95 backdrop-blur-2xl',
        'border border-accent-lineage/25',
        'shadow-glass-lg',
        // Subtle accent gradient on the top edge — premium "trace mode" tell
        'before:absolute before:inset-x-0 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-accent-lineage/60 before:to-transparent before:pointer-events-none',
      )}
    >
      <div className="relative h-full flex flex-col">
        {/* Drag-resize handle — only meaningful in expanded mode */}
        {expanded && (
          <div
            role="separator"
            aria-label="Resize trace dock"
            aria-orientation="horizontal"
            aria-valuenow={expandedHeight}
            aria-valuemin={MIN_EXPANDED_HEIGHT}
            aria-valuemax={Math.floor(window.innerHeight * MAX_VH_FRACTION)}
            onPointerDown={onResizeStart}
            className={cn(
              'absolute top-0 left-0 right-0 h-1.5 cursor-row-resize group z-10',
              'hover:bg-accent-lineage/10 transition-colors',
            )}
          >
            <div className="mx-auto mt-0.5 h-0.5 w-3 group-hover:w-8 rounded-full bg-glass-border group-hover:bg-accent-lineage/60 transition-all duration-150" />
          </div>
        )}

        {/* Title bar — sticky at top, always visible */}
        <TraceDockTitleBar
          trace={trace}
          displayMap={displayMap}
          expanded={expanded}
          onToggleExpanded={onToggleExpanded}
          onExit={onExit}
        />

        {/* Body — only in expanded mode */}
        <AnimatePresence initial={false}>
          {expanded && (
            <motion.div
              key="dock-body"
              id="trace-bottom-dock-body"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.18, ease: 'easeOut' }}
              className="flex-1 min-h-0 flex flex-col overflow-y-auto custom-scrollbar"
            >
              <TraceDockNoticeStrip
                result={trace.result}
                displayMap={displayMap}
                onReduceDepth={handleReduceDepth}
                onJumpToUrn={onJumpToUrn}
              />
              <TraceDockMetricStrip
                result={trace.result}
                focusNode={focusNode}
                totalNodes={trace.statistics.totalNodes}
                totalEdges={trace.statistics.totalEdges}
                upstreamCount={trace.upstreamCount}
                downstreamCount={trace.downstreamCount}
                resolveEdgeColor={resolveEdgeColor}
              />
              <TraceDockDrilldownList
                drilldowns={trace.drilldowns}
                displayMap={displayMap}
                onCollapse={trace.collapseDrilldown}
              />
              <TraceDockControls
                config={trace.config}
                granularityOptions={granularityOptions}
                availableEdgeTypes={availableEdgeTypes}
                resolveEdgeColor={resolveEdgeColor}
                onChangeConfig={trace.setConfig}
                onApply={() => { trace.retrace() }}
              />
              <TraceDockPerformance meta={trace.result?.meta} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}
