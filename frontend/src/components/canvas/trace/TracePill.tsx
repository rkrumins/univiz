import React from 'react'
import { motion } from 'framer-motion'
import { ChevronDown, ChevronUp, X, ArrowUp, ArrowDown, ArrowUpDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { MOTION } from '@/lib/motion'
import { useCountUp } from './useCountUp'

export interface TracePillProps {
  focusName: string
  focusType?: string
  effectiveLevel?: number
  upstreamCount: number
  downstreamCount: number
  showUpstream: boolean
  showDownstream: boolean
  onSetShowUpstream: (show: boolean) => void
  onSetShowDownstream: (show: boolean) => void
  detailsOpen: boolean
  onToggleDetails: () => void
  onExit: () => void
  isLoading?: boolean
}

type Direction = 'up' | 'both' | 'down'

function deriveDirection(showUpstream: boolean, showDownstream: boolean): Direction {
  if (showUpstream && !showDownstream) return 'up'
  if (!showUpstream && showDownstream) return 'down'
  return 'both'
}

export function TracePill({
  focusName,
  focusType,
  effectiveLevel,
  upstreamCount,
  downstreamCount,
  showUpstream,
  showDownstream,
  onSetShowUpstream,
  onSetShowDownstream,
  detailsOpen,
  onToggleDetails,
  onExit,
  isLoading = false,
}: TracePillProps) {
  const upDisplay = useCountUp(upstreamCount)
  const downDisplay = useCountUp(downstreamCount)
  const direction = deriveDirection(showUpstream, showDownstream)

  const setDirection = (dir: Direction) => {
    if (dir === 'up') {
      onSetShowUpstream(true)
      onSetShowDownstream(false)
    } else if (dir === 'down') {
      onSetShowUpstream(false)
      onSetShowDownstream(true)
    } else {
      onSetShowUpstream(true)
      onSetShowDownstream(true)
    }
  }

  return (
    <motion.div
      data-canvas-interactive
      initial={{ y: -16, opacity: 0, scale: 0.96 }}
      animate={{ y: 0, opacity: 1, scale: 1 }}
      exit={{ y: -16, opacity: 0, scale: 0.96 }}
      transition={MOTION.modalSpring}
      className={cn(
        'flex items-center gap-2.5 px-3 py-2 rounded-full',
        'bg-canvas-elevated/95 backdrop-blur-2xl',
        'border border-accent-lineage/30',
        'shadow-glass-lg shadow-accent-lineage/10',
        'text-xs',
      )}
      role="status"
      aria-live="polite"
    >
      {/* Pulsing dot — visual heartbeat */}
      <span className="relative inline-flex items-center justify-center w-2.5 h-2.5">
        <span
          className={cn(
            'absolute inset-0 rounded-full bg-accent-lineage opacity-60',
            isLoading ? 'animate-ping' : 'animate-pulse',
          )}
        />
        <span className="relative w-1.5 h-1.5 rounded-full bg-accent-lineage" />
      </span>

      <span className="font-semibold text-accent-lineage uppercase tracking-wider text-[10px]">
        {isLoading ? 'Tracing…' : 'Tracing'}
      </span>

      <span className="w-px h-4 bg-glass-border/60" aria-hidden />

      {/* Focus identity */}
      <div className="flex items-center gap-1.5 min-w-0">
        <span
          className="font-medium text-ink truncate max-w-[180px]"
          title={focusName}
        >
          {focusName}
        </span>
        {focusType && (
          <span className="px-1.5 py-0.5 rounded-md bg-accent-lineage/10 text-accent-lineage text-[10px] font-medium uppercase tracking-wide">
            {focusType}
          </span>
        )}
        {typeof effectiveLevel === 'number' && (
          <span className="px-1.5 py-0.5 rounded-md bg-white/5 text-ink-muted text-[10px] font-medium tabular-nums">
            L{effectiveLevel}
          </span>
        )}
      </div>

      <span className="w-px h-4 bg-glass-border/60" aria-hidden />

      {/* Animated counts */}
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            'inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md text-[11px] font-semibold tabular-nums',
            'bg-blue-500/10 text-blue-600 dark:text-blue-400',
          )}
          title={`${upstreamCount} upstream`}
        >
          <ArrowUp className="w-3 h-3" /> {upDisplay}
        </span>
        <span
          className={cn(
            'inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md text-[11px] font-semibold tabular-nums',
            'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
          )}
          title={`${downstreamCount} downstream`}
        >
          <ArrowDown className="w-3 h-3" /> {downDisplay}
        </span>
      </div>

      <span className="w-px h-4 bg-glass-border/60" aria-hidden />

      {/* Segmented direction control */}
      <div
        role="radiogroup"
        aria-label="Trace direction"
        className="inline-flex items-center rounded-lg bg-black/5 dark:bg-white/5 p-0.5 gap-0.5"
      >
        <DirButton
          active={direction === 'up'}
          onClick={() => setDirection('up')}
          icon={<ArrowUp className="w-3 h-3" />}
          label="Upstream only"
        />
        <DirButton
          active={direction === 'both'}
          onClick={() => setDirection('both')}
          icon={<ArrowUpDown className="w-3 h-3" />}
          label="Both directions"
        />
        <DirButton
          active={direction === 'down'}
          onClick={() => setDirection('down')}
          icon={<ArrowDown className="w-3 h-3" />}
          label="Downstream only"
        />
      </div>

      <span className="w-px h-4 bg-glass-border/60" aria-hidden />

      {/* Details toggle */}
      <button
        type="button"
        onClick={onToggleDetails}
        aria-expanded={detailsOpen}
        className={cn(
          'inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium',
          'transition-colors duration-150',
          detailsOpen
            ? 'bg-accent-lineage/15 text-accent-lineage'
            : 'text-ink-muted hover:text-ink hover:bg-white/5',
        )}
      >
        Details
        {detailsOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
      </button>

      {/* Exit */}
      <button
        type="button"
        onClick={onExit}
        title="Exit trace (ESC)"
        className={cn(
          'inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium',
          'text-ink-muted hover:text-ink hover:bg-rose-500/10 hover:text-rose-500',
          'transition-colors duration-150',
        )}
      >
        Exit <X className="w-3 h-3" />
      </button>
    </motion.div>
  )
}

interface DirButtonProps {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}

function DirButton({ active, onClick, icon, label }: DirButtonProps) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      aria-label={label}
      title={label}
      onClick={onClick}
      className={cn(
        'inline-flex items-center justify-center w-7 h-6 rounded-md transition-colors duration-150',
        active
          ? 'bg-accent-lineage/20 text-accent-lineage shadow-sm'
          : 'text-ink-muted hover:text-ink hover:bg-white/5',
      )}
    >
      {icon}
    </button>
  )
}
