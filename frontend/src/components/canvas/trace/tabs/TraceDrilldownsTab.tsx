import { ArrowRight, X, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceV2Result } from '@/providers/GraphDataProvider'
import type { DrilldownKey } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'

export interface TraceDrilldownsTabProps {
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
  // key format: `${sourceUrn}->${targetUrn}@${level}`
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

export function TraceDrilldownsTab({
  drilldowns,
  displayMap,
  onCollapse,
}: TraceDrilldownsTabProps) {
  const entries = Array.from(drilldowns.entries())

  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
        <Layers className="w-8 h-8 text-ink-muted/30" />
        <p className="text-xs text-ink-muted max-w-[280px]">
          Double-click an aggregated edge in the canvas to drill into its
          underlying lineage at a finer level.
        </p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-1.5">
      {entries.map(([key, v2]) => {
        const parsed = parseKey(key)
        if (!parsed) return null
        const sourceName = displayMap.get(parsed.sourceUrn)?.name ?? parsed.sourceUrn
        const targetName = displayMap.get(parsed.targetUrn)?.name ?? parsed.targetUrn
        const edgeCount = v2.edges?.length ?? 0
        return (
          <div
            key={key}
            className={cn(
              'flex items-center gap-2 px-3 py-2 rounded-xl',
              'bg-white/[0.03] border border-glass-border/40',
              'hover:bg-white/[0.05] transition-colors',
            )}
          >
            <div className="flex items-center gap-1.5 min-w-0 flex-1">
              <span className="text-[12px] text-ink truncate" title={sourceName}>
                {sourceName}
              </span>
              <ArrowRight className="w-3 h-3 text-ink-muted/60 shrink-0" />
              <span className="text-[12px] text-ink truncate" title={targetName}>
                {targetName}
              </span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <span className="px-1.5 py-0.5 rounded-md bg-accent-lineage/10 text-accent-lineage text-[10px] font-semibold tabular-nums">
                L{parsed.level - 1} → L{parsed.level}
              </span>
              <span className="px-1.5 py-0.5 rounded-md bg-white/5 text-ink-muted text-[10px] font-medium tabular-nums">
                {edgeCount} edge{edgeCount === 1 ? '' : 's'}
              </span>
              <button
                type="button"
                onClick={() => onCollapse(key)}
                title="Collapse drilldown"
                className="ml-1 inline-flex items-center justify-center w-6 h-6 rounded-md text-ink-muted/60 hover:text-ink hover:bg-rose-500/10 hover:text-rose-500 transition-colors"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
