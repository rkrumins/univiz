/**
 * LineageDisplayPopover — consolidates edge-rendering preferences (density +
 * direction arrows) behind a single trigger in the Context View toolbar.
 *
 * Replaces the top-level Stubs/Auto/Raw segmented control and Direction
 * button. Settings are most useful as a grouped, well-described surface
 * rather than competing chips alongside primary actions.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import type { LineageRenderMode } from '@/store/preferences'

interface LineageDisplayPopoverProps {
  lineageRenderMode: LineageRenderMode
  onSetLineageRenderMode: (mode: LineageRenderMode) => void
  showEdgeDirection: boolean
  onToggleEdgeDirection: () => void
}

interface DensityOption {
  mode: LineageRenderMode
  label: string
  technical: string
  description: string
}

const DENSITY_OPTIONS: DensityOption[] = [
  {
    mode: 'stubs',
    label: 'On Hover',
    technical: 'Stubs',
    description: 'Edges appear when you hover or select a node',
  },
  {
    mode: 'auto',
    label: 'Adaptive',
    technical: 'Auto',
    description: 'Real edges on small graphs, stubs above the size threshold',
  },
  {
    mode: 'raw',
    label: 'All Edges',
    technical: 'Raw',
    description: 'Render every projected edge (heavy on dense workspaces)',
  },
]

const MODE_SHORT_LABEL: Record<LineageRenderMode, string> = {
  stubs: 'Stubs',
  auto: 'Auto',
  raw: 'Raw',
}

const POPOVER_WIDTH = 300

export function LineageDisplayPopover({
  lineageRenderMode,
  onSetLineageRenderMode,
  showEdgeDirection,
  onToggleEdgeDirection,
}: LineageDisplayPopoverProps) {
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)

  // Compute popover position from the trigger's viewport rect. Re-runs on
  // open/resize/scroll so the popover stays anchored if the page reflows.
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
        title="Edge density and direction arrows"
        className={cn(
          'flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium transition-all duration-300',
          open
            ? 'bg-accent-lineage/15 border border-accent-lineage/35 text-accent-lineage shadow-sm shadow-accent-lineage/10 dark:bg-accent-lineage/20 dark:border-accent-lineage/30'
            : 'bg-black/[0.04] border border-black/[0.10] text-ink-muted hover:bg-black/[0.08] hover:text-ink dark:bg-white/[0.04] dark:border-white/[0.08] dark:hover:bg-white/[0.08]'
        )}
      >
        <LucideIcons.Sliders className="w-3.5 h-3.5" />
        <span className="tabular-nums">{MODE_SHORT_LABEL[lineageRenderMode]}</span>
        {showEdgeDirection && (
          <span className="flex items-center gap-1.5 text-cyan-700 dark:text-cyan-300">
            <span className="w-px h-3 bg-cyan-500/30 dark:bg-cyan-400/30" />
            <LucideIcons.MoveRight className="w-3.5 h-3.5" strokeWidth={2.2} />
          </span>
        )}
        <LucideIcons.ChevronDown
          className={cn('w-3 h-3 transition-transform duration-200', open && 'rotate-180')}
        />
      </button>

      {/* Portal escapes the header's stacking context (it has backdrop-filter,
          which creates one) so the popover is layered above the canvas body
          and reliably receives clicks. */}
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
              aria-label="Lineage display settings"
              style={{
                position: 'fixed',
                top: anchor.top,
                right: anchor.right,
                width: POPOVER_WIDTH,
                zIndex: 1000,
              }}
              className="rounded-xl bg-canvas-elevated/95 backdrop-blur-xl border border-black/[0.10] dark:border-white/[0.08] shadow-2xl shadow-black/20 dark:shadow-black/40 overflow-hidden"
            >
            {/* Edge Density */}
            <div className="px-3 pt-3 pb-2">
              <div className="flex items-center gap-1.5 px-1 text-[10px] font-semibold tracking-[0.1em] uppercase text-ink-muted/80">
                <LucideIcons.Layers className="w-3 h-3" />
                <span>Edge Density</span>
              </div>
              <div
                role="radiogroup"
                aria-label="Edge density"
                className="mt-2 flex flex-col gap-0.5"
              >
                {DENSITY_OPTIONS.map(opt => {
                  const active = lineageRenderMode === opt.mode
                  return (
                    <button
                      key={opt.mode}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => onSetLineageRenderMode(opt.mode)}
                      className={cn(
                        'flex items-start gap-2.5 px-2.5 py-2 rounded-lg text-left transition-colors',
                        active
                          ? 'bg-accent-lineage/12 dark:bg-accent-lineage/15'
                          : 'hover:bg-black/[0.04] dark:hover:bg-white/[0.04]'
                      )}
                    >
                      <div
                        className={cn(
                          'mt-0.5 w-3.5 h-3.5 rounded-full border-2 flex-shrink-0 flex items-center justify-center transition-colors',
                          active ? 'border-accent-lineage' : 'border-ink-muted/40'
                        )}
                      >
                        {active && (
                          <div className="w-1.5 h-1.5 rounded-full bg-accent-lineage" />
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div
                          className={cn(
                            'text-[12px] font-medium leading-tight flex items-baseline gap-1.5',
                            active ? 'text-accent-lineage' : 'text-ink'
                          )}
                        >
                          <span>{opt.label}</span>
                          <span className="text-[10px] text-ink-muted/60 font-normal">
                            ({opt.technical})
                          </span>
                        </div>
                        <div className="text-[11px] text-ink-muted/80 leading-snug mt-0.5">
                          {opt.description}
                        </div>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>

            <div className="h-px bg-black/[0.08] dark:bg-white/[0.06] mx-3" />

            {/* Direction toggle */}
            <div className="px-3 py-3">
              <button
                type="button"
                role="switch"
                aria-checked={showEdgeDirection}
                onClick={onToggleEdgeDirection}
                className="w-full flex items-center gap-3 px-1.5 py-1 rounded-lg hover:bg-black/[0.04] dark:hover:bg-white/[0.04] transition-colors text-left"
              >
                <div
                  className={cn(
                    'flex-shrink-0 w-[30px] h-[18px] rounded-full relative transition-colors duration-200',
                    showEdgeDirection
                      ? 'bg-cyan-500/85 dark:bg-cyan-400/80'
                      : 'bg-ink-muted/25 dark:bg-white/15'
                  )}
                >
                  <div
                    className={cn(
                      'absolute top-[2px] w-3.5 h-3.5 rounded-full bg-white shadow-sm transition-all duration-200',
                      showEdgeDirection ? 'left-[13px]' : 'left-[2px]'
                    )}
                  />
                </div>
                <div className="min-w-0 flex-1">
                  <div
                    className={cn(
                      'text-[12px] font-medium leading-tight flex items-center gap-1.5',
                      showEdgeDirection ? 'text-cyan-700 dark:text-cyan-300' : 'text-ink'
                    )}
                  >
                    <LucideIcons.MoveRight className="w-3.5 h-3.5" strokeWidth={2.2} />
                    <span>Direction</span>
                  </div>
                  <div className="text-[11px] text-ink-muted/80 leading-snug mt-0.5">
                    Show arrow markers on edges
                  </div>
                </div>
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
