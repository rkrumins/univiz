/**
 * useTraceFilteredHierarchy — produce a trace-filtered view of the canvas
 * hierarchy.
 *
 * When a trace is active, the canvas should hide everything that isn't part
 * of the trace context. Without this hook the canvas renders all nodes and
 * dims non-trace ones to 40% opacity ([FlatTreeItem.tsx:91, :157]) — which
 * leaves a 5-level-deep trace visually buried under hundreds of unrelated
 * datasets / columns.
 *
 * Behaviour:
 *  - !isTracing → returns the inputs unchanged (reference equality, no
 *    allocation). The hook is a pass-through outside trace mode.
 *  - isTracing  → returns a NEW Map / arrays containing only nodes in the
 *    trace `contextSet` (traced URNs ∪ drill-down URNs ∪ all containment
 *    ancestors). Children that aren't in the context are pruned, recursively
 *    to any depth.
 *
 * Drill-down support: `trace.drilldowns` (Map<key, TraceV2Result>) is a
 * direct input. Each drill-down's `nodes[].urn` is added to the context set
 * so deeper levels reveal automatically when the user double-clicks an
 * AGGREGATED edge — no extra wiring needed.
 *
 * Ancestors are kept so containers that host traced descendants stay visible
 * even when they themselves aren't part of the lineage (e.g. a Schema with
 * no direct lineage but Datasets underneath it that do).
 *
 * Pass-through fallback for explicitly-expanded leaves: if the user expands
 * a node that's IN the trace context (so they're navigating through traced
 * lineage) but NONE of its descendants are in the context (typically because
 * the underlying graph doesn't have AGGREGATED edges materialised at that
 * finer level — e.g. column-level lineage isn't pre-computed), then ALL
 * descendants of that node are kept verbatim. Without this fallback the
 * canvas would show the parent expanded with zero visible children but a
 * misleading "X more" pill (driven by `node.data.childCount` which doesn't
 * know about the trace filter). The fallback degrades gracefully: precise
 * trace subset when the data supports it, full subtree otherwise. The user
 * always sees something when they explicitly drill into a traced node.
 *
 * Pass-through nodes are added to `contextSet` so their lineage edges clear
 * `useEdgeProjection`'s trace gate (which checks contextSet membership of
 * both endpoints).
 */

import { useMemo } from 'react'
import type { HierarchyNode } from '@/types/hierarchy'
import type { TraceV2Result } from '@/providers/GraphDataProvider'

export interface UseTraceFilteredHierarchyOptions {
  /** Per-layer hierarchy from useLayerAssignment. */
  nodesByLayer: Map<string, HierarchyNode[]>
  /** Flat list of all hierarchy nodes (for downstream consumers). */
  displayFlat: HierarchyNode[]
  /** id → HierarchyNode lookup. */
  displayMap: Map<string, HierarchyNode>
  /** True when a trace is active. When false, hook is a no-op. */
  isTracing: boolean
  /** Strict trace membership — URNs returned by /trace/v2 (focus + upstream + downstream). */
  traceNodes: Set<string>
  /** Drill-down results from /trace/expand keyed by `${s}->${t}@${level}`. */
  drilldowns: Map<string, TraceV2Result>
  /** Canvas containment hierarchy: child id → parent id. From useContainmentHierarchy. */
  parentMap: Map<string, string>
  /** Canvas containment hierarchy: parent id → child ids. From useContainmentHierarchy. */
  childMap: Map<string, string[]>
  /** Nodes the user has explicitly expanded — drives the pass-through fallback. */
  expandedNodes: Set<string>
}

export interface UseTraceFilteredHierarchyResult {
  filteredByLayer: Map<string, HierarchyNode[]>
  filteredFlat: HierarchyNode[]
  filteredMap: Map<string, HierarchyNode>
  /** Trace context = traced URNs + drilldown URNs + ancestors. Empty when !isTracing. */
  contextSet: Set<string>
}

const EMPTY_CONTEXT = new Set<string>()

export function useTraceFilteredHierarchy(
  opts: UseTraceFilteredHierarchyOptions,
): UseTraceFilteredHierarchyResult {
  // childMap is intentionally accepted in the options for forward-compat
  // (we may use it for a more targeted descent-walk in the future) but not
  // read here — see the "intentionally do NOT walk DOWN" note below.
  const { nodesByLayer, displayFlat, displayMap, isTracing, traceNodes, drilldowns, parentMap, expandedNodes } = opts

  return useMemo(() => {
    if (!isTracing || (traceNodes.size === 0 && drilldowns.size === 0)) {
      return {
        filteredByLayer: nodesByLayer,
        filteredFlat: displayFlat,
        filteredMap: displayMap,
        contextSet: EMPTY_CONTEXT,
      }
    }

    // 1. Build the context set: trace URNs + drill-down URNs + all ancestors.
    //    Ancestors keep host containers visible even if the container itself
    //    has no direct lineage edges.
    const contextSet = new Set<string>()
    const addWithAncestors = (id: string | undefined | null) => {
      if (!id || contextSet.has(id)) return
      contextSet.add(id)
      let parent = parentMap.get(id)
      while (parent) {
        if (contextSet.has(parent)) break
        contextSet.add(parent)
        parent = parentMap.get(parent)
      }
    }
    traceNodes.forEach(addWithAncestors)
    drilldowns.forEach(d => d.nodes.forEach(n => addWithAncestors(n.urn)))

    // Note: we intentionally do NOT walk DOWN from trace participants to
    // include their loaded descendants. Including descendants would expose
    // every leaf's ambient lineage in trace mode, producing edge fan-out
    // explosions on hub-node traces. Trace-merged edges are handled by
    // `useEdgeProjection`'s `traceAddedEdgeIds` allowlist (Change 1.2),
    // which lets edges that came directly from /trace/v2 or /trace/expand
    // through the gate without requiring contextSet membership of both
    // endpoints. Browse-mode lineage of loaded children stays hidden.

    // 2. Recursively prune the hierarchy tree.
    //    Keep a node iff its id/urn is in the context OR any descendant is.
    //    Returns the rebuilt subtree, or null when the entire subtree is pruned.
    const filteredFlat: HierarchyNode[] = []
    const filteredMap = new Map<string, HierarchyNode>()

    const recordKept = (node: HierarchyNode) => {
      filteredFlat.push(node)
      filteredMap.set(node.id, node)
    }

    const collectSubtree = (node: HierarchyNode) => {
      // Used by pass-through: emit every descendant unchanged into the flat
      // map so search/edge-projection see them. Also add the URN to the
      // context set — pass-through nodes are visible, so edges between them
      // must clear `useEdgeProjection`'s trace gate (which checks contextSet
      // membership of both endpoints).
      contextSet.add(node.id)
      if (node.urn && node.urn !== node.id) contextSet.add(node.urn)
      recordKept(node)
      for (const c of node.children) collectSubtree(c)
    }

    const pruneTree = (node: HierarchyNode): HierarchyNode | null => {
      const filteredChildren: HierarchyNode[] = []
      for (const child of node.children) {
        const kept = pruneTree(child)
        if (kept) filteredChildren.push(kept)
      }

      const inContext = contextSet.has(node.id) || contextSet.has(node.urn)

      // PASS-THROUGH FALLBACK: traced node, user-expanded, has children but
      // none of them passed the normal filter. Show everything inside —
      // descendants are emitted verbatim so the user always sees something
      // when they explicitly drill into a traced node, even when the trace
      // data doesn't extend to finer levels.
      const shouldRelax =
        inContext
        && expandedNodes.has(node.id)
        && filteredChildren.length === 0
        && node.children.length > 0

      if (shouldRelax) {
        for (const c of node.children) collectSubtree(c)
        recordKept(node)
        return node
      }

      if (!inContext && filteredChildren.length === 0) return null

      const rebuilt: HierarchyNode = filteredChildren.length === node.children.length
        && filteredChildren.every((c, i) => c === node.children[i])
        ? node
        : { ...node, children: filteredChildren }

      recordKept(rebuilt)
      return rebuilt
    }

    const filteredByLayer = new Map<string, HierarchyNode[]>()
    nodesByLayer.forEach((layerNodes, layerId) => {
      const kept: HierarchyNode[] = []
      for (const root of layerNodes) {
        const subtree = pruneTree(root)
        if (subtree) kept.push(subtree)
      }
      filteredByLayer.set(layerId, kept)
    })

    return { filteredByLayer, filteredFlat, filteredMap, contextSet }
  }, [nodesByLayer, displayFlat, displayMap, isTracing, traceNodes, drilldowns, parentMap, expandedNodes])
}
