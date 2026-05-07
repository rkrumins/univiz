import { useMemo } from 'react'
import { GitBranch, Hash, ArrowUp, ArrowDown, Network } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { StatTile } from '../StatTile'

export interface TraceOverviewTabProps {
  result: TraceResult | null
  focusNode: HierarchyNode | null
  totalNodes: number
  totalEdges: number
  upstreamCount: number
  downstreamCount: number
  resolveEdgeColor: (edgeType: string) => string
}

export function TraceOverviewTab({
  result,
  focusNode,
  totalNodes,
  totalEdges,
  upstreamCount,
  downstreamCount,
  resolveEdgeColor,
}: TraceOverviewTabProps) {
  // Edge-type breakdown — count edges by type from the lineage result and any
  // active drilldowns can be added later. Using lineageResult.edges keeps it
  // simple and consistent with what's actually rendered on the canvas.
  const edgeTypeCounts = useMemo(() => {
    const counts = new Map<string, number>()
    result?.lineageResult?.edges.forEach(e => {
      counts.set(e.edgeType, (counts.get(e.edgeType) ?? 0) + 1)
    })
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1])
  }, [result])

  return (
    <div className="flex flex-col gap-3">
      {/* Focus card */}
      {focusNode && (
        <div className="flex flex-col gap-1.5 px-3 py-2.5 rounded-xl bg-white/[0.03] border border-glass-border/40">
          <div className="flex items-center gap-2">
            <GitBranch className="w-3.5 h-3.5 text-accent-lineage" />
            <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
              Focus
            </span>
            {result?.isInherited && (
              <span className="ml-auto px-1.5 py-0.5 rounded-md bg-accent-lineage/10 text-accent-lineage text-[10px] font-medium">
                Inherited
              </span>
            )}
          </div>
          <div className="text-sm font-semibold text-ink truncate" title={focusNode.name}>
            {focusNode.name}
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <Hash className="w-3 h-3 opacity-50" />
            <span className="font-mono truncate" title={focusNode.urn}>
              {focusNode.urn}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="px-1.5 py-0.5 rounded-md bg-accent-lineage/10 text-accent-lineage text-[10px] font-medium uppercase tracking-wide">
              {focusNode.typeId}
            </span>
            {typeof result?.effectiveLevel === 'number' && (
              <span className="px-1.5 py-0.5 rounded-md bg-white/5 text-ink-muted text-[10px] font-medium tabular-nums">
                Level {result.effectiveLevel}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Stat tiles */}
      <div className="grid grid-cols-4 gap-2">
        <StatTile label="Total" value={totalNodes} accent="lineage" icon={<Network className="w-3.5 h-3.5" />} />
        <StatTile label="Upstream" value={upstreamCount} accent="blue" icon={<ArrowUp className="w-3.5 h-3.5" />} />
        <StatTile label="Downstream" value={downstreamCount} accent="emerald" icon={<ArrowDown className="w-3.5 h-3.5" />} />
        <StatTile label="Edges" value={totalEdges} accent="violet" icon={<GitBranch className="w-3.5 h-3.5" />} />
      </div>

      {/* Edge-type breakdown */}
      {edgeTypeCounts.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
            Edge types
          </span>
          <div className="flex flex-wrap gap-1.5">
            {edgeTypeCounts.map(([edgeType, count]) => {
              const color = resolveEdgeColor(edgeType)
              return (
                <span
                  key={edgeType}
                  className={cn(
                    'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-[11px]',
                    'bg-white/5 border border-glass-border/40',
                  )}
                >
                  <span
                    className="w-2 h-2 rounded-full"
                    style={{ backgroundColor: color }}
                    aria-hidden
                  />
                  <span className="text-ink-muted">{edgeType}</span>
                  <span className="text-ink-muted/60 tabular-nums font-medium">{count}</span>
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
