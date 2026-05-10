import { useMemo } from 'react'
import { ArrowUp, ArrowDown, GitBranch, Network, Hash } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { useCountUp } from './useCountUp'

export interface TraceDockMetricStripProps {
  result: TraceResult | null
  focusNode: HierarchyNode | null
  totalNodes: number
  totalEdges: number
  upstreamCount: number
  downstreamCount: number
  resolveEdgeColor: (edgeType: string) => string
}

/**
 * Horizontal metric strip — focus identity + 4 inline animated counts +
 * edge-type breakdown chips. Single dense row(s), no oversized cards.
 */
export function TraceDockMetricStrip({
  result,
  focusNode,
  totalNodes,
  totalEdges,
  upstreamCount,
  downstreamCount,
  resolveEdgeColor,
}: TraceDockMetricStripProps) {
  const edgeTypeCounts = useMemo(() => {
    const counts = new Map<string, number>()
    result?.lineageResult?.edges.forEach(e => {
      counts.set(e.edgeType, (counts.get(e.edgeType) ?? 0) + 1)
    })
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1])
  }, [result])

  return (
    <div className="px-4 py-2.5 flex flex-col gap-2 border-b border-glass-border/30">
      {/* Focus URN row — when there's a focus node */}
      {focusNode && focusNode.urn && (
        <div className="flex items-center gap-1.5 text-[11px] text-ink-muted/70">
          <Hash className="w-3 h-3 opacity-60 shrink-0" />
          <span className="font-mono truncate" title={focusNode.urn}>{focusNode.urn}</span>
        </div>
      )}

      {/* Metrics + edge types — one inline row */}
      <div className="flex items-center gap-3 flex-wrap">
        <Metric
          label="Total"
          value={totalNodes}
          icon={<Network className="w-3 h-3" />}
          accent="lineage"
        />
        <span className="text-ink-muted/30 select-none">·</span>
        <Metric
          label="Upstream"
          value={upstreamCount}
          icon={<ArrowUp className="w-3 h-3" />}
          accent="blue"
        />
        <span className="text-ink-muted/30 select-none">·</span>
        <Metric
          label="Downstream"
          value={downstreamCount}
          icon={<ArrowDown className="w-3 h-3" />}
          accent="emerald"
        />
        <span className="text-ink-muted/30 select-none">·</span>
        <Metric
          label="Edges"
          value={totalEdges}
          icon={<GitBranch className="w-3 h-3" />}
          accent="violet"
        />

        {edgeTypeCounts.length > 0 && (
          <>
            <span className="text-ink-muted/30 select-none">·</span>
            <div className="flex items-center gap-1.5 flex-wrap">
              {edgeTypeCounts.map(([edgeType, count]) => {
                const color = resolveEdgeColor(edgeType)
                return (
                  <span
                    key={edgeType}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] bg-white/5"
                  >
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ backgroundColor: color }}
                      aria-hidden
                    />
                    <span className="text-ink-muted uppercase tracking-wide font-medium">{edgeType}</span>
                    <span className="text-ink-muted/70 tabular-nums font-semibold">{count}</span>
                  </span>
                )
              })}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

interface MetricProps {
  label: string
  value: number
  icon: React.ReactNode
  accent: 'lineage' | 'blue' | 'emerald' | 'violet'
}

const ACCENT_CLASS: Record<MetricProps['accent'], string> = {
  lineage: 'text-accent-lineage',
  blue: 'text-blue-600 dark:text-blue-400',
  emerald: 'text-emerald-600 dark:text-emerald-400',
  violet: 'text-violet-600 dark:text-violet-400',
}

function Metric({ label, value, icon, accent }: MetricProps) {
  const display = useCountUp(value)
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className={cn('inline-flex items-center justify-center w-3.5 h-3.5 self-center', ACCENT_CLASS[accent])}>
        {icon}
      </span>
      <span className={cn('text-[15px] font-bold tabular-nums leading-none', ACCENT_CLASS[accent])}>
        {display.toLocaleString()}
      </span>
      <span className="text-[10px] text-ink-muted uppercase tracking-wider font-semibold">
        {label}
      </span>
    </span>
  )
}
