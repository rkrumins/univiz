import { motion } from 'framer-motion'
import { Sliders, Activity } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { UseUnifiedTraceResult } from '@/hooks/useUnifiedTrace'
import { TraceDockControls, type GranularityOption } from './TraceDockControls'
import { TraceDockPerformance } from './TraceDockPerformance'

export interface TraceDockSettingsProps {
  trace: UseUnifiedTraceResult
  granularityOptions: GranularityOption[]
  availableEdgeTypes: string[]
  resolveEdgeColor: (edgeType: string) => string
}

/**
 * Settings tab — premium expert mode. Two sections, each headed by a
 * gradient identity icon + eyebrow (matches the app's section-header
 * vocabulary): Tuning (controls) and Performance (telemetry).
 */
export function TraceDockSettings({
  trace,
  granularityOptions,
  availableEdgeTypes,
  resolveEdgeColor,
}: TraceDockSettingsProps) {
  return (
    <motion.div
      key="settings"
      id="trace-dock-panel-settings"
      role="tabpanel"
      aria-labelledby="trace-dock-tab-settings"
      initial={{ opacity: 0, scale: 0.99 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.99 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className="flex-1 min-h-0 flex flex-col overflow-y-auto custom-scrollbar"
    >
      <div className="px-5 pt-4 pb-5 flex flex-col gap-5">
        <Section icon={<Sliders className="w-3.5 h-3.5" strokeWidth={2.2} />} label="Tuning">
          <TraceDockControls
            config={trace.config}
            granularityOptions={granularityOptions}
            availableEdgeTypes={availableEdgeTypes}
            resolveEdgeColor={resolveEdgeColor}
            onChangeConfig={trace.setConfig}
            onApply={() => { trace.retrace() }}
          />
        </Section>

        <Section icon={<Activity className="w-3.5 h-3.5" strokeWidth={2.2} />} label="Performance">
          <TraceDockPerformance meta={trace.result?.meta} />
        </Section>
      </div>
    </motion.div>
  )
}

function Section({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2.5">
        <div
          className={cn(
            'shrink-0 w-7 h-7 rounded-lg flex items-center justify-center',
            'bg-accent-lineage border border-accent-lineage shadow-sm shadow-accent-lineage/20',
          )}
        >
          <span className="text-white" aria-hidden>{icon}</span>
        </div>
        <span className="text-[11px] uppercase tracking-[0.14em] font-bold text-ink">
          {label}
        </span>
        <span className="flex-1 h-px bg-gradient-to-r from-accent-lineage/40 via-white/10 to-transparent" />
      </div>
      <div className="pl-0">{children}</div>
    </div>
  )
}
