import { useState, useCallback, useRef, useEffect } from 'react'
import { useCanvasStore, type LineageNode, type LineageEdge } from '@/store/canvas'
import { useGraphProvider, useGraphProviderContext } from '@/providers/GraphProviderContext'
import {
    useActiveView,
    isContainmentEdgeType,
    normalizeEdgeType,
} from '@/store/schema'
import {
    useViewContainmentEdgeTypes,
    useViewLineageEdgeTypes,
    useViewRootEntityTypes,
    useViewEntityTypes,
    useViewSchemaIsReady,
} from '@/hooks/useViewSchema'
import type { GraphNode, GraphEdge, EntityTypeDefinition } from '@/providers/GraphDataProvider'

// ─── Constants ──────────────────────────────────────────────────────────────

/** Max entities per type per fetch. Keeps initial loads manageable. */
const PER_TYPE_LIMIT = 200

// ─── Interfaces ─────────────────────────────────────────────────────────────

interface LoadChildrenOptions {
    // Reserved for future use. The useAllSchemaTypes option was removed —
    // the Entity Browser now uses the dedicated useEntityBrowser hook.
}

export type HydrationPhase = 'idle' | 'roots' | 'edges' | 'children' | 'complete'

export interface UseGraphHydrationResult {
    /** Load children for a node (empty string = load roots). */
    loadChildren: (parentId: string, options?: LoadChildrenOptions) => Promise<void>
    /** Search children under a parent node. */
    searchChildren: (parentId: string, query: string) => Promise<void>
    /** True when any loading operation is in progress. */
    isLoading: boolean
    /** Set of node IDs currently being loaded. */
    loadingNodes: Set<string>
    /** Set of node IDs that failed to load. */
    failedNodes: Set<string>
    /** Current phase of initial hydration (only meaningful when hydrate=true). */
    hydrationPhase: HydrationPhase
    /** Error message if hydration failed (e.g. provider unavailable). */
    hydrationError: string | null
}

interface UseGraphHydrationOptions {
    /**
     * When true, runs the initial hydration effect that loads root nodes + edges
     * on mount / view change. Only ONE component should set this to true
     * (CanvasRouter). All other consumers should leave it false (default) and
     * only use loadChildren / searchChildren.
     */
    hydrate?: boolean
}

// ─── Conversion Utilities (exported for reuse) ──────────────────────────────

/**
 * Convert a backend GraphNode to a canvas LineageNode.
 *
 * Explicit field-by-field map (no `...n` spread) so the canvas store doesn't
 * carry both `displayName` AND `label`, both `entityType` AND `type`, etc.
 * Values pass through verbatim — including empty strings — to preserve
 * wire fidelity. Callers that need a "treat empty as absent" rule should
 * apply it at the consumer, not here.
 */
export function toCanvasNode(n: GraphNode, opts?: { randomPosition?: boolean }): LineageNode {
    return {
        id: n.urn,
        type: 'generic' as const,
        position: opts?.randomPosition
            ? { x: Math.random() * 800, y: Math.random() * 600 }
            : { x: 0, y: 0 },
        data: {
            // Identity
            urn: n.urn,
            label: n.displayName,
            type: n.entityType,
            // Descriptive — verbatim from the backend GraphNode
            qualifiedName: n.qualifiedName,
            description: n.description,
            sourceSystem: n.sourceSystem,
            layerAssignment: n.layerAssignment,
            lastSyncedAt: n.lastSyncedAt,
            childCount: n.childCount,
            // Editable property bag (renamed from `metadata` → `properties`)
            properties: n.properties,
            // Frontend conveniences derived from the property bag / tags
            classifications: n.tags,
            businessLabel: (n.properties?.businessLabel as string) ?? undefined,
        },
    }
}

/** Convert a backend GraphEdge to a canvas LineageEdge using real backend edge data. */
export function toCanvasEdge(e: GraphEdge): LineageEdge {
    return {
        id: e.id,
        source: e.sourceUrn,
        target: e.targetUrn,
        type: 'lineage',
        data: {
            edgeType: e.edgeType,
            relationship: e.edgeType,
            confidence: e.confidence,
        },
    }
}

/**
 * Compute the "view-scoped root types" for a reference/context view.
 *
 * A type is a VIEW ROOT if none of its canBeContainedBy parents appear in
 * the view's visibleEntityTypes set.
 */
export function computeViewScopedRoots(
    visibleTypes: string[],
    schemaEntityTypes: EntityTypeDefinition[],
    globalRoots: string[],
): string[] {
    if (visibleTypes.length === 0) return globalRoots

    const visibleSet = new Set(visibleTypes)

    const roots = visibleTypes.filter(typeId => {
        const et = schemaEntityTypes.find(e => e.id === typeId)
        if (!et) return true
        const parents = et.hierarchy?.canBeContainedBy ?? []
        return parents.every(parentType => !visibleSet.has(parentType))
    })

    if (roots.length > 0) return roots

    const globalOverlap = globalRoots.filter(r => visibleSet.has(r))
    return globalOverlap.length > 0 ? globalOverlap : [visibleTypes[0]]
}

// ─── The Hook ───────────────────────────────────────────────────────────────

export function useGraphHydration(options?: UseGraphHydrationOptions): UseGraphHydrationResult {
    const enableHydration = options?.hydrate ?? false

    const provider = useGraphProvider()
    const { providerVersion } = useGraphProviderContext()
    const containmentEdgeTypes = useViewContainmentEdgeTypes()
    const lineageEdgeTypes = useViewLineageEdgeTypes()
    const rootEntityTypes = useViewRootEntityTypes()
    const schemaEntityTypes = useViewEntityTypes()
    const isSchemaReady = useViewSchemaIsReady()
    const activeView = useActiveView()

    const [loadingNodes, setLoadingNodes] = useState<Set<string>>(new Set())
    const [failedNodes, setFailedNodes] = useState<Set<string>>(new Set())
    const [hydrationPhase, setHydrationPhase] = useState<HydrationPhase>('idle')
    const [hydrationError, setHydrationError] = useState<string | null>(null)

    // Prevent infinite retries when API returns [] for roots
    const rootsAttemptedForRef = useRef<string | null>(null)

    // Track (provider, viewId) so reference views reload when the active view changes.
    const initializedKeyRef = useRef<string | null>(null)

    // Cancellation: abort in-flight child loads when provider/view changes
    const loadAbortRef = useRef<AbortController>(new AbortController())

    // Reset when provider changes (e.g. workspace/datasource switch)
    useEffect(() => {
        rootsAttemptedForRef.current = null
        // Also reset the hydration guard so re-hydration happens on provider change
        initializedKeyRef.current = null
        // Cancel any in-flight child loads from the previous provider
        loadAbortRef.current.abort()
        loadAbortRef.current = new AbortController()
    }, [provider])

    // ─── Initial Hydration Effect (only when hydrate=true) ──────────────
    //
    // This effect ONLY fires in CanvasRouter. Individual canvas components
    // (HierarchyCanvas, ContextViewCanvas, etc.) call useGraphHydration()
    // without { hydrate: true }, so they skip this entirely and only use
    // loadChildren / searchChildren.

    useEffect(() => {
        if (!enableHydration) return
        // Inside ViewExecutionProvider, isSchemaReady is always true because
        // the provider gates children behind schema readiness. Outside (legacy),
        // it reflects the global schema loading state.
        if (!isSchemaReady) return

        const layoutType = activeView?.layout.type ?? 'graph'
        const isReferenceView = layoutType === 'reference'

        // Key on providerVersion + view ID + layout type. The provider IS the
        // scope (its wsId/dsId are fixed at construction), so we don't need to
        // include workspace/datasource IDs in the key — changing the view's
        // scope changes the provider, which changes providerVersion.
        const initKey = `${providerVersion}:${activeView?.id ?? 'default'}:${layoutType}`

        if (initializedKeyRef.current === initKey) return
        initializedKeyRef.current = initKey

        const { setGraph } = useCanvasStore.getState()

        // Clear canvas and error state atomically
        setGraph([], [])
        setHydrationError(null)

        const controller = new AbortController()

        const hydrate = async () => {
            try {
                if (isReferenceView) {
                    // ── Reference / Context View ────────────────────────
                    // Strategy: load ONLY the entities that are relevant to this view.
                    //
                    // If the view has explicit layer assignments (entityAssignments),
                    // load those specific entities by URN. This matches exactly what
                    // the user configured in the ViewWizard/LayerStudio.
                    //
                    // If no assignments exist (new/empty view), fall back to loading
                    // by entity type so the user has something to work with.

                    const viewLayers = activeView?.layout?.referenceLayout?.layers ?? []
                    const viewTypes = activeView?.content?.visibleEntityTypes ?? []

                    // Collect all explicitly assigned entity URNs across all layers
                    const assignedUrns = new Set<string>()
                    for (const layer of viewLayers) {
                        if (layer.entityAssignments) {
                            for (const assignment of layer.entityAssignments) {
                                if (assignment.entityId) assignedUrns.add(assignment.entityId)
                            }
                        }
                    }

                    const hasExplicitAssignments = assignedUrns.size > 0

                    setHydrationPhase('roots')

                    let allNodes: GraphNode[] = []

                    if (hasExplicitAssignments) {
                        // ── Assignment-driven loading ──
                        // Load the specific entities assigned to layers by URN.
                        // This is precise: only what the user configured appears.
                        const urnBatches: string[][] = []
                        const urnArray = [...assignedUrns]
                        // Batch URNs to avoid overly large queries
                        for (let i = 0; i < urnArray.length; i += 100) {
                            urnBatches.push(urnArray.slice(i, i + 100))
                        }

                        const batchResults = await Promise.all(
                            urnBatches.map(batch =>
                                provider.getNodes({ urns: batch as any[], limit: batch.length })
                                    .catch(() => [] as GraphNode[])
                            )
                        )
                        allNodes = batchResults.flat()
                        if (controller.signal.aborted) return

                        // Also load children of assigned entities (for hierarchy within layers).
                        // This ensures containment trees are populated, not just top-level entities.
                        setHydrationPhase('children')
                        if (allNodes.length > 0) {
                            const parentsWithChildren = allNodes.filter(n => (n.childCount ?? 0) > 0)
                            if (parentsWithChildren.length > 0) {
                                const childResults = await Promise.all(
                                    parentsWithChildren.map(parent =>
                                        provider.getChildren(parent.urn, { limit: 100 })
                                            .catch(() => [] as GraphNode[])
                                    )
                                )
                                const childNodes = childResults.flat()
                                if (controller.signal.aborted) return
                                // Dedup: children might overlap with assigned entities
                                const knownUrns = new Set(allNodes.map(n => n.urn))
                                const newChildren = childNodes.filter(c => !knownUrns.has(c.urn))
                                allNodes = [...allNodes, ...newChildren]
                            }
                        }
                    } else {
                        // ── Type-based loading (empty/new views) ──
                        // No assignments yet — load by entity type so the view has data
                        // for the user to start assigning in the wizard.
                        const rootTypes = computeViewScopedRoots(viewTypes, schemaEntityTypes, rootEntityTypes)
                        if (rootTypes.length === 0) return

                        const rootResults = await Promise.all(
                            rootTypes.map(et =>
                                provider.getNodes({ entityTypes: [et], limit: PER_TYPE_LIMIT })
                                    .catch(() => [] as GraphNode[])
                            )
                        )
                        allNodes = rootResults.flat()
                        if (controller.signal.aborted || allNodes.length === 0) return

                        // Also load remaining visible types (non-root layers)
                        setHydrationPhase('children')
                        const loadedRootTypes = new Set(allNodes.map(n => n.entityType))
                        const remainingTypes = viewTypes.filter(t => !loadedRootTypes.has(t))
                        if (remainingTypes.length > 0) {
                            const childResults = await Promise.all(
                                remainingTypes.map(et =>
                                    provider.getNodes({ entityTypes: [et], limit: PER_TYPE_LIMIT })
                                        .catch(() => [] as GraphNode[])
                                )
                            )
                            const childNodes = childResults.flat()
                            if (controller.signal.aborted) return
                            allNodes = [...allNodes, ...childNodes]
                        }
                    }

                    if (allNodes.length === 0) return

                    // Show nodes immediately, then fetch edges
                    setGraph(
                        allNodes.map(n => toCanvasNode(n)),
                        [],
                    )

                    // Fetch edges between all loaded nodes
                    setHydrationPhase('edges')
                    const allUrns = allNodes.map(n => n.urn)
                    const allEdges = await provider.getEdgesBetween(allUrns).catch(() => [] as GraphEdge[])
                    if (controller.signal.aborted) return

                    // Replace with complete dataset atomically
                    setGraph(
                        allNodes.map(n => toCanvasNode(n)),
                        allEdges.map(e => toCanvasEdge(e)),
                    )

                    console.log(`[useGraphHydration] Reference view: loaded ${allNodes.length} nodes (${assignedUrns.size} assigned), ${allEdges.length} edges`)
                } else {
                    // ── Hierarchy / Graph view ──────────────────────────
                    // Mirrors old App.tsx behavior: load roots, then first-level
                    // children for each root, then all edges between them.
                    // This ensures HierarchyCanvas has containment edges to
                    // build its tree immediately.
                    const typesToLoad = rootEntityTypes.length > 0
                        ? rootEntityTypes
                        : schemaEntityTypes.map(et => et.id)

                    if (typesToLoad.length === 0) return

                    // Step 1: Fetch root nodes
                    setHydrationPhase('roots')
                    const rootNodes = await provider.getNodes({
                        entityTypes: typesToLoad as any[],
                        limit: PER_TYPE_LIMIT,
                    })
                    if (controller.signal.aborted || rootNodes.length === 0) return

                    // Show roots immediately
                    setGraph(
                        rootNodes.map(n => toCanvasNode(n, { randomPosition: true })),
                        [],
                    )

                    // Step 2: Fetch first-level children for all roots (parallel)
                    setHydrationPhase('children')
                    const childrenPromises = rootNodes.map(root =>
                        provider.getChildren(root.urn, { limit: 100 })
                            .catch(() => [] as GraphNode[])
                    )
                    const childrenResults = await Promise.all(childrenPromises)
                    const allChildren = childrenResults.flat()
                    if (controller.signal.aborted) return

                    // Step 3: Fetch orphaned nodes of child types (nodes without
                    // a parent in our root set, e.g. dataPlatforms without a domain)
                    const entityTypeHierarchy = schemaEntityTypes
                    const childTypes = new Set<string>()
                    for (const rootType of typesToLoad) {
                        const et = entityTypeHierarchy.find(e => e.id === rootType)
                        et?.hierarchy?.canContain?.forEach((t: string) => childTypes.add(t))
                    }

                    let orphanNodes: GraphNode[] = []
                    if (childTypes.size > 0) {
                        const childTypeNodes = await provider.getNodes({
                            entityTypes: [...childTypes] as any[],
                            limit: PER_TYPE_LIMIT,
                        }).catch(() => [] as GraphNode[])
                        if (controller.signal.aborted) return

                        // Filter out nodes we already have
                        const knownUrns = new Set([
                            ...rootNodes.map(n => n.urn),
                            ...allChildren.map(n => n.urn),
                        ])
                        orphanNodes = childTypeNodes.filter(n => !knownUrns.has(n.urn))
                    }

                    // Deduplicate all nodes
                    const nodeMap = new Map<string, GraphNode>()
                    for (const n of [...rootNodes, ...allChildren, ...orphanNodes]) {
                        nodeMap.set(n.urn, n)
                    }
                    const uniqueNodes = Array.from(nodeMap.values())
                    if (uniqueNodes.length === 0) return

                    // Step 4: Fetch edges between ALL loaded nodes
                    setHydrationPhase('edges')
                    const allUrns = uniqueNodes.map(n => n.urn)
                    const allEdges = await provider.getEdgesBetween(allUrns).catch(() => [] as GraphEdge[])
                    if (controller.signal.aborted) return

                    console.log(`[useGraphHydration] Loaded ${uniqueNodes.length} nodes (${rootNodes.length} roots, ${allChildren.length} children, ${orphanNodes.length} orphans), ${allEdges.length} edges`)

                    setGraph(
                        uniqueNodes.map(n => toCanvasNode(n, { randomPosition: true })),
                        allEdges.map(e => toCanvasEdge(e)),
                    )
                }

                setHydrationPhase('complete')
            } catch (err) {
                if (!controller.signal.aborted) {
                    console.error('[useGraphHydration] Hydration failed:', err)
                    const msg = err instanceof Error ? err.message : 'Failed to load graph data'
                    setHydrationError(
                        msg.includes('circuit') || msg.includes('timed out') || msg.includes('503')
                            ? 'The graph provider for this view is unavailable. The view definition is loaded but graph data cannot be displayed.'
                            : msg
                    )
                }
            }
        }

        hydrate()
        return () => {
            controller.abort()
            // Reset the guard so the next effect run (same initKey, new deps snapshot)
            // can re-start hydration. Without this, a mid-flight abort (e.g. background
            // schema refresh changing rootEntityTypes) leaves initializedKeyRef permanently
            // set to initKey, causing the subsequent run to return early and the canvas
            // to stay empty — most visible on cross-workspace view navigation.
            if (initializedKeyRef.current === initKey) {
                initializedKeyRef.current = null
            }
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [enableHydration, provider, providerVersion, activeView?.id, activeView?.layout.type, rootEntityTypes, schemaEntityTypes, isSchemaReady])

    // ─── loadChildren ───────────────────────────────────────────────────

    const loadChildren = useCallback(async (parentId: string, _options?: LoadChildrenOptions) => {
        const { nodes, edges, addGraph } = useCanvasStore.getState()

        // ── Handle root loading (empty parentId) ────────────────────
        if (!parentId) {
            if (loadingNodes.has('ROOT')) return
            if (!isSchemaReady) return

            const typesToLoad = rootEntityTypes

            if (typesToLoad.length === 0) return

            const key = `all:${typesToLoad.join(',')}`
            if (rootsAttemptedForRef.current === key) return
            rootsAttemptedForRef.current = key

            setLoadingNodes(prev => new Set(prev).add('ROOT'))
            try {
                const roots = await provider.getNodes({
                    entityTypes: typesToLoad as any[],
                    limit: 200,
                })

                if (roots.length > 0) {
                    const nodesToAdd = roots.map(root => toCanvasNode(root))

                    // Fetch real edges from backend
                    const existingUrns = useCanvasStore.getState().nodes.map(n => n.id)
                    const allUrns = [...new Set([...roots.map(r => r.urn), ...existingUrns])]
                    const backendEdges = await provider.getEdgesBetween(allUrns).catch(() => [] as GraphEdge[])

                    addGraph(nodesToAdd, backendEdges.map(e => toCanvasEdge(e)))
                }
            } catch (err) {
                console.error('[useGraphHydration] Failed to load roots', err)
            } finally {
                setLoadingNodes(prev => {
                    const next = new Set(prev)
                    next.delete('ROOT')
                    return next
                })
            }
            return
        }

        // ── Handle child loading (specific parentId) ────────────────
        const parentNode = nodes.find(n => n.id === parentId)
        if (!parentNode) return
        if (loadingNodes.has(parentId)) return

        const nodeData = parentNode.data as any
        const childCount = (nodeData.childCount as number) ?? (nodeData.metadata?.childCount as number) ?? 0
        if (childCount === 0) return

        const existingNodeIds = new Set(nodes.map(n => n.id))

        // Count loaded children via containment edges (ontology-driven)
        const currentChildrenCount = edges.filter(e => {
            if (e.source !== parentId) return false
            if (!existingNodeIds.has(e.target)) return false
            return isContainmentEdgeType(normalizeEdgeType(e), containmentEdgeTypes)
        }).length

        // If we have all children, don't refetch
        if (currentChildrenCount >= childCount && childCount > 0) return

        // Fetch children
        setFailedNodes(prev => { const next = new Set(prev); next.delete(parentId); return next })
        setLoadingNodes(prev => new Set(prev).add(parentId))
        try {
            const urn = (parentNode.data.urn as string) || parentId
            const fetchTypes = containmentEdgeTypes.length > 0 ? containmentEdgeTypes : undefined

            // Single round-trip: children + containment edges + lineage edges
            const result = await provider.getChildrenWithEdges(urn, {
                edgeTypes: fetchTypes,
                lineageEdgeTypes: lineageEdgeTypes.length > 0 ? lineageEdgeTypes : undefined,
                limit: 20,
                offset: currentChildrenCount,
                includeLineageEdges: true,
            })

            if (result.children.length > 0) {
                const currentExistingNodeIds = new Set(
                    useCanvasStore.getState().nodes.map(n => n.id)
                )

                const nodesToAdd: LineageNode[] = []
                const newIds = new Set<string>()

                result.children.forEach(child => {
                    if (!currentExistingNodeIds.has(child.urn) && !newIds.has(child.urn)) {
                        nodesToAdd.push(toCanvasNode(child))
                        newIds.add(child.urn)
                    }
                })

                const edgesToAdd = [
                    ...result.containmentEdges,
                    ...result.lineageEdges,
                ].map(e => toCanvasEdge(e))

                // Cancellation check before committing to the store
                if (loadAbortRef.current.signal.aborted) return

                // Single atomic commit — nodes and edges arrive together
                const { addGraph: addGraphFresh } = useCanvasStore.getState()
                addGraphFresh(nodesToAdd, edgesToAdd)

                console.log(`[useGraphHydration] Loaded ${nodesToAdd.length} children for ${parentId}`)
            }
        } catch (err) {
            console.error(`[useGraphHydration] Failed to load children for ${parentId}`, err)
            setFailedNodes(prev => new Set(prev).add(parentId))
        } finally {
            setLoadingNodes(prev => {
                const next = new Set(prev)
                next.delete(parentId)
                return next
            })
        }
    }, [provider, containmentEdgeTypes, lineageEdgeTypes, rootEntityTypes, schemaEntityTypes, isSchemaReady, loadingNodes])

    // ─── searchChildren ─────────────────────────────────────────────────

    const searchChildren = useCallback(async (parentId: string, query: string) => {
        if (!query.trim()) return

        setLoadingNodes(prev => new Set(prev).add(parentId))
        try {
            const { nodes, removeNodes, removeEdges, addGraph } = useCanvasStore.getState()

            const parentNode = nodes.find(n => n.id === parentId)
            const urn = parentNode ? (parentNode.data.urn as string || parentId) : parentId
            const fetchTypes = containmentEdgeTypes.length > 0 ? containmentEdgeTypes : undefined

            // Single round-trip: children + edges for search results
            const result = await provider.getChildrenWithEdges(urn, {
                edgeTypes: fetchTypes,
                lineageEdgeTypes: lineageEdgeTypes.length > 0 ? lineageEdgeTypes : undefined,
                searchQuery: query,
                limit: 50,
                includeLineageEdges: true,
            })

            // Get freshest state right before mutating
            const freshNodes = useCanvasStore.getState().nodes
            const freshEdges = useCanvasStore.getState().edges

            // Clean up existing children of this node to replace with search results
            const existingEdgesToRemove = freshEdges.filter(e => e.source === parentId)
            const targetNodeIdsToRemove = new Set(existingEdgesToRemove.map(e => e.target))

            // Keep nodes connected to other parents
            const otherEdges = freshEdges.filter(e => e.source !== parentId)
            const safeNodesToKeep = new Set(otherEdges.map(e => e.target))
            const nodeIdsToRemove = Array.from(targetNodeIdsToRemove).filter(id => !safeNodesToKeep.has(id))
            const edgeIdsToRemove = existingEdgesToRemove.map(e => e.id)

            if (nodeIdsToRemove.length > 0) removeNodes(nodeIdsToRemove)
            if (edgeIdsToRemove.length > 0) removeEdges(edgeIdsToRemove)

            if (result.children.length > 0) {
                const nodesToAdd: LineageNode[] = []

                const remainingNodeIds = new Set(freshNodes.map(n => n.id))
                nodeIdsToRemove.forEach(id => remainingNodeIds.delete(id))
                const newIds = new Set<string>()

                result.children.forEach(child => {
                    if (!remainingNodeIds.has(child.urn) && !newIds.has(child.urn)) {
                        nodesToAdd.push(toCanvasNode(child))
                        newIds.add(child.urn)
                    }
                })

                const edgesToAdd = [
                    ...result.containmentEdges,
                    ...result.lineageEdges,
                ].map(e => toCanvasEdge(e))

                if (loadAbortRef.current.signal.aborted) return
                addGraph(nodesToAdd, edgesToAdd)
            }
        } catch (err) {
            console.error(`[useGraphHydration] Failed to search children for ${parentId}`, err)
        } finally {
            setLoadingNodes(prev => {
                const next = new Set(prev)
                next.delete(parentId)
                return next
            })
        }
    }, [provider, containmentEdgeTypes, lineageEdgeTypes])

    return {
        loadChildren,
        searchChildren,
        isLoading: loadingNodes.size > 0,
        loadingNodes,
        failedNodes,
        hydrationPhase,
        hydrationError,
    }
}
