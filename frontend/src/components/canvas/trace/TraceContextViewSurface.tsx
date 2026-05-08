import { useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { TracePill } from './TracePill'
import { TraceHistoryStrip } from './TraceHistoryStrip'
import { TraceNarrativeBanner } from './TraceNarrativeBanner'
import { TraceDetailsPanel } from './TraceDetailsPanel'
import type { GranularityOption } from './tabs/TraceSettingsTab'

export interface TraceContextViewSurfaceProps {
  trace: UseUnifiedTraceResult
  displayMap: Map<string, HierarchyNode>
  availableEdgeTypes: string[]
  granularityOptions: GranularityOption[]
  resolveEdgeColor: (edgeType: string) => string
  /** Imperative exit handler — clears trace AND any expanded-from-trace state in the parent. */
  onExit: () => void
  /** Jump trace focus to a different URN (used by the inheritance banner). */
  onJumpToUrn: (urn: string) => void
}

/**
 * The premium ContextView trace surface — composes the pill, history strip,
 * narrative banners, and the collapsible details panel. Replaces the
 * legacy floating TraceToolbar inside ContextViewHeader. Other canvases
 * (GraphCanvas / HierarchyCanvas) still use the original TraceToolbar.
 */
export function TraceContextViewSurface({
  trace,
  displayMap,
  availableEdgeTypes,
  granularityOptions,
  resolveEdgeColor,
  onExit,
  onJumpToUrn,
}: TraceContextViewSurfaceProps) {
  const [detailsOpen, setDetailsOpen] = useState(false)

  // The pill is the primary surface; everything else is conditional on
  // tracing being active. AnimatePresence lets the whole surface
  // smoothly enter/exit when the user toggles trace.
  const focusNode = trace.focusId ? displayMap.get(trace.focusId) : undefined
  const focusName = focusNode?.name ?? trace.focusId ?? 'Unknown'
  const focusType = focusNode?.typeId

  const handleReduceDepth = () => {
    trace.setConfig({
      upstreamDepth: Math.max(1, Math.floor(trace.config.upstreamDepth / 2)),
      downstreamDepth: Math.max(1, Math.floor(trace.config.downstreamDepth / 2)),
    })
    trace.retrace()
  }

  return (
    <AnimatePresence>
      {trace.isTracing && (
        <div
          key="trace-surface"
          className="absolute top-3 left-1/2 -translate-x-1/2 z-50 flex flex-col items-center gap-2 pointer-events-none"
        >
          <div className="pointer-events-auto">
            <TracePill
              focusName={focusName}
              focusType={focusType}
              effectiveLevel={trace.result?.effectiveLevel}
              upstreamCount={trace.upstreamCount}
              downstreamCount={trace.downstreamCount}
              showUpstream={trace.showUpstream}
              showDownstream={trace.showDownstream}
              onSetShowUpstream={trace.setShowUpstream}
              onSetShowDownstream={trace.setShowDownstream}
              detailsOpen={detailsOpen}
              onToggleDetails={() => setDetailsOpen(v => !v)}
              onExit={onExit}
              isLoading={trace.isLoading}
            />
          </div>

          <AnimatePresence>
            {trace.result && (
              <div key="banner" className="pointer-events-auto">
                <TraceNarrativeBanner
                  result={trace.result}
                  displayMap={displayMap}
                  onReduceDepth={handleReduceDepth}
                  onJumpToUrn={onJumpToUrn}
                />
              </div>
            )}
          </AnimatePresence>

          <AnimatePresence>
            {trace.traceHistory.length >= 2 && (
              <div key="history" className="pointer-events-auto">
                <TraceHistoryStrip
                  history={trace.traceHistory}
                  displayMap={displayMap}
                  activeFocusId={trace.focusId}
                  onJump={trace.jumpToHistoryEntry}
                  onClear={trace.clearTraceHistory}
                />
              </div>
            )}
          </AnimatePresence>

          <AnimatePresence>
            {detailsOpen && (
              <div key="details" className="pointer-events-auto">
                <TraceDetailsPanel
                  trace={trace}
                  displayMap={displayMap}
                  availableEdgeTypes={availableEdgeTypes}
                  granularityOptions={granularityOptions}
                  resolveEdgeColor={resolveEdgeColor}
                />
              </div>
            )}
          </AnimatePresence>
        </div>
      )}
    </AnimatePresence>
  )
}
