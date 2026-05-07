import React from 'react'
import { Eye, EyeOff, ArrowUp, ArrowDown } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface TraceFiltersTabProps {
  /** All ontology lineage edge types available for filtering. */
  availableEdgeTypes: string[]
  /** Current whitelist; empty = "all types included". */
  selectedEdgeTypes: string[]
  onChangeSelectedEdgeTypes: (next: string[]) => void
  resolveEdgeColor: (edgeType: string) => string
  showUpstream: boolean
  showDownstream: boolean
  onSetShowUpstream: (show: boolean) => void
  onSetShowDownstream: (show: boolean) => void
  /** Apply the new filter (caller decides whether to debounce / retrace). */
  onApply: () => void
}

export function TraceFiltersTab({
  availableEdgeTypes,
  selectedEdgeTypes,
  onChangeSelectedEdgeTypes,
  resolveEdgeColor,
  showUpstream,
  showDownstream,
  onSetShowUpstream,
  onSetShowDownstream,
  onApply,
}: TraceFiltersTabProps) {
  // Empty whitelist semantics: backend interprets `[]` as "all". Toggling a
  // type in/out of the visible set therefore needs a small dance: when the
  // user explicitly enables one or more types, we send only those; when the
  // user re-enables every type, we clear the array back to "all".
  const isAllSelected = selectedEdgeTypes.length === 0 ||
    selectedEdgeTypes.length === availableEdgeTypes.length
  const effectiveSet = new Set(
    isAllSelected ? availableEdgeTypes : selectedEdgeTypes,
  )

  const toggleEdgeType = (edgeType: string) => {
    const next = new Set(effectiveSet)
    if (next.has(edgeType)) next.delete(edgeType)
    else next.add(edgeType)
    // If the user just re-selected every type, store as `[]` (== all).
    if (next.size === availableEdgeTypes.length) {
      onChangeSelectedEdgeTypes([])
    } else {
      onChangeSelectedEdgeTypes(Array.from(next))
    }
    onApply()
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Direction visibility */}
      <div className="flex flex-col gap-1.5">
        <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          Direction
        </span>
        <div className="flex items-center gap-1.5">
          <DirectionToggle
            active={showUpstream}
            onClick={() => onSetShowUpstream(!showUpstream)}
            icon={<ArrowUp className="w-3.5 h-3.5" />}
            label="Upstream"
            color="blue"
          />
          <DirectionToggle
            active={showDownstream}
            onClick={() => onSetShowDownstream(!showDownstream)}
            icon={<ArrowDown className="w-3.5 h-3.5" />}
            label="Downstream"
            color="emerald"
          />
        </div>
      </div>

      {/* Edge types */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
            Edge types
          </span>
          {!isAllSelected && (
            <button
              type="button"
              onClick={() => { onChangeSelectedEdgeTypes([]); onApply() }}
              className="text-[10px] text-accent-lineage hover:underline font-medium"
            >
              Reset to all
            </button>
          )}
        </div>
        {availableEdgeTypes.length === 0 ? (
          <p className="text-xs text-ink-muted">
            No lineage edge types defined in the active ontology.
          </p>
        ) : (
          <div className="flex flex-col gap-1">
            {availableEdgeTypes.map(edgeType => {
              const visible = effectiveSet.has(edgeType)
              const color = resolveEdgeColor(edgeType)
              return (
                <button
                  key={edgeType}
                  type="button"
                  onClick={() => toggleEdgeType(edgeType)}
                  className={cn(
                    'flex items-center gap-2 px-2.5 py-1.5 rounded-lg w-full text-left',
                    'transition-colors duration-150',
                    visible
                      ? 'bg-white/[0.04] hover:bg-white/[0.08]'
                      : 'bg-transparent opacity-50 hover:opacity-80 hover:bg-white/[0.03]',
                  )}
                >
                  <span
                    className="w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: color }}
                    aria-hidden
                  />
                  <span className="text-[12px] text-ink flex-1 truncate">
                    {edgeType}
                  </span>
                  {visible
                    ? <Eye className="w-3.5 h-3.5 text-ink-muted" />
                    : <EyeOff className="w-3.5 h-3.5 text-ink-muted/60" />}
                </button>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

interface DirectionToggleProps {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
  color: 'blue' | 'emerald'
}

function DirectionToggle({ active, onClick, icon, label, color }: DirectionToggleProps) {
  const activeStyle = color === 'blue'
    ? 'bg-blue-500/15 text-blue-600 dark:text-blue-400 ring-1 ring-blue-500/30'
    : 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 ring-1 ring-emerald-500/30'
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-medium',
        'transition-colors duration-150',
        active ? activeStyle : 'text-ink-muted hover:text-ink hover:bg-white/5',
      )}
    >
      {icon} {label}
    </button>
  )
}
