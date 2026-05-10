import { motion } from 'framer-motion'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { TraceDockNoticeStrip } from './TraceDockNoticeStrip'
import { TraceDockMetricStrip } from './TraceDockMetricStrip'

export interface TraceDockOverviewProps {
  trace: UseUnifiedTraceResult
  displayMap: Map<string, HierarchyNode>
  focusNode: HierarchyNode | null
  resolveEdgeColor: (edgeType: string) => string
  onReduceDepth: () => void
  onJumpToUrn: (urn: string) => void
}

/**
 * Overview tab — calm summary of the active trace. Notice strip surfaces
 * truncation/inheritance issues at the top; below sits the hero metric
 * grid (4 large numerals) and the edge-type breakdown. Generous breathing
 * room — this is the resting view.
 */
export function TraceDockOverview({
  trace,
  displayMap,
  focusNode,
  resolveEdgeColor,
  onReduceDepth,
  onJumpToUrn,
}: TraceDockOverviewProps) {
  return (
    <motion.div
      key="overview"
      id="trace-dock-panel-overview"
      role="tabpanel"
      aria-labelledby="trace-dock-tab-overview"
      initial={{ opacity: 0, scale: 0.99 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.99 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className="flex-1 min-h-0 flex flex-col overflow-y-auto custom-scrollbar"
    >
      <TraceDockNoticeStrip
        result={trace.result}
        displayMap={displayMap}
        onReduceDepth={onReduceDepth}
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
    </motion.div>
  )
}
