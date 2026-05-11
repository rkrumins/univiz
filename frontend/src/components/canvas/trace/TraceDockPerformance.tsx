import { Zap, Database, Clock, Layers, Activity } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceMeta } from '@/services/traceApi'

export interface TraceDockPerformanceProps {
  meta: TraceMeta | undefined
}

interface Tone {
  iconBg: string
  iconBorder: string
}

const NEUTRAL: Tone = {
  iconBg: 'bg-white/[0.10]',
  iconBorder: 'border-white/[0.20]',
}

const REGIME_TONE: Record<string, Tone> = {
  materialized: { iconBg: 'bg-emerald-500', iconBorder: 'border-emerald-500' },
  runtime: { iconBg: 'bg-blue-500', iconBorder: 'border-blue-500' },
  demoted: { iconBg: 'bg-amber-500', iconBorder: 'border-amber-500' },
}

const CACHE_TONE: Record<string, Tone> = {
  hit: { iconBg: 'bg-emerald-500', iconBorder: 'border-emerald-500' },
  miss: { iconBg: 'bg-amber-500', iconBorder: 'border-amber-500' },
  bypass: NEUTRAL,
}

const LATENCY_TONE: Tone = {
  iconBg: 'bg-accent-lineage',
  iconBorder: 'border-accent-lineage',
}

/**
 * Performance telemetry — five neutral-glass cells, each with a solid
 * accent icon container on the left and bright `text-ink` values on the
 * right. High contrast in both light and dark mode; no tinted-text-on-
 * tinted-bg fade.
 */
export function TraceDockPerformance({ meta }: TraceDockPerformanceProps) {
  if (!meta) {
    return (
      <div
        className={cn(
          'flex items-center gap-2.5 px-3 h-10 rounded-xl',
          'bg-white/[0.04] border border-white/[0.10]',
          'text-xs text-ink-muted',
        )}
      >
        <Activity className="w-4 h-4" />
        <span>Performance metrics unavailable for this trace.</span>
      </div>
    )
  }

  const hitPct = Math.round((meta.materializedHitRate ?? 0) * 100)
  const cacheTone = CACHE_TONE[meta.cacheStatus] ?? NEUTRAL
  const regimeTone = REGIME_TONE[meta.regime] ?? NEUTRAL
  const materialisedTone: Tone = hitPct >= 80 ? REGIME_TONE.materialized : NEUTRAL

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
      <PerfCell
        icon={<Database className="w-4 h-4" strokeWidth={2.4} />}
        label="Cache"
        value={meta.cacheStatus}
        tone={cacheTone}
      />
      <PerfCell
        icon={<Zap className="w-4 h-4" strokeWidth={2.4} />}
        label="Regime"
        value={meta.regime}
        tone={regimeTone}
      />
      <PerfCell
        icon={<Clock className="w-4 h-4" strokeWidth={2.4} />}
        label="Latency"
        value={`${meta.queryMs.toLocaleString()}ms`}
        tone={LATENCY_TONE}
      />
      <PerfCell
        icon={<Layers className="w-4 h-4" strokeWidth={2.4} />}
        label="Materialised"
        value={`${hitPct}%`}
        tone={materialisedTone}
      />
      <PerfCell
        icon={<Layers className="w-4 h-4" strokeWidth={2.4} />}
        label="Target Level"
        value={`L${meta.targetLevel}`}
        tone={NEUTRAL}
        sublabel={meta.targetLevelSource}
      />
    </div>
  )
}

interface PerfCellProps {
  icon: React.ReactNode
  label: string
  value: string
  tone: Tone
  sublabel?: string
}

function PerfCell({ icon, label, value, tone, sublabel }: PerfCellProps) {
  return (
    <div
      className={cn(
        'flex items-center gap-2.5 px-2.5 py-2 rounded-xl min-w-0',
        'bg-white/[0.04] border border-white/[0.10]',
      )}
    >
      <div
        className={cn(
          'shrink-0 w-8 h-8 rounded-lg flex items-center justify-center',
          tone.iconBg,
          'border',
          tone.iconBorder,
        )}
      >
        <span className="text-white" aria-hidden>{icon}</span>
      </div>
      <div className="flex flex-col gap-0.5 min-w-0">
        <span className="text-[10px] uppercase tracking-[0.14em] font-bold text-ink-muted truncate">
          {label}
        </span>
        <div className="flex items-baseline gap-1.5 min-w-0">
          <span className="text-sm font-bold uppercase tabular-nums tracking-tight truncate text-ink">
            {value}
          </span>
          {sublabel && (
            <span className="text-[10px] text-ink-muted truncate">({sublabel})</span>
          )}
        </div>
      </div>
    </div>
  )
}
