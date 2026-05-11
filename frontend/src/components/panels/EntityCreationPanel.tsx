/**
 * EntityCreationPanel - Ontology-aware entity creation form
 * 
 * Provides a dynamic form for creating new entities with:
 * - Entity type selection filtered by ontology rules
 * - Dynamic field generation based on entity type schema
 * - Parent container selection with hierarchy validation
 * - Automatic containment edge creation
 */

import React, { useState, useMemo, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import {
    useEntityTypes,
    useContainmentEdgeTypes,
    useRootEntityTypes,
    useEntityTypeHierarchyMap,
} from '@/store/schema'
import { useCanvasStore } from '@/store/canvas'
import { useGraphProvider } from '@/providers/GraphProviderContext'
import { useStagedChangesStore } from '@/store/stagedChangesStore'
import { generateId } from '@/lib/utils'
import type { EntityTypeSchema } from '@/types/schema'

// Dynamic icon component
function DynamicIcon({ name, className, style }: { name: string; className?: string; style?: React.CSSProperties }) {
    const IconComponent = (LucideIcons as unknown as Record<string, React.ComponentType<{ className?: string; style?: React.CSSProperties }>>)[name]
    if (!IconComponent) {
        return <LucideIcons.Box className={className} style={style} />
    }
    return <IconComponent className={className} style={style} />
}

interface EntityCreationPanelProps {
    isOpen: boolean
    onClose: () => void
    /** Pre-selected parent for nested creation */
    parentId?: string | null
    /** Pre-selected layer for layer-based creation */
    layerId?: string | null
    /** Callback when entity is created successfully */
    onEntityCreated?: (nodeId: string, parentId?: string) => void
}

interface FormData {
    entityType: string
    displayName: string
    description: string
    parentUrn: string
    tags: string
    properties: Record<string, unknown>
}

export function EntityCreationPanel({
    isOpen,
    onClose,
    parentId,
    layerId,
    onEntityCreated,
}: EntityCreationPanelProps) {
    const entityTypes = useEntityTypes()
    const nodes = useCanvasStore((s) => s.nodes)
    const addNodes = useCanvasStore((s) => s.addNodes)
    const addEdges = useCanvasStore((s) => s.addEdges)
    const removeNode = useCanvasStore((s) => s.removeNode)
    const removeEdge = useCanvasStore((s) => s.removeEdge)
    const provider = useGraphProvider()
    const containmentEdgeTypes = useContainmentEdgeTypes()
    const rootEntityTypes = useRootEntityTypes()
    const entityTypeHierarchy = useEntityTypeHierarchyMap()
    const stageChange = useStagedChangesStore((s) => s.stage)

    // Form state
    const [formData, setFormData] = useState<FormData>({
        entityType: '',
        displayName: '',
        description: '',
        parentUrn: parentId || '',
        tags: '',
        properties: {},
    })
    const [isSubmitting, setIsSubmitting] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [successMessage, setSuccessMessage] = useState<string | null>(null)

    // Get parent node info
    const parentNode = useMemo(() => {
        if (!formData.parentUrn) return null
        return nodes.find(n => n.id === formData.parentUrn || (n.data?.urn as string) === formData.parentUrn)
    }, [nodes, formData.parentUrn])

    // Filter entity types based on parent selection (ontology rules)
    const availableEntityTypes = useMemo(() => {
        if (!parentNode) {
            // No parent selected — show root-level entity types as defined by the ontology.
            // Use canBeContainedBy === [] as the canonical definition of a root type.
            if (rootEntityTypes.length > 0) {
                return entityTypes.filter(et => rootEntityTypes.includes(et.id))
            }
            // Ontology not yet loaded — show types the ontology says can't be contained.
            return entityTypes.filter(et => et.hierarchy.canBeContainedBy.length === 0)
        }

        // Get parent entity type
        const parentType = parentNode.data?.type as string
        if (!parentType) return entityTypes

        // Get what this parent can contain from ontology
        const parentSchema = entityTypes.find(et => et.id === parentType)
        const canContain = parentSchema?.hierarchy.canContain || []

        // Also check ontology metadata
        const ontologyCanContain = entityTypeHierarchy[parentType]?.canContain || []
        const allCanContain = [...new Set([...canContain, ...ontologyCanContain])]

        if (allCanContain.length === 0) {
            // No restrictions, show all
            return entityTypes
        }

        return entityTypes.filter(et => allCanContain.includes(et.id))
    }, [entityTypes, parentNode, rootEntityTypes, entityTypeHierarchy])

    // Get potential parent containers from existing nodes
    const potentialParents = useMemo(() => {
        return nodes.filter(n => {
            const nodeType = n.data?.type as string
            const schema = entityTypes.find(et => et.id === nodeType)
            // Can be a parent if it can contain something
            return schema?.hierarchy.canContain && schema.hierarchy.canContain.length > 0
        }).map(n => ({
            id: n.id,
            urn: (n.data?.urn as string) || n.id,
            name: (n.data?.label as string) || (n.data?.displayName as string) || n.id,
            type: (n.data?.type as string) || 'unknown',
        }))
    }, [nodes, entityTypes])

    // Selected entity type schema
    const selectedEntityType = useMemo(() => {
        return entityTypes.find(et => et.id === formData.entityType)
    }, [entityTypes, formData.entityType])

    // Handle form field changes
    const handleFieldChange = useCallback((field: keyof FormData, value: string) => {
        setFormData(prev => ({
            ...prev,
            [field]: value,
        }))
        setError(null)
        setSuccessMessage(null)
    }, [])

    // Handle property field changes
    const handlePropertyChange = useCallback((fieldId: string, value: unknown) => {
        setFormData(prev => ({
            ...prev,
            properties: {
                ...prev.properties,
                [fieldId]: value,
            },
        }))
    }, [])

    // Reset form
    const resetForm = useCallback(() => {
        setFormData({
            entityType: '',
            displayName: '',
            description: '',
            parentUrn: parentId || '',
            tags: '',
            properties: {},
        })
        setError(null)
        setSuccessMessage(null)
    }, [parentId])

    // Submit form
    const handleSubmit = useCallback(async (e: React.FormEvent) => {
        e.preventDefault()

        if (!formData.entityType) {
            setError('Please select an entity type')
            return
        }

        if (!formData.displayName.trim()) {
            setError('Please enter a display name')
            return
        }

        setIsSubmitting(true)
        setError(null)

        try {
            // Parse tags
            const tags = formData.tags
                .split(',')
                .map(t => t.trim())
                .filter(t => t.length > 0)

            // Build properties
            const properties = {
                ...formData.properties,
                description: formData.description,
            }

            // STAGE the creation. The entity is added to the canvas immediately
            // with isPending='create' so the user sees it; the actual backend
            // call happens when they click Save.
            const tempUrn = `urn:staged:${formData.entityType}:${generateId('new')}`
            const containmentEdgeId = formData.parentUrn
                ? `contains-${formData.parentUrn}-${tempUrn}`
                : null
            const containmentEdgeType = containmentEdgeTypes[0] ?? 'CONTAINS'

            addNodes([{
                id: tempUrn,
                type: 'generic',
                position: { x: 0, y: 0 },
                data: {
                    label: formData.displayName,
                    type: formData.entityType,
                    urn: tempUrn,
                    classifications: tags,
                    isPending: 'create',
                    metadata: properties,
                },
            }])

            if (containmentEdgeId && formData.parentUrn) {
                addEdges([{
                    id: containmentEdgeId,
                    source: formData.parentUrn,
                    target: tempUrn,
                    type: 'containment',
                    data: {
                        edgeType: containmentEdgeType,
                        relationship: containmentEdgeType.toLowerCase(),
                    },
                }])
            }

            const snapshot = { ...formData, tags, properties }

            stageChange({
                type: 'create_entity',
                targetId: tempUrn,
                targetUrn: tempUrn,
                after: snapshot,
                summary: `Create ${formData.entityType}: '${formData.displayName}'`,
                apply: async ({ provider: p, registerTempIdResolution }) => {
                    if (!p) {
                        // No provider — accept the local-only creation by clearing the pending flag.
                        useCanvasStore.getState().updateNode(tempUrn, { isPending: undefined })
                        return
                    }
                    const result = await p.createNode({
                        entityType: snapshot.entityType as any,
                        displayName: snapshot.displayName,
                        parentUrn: snapshot.parentUrn || undefined,
                        properties: snapshot.properties,
                        tags: snapshot.tags,
                    })
                    if (!result.success || !result.node) {
                        throw new Error(result.error || 'Failed to create entity')
                    }
                    registerTempIdResolution(tempUrn, result.node.urn)
                    // Replace temp node with backend-issued one (URN may differ).
                    useCanvasStore.getState().removeNode(tempUrn)
                    if (containmentEdgeId) useCanvasStore.getState().removeEdge(containmentEdgeId)
                    useCanvasStore.getState().addNodes([{
                        id: result.node.urn,
                        type: 'generic',
                        position: { x: 0, y: 0 },
                        data: {
                            label: result.node.displayName,
                            type: result.node.entityType,
                            urn: result.node.urn,
                            classifications: result.node.tags,
                            metadata: result.node.properties,
                        },
                    }])
                    if (result.containmentEdge) {
                        useCanvasStore.getState().addEdges([{
                            id: result.containmentEdge.id,
                            source: result.containmentEdge.sourceUrn,
                            target: result.containmentEdge.targetUrn,
                            type: 'containment',
                            data: {
                                edgeType: result.containmentEdge.edgeType,
                                relationship: 'contains',
                            },
                        }])
                    }
                },
                discard: () => {
                    if (containmentEdgeId) removeEdge(containmentEdgeId)
                    removeNode(tempUrn)
                },
            })

            setSuccessMessage(`Staged: '${formData.displayName}' — click Save to commit`)
            onEntityCreated?.(tempUrn, formData.parentUrn || undefined)

            setTimeout(() => {
                setFormData(prev => ({
                    ...prev,
                    displayName: '',
                    description: '',
                    tags: '',
                    properties: {},
                }))
                setSuccessMessage(null)
            }, 1500)
        } catch (err) {
            setError(err instanceof Error ? err.message : 'An error occurred')
        } finally {
            setIsSubmitting(false)
        }
    }, [formData, provider, addNodes, addEdges, removeNode, removeEdge, containmentEdgeTypes, stageChange, onEntityCreated])

    if (!isOpen) return null

    return (
        <AnimatePresence>
            <motion.div
                initial={{ opacity: 0, x: 400 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 400 }}
                className="fixed right-0 top-0 bottom-0 w-[400px] z-50 glass-panel border-l border-glass-border shadow-lg flex flex-col"
            >
                {/* Header */}
                <div className="flex-shrink-0 px-5 py-4 border-b border-glass-border bg-canvas-elevated/95">
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                            <div className="w-9 h-9 rounded-lg bg-green-500/10 flex items-center justify-center">
                                <LucideIcons.Plus className="w-5 h-5 text-green-500" />
                            </div>
                            <div>
                                <h3 className="text-base font-semibold text-ink">Create Entity</h3>
                                <p className="text-xs text-ink-muted">Add a new entity to the graph</p>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/10 transition-colors"
                        >
                            <LucideIcons.X className="w-5 h-5 text-ink-muted" />
                        </button>
                    </div>
                </div>

                {/* Form Content */}
                <form onSubmit={handleSubmit} className="flex-1 overflow-y-auto p-5 space-y-5">
                    {/* Error/Success Messages */}
                    <AnimatePresence>
                        {error && (
                            <motion.div
                                key="error-message"
                                initial={{ opacity: 0, y: -10 }}
                                animate={{ opacity: 1, y: 0 }}
                                exit={{ opacity: 0, y: -10 }}
                                className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-600 dark:text-red-400 text-sm flex items-center gap-2"
                            >
                                <LucideIcons.AlertCircle className="w-4 h-4 flex-shrink-0" />
                                {error}
                            </motion.div>
                        )}
                        {successMessage && (
                            <motion.div
                                key="success-message"
                                initial={{ opacity: 0, y: -10 }}
                                animate={{ opacity: 1, y: 0 }}
                                exit={{ opacity: 0, y: -10 }}
                                className="p-3 rounded-lg bg-green-500/10 border border-green-500/20 text-green-600 dark:text-green-400 text-sm flex items-center gap-2"
                            >
                                <LucideIcons.CheckCircle className="w-4 h-4 flex-shrink-0" />
                                {successMessage}
                            </motion.div>
                        )}
                    </AnimatePresence>

                    {/* Parent Container Selection */}
                    <div className="space-y-2">
                        <label className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                            Parent Container (Optional)
                        </label>
                        <select
                            value={formData.parentUrn}
                            onChange={(e) => handleFieldChange('parentUrn', e.target.value)}
                            className="input w-full"
                        >
                            <option value="">— No Parent (Root Level) —</option>
                            {potentialParents.map(p => (
                                <option key={p.urn} value={p.urn}>
                                    {p.name} ({p.type})
                                </option>
                            ))}
                        </select>
                        {parentNode && (
                            <p className="text-xs text-ink-muted flex items-center gap-1">
                                <LucideIcons.Info className="w-3 h-3" />
                                New entity will be contained within this parent
                            </p>
                        )}
                    </div>

                    {/* Entity Type Selection */}
                    <div className="space-y-2">
                        <label className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                            Entity Type <span className="text-red-400">*</span>
                        </label>
                        <div className="grid grid-cols-2 gap-2">
                            {availableEntityTypes.map(et => (
                                <button
                                    key={et.id}
                                    type="button"
                                    onClick={() => handleFieldChange('entityType', et.id)}
                                    className={cn(
                                        "p-3 rounded-lg border-2 transition-colors duration-150 text-left",
                                        formData.entityType === et.id
                                            ? "border-accent-primary bg-accent-primary/5"
                                            : "border-glass-border hover:border-ink-muted/50"
                                    )}
                                >
                                    <div className="flex items-center gap-2">
                                        <div
                                            className="w-6 h-6 rounded flex items-center justify-center"
                                            style={{ backgroundColor: `${et.visual.color}20` }}
                                        >
                                            <DynamicIcon
                                                name={et.visual.icon}
                                                className="w-3.5 h-3.5"
                                                style={{ color: et.visual.color }}
                                            />
                                        </div>
                                        <span className="text-sm font-medium text-ink truncate">{et.name}</span>
                                    </div>
                                </button>
                            ))}
                        </div>
                        {availableEntityTypes.length === 0 && (
                            <p className="text-xs text-amber-500 flex items-center gap-1">
                                <LucideIcons.AlertTriangle className="w-3 h-3" />
                                No entity types available for this parent
                            </p>
                        )}
                    </div>

                    {/* Display Name */}
                    <div className="space-y-2">
                        <label className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                            Display Name <span className="text-red-400">*</span>
                        </label>
                        <input
                            type="text"
                            value={formData.displayName}
                            onChange={(e) => handleFieldChange('displayName', e.target.value)}
                            placeholder="Enter display name..."
                            className="input w-full"
                            autoFocus
                        />
                    </div>

                    {/* Description */}
                    <div className="space-y-2">
                        <label className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                            Description
                        </label>
                        <textarea
                            value={formData.description}
                            onChange={(e) => handleFieldChange('description', e.target.value)}
                            placeholder="Enter description..."
                            rows={3}
                            className="input w-full resize-none"
                        />
                    </div>

                    {/* Tags */}
                    <div className="space-y-2">
                        <label className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                            Tags
                        </label>
                        <input
                            type="text"
                            value={formData.tags}
                            onChange={(e) => handleFieldChange('tags', e.target.value)}
                            placeholder="tag1, tag2, tag3..."
                            className="input w-full"
                        />
                        <p className="text-xs text-ink-muted">Separate multiple tags with commas</p>
                    </div>

                    {/* Dynamic Fields from Entity Type Schema */}
                    {selectedEntityType && selectedEntityType.fields.filter(f => f.showInPanel && !['name', 'description'].includes(f.id)).length > 0 && (
                        <div className="space-y-3">
                            <div className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                                Additional Fields
                            </div>
                            {selectedEntityType.fields
                                .filter(f => f.showInPanel && !['name', 'description', 'urn'].includes(f.id))
                                .sort((a, b) => a.displayOrder - b.displayOrder)
                                .map(field => (
                                    <div key={field.id} className="space-y-1">
                                        <label className="text-xs text-ink-muted">
                                            {field.name}
                                            {field.required && <span className="text-red-400 ml-1">*</span>}
                                        </label>
                                        {field.type === 'boolean' ? (
                                            <label className="flex items-center gap-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={!!formData.properties[field.id]}
                                                    onChange={(e) => handlePropertyChange(field.id, e.target.checked)}
                                                    className="rounded"
                                                />
                                                <span className="text-sm text-ink">{field.name}</span>
                                            </label>
                                        ) : field.type === 'number' ? (
                                            <input
                                                type="number"
                                                value={(formData.properties[field.id] as number) || ''}
                                                onChange={(e) => handlePropertyChange(field.id, parseFloat(e.target.value) || 0)}
                                                className="input w-full"
                                            />
                                        ) : (
                                            <input
                                                type="text"
                                                value={(formData.properties[field.id] as string) || ''}
                                                onChange={(e) => handlePropertyChange(field.id, e.target.value)}
                                                className="input w-full"
                                            />
                                        )}
                                    </div>
                                ))}
                        </div>
                    )}
                </form>

                {/* Footer Actions */}
                <div className="flex-shrink-0 px-5 py-4 border-t border-glass-border bg-canvas-elevated/95 flex items-center justify-between gap-3">
                    <button
                        type="button"
                        onClick={resetForm}
                        className="px-4 py-2 text-sm font-medium text-ink-muted hover:text-ink transition-colors"
                    >
                        Reset
                    </button>
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={onClose}
                            className="px-4 py-2 rounded-lg text-sm font-medium bg-black/5 dark:bg-white/10 text-ink hover:bg-black/10 dark:hover:bg-white/20 transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            onClick={handleSubmit}
                            disabled={isSubmitting || !formData.entityType || !formData.displayName.trim()}
                            className={cn(
                                "px-4 py-2 rounded-lg text-sm font-semibold transition-colors duration-150 flex items-center gap-2",
                                isSubmitting || !formData.entityType || !formData.displayName.trim()
                                    ? "bg-gray-300 dark:bg-gray-700 text-gray-500 cursor-not-allowed"
                                    : "bg-green-500 text-white hover:bg-green-600 shadow-sm hover:shadow-md"
                            )}
                        >
                            {isSubmitting ? (
                                <>
                                    <LucideIcons.Loader2 className="w-4 h-4 animate-spin" />
                                    Creating...
                                </>
                            ) : (
                                <>
                                    <LucideIcons.Plus className="w-4 h-4" />
                                    Create Entity
                                </>
                            )}
                        </button>
                    </div>
                </div>
            </motion.div>
        </AnimatePresence>
    )
}

export default EntityCreationPanel

