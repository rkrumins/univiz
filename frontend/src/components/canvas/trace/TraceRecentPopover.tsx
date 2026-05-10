import { useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { Clock } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceHistoryEntry } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { useTraceEscStack } from './useTraceEscStack'

export interface TraceRecentPopoverProps {
  history: TraceHistoryEntry[]
  displayMap: Map<string, HierarchyNode>
  activeFocusId: string | null
  onJump: (entry: TraceHistoryEntry) => void
  onClear: () => void
  onClose: () => void
  /** The trigger button — focus returns here on close. */
  triggerRef: React.RefObject<HTMLElement | null>
}

function relativeTime(ts: number): string {
  const seconds = Math.floor((Date.now() - ts) / 1000)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ago`
}

export function TraceRecentPopover({
  history,
  displayMap,
  activeFocusId,
  onJump,
  onClear,
  onClose,
  triggerRef,
}: TraceRecentPopoverProps) {
  const popoverRef = useRef<HTMLDivElement>(null)
  const firstItemRef = useRef<HTMLButtonElement>(null)

  // ESC closes; consume before the trace's own ESC handler.
  useTraceEscStack(true, onClose, 100)

  // Outside-click closes — but ignore clicks on the trigger so re-clicking
  // the trigger toggles cleanly without a flash of close+open.
  useEffect(() => {
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node | null
      if (!target) return
      if (popoverRef.current?.contains(target)) return
      if (triggerRef.current?.contains(target)) return
      onClose()
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [onClose, triggerRef])

  // Focus the first menuitem on open. On close, ContextViewHeader (the
  // parent) is responsible for restoring focus to the trigger.
  useEffect(() => {
    firstItemRef.current?.focus()
  }, [])

  // Roving focus inside the menu via Up/Down arrow keys.
  const onKeyDown = (e: React.KeyboardEvent) => {
    const items = Array.from(
      popoverRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [],
    )
    if (items.length === 0) return
    const current = items.indexOf(document.activeElement as HTMLElement)
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      const next = items[(current + 1 + items.length) % items.length]
      next.focus()
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      const prev = items[(current - 1 + items.length) % items.length]
      prev.focus()
    } else if (e.key === 'Home') {
      e.preventDefault()
      items[0].focus()
    } else if (e.key === 'End') {
      e.preventDefault()
      items[items.length - 1].focus()
    }
  }

  return (
    <motion.div
      ref={popoverRef}
      data-canvas-interactive
      role="menu"
      aria-label="Recent trace focuses"
      initial={{ opacity: 0, y: -4, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, scale: 0.98 }}
      transition={{ duration: 0.15, ease: 'easeOut' }}
      onKeyDown={onKeyDown}
      className={cn(
        'absolute right-0 top-full mt-1 z-[60]',
        'min-w-[260px] max-w-[320px] py-1 rounded-xl',
        'bg-canvas-elevated/98 backdrop-blur-2xl',
        'border border-glass-border shadow-glass-lg',
      )}
    >
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-glass-border/40">
        <span className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          <Clock className="w-3 h-3" /> Recent traces
        </span>
        <button
          type="button"
          onClick={() => { onClear(); onClose() }}
          className="text-[10px] text-ink-muted hover:text-ink transition-colors"
        >
          Clear
        </button>
      </div>
      <div className="py-1">
        {history.map((entry, idx) => {
          const node = displayMap.get(entry.focusId) ?? displayMap.get(entry.focusUrn)
          const name = node?.name ?? entry.focusUrn
          const typeId = node?.typeId
          const isActive = entry.focusId === activeFocusId
          return (
            <button
              key={`${entry.focusId}-${entry.timestamp}`}
              ref={idx === 0 ? firstItemRef : undefined}
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={isActive}
              onClick={() => { if (!isActive) { onJump(entry); onClose() } }}
              className={cn(
                'flex items-center justify-between gap-3 w-full px-3 py-1.5 text-left',
                'transition-colors duration-100',
                isActive
                  ? 'bg-accent-lineage/10 text-accent-lineage cursor-default'
                  : 'text-ink hover:bg-white/[0.04] focus-visible:bg-white/[0.06] focus-visible:outline-none',
              )}
            >
              <div className="flex flex-col min-w-0">
                <span className="text-[12px] font-medium truncate" title={name}>{name}</span>
                {typeId && (
                  <span className="text-[10px] text-ink-muted uppercase tracking-wide">{typeId}</span>
                )}
              </div>
              <span className="text-[10px] text-ink-muted/70 shrink-0 tabular-nums">
                {isActive ? 'active' : relativeTime(entry.timestamp)}
              </span>
            </button>
          )
        })}
      </div>
    </motion.div>
  )
}
