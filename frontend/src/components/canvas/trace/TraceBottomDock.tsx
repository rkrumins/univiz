import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { cn } from '@/lib/utils'
import { MOTION } from '@/lib/motion'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { TraceDockTitleBar } from './TraceDockTitleBar'
import { TraceDockTabs, type TraceDockTab } from './TraceDockTabs'
import { TraceDockOverview } from './TraceDockOverview'
import { TraceDockDrilldownList } from './TraceDockDrilldownList'
import { TraceDockSettings } from './TraceDockSettings'
import { shouldShowTruncationNotice } from './TraceDockNoticeStrip'
import type { GranularityOption } from './TraceDockControls'
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

const COMPACT_HEIGHT = 64
const MIN_EXPANDED_HEIGHT = 240
const DEFAULT_EXPANDED_HEIGHT = 320
const MAX_VH_FRACTION = 0.6

// Module-level so the user's preferred expanded height persists across
// open/close within the session. Resets on page reload (deliberate; no
// localStorage thrash).
let lastExpandedHeight = DEFAULT_EXPANDED_HEIGHT

/**
 * The bottom Trace Dock — a floating shelf at the bottom of canvas-body.
 *
 * Compact mode (56px): just the title bar with focus + counts + direction
 * + Recent + Expand + Exit controls.
 *
 * Expanded mode (~300px default, drag-resize between 220px and 60vh):
 * title bar, then a 36px tab strip (Overview · Drilldowns · Settings),
 * then the active tab body. Tabs cross-fade with a 180ms ease.
 *
 * The dock sits inside canvas-body's relative-positioned container and
 * spans its full width (`left-3 right-3`). canvas-body itself shrinks
 * when the EntityDrawer (a flex sibling of the canvas column) opens, so
 * the dock follows along for free — no manual offset needed. z-index 30
 * keeps it above EdgeLegend; the parent lifts EdgeLegend via the
 * `--trace-dock-height` CSS variable the dock publishes.
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
  const [tab, setTab] = useState<TraceDockTab>('overview')
  const dockRef = useRef<HTMLDivElement>(null)
  const hasAutoJumpedRef = useRef(false)

  useTraceEscStack(expanded, onToggleExpanded, 50)

  // Persist height across the session.
  useEffect(() => () => { lastExpandedHeight = expandedHeight }, [expandedHeight])

  // Auto-jump tab on first expand. Notice takes priority — surface it on
  // Overview so users see why the trace is constrained. Otherwise, jump
  // to Drilldowns when there are active drilldowns. Otherwise stay on
  // Overview. Reset the flag when the dock collapses so the next expand
  // re-evaluates.
  const drilldownCount = trace.drilldowns.size
  // Mirror the notice-strip's own gating logic so the title-bar/tab amber
  // alert dot is only lit when the strip will actually render. Truncation
  // is filtered through `shouldShowTruncationNotice` to suppress false
  // alarms on tiny results.
  const hasNotice = !!(
    shouldShowTruncationNotice(trace.result) ||
    (trace.result?.isInherited && trace.result?.inheritedFromUrn)
  )

  useEffect(() => {
    if (!expanded) {
      hasAutoJumpedRef.current = false
      return
    }
    if (hasAutoJumpedRef.current) return
    if (hasNotice) {
      setTab('overview')
    } else if (drilldownCount > 0) {
      setTab('drilldowns')
    }
    hasAutoJumpedRef.current = true
  }, [expanded, hasNotice, drilldownCount])

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

  // Drag-to-resize on the top edge. Dock extends UPWARD when grown, so
  // dragging UP increases height (negative deltaY).
  const onResizeStart = (e: React.PointerEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startHeight = expandedHeight
    const maxHeight = Math.floor(window.innerHeight * MAX_VH_FRACTION)
    const onMove = (ev: PointerEvent) => {
      const delta = startY - ev.clientY
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
  useEffect(() => {
    const parent = dockRef.current?.closest<HTMLElement>('[data-canvas-body]')
    if (!parent) return
    parent.style.setProperty('--trace-dock-height', `${dockHeight + 12}px`)
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
        // Premium frosted shell: gradient base + heavy blur + accent-tinted border
        'bg-gradient-to-b from-canvas-elevated/96 via-canvas-elevated/95 to-canvas-elevated/96',
        'backdrop-blur-2xl',
        'border border-accent-lineage/25',
        'shadow-glass-lg shadow-accent-lineage/10',
        // Top-edge accent hairline — the "trace mode is live" tell
        'before:absolute before:inset-x-0 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-accent-lineage/70 before:to-transparent before:pointer-events-none',
        // Soft ambient glow at the top edge — quiet halo that says "alive"
        'after:absolute after:inset-x-16 after:-top-px after:h-[2px] after:bg-accent-lineage/40 after:blur-md after:pointer-events-none',
      )}
    >
      <div className="relative h-full flex flex-col">
        {/* Drag-resize handle — only meaningful in expanded mode. The
            invisible 8px hit area sits over the top edge with a 4px-on-
            hover band and a centered 3-dot grip indicator. */}
        {expanded && (
          <div
            role="separator"
            aria-label="Resize trace dock"
            aria-orientation="horizontal"
            aria-valuenow={expandedHeight}
            aria-valuemin={MIN_EXPANDED_HEIGHT}
            aria-valuemax={Math.floor(window.innerHeight * MAX_VH_FRACTION)}
            onPointerDown={onResizeStart}
            className="absolute top-0 left-0 right-0 h-2 cursor-row-resize group z-20"
          >
            {/* Hover band — soft gradient glow */}
            <div className="absolute inset-x-0 top-0 h-1 opacity-0 group-hover:opacity-100 transition-opacity duration-200 bg-gradient-to-r from-transparent via-accent-lineage/20 to-transparent" />
            {/* Grip pill — subtle at rest, lights up on hover */}
            <div
              className={cn(
                'absolute top-[3px] left-1/2 -translate-x-1/2',
                'inline-flex items-center gap-[3px] px-1.5 h-1.5 rounded-full',
                'bg-white/10 group-hover:bg-accent-lineage/50',
                'opacity-50 group-hover:opacity-100 transition-all duration-200',
              )}
            >
              <span className="block w-0.5 h-0.5 rounded-full bg-current" />
              <span className="block w-0.5 h-0.5 rounded-full bg-current" />
              <span className="block w-0.5 h-0.5 rounded-full bg-current" />
            </div>
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

        {/* Tabs + body — only in expanded mode */}
        <AnimatePresence initial={false}>
          {expanded && (
            <motion.div
              key="dock-body"
              id="trace-bottom-dock-body"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.18, ease: 'easeOut' }}
              className="flex-1 min-h-0 flex flex-col"
            >
              <TraceDockTabs
                active={tab}
                onChange={setTab}
                drilldownCount={drilldownCount}
                hasNotice={hasNotice}
              />

              <AnimatePresence mode="wait" initial={false}>
                {tab === 'overview' && (
                  <TraceDockOverview
                    key="overview"
                    trace={trace}
                    displayMap={displayMap}
                    focusNode={focusNode}
                    resolveEdgeColor={resolveEdgeColor}
                    onReduceDepth={handleReduceDepth}
                    onJumpToUrn={onJumpToUrn}
                  />
                )}
                {tab === 'drilldowns' && (
                  <motion.div
                    key="drilldowns"
                    id="trace-dock-panel-drilldowns"
                    role="tabpanel"
                    aria-labelledby="trace-dock-tab-drilldowns"
                    initial={{ opacity: 0, scale: 0.99 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.99 }}
                    transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
                    className="flex-1 min-h-0 flex flex-col"
                  >
                    <TraceDockDrilldownList
                      drilldowns={trace.drilldowns}
                      displayMap={displayMap}
                      onCollapse={trace.collapseDrilldown}
                    />
                  </motion.div>
                )}
                {tab === 'settings' && (
                  <TraceDockSettings
                    key="settings"
                    trace={trace}
                    granularityOptions={granularityOptions}
                    availableEdgeTypes={availableEdgeTypes}
                    resolveEdgeColor={resolveEdgeColor}
                  />
                )}
              </AnimatePresence>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}
