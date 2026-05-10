import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Filter, Check, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface TraceDockEdgeFilterProps {
  availableEdgeTypes: string[]
  effectiveSet: Set<string>
  resolveEdgeColor: (edgeType: string) => string
  onToggle: (edgeType: string) => void
  onSelectAll?: () => void
}

/**
 * Edge-type filter. Inline gradient chips for ≤3 types; collapses into
 * a "Edges (n / m)" gradient pill trigger for ≥4 types that opens a
 * popover with the full chip set. All chrome matches the app's gradient
 * pill vocabulary.
 */
export function TraceDockEdgeFilter({
  availableEdgeTypes,
  effectiveSet,
  resolveEdgeColor,
  onToggle,
  onSelectAll,
}: TraceDockEdgeFilterProps) {
  if (availableEdgeTypes.length === 0) {
    return (
      <div
        className={cn(
          'inline-flex items-center gap-1.5 px-3 h-7 rounded-xl',
          'bg-white/[0.03] border border-white/[0.06]',
          'text-xs text-ink-muted/60',
        )}
      >
        <Filter className="w-3.5 h-3.5" />
        <span>No edge types</span>
      </div>
    )
  }

  if (availableEdgeTypes.length <= 3) {
    return (
      <div className="flex items-center gap-1.5 flex-wrap">
        {availableEdgeTypes.map(edgeType => (
          <EdgeChip
            key={edgeType}
            edgeType={edgeType}
            color={resolveEdgeColor(edgeType)}
            visible={effectiveSet.has(edgeType)}
            onToggle={() => onToggle(edgeType)}
          />
        ))}
      </div>
    )
  }

  return (
    <PopoverFilter
      availableEdgeTypes={availableEdgeTypes}
      effectiveSet={effectiveSet}
      resolveEdgeColor={resolveEdgeColor}
      onToggle={onToggle}
      onSelectAll={onSelectAll}
    />
  )
}

function PopoverFilter({
  availableEdgeTypes,
  effectiveSet,
  resolveEdgeColor,
  onToggle,
  onSelectAll,
}: TraceDockEdgeFilterProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const visibleCount = availableEdgeTypes.filter(t => effectiveSet.has(t)).length
  const total = availableEdgeTypes.length
  const allOn = visibleCount === total
  const filtered = !allOn

  return (
    <div className="relative" ref={wrapRef}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Edge filter: ${visibleCount} of ${total} types visible`}
        className={cn(
          'inline-flex items-center gap-2 px-3 h-8 rounded-xl',
          'text-xs font-semibold tracking-tight',
          'transition-all duration-200',
          'border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
          open
            ? 'bg-accent-lineage border-accent-lineage text-white shadow-lg shadow-accent-lineage/30'
            : filtered
              ? 'bg-white/[0.08] border-accent-lineage/50 text-ink hover:bg-white/[0.14] hover:border-accent-lineage'
              : 'bg-white/[0.08] border-white/[0.15] text-ink hover:bg-white/[0.14] hover:border-white/[0.25]',
        )}
      >
        <Filter className="w-4 h-4" strokeWidth={2.2} />
        <span>Edges</span>
        <span className="tabular-nums font-bold">
          {visibleCount}<span className="opacity-50">/{total}</span>
        </span>
        <ChevronDown
          className={cn('w-3.5 h-3.5 transition-transform duration-200', open && 'rotate-180')}
          strokeWidth={2.4}
        />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: 4, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 4, scale: 0.98 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            role="dialog"
            aria-label="Edge type filter"
            className={cn(
              'absolute bottom-full mb-2 right-0 z-50 min-w-[280px] max-w-[400px]',
              'rounded-xl bg-canvas-elevated/98 backdrop-blur-2xl',
              'border border-accent-lineage/25 shadow-glass-lg',
              'overflow-hidden',
            )}
          >
            <div className="px-3 py-2 flex items-center justify-between border-b border-white/[0.06] bg-gradient-to-r from-accent-lineage/[0.04] to-transparent">
              <span className="text-[10px] uppercase tracking-[0.14em] font-bold text-ink-muted">
                Edge types
              </span>
              <button
                type="button"
                disabled={allOn}
                onClick={() => onSelectAll?.()}
                className={cn(
                  'inline-flex items-center gap-1 px-2 h-6 rounded-md text-[10px] font-bold uppercase tracking-wider transition-colors',
                  allOn
                    ? 'text-ink-muted/30 cursor-not-allowed'
                    : 'text-accent-lineage hover:bg-accent-lineage/15',
                )}
              >
                Show all
              </button>
            </div>
            <div className="max-h-[280px] overflow-y-auto custom-scrollbar p-2 grid grid-cols-1 gap-1">
              {availableEdgeTypes.map(edgeType => {
                const visible = effectiveSet.has(edgeType)
                const color = resolveEdgeColor(edgeType)
                return (
                  <button
                    key={edgeType}
                    type="button"
                    onClick={() => onToggle(edgeType)}
                    className={cn(
                      'group flex items-center gap-2.5 px-2.5 h-9 rounded-lg',
                      'text-xs transition-all duration-150',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
                      visible
                        ? 'bg-white/[0.04] text-ink hover:bg-white/[0.08]'
                        : 'text-ink-muted/60 hover:bg-white/[0.03] hover:text-ink-muted',
                    )}
                    aria-pressed={visible}
                  >
                    <span
                      className={cn(
                        'shrink-0 inline-flex items-center justify-center w-4 h-4 rounded border transition-all duration-150',
                        visible
                          ? 'border-transparent shadow-sm'
                          : 'border-white/[0.15] group-hover:border-white/[0.25]',
                      )}
                      style={visible ? { backgroundColor: color, boxShadow: `0 0 0 1px ${color}40` } : undefined}
                      aria-hidden
                    >
                      {visible && <Check className="w-3 h-3 text-canvas" strokeWidth={3.5} />}
                    </span>
                    <span
                      className="w-2 h-2 rounded-full shrink-0 transition-opacity"
                      style={{ backgroundColor: color, opacity: visible ? 1 : 0.4 }}
                      aria-hidden
                    />
                    <span className="flex-1 truncate uppercase tracking-wide font-semibold text-[11px]">
                      {edgeType}
                    </span>
                  </button>
                )
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

interface EdgeChipProps {
  edgeType: string
  color: string
  visible: boolean
  onToggle: () => void
}

function EdgeChip({ edgeType, color, visible, onToggle }: EdgeChipProps) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={visible}
      className={cn(
        'inline-flex items-center gap-1.5 px-2.5 h-7 rounded-xl',
        'text-[11px] font-bold uppercase tracking-wide',
        'border transition-all duration-200',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
        visible
          ? 'bg-white/[0.10] border-white/[0.20] text-ink hover:bg-white/[0.16] hover:border-white/[0.30]'
          : 'bg-white/[0.02] border-white/[0.08] text-ink-muted hover:text-ink hover:bg-white/[0.06] hover:border-white/[0.15]',
      )}
    >
      <span
        className="w-2 h-2 rounded-full transition-opacity"
        style={{
          backgroundColor: color,
          opacity: visible ? 1 : 0.35,
          boxShadow: visible ? `0 0 6px ${color}80` : 'none',
        }}
        aria-hidden
      />
      <span>{edgeType}</span>
    </button>
  )
}
