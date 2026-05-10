import { motion, AnimatePresence } from 'framer-motion'
import { ArrowRight, X, Layers3, MousePointerClick } from 'lucide-react'
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
 * Drilldowns tab — empty state has the app's gradient identity icon
 * treatment (matches ContextViewHeader's pattern); populated state has
 * a sticky pill-card header and gradient-row list with hover lift.
 */
export function TraceDockDrilldownList({
  drilldowns,
  displayMap,
  onCollapse,
}: TraceDockDrilldownListProps) {
  const entries = Array.from(drilldowns.entries())

  if (entries.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center px-8 py-10 text-center">
        <div
          className={cn(
            'relative w-14 h-14 rounded-2xl flex items-center justify-center mb-4',
            'bg-gradient-to-br from-accent-lineage to-purple-600',
            'border border-accent-lineage/60',
            'shadow-lg shadow-accent-lineage/30',
          )}
        >
          <Layers3 className="w-6 h-6 text-white" strokeWidth={2.2} />
          <span
            className={cn(
              'absolute -bottom-1 -right-1 w-6 h-6 rounded-xl flex items-center justify-center',
              'bg-canvas-elevated border border-white/[0.20] shadow-md',
            )}
            aria-hidden
          >
            <MousePointerClick className="w-3.5 h-3.5 text-ink" strokeWidth={2.4} />
          </span>
        </div>
        <p className="text-sm font-display font-semibold text-ink mb-1.5 tracking-tight">
          No drilldowns active
        </p>
        <p className="text-xs text-ink-muted max-w-xs leading-relaxed">
          Double-click an aggregated edge in the canvas to drill into its underlying connections — they'll appear here.
        </p>
      </div>
    )
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <div
        className={cn(
          'sticky top-0 z-10 px-5 py-2.5 flex items-center justify-between',
          'bg-canvas-elevated/95 backdrop-blur-xl border-b border-white/[0.06]',
        )}
      >
        <div className="flex items-center gap-2.5">
          <div
            className={cn(
              'w-7 h-7 rounded-lg flex items-center justify-center',
              'bg-accent-lineage border border-accent-lineage',
              'shadow-sm shadow-accent-lineage/20',
            )}
          >
            <Layers3 className="w-4 h-4 text-white" strokeWidth={2.4} />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="text-[11px] uppercase tracking-[0.14em] font-bold text-ink">
              Active drilldowns
            </span>
            <span className="text-[10px] text-ink-muted tabular-nums">
              {entries.length} {entries.length === 1 ? 'expansion' : 'expansions'}
            </span>
          </div>
        </div>
        <span
          className={cn(
            'inline-flex items-center justify-center min-w-[24px] h-6 px-2 rounded-lg',
            'bg-accent-lineage border border-accent-lineage',
            'text-white text-xs font-bold tabular-nums leading-none',
          )}
        >
          {entries.length}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar px-3 py-2 flex flex-col gap-1.5">
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
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 4, scale: 0.98 }}
                transition={{ duration: 0.18, delay: idx * 0.03 }}
                className={cn(
                  'group relative flex items-center gap-3 px-3 py-2 rounded-xl',
                  'bg-white/[0.04] border border-white/[0.10]',
                  'hover:bg-white/[0.10] hover:border-accent-lineage/40',
                  'hover:shadow-md hover:shadow-accent-lineage/10',
                  'transition-all duration-200',
                )}
              >
                <span
                  className={cn(
                    'shrink-0 inline-flex items-center gap-0.5 px-2 h-6 rounded-lg',
                    'bg-accent-lineage border border-accent-lineage',
                    'text-white text-[10px] font-bold tabular-nums uppercase tracking-wider',
                  )}
                  title={`From level ${parsed.level - 1} to level ${parsed.level}`}
                >
                  <span>L{parsed.level - 1}</span>
                  <ArrowRight className="w-3 h-3 opacity-70" strokeWidth={2.4} />
                  <span>L{parsed.level}</span>
                </span>

                <div className="flex items-center gap-2 min-w-0 flex-1 text-xs">
                  <span className="text-ink truncate font-semibold tracking-tight" title={sourceName}>
                    {sourceName}
                  </span>
                  <ArrowRight className="w-3.5 h-3.5 text-accent-lineage/50 shrink-0" strokeWidth={2.2} />
                  <span className="text-ink truncate font-semibold tracking-tight" title={targetName}>
                    {targetName}
                  </span>
                </div>

                <span
                  className={cn(
                    'shrink-0 inline-flex items-center gap-1 px-2 h-6 rounded-lg',
                    'bg-white/[0.08] border border-white/[0.15]',
                  )}
                >
                  <span className="text-xs font-bold tabular-nums text-ink">
                    {edgeCount.toLocaleString()}
                  </span>
                  <span className="text-[10px] uppercase tracking-wider text-ink-muted">edges</span>
                </span>

                <button
                  type="button"
                  onClick={() => onCollapse(key)}
                  title="Collapse drilldown"
                  aria-label={`Collapse drilldown from ${sourceName} to ${targetName}`}
                  className={cn(
                    'shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-lg',
                    'bg-white/[0.06] border border-white/[0.15] text-ink-muted',
                    'hover:bg-rose-500 hover:text-white hover:border-rose-500',
                    'transition-all duration-150 opacity-0 group-hover:opacity-100',
                    'focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/40',
                  )}
                >
                  <X className="w-4 h-4" strokeWidth={2.4} />
                </button>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>
    </div>
  )
}
