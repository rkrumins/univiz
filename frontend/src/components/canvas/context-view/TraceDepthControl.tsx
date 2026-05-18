/**
 * TraceDepthControl — header affordance for the active trace's depth.
 *
 * Mounted next to the Trace button and rendered ONLY when a trace is
 * active. The trigger shows the current upstream/downstream hop counts
 * with EntityDrawer-aligned colors (blue = upstream / Root Cause, green =
 * downstream / Impact). The popover exposes a slider + stepper per
 * direction plus preset shortcuts. Edits propagate via `onChange`; the
 * parent wires this to `trace.setConfig` + `retrace()` so the canvas
 * reflects the change without a manual re-trace.
 *
 * Depth values: 0 disables the side (matches the dock direction-arrow
 * contract), 1..MAX_DEPTH is the literal hop count passed to /trace/v2.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { ArrowUp, ArrowDown, ChevronDown, Minus, Plus, Workflow } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface TraceDepthControlProps {
  upstreamDepth: number
  downstreamDepth: number
  onChange: (dir: 'upstream' | 'downstream', value: number) => void
}

const POPOVER_WIDTH = 360
const MAX_DEPTH = 100
const MIN_DEPTH = 0

interface DepthPreset {
  label: string
  upstream: number
  downstream: number
}

const DEPTH_PRESETS: DepthPreset[] = [
  { label: 'Default', upstream: 25, downstream: 25 },
  { label: 'Deep', upstream: 50, downstream: 50 },
  { label: 'Max', upstream: 100, downstream: 100 },
]

function clampDepth(v: number): number {
  if (Number.isNaN(v)) return 0
  return Math.max(MIN_DEPTH, Math.min(MAX_DEPTH, Math.round(v)))
}

export function TraceDepthControl({
  upstreamDepth,
  downstreamDepth,
  onChange,
}: TraceDepthControlProps) {
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    if (!open) return
    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect()
      if (!rect) return
      setAnchor({
        top: rect.bottom + 8,
        right: window.innerWidth - rect.right,
      })
    }
    update()
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true)
    return () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node
      const insideTrigger = triggerRef.current?.contains(target) ?? false
      const insidePopover = popoverRef.current?.contains(target) ?? false
      if (!insideTrigger && !insidePopover) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={`Trace depth — ${upstreamDepth} upstream, ${downstreamDepth} downstream`}
        className={cn(
          'flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium transition-all duration-300',
          open
            ? 'bg-accent-lineage/15 border border-accent-lineage/35 text-ink shadow-sm shadow-accent-lineage/10 dark:bg-accent-lineage/20 dark:border-accent-lineage/30'
            : 'bg-black/[0.04] border border-black/[0.10] text-ink-muted hover:bg-black/[0.08] hover:text-ink dark:bg-white/[0.04] dark:border-white/[0.08] dark:hover:bg-white/[0.08]',
        )}
      >
        <Workflow className="w-3.5 h-3.5" />
        <span className="flex items-center gap-1 tabular-nums">
          <ArrowUp className="w-3 h-3 text-blue-500 dark:text-blue-400" strokeWidth={2.4} />
          <span className="text-blue-600 dark:text-blue-400 font-semibold">{upstreamDepth}</span>
          <span className="opacity-30 mx-0.5">·</span>
          <ArrowDown className="w-3 h-3 text-green-500 dark:text-green-400" strokeWidth={2.4} />
          <span className="text-green-600 dark:text-green-400 font-semibold">{downstreamDepth}</span>
        </span>
        <ChevronDown
          className={cn('w-3 h-3 transition-transform duration-200', open && 'rotate-180')}
        />
      </button>

      {typeof document !== 'undefined' && createPortal(
        <AnimatePresence>
          {open && anchor && (
            <motion.div
              ref={popoverRef}
              initial={{ opacity: 0, y: -6, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.97 }}
              transition={{ duration: 0.15, ease: 'easeOut' }}
              role="dialog"
              aria-label="Trace depth settings"
              style={{
                position: 'fixed',
                top: anchor.top,
                right: anchor.right,
                width: POPOVER_WIDTH,
                zIndex: 1000,
              }}
              className="rounded-xl bg-canvas-elevated/95 backdrop-blur-xl border border-black/[0.10] dark:border-white/[0.08] shadow-2xl shadow-black/20 dark:shadow-black/40 overflow-hidden"
            >
              <div className="px-3 pt-3 pb-1 flex items-center gap-2 border-b border-black/[0.06] dark:border-white/[0.04]">
                <div className="w-6 h-6 rounded-lg bg-gradient-to-br from-accent-lineage/25 to-purple-500/15 flex items-center justify-center">
                  <Workflow className="w-3.5 h-3.5 text-accent-lineage" strokeWidth={2.2} />
                </div>
                <div className="text-[12px] font-semibold text-ink tracking-tight">Trace Depth</div>
              </div>

              <div className="px-3 pt-2.5 pb-2">
                <p className="px-1 pb-2 text-[11px] text-ink-muted/80 leading-snug">
                  Hop count per direction. Set a side to 0 to disable it; the dock arrows mirror this.
                </p>

                <DepthRow
                  label="Upstream"
                  sublabel="Root Cause"
                  variant="upstream"
                  value={upstreamDepth}
                  onChange={v => onChange('upstream', v)}
                />
                <DepthRow
                  label="Downstream"
                  sublabel="Impact"
                  variant="downstream"
                  value={downstreamDepth}
                  onChange={v => onChange('downstream', v)}
                />
              </div>

              <div className="h-px bg-black/[0.08] dark:bg-white/[0.06] mx-3" />

              <div className="px-3 py-2.5 flex items-center gap-1.5">
                {DEPTH_PRESETS.map(preset => {
                  const active = upstreamDepth === preset.upstream && downstreamDepth === preset.downstream
                  return (
                    <button
                      key={preset.label}
                      type="button"
                      aria-pressed={active}
                      onClick={() => {
                        onChange('upstream', preset.upstream)
                        onChange('downstream', preset.downstream)
                      }}
                      className={cn(
                        'flex-1 px-2 py-1.5 rounded-lg text-[11px] font-medium border transition-colors',
                        active
                          ? 'bg-accent-lineage/15 text-accent-lineage border-accent-lineage/40 shadow-sm shadow-accent-lineage/10 dark:bg-accent-lineage/20 dark:border-accent-lineage/35'
                          : 'bg-black/[0.04] text-ink-muted hover:text-ink hover:bg-black/[0.08] border-black/[0.10] dark:bg-white/[0.04] dark:border-white/[0.06] dark:hover:bg-white/[0.08]',
                      )}
                    >
                      <span className="block leading-none">{preset.label}</span>
                      <span className="block text-[10px] font-normal opacity-70 mt-0.5 tabular-nums">
                        {preset.upstream}/{preset.downstream}
                      </span>
                    </button>
                  )
                })}
              </div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}

type Variant = 'upstream' | 'downstream'

const VARIANT_CLASSES: Record<Variant, {
  icon: string
  text: string
  sub: string
  sliderTrack: string
  sliderThumb: string
  sliderFocus: string
}> = {
  upstream: {
    icon: 'text-blue-500 dark:text-blue-400',
    text: 'text-blue-600 dark:text-blue-400',
    sub: 'text-blue-500/60 dark:text-blue-400/60',
    sliderTrack: 'bg-blue-500/15 dark:bg-blue-400/15',
    sliderThumb: '[&::-webkit-slider-thumb]:bg-blue-500 [&::-moz-range-thumb]:bg-blue-500',
    sliderFocus: 'focus:bg-blue-500/10',
  },
  downstream: {
    icon: 'text-green-500 dark:text-green-400',
    text: 'text-green-600 dark:text-green-400',
    sub: 'text-green-500/60 dark:text-green-400/60',
    sliderTrack: 'bg-green-500/15 dark:bg-green-400/15',
    sliderThumb: '[&::-webkit-slider-thumb]:bg-green-500 [&::-moz-range-thumb]:bg-green-500',
    sliderFocus: 'focus:bg-green-500/10',
  },
}

/**
 * One row of the Trace Depth control — paired slider and stepper for a
 * single direction. The slider drives coarse scrubbing; the number
 * input + minus/plus buttons handle precise edits and keyboard input.
 * All three controls share `onChange` so they stay in sync. Direction
 * is color-coded to match EntityDrawer's Root Cause (blue) / Impact
 * (green) treatment.
 */
function DepthRow({
  label,
  sublabel,
  variant,
  value,
  onChange,
}: {
  label: string
  sublabel: string
  variant: Variant
  value: number
  onChange: (v: number) => void
}) {
  const c = VARIANT_CLASSES[variant]
  const Icon = variant === 'upstream' ? ArrowUp : ArrowDown
  return (
    <div className="px-1 py-1.5">
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1.5 w-[110px] flex-shrink-0">
          <Icon className={cn('w-3.5 h-3.5', c.icon)} strokeWidth={2.4} />
          <div className="flex flex-col leading-tight">
            <span className={cn('text-[12px] font-medium', c.text)}>{label}</span>
            <span className={cn('text-[10px]', c.sub)}>{sublabel}</span>
          </div>
        </div>
        <input
          type="range"
          min={MIN_DEPTH}
          max={MAX_DEPTH}
          value={value}
          onChange={e => onChange(clampDepth(Number(e.target.value)))}
          aria-label={`${label} depth slider`}
          className={cn(
            'flex-1 h-1.5 rounded-full appearance-none cursor-pointer',
            c.sliderTrack,
            '[&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-canvas-elevated [&::-webkit-slider-thumb]:shadow-md [&::-webkit-slider-thumb]:cursor-grab [&::-webkit-slider-thumb]:transition-transform [&::-webkit-slider-thumb]:active:scale-110',
            '[&::-moz-range-thumb]:w-3.5 [&::-moz-range-thumb]:h-3.5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-canvas-elevated [&::-moz-range-thumb]:cursor-grab',
            c.sliderThumb,
          )}
        />
        <div className="flex items-stretch rounded-lg overflow-hidden border border-black/[0.10] dark:border-white/[0.08] bg-black/[0.03] dark:bg-white/[0.03] flex-shrink-0">
          <button
            type="button"
            onClick={() => onChange(clampDepth(value - 1))}
            disabled={value <= MIN_DEPTH}
            aria-label={`Decrease ${label.toLowerCase()} depth`}
            className="px-1.5 py-1 text-ink-muted hover:text-ink hover:bg-black/[0.06] dark:hover:bg-white/[0.06] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <Minus className="w-3 h-3" strokeWidth={2.4} />
          </button>
          <input
            type="number"
            min={MIN_DEPTH}
            max={MAX_DEPTH}
            value={value}
            onChange={e => onChange(clampDepth(Number(e.target.value)))}
            aria-label={`${label} depth`}
            className={cn(
              'w-10 text-center text-[12px] font-semibold tabular-nums bg-transparent text-ink focus:outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none',
              c.sliderFocus,
            )}
          />
          <button
            type="button"
            onClick={() => onChange(clampDepth(value + 1))}
            disabled={value >= MAX_DEPTH}
            aria-label={`Increase ${label.toLowerCase()} depth`}
            className="px-1.5 py-1 text-ink-muted hover:text-ink hover:bg-black/[0.06] dark:hover:bg-white/[0.06] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <Plus className="w-3 h-3" strokeWidth={2.4} />
          </button>
        </div>
      </div>
    </div>
  )
}
