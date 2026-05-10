/**
 * EntityDrawer - Unified entity details and editing drawer
 * 
 * A modern slide-in drawer that appears when any entity is selected, providing:
 * - View mode: Rich entity details, properties, lineage preview, activity
 * - Edit mode: Inline property editing with validation
 * - Raw JSON mode: Advanced editing for power users
 * - Quick actions: Trace, Pin, External links
 * - Responsive and accessible design
 */

import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { useCanvasStore } from '@/store/canvas'
import { useSchemaStore } from '@/store/schema'
import { usePersonaStore } from '@/store/persona'
import { useEntityColorSet } from '@/hooks/useEntityVisual'
import { useStagedChangesStore } from '@/store/stagedChangesStore'
import { cn } from '@/lib/utils'

// ============================================
// Types
// ============================================

interface EntityDrawerProps {
  /** Callback when trace upstream is triggered */
  onTraceUp?: (nodeId: string) => void
  /** Callback when trace downstream is triggered */
  onTraceDown?: (nodeId: string) => void
  /** Callback when full trace is triggered */
  onFullTrace?: (nodeId: string) => void
  /** External link URL builder */
  getExternalUrl?: (urn: string) => string | null
}

type ViewMode = 'view' | 'edit' | 'json'

// ============================================
// Main Component
// ============================================

export function EntityDrawer({
  onTraceUp,
  onTraceDown,
  onFullTrace,
  getExternalUrl,
}: EntityDrawerProps) {
  const { selectedNodeIds, nodes, updateNode, clearSelection } = useCanvasStore()
  const { schema } = useSchemaStore()
  const mode = usePersonaStore((s) => s.mode)

  // Only show if exactly one non-logical node is selected.
  // Logical nodes (id starts with "logical:") are virtual groupings, not physical entities.
  const selectedNode = selectedNodeIds.length === 1 && !selectedNodeIds[0].startsWith('logical:')
    ? nodes.find(n => n.id === selectedNodeIds[0])
    : null

  const isOpen = !!selectedNode

  // Local state
  const [viewMode, setViewMode] = useState<ViewMode>('view')
  const [formData, setFormData] = useState<Record<string, any>>({})
  const [rawJson, setRawJson] = useState('')
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [showSaved, setShowSaved] = useState(false)
  const [isPinned, setIsPinned] = useState(false)
  const [copiedUrn, setCopiedUrn] = useState(false)
  const drawerRef = useRef<HTMLElement>(null)

  // Reset state when selection changes
  useEffect(() => {
    if (selectedNode) {
      const data = selectedNode.data as Record<string, any>
      setFormData({ ...data })
      setRawJson(JSON.stringify(data, null, 2))
      setHasChanges(false)
      setJsonError(null)
      setViewMode('view')
    }
  }, [selectedNode?.id])

  // Get entity type info from schema
  const entityType = useMemo(() => {
    if (!selectedNode || !schema) return null
    return schema.entityTypes.find(t => t.id === selectedNode.data.type)
  }, [selectedNode, schema])

  // Colors based on entity type (resolved from schema with hash-based fallback)
  const colors = useEntityColorSet((selectedNode?.data.type as string) ?? '')

  // Get display label based on persona mode
  const displayLabel = useMemo(() => {
    if (!selectedNode) return ''
    const data = selectedNode.data as Record<string, any>
    return mode === 'business'
      ? (data.businessLabel || data.label || data.name || selectedNode.id)
      : (data.technicalLabel || data.label || data.name || selectedNode.id)
  }, [selectedNode, mode])

  // Handle form field changes
  const handleChange = useCallback((key: string, value: any) => {
    const newData = { ...formData, [key]: value }
    setFormData(newData)
    setRawJson(JSON.stringify(newData, null, 2))
    setHasChanges(true)
    setJsonError(null)
  }, [formData])

  // Handle raw JSON changes
  const handleRawJsonChange = useCallback((value: string) => {
    setRawJson(value)
    setHasChanges(true)
    try {
      const parsed = JSON.parse(value)
      setFormData(parsed)
      setJsonError(null)
    } catch (e) {
      setJsonError((e as Error).message)
    }
  }, [])

  // Save changes — staged, not committed to backend until user clicks Save Blueprint.
  // Drawer edits primarily change the entity label, so we record a `rename_entity`
  // staged change. Other field changes still update the canvas immediately for
  // visual feedback, and the staging entry captures the full before/after diff
  // so the review panel makes the change visible.
  const handleSave = useCallback(() => {
    if (!selectedNode) return
    if (jsonError) return

    const previousData = { ...(selectedNode.data as Record<string, any>) }
    const previousLabel = (previousData.label as string) ?? ''
    const newLabel = (formData.label as string) ?? previousLabel

    updateNode(selectedNode.id, formData)
    setHasChanges(false)
    setShowSaved(true)
    setTimeout(() => setShowSaved(false), 2000)
    setRawJson(JSON.stringify(formData, null, 2))

    // Stage the change so it becomes visible in the review panel + count badge.
    // stageOrReplace coalesces consecutive edits to the same entity into one entry.
    const isLabelChange = previousLabel !== newLabel
    const stagedChanges = useStagedChangesStore.getState()
    const summary = isLabelChange
      ? `Edit '${previousLabel}' → '${newLabel}'`
      : `Edit fields on '${previousLabel || selectedNode.id}'`
    stagedChanges.stageOrReplace(
      (c) => c.type === 'rename_entity' && c.targetId === selectedNode.id,
      {
        type: 'rename_entity',
        targetId: selectedNode.id,
        targetUrn: previousData.urn,
        before: previousData,
        after: { ...formData },
        summary,
        discard: () => {
          useCanvasStore.getState().updateNode(selectedNode.id, previousData)
        },
      },
    )
  }, [selectedNode, formData, jsonError, updateNode])

  // Cancel changes
  const handleCancel = useCallback(() => {
    if (selectedNode) {
      const data = selectedNode.data as Record<string, any>
      setFormData({ ...data })
      setRawJson(JSON.stringify(data, null, 2))
      setHasChanges(false)
      setJsonError(null)
    }
    setViewMode('view')
  }, [selectedNode])

  // Copy URN
  const handleCopyUrn = useCallback(async () => {
    const urn = formData.urn || selectedNode?.id
    if (urn) {
      await navigator.clipboard.writeText(urn)
      setCopiedUrn(true)
      setTimeout(() => setCopiedUrn(false), 2000)
    }
  }, [formData.urn, selectedNode?.id])

  // Close drawer
  const handleClose = useCallback(() => {
    if (!isPinned) {
      clearSelection()
    }
  }, [clearSelection, isPinned])

  // Click-outside to close drawer (respects pin state)
  useEffect(() => {
    if (!isOpen || isPinned) return
    const handleMouseDown = (e: MouseEvent) => {
      if (drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
        clearSelection()
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [isOpen, isPinned, clearSelection])

  // Get external URL
  const externalUrl = useMemo(() => {
    const urn = formData.urn || selectedNode?.id
    return urn && getExternalUrl ? getExternalUrl(urn) : null
  }, [formData.urn, selectedNode?.id, getExternalUrl])

  // Don't render if no node selected
  if (!isOpen || !selectedNode) return null

  const urn = formData.urn || selectedNode.id
  const childCount = formData.childCount || formData._collapsedChildCount || 0

  // Define which fields are core vs additional
  const coreFields = ['label', 'name', 'description', 'urn', 'type', 'businessLabel', 'technicalLabel']
  const additionalFields = Object.keys(formData).filter(
    k => !coreFields.includes(k) && !k.startsWith('_') && k !== 'childCount' && k !== 'classifications' && k !== 'metadata'
  )

  return (
    <AnimatePresence>
      <motion.aside
        ref={drawerRef}
        data-panel="entity-drawer"
        initial={{ x: '100%', opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        exit={{ x: '100%', opacity: 0 }}
        transition={{ type: 'spring', stiffness: 400, damping: 35 }}
        className={cn(
          "fixed right-0 top-0 bottom-0 w-[clamp(420px,32vw,560px)] z-50",
          "bg-canvas-elevated/98 backdrop-blur-2xl",
          "border-l border-glass-border shadow-lg shadow-black/20",
          "flex flex-col overflow-hidden"
        )}
      >
        {/* Header */}
        <div
          className="flex-shrink-0 p-5 border-b border-glass-border/50"
          style={{
            background: `linear-gradient(135deg, ${colors.accent}10 0%, transparent 60%)`
          }}
        >
          {/* Type Badge & Close */}
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span
                className="px-2.5 py-1 rounded-lg text-xs font-semibold uppercase tracking-wide"
                style={{ backgroundColor: colors.bg, color: colors.text }}
              >
                {entityType?.name || selectedNode.data.type}
              </span>
              {formData.confidence !== undefined && (
                <span className={cn(
                  "text-xs font-medium",
                  formData.confidence >= 0.8 ? "text-green-500" :
                    formData.confidence >= 0.5 ? "text-amber-500" : "text-red-500"
                )}>
                  {Math.round(formData.confidence * 100)}%
                </span>
              )}
            </div>
            <button
              onClick={handleClose}
              className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-white/10 transition-colors duration-150"
            >
              <LucideIcons.X className="w-4 h-4" />
            </button>
          </div>

          {/* Entity Name */}
          <h2 className="text-xl font-display font-semibold text-ink leading-tight mb-4">
            {displayLabel}
          </h2>

          {/* Prominent Trace Actions - Industry-Standard One-Click Lineage */}
          <div className="flex flex-col gap-3 mb-4">
            <div className="grid grid-cols-3 gap-2">
              <motion.button
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                onClick={() => onTraceUp?.(selectedNode.id)}
                className="flex flex-col items-center gap-1.5 p-3 rounded-xl bg-blue-500/10 border border-blue-500/20 hover:bg-blue-500/20 transition-colors duration-150 group"
              >
                <div className="w-10 h-10 rounded-full bg-blue-500/20 flex items-center justify-center group-hover:bg-blue-500/30 transition-colors">
                  <LucideIcons.ArrowUpLeft className="w-5 h-5 text-blue-500" />
                </div>
                <span className="text-xs font-medium text-blue-600 dark:text-blue-400">Root Cause</span>
                <span className="text-[10px] text-blue-500/60">Trace Upstream</span>
              </motion.button>

              <motion.button
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                onClick={() => onTraceDown?.(selectedNode.id)}
                className="flex flex-col items-center gap-1.5 p-3 rounded-xl bg-green-500/10 border border-green-500/20 hover:bg-green-500/20 transition-colors duration-150 group"
              >
                <div className="w-10 h-10 rounded-full bg-green-500/20 flex items-center justify-center group-hover:bg-green-500/30 transition-colors">
                  <LucideIcons.ArrowDownRight className="w-5 h-5 text-green-500" />
                </div>
                <span className="text-xs font-medium text-green-600 dark:text-green-400">Impact</span>
                <span className="text-[10px] text-green-500/60">Trace Downstream</span>
              </motion.button>

              <motion.button
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                onClick={() => onFullTrace?.(selectedNode.id)}
                className="flex flex-col items-center gap-1.5 p-3 rounded-xl bg-purple-500/10 border border-purple-500/20 hover:bg-purple-500/20 transition-colors duration-150 group"
              >
                <div className="w-10 h-10 rounded-full bg-purple-500/20 flex items-center justify-center group-hover:bg-purple-500/30 transition-colors">
                  <LucideIcons.GitBranch className="w-5 h-5 text-purple-500" />
                </div>
                <span className="text-xs font-medium text-purple-600 dark:text-purple-400">Full Lineage</span>
                <span className="text-[10px] text-purple-500/60">Both Directions</span>
              </motion.button>
            </div>
          </div>

          {/* Secondary Quick Actions */}
          <div className="flex items-center gap-2 flex-wrap">
            <ActionButton
              icon={LucideIcons.Pin}
              label={isPinned ? "Unpin" : "Pin"}
              active={isPinned}
              onClick={() => setIsPinned(!isPinned)}
            />
            <ActionButton
              icon={LucideIcons.Copy}
              label={copiedUrn ? "Copied!" : "Copy URN"}
              onClick={() => {
                const urn = (selectedNode.data as Record<string, any>).urn || selectedNode.id
                navigator.clipboard.writeText(urn)
                setCopiedUrn(true)
                setTimeout(() => setCopiedUrn(false), 2000)
              }}
            />
            {externalUrl && (
              <ActionButton
                icon={LucideIcons.ExternalLink}
                label="Open"
                onClick={() => window.open(externalUrl, '_blank')}
              />
            )}
          </div>

          {/* Mode Toggle Strip */}
          <div className="flex items-center gap-1 mt-4 p-1 rounded-xl bg-black/5 dark:bg-white/5">
            <ModeTab
              active={viewMode === 'view'}
              onClick={() => setViewMode('view')}
              icon={LucideIcons.Eye}
              label="View"
            />
            <ModeTab
              active={viewMode === 'edit'}
              onClick={() => setViewMode('edit')}
              icon={LucideIcons.Pencil}
              label="Edit"
              badge={hasChanges ? '•' : undefined}
            />
            <ModeTab
              active={viewMode === 'json'}
              onClick={() => setViewMode('json')}
              icon={LucideIcons.Code}
              label="JSON"
            />
          </div>

          {/* Status Indicators */}
          <AnimatePresence>
            {(hasChanges || showSaved || jsonError) && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="mt-3"
              >
                {jsonError ? (
                  <div className="px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-500 text-xs flex items-center gap-2">
                    <LucideIcons.AlertCircle className="w-4 h-4" />
                    Invalid JSON: {jsonError}
                  </div>
                ) : showSaved ? (
                  <div className="px-3 py-2 rounded-lg bg-green-500/10 border border-green-500/20 text-green-500 text-xs flex items-center gap-2">
                    <LucideIcons.CheckCircle className="w-4 h-4" />
                    Changes saved successfully
                  </div>
                ) : hasChanges ? (
                  <div className="px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-500 text-xs flex items-center gap-2">
                    <LucideIcons.AlertTriangle className="w-4 h-4" />
                    You have unsaved changes
                  </div>
                ) : null}
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {viewMode === 'view' && (
            <ViewModeContent
              formData={formData}
              urn={urn}
              childCount={childCount}
              colors={colors}
              entityType={entityType}
              additionalFields={additionalFields}
              onCopyUrn={handleCopyUrn}
              copiedUrn={copiedUrn}
            />
          )}

          {viewMode === 'edit' && (
            <EditModeContent
              formData={formData}
              entityType={entityType}
              urn={urn}
              additionalFields={additionalFields}
              onChange={handleChange}
              onCopyUrn={handleCopyUrn}
              onSwitchToJson={() => setViewMode('json')}
            />
          )}

          {viewMode === 'json' && (
            <JsonModeContent
              rawJson={rawJson}
              jsonError={jsonError}
              onChange={handleRawJsonChange}
            />
          )}
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 p-4 border-t border-glass-border/50 bg-canvas-elevated/50">
          {viewMode === 'view' ? (
            <div className="flex items-center justify-between text-xs text-ink-muted">
              <div className="flex items-center gap-1.5">
                <LucideIcons.Calendar className="w-3.5 h-3.5" />
                <span>Last synced 5 min ago</span>
              </div>
              {externalUrl && (
                <button
                  onClick={() => window.open(externalUrl, '_blank')}
                  className="text-accent-lineage hover:underline flex items-center gap-1"
                >
                  View in DataHub
                  <LucideIcons.ArrowUpRight className="w-3 h-3" />
                </button>
              )}
            </div>
          ) : (
            <div className="flex items-center justify-end gap-3">
              <button
                onClick={handleCancel}
                className="px-4 py-2 text-sm font-medium text-ink-muted hover:text-ink hover:bg-white/5 rounded-xl transition-colors duration-150"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={!hasChanges || !!jsonError}
                className={cn(
                  "px-5 py-2 rounded-xl text-sm font-semibold flex items-center gap-2 transition-colors duration-150",
                  hasChanges && !jsonError
                    ? "bg-accent-lineage text-white hover:brightness-110 shadow-lg shadow-accent-lineage/25"
                    : "bg-white/5 text-ink-muted cursor-not-allowed"
                )}
              >
                <LucideIcons.Save className="w-4 h-4" />
                Save Changes
              </button>
            </div>
          )}
        </div>
      </motion.aside>
    </AnimatePresence>
  )
}

// ============================================
// Sub-Components
// ============================================

interface ActionButtonProps {
  icon: React.ComponentType<{ className?: string }>
  label: string
  primary?: boolean
  active?: boolean
  onClick?: () => void
}

function ActionButton({ icon: Icon, label, primary, active, onClick }: ActionButtonProps) {
  return (
    <button
      onClick={onClick}
      title={label}
      className={cn(
        "h-9 px-3 rounded-xl flex items-center gap-2 text-sm font-medium transition-colors duration-150 duration-200",
        primary
          ? "bg-accent-lineage text-white hover:brightness-110 shadow-md shadow-accent-lineage/20"
          : active
            ? "bg-white/15 text-ink"
            : "bg-white/5 text-ink-muted hover:text-ink hover:bg-white/10"
      )}
    >
      <Icon className="w-4 h-4" />
      <span className="hidden lg:inline">{label}</span>
    </button>
  )
}

interface ModeTabProps {
  active: boolean
  onClick: () => void
  icon: React.ComponentType<{ className?: string }>
  label: string
  badge?: string
}

function ModeTab({ active, onClick, icon: Icon, label, badge }: ModeTabProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors duration-150 duration-200",
        active
          ? "bg-white/10 text-ink shadow-sm"
          : "text-ink-muted hover:text-ink hover:bg-white/5"
      )}
    >
      <Icon className="w-4 h-4" />
      {label}
      {badge && (
        <span className="text-amber-500 text-xs">{badge}</span>
      )}
    </button>
  )
}

interface SectionProps {
  title: string
  icon?: React.ComponentType<{ className?: string }>
  children: React.ReactNode
  action?: React.ReactNode
}

function Section({ title, icon: Icon, children, action }: SectionProps) {
  return (
    <div className="px-5 py-4">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="w-4 h-4 text-ink-muted" />}
          <h3 className="text-xs font-semibold text-ink-muted uppercase tracking-wider">
            {title}
          </h3>
        </div>
        {action}
      </div>
      {children}
    </div>
  )
}

// ============================================
// View Mode Content
// ============================================

interface ViewModeContentProps {
  formData: Record<string, any>
  urn: string
  childCount: number
  colors: { hex: string; bg: string; text: string; accent: string }
  entityType: any
  additionalFields: string[]
  onCopyUrn: () => void
  copiedUrn: boolean
}

function ViewModeContent({
  formData,
  urn,
  childCount,
  colors,
  entityType,
  additionalFields,
  onCopyUrn,
  copiedUrn,
}: ViewModeContentProps) {
  return (
    <div className="divide-y divide-glass-border/30">
      {/* Identifier */}
      <Section title="Identifier" icon={LucideIcons.Link}>
        <div className="flex items-center gap-2 p-3 rounded-xl bg-black/5 dark:bg-white/5">
          <code className="flex-1 text-xs font-mono text-ink-muted truncate">
            {urn}
          </code>
          <button
            onClick={onCopyUrn}
            className="p-2 rounded-lg hover:bg-white/10 text-ink-muted hover:text-ink transition-colors duration-150"
            title="Copy URN"
          >
            {copiedUrn ? (
              <LucideIcons.Check className="w-4 h-4 text-green-500" />
            ) : (
              <LucideIcons.Copy className="w-4 h-4" />
            )}
          </button>
        </div>
      </Section>

      {/* Properties */}
      {additionalFields.length > 0 && (
        <Section title="Properties" icon={LucideIcons.FileText}>
          <div className="space-y-1">
            {additionalFields.slice(0, 10).map(key => {
              const value = formData[key]
              const displayValue = typeof value === 'object'
                ? JSON.stringify(value)
                : String(value ?? '—')

              return (
                <div key={key} className="flex items-start justify-between gap-4 py-2">
                  <span className="text-sm text-ink-muted capitalize min-w-[100px]">
                    {key.replace(/([A-Z])/g, ' $1').replace(/_/g, ' ').trim()}
                  </span>
                  <span className="text-sm text-ink text-right truncate flex-1 min-w-0" title={displayValue}>
                    {displayValue.length > 80 ? displayValue.slice(0, 80) + '...' : displayValue}
                  </span>
                </div>
              )
            })}
            {additionalFields.length > 10 && (
              <p className="text-xs text-ink-muted pt-2">
                +{additionalFields.length - 10} more properties
              </p>
            )}
          </div>
        </Section>
      )}

      {/* Classifications */}
      {formData.classifications && Array.isArray(formData.classifications) && formData.classifications.length > 0 && (
        <Section title="Classifications" icon={LucideIcons.Tag}>
          <div className="flex flex-wrap gap-2">
            {(formData.classifications as string[]).map(tag => (
              <span
                key={tag}
                className="px-3 py-1.5 rounded-lg text-xs font-medium"
                style={{ backgroundColor: colors.bg, color: colors.text }}
              >
                {tag}
              </span>
            ))}
          </div>
        </Section>
      )}

      {/* Lineage Preview */}
      <Section title="Lineage Preview" icon={LucideIcons.GitBranch}>
        <div className="space-y-2">
          <LineagePreviewRow direction="upstream" count={3} label="Data Sources" />
          <LineagePreviewRow direction="downstream" count={7} label="Data Consumers" />
        </div>
      </Section>

      {/* Recent Activity */}
      <Section title="Recent Activity" icon={LucideIcons.History}>
        <div className="space-y-3">
          <ActivityRow action="Schema updated" time="2 hours ago" user="system" />
          <ActivityRow action="Classification added" time="1 day ago" user="jane.doe@company.com" />
          <ActivityRow action="Created" time="2 weeks ago" user="data-catalog" />
        </div>
      </Section>
    </div>
  )
}

// ============================================
// Edit Mode Content
// ============================================

interface EditModeContentProps {
  formData: Record<string, any>
  entityType: any
  urn: string
  additionalFields: string[]
  onChange: (key: string, value: any) => void
  onCopyUrn: () => void
  onSwitchToJson: () => void
}

function EditModeContent({
  formData,
  entityType,
  urn,
  additionalFields,
  onChange,
  onCopyUrn,
  onSwitchToJson,
}: EditModeContentProps) {
  return (
    <div className="p-5 space-y-5">
      {/* Core Fields */}
      <div className="space-y-4">
        {/* Name/Label */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
            <LucideIcons.Type className="w-3.5 h-3.5" />
            Name
          </label>
          <input
            type="text"
            value={formData.label || formData.name || ''}
            onChange={(e) => onChange('label', e.target.value)}
            className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 focus:bg-white/8 transition-colors duration-150 outline-none text-sm"
            placeholder="Entity name..."
          />
        </div>

        {/* Business Label */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
            <LucideIcons.Briefcase className="w-3.5 h-3.5" />
            Business Label
          </label>
          <input
            type="text"
            value={formData.businessLabel || ''}
            onChange={(e) => onChange('businessLabel', e.target.value)}
            className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 focus:bg-white/8 transition-colors duration-150 outline-none text-sm"
            placeholder="Business-friendly name..."
          />
        </div>

        {/* Description */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
            <LucideIcons.FileText className="w-3.5 h-3.5" />
            Description
          </label>
          <textarea
            value={formData.description || ''}
            onChange={(e) => onChange('description', e.target.value)}
            rows={3}
            className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 focus:bg-white/8 transition-colors duration-150 outline-none text-sm resize-none"
            placeholder="Add a description..."
          />
        </div>

        {/* URN (read-only) */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
            <LucideIcons.Link className="w-3.5 h-3.5" />
            URN (read-only)
          </label>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={urn}
              readOnly
              className="flex-1 px-4 py-3 rounded-xl bg-black/10 dark:bg-white/5 border border-transparent text-ink-muted text-sm font-mono cursor-not-allowed"
            />
            <button
              onClick={onCopyUrn}
              className="p-3 rounded-xl bg-white/5 hover:bg-white/10 text-ink-muted hover:text-ink transition-colors duration-150"
              title="Copy URN"
            >
              <LucideIcons.Copy className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Dynamic Schema Fields */}
      {entityType?.fields && entityType.fields.filter((f: any) => !['name', 'label', 'description', 'urn', 'businessLabel'].includes(f.id)).length > 0 && (
        <div className="pt-5 border-t border-glass-border/30">
          <h4 className="text-xs font-semibold text-ink-muted uppercase tracking-wider mb-4">
            Schema Properties
          </h4>
          <div className="space-y-4">
            {entityType.fields.filter((f: any) => !['name', 'label', 'description', 'urn', 'businessLabel'].includes(f.id)).map((field: any) => (
              <div key={field.id} className="space-y-2">
                <label className="text-xs font-medium text-ink-muted">{field.name}</label>
                {field.type === 'textarea' || field.type === 'markdown' ? (
                  <textarea
                    value={formData[field.id] || ''}
                    onChange={(e) => onChange(field.id, e.target.value)}
                    rows={2}
                    className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 transition-colors duration-150 outline-none text-sm resize-none"
                  />
                ) : (
                  <input
                    type="text"
                    value={formData[field.id] || ''}
                    onChange={(e) => onChange(field.id, e.target.value)}
                    className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 transition-colors duration-150 outline-none text-sm"
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Additional Properties */}
      {additionalFields.length > 0 && (
        <div className="pt-5 border-t border-glass-border/30">
          <h4 className="text-xs font-semibold text-ink-muted uppercase tracking-wider mb-4">
            Additional Properties
          </h4>
          <div className="space-y-4">
            {additionalFields.slice(0, 8).map(key => (
              <div key={key} className="space-y-2">
                <label className="text-xs font-medium text-ink-muted capitalize">
                  {key.replace(/([A-Z])/g, ' $1').replace(/_/g, ' ').trim()}
                </label>
                <input
                  type="text"
                  value={typeof formData[key] === 'object' ? JSON.stringify(formData[key]) : formData[key] || ''}
                  onChange={(e) => onChange(key, e.target.value)}
                  className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 transition-colors duration-150 outline-none text-sm"
                />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Switch to JSON */}
      <button
        onClick={onSwitchToJson}
        className="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-ink-muted hover:text-ink text-sm font-medium transition-colors duration-150"
      >
        <LucideIcons.Code className="w-4 h-4" />
        Edit as Raw JSON
      </button>
    </div>
  )
}

// ============================================
// JSON Mode Content
// ============================================

interface JsonModeContentProps {
  rawJson: string
  jsonError: string | null
  onChange: (value: string) => void
}

function JsonModeContent({ rawJson, jsonError, onChange }: JsonModeContentProps) {
  return (
    <div className="p-5">
      <div className="flex items-center justify-between mb-3">
        <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
          <LucideIcons.Code className="w-3.5 h-3.5" />
          Raw JSON Data
        </label>
        <span className={cn(
          "text-xs px-2 py-1 rounded-lg",
          jsonError
            ? "bg-red-500/10 text-red-500"
            : "bg-green-500/10 text-green-500"
        )}>
          {jsonError ? '⚠️ Invalid' : '✓ Valid'}
        </span>
      </div>
      <textarea
        value={rawJson}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          "w-full h-[500px] px-4 py-3 rounded-xl bg-black/10 dark:bg-white/5 border transition-colors duration-150 outline-none text-xs font-mono resize-none custom-scrollbar",
          jsonError
            ? "border-red-500/30 focus:border-red-500/50"
            : "border-white/10 focus:border-accent-lineage/50"
        )}
        spellCheck={false}
      />
    </div>
  )
}

// ============================================
// Helper Components
// ============================================

interface LineagePreviewRowProps {
  direction: 'upstream' | 'downstream'
  count: number
  label: string
}

function LineagePreviewRow({ direction, count, label }: LineagePreviewRowProps) {
  return (
    <button className={cn(
      "w-full flex items-center gap-3 p-3 rounded-xl",
      "bg-black/5 dark:bg-white/5",
      "hover:bg-black/10 dark:hover:bg-white/10 transition-colors duration-150"
    )}>
      {direction === 'upstream' ? (
        <LucideIcons.ArrowUpRight className="w-4 h-4 text-ink-muted" />
      ) : (
        <LucideIcons.ArrowDownLeft className="w-4 h-4 text-ink-muted" />
      )}
      <div className="flex-1 text-left">
        <span className="text-sm font-medium text-ink">{count} {label}</span>
      </div>
      <span className="text-xs text-ink-muted">View →</span>
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
    <div className="flex items-center gap-3">
      <div className="w-8 h-8 rounded-full bg-black/5 dark:bg-white/5 flex items-center justify-center flex-shrink-0">
        <LucideIcons.Users className="w-4 h-4 text-ink-muted" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-ink truncate">{action}</p>
        <p className="text-xs text-ink-muted">{user} • {time}</p>
      </div>
    </div>
  )
}

export default EntityDrawer

