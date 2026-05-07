import React from 'react'
import { Activity, Database, Clock, Zap } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceMeta } from '@/services/traceApi'

export interface TracePerformanceTabProps {
  meta: TraceMeta | undefined
}

const REGIME_STYLES: Record<string, string> = {
  materialized: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 ring-emerald-500/30',
  runtime: 'bg-blue-500/15 text-blue-600 dark:text-blue-400 ring-blue-500/30',
  demoted: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 ring-amber-500/30',
}

const CACHE_STYLES: Record<string, string> = {
  hit: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 ring-emerald-500/30',
  miss: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 ring-amber-500/30',
  bypass: 'bg-white/5 text-ink-muted ring-glass-border/40',
}

export function TracePerformanceTab({ meta }: TracePerformanceTabProps) {
  if (!meta) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
        <Activity className="w-8 h-8 text-ink-muted/30" />
        <p className="text-xs text-ink-muted max-w-[280px]">
          Performance metrics aren't available for this trace.
        </p>
      </div>
    )
  }

  const hitPct = Math.round((meta.materializedHitRate ?? 0) * 100)

  return (
    <div className="flex flex-col gap-3">
      {/* Top-level metrics row */}
      <div className="flex flex-wrap items-center gap-2">
        <MetricBadge
          icon={<Database className="w-3.5 h-3.5" />}
          label="Cache"
          value={meta.cacheStatus}
          className={CACHE_STYLES[meta.cacheStatus] ?? CACHE_STYLES.bypass}
        />
        <MetricBadge
          icon={<Zap className="w-3.5 h-3.5" />}
          label="Regime"
          value={meta.regime}
          className={REGIME_STYLES[meta.regime] ?? 'bg-white/5 text-ink-muted ring-glass-border/40'}
        />
        <MetricBadge
          icon={<Clock className="w-3.5 h-3.5" />}
          label="Latency"
          value={`${meta.queryMs.toLocaleString()}ms`}
          className="bg-accent-lineage/10 text-accent-lineage ring-accent-lineage/25"
        />
      </div>

      {/* Materialised hit-rate bar */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
            Materialised hit rate
          </span>
          <span className="text-xs font-semibold tabular-nums text-ink">{hitPct}%</span>
        </div>
        <div className="w-full h-1.5 rounded-full bg-white/5 overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-accent-lineage/80 to-accent-lineage rounded-full transition-all duration-500"
            style={{ width: `${hitPct}%` }}
          />
        </div>
      </div>

      {/* Notices and warnings */}
      {meta.warnings.length > 0 && (
        <NoticeList tone="warn" label="Warnings" items={meta.warnings} />
      )}
      {meta.notices.length > 0 && (
        <NoticeList tone="info" label="Notices" items={meta.notices} />
      )}

      {/* Footer metadata */}
      <div className="flex flex-col gap-0.5 pt-1 border-t border-glass-border/30 text-[10px] text-ink-muted/70">
        <div className="flex items-center justify-between">
          <span>Target level</span>
          <span className="tabular-nums">L{meta.targetLevel} ({meta.targetLevelSource})</span>
        </div>
        <div className="flex items-center justify-between">
          <span>Ontology digest</span>
          <span className="font-mono truncate ml-2 max-w-[160px]" title={meta.ontologyDigest}>
            {meta.ontologyDigest}
          </span>
        </div>
      </div>
    </div>
  )
}

interface MetricBadgeProps {
  icon: React.ReactNode
  label: string
  value: string
  className?: string
}

function MetricBadge({ icon, label, value, className }: MetricBadgeProps) {
  return (
    <div className={cn(
      'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg ring-1 ring-inset',
      className,
    )}>
      {icon}
      <span className="text-[10px] uppercase tracking-wider font-semibold opacity-80">
        {label}
      </span>
      <span className="text-xs font-semibold tabular-nums uppercase">{value}</span>
    </div>
  )
}

interface NoticeListProps {
  tone: 'info' | 'warn'
  label: string
  items: string[]
}

function NoticeList({ tone, label, items }: NoticeListProps) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
        {label}
      </span>
      <ul className={cn(
        'flex flex-col gap-1 px-2.5 py-1.5 rounded-lg text-[11px]',
        tone === 'warn'
          ? 'bg-amber-500/10 text-amber-700 dark:text-amber-400'
          : 'bg-accent-lineage/10 text-accent-lineage',
      )}>
        {items.map((item, i) => (
          <li key={i} className="leading-snug">• {item}</li>
        ))}
      </ul>
    </div>
  )
}
