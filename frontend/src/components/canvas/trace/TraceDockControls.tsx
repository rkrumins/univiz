import { ArrowUp, ArrowDown, Eye, EyeOff, Filter } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceConfig } from '@/hooks/useUnifiedTrace'

export interface GranularityOption {
  id: string
  name: string
  level: number
}

export interface TraceDockControlsProps {
  config: TraceConfig
  granularityOptions: GranularityOption[]
  availableEdgeTypes: string[]
  resolveEdgeColor: (edgeType: string) => string
  onChangeConfig: (patch: Partial<TraceConfig>) => void
  onApply: () => void
}

/**
 * Inline controls strip — dual depth sliders + level select + edge-type filter
 * chips, all on (ideally) one or two dense rows. No card chrome, no section
 * headers, just labeled controls inline.
 */
export function TraceDockControls({
  config,
  granularityOptions,
  availableEdgeTypes,
  resolveEdgeColor,
  onChangeConfig,
  onApply,
}: TraceDockControlsProps) {
  // Empty whitelist semantics: backend treats `[]` as "all types". The
  // visible "selected" set therefore mirrors `availableEdgeTypes` when the
  // whitelist is empty.
  const isAllSelected = config.lineageEdgeTypes.length === 0
  const effectiveSet = new Set(isAllSelected ? availableEdgeTypes : config.lineageEdgeTypes)

  const toggleEdgeType = (edgeType: string) => {
    const next = new Set(effectiveSet)
    if (next.has(edgeType)) next.delete(edgeType)
    else next.add(edgeType)
    onChangeConfig({
      lineageEdgeTypes: next.size === availableEdgeTypes.length ? [] : Array.from(next),
    })
    onApply()
  }

  return (
    <div className="px-4 py-2 flex items-center gap-4 flex-wrap border-b border-glass-border/30">
      {/* Reach upstream slider */}
      <DepthSlider
        label="Reach"
        icon={<ArrowUp className="w-3 h-3" />}
        accent="blue"
        value={config.upstreamDepth}
        onChange={v => onChangeConfig({ upstreamDepth: v })}
        onCommit={onApply}
      />

      {/* Reach downstream slider */}
      <DepthSlider
        label="Reach"
        icon={<ArrowDown className="w-3 h-3" />}
        accent="emerald"
        value={config.downstreamDepth}
        onChange={v => onChangeConfig({ downstreamDepth: v })}
        onCommit={onApply}
      />

      <span className="text-ink-muted/30 select-none">·</span>

      {/* Level select */}
      <label className="inline-flex items-center gap-1.5 text-[11px] text-ink-muted">
        <span className="text-[10px] uppercase tracking-wider font-semibold">Level</span>
        <select
          value={typeof config.level === 'string' || typeof config.level === 'number' ? String(config.level) : 'auto'}
          onChange={e => {
            const v = e.target.value
            const next: TraceConfig['level'] = v === 'auto' ? 'auto' : v
            onChangeConfig({ level: next })
            onApply()
          }}
          className="px-2 py-1 rounded-md text-[11px] bg-white/5 border border-glass-border/40 text-ink hover:bg-white/[0.08] focus:outline-none focus:ring-2 focus:ring-accent-lineage/40"
        >
          <option value="auto">Auto</option>
          {granularityOptions.map(opt => (
            <option key={opt.id} value={opt.id}>{opt.name} (L{opt.level})</option>
          ))}
        </select>
      </label>

      <span className="text-ink-muted/30 select-none">·</span>

      {/* Filter chips */}
      <div className="inline-flex items-center gap-1.5 flex-wrap">
        <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          <Filter className="w-3 h-3" /> Filter
        </span>
        {availableEdgeTypes.length === 0 ? (
          <span className="text-[11px] text-ink-muted/60">no edge types</span>
        ) : (
          availableEdgeTypes.map(edgeType => {
            const visible = effectiveSet.has(edgeType)
            const color = resolveEdgeColor(edgeType)
            return (
              <button
                key={edgeType}
                type="button"
                onClick={() => toggleEdgeType(edgeType)}
                className={cn(
                  'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium uppercase tracking-wide',
                  'transition-colors duration-150',
                  visible
                    ? 'bg-white/[0.06] text-ink hover:bg-white/[0.10]'
                    : 'bg-transparent text-ink-muted/50 hover:text-ink-muted hover:bg-white/[0.04]',
                )}
              >
                <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} aria-hidden />
                <span>{edgeType}</span>
                {visible
                  ? <Eye className="w-2.5 h-2.5 opacity-60" />
                  : <EyeOff className="w-2.5 h-2.5 opacity-60" />}
              </button>
            )
          })
        )}
      </div>
    </div>
  )
}

interface DepthSliderProps {
  label: string
  icon: React.ReactNode
  accent: 'blue' | 'emerald'
  value: number
  onChange: (v: number) => void
  onCommit: () => void
}

function DepthSlider({ label, icon, accent, value, onChange, onCommit }: DepthSliderProps) {
  const accentClass = accent === 'blue' ? 'accent-blue-500' : 'accent-emerald-500'
  const accentText = accent === 'blue' ? 'text-blue-600 dark:text-blue-400' : 'text-emerald-600 dark:text-emerald-400'
  return (
    <label className="inline-flex items-center gap-1.5 text-[11px]">
      <span className={cn('inline-flex items-center', accentText)}>{icon}</span>
      <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">{label}</span>
      <input
        type="range"
        min={0}
        max={50}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        onMouseUp={onCommit}
        onTouchEnd={onCommit}
        onKeyUp={onCommit}
        className={cn('w-24 h-1 rounded-full cursor-pointer', accentClass)}
      />
      <span className={cn('text-[11px] font-bold tabular-nums w-6 text-right', accentText)}>{value}</span>
    </label>
  )
}
