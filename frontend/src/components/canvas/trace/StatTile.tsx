import React from 'react'
import { cn } from '@/lib/utils'
import { useCountUp } from './useCountUp'

export interface StatTileProps {
  label: string
  value: number
  /** Tailwind hue (e.g. 'blue', 'emerald', 'violet'). Defaults to neutral lineage accent. */
  accent?: 'lineage' | 'blue' | 'emerald' | 'amber' | 'violet'
  icon?: React.ReactNode
}

const ACCENT_STYLES: Record<NonNullable<StatTileProps['accent']>, string> = {
  lineage: 'from-accent-lineage/15 via-accent-lineage/8 ring-accent-lineage/25 text-accent-lineage',
  blue: 'from-blue-500/15 via-blue-500/8 ring-blue-500/25 text-blue-600 dark:text-blue-400',
  emerald: 'from-emerald-500/15 via-emerald-500/8 ring-emerald-500/25 text-emerald-600 dark:text-emerald-400',
  amber: 'from-amber-500/15 via-amber-500/8 ring-amber-500/25 text-amber-600 dark:text-amber-400',
  violet: 'from-violet-500/15 via-violet-500/8 ring-violet-500/25 text-violet-600 dark:text-violet-400',
}

export function StatTile({ label, value, accent = 'lineage', icon }: StatTileProps) {
  const display = useCountUp(value)
  const accentClass = ACCENT_STYLES[accent]

  return (
    <div
      className={cn(
        'relative flex flex-col gap-1 rounded-2xl px-3 py-2.5',
        'bg-gradient-to-br to-transparent',
        'ring-1 ring-inset border border-glass-border/40',
        accentClass,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          {label}
        </span>
        {icon && <span className="opacity-70">{icon}</span>}
      </div>
      <span className="text-xl font-bold tabular-nums leading-none">
        {display.toLocaleString()}
      </span>
    </div>
  )
}
