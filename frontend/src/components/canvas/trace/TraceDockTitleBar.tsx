import { useEffect, useRef, useState } from 'react'
import { motion, LayoutGroup } from 'framer-motion'
import {
  ArrowUp,
  ArrowDown,
  ArrowUpDown,
  ChevronDown,
  ChevronUp,
  Clock,
  X,
  Workflow,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { useCountUp } from './useCountUp'
import { TraceRecentPopover } from './TraceRecentPopover'

export interface TraceDockTitleBarProps {
  trace: UseUnifiedTraceResult
  displayMap: Map<string, HierarchyNode>
  expanded: boolean
  onToggleExpanded: () => void
  onExit: () => void
}

type Direction = 'up' | 'both' | 'down'

function deriveDirection(showUpstream: boolean, showDownstream: boolean): Direction {
  if (showUpstream && !showDownstream) return 'up'
  if (!showUpstream && showDownstream) return 'down'
  return 'both'
}

/**
 * The always-visible 56px title row. Mirrors the ContextViewHeader vocabulary:
 *   - 9x9 gradient identity icon ("section badge")
 *   - Gradient-tinted focus chip with type + level micro-pills
 *   - Counts as gradient pills with semantic accent
 *   - Vertical hairline dividers between major groups (gradient)
 *   - rounded-xl buttons with gradient hover + glow shadow
 *
 * A11y model:
 *  - `role="toolbar"` with roving tabindex
 *  - Direction segmented control is `role="radiogroup"`
 *  - Pulsing badge gated by `prefers-reduced-motion`
 *  - Live region announces trace start
 */
export function TraceDockTitleBar({
  trace,
  displayMap,
  expanded,
  onToggleExpanded,
  onExit,
}: TraceDockTitleBarProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const recentTriggerRef = useRef<HTMLButtonElement>(null)
  const [recentOpen, setRecentOpen] = useState(false)
  const [focusedIdx, setFocusedIdx] = useState(0)

  const focusNode = trace.focusId ? displayMap.get(trace.focusId) : undefined
  const focusName = focusNode?.name ?? trace.focusId ?? 'Unknown'
  const focusType = focusNode?.typeId
  const liveMsg = `Tracing ${focusName}${focusType ? `, ${focusType}` : ''}. ${trace.upstreamCount} upstream, ${trace.downstreamCount} downstream nodes.`

  const upDisplay = useCountUp(trace.upstreamCount)
  const downDisplay = useCountUp(trace.downstreamCount)
  const direction = deriveDirection(trace.showUpstream, trace.showDownstream)
  const recentCount = trace.traceHistory.length

  const setDirection = (dir: Direction) => {
    if (dir === 'up') { trace.setShowUpstream(true); trace.setShowDownstream(false) }
    else if (dir === 'down') { trace.setShowUpstream(false); trace.setShowDownstream(true) }
    else { trace.setShowUpstream(true); trace.setShowDownstream(true) }
  }

  const controlsRef = useRef<HTMLElement[]>([])
  useEffect(() => {
    const root = containerRef.current
    if (!root) return
    controlsRef.current = Array.from(root.querySelectorAll<HTMLElement>('[data-trace-control]'))
    controlsRef.current.forEach((el, i) => { el.tabIndex = i === focusedIdx ? 0 : -1 })
  })

  const onKeyDown = (e: React.KeyboardEvent) => {
    const items = controlsRef.current
    if (items.length === 0) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const dir = e.key === 'ArrowRight' ? 1 : -1
      const next = (focusedIdx + dir + items.length) % items.length
      setFocusedIdx(next); items[next].focus()
    } else if (e.key === 'Home') {
      e.preventDefault(); setFocusedIdx(0); items[0].focus()
    } else if (e.key === 'End') {
      e.preventDefault(); setFocusedIdx(items.length - 1); items[items.length - 1].focus()
    }
  }

  const onContainerFocus = (e: React.FocusEvent) => {
    if (e.target === containerRef.current && controlsRef.current[focusedIdx]) {
      controlsRef.current[focusedIdx].focus()
    }
  }

  return (
    <div
      ref={containerRef}
      role="toolbar"
      aria-orientation="horizontal"
      aria-label={`Trace controls for ${focusName}`}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onFocus={onContainerFocus}
      data-canvas-interactive
      className={cn(
        'relative flex items-center gap-3.5 px-5 h-16 shrink-0',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40 focus-visible:ring-inset',
      )}
    >
      <span className="sr-only" aria-live="polite" aria-atomic="true">{liveMsg}</span>

      {/* Subtle ambient gradient overlay — same idiom as ContextViewHeader */}
      <div
        className="absolute inset-0 bg-gradient-to-r from-accent-lineage/[0.04] via-transparent to-purple-500/[0.03] pointer-events-none"
        aria-hidden
      />

      {/* Section identity — solid accent badge for high-contrast readability */}
      <div className="relative flex items-center gap-2.5 shrink-0">
        <div
          className={cn(
            'relative w-10 h-10 rounded-xl flex items-center justify-center shrink-0',
            'bg-gradient-to-br from-accent-lineage to-purple-600',
            'border border-accent-lineage/60',
            'shadow-lg shadow-accent-lineage/30',
          )}
        >
          <Workflow className="w-5 h-5 text-white drop-shadow-[0_0_4px_rgba(168,85,247,0.6)]" strokeWidth={2.4} aria-hidden />
          {/* Pulsing live indicator — anchored to the badge bottom-right */}
          <span
            className="absolute -bottom-0.5 -right-0.5 inline-flex w-2.5 h-2.5"
            aria-hidden="true"
          >
            <span className="absolute inset-0 rounded-full bg-accent-lineage/60 animate-ping motion-reduce:animate-none" />
            <span className="relative w-2.5 h-2.5 rounded-full bg-accent-lineage border-2 border-canvas-elevated" />
          </span>
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-[11px] font-bold text-ink uppercase tracking-[0.18em]">
            {trace.isLoading ? 'Tracing…' : 'Active Trace'}
          </span>
          <span className="text-[10px] text-ink-muted tracking-wide">
            {trace.isLoading ? 'computing lineage' : 'live lineage view'}
          </span>
        </div>
      </div>

      {/* Vertical hairline divider — matches ContextViewHeader idiom */}
      <span
        className="w-px h-8 bg-gradient-to-b from-transparent via-white/15 to-transparent shrink-0"
        aria-hidden
      />

      {/* Focus chip — neutral glass with bright name + accent micro-pills */}
      <div
        className={cn(
          'flex items-center gap-2 px-3 h-9 rounded-xl min-w-0 shrink',
          'bg-white/[0.06] border border-white/[0.12]',
        )}
        title={focusName}
      >
        <span className="text-sm font-display font-semibold text-ink truncate max-w-[180px] tracking-tight">
          {focusName}
        </span>
        {focusType && (
          <span className="hidden xl:inline-flex shrink-0 px-1.5 py-0.5 rounded-md bg-accent-lineage/20 text-accent-lineage text-[10px] font-bold uppercase tracking-wider border border-accent-lineage/30">
            {focusType}
          </span>
        )}
        {typeof trace.result?.effectiveLevel === 'number' && (
          <span className="hidden 2xl:inline-flex shrink-0 px-1.5 py-0.5 rounded-md bg-white/[0.10] text-ink text-[10px] font-bold tabular-nums">
            L{trace.result.effectiveLevel}
          </span>
        )}
      </div>

      {/* Counts — neutral glass pills with accent icon + bright value */}
      <div className="flex items-center gap-2 shrink-0">
        <span
          className={cn(
            'inline-flex items-center gap-1.5 px-2.5 h-9 rounded-xl',
            'bg-white/[0.06] border border-blue-400/40',
          )}
          aria-label={`${trace.upstreamCount} upstream nodes`}
        >
          <ArrowUp className="w-4 h-4 text-blue-600 dark:text-blue-400" strokeWidth={2.4} aria-hidden />
          <span className="text-sm font-bold tabular-nums text-ink">{upDisplay.toLocaleString()}</span>
        </span>
        <span
          className={cn(
            'inline-flex items-center gap-1.5 px-2.5 h-9 rounded-xl',
            'bg-white/[0.06] border border-emerald-400/40',
          )}
          aria-label={`${trace.downstreamCount} downstream nodes`}
        >
          <ArrowDown className="w-4 h-4 text-emerald-600 dark:text-emerald-400" strokeWidth={2.4} aria-hidden />
          <span className="text-sm font-bold tabular-nums text-ink">{downDisplay.toLocaleString()}</span>
        </span>
      </div>

      <span
        className="w-px h-8 bg-gradient-to-b from-transparent via-white/15 to-transparent shrink-0"
        aria-hidden
      />

      {/* Direction radiogroup with sliding underline */}
      <LayoutGroup id="trace-dock-direction">
        <div
          role="radiogroup"
          aria-label="Trace direction visibility"
          className={cn(
            'inline-flex items-center rounded-xl p-1 gap-0.5 shrink-0 h-9',
            'bg-white/[0.06] border border-white/[0.12]',
          )}
        >
          <DirRadio
            checked={direction === 'up'}
            onSelect={() => setDirection('up')}
            icon={<ArrowUp className="w-4 h-4" strokeWidth={2.4} />}
            label="Upstream only"
          />
          <DirRadio
            checked={direction === 'both'}
            onSelect={() => setDirection('both')}
            icon={<ArrowUpDown className="w-4 h-4" strokeWidth={2.4} />}
            label="Both directions"
          />
          <DirRadio
            checked={direction === 'down'}
            onSelect={() => setDirection('down')}
            icon={<ArrowDown className="w-4 h-4" strokeWidth={2.4} />}
            label="Downstream only"
          />
        </div>
      </LayoutGroup>

      <div className="flex-1 min-w-2" />

      {/* Recent popover trigger */}
      {recentCount > 0 && (
        <div className="relative shrink-0">
          <button
            ref={recentTriggerRef}
            type="button"
            data-trace-control
            aria-haspopup="menu"
            aria-expanded={recentOpen}
            aria-label={`Recent trace history, ${recentCount} ${recentCount === 1 ? 'entry' : 'entries'}`}
            onClick={() => setRecentOpen(v => !v)}
            className={cn(
              'inline-flex items-center gap-2 px-3.5 h-9 rounded-xl text-sm font-semibold',
              'transition-all duration-200',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
              recentOpen
                ? 'bg-accent-lineage text-white border border-accent-lineage shadow-lg shadow-accent-lineage/30'
                : 'bg-white/[0.08] border border-white/[0.15] text-ink hover:bg-white/[0.14] hover:border-white/[0.25]',
            )}
          >
            <Clock className="w-4 h-4" strokeWidth={2.4} />
            <span className="hidden md:inline tracking-tight">Recent</span>
            <span
              className={cn(
                'inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full',
                'text-[10px] font-bold tabular-nums leading-none',
                recentOpen
                  ? 'bg-white/25 text-white'
                  : 'bg-white/[0.15] text-ink',
              )}
            >
              {recentCount}
            </span>
          </button>
          {recentOpen && (
            <TraceRecentPopover
              history={trace.traceHistory}
              displayMap={displayMap}
              activeFocusId={trace.focusId}
              onJump={trace.jumpToHistoryEntry}
              onClear={trace.clearTraceHistory}
              onClose={() => { setRecentOpen(false); recentTriggerRef.current?.focus() }}
              triggerRef={recentTriggerRef}
            />
          )}
        </div>
      )}

      {/* Expand / Compact toggle */}
      <button
        type="button"
        data-trace-control
        aria-expanded={expanded}
        aria-controls="trace-bottom-dock-body"
        aria-keyshortcuts="Control+I Meta+I"
        onClick={onToggleExpanded}
        className={cn(
          'inline-flex items-center gap-2 px-3.5 h-9 rounded-xl text-sm font-semibold shrink-0',
          'transition-all duration-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
          expanded
            ? 'bg-accent-lineage text-white border border-accent-lineage shadow-lg shadow-accent-lineage/30'
            : 'bg-white/[0.08] border border-white/[0.15] text-ink hover:bg-white/[0.14] hover:border-white/[0.25]',
        )}
      >
        <span className="hidden md:inline tracking-tight">{expanded ? 'Compact' : 'Expand'}</span>
        {expanded
          ? <ChevronDown className="w-4 h-4" strokeWidth={2.4} />
          : <ChevronUp className="w-4 h-4" strokeWidth={2.4} />}
      </button>

      {/* Exit */}
      <button
        type="button"
        data-trace-control
        title="Exit trace (ESC)"
        aria-label="Exit trace"
        onClick={onExit}
        className={cn(
          'inline-flex items-center justify-center w-9 h-9 rounded-xl shrink-0',
          'bg-white/[0.08] border border-white/[0.15] text-ink',
          'hover:bg-rose-500 hover:text-white hover:border-rose-500 hover:shadow-lg hover:shadow-rose-500/30',
          'transition-all duration-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/40',
        )}
      >
        <X className="w-4 h-4" strokeWidth={2.4} />
      </button>
    </div>
  )
}

interface DirRadioProps {
  checked: boolean
  onSelect: () => void
  icon: React.ReactNode
  label: string
}

function DirRadio({ checked, onSelect, icon, label }: DirRadioProps) {
  return (
    <button
      type="button"
      data-trace-control
      role="radio"
      aria-checked={checked}
      aria-label={label}
      title={label}
      onClick={onSelect}
      className={cn(
        'relative inline-flex items-center justify-center w-10 h-full rounded-lg transition-colors duration-150',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
        checked
          ? 'text-white'
          : 'text-ink-muted hover:text-ink',
      )}
    >
      {checked && (
        <motion.span
          layoutId="trace-dock-direction-active"
          transition={{ type: 'spring', stiffness: 500, damping: 38 }}
          className="absolute inset-0 rounded-lg bg-accent-lineage shadow-sm shadow-accent-lineage/40"
        />
      )}
      <span className="relative">{icon}</span>
    </button>
  )
}
