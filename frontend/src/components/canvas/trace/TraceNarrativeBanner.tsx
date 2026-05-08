import React from 'react'
import { motion } from 'framer-motion'
import { Info, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceResult } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'

export interface TraceNarrativeBannerProps {
  result: TraceResult | null
  displayMap: Map<string, HierarchyNode>
  /** Halve both depths and re-trace. */
  onReduceDepth: () => void
  /** Jump trace focus to an ancestor URN. */
  onJumpToUrn: (urn: string) => void
}

export function TraceNarrativeBanner({
  result,
  displayMap,
  onReduceDepth,
  onJumpToUrn,
}: TraceNarrativeBannerProps) {
  if (!result) return null

  // Inheritance takes priority — it explains *why* the trace shows ancestor
  // lineage rather than the focus's own.
  if (result.isInherited && result.inheritedFromUrn) {
    const ancestor = displayMap.get(result.inheritedFromUrn)
    const ancestorName = ancestor?.name ?? result.inheritedFromUrn
    return (
      <Banner
        tone="info"
        icon={<Info className="w-3.5 h-3.5" />}
        message={
          <>
            Lineage inherited from{' '}
            <span className="font-semibold text-ink">{ancestorName}</span> — this
            entity has no direct edges of its own.
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
      <Banner
        tone="warn"
        icon={<AlertTriangle className="w-3.5 h-3.5" />}
        message={
          <>
            Showing top {total.toLocaleString()} — {reason}. Try narrowing depth
            or filtering edge types.
          </>
        }
        actionLabel="Reduce depth"
        onAction={onReduceDepth}
      />
    )
  }

  return null
}

interface BannerProps {
  tone: 'info' | 'warn'
  icon: React.ReactNode
  message: React.ReactNode
  actionLabel: string
  onAction: () => void
}

function Banner({ tone, icon, message, actionLabel, onAction }: BannerProps) {
  return (
    <motion.div
      data-canvas-interactive
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: 0.2, ease: 'easeOut' }}
      className={cn(
        'flex items-center gap-2 px-3 py-1.5 rounded-full text-[11px] backdrop-blur-md',
        tone === 'info'
          ? 'bg-accent-lineage/10 border border-accent-lineage/30 text-accent-lineage'
          : 'bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-400',
      )}
    >
      {icon}
      <span className="text-ink-muted">{message}</span>
      <button
        type="button"
        onClick={onAction}
        className={cn(
          'ml-1 px-2 py-0.5 rounded-md text-[10px] font-semibold transition-colors',
          tone === 'info'
            ? 'bg-accent-lineage/15 hover:bg-accent-lineage/25 text-accent-lineage'
            : 'bg-amber-500/15 hover:bg-amber-500/25 text-amber-700 dark:text-amber-400',
        )}
      >
        {actionLabel}
      </button>
    </motion.div>
  )
}
