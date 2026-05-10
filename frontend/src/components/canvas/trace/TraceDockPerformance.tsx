import { Zap, Database, Clock, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceMeta } from '@/services/traceApi'

export interface TraceDockPerformanceProps {
  meta: TraceMeta | undefined
}

const REGIME_TINT: Record<string, string> = {
  materialized: 'text-emerald-600 dark:text-emerald-400',
  runtime: 'text-blue-600 dark:text-blue-400',
  demoted: 'text-amber-600 dark:text-amber-400',
}

const CACHE_TINT: Record<string, string> = {
  hit: 'text-emerald-600 dark:text-emerald-400',
  miss: 'text-amber-600 dark:text-amber-400',
  bypass: 'text-ink-muted',
}

/**
 * Single-line performance badges. Cache · regime · latency · materialised
 * hit-rate, all dense and inline. Empty state when meta isn't emitted.
 */
export function TraceDockPerformance({ meta }: TraceDockPerformanceProps) {
  if (!meta) {
    return (
      <div className="px-4 py-1.5 flex items-center gap-1.5 text-[11px] text-ink-muted/60">
        <Zap className="w-3 h-3 opacity-60" />
        <span>Performance metrics unavailable for this trace.</span>
      </div>
    )
  }

  const hitPct = Math.round((meta.materializedHitRate ?? 0) * 100)

  return (
    <div className="px-4 py-1.5 flex items-center gap-3 text-[11px] flex-wrap">
      <Badge
        icon={<Database className="w-3 h-3" />}
        label="cache"
        value={meta.cacheStatus}
        valueClass={CACHE_TINT[meta.cacheStatus] ?? 'text-ink-muted'}
      />
      <span className="text-ink-muted/30 select-none">·</span>
      <Badge
        icon={<Zap className="w-3 h-3" />}
        label="regime"
        value={meta.regime}
        valueClass={REGIME_TINT[meta.regime] ?? 'text-ink-muted'}
      />
      <span className="text-ink-muted/30 select-none">·</span>
      <Badge
        icon={<Clock className="w-3 h-3" />}
        label="latency"
        value={`${meta.queryMs.toLocaleString()}ms`}
        valueClass="text-accent-lineage"
      />
      <span className="text-ink-muted/30 select-none">·</span>
      <Badge
        icon={<Layers className="w-3 h-3" />}
        label="materialised"
        value={`${hitPct}%`}
        valueClass={hitPct >= 80 ? 'text-emerald-600 dark:text-emerald-400' : 'text-ink'}
      />
      <span className="text-ink-muted/30 select-none">·</span>
      <span className="inline-flex items-center gap-1 text-ink-muted">
        <span className="text-[10px] uppercase tracking-wider font-semibold">level</span>
        <span className="text-ink tabular-nums">L{meta.targetLevel}</span>
        <span className="text-ink-muted/60 text-[10px]">({meta.targetLevelSource})</span>
      </span>
    </div>
  )
}

interface BadgeProps {
  icon: React.ReactNode
  label: string
  value: string
  valueClass?: string
}

function Badge({ icon, label, value, valueClass }: BadgeProps) {
  return (
    <span className="inline-flex items-center gap-1 text-ink-muted">
      <span className="opacity-70">{icon}</span>
      <span className="text-[10px] uppercase tracking-wider font-semibold">{label}</span>
      <span className={cn('font-semibold uppercase tabular-nums', valueClass)}>{value}</span>
    </span>
  )
}
