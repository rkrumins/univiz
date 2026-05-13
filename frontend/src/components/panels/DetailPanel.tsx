import { useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X,
  ArrowUpRight,
  ArrowDownLeft,
  GitBranch,
  ExternalLink,
  Copy,
  Pin,
  History,
  Tag,
  Users,
  Calendar,
  Database
} from 'lucide-react'
import { useCanvasStore } from '@/store/canvas'
import { usePersonaStore } from '@/store/persona'
import { useEntityColorSet } from '@/hooks/useEntityVisual'
import { cn } from '@/lib/utils'

interface DetailPanelProps {
  isOpen: boolean
  nodeId?: string
}

export function DetailPanel({ isOpen, nodeId }: DetailPanelProps) {
  const nodes = useCanvasStore((s) => s.nodes)
  const clearSelection = useCanvasStore((s) => s.clearSelection)
  const mode = usePersonaStore((s) => s.mode)

  const node = useMemo(() =>
    nodes.find((n) => n.id === nodeId),
    [nodes, nodeId]
  )

  const colors = useEntityColorSet((node?.data.type as string) ?? '')

  if (!node) return null

  const label = mode === 'business'
    ? (node.data.businessLabel || node.data.label)
    : (node.data.technicalLabel || node.data.label)

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.aside
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 380, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ type: 'spring', stiffness: 400, damping: 30 }}
          className={cn(
            "h-full border-l border-glass-border bg-canvas-elevated",
            "flex flex-col overflow-hidden"
          )}
        >
          {/* Header */}
          <div className="p-4 border-b border-glass-border">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className="px-2 py-0.5 rounded text-2xs font-medium uppercase"
                    style={{ backgroundColor: colors.bg, color: colors.text }}
                  >
                    {node.data.type}
                  </span>
                  {node.data.confidence !== undefined && (
                    <span className={cn(
                      "text-2xs font-medium",
                      node.data.confidence >= 0.8 ? "text-green-500" :
                        node.data.confidence >= 0.5 ? "text-amber-500" : "text-red-500"
                    )}>
                      {Math.round(node.data.confidence * 100)}% confidence
                    </span>
                  )}
                </div>
                <h2 className="font-display font-semibold text-lg text-ink leading-tight">
                  {label}
                </h2>
              </div>
              <button
                onClick={clearSelection}
                className={cn(
                  "w-8 h-8 rounded-lg flex items-center justify-center",
                  "text-ink-muted hover:text-ink hover:bg-black/5 dark:hover:bg-white/5",
                  "transition-colors"
                )}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Quick Actions */}
            <div className="flex items-center gap-2 mt-3">
              <ActionButton icon={ArrowUpRight} label="Trace Up" primary />
              <ActionButton icon={ArrowDownLeft} label="Trace Down" />
              <ActionButton icon={GitBranch} label="Full Trace" />
              <div className="flex-1" />
              <ActionButton icon={Pin} label="Pin" />
              <ActionButton icon={ExternalLink} label="Open" />
            </div>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto custom-scrollbar p-4 space-y-6">
            {/* URN Section */}
            {node.data.urn && (
              <Section title="Identifier">
                <div className="flex items-center gap-2">
                  <code className={cn(
                    "flex-1 text-xs font-mono p-2 rounded-lg",
                    "bg-black/5 dark:bg-white/5 text-ink-secondary",
                    "overflow-x-auto"
                  )}>
                    {node.data.urn}
                  </code>
                  <button
                    className={cn(
                      "w-8 h-8 rounded-lg flex items-center justify-center",
                      "text-ink-muted hover:text-ink hover:bg-black/5 dark:hover:bg-white/5"
                    )}
                    title="Copy URN"
                  >
                    <Copy className="w-4 h-4" />
                  </button>
                </div>
              </Section>
            )}

            {/* Classifications */}
            {node.data.classifications && node.data.classifications.length > 0 && (
              <Section title="Classifications" icon={Tag}>
                <div className="flex flex-wrap gap-2">
                  {node.data.classifications.map((tag) => (
                    <span
                      key={tag}
                      className="px-2 py-1 rounded-lg text-xs font-medium"
                      style={{ backgroundColor: colors.bg, color: colors.text }}
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </Section>
            )}

            {/* Properties */}
            {node.data.properties && Object.keys(node.data.properties).length > 0 && (
              <Section title="Properties" icon={Database}>
                <div className="space-y-2">
                  {Object.entries(node.data.properties).map(([key, value]) => (
                    <MetadataRow key={key} label={key} value={value} />
                  ))}
                </div>
              </Section>
            )}

            {/* Lineage Preview */}
            <Section title="Lineage Preview" icon={GitBranch}>
              <div className="space-y-3">
                <LineagePreviewRow
                  direction="upstream"
                  count={3}
                  label="Data Sources"
                />
                <LineagePreviewRow
                  direction="downstream"
                  count={7}
                  label="Data Consumers"
                />
              </div>
            </Section>

            {/* Activity */}
            <Section title="Recent Activity" icon={History}>
              <div className="space-y-2">
                <ActivityRow
                  action="Schema updated"
                  time="2 hours ago"
                  user="system"
                />
                <ActivityRow
                  action="Classification added"
                  time="1 day ago"
                  user="jane.doe@company.com"
                />
                <ActivityRow
                  action="Created"
                  time="2 weeks ago"
                  user="data-catalog"
                />
              </div>
            </Section>
          </div>

          {/* Footer */}
          <div className="p-4 border-t border-glass-border">
            <div className="flex items-center justify-between text-2xs text-ink-muted">
              <div className="flex items-center gap-1">
                <Calendar className="w-3 h-3" />
                <span>Last synced 5 min ago</span>
              </div>
              <button className="text-accent-lineage hover:underline">
                View in DataHub →
              </button>
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  )
}

interface ActionButtonProps {
  icon: React.ComponentType<{ className?: string }>
  label: string
  primary?: boolean
  onClick?: () => void
}

function ActionButton({ icon: Icon, label, primary, onClick }: ActionButtonProps) {
  return (
    <button
      onClick={onClick}
      title={label}
      className={cn(
        "h-8 px-2.5 rounded-lg flex items-center gap-1.5",
        "text-sm font-medium transition-colors",
        primary
          ? "bg-accent-lineage text-white hover:brightness-110"
          : "bg-black/5 dark:bg-white/5 text-ink-secondary hover:text-ink hover:bg-black/10 dark:hover:bg-white/10"
      )}
    >
      <Icon className="w-3.5 h-3.5" />
      <span className="hidden sm:inline">{label}</span>
    </button>
  )
}

interface SectionProps {
  title: string
  icon?: React.ComponentType<{ className?: string }>
  children: React.ReactNode
}

function Section({ title, icon: Icon, children }: SectionProps) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        {Icon && <Icon className="w-4 h-4 text-ink-muted" />}
        <h3 className="text-xs font-medium text-ink-muted uppercase tracking-wider">
          {title}
        </h3>
      </div>
      {children}
    </div>
  )
}

interface MetadataRowProps {
  label: string
  value: unknown
}

function MetadataRow({ label, value }: MetadataRowProps) {
  const displayValue = typeof value === 'object'
    ? JSON.stringify(value)
    : String(value)

  return (
    <div className="flex items-start justify-between gap-4 py-1">
      <span className="text-sm text-ink-muted capitalize">
        {label.replace(/_/g, ' ')}
      </span>
      <span className="text-sm text-ink text-right truncate max-w-[180px]">
        {displayValue}
      </span>
    </div>
  )
}

interface LineagePreviewRowProps {
  direction: 'upstream' | 'downstream'
  count: number
  label: string
}

function LineagePreviewRow({ direction, count, label }: LineagePreviewRowProps) {
  return (
    <button className={cn(
      "w-full flex items-center gap-3 p-2 rounded-lg",
      "bg-black/5 dark:bg-white/5",
      "hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
    )}>
      {direction === 'upstream' ? (
        <ArrowUpRight className="w-4 h-4 text-ink-muted" />
      ) : (
        <ArrowDownLeft className="w-4 h-4 text-ink-muted" />
      )}
      <div className="flex-1 text-left">
        <span className="text-sm font-medium text-ink">{count} {label}</span>
      </div>
      <span className="text-2xs text-ink-muted">View →</span>
    </button>
  )
}

interface ActivityRowProps {
  action: string
  time: string
  user: string
}

function ActivityRow({ action, time, user }: ActivityRowProps) {
  return (
    <div className="flex items-center gap-3 py-1">
      <div className="w-6 h-6 rounded-full bg-black/5 dark:bg-white/5 flex items-center justify-center">
        <Users className="w-3 h-3 text-ink-muted" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-ink truncate">{action}</p>
        <p className="text-2xs text-ink-muted">{user} • {time}</p>
      </div>
    </div>
  )
}

