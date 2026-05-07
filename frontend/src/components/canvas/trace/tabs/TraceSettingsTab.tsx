import { cn } from '@/lib/utils'
import type { TraceConfig } from '@/hooks/useUnifiedTrace'

export interface GranularityOption {
  id: string
  name: string
  level: number
}

export interface TraceSettingsTabProps {
  config: TraceConfig
  granularityOptions: GranularityOption[]
  onChangeConfig: (patch: Partial<TraceConfig>) => void
  onApply: () => void
}

export function TraceSettingsTab({
  config,
  granularityOptions,
  onChangeConfig,
  onApply,
}: TraceSettingsTabProps) {
  return (
    <div className="flex flex-col gap-3">
      {/* Depth controls */}
      <div className="grid grid-cols-2 gap-3">
        <DepthSlider
          label="Reach upstream"
          value={config.upstreamDepth}
          onChange={v => onChangeConfig({ upstreamDepth: v })}
          onCommit={onApply}
          accent="blue"
        />
        <DepthSlider
          label="Reach downstream"
          value={config.downstreamDepth}
          onChange={v => onChangeConfig({ downstreamDepth: v })}
          onCommit={onApply}
          accent="emerald"
        />
      </div>

      {/* Level selector */}
      <div className="flex flex-col gap-1.5">
        <label className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          Trace level
        </label>
        <select
          value={typeof config.level === 'string' || typeof config.level === 'number' ? String(config.level) : 'auto'}
          onChange={e => {
            const v = e.target.value
            const next: TraceConfig['level'] = v === 'auto' ? 'auto' : v
            onChangeConfig({ level: next })
            onApply()
          }}
          className={cn(
            'px-2.5 py-1.5 rounded-lg text-xs',
            'bg-white/[0.04] border border-glass-border/40 text-ink',
            'focus:outline-none focus:ring-2 focus:ring-accent-lineage/40',
          )}
        >
          <option value="auto">Auto (focus level)</option>
          {granularityOptions.map(opt => (
            <option key={opt.id} value={opt.id}>
              {opt.name} (L{opt.level})
            </option>
          ))}
        </select>
      </div>

      {/* Advanced toggles */}
      <div className="flex flex-col gap-1">
        <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted mb-0.5">
          Advanced
        </span>
        <ToggleRow
          label="Path only"
          description="Hide context, show only direct lineage path"
          value={config.pathOnly}
          onChange={v => { onChangeConfig({ pathOnly: v }); onApply() }}
        />
        <ToggleRow
          label="Exclude containment edges"
          description="Pure data lineage — drop parent-child edges"
          value={config.excludeContainmentEdges}
          onChange={v => { onChangeConfig({ excludeContainmentEdges: v }); onApply() }}
        />
        <ToggleRow
          label="Inherit lineage from parent"
          description="When focus has no edges, use ancestor lineage"
          value={config.includeInheritedLineage}
          onChange={v => { onChangeConfig({ includeInheritedLineage: v }); onApply() }}
        />
        <ToggleRow
          label="Auto-expand ancestors"
          description="Open every container that hosts a traced descendant"
          value={config.autoExpandAncestors}
          onChange={v => onChangeConfig({ autoExpandAncestors: v })}
        />
        <ToggleRow
          label="Auto-sync to canvas"
          description="Merge traced nodes/edges into the canvas store"
          value={config.autoSyncToStore}
          onChange={v => onChangeConfig({ autoSyncToStore: v })}
        />
      </div>
    </div>
  )
}

interface DepthSliderProps {
  label: string
  value: number
  onChange: (v: number) => void
  onCommit: () => void
  accent: 'blue' | 'emerald'
}

function DepthSlider({ label, value, onChange, onCommit, accent }: DepthSliderProps) {
  const accentClass = accent === 'blue' ? 'accent-blue-500' : 'accent-emerald-500'
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
          {label}
        </span>
        <span className="text-xs font-semibold tabular-nums text-ink">
          {value}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={50}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        onMouseUp={onCommit}
        onTouchEnd={onCommit}
        onKeyUp={onCommit}
        className={cn('w-full h-1 rounded-full cursor-pointer', accentClass)}
      />
    </div>
  )
}

interface ToggleRowProps {
  label: string
  description: string
  value: boolean
  onChange: (v: boolean) => void
}

function ToggleRow({ label, description, value, onChange }: ToggleRowProps) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      className={cn(
        'flex items-center justify-between gap-3 px-2.5 py-1.5 rounded-lg w-full text-left',
        'hover:bg-white/[0.03] transition-colors duration-150',
      )}
    >
      <div className="flex flex-col gap-0.5 min-w-0">
        <span className="text-[12px] font-medium text-ink">{label}</span>
        <span className="text-[10px] text-ink-muted truncate">{description}</span>
      </div>
      <span
        className={cn(
          'relative inline-flex items-center w-7 h-4 rounded-full shrink-0 transition-colors',
          value ? 'bg-accent-lineage' : 'bg-white/10',
        )}
        aria-hidden
      >
        <span
          className={cn(
            'absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-transform',
            value ? 'translate-x-3.5' : 'translate-x-0.5',
          )}
        />
      </span>
    </button>
  )
}
