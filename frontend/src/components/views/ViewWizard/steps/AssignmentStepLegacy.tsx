/**
 * AssignmentStepLegacy - Assignment step for non-reference layout views.
 * Two-panel layout: WizardAssignmentTree (left) + LayerManager (right).
 *
 * All hooks are called unconditionally — no early returns, no rules-of-hooks violations.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { WizardAssignmentTree } from '../WizardAssignmentTree'
import { LayerManager } from '../../LayerManager'
import { ChildReassignConfirmDialog, type ChildReassignInfo } from '../../../dialogs/ChildReassignConfirmDialog'

import { useReferenceModelStore } from '@/store/referenceModelStore'
import { useCanvasStore } from '@/store/canvas'
import { useContainmentEdgeTypes, normalizeEdgeType, isContainmentEdgeType } from '@/store/schema'
import type { AssignmentStepProps } from './AssignmentStep'
import type { LogicalNodeConfig, LayerAssignmentRuleConfig } from '@/types/schema'

export function AssignmentStepLegacy({ formData, updateFormData }: AssignmentStepProps) {
    // NOTE: We intentionally do NOT sync formData.layers to the store here.
    // The wizard buffers all changes locally in formData and only commits
    // to the store on final submit (in ViewWizard.handleSubmit). Syncing
    // during the wizard causes premature background rendering in ContextViewCanvas.

    // Build containment parent map from canvas edges + API-driven browser data
    const canvasEdges = useCanvasStore(s => s.edges)
    const canvasNodes = useCanvasStore(s => s.nodes)
    const containmentEdgeTypes = useContainmentEdgeTypes()
    const storeParentMap = useReferenceModelStore(s => s.parentMap)
    const storeEffectiveAssignments = useReferenceModelStore(s => s.effectiveAssignments)

    // The Entity Browser's API-sourced containment data (from useEntityBrowser hook)
    const [browserParentMap, setBrowserParentMap] = useState(new Map<string, string>())

    const parentMap = useMemo(() => {
        const map = new Map<string, string>()
        // Canvas edges (may have data from hydration)
        canvasEdges.forEach(edge => {
            if (isContainmentEdgeType(normalizeEdgeType(edge), containmentEdgeTypes)) {
                map.set(edge.target, edge.source)
            }
        })
        // Store parent map as fallback
        if (map.size === 0) {
            storeParentMap.forEach((parent, child) => map.set(child, parent))
        }
        // Browser's API-sourced containment data takes precedence
        browserParentMap.forEach((parent, child) => map.set(child, parent))
        return map
    }, [canvasEdges, containmentEdgeTypes, storeParentMap, browserParentMap])

    // Layer assignment lookup: wizard formData.layers > store effectiveAssignments
    const layerAssignmentMap = useMemo(() => {
        const map = new Map<string, string>()
        storeEffectiveAssignments.forEach((a, entityId) => map.set(entityId, a.layerId))
        ;(formData.layers ?? []).forEach(layer => {
            layer.entityAssignments?.forEach(a => map.set(a.entityId, layer.id))
        })
        return map
    }, [storeEffectiveAssignments, formData.layers])

    // Build reverse child map from parentMap for DOWN checks
    const childMap = useMemo(() => {
        const map = new Map<string, string[]>()
        parentMap.forEach((pId, cId) => {
            const list = map.get(pId) ?? []
            list.push(cId)
            map.set(pId, list)
        })
        return map
    }, [parentMap])

    const nodeNameMap = useMemo(() => {
        const map = new Map<string, string>()
        canvasNodes.forEach(n => {
            map.set(n.id, (n.data as { label?: string; businessLabel?: string }).label
                ?? (n.data as { businessLabel?: string }).businessLabel ?? n.id)
        })
        return map
    }, [canvasNodes])

    /** Get all containment descendants currently assigned to a different layer */
    const getDescendantsInDifferentLayer = useCallback((entityId: string, targetLayerId: string): string[] => {
        const result: string[] = []
        const queue = [...(childMap.get(entityId) ?? [])]
        const visited = new Set<string>()
        while (queue.length > 0) {
            const cId = queue.shift()!
            if (visited.has(cId)) continue
            visited.add(cId)
            const currentLayer = layerAssignmentMap.get(cId)
            if (currentLayer && currentLayer !== targetLayerId) {
                result.push(cId)
            }
            queue.push(...(childMap.get(cId) ?? []))
        }
        return result
    }, [childMap, layerAssignmentMap])

    const [assignmentWarning, setAssignmentWarning] = useState<string | null>(null)
    const warningTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

    const showAssignmentWarning = useCallback((message: string) => {
        setAssignmentWarning(message)
        if (warningTimerRef.current) clearTimeout(warningTimerRef.current)
        warningTimerRef.current = setTimeout(() => setAssignmentWarning(null), 5000)
    }, [])

    const [pendingReassign, setPendingReassign] = useState<{
        info: ChildReassignInfo; commit: () => void
    } | null>(null)

    /** Commit entity + descendants to a layer (local formData only) */
    const commitAssignment = useCallback((entityIds: string[], descendantsToMove: string[], layerId: string, logicalNodeId?: string | null) => {
        if (!formData.layers) return
        const allMovedIds = new Set([...entityIds, ...descendantsToMove])
        const updatedLayers = formData.layers.map(layer => {
            const filtered = (layer.entityAssignments || []).filter(a => !allMovedIds.has(a.entityId))
            if (layer.id === layerId) {
                return {
                    ...layer,
                    entityAssignments: [
                        ...filtered,
                        ...entityIds.map(id => ({ entityId: id, layerId: layer.id, logicalNodeId: logicalNodeId ?? undefined, inheritsChildren: true, priority: 1000, assignedBy: 'user' as const, assignedAt: new Date().toISOString() })),
                        ...descendantsToMove.map(dId => ({ entityId: dId, layerId: layer.id, logicalNodeId: logicalNodeId ?? undefined, inheritsChildren: true, priority: 999, assignedBy: 'rule' as const, assignedAt: new Date().toISOString() })),
                    ],
                }
            }
            return { ...layer, entityAssignments: filtered }
        })
        updateFormData({ layers: updatedLayers })
    }, [formData.layers, updateFormData])

    /** Show confirmation dialog if descendants need moving, otherwise commit immediately */
    const confirmOrCommit = useCallback((entityIds: string[], descendantsToMove: string[], layerId: string, logicalNodeId?: string | null) => {
        if (descendantsToMove.length === 0) {
            commitAssignment(entityIds, [], layerId, logicalNodeId)
            return
        }
        const info: ChildReassignInfo = {
            entityId: entityIds[0],
            entityName: entityIds.length === 1 ? (nodeNameMap.get(entityIds[0]) ?? entityIds[0]) : `${entityIds.length} entities`,
            targetLayerId: layerId,
            descendantsToMove: descendantsToMove.map(dId => ({
                id: dId, name: nodeNameMap.get(dId) ?? dId, currentLayerId: layerAssignmentMap.get(dId) ?? '',
            })),
        }
        setPendingReassign({
            info,
            commit: () => { commitAssignment(entityIds, descendantsToMove, layerId, logicalNodeId); setPendingReassign(null) },
        })
    }, [commitAssignment, nodeNameMap, layerAssignmentMap])

    const handleAssignmentChange = useCallback((entityId: string, layerId: string | null, logicalNodeId?: string | null) => {
        if (!formData.layers) return

        if (layerId) {
            const parentId = parentMap.get(entityId)
            if (parentId) {
                const parentLayerId = layerAssignmentMap.get(parentId)
                if (parentLayerId && parentLayerId !== layerId) {
                    showAssignmentWarning('Cannot assign child to a different layer than its parent.')
                    return
                }
            }
        }

        const descendantsToMove = layerId ? getDescendantsInDifferentLayer(entityId, layerId) : []
        if (!layerId) {
            commitAssignment([entityId], [], '')
        } else {
            confirmOrCommit([entityId], descendantsToMove, layerId, logicalNodeId)
        }
    }, [formData.layers, parentMap, layerAssignmentMap, getDescendantsInDifferentLayer, showAssignmentWarning, confirmOrCommit, commitAssignment])

    const handleBulkAssignment = useCallback((layerId: string, entityIds: string[], logicalNodeId?: string | null) => {
        if (!formData.layers) return

        const allowed = entityIds.filter(id => {
            const parentId = parentMap.get(id)
            if (!parentId) return true
            const parentLayerId = layerAssignmentMap.get(parentId)
            return !parentLayerId || parentLayerId === layerId
        })
        const blockedCount = entityIds.length - allowed.length
        if (blockedCount > 0) {
            showAssignmentWarning(`${blockedCount} assignment(s) blocked: children inherit their parent's layer.`)
        }
        if (allowed.length === 0) return

        const allDescendantsToMove: string[] = []
        allowed.forEach(id => allDescendantsToMove.push(...getDescendantsInDifferentLayer(id, layerId)))

        confirmOrCommit(allowed, allDescendantsToMove, layerId, logicalNodeId)
    }, [formData.layers, parentMap, layerAssignmentMap, getDescendantsInDifferentLayer, showAssignmentWarning, confirmOrCommit])

    /** Append a scoped rule to a layer (or to a logical node within a layer). */
    const handleApplyRule = useCallback((layerId: string, logicalNodeId: string | null, rule: LayerAssignmentRuleConfig) => {
        if (!formData.layers) return
        const attachToLogicalNode = (nodes: LogicalNodeConfig[] | undefined): LogicalNodeConfig[] | undefined => {
            if (!nodes) return nodes
            return nodes.map(n => {
                if (n.id === logicalNodeId) {
                    return { ...n, rules: [...(n.rules ?? []), rule] }
                }
                return { ...n, children: attachToLogicalNode(n.children) }
            })
        }
        const updatedLayers = formData.layers.map(layer => {
            if (layer.id !== layerId) return layer
            if (logicalNodeId) {
                return { ...layer, logicalNodes: attachToLogicalNode(layer.logicalNodes) }
            }
            return { ...layer, rules: [...(layer.rules ?? []), rule] }
        })
        updateFormData({ layers: updatedLayers })
    }, [formData.layers, updateFormData])

    return (
        <div className="flex flex-col h-[650px] gap-2">
            {assignmentWarning && (
                <div className="mx-2 px-3 py-2 rounded-md bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400 text-xs flex items-center gap-2">
                    <span className="font-medium">Assignment blocked.</span>
                    <span className="flex-1">{assignmentWarning}</span>
                    <button onClick={() => setAssignmentWarning(null)} className="text-red-400 hover:text-red-600">&times;</button>
                </div>
            )}
            <div className="flex flex-1 min-h-0 gap-6">
                <div className="w-2/5 min-w-[380px] flex flex-col">
                    <WizardAssignmentTree
                        layers={formData.layers || []}
                        onAssignmentChange={handleAssignmentChange}
                        onBulkAssign={handleBulkAssignment}
                        onApplyRule={handleApplyRule}
                        onParentMapChange={setBrowserParentMap}
                        className="h-full"
                    />
                </div>
                <div className="flex-1 flex flex-col min-h-0">
                    <div className="mb-3">
                        <h3 className="text-lg font-semibold text-slate-800 dark:text-white">Layer Targets</h3>
                        <p className="text-sm text-slate-500">Drop entities here or use the dropdown in the tree</p>
                    </div>
                    <div className="flex-1 overflow-y-auto pr-2">
                        <LayerManager
                            layers={formData.layers || []}
                            onUpdate={(layers) => updateFormData({ layers })}
                            onBulkAssign={handleBulkAssignment}
                            mode="assignment"
                            className="pb-4"
                        />
                    </div>
                </div>
            </div>

            <ChildReassignConfirmDialog
                info={pendingReassign?.info ?? null}
                layers={formData.layers || []}
                onConfirm={() => pendingReassign?.commit()}
                onCancel={() => setPendingReassign(null)}
            />
        </div>
    )
}
