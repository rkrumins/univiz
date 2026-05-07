import { motion, AnimatePresence } from 'framer-motion'
import { History, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceHistoryEntry } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'

export interface TraceHistoryStripProps {
  history: TraceHistoryEntry[]
  /** Resolves an entry's `focusId` (or URN) to display info. */
  displayMap: Map<string, HierarchyNode>
  /** Currently-active focus ID — that entry is rendered as the head and is non-clickable. */
  activeFocusId: string | null
  onJump: (entry: TraceHistoryEntry) => void
  onClear: () => void
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

export function TraceHistoryStrip({
  history,
  displayMap,
  activeFocusId,
  onJump,
  onClear,
}: TraceHistoryStripProps) {
  // Hide the strip until there's something useful to navigate to —
  // a single entry would just mirror the pill's focus.
  if (history.length < 2) return null

  return (
    <motion.div
      data-canvas-interactive
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: 0.2, ease: 'easeOut' }}
      className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-canvas-elevated/85 backdrop-blur-md border border-glass-border/60 text-[11px]"
    >
      <span className="inline-flex items-center gap-1 text-ink-muted font-medium">
        <History className="w-3 h-3" /> Recent
      </span>

      <div className="flex items-center gap-1">
        <AnimatePresence initial={false}>
          {history.map(entry => {
            const node = displayMap.get(entry.focusId) ?? displayMap.get(entry.focusUrn)
            const name = node?.name ?? entry.focusUrn
            const isActive = entry.focusId === activeFocusId
            return (
              <motion.button
                key={`${entry.focusId}-${entry.timestamp}`}
                type="button"
                onClick={() => !isActive && onJump(entry)}
                disabled={isActive}
                title={`${name} · ${relativeTime(entry.timestamp)}`}
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={{ duration: 0.15 }}
                className={cn(
                  'inline-flex items-center gap-1 px-2 py-0.5 rounded-md max-w-[140px] truncate',
                  'transition-colors duration-150',
                  isActive
                    ? 'bg-accent-lineage/15 text-accent-lineage cursor-default'
                    : 'bg-white/5 text-ink-muted hover:text-ink hover:bg-white/10',
                )}
              >
                <span className="truncate">{name}</span>
              </motion.button>
            )
          })}
        </AnimatePresence>
      </div>

      <button
        type="button"
        onClick={onClear}
        title="Clear history"
        className="inline-flex items-center justify-center w-5 h-5 rounded-md text-ink-muted/60 hover:text-ink hover:bg-white/10 transition-colors"
      >
        <X className="w-3 h-3" />
      </button>
    </motion.div>
  )
}
