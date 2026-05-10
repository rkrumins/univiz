import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, LayoutGroup } from 'framer-motion'
import {
  ArrowUp,
  ArrowDown,
  ArrowUpDown,
  ChevronDown,
  ChevronUp,
  Clock,
  X,
  AlertTriangle,
  Info,
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
 * The always-visible 52px title row that lives at the top of TraceBottomDock.
 * In compact mode this IS the dock; in expanded mode it sits above the
 * content sections. Sticky so it stays visible during internal scroll.
 *
 * A11y model:
 *  - `role="toolbar"` with roving tabindex (single Tab stop, arrow keys rove)
 *  - Direction segmented control is `role="radiogroup"` (one Tab stop)
 *  - Pulsing dot is decorative, gated by `prefers-reduced-motion`
 *  - Live region announces trace start (consolidated single message)
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

  const hasNotice = trace.result?.truncated || (trace.result?.isInherited && trace.result?.inheritedFromUrn)
  const noticeKind: 'warn' | 'info' | null = trace.result?.truncated
    ? 'warn'
    : trace.result?.isInherited && trace.result?.inheritedFromUrn ? 'info' : null

  // Roving tabindex across all interactive controls.
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

  const handleNoticeClick = useCallback(() => {
    if (!expanded) onToggleExpanded()
  }, [expanded, onToggleExpanded])

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
        'flex items-center gap-2.5 px-4 h-[52px] shrink-0',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40 focus-visible:ring-inset',
        'text-xs',
      )}
    >
      {/* Live-region for screen readers */}
      <span className="sr-only" aria-live="polite" aria-atomic="true">{liveMsg}</span>

      {/* Pulsing dot */}
      <span className="relative inline-flex items-center justify-center w-2.5 h-2.5 shrink-0" aria-hidden="true">
        <span className="absolute inset-0 rounded-full bg-accent-lineage opacity-60 animate-pulse motion-reduce:animate-none" />
        <span className="relative w-1.5 h-1.5 rounded-full bg-accent-lineage" />
      </span>

      <span className="font-semibold text-accent-lineage uppercase tracking-[0.12em] text-[10px] shrink-0">
        {trace.isLoading ? 'Tracing…' : 'Tracing'}
      </span>

      <span className="w-px h-4 bg-glass-border/60 shrink-0" aria-hidden />

      {/* Focus identity */}
      <div className="flex items-center gap-1.5 min-w-0 shrink">
        <span className="font-semibold text-ink truncate max-w-[180px]" title={focusName}>
          {focusName}
        </span>
        {focusType && (
          <span className="hidden xl:inline-flex px-1.5 py-0.5 rounded-md bg-accent-lineage/10 text-accent-lineage text-[10px] font-semibold uppercase tracking-wide shrink-0">
            {focusType}
          </span>
        )}
        {typeof trace.result?.effectiveLevel === 'number' && (
          <span className="hidden 2xl:inline-flex px-1.5 py-0.5 rounded-md bg-white/5 text-ink-muted text-[10px] font-semibold tabular-nums shrink-0">
            L{trace.result.effectiveLevel}
          </span>
        )}
      </div>

      <span className="w-px h-4 bg-glass-border/60 shrink-0" aria-hidden />

      {/* Counts — animated */}
      <div className="flex items-center gap-1.5 shrink-0">
        <span
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] font-semibold tabular-nums bg-blue-500/10 text-blue-600 dark:text-blue-400"
          aria-label={`${trace.upstreamCount} upstream nodes`}
        >
          <ArrowUp className="w-3 h-3" aria-hidden /> {upDisplay}
        </span>
        <span
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] font-semibold tabular-nums bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          aria-label={`${trace.downstreamCount} downstream nodes`}
        >
          <ArrowDown className="w-3 h-3" aria-hidden /> {downDisplay}
        </span>
      </div>

      <span className="w-px h-4 bg-glass-border/60 shrink-0" aria-hidden />

      {/* Direction radiogroup with sliding underline */}
      <LayoutGroup id="trace-dock-direction">
        <div
          role="radiogroup"
          aria-label="Trace direction visibility"
          className="inline-flex items-center rounded-lg bg-black/5 dark:bg-white/5 p-0.5 gap-0.5 shrink-0"
        >
          <DirRadio checked={direction === 'up'} onSelect={() => setDirection('up')} icon={<ArrowUp className="w-3 h-3" />} label="Upstream only" />
          <DirRadio checked={direction === 'both'} onSelect={() => setDirection('both')} icon={<ArrowUpDown className="w-3 h-3" />} label="Both directions" />
          <DirRadio checked={direction === 'down'} onSelect={() => setDirection('down')} icon={<ArrowDown className="w-3 h-3" />} label="Downstream only" />
        </div>
      </LayoutGroup>

      {/* Notice icon — only when notice is active */}
      {hasNotice && (
        <button
          type="button"
          data-trace-control
          onClick={handleNoticeClick}
          aria-label={
            noticeKind === 'warn'
              ? `Trace truncated. ${expanded ? 'See notice in dock.' : 'Click to expand dock and see notice.'}`
              : `Lineage inherited from parent. ${expanded ? 'See notice in dock.' : 'Click to expand dock and see notice.'}`
          }
          className={cn(
            'inline-flex items-center justify-center w-6 h-6 rounded-md shrink-0',
            'transition-colors duration-150',
            'focus-visible:outline-none focus-visible:ring-2',
            noticeKind === 'warn'
              ? 'bg-amber-500/15 text-amber-600 dark:text-amber-400 hover:bg-amber-500/25 focus-visible:ring-amber-500/40'
              : 'bg-accent-lineage/15 text-accent-lineage hover:bg-accent-lineage/25 focus-visible:ring-accent-lineage/40',
          )}
        >
          {noticeKind === 'warn' ? <AlertTriangle className="w-3.5 h-3.5" /> : <Info className="w-3.5 h-3.5" />}
        </button>
      )}

      {/* Spacer pushes the right cluster to the edge */}
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
              'inline-flex items-center gap-1.5 px-2 h-7 rounded-md text-[11px] font-medium',
              'transition-colors duration-150',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
              recentOpen
                ? 'bg-accent-lineage/15 text-accent-lineage'
                : 'text-ink-muted hover:text-ink hover:bg-white/5',
            )}
          >
            <Clock className="w-3 h-3" />
            <span className="hidden md:inline">Recent</span>
            <span className="px-1 py-px rounded-full bg-accent-lineage/15 text-accent-lineage text-[9px] font-bold tabular-nums leading-none">
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
          'inline-flex items-center gap-1.5 px-2 h-7 rounded-md text-[11px] font-medium shrink-0',
          'transition-colors duration-150',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
          expanded
            ? 'bg-accent-lineage/15 text-accent-lineage'
            : 'text-ink-muted hover:text-ink hover:bg-white/5',
        )}
      >
        <span className="hidden md:inline">{expanded ? 'Compact' : 'Expand'}</span>
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
      </button>

      {/* Exit */}
      <button
        type="button"
        data-trace-control
        title="Exit trace (ESC)"
        aria-label="Exit trace"
        onClick={onExit}
        className={cn(
          'inline-flex items-center gap-1.5 px-2 h-7 rounded-md text-[11px] font-medium shrink-0',
          'text-ink-muted hover:text-rose-500 hover:bg-rose-500/10',
          'transition-colors duration-150',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/40',
        )}
      >
        <span className="hidden md:inline">Exit</span>
        <X className="w-3 h-3" />
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
        'relative inline-flex items-center justify-center w-7 h-6 rounded-md transition-colors duration-150',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
        checked
          ? 'text-accent-lineage'
          : 'text-ink-muted hover:text-ink',
      )}
    >
      {checked && (
        <motion.span
          layoutId="trace-dock-direction-active"
          transition={{ type: 'spring', stiffness: 500, damping: 38 }}
          className="absolute inset-0 rounded-md bg-accent-lineage/20"
        />
      )}
      <span className="relative">{icon}</span>
    </button>
  )
}
