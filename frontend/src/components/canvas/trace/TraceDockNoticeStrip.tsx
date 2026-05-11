import { AlertTriangle, Info, ArrowRight } from 'lucide-react'
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
 * Inline notice — only renders when something is worth saying. Premium
 * gradient pill-card with a glowing icon container and a gradient action
 * button. Mirrors the ContextViewHeader's pending-changes treatment.
 */
export function TraceDockNoticeStrip({
  result,
  displayMap,
  onReduceDepth,
  onJumpToUrn,
}: TraceDockNoticeStripProps) {
  if (!result) return null

  if (result.isInherited && result.inheritedFromUrn) {
    const ancestor = displayMap.get(result.inheritedFromUrn)
    const ancestorName = ancestor?.name ?? result.inheritedFromUrn
    return (
      <Strip
        tone="info"
        icon={<Info className="w-4 h-4" strokeWidth={2.2} />}
        title="Lineage inherited"
        message={
          <>
            From <span className="font-semibold text-ink">{ancestorName}</span> — this entity has no direct edges of its own.
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
        icon={<AlertTriangle className="w-4 h-4" strokeWidth={2.2} />}
        title="Trace truncated"
        message={
          <>
            Showing top <span className="font-semibold text-ink tabular-nums">{total.toLocaleString()}</span> nodes — {reason}. Try narrowing depth or filtering edge types.
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
  title: string
  message: React.ReactNode
  actionLabel: string
  onAction: () => void
}

const TONE = {
  info: {
    bg: 'bg-white/[0.04]',
    border: 'border-accent-lineage/40',
    iconBg: 'bg-accent-lineage',
    iconBorder: 'border-accent-lineage',
    iconText: 'text-white',
    titleText: 'text-accent-lineage',
    glow: 'shadow-accent-lineage/15',
    btnBg: 'bg-accent-lineage',
    btnBorder: 'border-accent-lineage',
    btnText: 'text-white',
    btnHover: 'hover:bg-purple-500 hover:border-purple-500 hover:shadow-lg hover:shadow-accent-lineage/30',
    ring: 'focus-visible:ring-accent-lineage/50',
  },
  warn: {
    bg: 'bg-white/[0.04]',
    border: 'border-amber-400/40',
    iconBg: 'bg-amber-500',
    iconBorder: 'border-amber-500',
    iconText: 'text-white',
    titleText: 'text-amber-600 dark:text-amber-300',
    glow: 'shadow-amber-500/15',
    btnBg: 'bg-amber-500',
    btnBorder: 'border-amber-500',
    btnText: 'text-white',
    btnHover: 'hover:bg-amber-600 hover:border-amber-600 hover:shadow-lg hover:shadow-amber-500/30',
    ring: 'focus-visible:ring-amber-500/50',
  },
} as const

function Strip({ tone, icon, title, message, actionLabel, onAction }: StripProps) {
  const palette = TONE[tone]
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'mx-5 mt-4 mb-1 flex items-start gap-3 px-3 py-2.5 rounded-xl',
        palette.bg,
        'border',
        palette.border,
        'shadow-md',
        palette.glow,
      )}
    >
      <div
        className={cn(
          'shrink-0 w-8 h-8 rounded-lg flex items-center justify-center',
          palette.iconBg,
          'border',
          palette.iconBorder,
        )}
      >
        <span className={palette.iconText}>{icon}</span>
      </div>

      <div className="flex-1 min-w-0 flex flex-col gap-0.5 pt-0.5">
        <span
          className={cn(
            'text-[10px] uppercase tracking-[0.14em] font-bold',
            palette.titleText,
          )}
        >
          {title}
        </span>
        <span className="text-xs text-ink leading-relaxed">{message}</span>
      </div>

      <button
        type="button"
        onClick={onAction}
        className={cn(
          'shrink-0 inline-flex items-center gap-1.5 px-3 h-8 rounded-xl',
          'text-xs font-bold tracking-tight',
          palette.btnBg,
          'border',
          palette.btnBorder,
          palette.btnText,
          'transition-all duration-200',
          palette.btnHover,
          'hover:scale-[1.02] active:scale-[0.98]',
          'focus-visible:outline-none focus-visible:ring-2',
          palette.ring,
        )}
      >
        {actionLabel}
        <ArrowRight className="w-3.5 h-3.5" strokeWidth={2.4} />
      </button>
    </div>
  )
}
