import { motion, LayoutGroup } from 'framer-motion'
import { Compass, Layers3, SlidersHorizontal } from 'lucide-react'
import { cn } from '@/lib/utils'

export type TraceDockTab = 'overview' | 'drilldowns' | 'settings'

export interface TraceDockTabsProps {
  active: TraceDockTab
  onChange: (t: TraceDockTab) => void
  drilldownCount: number
  hasNotice: boolean
}

interface TabSpec {
  id: TraceDockTab
  label: string
  icon: React.ComponentType<{ className?: string; strokeWidth?: number }>
  count?: number
  alert?: boolean
}

/**
 * Premium tab strip — matches the app's gradient pill button vocabulary
 * (rounded-xl, gradient bg on active, glow shadow on hover). Active tab
 * uses an accent-lineage gradient with shadow; inactive tabs are quiet
 * glass with hover lift. The sliding underline (layoutId) provides a
 * second visual anchor for the active state.
 */
export function TraceDockTabs({ active, onChange, drilldownCount, hasNotice }: TraceDockTabsProps) {
  const tabs: TabSpec[] = [
    { id: 'overview', label: 'Overview', icon: Compass, alert: hasNotice },
    { id: 'drilldowns', label: 'Drilldowns', icon: Layers3, count: drilldownCount > 0 ? drilldownCount : undefined },
    { id: 'settings', label: 'Settings', icon: SlidersHorizontal },
  ]

  return (
    <LayoutGroup id="trace-dock-tabs">
      <div
        role="tablist"
        aria-label="Trace dock sections"
        className={cn(
          'relative flex items-center gap-1.5 px-4 h-11 shrink-0',
          'border-b border-white/[0.06]',
          'bg-gradient-to-r from-white/[0.02] via-transparent to-white/[0.02]',
        )}
      >
        {tabs.map(tab => {
          const Icon = tab.icon
          const isActive = active === tab.id
          return (
            <button
              key={tab.id}
              role="tab"
              type="button"
              aria-selected={isActive}
              aria-controls={`trace-dock-panel-${tab.id}`}
              id={`trace-dock-tab-${tab.id}`}
              tabIndex={isActive ? 0 : -1}
              onClick={() => onChange(tab.id)}
              className={cn(
                'relative inline-flex items-center gap-2 px-3.5 h-8 rounded-xl',
                'text-sm font-semibold transition-all duration-200',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-lineage/40',
                isActive
                  ? 'bg-accent-lineage text-white border border-accent-lineage shadow-lg shadow-accent-lineage/30'
                  : 'text-ink-muted hover:text-ink hover:bg-white/[0.08] border border-transparent hover:border-white/[0.12]',
              )}
            >
              <Icon className="w-4 h-4" strokeWidth={isActive ? 2.4 : 2} />
              <span className={cn('tracking-tight', isActive && 'font-semibold')}>
                {tab.label}
              </span>
              {tab.count !== undefined && (
                <span
                  className={cn(
                    'inline-flex items-center justify-center min-w-[20px] h-[18px] px-1.5 rounded-full',
                    'text-[10px] font-bold tabular-nums leading-none',
                    isActive
                      ? 'bg-white/25 text-white'
                      : 'bg-white/[0.15] text-ink',
                  )}
                >
                  {tab.count}
                </span>
              )}
              {tab.alert && (
                <span
                  className="relative inline-flex w-2 h-2"
                  aria-label="Notice present"
                >
                  <span className="absolute inset-0 rounded-full bg-amber-400/60 animate-ping motion-reduce:animate-none" />
                  <span className="relative w-2 h-2 rounded-full bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.6)]" />
                </span>
              )}
              {isActive && (
                <motion.span
                  layoutId="trace-tab-indicator"
                  transition={{ type: 'spring', stiffness: 380, damping: 32 }}
                  className="absolute -bottom-[5px] left-3 right-3 h-[2px] rounded-full bg-accent-lineage"
                />
              )}
            </button>
          )
        })}
      </div>
    </LayoutGroup>
  )
}
