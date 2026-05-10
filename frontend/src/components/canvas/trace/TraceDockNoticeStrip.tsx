import { AlertTriangle, Info } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'

export interface TraceDockNoticeStripProps {
  result: TraceResult | null
  displayMap: Map<string, HierarchyNode>
  onReduceDepth: () => void
  onJumpToUrn: (urn: string) => void
}

/**
 * Inline notice strip — appears at the top of the dock body when the trace
 * result is truncated or inherited. Single dense line: icon + message + action.
 */
export function TraceDockNoticeStrip({
  result,
  displayMap,
  onReduceDepth,
  onJumpToUrn,
}: TraceDockNoticeStripProps) {
  if (!result) return null

  // Inheritance takes priority — explains *why* the trace shows ancestor lineage.
  if (result.isInherited && result.inheritedFromUrn) {
    const ancestor = displayMap.get(result.inheritedFromUrn)
    const ancestorName = ancestor?.name ?? result.inheritedFromUrn
    return (
      <Strip
        tone="info"
        icon={<Info className="w-3.5 h-3.5" />}
        message={
          <>
            Lineage inherited from <span className="font-semibold text-ink">{ancestorName}</span> — this entity has no direct edges.
          </>
        }
        actionLabel="View parent"
        onAction={() => onJumpToUrn(result.inheritedFromUrn!)}
      />
    )
  }

  if (result.truncated) {
    const total = result.traceNodes.size
    const reason = result.truncationReason === 'timeout'
      ? 'the trace timed out'
      : 'the result hit the size limit'
    return (
      <Strip
        tone="warn"
        icon={<AlertTriangle className="w-3.5 h-3.5" />}
        message={
          <>
            Showing top <span className="font-semibold text-ink tabular-nums">{total.toLocaleString()}</span> — {reason}. Try narrowing depth or filtering edge types.
          </>
        }
        actionLabel="Reduce depth"
        onAction={onReduceDepth}
      />
    )
  }

  return null
}

interface StripProps {
  tone: 'info' | 'warn'
  icon: React.ReactNode
  message: React.ReactNode
  actionLabel: string
  onAction: () => void
}

function Strip({ tone, icon, message, actionLabel, onAction }: StripProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex items-center gap-2 px-4 py-1.5 text-[11px] border-b',
        tone === 'info'
          ? 'bg-accent-lineage/8 border-accent-lineage/20'
          : 'bg-amber-500/8 border-amber-500/20',
      )}
    >
      <span className={cn(
        'shrink-0',
        tone === 'info' ? 'text-accent-lineage' : 'text-amber-600 dark:text-amber-400',
      )}>{icon}</span>
      <span className="text-ink-muted flex-1 truncate">{message}</span>
      <button
        type="button"
        onClick={onAction}
        className={cn(
          'shrink-0 ml-1 px-2 py-0.5 rounded-md text-[10px] font-semibold uppercase tracking-wide transition-colors',
          tone === 'info'
            ? 'bg-accent-lineage/15 hover:bg-accent-lineage/25 text-accent-lineage focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/50'
            : 'bg-amber-500/15 hover:bg-amber-500/25 text-amber-700 dark:text-amber-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50',
        )}
      >
        {actionLabel}
      </button>
    </div>
  )
}
