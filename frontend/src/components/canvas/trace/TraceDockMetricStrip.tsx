import { useMemo, useState } from 'react'
import { ArrowUp, ArrowDown, GitBranch, Network, Hash, Copy, Check } from 'lucide-react'
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
 * Hero metrics for the Overview tab. High-contrast hierarchy:
 *   - Card background is neutral glass (no hue tint) so values pop
 *   - Icon container is the only tinted surface — small, easy to read
 *   - Big numerals are `text-ink` (bright) for instant scanability
 *   - Accent color drives icons, labels, and an underline strip
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
  const [copied, setCopied] = useState(false)

  const edgeTypeCounts = useMemo(() => {
    const counts = new Map<string, number>()
    result?.lineageResult?.edges.forEach(e => {
      counts.set(e.edgeType, (counts.get(e.edgeType) ?? 0) + 1)
    })
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1])
  }, [result])

  const handleCopyUrn = async () => {
    if (!focusNode?.urn) return
    try {
      await navigator.clipboard.writeText(focusNode.urn)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard might be blocked — silently fail
    }
  }

  return (
    <div className="px-5 pt-4 pb-4 flex flex-col gap-4">
      {focusNode?.urn && (
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="text-[11px] uppercase tracking-[0.14em] font-bold text-ink shrink-0">
            URN
          </span>
          <button
            type="button"
            onClick={handleCopyUrn}
            title={copied ? 'Copied' : 'Copy URN'}
            className={cn(
              'group inline-flex items-center gap-2 px-2.5 h-7 rounded-lg max-w-full min-w-0',
              'bg-white/[0.08] border border-white/[0.15]',
              'transition-all duration-200',
              'hover:bg-white/[0.14] hover:border-accent-lineage/50',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
            )}
          >
            <Hash className="w-3.5 h-3.5 text-ink-muted shrink-0" aria-hidden />
            <span className="font-mono text-xs text-ink truncate" title={focusNode.urn}>
              {focusNode.urn}
            </span>
            {copied ? (
              <Check className="w-4 h-4 text-emerald-600 dark:text-emerald-400 shrink-0" strokeWidth={3} />
            ) : (
              <Copy className="w-3.5 h-3.5 text-ink-muted group-hover:text-ink shrink-0" />
            )}
          </button>
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <HeroMetric
          label="Total Nodes"
          value={totalNodes}
          icon={<Network className="w-4 h-4" strokeWidth={2.2} />}
          accent="lineage"
        />
        <HeroMetric
          label="Upstream"
          value={upstreamCount}
          icon={<ArrowUp className="w-4 h-4" strokeWidth={2.4} />}
          accent="blue"
        />
        <HeroMetric
          label="Downstream"
          value={downstreamCount}
          icon={<ArrowDown className="w-4 h-4" strokeWidth={2.4} />}
          accent="emerald"
        />
        <HeroMetric
          label="Total Edges"
          value={totalEdges}
          icon={<GitBranch className="w-4 h-4" strokeWidth={2.2} />}
          accent="lineage"
        />
      </div>

      {edgeTypeCounts.length > 0 && (
        <div className="flex items-center gap-3 flex-wrap pt-1">
          <span className="text-[11px] uppercase tracking-[0.14em] font-bold text-ink shrink-0">
            By edge type
          </span>
          <div className="flex items-center gap-1.5 flex-wrap">
            {edgeTypeCounts.map(([edgeType, count]) => {
              const color = resolveEdgeColor(edgeType)
              return (
                <span
                  key={edgeType}
                  className={cn(
                    'inline-flex items-center gap-1.5 px-2 h-6 rounded-lg',
                    'bg-white/[0.08] border border-white/[0.15]',
                    'text-[11px]',
                  )}
                >
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: color, boxShadow: `0 0 6px ${color}` }}
                    aria-hidden
                  />
                  <span className="text-ink uppercase tracking-wide font-bold">{edgeType}</span>
                  <span className="text-ink tabular-nums font-bold">{count}</span>
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

interface HeroMetricProps {
  label: string
  value: number
  icon: React.ReactNode
  accent: 'lineage' | 'blue' | 'emerald'
}

const ACCENT = {
  lineage: {
    iconBg: 'bg-accent-lineage',
    iconBorder: 'border-accent-lineage',
    accentLine: 'bg-accent-lineage',
  },
  blue: {
    iconBg: 'bg-blue-500',
    iconBorder: 'border-blue-500',
    accentLine: 'bg-blue-500',
  },
  emerald: {
    iconBg: 'bg-emerald-500',
    iconBorder: 'border-emerald-500',
    accentLine: 'bg-emerald-500',
  },
} as const

function HeroMetric({ label, value, icon, accent }: HeroMetricProps) {
  const display = useCountUp(value)
  const palette = ACCENT[accent]
  return (
    <div
      className={cn(
        'relative flex items-center gap-3 px-3 py-2.5 rounded-xl min-w-0 overflow-hidden',
        // Neutral glass backdrop — no hue tint so values read cleanly in any mode
        'bg-white/[0.06] border border-white/[0.12]',
      )}
    >
      {/* Solid accent stripe — clear hue tell on the left edge */}
      <span
        className={cn('absolute inset-y-2 left-0 w-[3px] rounded-r-full', palette.accentLine)}
        aria-hidden
      />

      <div
        className={cn(
          'relative shrink-0 w-10 h-10 rounded-lg flex items-center justify-center',
          palette.iconBg,
          'border',
          palette.iconBorder,
        )}
      >
        <span className="text-white" aria-hidden>{icon}</span>
      </div>

      <div className="flex flex-col gap-0.5 min-w-0">
        <span className="text-2xl font-display font-bold tabular-nums leading-none tracking-tight text-ink">
          {display.toLocaleString()}
        </span>
        <span className="text-[10px] uppercase tracking-[0.14em] font-bold text-ink-muted truncate">
          {label}
        </span>
      </div>
    </div>
  )
}
