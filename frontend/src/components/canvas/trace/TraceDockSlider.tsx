import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export interface TraceDockSliderProps {
  label: string
  icon: ReactNode
  accent: 'blue' | 'emerald'
  value: number
  min?: number
  max?: number
  onChange: (v: number) => void
  onCommit: () => void
}

const ACCENT = {
  blue: {
    iconBg: 'bg-blue-500',
    iconBorder: 'border-blue-500',
    iconText: 'text-white',
    valueText: 'text-blue-700 dark:text-blue-300',
    fillBg: 'bg-blue-500',
    thumbBg: 'bg-blue-500 border-2 border-white dark:border-blue-100',
    thumbText: 'text-white',
    thumbGlow: 'shadow-[0_0_0_3px_rgba(59,130,246,0.25),0_4px_8px_rgba(59,130,246,0.4)]',
  },
  emerald: {
    iconBg: 'bg-emerald-500',
    iconBorder: 'border-emerald-500',
    iconText: 'text-white',
    valueText: 'text-emerald-700 dark:text-emerald-300',
    fillBg: 'bg-emerald-500',
    thumbBg: 'bg-emerald-500 border-2 border-white dark:border-emerald-100',
    thumbText: 'text-white',
    thumbGlow: 'shadow-[0_0_0_3px_rgba(16,185,129,0.25),0_4px_8px_rgba(16,185,129,0.4)]',
  },
} as const

/**
 * High-contrast slider — solid accent icon container, solid track fill,
 * solid pill thumb with the value baked in. Works in both light and dark
 * mode without any text-on-tint contrast issues. Native input overlays
 * preserve a11y / pointer / keyboard behavior.
 */
export function TraceDockSlider({
  label,
  icon,
  accent,
  value,
  min = 0,
  max = 50,
  onChange,
  onCommit,
}: TraceDockSliderProps) {
  const palette = ACCENT[accent]
  const range = max - min || 1
  const pct = Math.max(0, Math.min(100, ((value - min) / range) * 100))

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <div
          className={cn(
            'shrink-0 w-6 h-6 rounded-md flex items-center justify-center',
            palette.iconBg,
            'border',
            palette.iconBorder,
          )}
        >
          <span className={palette.iconText} aria-hidden>{icon}</span>
        </div>
        <span className="text-[11px] uppercase tracking-[0.14em] font-bold text-ink">
          {label}
        </span>
        <span className={cn('ml-auto text-sm font-bold tabular-nums', palette.valueText)}>
          {value}
        </span>
        <span className="text-[10px] uppercase tracking-[0.14em] font-semibold text-ink-muted">
          / {max}
        </span>
      </div>

      <div className="relative h-6 group px-1">
        {/* Track background */}
        <div className="absolute top-1/2 -translate-y-1/2 inset-x-2 h-1 rounded-full bg-white/[0.10] overflow-hidden border border-white/[0.05]">
          <div
            className={cn('h-full rounded-full transition-all duration-150', palette.fillBg)}
            style={{ width: `${pct}%` }}
          />
        </div>

        {/* Quartile tick marks — clearly visible */}
        <div className="absolute top-1/2 -translate-y-1/2 inset-x-2 h-1 flex items-center justify-between pointer-events-none">
          {[0, 25, 50, 75, 100].map(p => (
            <span
              key={p}
              className={cn(
                'block w-px h-2 rounded-full transition-colors duration-150',
                p <= pct ? 'bg-white/60' : 'bg-white/20',
              )}
            />
          ))}
        </div>

        {/* Thumb pill — solid color, value baked in */}
        <div
          className={cn(
            'absolute top-1/2 inline-flex items-center justify-center min-w-[30px] h-[22px] px-1.5 rounded-full',
            'text-[11px] font-bold tabular-nums leading-none',
            'pointer-events-none transition-transform duration-150',
            palette.thumbBg,
            palette.thumbText,
            palette.thumbGlow,
            'group-hover:scale-110 group-active:scale-95',
          )}
          style={{
            left: `calc(${pct}% + 4px - ${pct * 0.08}px)`,
            transform: `translate(-${pct}%, -50%)`,
          }}
          aria-hidden
        >
          {value}
        </div>

        {/* Native input — invisible overlay */}
        <input
          type="range"
          min={min}
          max={max}
          value={value}
          onChange={e => onChange(Number(e.target.value))}
          onMouseUp={onCommit}
          onTouchEnd={onCommit}
          onKeyUp={onCommit}
          aria-label={`${label} depth`}
          aria-valuetext={`${value} levels`}
          className={cn(
            'absolute inset-0 w-full h-full opacity-0 cursor-pointer',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40 focus-visible:rounded-full',
          )}
        />
      </div>
    </div>
  )
}
