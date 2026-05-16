/**
 * useLayerAssignment - Extracted from ReferenceModelCanvas.tsx
 *
 * Encapsulates:
 * - layerRules: build layer assignment rules from sorted layers
 * - nodesByLayer: core layer assignment algorithm with deep inheritance
 * - displayFlat / displayMap: flattened node list and lookup map
 * - urnToIdMap: O(1) URN-to-ID lookup
 */

import { useMemo } from 'react'
import type { ViewLayerConfig, LogicalNodeConfig } from '@/types/schema'
import {
  type GraphNode,
  resolveLayerAssignment,
  type LayerAssignmentRule,
  type EntityType,
} from '@/providers/GraphDataProvider'
import type { HierarchyNode } from '@/types/hierarchy'

// ============================================
// Types
// ============================================

export interface UseLayerAssignmentOptions {
  nodes: any[]
  sortedLayers: ViewLayerConfig[]
  nodeEdgeFingerprint: string
  instanceAssignments: Map<string, { layerId: string }>
  effectiveAssignments: Map<string, { layerId: string }>
  nodeMap: Map<string, any>
  childMap: Map<string, string[]>
  parentMap: Map<string, string>
}

export interface UseLayerAssignmentResult {
  layerRules: LayerAssignmentRule[]
  nodesByLayer: Map<string, HierarchyNode[]>
  displayFlat: HierarchyNode[]
  displayMap: Map<string, HierarchyNode>
  urnToIdMap: Map<string, string>
  /** Final effective layer per node id — used by the trace merge to derive
   *  an assignmentHint for lineage participants whose containment chain
   *  doesn't reach an already-placed canvas anchor. */
  nodeLayerMap: Map<string, string>
}

// ============================================
// Hook
// ============================================

export function useLayerAssignment({
  nodes,
  sortedLayers,
  nodeEdgeFingerprint,
  instanceAssignments,
  effectiveAssignments,
  nodeMap,
  childMap,
  parentMap,
}: UseLayerAssignmentOptions): UseLayerAssignmentResult {

  // Build layer assignment rules
  const layerRules = useMemo<LayerAssignmentRule[]>(() => {
    const generatedRules: LayerAssignmentRule[] = []

    sortedLayers.forEach(layer => {
      // 1. Explicit rules from config
      if (layer.rules) {
        layer.rules.forEach(rule => {
          generatedRules.push({
            id: rule.id,
            layerId: layer.id,
            entityTypes: (rule.entityTypes ?? []) as EntityType[],
            tags: rule.tags,
            urnPattern: rule.urnPattern,
            propertyMatch: rule.propertyMatch,
            priority: rule.priority
          })
        })
      }

      // 2. Auto-generate entity-type rules from layer.entityTypes.
      // When a layer declares entityTypes: ['glossary', 'term'], nodes of those
      // types are automatically routed here — this is the primary ontology-driven
      // assignment mechanism. Explicit entity assignments and rules above take
      // precedence (higher priority values win in resolveLayerAssignment).
      if (layer.entityTypes && layer.entityTypes.length > 0) {
        layer.entityTypes.forEach((entityType, idx) => {
          generatedRules.push({
            id: `${layer.id}-type-${entityType}`,
            layerId: layer.id,
            entityTypes: [entityType],
            priority: layer.order * 10 + idx,
          })
        })
      }
    })

    return generatedRules
  }, [sortedLayers])

  // Core Logic: Group nodes by layer with Deep Inheritance support
  const nodesByLayer = useMemo(() => {
    const grouped = new Map<string, HierarchyNode[]>()

    // Initialize layers
    sortedLayers.forEach(l => grouped.set(l.id, []))

    // 1. Build explicit assignments from view layers (lowest priority, used as fallback)
    // These come from saved entityAssignments in the view configuration
    const explicitAssignments = new Map<string, string>() // nodeId -> layerId
    sortedLayers.forEach(l => {
      l.entityAssignments?.forEach(a => {
        explicitAssignments.set(a.entityId, l.id)
      })
    })

    // 2. Build rule-based assignments (fallback if no explicit assignment)
    const ruleAssignments = new Map<string, string>() // nodeId -> layerId
    nodes.forEach(node => {
      // Skip if already has explicit assignment from view
      if (explicitAssignments.has(node.id)) return

      // Rule match
      const graphNode: GraphNode = {
        urn: node.data.urn || node.id,
        entityType: (node.data.type as string) || '',
        displayName: node.data.label || node.data.businessLabel || node.id,
        properties: node.data as Record<string, unknown>,
        tags: node.data.classifications || []
      }

      const ruleLayerId = resolveLayerAssignment(graphNode, layerRules)
      if (ruleLayerId) {
        ruleAssignments.set(node.id, ruleLayerId)
      }
    })

    // 2. Determine "Effective Layer" for every node, considering inheritance
    // We traverse top-down. If a node has explicit, it wins. If not, it inherits.
    const effectiveLayer = new Map<string, string>() // nodeId -> layerId

    // We can't just iterate nodes orderless. We need top-down.
    // Use a Set to track processed.
    const processed = new Set<string>()

    // Iterative top-down traversal (prevents stack overflow on deep hierarchies)
    // HARD RULE: Containment children ALWAYS inherit parent's layer (no override).
    // Root-level nodes use priority chain:
    // 1. effectiveAssignments (backend) 2. instanceAssignments (user drag)
    // 3. explicitAssignments (view config) 4. ruleAssignments (rules) 5. inheritance
    const roots = nodes.filter((n: any) => !parentMap.has(n.id))
    const stack: Array<{ nodeId: string; inheritedLayerId?: string }> = []
    // Push roots in reverse so first root is processed first
    for (let i = roots.length - 1; i >= 0; i--) {
      stack.push({ nodeId: roots[i].id })
    }

    while (stack.length > 0) {
      const { nodeId, inheritedLayerId } = stack.pop()!
      if (processed.has(nodeId)) continue
      processed.add(nodeId)

      let myLayerId: string | undefined

      // HARD RULE: Containment children ALWAYS inherit parent's layer.
      // No assignment (backend, user drag, explicit, rules) can override this.
      // Only root-level nodes (no containment parent) use the priority chain.
      const hasContainmentParent = parentMap.has(nodeId)

      if (hasContainmentParent && inheritedLayerId) {
        // Absolute inheritance — child is locked to parent's layer
        myLayerId = inheritedLayerId
      } else {
        // Root-level node: use priority chain
        // 1. effectiveAssignments (backend)
        const backendAssignment = effectiveAssignments.get(nodeId)
        if (backendAssignment?.layerId) myLayerId = backendAssignment.layerId

        // 2. instanceAssignments (user drag)
        if (!myLayerId) {
          const instanceAssignment = instanceAssignments.get(nodeId)
          if (instanceAssignment) myLayerId = instanceAssignment.layerId
        }

        // 3. explicitAssignments (view config)
        if (!myLayerId) myLayerId = explicitAssignments.get(nodeId)

        // 4. ruleAssignments (ontology rules)
        if (!myLayerId) myLayerId = ruleAssignments.get(nodeId)

        // 5. inheritance (for non-containment relationships, if any)
        if (!myLayerId && inheritedLayerId) myLayerId = inheritedLayerId

        // 6. assignmentHint — last-resort fallback used by the trace merge.
        //    When /trace/v2 returns a lineage participant whose containment
        //    chain doesn't reach a known canvas anchor, the merge stamps a
        //    hint (typically the focus's layer) onto the node's metadata so
        //    it lands somewhere visible instead of being silently dropped.
        if (!myLayerId) {
          const nodeRecord = nodeMap.get(nodeId)
          const hint = (nodeRecord?.data?.metadata as Record<string, unknown> | undefined)?.assignmentHint
          if (typeof hint === 'string' && hint.length > 0) myLayerId = hint
        }
      }

      if (myLayerId === '__UNASSIGNED__') myLayerId = undefined

      if (myLayerId) effectiveLayer.set(nodeId, myLayerId)

      const children = childMap.get(nodeId) || []
      for (let i = children.length - 1; i >= 0; i--) {
        stack.push({ nodeId: children[i], inheritedLayerId: myLayerId })
      }
    }

    // Also handle orphans (cycles or disconnected) if any missed?
    // The recursive step above should cover all reachable from roots.
    // If there are unparented nodes that are not in `roots` (impossible by definition), they are covered.

    // 3. Construct Hierarchy Trees per Layer
    // A node is a "Visual Root" in Layer L if:
    // - It is effectively in Layer L
    // - AND (Its parent is NOT in Layer L OR it has no parent)

    // Iterative hierarchy builder — post-order traversal so children are ready before parents
    const buildHierarchyNode = (rootId: string): HierarchyNode | null => {
      const rootNode = nodeMap.get(rootId)
      if (!rootNode) return null

      const rootLayer = effectiveLayer.get(rootId)
      // Phase 1: collect nodes in DFS order (iterative)
      const order: Array<{ nodeId: string; depth: number; parentIdx: number }> = []
      const dfsStack: Array<{ nodeId: string; depth: number; parentIdx: number }> = [
        { nodeId: rootId, depth: 0, parentIdx: -1 }
      ]
      while (dfsStack.length > 0) {
        const item = dfsStack.pop()!
        const idx = order.length
        order.push(item)

        const childrenIds = childMap.get(item.nodeId) || []
        // Push in reverse so first child is processed first
        for (let i = childrenIds.length - 1; i >= 0; i--) {
          const cid = childrenIds[i]
          if (effectiveLayer.get(cid) === rootLayer) {
            dfsStack.push({ nodeId: cid, depth: item.depth + 1, parentIdx: idx })
          }
        }
      }

      // Phase 2: build HierarchyNodes bottom-up
      const built: (HierarchyNode | null)[] = new Array(order.length).fill(null)
      const childrenOf: HierarchyNode[][] = order.map(() => [])

      for (let i = order.length - 1; i >= 0; i--) {
        const { nodeId, depth, parentIdx } = order[i]
        const node = nodeMap.get(nodeId)
        if (!node) continue

        const children = childrenOf[i].sort((a, b) => a.name.localeCompare(b.name))
        const hNode: HierarchyNode = {
          id: node.id,
          typeId: node.data.type,
          name: node.data.label ?? node.data.businessLabel ?? node.id,
          data: node.data as Record<string, unknown>,
          children,
          depth,
          urn: node.data.urn || node.id,
          entityTypeOption: (node.data.type as string) || '',
          tags: node.data.classifications || []
        }
        built[i] = hNode
        if (parentIdx >= 0) childrenOf[parentIdx].push(hNode)
      }

      return built[0]
    }

    nodes.forEach((node: any) => {
      const layerId = effectiveLayer.get(node.id)
      if (!layerId) return // Unassigned

      // Check if this is a Visual Root for this layer
      const parentId = parentMap.get(node.id)
      const parentLayerId = parentId ? effectiveLayer.get(parentId) : undefined

      if (layerId !== parentLayerId) {
        // It's a root in this layer context!
        const hNode = buildHierarchyNode(node.id)
        if (hNode) {
          const list = grouped.get(layerId)
          if (list) list.push(hNode)
        }
      }
    })

    // Sort all lists
    grouped.forEach(list => list.sort((a, b) => a.name.localeCompare(b.name)))

    // 4. Wrap entities in logical groups where configured.
    // Build entityId -> logicalNodeId map from all layer entityAssignments,
    // then for each layer with logicalNodes, create wrapper HierarchyNodes
    // and move assigned entities under them.
    const entityLogicalMap = new Map<string, string>() // entityId -> logicalNodeId
    sortedLayers.forEach(l => {
      l.entityAssignments?.forEach(a => {
        if (a.logicalNodeId) entityLogicalMap.set(a.entityId, a.logicalNodeId)
      })
    })
    // Also check instanceAssignments (user drag in current session)
    instanceAssignments.forEach((a, entityId) => {
      if ('logicalNodeId' in a && (a as { logicalNodeId?: string }).logicalNodeId) {
        entityLogicalMap.set(entityId, (a as { logicalNodeId?: string }).logicalNodeId!)
      }
    })

    if (entityLogicalMap.size > 0) {
      sortedLayers.forEach(layer => {
        if (!layer.logicalNodes || layer.logicalNodes.length === 0) return
        const layerNodes = grouped.get(layer.id)
        if (!layerNodes || layerNodes.length === 0) return

        // Build a flat lookup of all logical nodes in this layer (recursive)
        const logicalLookup = new Map<string, LogicalNodeConfig>()
        const collectLogicalNodes = (nodes: LogicalNodeConfig[]) => {
          nodes.forEach(n => {
            logicalLookup.set(n.id, n)
            if (n.children) collectLogicalNodes(n.children)
          })
        }
        collectLogicalNodes(layer.logicalNodes)

        if (logicalLookup.size === 0) return

        // Partition: entities assigned to a logical group vs unassigned
        const logicalChildren = new Map<string, HierarchyNode[]>() // logicalNodeId -> entities
        const ungrouped: HierarchyNode[] = []

        layerNodes.forEach(hNode => {
          const logicalId = entityLogicalMap.get(hNode.id)
          if (logicalId && logicalLookup.has(logicalId)) {
            const list = logicalChildren.get(logicalId) ?? []
            list.push(hNode)
            logicalChildren.set(logicalId, list)
          } else {
            ungrouped.push(hNode)
          }
        })

        // Only restructure if at least one entity is assigned to a logical group
        if (logicalChildren.size === 0) return

        // Build logical group wrapper HierarchyNodes (recursive for nested groups)
        const buildLogicalHierarchy = (configs: LogicalNodeConfig[], depth: number): HierarchyNode[] => {
          return configs.map(config => {
            const assignedEntities = logicalChildren.get(config.id) ?? []
            const childGroups = config.children
              ? buildLogicalHierarchy(config.children, depth + 1)
              : []

            return {
              id: `logical:${config.id}`,
              typeId: config.type,
              name: config.name,
              data: { type: config.type, label: config.name, isLogical: true },
              children: [...childGroups, ...assignedEntities].sort((a, b) => a.name.localeCompare(b.name)),
              depth,
              urn: `logical:${config.id}`,
              entityTypeOption: config.type,
              tags: [],
              isLogical: true,
              logicalConfig: config,
            } satisfies HierarchyNode
          }).filter(g => g.children.length > 0 || logicalChildren.has(g.id.replace('logical:', '')))
        }

        const logicalWrappers = buildLogicalHierarchy(layer.logicalNodes, 0)

        // Replace layer's node list: logical groups first, then ungrouped
        grouped.set(layer.id, [...logicalWrappers, ...ungrouped])
      })
    }

    return grouped
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeEdgeFingerprint, sortedLayers, layerRules, instanceAssignments, nodeMap, childMap, parentMap, effectiveAssignments])

  // Flatten logical/physical nodes for search and lookup
  const { displayFlat, displayMap } = useMemo(() => {
    const flat: HierarchyNode[] = []
    const map = new Map<string, HierarchyNode>()

    nodesByLayer.forEach((layerNodes) => {
      // Iterative DFS to prevent stack overflow on deep hierarchies
      const stack = [...layerNodes]
      while (stack.length > 0) {
        const node = stack.pop()!
        if (map.has(node.id)) continue
        flat.push(node)
        map.set(node.id, node)
        // Push children in reverse so first child is visited first
        for (let i = node.children.length - 1; i >= 0; i--) {
          stack.push(node.children[i])
        }
      }
    })

    return { displayFlat: flat, displayMap: map }
  }, [nodesByLayer])

  // O(1) URN->ID lookup (replaces O(N) displayFlat.find() per edge)
  const urnToIdMap = useMemo(() => {
    const map = new Map<string, string>()
    displayFlat.forEach(node => {
      if (node.urn) map.set(node.urn, node.id)
    })
    return map
  }, [displayFlat])

  // Re-derive nodeLayerMap from the rendered hierarchy: every HierarchyNode
  // we ended up emitting under a layer must have had a layer assignment, so
  // we recover the map without changing the algorithm's return shape.
  const nodeLayerMap = useMemo(() => {
    const map = new Map<string, string>()
    nodesByLayer.forEach((layerNodes, layerId) => {
      const stack = [...layerNodes]
      while (stack.length > 0) {
        const node = stack.pop()!
        map.set(node.id, layerId)
        for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i])
      }
    })
    return map
  }, [nodesByLayer])

  return { layerRules, nodesByLayer, displayFlat, displayMap, urnToIdMap, nodeLayerMap }
}
