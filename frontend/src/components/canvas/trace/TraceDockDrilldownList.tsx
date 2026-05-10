import { motion, AnimatePresence } from 'framer-motion'
import { ArrowRight, X, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceV2Result } from '@/providers/GraphDataProvider'
import type { DrilldownKey } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'

export interface TraceDockDrilldownListProps {
  drilldowns: Map<DrilldownKey, TraceV2Result>
  displayMap: Map<string, HierarchyNode>
  onCollapse: (key: DrilldownKey) => void
}

interface ParsedKey {
  sourceUrn: string
  targetUrn: string
  level: number
}

function parseKey(key: DrilldownKey): ParsedKey | null {
  const at = key.lastIndexOf('@')
  if (at < 0) return null
  const pair = key.slice(0, at)
  const levelStr = key.slice(at + 1)
  const arrow = pair.indexOf('->')
  if (arrow < 0) return null
  return {
    sourceUrn: pair.slice(0, arrow),
    targetUrn: pair.slice(arrow + 2),
    level: Number(levelStr),
  }
}

/**
 * Compact inline list of active drilldowns. Empty state stays out of the way
 * (just a hint). Each row is a single dense line with collapse button.
 */
export function TraceDockDrilldownList({
  drilldowns,
  displayMap,
  onCollapse,
}: TraceDockDrilldownListProps) {
  const entries = Array.from(drilldowns.entries())

  if (entries.length === 0) {
    return (
      <div className="px-4 py-2 flex items-center gap-2 text-[11px] text-ink-muted/60 border-b border-glass-border/30">
        <Layers className="w-3 h-3 opacity-60 shrink-0" />
        <span>No drilldowns active. Double-click an aggregated edge in the canvas to drill in.</span>
      </div>
    )
  }

  return (
    <div className="px-4 py-2 flex flex-col gap-1 border-b border-glass-border/30">
      <div className="flex items-center gap-1.5 mb-0.5">
        <Layers className="w-3 h-3 text-ink-muted/60" />
        <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          Drilldowns
        </span>
        <span className="text-[10px] text-ink-muted/60 tabular-nums">({entries.length})</span>
      </div>
      <AnimatePresence initial={false}>
        {entries.map(([key, v2], idx) => {
          const parsed = parseKey(key)
          if (!parsed) return null
          const sourceName = displayMap.get(parsed.sourceUrn)?.name ?? parsed.sourceUrn
          const targetName = displayMap.get(parsed.targetUrn)?.name ?? parsed.targetUrn
          const edgeCount = v2.edges?.length ?? 0
          return (
            <motion.div
              key={key}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 8 }}
              transition={{ duration: 0.15, delay: idx * 0.03 }}
              className={cn(
                'flex items-center gap-2 px-2 py-1 rounded-md text-[11px]',
                'hover:bg-white/[0.04] transition-colors group',
              )}
            >
              <span className="px-1 py-px rounded bg-accent-lineage/10 text-accent-lineage text-[9px] font-bold tabular-nums uppercase tracking-wide shrink-0">
                L{parsed.level - 1}→L{parsed.level}
              </span>
              <div className="flex items-center gap-1 min-w-0 flex-1">
                <span className="text-ink truncate" title={sourceName}>{sourceName}</span>
                <ArrowRight className="w-2.5 h-2.5 text-ink-muted/60 shrink-0" />
                <span className="text-ink truncate" title={targetName}>{targetName}</span>
              </div>
              <span className="text-ink-muted/70 tabular-nums shrink-0">{edgeCount} edges</span>
              <button
                type="button"
                onClick={() => onCollapse(key)}
                title="Collapse drilldown"
                className="inline-flex items-center justify-center w-5 h-5 rounded text-ink-muted/40 hover:text-rose-500 hover:bg-rose-500/10 transition-colors opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
              >
                <X className="w-3 h-3" />
              </button>
            </motion.div>
          )
        })}
      </AnimatePresence>
    </div>
  )
}
