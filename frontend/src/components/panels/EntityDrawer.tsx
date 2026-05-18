/**
 * EntityDrawer - Unified entity details and editing drawer
 *
 * A modern slide-in drawer that appears when any entity is selected, providing:
 * - View mode: Rich entity details, properties (nested-JSON tree), lineage preview, activity
 * - Edit mode: Inline property editing with PropertyEditor for nested values
 * - Raw JSON mode: Advanced editing for power users
 * - Quick actions: Trace, Pin, External links
 *
 * TODO(backend): Drawer edits currently stage as `update_entity` with a no-op
 * apply hook. To persist edits to the backend we need:
 *   1. `PATCH /api/v1/{wsId}/graph/nodes/{urn}` route in
 *      backend/app/api/v1/endpoints/graph.py
 *   2. `GraphDataProvider.update_node(urn, properties)` ABC method in
 *      backend/common/interfaces/provider.py
 *   3. Implementations in FalkorDB / Neo4j / Spanner providers
 * Wire the apply hook in `handleSave` once the endpoint exists.
 */

import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { useCanvasStore } from '@/store/canvas'
import { useSchemaStore } from '@/store/schema'
import { usePersonaStore } from '@/store/persona'
import { useEntityColorSet } from '@/hooks/useEntityVisual'
import { useStagedChangesStore } from '@/store/stagedChangesStore'
import { PropertyEditor } from '@/components/panels/PropertyEditor'
import { LineageNeighbors } from '@/components/panels/LineageNeighbors'
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
  /** Center the underlying canvas on the given node id. Optional — some
   *  canvas variants (Hierarchy, ContextView) don't yet have a focus API
   *  wired, in which case clicking a neighbor still swaps the drawer. */
  onFocusNode?: (nodeId: string) => void
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
  onFocusNode,
  getExternalUrl,
}: EntityDrawerProps) {
  const { drawerNodeId, nodes, updateNode, clearSelection, closeNodeDrawer } = useCanvasStore()
  const { schema } = useSchemaStore()
  const mode = usePersonaStore((s) => s.mode)

  // The drawer is sticky: it shows whichever entity it was last opened on
  // (drawerNodeId), independent of canvas highlight selection. It stays open
  // until explicitly closed via the X button.
  // Logical nodes (id starts with "logical:") are virtual groupings, not physical entities.
  const selectedNode = drawerNodeId && !drawerNodeId.startsWith('logical:')
    ? nodes.find(n => n.id === drawerNodeId)
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

  // Replace the entire `properties` bag — PropertyEditor emits a fresh object
  // on every mutation (add/remove/rename/type-change/reorder). Other top-level
  // canvas-store fields are untouched.
  const handlePropertiesChange = useCallback(
    (nextProperties: Record<string, any>) => {
      const next = { ...formData, properties: nextProperties }
      setFormData(next)
      setRawJson(JSON.stringify(next, null, 2))
      setHasChanges(true)
      setJsonError(null)
    },
    [formData],
  )

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

  // Stage changes — recorded for review, not committed to backend until the
  // user clicks Save Blueprint.
  //
  // Diff strategy: if only `label` differs, stage as `rename_entity` (existing
  // semantics). For any other change (including nested objects like `metadata`),
  // stage as `update_entity` carrying the full before/after diff. The canvas is
  // mutated immediately for visual feedback; staging captures provenance so the
  // review panel can render and discard the change.
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

    // Compute changed keys via shallow JSON-equality (handles nested objects).
    const allKeys = new Set([
      ...Object.keys(previousData),
      ...Object.keys(formData),
    ])
    const changedKeys: string[] = []
    for (const k of allKeys) {
      if (JSON.stringify(previousData[k]) !== JSON.stringify(formData[k])) {
        changedKeys.push(k)
      }
    }

    if (changedKeys.length === 0) return

    const stagedChanges = useStagedChangesStore.getState()
    const onlyLabel = changedKeys.length === 1 && changedKeys[0] === 'label'

    if (onlyLabel) {
      stagedChanges.stageOrReplace(
        (c) => c.type === 'rename_entity' && c.targetId === selectedNode.id,
        {
          type: 'rename_entity',
          targetId: selectedNode.id,
          targetUrn: previousData.urn,
          before: previousData,
          after: { ...formData },
          summary: `Rename '${previousLabel}' → '${newLabel}'`,
          discard: () => {
            useCanvasStore.getState().updateNode(selectedNode.id, previousData)
          },
        },
      )
      return
    }

    // Multi-field edit — stage as update_entity. Apply hook is a stub until
    // the backend ships PATCH /api/v1/{wsId}/graph/nodes/{urn}; see the file
    // header for the full backlog.
    stagedChanges.stageOrReplace(
      (c) => c.type === 'update_entity' && c.targetId === selectedNode.id,
      {
        type: 'update_entity',
        targetId: selectedNode.id,
        targetUrn: previousData.urn,
        before: previousData,
        after: { ...formData },
        summary: `Edit ${changedKeys.length} field${changedKeys.length === 1 ? '' : 's'} on '${previousLabel || selectedNode.id}'`,
        discard: () => {
          useCanvasStore.getState().updateNode(selectedNode.id, previousData)
        },
        apply: async () => {
          // TODO(backend): replace with
          //   await authFetch(`/api/v1/${wsId}/graph/nodes/${urn}`, {
          //     method: 'PATCH', body: JSON.stringify({ properties: after })
          //   })
          // once the endpoint and provider methods land.
          console.warn(
            '[update_entity] TODO: PATCH /api/v1/{wsId}/graph/nodes/{urn} not yet implemented',
            { targetId: selectedNode.id, urn: previousData.urn, changedKeys },
          )
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

  // Close drawer — the X button is the only close path. The drawer is
  // sticky: clicking other entities or the canvas background never closes
  // it, it only swaps the data shown inside.
  const handleClose = useCallback(() => {
    if (!isPinned) {
      closeNodeDrawer()
      clearSelection()
    }
  }, [closeNodeDrawer, clearSelection, isPinned])

  // Get external URL
  const externalUrl = useMemo(() => {
    const urn = formData.urn || selectedNode?.id
    return urn && getExternalUrl ? getExternalUrl(urn) : null
  }, [formData.urn, selectedNode?.id, getExternalUrl])

  // Don't render if no node selected
  if (!isOpen || !selectedNode) return null

  const urn = formData.urn || selectedNode.id
  const childCount = formData.childCount || formData._collapsedChildCount || 0

  // After the converter cleanup in useGraphHydration, the editable property
  // bag lives in a single explicit field (`properties`). PropertyEditor
  // targets it directly; everything else on `data` is structured.
  const propertiesBag: Record<string, any> =
    (formData.properties as Record<string, any> | undefined) ?? {}

  return (
    <AnimatePresence>
      <motion.aside
        ref={drawerRef}
        data-panel="entity-drawer"
        initial={{ width: 0, opacity: 0 }}
        animate={{ width: 'clamp(420px, 32vw, 560px)', opacity: 1 }}
        exit={{ width: 0, opacity: 0 }}
        transition={{ type: 'spring', stiffness: 400, damping: 35 }}
        className={cn(
          "relative h-full flex-shrink-0 overflow-hidden",
          "bg-canvas-elevated/98 backdrop-blur-2xl",
          "border-l border-glass-border shadow-lg shadow-black/20"
        )}
      >
        <div className="w-[clamp(420px,32vw,560px)] h-full flex flex-col overflow-hidden">
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
              nodeId={selectedNode.id}
              formData={formData}
              urn={urn}
              childCount={childCount}
              colors={colors}
              entityType={entityType}
              propertiesBag={propertiesBag}
              onCopyUrn={handleCopyUrn}
              copiedUrn={copiedUrn}
              onFocusNode={onFocusNode}
            />
          )}

          {viewMode === 'edit' && (
            <EditModeContent
              formData={formData}
              entityType={entityType}
              urn={urn}
              propertiesBag={propertiesBag}
              onChange={handleChange}
              onPropertiesChange={handlePropertiesChange}
              onCopyUrn={handleCopyUrn}
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
                <ComingSoonChip />
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
                Stage Changes
              </button>
            </div>
          )}
        </div>
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

// Subtle "Coming soon" chip for sections that show placeholder data until
// the backend lands (activity log, lineage counts, last-synced timestamp).
function ComingSoonChip() {
  return (
    <span className="px-2 py-0.5 rounded-md text-[10px] font-medium uppercase tracking-wider bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
      Coming soon
    </span>
  )
}

/**
 * Read-only display of the descriptive backend fields surfaced on
 * `LineageNode.data` (qualifiedName, sourceSystem, layerAssignment,
 * lastSyncedAt, childCount, description). Hidden entirely when none have
 * values so empty entities don't show a useless section.
 */
function DetailsList({ formData }: { formData: Record<string, any> }) {
  const rows: Array<{ key: string; label: string; value: React.ReactNode }> = []
  const push = (key: string, label: string, raw: unknown) => {
    if (raw === undefined || raw === null || raw === '') return
    rows.push({ key, label, value: String(raw) })
  }
  push('qualifiedName', 'Qualified name', formData.qualifiedName)
  push('description', 'Description', formData.description)
  push('sourceSystem', 'Source system', formData.sourceSystem)
  push('layerAssignment', 'Layer', formData.layerAssignment)
  push('lastSyncedAt', 'Last synced', formData.lastSyncedAt)
  if (typeof formData.childCount === 'number' && formData.childCount > 0) {
    rows.push({ key: 'childCount', label: 'Children', value: String(formData.childCount) })
  }
  if (rows.length === 0) return null
  return (
    <Section title="Details" icon={LucideIcons.Info}>
      <div className="space-y-1">
        {rows.map((row) => (
          <div key={row.key} className="flex items-start justify-between gap-4 py-1.5">
            <span className="text-xs text-ink-muted min-w-[110px]">{row.label}</span>
            <span className="text-xs text-ink text-right break-all">{row.value}</span>
          </div>
        ))}
      </div>
    </Section>
  )
}

// ============================================
// View Mode Content
// ============================================

interface ViewModeContentProps {
  nodeId: string
  formData: Record<string, any>
  urn: string
  childCount: number
  colors: { hex: string; bg: string; text: string; accent: string }
  entityType: any
  propertiesBag: Record<string, any>
  onCopyUrn: () => void
  copiedUrn: boolean
  onFocusNode?: (nodeId: string) => void
}

function ViewModeContent({
  nodeId,
  formData,
  urn,
  colors,
  propertiesBag,
  onCopyUrn,
  copiedUrn,
  onFocusNode,
}: ViewModeContentProps) {
  const hasAdditional = Object.keys(propertiesBag).length > 0
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

      {/* Details — first-class descriptive fields carried from the backend
          GraphNode. Only rendered when at least one has a real value. */}
      <DetailsList formData={formData} />

      {/* Properties — nested-JSON tree rendered read-only via PropertyEditor.
          pointer-events-none keeps the recursive UI from accepting edits in
          View mode; the same component is used (editable) in Edit mode. */}
      {hasAdditional && (
        <Section title="Properties" icon={LucideIcons.FileText}>
          <div className="pointer-events-none opacity-95">
            <PropertyEditor value={propertiesBag} onChange={() => {}} bare />
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

      {/* Lineage — real 1-hop neighbors with direction/entity/edge filters. */}
      <LineageNeighbors nodeId={nodeId} onFocusNode={onFocusNode} />

      {/* Recent Activity */}
      <Section title="Recent Activity" icon={LucideIcons.History} action={<ComingSoonChip />}>
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
  propertiesBag: Record<string, any>
  onChange: (key: string, value: any) => void
  onPropertiesChange: (next: Record<string, any>) => void
  onCopyUrn: () => void
}

function EditModeContent({
  formData,
  entityType,
  urn,
  propertiesBag,
  onChange,
  onPropertiesChange,
  onCopyUrn,
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

      {/* Metadata — first-class descriptive fields carried from the backend
          GraphNode. lastSyncedAt and childCount are backend-managed and
          rendered read-only; the rest accept user edits. */}
      <div className="pt-5 border-t border-glass-border/30">
        <h4 className="text-xs font-semibold text-ink-muted uppercase tracking-wider mb-4">
          Metadata
        </h4>
        <div className="space-y-4">
          <div className="space-y-2">
            <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
              <LucideIcons.AtSign className="w-3.5 h-3.5" />
              Qualified name
            </label>
            <input
              type="text"
              value={(formData.qualifiedName as string) || ''}
              onChange={(e) => onChange('qualifiedName', e.target.value)}
              className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 transition-colors duration-150 outline-none text-sm"
              placeholder="Fully qualified name..."
            />
          </div>
          <div className="space-y-2">
            <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
              <LucideIcons.Database className="w-3.5 h-3.5" />
              Source system
            </label>
            <input
              type="text"
              value={(formData.sourceSystem as string) || ''}
              onChange={(e) => onChange('sourceSystem', e.target.value)}
              className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 transition-colors duration-150 outline-none text-sm"
              placeholder="Origin system identifier..."
            />
          </div>
          <div className="space-y-2">
            <label className="text-xs font-semibold text-ink-muted flex items-center gap-2">
              <LucideIcons.Layers className="w-3.5 h-3.5" />
              Layer
            </label>
            <input
              type="text"
              value={(formData.layerAssignment as string) || ''}
              onChange={(e) => onChange('layerAssignment', e.target.value)}
              className="w-full px-4 py-3 rounded-xl bg-white/5 border border-white/10 focus:border-accent-lineage/50 transition-colors duration-150 outline-none text-sm"
              placeholder="Layer assignment..."
            />
          </div>
          {(formData.lastSyncedAt || typeof formData.childCount === 'number') && (
            <div className="flex items-center justify-between gap-4 px-4 py-3 rounded-xl bg-black/5 dark:bg-white/[0.03] text-xs">
              {formData.lastSyncedAt ? (
                <div className="flex items-center gap-2 text-ink-muted">
                  <LucideIcons.Clock className="w-3.5 h-3.5" />
                  <span>Last synced</span>
                  <span className="text-ink">{String(formData.lastSyncedAt)}</span>
                </div>
              ) : <span />}
              {typeof formData.childCount === 'number' && formData.childCount > 0 && (
                <div className="flex items-center gap-2 text-ink-muted">
                  <LucideIcons.GitBranch className="w-3.5 h-3.5" />
                  <span>{formData.childCount} children</span>
                </div>
              )}
            </div>
          )}
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

      {/* Properties — nested-JSON CRUD via PropertyEditor. Targets the
          single `properties` bag on node.data (renamed from `metadata`).
          Users can add/remove/rename keys, change value types, and
          drag-reorder array items. */}
      <div className="pt-5 border-t border-glass-border/30">
        <h4 className="text-xs font-semibold text-ink-muted uppercase tracking-wider mb-4">
          Properties
        </h4>
        <PropertyEditor
          value={propertiesBag}
          onChange={(next) => onPropertiesChange(next as Record<string, any>)}
          bare
        />
      </div>
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

