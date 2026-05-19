/**
 * ViewEditor - Component for editing view configurations
 * 
 * Allows users to configure:
 * - Visible entity types
 * - Visible relationship types
 * - Projection settings (aggregation, collapse)
 * - Reference model layers
 * - Layout options
 */

import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { useSchemaStore } from '@/store/schema'
import { generateIconFallback } from '@/lib/type-visuals'
import { resolveEntityIcon } from '@/lib/entityIcon'
import type { ViewConfiguration, EntityTypeSchema, ViewLayerConfig, LayerAssignmentRuleConfig, LogicalNodeConfig } from '@/types/schema'
import { cn } from '@/lib/utils'
import { EntityAssignmentPanel } from './EntityAssignmentPanel'
import { LayerDropZoneRow } from './LayerDropZone'
import { ReferenceModelBuilder } from './ReferenceModelBuilder'
import { useInstanceAssignments } from '@/store/referenceModelStore'

// Dynamic icon component — validates the Lucide lookup is renderable
// (forwardRef object or function component), else falls back to Box.
function DynamicIcon({ name, className, style }: { name: string; className?: string; style?: React.CSSProperties }) {
  const IconComponent = resolveEntityIcon(name)
  return <IconComponent className={className} style={style} />
}

interface ViewEditorProps {
  viewId?: string // Existing view ID to edit, or undefined for new
  onClose: () => void
  onSave: (view: ViewConfiguration) => void
}

const GRANULARITY_LEVELS = [
  { value: 0, label: 'Column', description: 'Most detailed - show all columns' },
  { value: 1, label: 'Table', description: 'Aggregate to table level' },
  { value: 2, label: 'Schema', description: 'Aggregate to schema level' },
  { value: 3, label: 'System', description: 'Aggregate to system level' },
  { value: 4, label: 'Domain', description: 'Most abstract - domains only' },
]

const LAYOUT_TYPES = [
  { value: 'graph', label: 'Graph', icon: 'Network', description: 'Force-directed or DAG layout' },
  { value: 'hierarchy', label: 'Hierarchy', icon: 'ListTree', description: 'Nested tree view' },
  { value: 'reference', label: 'Reference Model', icon: 'LayoutTemplate', description: 'Horizontal layer columns' },
]

export function ViewEditor({ viewId, onClose, onSave }: ViewEditorProps) {
  const schema = useSchemaStore((s) => s.schema)
  const instanceAssignments = useInstanceAssignments()

  // Load existing view or create new one
  const existingView = viewId
    ? schema?.views.find((v) => v.id === viewId)
    : undefined

  const [view, setView] = useState<Partial<ViewConfiguration>>(() => {
    if (existingView) return { ...existingView }
    return {
      id: `view-${Date.now()}`,
      name: 'New View',
      description: '',
      icon: 'Layout',
      content: {
        visibleEntityTypes: schema?.entityTypes.map((e) => e.id) ?? [],
        visibleRelationshipTypes: schema?.relationshipTypes.map((r) => r.id) ?? [],
        defaultDepth: 5,
        maxDepth: 10,
        rootEntityTypes: ['domain'],
      },
      layout: {
        type: 'graph',
        graphLayout: {
          algorithm: 'dagre',
          direction: 'LR',
          nodeSpacing: 60,
          levelSpacing: 120,
        },
        lod: { enabled: false, levels: [] },
        projection: {
          targetGranularity: 1,
          aggregateLineage: false,
          collapseChildren: false,
          containerTypes: [],
        },
        referenceLayout: {
          layers: [],
        },
      },
      filters: {
        entityTypeFilters: [],
        fieldFilters: [],
        searchableFields: [],
        quickFilters: [],
      },
      entityOverrides: {},
      isDefault: false,
      isPublic: true,
      createdBy: 'user',
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    }
  })

  const [activeTab, setActiveTab] = useState<'general' | 'entities' | 'projection' | 'layers'>('general')
  const [isAssignmentPanelOpen, setIsAssignmentPanelOpen] = useState(false)
  const [isBuilderOpen, setIsBuilderOpen] = useState(false)

  // Compute entity counts per layer for visual feedback
  const entityCounts = useMemo(() => {
    const counts = new Map<string, number>()
    instanceAssignments.forEach((assignment) => {
      const current = counts.get(assignment.layerId) ?? 0
      counts.set(assignment.layerId, current + 1)
    })
    return counts
  }, [instanceAssignments])

  // Update a nested property
  const updateView = <K extends keyof ViewConfiguration>(
    key: K,
    value: ViewConfiguration[K]
  ) => {
    setView((prev) => ({ ...prev, [key]: value }))
  }

  const updateProjection = (key: string, value: unknown) => {
    setView((prev) => ({
      ...prev,
      layout: {
        ...prev.layout!,
        projection: {
          ...prev.layout?.projection,
          [key]: value,
        } as any,
      },
    }))
  }

  const updateContent = (key: string, value: unknown) => {
    setView((prev) => ({
      ...prev,
      content: {
        ...prev.content!,
        [key]: value,
      },
    }))
  }

  // Toggle entity type visibility
  const toggleEntityType = (typeId: string) => {
    const current = view.content?.visibleEntityTypes ?? []
    const updated = current.includes(typeId)
      ? current.filter((id) => id !== typeId)
      : [...current, typeId]
    updateContent('visibleEntityTypes', updated)
  }

  // Add a layer
  const addLayer = () => {
    const layers = view.layout?.referenceLayout?.layers ?? []
    const newLayer: ViewLayerConfig = {
      id: `layer-${Date.now()}`,
      name: `Layer ${layers.length + 1}`,
      description: '',
      icon: 'Layers',
      color: '#6366f1',
      entityTypes: [],
      order: layers.length,
    }
    setView((prev) => ({
      ...prev,
      layout: {
        ...prev.layout!,
        referenceLayout: {
          layers: [...layers, newLayer],
        },
      },
    }))
  }

  const updateLayer = (layerId: string, updates: Partial<ViewLayerConfig>) => {
    const layers = view.layout?.referenceLayout?.layers ?? []
    const updated = layers.map((l) =>
      l.id === layerId ? { ...l, ...updates } : l
    )
    setView((prev) => ({
      ...prev,
      layout: {
        ...prev.layout!,
        referenceLayout: { layers: updated },
      },
    }))
  }

  const removeLayer = (layerId: string) => {
    const layers = view.layout?.referenceLayout?.layers ?? []
    setView((prev) => ({
      ...prev,
      layout: {
        ...prev.layout!,
        referenceLayout: {
          layers: layers.filter((l) => l.id !== layerId),
        },
      },
    }))
  }

  const handleSave = () => {
    const finalView: ViewConfiguration = {
      ...view,
      updatedAt: new Date().toISOString(),
    } as ViewConfiguration
    onSave(finalView)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
      />

      {/* Modal */}
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="relative w-full max-w-3xl max-h-[85vh] glass-panel rounded-2xl shadow-lg overflow-hidden flex flex-col"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-glass-border">
          <div>
            <h2 className="text-lg font-display font-semibold text-ink">
              {existingView ? 'Edit View' : 'Create New View'}
            </h2>
            <p className="text-sm text-ink-muted">Configure how data is displayed</p>
          </div>
          <button onClick={onClose} className="btn btn-ghost p-2">
            <LucideIcons.X className="w-5 h-5" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-glass-border px-6">
          {[
            { id: 'general', label: 'General', icon: 'Settings' },
            { id: 'entities', label: 'Entities', icon: 'Grid3x3' },
            { id: 'projection', label: 'Projection', icon: 'Layers' },
            { id: 'layers', label: 'Layers', icon: 'LayoutTemplate' },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as typeof activeTab)}
              className={cn(
                "flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                activeTab === tab.id
                  ? "border-accent-lineage text-accent-lineage"
                  : "border-transparent text-ink-muted hover:text-ink"
              )}
            >
              <DynamicIcon name={tab.icon} className="w-4 h-4" />
              {tab.label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 custom-scrollbar">
          <AnimatePresence mode="wait">
            {activeTab === 'general' && (
              <motion.div
                key="general"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                className="space-y-6"
              >
                {/* Name & Description */}
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-ink mb-1.5">View Name</label>
                    <input
                      type="text"
                      value={view.name ?? ''}
                      onChange={(e) => updateView('name', e.target.value)}
                      className="input"
                      placeholder="e.g., Data Lineage, Impact Analysis"
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-ink mb-1.5">Description</label>
                    <textarea
                      value={view.description ?? ''}
                      onChange={(e) => updateView('description', e.target.value)}
                      className="input min-h-[80px]"
                      placeholder="Describe what this view shows..."
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-ink mb-1.5">Icon</label>
                    <input
                      type="text"
                      value={view.icon ?? ''}
                      onChange={(e) => updateView('icon', e.target.value)}
                      className="input"
                      placeholder="Lucide icon name (e.g., Network, Layers)"
                    />
                  </div>
                </div>

                {/* Layout Type */}
                <div>
                  <label className="block text-sm font-medium text-ink mb-3">Layout Type</label>
                  <div className="grid grid-cols-3 gap-3">
                    {LAYOUT_TYPES.map((layout) => (
                      <button
                        key={layout.value}
                        onClick={() => setView((prev) => ({
                          ...prev,
                          layout: { ...prev.layout!, type: layout.value as ViewConfiguration['layout']['type'] },
                        }))}
                        className={cn(
                          "p-4 rounded-xl border-2 text-left transition-colors duration-150",
                          view.layout?.type === layout.value
                            ? "border-accent-lineage bg-accent-lineage/5"
                            : "border-glass-border hover:border-accent-lineage/50"
                        )}
                      >
                        <DynamicIcon name={layout.icon} className="w-6 h-6 mb-2 text-accent-lineage" />
                        <div className="font-medium text-sm">{layout.label}</div>
                        <div className="text-2xs text-ink-muted mt-1">{layout.description}</div>
                      </button>
                    ))}
                  </div>
                </div>
              </motion.div>
            )}

            {activeTab === 'entities' && (
              <motion.div
                key="entities"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                className="space-y-4"
              >
                <p className="text-sm text-ink-muted">
                  Select which entity types are visible in this view.
                </p>

                <div className="bg-blue-500/10 border border-blue-500/20 rounded-lg p-3 text-xs text-ink-muted mb-4">
                  <strong>Note:</strong> Selected entities will be available to all views.
                  However, they must also be assigned to a layer in the "Layers" tab to appear in the Reference Model.
                </div>

                <div className="grid grid-cols-2 gap-3">
                  {schema?.entityTypes.map((entityType) => {
                    const isVisible = view.content?.visibleEntityTypes?.includes(entityType.id) ?? false
                    return (
                      <button
                        key={entityType.id}
                        onClick={() => toggleEntityType(entityType.id)}
                        className={cn(
                          "flex items-center gap-3 p-3 rounded-lg border-2 text-left transition-colors duration-150",
                          isVisible
                            ? "border-accent-lineage bg-accent-lineage/5"
                            : "border-glass-border hover:border-glass-border/80 opacity-50"
                        )}
                      >
                        <div
                          className="w-8 h-8 rounded-lg flex items-center justify-center"
                          style={{ backgroundColor: `${entityType.visual.color}20` }}
                        >
                          <DynamicIcon
                            name={entityType.visual.icon}
                            className="w-4 h-4"
                            style={{ color: entityType.visual.color }}
                          />
                        </div>
                        <div>
                          <div className="font-medium text-sm">{entityType.name}</div>
                          <div className="text-2xs text-ink-muted">{entityType.pluralName}</div>
                        </div>
                        {isVisible && (
                          <LucideIcons.Check className="w-4 h-4 text-accent-lineage ml-auto" />
                        )}
                      </button>
                    )
                  })}
                </div>
              </motion.div>
            )}

            {activeTab === 'projection' && (
              <motion.div
                key="projection"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                className="space-y-6"
              >
                <p className="text-sm text-ink-muted">
                  Configure how data is projected and aggregated in this view.
                </p>

                {/* Target Granularity */}
                <div>
                  <label className="block text-sm font-medium text-ink mb-3">Target Granularity</label>
                  <div className="space-y-2">
                    {GRANULARITY_LEVELS.map((level) => (
                      <button
                        key={level.value}
                        onClick={() => updateProjection('targetGranularity', level.value)}
                        className={cn(
                          "w-full flex items-center gap-3 p-3 rounded-lg border-2 text-left transition-colors duration-150",
                          view.layout?.projection?.targetGranularity === level.value
                            ? "border-accent-lineage bg-accent-lineage/5"
                            : "border-glass-border hover:border-glass-border/80"
                        )}
                      >
                        <div className={cn(
                          "w-8 h-8 rounded-lg flex items-center justify-center text-xs font-bold",
                          view.layout?.projection?.targetGranularity === level.value
                            ? "bg-accent-lineage text-white"
                            : "bg-black/5 dark:bg-white/10 text-ink-muted"
                        )}>
                          L{level.value}
                        </div>
                        <div className="flex-1">
                          <div className="font-medium text-sm">{level.label}</div>
                          <div className="text-2xs text-ink-muted">{level.description}</div>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                {/* Toggle Options */}
                <div className="space-y-3">
                  <ToggleOption
                    label="Aggregate Lineage"
                    description="Roll up column-level lineage to table-level"
                    enabled={view.layout?.projection?.aggregateLineage ?? false}
                    onChange={(v) => updateProjection('aggregateLineage', v)}
                  />

                  <ToggleOption
                    label="Collapse Children"
                    description="Hide child entities and show count badges"
                    enabled={view.layout?.projection?.collapseChildren ?? false}
                    onChange={(v) => updateProjection('collapseChildren', v)}
                  />
                </div>
              </motion.div>
            )}

            {activeTab === 'layers' && (
              <motion.div
                key="layers"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                className="space-y-4"
              >
                <div className="flex items-center justify-between">
                  <p className="text-sm text-ink-muted">
                    Define horizontal layers for Reference Model layout.
                    <br />
                    <span className="text-2xs opacity-70">
                      (Assign entity types to each layer to determine where they appear)
                    </span>
                  </p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setIsBuilderOpen(true)}
                      className="btn btn-secondary btn-sm"
                    >
                      <LucideIcons.LayoutTemplate className="w-4 h-4" />
                      Full Builder
                    </button>
                    <button
                      onClick={() => setIsAssignmentPanelOpen(true)}
                      className="btn btn-secondary btn-sm"
                    >
                      <LucideIcons.Users className="w-4 h-4" />
                      Assign Entities
                    </button>
                    <button onClick={addLayer} className="btn btn-primary btn-sm">
                      <LucideIcons.Plus className="w-4 h-4" />
                      Add Layer
                    </button>
                  </div>
                </div>

                {/* Layer Drop Zones for Quick Assignment */}
                {(view.layout?.referenceLayout?.layers ?? []).length > 0 && (
                  <div className="mt-4">
                    <p className="text-xs text-ink-muted mb-2">Drop entities here to assign:</p>
                    <LayerDropZoneRow
                      layers={view.layout?.referenceLayout?.layers ?? []}
                      entityCounts={entityCounts}
                    />
                  </div>
                )}

                {(view.layout?.referenceLayout?.layers ?? []).length === 0 ? (
                  <div className="text-center py-12 text-ink-muted">
                    <LucideIcons.LayoutTemplate className="w-12 h-12 mx-auto mb-3 opacity-30" />
                    <p>No layers defined yet</p>
                    <p className="text-2xs mt-1">Add layers to create a Reference Model view</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {(view.layout?.referenceLayout?.layers ?? [])
                      .sort((a, b) => a.order - b.order)
                      .map((layer, index) => (
                        <LayerEditor
                          key={layer.id}
                          layer={layer}
                          index={index}
                          entityTypes={schema?.entityTypes ?? []}
                          onUpdate={(updates) => updateLayer(layer.id, updates)}
                          onRemove={() => removeLayer(layer.id)}
                        />
                      ))}
                  </div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-glass-border">
          <button onClick={onClose} className="btn btn-secondary btn-md">
            Cancel
          </button>
          <button onClick={handleSave} className="btn btn-primary btn-md">
            <LucideIcons.Save className="w-4 h-4" />
            Save View
          </button>
        </div>
      </motion.div>

      {/* Entity Assignment Panel */}
      <EntityAssignmentPanel
        isOpen={isAssignmentPanelOpen}
        onClose={() => setIsAssignmentPanelOpen(false)}
      />

      {/* Reference Model Builder */}
      <ReferenceModelBuilder
        isOpen={isBuilderOpen}
        onClose={() => setIsBuilderOpen(false)}
        onSave={(newLayers) => {
          setView(prev => ({
            ...prev,
            layout: {
              ...prev.layout!,
              referenceLayout: {
                ...prev.layout?.referenceLayout,
                layers: newLayers
              }
            }
          }))
        }}
      />
    </div>
  )
}
// Helper to detect rule type
const getRuleType = (rule: LayerAssignmentRuleConfig): 'identifier' | 'name' | 'tag' | 'property' => {
  if (rule.urnPattern) return 'identifier'
  if (rule.propertyMatch?.field === 'name') return 'name'
  if (rule.tags && rule.tags.length > 0) return 'tag'
  return 'property'
}

function RuleListEditor({
  rules,
  onUpdate
}: {
  rules: LayerAssignmentRuleConfig[]
  onUpdate: (rules: LayerAssignmentRuleConfig[]) => void
}) {
  return (
    <div className="space-y-2">
      {rules.map((rule, idx) => {
        const ruleType = getRuleType(rule)

        const updateRule = (newRule: Partial<LayerAssignmentRuleConfig>) => {
          const newRules = [...rules]
          newRules[idx] = { ...rule, ...newRule }
          onUpdate(newRules)
        }

        const changeType = (newType: string) => {
          // Reset fields when changing type
          const base = { id: rule.id, priority: rule.priority }
          if (newType === 'identifier') updateRule({ ...base, urnPattern: '' })
          if (newType === 'name') updateRule({ ...base, propertyMatch: { field: 'name', operator: 'contains', value: '' } })
          if (newType === 'tag') updateRule({ ...base, tags: [''] })
          if (newType === 'property') updateRule({ ...base, propertyMatch: { field: '', operator: 'equals', value: '' } })
        }

        return (
          <div key={rule.id} className="p-3 rounded bg-black/5 dark:bg-white/5 space-y-3">
            <div className="flex items-start gap-2">
              {/* Type Selector */}
              <div className="w-24 flex-shrink-0">
                <select
                  value={ruleType}
                  onChange={(e) => changeType(e.target.value)}
                  className="w-full bg-transparent text-xs font-medium text-ink border-b border-glass-border focus:outline-none py-1"
                >
                  <option value="identifier">Identifier</option>
                  <option value="name">Name</option>
                  <option value="tag">Tag</option>
                  <option value="property">Property</option>
                </select>
              </div>

              {/* Dynamic Inputs */}
              <div className="flex-1 min-w-0">
                {ruleType === 'identifier' && (
                  <input
                    type="text"
                    value={rule.urnPattern ?? ''}
                    onChange={(e) => updateRule({ urnPattern: e.target.value })}
                    className="w-full bg-transparent border-b border-glass-border text-xs px-1 py-1 focus:outline-none focus:border-accent-lineage"
                    placeholder="URN pattern (e.g. *finance*)"
                  />
                )}

                {ruleType === 'name' && (
                  <div className="flex gap-2">
                    <span className="text-xs text-ink-muted py-1">contains</span>
                    <input
                      type="text"
                      value={String(rule.propertyMatch?.value ?? '')}
                      onChange={(e) => updateRule({
                        propertyMatch: { field: 'name', operator: 'contains', value: e.target.value }
                      })}
                      className="flex-1 bg-transparent border-b border-glass-border text-xs px-1 py-1 focus:outline-none focus:border-accent-lineage"
                      placeholder="Name text..."
                    />
                  </div>
                )}

                {ruleType === 'tag' && (
                  <input
                    type="text"
                    value={rule.tags?.[0] ?? ''}
                    onChange={(e) => updateRule({ tags: [e.target.value] })}
                    className="w-full bg-transparent border-b border-glass-border text-xs px-1 py-1 focus:outline-none focus:border-accent-lineage"
                    placeholder="Tag (e.g. source)"
                  />
                )}

                {ruleType === 'property' && (
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={rule.propertyMatch?.field ?? ''}
                      onChange={(e) => updateRule({
                        propertyMatch: { ...(rule.propertyMatch!), field: e.target.value }
                      })}
                      className="w-20 bg-transparent border-b border-glass-border text-xs px-1 py-1 focus:outline-none focus:border-accent-lineage"
                      placeholder="Field"
                    />
                    <select
                      value={rule.propertyMatch?.operator ?? 'equals'}
                      onChange={(e) => updateRule({
                        propertyMatch: { ...(rule.propertyMatch!), operator: e.target.value as any }
                      })}
                      className="w-20 bg-transparent border-b border-glass-border text-xs py-1 focus:outline-none"
                    >
                      <option value="equals">=</option>
                      <option value="contains">contains</option>
                      <option value="startsWith">starts with</option>
                    </select>
                    <input
                      type="text"
                      value={String(rule.propertyMatch?.value ?? '')}
                      onChange={(e) => updateRule({
                        propertyMatch: { ...(rule.propertyMatch!), value: e.target.value }
                      })}
                      className="flex-1 bg-transparent border-b border-glass-border text-xs px-1 py-1 focus:outline-none focus:border-accent-lineage"
                      placeholder="Value"
                    />
                  </div>
                )}
              </div>

              {/* Remove Rule */}
              <button
                onClick={() => {
                  const newRules = rules.filter(r => r.id !== rule.id)
                  onUpdate(newRules)
                }}
                className="text-ink-muted hover:text-red-500 pt-1"
              >
                <LucideIcons.X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}

// Toggle option component
function ToggleOption({
  label,
  description,
  enabled,
  onChange,
}: {
  label: string
  description: string
  enabled: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <button
      onClick={() => onChange(!enabled)}
      className={cn(
        "w-full flex items-center justify-between p-4 rounded-lg border-2 text-left transition-colors duration-150",
        enabled
          ? "border-accent-lineage bg-accent-lineage/5"
          : "border-glass-border hover:border-glass-border/80"
      )}
    >
      <div>
        <div className="font-medium text-sm">{label}</div>
        <div className="text-2xs text-ink-muted">{description}</div>
      </div>
      <div className={cn(
        "w-12 h-6 rounded-full p-1 transition-colors",
        enabled ? "bg-accent-lineage" : "bg-black/10 dark:bg-white/10"
      )}>
        <div className={cn(
          "w-4 h-4 rounded-full bg-white shadow transition-transform",
          enabled ? "translate-x-6" : "translate-x-0"
        )} />
      </div>
    </button>
  )
}

// Logical Node Editor (Recursive)
function LogicalNodeEditor({
  node,
  onUpdate,
  onRemove
}: {
  node: LogicalNodeConfig
  onUpdate: (updates: Partial<LogicalNodeConfig>) => void
  onRemove: () => void
}) {
  const [isExpanded, setIsExpanded] = useState(false)

  const updateChild = (childId: string, updates: Partial<LogicalNodeConfig>) => {
    const newChildren = (node.children ?? []).map(c =>
      c.id === childId ? { ...c, ...updates } : c
    )
    onUpdate({ children: newChildren })
  }

  const removeChild = (childId: string) => {
    const newChildren = (node.children ?? []).filter(c => c.id !== childId)
    onUpdate({ children: newChildren })
  }

  const addChild = () => {
    const newChild: LogicalNodeConfig = {
      id: `group-${Date.now()}`,
      name: 'New Group',
      type: 'group',
      children: [],
      rules: []
    }
    onUpdate({ children: [...(node.children ?? []), newChild] })
  }

  return (
    <div className="border border-glass-border rounded-lg overflow-hidden bg-black/5 dark:bg-white/5">
      {/* Header */}
      <div
        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-black/5 dark:hover:bg-white/5 transition-colors"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <LucideIcons.ChevronRight className={cn("w-4 h-4 text-ink-muted transition-transform", isExpanded && "rotate-90")} />
        <DynamicIcon name={generateIconFallback(node.type || 'unknown')} className="w-4 h-4 text-accent-lineage" />
        <input
          type="text"
          value={node.name}
          onChange={(e) => {
            e.stopPropagation()
            onUpdate({ name: e.target.value })
          }}
          onClick={(e) => e.stopPropagation()}
          className="bg-transparent text-sm font-medium focus:outline-none flex-1 min-w-0"
        />
        <button
          onClick={(e) => {
            e.stopPropagation()
            onRemove()
          }}
          className="text-ink-muted hover:text-red-500 p-1"
        >
          <LucideIcons.Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 'auto' }}
            exit={{ height: 0 }}
            className="overflow-hidden"
          >
            <div className="p-3 space-y-4 border-t border-glass-border">
              {/* Type & Description */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-ink-muted mb-1">Type</label>
                  <select
                    value={node.type}
                    onChange={(e) => onUpdate({ type: e.target.value as any })}
                    className="w-full bg-transparent border-b border-glass-border text-xs py-1 focus:outline-none"
                  >
                    <option value="container">Container</option>
                    <option value="group">Group</option>
                    <option value="system">System</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-ink-muted mb-1">Description</label>
                  <input
                    type="text"
                    value={node.description ?? ''}
                    onChange={(e) => onUpdate({ description: e.target.value })}
                    className="w-full bg-transparent border-b border-glass-border text-xs py-1 focus:outline-none"
                    placeholder="Optional description"
                  />
                </div>
              </div>

              {/* Rules */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-medium text-ink-muted">Mapping Rules</label>
                  <button
                    onClick={() => onUpdate({
                      rules: [...(node.rules ?? []), { id: `rule-${Date.now()}`, priority: 10 }]
                    })}
                    className="text-xs text-accent-lineage hover:underline"
                  >
                    + Add Rule
                  </button>
                </div>
                {(!node.rules || node.rules.length === 0) ? (
                  <p className="text-xs text-ink-muted italic">No rules (drag physical entities here or add rules)</p>
                ) : (
                  <RuleListEditor
                    rules={node.rules}
                    onUpdate={(rules) => onUpdate({ rules })}
                  />
                )}
              </div>

              {/* Children */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-medium text-ink-muted">Nested Groups</label>
                  <button
                    onClick={addChild}
                    className="text-xs text-accent-lineage hover:underline"
                  >
                    + Add Group
                  </button>
                </div>
                <div className="space-y-2 pl-2 border-l border-glass-border">
                  {(node.children ?? []).map(child => (
                    <LogicalNodeEditor
                      key={child.id}
                      node={child}
                      onUpdate={(u) => updateChild(child.id, u)}
                      onRemove={() => removeChild(child.id)}
                    />
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// Layer editor component
function LayerEditor({
  layer,
  index,
  entityTypes,
  onUpdate,
  onRemove,
}: {
  layer: ViewLayerConfig
  index: number
  entityTypes: EntityTypeSchema[]
  onUpdate: (updates: Partial<ViewLayerConfig>) => void
  onRemove: () => void
}) {
  const [isExpanded, setIsExpanded] = useState(false)



  return (
    <div className="rounded-lg border border-glass-border overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center gap-3 px-4 py-3 bg-canvas-elevated cursor-pointer"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <div
          className="w-3 h-8 rounded"
          style={{ backgroundColor: layer.color }}
        />
        <div className="flex-1">
          <input
            type="text"
            value={layer.name}
            onChange={(e) => {
              e.stopPropagation()
              onUpdate({ name: e.target.value })
            }}
            onClick={(e) => e.stopPropagation()}
            className="bg-transparent font-medium text-sm focus:outline-none"
          />
          <div className="text-2xs text-ink-muted">
            {layer.entityTypes.length} entity types · Order: {index + 1}
          </div>
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation()
            onRemove()
          }}
          className="p-1.5 text-ink-muted hover:text-red-500 transition-colors"
        >
          <LucideIcons.Trash2 className="w-4 h-4" />
        </button>
        <LucideIcons.ChevronDown
          className={cn(
            "w-4 h-4 text-ink-muted transition-transform",
            isExpanded && "rotate-180"
          )}
        />
      </div>

      {/* Expanded Content */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="p-4 space-y-4 border-t border-glass-border">
              {/* Color Picker */}
              <div>
                <label className="block text-xs font-medium text-ink-muted mb-1.5">Color</label>
                <input
                  type="color"
                  value={layer.color}
                  onChange={(e) => onUpdate({ color: e.target.value })}
                  className="w-full h-8 rounded cursor-pointer"
                />
              </div>

              {/* Description */}
              <div>
                <label className="block text-xs font-medium text-ink-muted mb-1.5">Description</label>
                <input
                  type="text"
                  value={layer.description ?? ''}
                  onChange={(e) => onUpdate({ description: e.target.value })}
                  className="input text-sm"
                  placeholder="e.g., Raw data sources"
                />
              </div>

              {/* Entity Types */}
              <div>
                <label className="block text-xs font-medium text-ink-muted mb-2">Entity Types in this Layer</label>
                <div className="flex flex-wrap gap-2">
                  {entityTypes.map((et) => {
                    const isInLayer = layer.entityTypes.includes(et.id)
                    return (
                      <button
                        key={et.id}
                        onClick={() => {
                          const updated = isInLayer
                            ? layer.entityTypes.filter((id) => id !== et.id)
                            : [...layer.entityTypes, et.id]
                          onUpdate({ entityTypes: updated })
                        }}
                        className={cn(
                          "px-2 py-1 rounded-md text-xs font-medium transition-colors duration-150",
                          isInLayer
                            ? "text-white"
                            : "bg-black/5 dark:bg-white/10 text-ink-muted hover:text-ink"
                        )}
                        style={isInLayer ? { backgroundColor: layer.color } : undefined}
                      >
                        {et.name}
                      </button>
                    )
                  })}
                </div>
              </div>


              {/* Assignment Rules */}
              <div className="pt-3 border-t border-glass-border">
                <div className="flex items-center justify-between mb-2">
                  <label className="block text-xs font-medium text-ink-muted">Assignment Rules (Legacy)</label>
                  <button
                    onClick={() => {
                      const rules = layer.rules ?? []
                      onUpdate({
                        rules: [
                          ...rules,
                          {
                            id: `rule-${Date.now()}`,
                            priority: 10,
                            urnPattern: '' // Default to identifier
                          }
                        ]
                      })
                    }}
                    className="text-xs text-accent-lineage hover:underline"
                  >
                    + Add Rule
                  </button>
                </div>

                <div className="space-y-2">
                  <RuleListEditor
                    rules={layer.rules ?? []}
                    onUpdate={(rules) => onUpdate({ rules })}
                  />
                  {(layer.rules ?? []).length === 0 && (
                    <p className="text-2xs text-ink-muted italic">
                      No rules defined. Entities will be assigned based on type only.
                    </p>
                  )}
                </div>
              </div>

              {/* Logical Hierarchy (New) */}
              <div className="pt-3 border-t border-glass-border">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <label className="text-xs font-medium text-ink-muted">Logical Hierarchy</label>
                    <span className="px-1.5 py-0.5 rounded text-2xs bg-accent-lineage/10 text-accent-lineage font-medium">New</span>
                  </div>
                  <button
                    onClick={() => {
                      const newRoot: LogicalNodeConfig = {
                        id: `group-${Date.now()}`,
                        name: 'New Group',
                        type: 'container',
                        rules: [],
                        children: []
                      }
                      onUpdate({ logicalNodes: [...(layer.logicalNodes ?? []), newRoot] })
                    }}
                    className="text-xs text-accent-lineage hover:underline"
                  >
                    + Add Root Group
                  </button>
                </div>

                <div className="space-y-2">
                  {(layer.logicalNodes ?? []).length === 0 ? (
                    <p className="text-2xs text-ink-muted italic">
                      Define logical containers (e.g. Domains, Platforms) and map entities to them.
                    </p>
                  ) : (
                    (layer.logicalNodes ?? []).map((node, idx) => (
                      <LogicalNodeEditor
                        key={node.id}
                        node={node}
                        onUpdate={(u) => {
                          const newNodes = [...(layer.logicalNodes ?? [])]
                          newNodes[idx] = { ...node, ...u }
                          onUpdate({ logicalNodes: newNodes })
                        }}
                        onRemove={() => {
                          const newNodes = (layer.logicalNodes ?? []).filter(n => n.id !== node.id)
                          onUpdate({ logicalNodes: newNodes })
                        }}
                      />
                    ))
                  )}
                </div>

                <div className="mt-3 flex items-center gap-2">
                  <input
                    type="checkbox"
                    id={`unassigned-${layer.id}`}
                    checked={layer.showUnassigned !== false}
                    onChange={(e) => onUpdate({ showUnassigned: e.target.checked })}
                    className="rounded border-glass-border text-accent-lineage focus:ring-accent-lineage"
                  />
                  <label htmlFor={`unassigned-${layer.id}`} className="text-xs text-ink-muted cursor-pointer select-none">
                    Show unassigned entities matching layer types
                  </label>
                </div>
              </div>

            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default ViewEditor

