import React, { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { LayoutDashboard, Layers, Filter, Settings, Activity } from 'lucide-react'
import { cn } from '@/lib/utils'
import { MOTION } from '@/lib/motion'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { TraceOverviewTab } from './tabs/TraceOverviewTab'
import { TraceDrilldownsTab } from './tabs/TraceDrilldownsTab'
import { TraceFiltersTab } from './tabs/TraceFiltersTab'
import { TraceSettingsTab, type GranularityOption } from './tabs/TraceSettingsTab'
import { TracePerformanceTab } from './tabs/TracePerformanceTab'

type TabId = 'overview' | 'drilldowns' | 'filters' | 'settings' | 'performance'

const TABS: ReadonlyArray<{ id: TabId; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'drilldowns', label: 'Drilldowns', icon: Layers },
  { id: 'filters', label: 'Filters', icon: Filter },
  { id: 'settings', label: 'Settings', icon: Settings },
  { id: 'performance', label: 'Performance', icon: Activity },
]

export interface TraceDetailsPanelProps {
  trace: UseUnifiedTraceResult
  displayMap: Map<string, HierarchyNode>
  availableEdgeTypes: string[]
  granularityOptions: GranularityOption[]
  resolveEdgeColor: (edgeType: string) => string
}

export function TraceDetailsPanel({
  trace,
  displayMap,
  availableEdgeTypes,
  granularityOptions,
  resolveEdgeColor,
}: TraceDetailsPanelProps) {
  const [activeTab, setActiveTab] = useState<TabId>('overview')

  const focusNode = trace.focusId ? displayMap.get(trace.focusId) ?? null : null
  const drilldownsCount = trace.drilldowns.size

  return (
    <motion.div
      data-canvas-interactive
      initial={{ opacity: 0, y: -8, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -8, scale: 0.98 }}
      transition={MOTION.modalSpring}
      className={cn(
        'flex flex-col gap-3 w-[520px] max-w-[92vw] p-3 rounded-2xl',
        'bg-canvas-elevated/95 backdrop-blur-2xl',
        'border border-glass-border shadow-glass-lg',
      )}
    >
      {/* Tab strip */}
      <div className="flex items-center gap-1 p-1 rounded-xl bg-black/5 dark:bg-white/5">
        {TABS.map(tab => {
          const Icon = tab.icon
          const isActive = activeTab === tab.id
          const showBadge = tab.id === 'drilldowns' && drilldownsCount > 0
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                'relative flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-lg',
                'text-[11px] font-medium transition-colors duration-150',
                isActive
                  ? 'bg-white/[0.08] text-ink shadow-sm'
                  : 'text-ink-muted hover:text-ink hover:bg-white/[0.04]',
              )}
            >
              <Icon className="w-3.5 h-3.5" />
              <span>{tab.label}</span>
              {showBadge && (
                <span className="px-1 py-px rounded-full bg-accent-lineage/20 text-accent-lineage text-[9px] font-bold tabular-nums">
                  {drilldownsCount}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* Tab body */}
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={MOTION.stepSwap}
        >
          {activeTab === 'overview' && (
            <TraceOverviewTab
              result={trace.result}
              focusNode={focusNode}
              totalNodes={trace.statistics.totalNodes}
              totalEdges={trace.statistics.totalEdges}
              upstreamCount={trace.upstreamCount}
              downstreamCount={trace.downstreamCount}
              resolveEdgeColor={resolveEdgeColor}
            />
          )}
          {activeTab === 'drilldowns' && (
            <TraceDrilldownsTab
              drilldowns={trace.drilldowns}
              displayMap={displayMap}
              onCollapse={trace.collapseDrilldown}
            />
          )}
          {activeTab === 'filters' && (
            <TraceFiltersTab
              availableEdgeTypes={availableEdgeTypes}
              selectedEdgeTypes={trace.config.lineageEdgeTypes}
              onChangeSelectedEdgeTypes={types => trace.setConfig({ lineageEdgeTypes: types })}
              resolveEdgeColor={resolveEdgeColor}
              showUpstream={trace.showUpstream}
              showDownstream={trace.showDownstream}
              onSetShowUpstream={trace.setShowUpstream}
              onSetShowDownstream={trace.setShowDownstream}
              onApply={() => { trace.retrace() }}
            />
          )}
          {activeTab === 'settings' && (
            <TraceSettingsTab
              config={trace.config}
              granularityOptions={granularityOptions}
              onChangeConfig={trace.setConfig}
              onApply={() => { trace.retrace() }}
            />
          )}
          {activeTab === 'performance' && (
            <TracePerformanceTab meta={trace.result?.meta} />
          )}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  )
}
