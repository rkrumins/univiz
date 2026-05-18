/**
 * TraceDepthControl — compact header affordance for the trace depth.
 *
 * Always renders the current upstream/downstream depths so users see
 * the active scope at a glance. The popover exposes per-direction
 * steppers (0–50) plus a "10/10" preset button. When a trace is
 * already active, edits fire `onChange` which the parent wires to
 * `trace.setConfig` + `retrace()` so the canvas reflects the new
 * depth without a manual re-trace.
 *
 * Depth values:
 *   - 0     = direction disabled (server-side filter; matches the
 *             dock direction-arrow contract)
 *   - 1..50 = literal hop count passed to /trace/v2
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { ArrowUp, ArrowDown, ChevronDown, Minus, Plus, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface TraceDepthControlProps {
  upstreamDepth: number
  downstreamDepth: number
  onChange: (dir: 'upstream' | 'downstream', value: number) => void
}

const POPOVER_WIDTH = 280
const MAX_DEPTH = 50
const MIN_DEPTH = 0

function clamp(v: number): number {
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

  const summary = `${upstreamDepth}/${downstreamDepth}`

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
          'flex items-center gap-1.5 px-2.5 py-2 rounded-xl text-xs font-medium transition-all duration-300',
          open
            ? 'bg-accent-lineage/15 border border-accent-lineage/35 text-accent-lineage shadow-sm shadow-accent-lineage/10 dark:bg-accent-lineage/20 dark:border-accent-lineage/30'
            : 'bg-black/[0.04] border border-black/[0.10] text-ink-muted hover:bg-black/[0.08] hover:text-ink dark:bg-white/[0.04] dark:border-white/[0.08] dark:hover:bg-white/[0.08]',
        )}
      >
        <Layers className="w-3.5 h-3.5" />
        <span className="flex items-center gap-0.5 tabular-nums">
          <ArrowUp className="w-3 h-3 opacity-70" strokeWidth={2.4} />
          <span>{upstreamDepth}</span>
          <span className="opacity-40 mx-0.5">·</span>
          <ArrowDown className="w-3 h-3 opacity-70" strokeWidth={2.4} />
          <span>{downstreamDepth}</span>
        </span>
        <ChevronDown
          className={cn('w-3 h-3 transition-transform duration-200', open && 'rotate-180')}
        />
        <span className="sr-only">Current depth {summary}</span>
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
              <div className="px-3 pt-3 pb-2">
                <div className="flex items-center gap-1.5 px-1 text-[10px] font-semibold tracking-[0.1em] uppercase text-ink-muted/80">
                  <Layers className="w-3 h-3" />
                  <span>Trace Depth</span>
                </div>
                <p className="px-1 pt-1.5 pb-2 text-[11px] text-ink-muted/80 leading-snug">
                  Hop count for upstream and downstream traversal. Set to 0 to disable that side.
                </p>

                <DepthRow
                  label="Upstream"
                  icon={<ArrowUp className="w-3.5 h-3.5" strokeWidth={2.4} />}
                  value={upstreamDepth}
                  onChange={v => onChange('upstream', v)}
                />
                <DepthRow
                  label="Downstream"
                  icon={<ArrowDown className="w-3.5 h-3.5" strokeWidth={2.4} />}
                  value={downstreamDepth}
                  onChange={v => onChange('downstream', v)}
                />
              </div>

              <div className="h-px bg-black/[0.08] dark:bg-white/[0.06] mx-3" />

              <div className="px-3 py-2.5 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    onChange('upstream', 10)
                    onChange('downstream', 10)
                  }}
                  className="flex-1 px-2 py-1.5 rounded-lg text-[11px] font-medium bg-accent-lineage/10 hover:bg-accent-lineage/20 text-accent-lineage border border-accent-lineage/25 transition-colors"
                >
                  Default 10 / 10
                </button>
                <button
                  type="button"
                  onClick={() => {
                    onChange('upstream', 25)
                    onChange('downstream', 25)
                  }}
                  className="flex-1 px-2 py-1.5 rounded-lg text-[11px] font-medium bg-black/[0.04] hover:bg-black/[0.08] text-ink-muted hover:text-ink border border-black/[0.08] dark:bg-white/[0.04] dark:border-white/[0.06] dark:hover:bg-white/[0.08] transition-colors"
                >
                  Deep 25 / 25
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}

function DepthRow({
  label,
  icon,
  value,
  onChange,
}: {
  label: string
  icon: React.ReactNode
  value: number
  onChange: (v: number) => void
}) {
  return (
    <div className="flex items-center gap-2 px-1 py-1.5">
      <div className="flex items-center gap-1.5 flex-1 text-[12px] font-medium text-ink">
        <span className="text-ink-muted">{icon}</span>
        <span>{label}</span>
      </div>
      <div className="flex items-stretch rounded-lg overflow-hidden border border-black/[0.10] dark:border-white/[0.08] bg-black/[0.03] dark:bg-white/[0.03]">
        <button
          type="button"
          onClick={() => onChange(clamp(value - 1))}
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
          onChange={e => onChange(clamp(Number(e.target.value)))}
          aria-label={`${label} depth`}
          className="w-10 text-center text-[12px] font-semibold tabular-nums bg-transparent text-ink focus:outline-none focus:bg-accent-lineage/10 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
        />
        <button
          type="button"
          onClick={() => onChange(clamp(value + 1))}
          disabled={value >= MAX_DEPTH}
          aria-label={`Increase ${label.toLowerCase()} depth`}
          className="px-1.5 py-1 text-ink-muted hover:text-ink hover:bg-black/[0.06] dark:hover:bg-white/[0.06] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          <Plus className="w-3 h-3" strokeWidth={2.4} />
        </button>
      </div>
    </div>
  )
}
