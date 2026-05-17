/**
 * useEdgeProjection - Extracted from ReferenceModelCanvas.tsx
 *
 * Encapsulates:
 * - lineageEdges: aggregated + expanded detailed + trace/regular edges
 * - visibleLineageEdges: edge projection/roll-up to visible ancestors
 *
 * Phase 5.1: ancestorMap is built incrementally — on expand/collapse only
 * the changed subtree is patched, avoiding a full O(N) traversal on every
 * user interaction. Full rebuild only happens when nodesByLayer changes
 * (layer re-assignment, initial load).
 */

import { useMemo, useRef } from 'react'
import { normalizeEdgeType } from '@/store/schema'
import type { HierarchyNode } from '@/types/hierarchy'

// ============================================
// Types
// ============================================

export interface UseEdgeProjectionOptions {
  edges: any[]
  aggregatedEdges: Map<string, any>
  nodesByLayer: Map<string, HierarchyNode[]>
  expandedNodes: Set<string>
  displayFlat: HierarchyNode[]
  displayMap: Map<string, HierarchyNode>
  urnToIdMap: Map<string, string>
  showLineageFlow: boolean
  isTracing: boolean
  traceContextSet: Set<string>
  isContainmentEdge: (edgeType: string) => boolean
  /** Currently hovered node — expanded parents show edges on hover */
  hoveredNodeId?: string | null
  /**
   * URN-pair keys (`${sourceUrn}->${targetUrn}`) for parent AGGREGATED edges
   * that have been drilled into and currently have at least one finer-level
   * edge visible. Suppresses the parent AGG so the canvas doesn't render the
   * same lineage twice (rolled-up + detailed). Restored automatically when
   * either endpoint is collapsed. Only consulted in trace mode.
   */
  suppressedAggEdgeKeys?: Set<string>
  /**
   * Edge ids the trace explicitly merged into the canvas store (from
   * `trace.addedEdgeIds`). When in trace mode, these bypass the
   * `traceContextSet` gate: by construction they belong to the trace and
   * must render even if one endpoint hasn't yet been routed into the
   * trace-filtered hierarchy. Endpoint resolution via displayMap/ancestorMap
   * still applies — edges to entirely-unresolved nodes are still dropped.
   */
  traceAddedEdgeIds?: Set<string>
  /**
   * Canvas containment parent map (child id → parent id) from
   * useContainmentHierarchy. Used by the trace-mode bundling projection
   * to walk leaf-level edge endpoints up to the focus's hierarchy level so
   * thousands of column-to-column edges collapse into a handful of
   * container-to-container bundles.
   */
  traceBundleParentMap?: Map<string, string>
  /** entityType → hierarchy.level map. Required for traceFocusLevel-based bundling. */
  entityTypeLevels?: Map<string, number>
  /**
   * Hierarchy level the active trace ran at (`result.effectiveLevel`). When
   * set with `traceBundleParentMap` + `entityTypeLevels`, edges whose
   * endpoints are at a finer level get projected UP to the closest
   * ancestor that sits at this level — visualising as bundled rollups
   * rather than per-leaf spaghetti.
   */
  traceFocusLevel?: number
  /**
   * Browse-mode bundling. When enabled, edges between distinct visible
   * leaf nodes get projected up the containment hierarchy until pair-count
   * fan-in shrinks below the threshold — collapsing hub-style "every
   * object → every object" densities into one bundle per parent pair.
   * Independent of trace mode; controlled by the canvas.
   */
  browseBundleEnabled?: boolean
  /** Containment parent map used for browse-mode bundling. */
  browseBundleParentMap?: Map<string, string>
  /**
   * Maximum edges per (source, target) pair in browse mode before the
   * projection walks endpoints one level coarser. Default 1 — every pair
   * collapses on the first walk pass.
   */
  browseBundleFanInThreshold?: number
  /**
   * nodeId → layer index map (Source=0, Staging=1, …). When provided,
   * each projected edge gets `isReverseFlow` set true if the target's
   * layer index is strictly less than the source's. Used by the
   * renderer to route reverse-flow edges through a dedicated lane.
   */
  nodeLayerIndexMap?: Map<string, number>
}

// ============================================
// Tree helpers for incremental ancestorMap updates
// ============================================

/** Build a flat id→node lookup from the full tree. O(N) once, then O(1) per lookup. */
function buildNodeIndex(nodesByLayer: Map<string, HierarchyNode[]>): Map<string, HierarchyNode> {
  const index = new Map<string, HierarchyNode>()
  const stack: HierarchyNode[] = []
  nodesByLayer.forEach(roots => { for (let i = roots.length - 1; i >= 0; i--) stack.push(roots[i]) })
  while (stack.length > 0) {
    const node = stack.pop()!
    index.set(node.id, node)
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i])
  }
  return index
}

/** Map a node and all its descendants to `anchor` in the given map. Iterative. */
function collapseSubtreeInMap(root: HierarchyNode, anchor: string, map: Map<string, string>) {
  const stack: HierarchyNode[] = [root]
  while (stack.length > 0) {
    const node = stack.pop()!
    if (node.urn) map.set(node.urn, anchor)
    map.set(node.id, anchor)
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i])
  }
}

/**
 * When `node` is expanded, each direct child becomes visible and maps to
 * itself. If a child is already expanded, process its children too.
 * If a child is collapsed, all its descendants roll up to it. Iterative.
 */
function expandNodeInMap(node: HierarchyNode, expandedNodes: Set<string>, map: Map<string, string>) {
  const stack: HierarchyNode[] = [node]
  while (stack.length > 0) {
    const current = stack.pop()!
    for (const child of current.children) {
      if (child.urn) map.set(child.urn, child.id)
      map.set(child.id, child.id)
      if (expandedNodes.has(child.id)) {
        stack.push(child)
      } else {
        for (const gc of child.children) collapseSubtreeInMap(gc, child.id, map)
      }
    }
  }
}

/** Full O(N) build. Called on initial load and whenever nodesByLayer changes. Iterative. */
function buildFullAncestorMap(
  nodesByLayer: Map<string, HierarchyNode[]>,
  expandedNodes: Set<string>,
  displayFlat: HierarchyNode[],
): Map<string, string> {
  const map = new Map<string, string>()

  const stack: Array<{ node: HierarchyNode; anchor: string }> = []
  nodesByLayer.forEach(roots => {
    for (let i = roots.length - 1; i >= 0; i--) stack.push({ node: roots[i], anchor: roots[i].id })
  })

  while (stack.length > 0) {
    const { node, anchor } = stack.pop()!
    if (node.urn) map.set(node.urn, anchor)
    map.set(node.id, anchor)

    let childAnchor = anchor
    if (node.id === anchor) {
      childAnchor = expandedNodes.has(node.id) ? 'USE_CHILD_ID' : node.id
    }

    for (let i = node.children.length - 1; i >= 0; i--) {
      const child = node.children[i]
      stack.push({ node: child, anchor: childAnchor === 'USE_CHILD_ID' ? child.id : childAnchor })
    }
  }

  // Safety pass: visible nodes always map to themselves
  displayFlat.forEach(node => {
    if (!map.has(node.id)) map.set(node.id, node.id)
    if (node.urn && !map.has(node.urn)) map.set(node.urn, node.id)
  })

  return map
}

// ============================================
// Hook
// ============================================

export function useEdgeProjection({
  edges,
  aggregatedEdges,
  nodesByLayer,
  expandedNodes,
  displayFlat,
  displayMap,
  urnToIdMap,
  showLineageFlow,
  isTracing,
  traceContextSet,
  isContainmentEdge,
  hoveredNodeId,
  suppressedAggEdgeKeys,
  traceAddedEdgeIds,
  traceBundleParentMap,
  entityTypeLevels,
  traceFocusLevel,
  browseBundleEnabled = false,
  browseBundleParentMap,
  browseBundleFanInThreshold = 1,
  nodeLayerIndexMap,
}: UseEdgeProjectionOptions): { lineageEdges: any[], visibleLineageEdges: any[], unresolvedAggregatedCount: number } {

  // Telemetry for silently-dropped aggregated edges whose endpoints can't be
  // resolved through displayMap/ancestorMap/urnToIdMap. Surfacing this lets
  // callers render a "X edges hidden — expand parents to reveal" badge.
  const unresolvedAggregatedRef = useRef(0)
  const lastWarnAtRef = useRef(0)

  // ── Flat node index — O(1) lookup replacing O(N) tree search ──────────
  const nodeIndex = useMemo(() => buildNodeIndex(nodesByLayer), [nodesByLayer])

  // ── Incremental ancestorMap state ──────────────────────────────────────
  const ancestorMapRef = useRef<Map<string, string>>(new Map())
  const prevNodesByLayerRef = useRef<Map<string, HierarchyNode[]> | null>(null)
  const prevExpandedNodesRef = useRef<Set<string>>(new Set())

  // ── lineageEdges ───────────────────────────────────────────────────────
  // Flow is the master switch for edge rendering. Trace mode keeps its node
  // highlights and side panels but respects Flow off — the canvas stays clean
  // when the user wants to inspect a trace path without ambient mesh noise.
  const lineageEdges = useMemo(() => {
    if (!showLineageFlow) return []

    // 1. Aggregated Edges
    const aggEdges = Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'collapsed')
      .map(e => ({
        id: e.aggregated.id,
        source: e.aggregated.sourceUrn,
        target: e.aggregated.targetUrn,
        data: {
          edgeType: 'AGGREGATED',
          relationship: 'aggregated',
          isAggregated: true,
          edgeCount: e.aggregated.edgeCount,
          edgeTypes: e.aggregated.edgeTypes,
          confidence: e.aggregated.confidence,
        }
      }))

    // 2. Expanded Detailed Edges
    const expandedDetailedEdges = Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'expanded')
      .flatMap(e => e.detailedEdges
        .filter((de: any) => !isContainmentEdge(de.edgeType))
        .map((de: any) => ({
          id: de.id,
          source: de.sourceUrn,
          target: de.targetUrn,
          data: {
            edgeType: de.edgeType,
            relationship: de.edgeType,
            confidence: de.confidence,
          }
        })))

    // 3. Regular canvas edges — performance guard: only include edges where
    // at least one endpoint is in displayMap.
    const regularEdges = edges.filter(edge => {
      if (isContainmentEdge(normalizeEdgeType(edge))) return false
      return displayMap.has(edge.source) || displayMap.has(edge.target)
    })

    return [...aggEdges, ...expandedDetailedEdges, ...regularEdges]
  }, [edges, showLineageFlow, aggregatedEdges, isContainmentEdge, displayMap])

  // ── ancestorMap (Phase 5.1 — incremental) ─────────────────────────────
  //
  // Full rebuild: when nodesByLayer reference changes (layer re-assignment,
  // initial data load). This is infrequent.
  //
  // Incremental patch: when only expandedNodes changes (user expands /
  // collapses a tree node). We diff the previous/current Set and only
  // traverse the affected subtrees — O(subtree) instead of O(N).
  const ancestorMap = useMemo(() => {
    const needsFullRebuild = prevNodesByLayerRef.current !== nodesByLayer

    if (needsFullRebuild) {
      const map = buildFullAncestorMap(nodesByLayer, expandedNodes, displayFlat)
      prevNodesByLayerRef.current = nodesByLayer
      prevExpandedNodesRef.current = expandedNodes
      ancestorMapRef.current = map
      return map
    }

    // Same nodesByLayer — check if expandedNodes changed
    const prev = prevExpandedNodesRef.current
    if (prev === expandedNodes) {
      return ancestorMapRef.current
    }

    // Diff
    const expanded: string[] = []
    const collapsed: string[] = []
    expandedNodes.forEach(id => { if (!prev.has(id)) expanded.push(id) })
    prev.forEach(id => { if (!expandedNodes.has(id)) collapsed.push(id) })

    if (expanded.length === 0 && collapsed.length === 0) {
      prevExpandedNodesRef.current = expandedNodes
      return ancestorMapRef.current
    }

    // Shallow copy then patch only changed subtrees
    const map = new Map(ancestorMapRef.current)

    // Collapses first: all descendants → collapsed node
    collapsed.forEach(id => {
      const node = nodeIndex.get(id)
      if (node) {
        node.children.forEach(child => collapseSubtreeInMap(child, id, map))
      }
    })

    // Expansions: children become individually visible
    expanded.forEach(id => {
      const node = nodeIndex.get(id)
      if (node) {
        expandNodeInMap(node, expandedNodes, map)
      }
    })

    prevExpandedNodesRef.current = expandedNodes
    ancestorMapRef.current = map
    return map
  }, [nodesByLayer, expandedNodes, displayFlat, nodeIndex])

  // ── Edge projection ────────────────────────────────────────────────────
  //
  // Now depends on the stable `ancestorMap` instead of rebuilding it here.
  // This memo only re-runs when edges or the ancestorMap actually change.
  const projectedEdges = useMemo(() => {
    if (!showLineageFlow) return []

    const edgeGroups = new Map<string, any[]>()

    const addEdgeToGroup = (sourceId: string, targetId: string, edge: any, type: string) => {
      const groupKey = `${sourceId}->${targetId}`
      if (!edgeGroups.has(groupKey)) edgeGroups.set(groupKey, [])
      edgeGroups.get(groupKey)!.push({ ...edge, source: sourceId, target: targetId, originalType: type })
    }

    // A. Aggregated Edges
    let unresolvedThisPass = 0
    Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'collapsed')
      .forEach(e => {
        const agg = e.aggregated
        // Suppress parent AGG when its drill is producing visible finer-level edges.
        if (isTracing && suppressedAggEdgeKeys?.has(`${agg.sourceUrn}->${agg.targetUrn}`)) return
        let sId = displayMap.has(agg.sourceUrn) ? agg.sourceUrn : ancestorMap.get(agg.sourceUrn)
        let tId = displayMap.has(agg.targetUrn) ? agg.targetUrn : ancestorMap.get(agg.targetUrn)
        if (!sId) sId = urnToIdMap.get(agg.sourceUrn)
        if (!tId) tId = urnToIdMap.get(agg.targetUrn)
        if (sId && tId && sId !== tId) {
          addEdgeToGroup(sId, tId, {
            id: agg.id,
            data: {
              edgeType: 'AGGREGATED',
              relationship: 'aggregated',
              isAggregated: true,
              edgeCount: agg.edgeCount,
              edgeTypes: agg.edgeTypes,
              confidence: agg.confidence,
              sourceEdgeIds: agg.sourceEdgeIds,
            }
          }, 'AGGREGATED')
        } else if (!sId && !tId) {
          unresolvedThisPass++
        }
      })
    unresolvedAggregatedRef.current = unresolvedThisPass
    if (unresolvedThisPass > 0) {
      const now = Date.now()
      if (now - lastWarnAtRef.current > 1000) {
        lastWarnAtRef.current = now
        console.warn(`[useEdgeProjection] ${unresolvedThisPass} aggregated edges hidden — endpoints unresolvable via displayMap/ancestorMap/urnToIdMap`)
      }
    }

    // Trace-mode bundling: walk an endpoint up the canvas containment
    // hierarchy until we land on an ancestor at the focus's trace level
    // (or coarser). This rolls every column-to-column edge up to the
    // object/dataset/schema level the trace ran at, so per-pair bundling
    // (which groups by `${sourceId}->${targetId}`) actually collapses
    // thousands of leaf edges into a handful of container bundles. Without
    // this, expanding a container exposes every leaf's lineage and the
    // canvas renders 10k+ individual edges.
    //
    // Returns null when no ancestor in `traceBundleParentMap` reaches the
    // focus level — that's when we fall back to the regular ancestorMap
    // projection. Disabled outside trace mode and when configuration is
    // missing (so non-trace canvases behave unchanged).
    const bundleEnabled =
      isTracing
      && traceBundleParentMap !== undefined
      && entityTypeLevels !== undefined
      && typeof traceFocusLevel === 'number'
    // Trace renders at exactly the level the user requested. The walk
    // only consolidates endpoints that are FINER than the focus (e.g. a
    // dataset-level focus trace returned a column-level edge because of
    // inherited lineage — walk the column up to its parent dataset). When
    // the endpoint is already at focus level or coarser, the walk exits
    // immediately and the edge keeps its visible endpoints.
    //
    // Visual density at hub-node traces is the renderer's responsibility
    // (the density-adaptive tiers in LineageFlowOverlay), not the
    // projection's. An attribute-level focus must surface as
    // attribute-to-attribute edges — not be silently rolled up to the
    // parent object — or the user can't trust what they're seeing.
    const effectiveBundleCeiling = bundleEnabled ? traceFocusLevel! : 0
    const projectToTraceLevel = (endpointId: string): string | null => {
      if (!bundleEnabled) return null
      let cursor: string | undefined = endpointId
      const seen = new Set<string>()
      while (cursor && !seen.has(cursor)) {
        seen.add(cursor)
        const node = nodeIndex.get(cursor) ?? displayMap.get(cursor)
        const entityType = (node?.data?.type as string | undefined) ?? node?.typeId
        const level = entityType ? entityTypeLevels!.get(entityType) : undefined
        if (level === undefined) {
          return cursor
        }
        if (level <= effectiveBundleCeiling) return cursor
        const parent = traceBundleParentMap!.get(cursor)
        if (!parent) return cursor
        cursor = parent
      }
      return cursor ?? null
    }

    // B. Regular / Trace Edges
    edges
      .filter(edge => !isContainmentEdge(normalizeEdgeType(edge)))
      .forEach(edge => {
        let sId = ancestorMap.get(edge.source) || (displayMap.has(edge.source) ? edge.source : null)
        let tId = ancestorMap.get(edge.target) || (displayMap.has(edge.target) ? edge.target : null)

        if (sId && tId && bundleEnabled) {
          // Apply the trace-level rollup. Result endpoints are always at
          // the focus level (or coarser); ancestor pairs become the
          // visible bundle.
          const bundledS = projectToTraceLevel(sId)
          const bundledT = projectToTraceLevel(tId)
          if (bundledS) sId = bundledS
          if (bundledT) tId = bundledT
        }

        if (sId && tId && sId !== tId) {
          // Trace-merged edges (recorded in addedEdgeIds) bypass the
          // contextSet gate — they're definitionally part of the trace and
          // must render even if one endpoint hasn't been routed into the
          // trace-filtered hierarchy yet. Ambient (non-trace) edges still
          // need both endpoints inside the trace context.
          const isTraceMerged = isTracing && traceAddedEdgeIds?.has(edge.id)
          if (isTracing && !isTraceMerged
              && (!traceContextSet.has(sId) || !traceContextSet.has(tId))) return
          // Suppress drilled parent AGG edges (URN-pair match on the original endpoints).
          if (
            isTracing
            && String((edge.data?.edgeType) ?? '').toUpperCase() === 'AGGREGATED'
            && suppressedAggEdgeKeys?.has(`${edge.source}->${edge.target}`)
          ) return
          addEdgeToGroup(sId, tId, { ...edge, data: edge.data || {} }, normalizeEdgeType(edge))
        }
      })

    // C. Expanded Detailed Edges
    Array.from(aggregatedEdges.values())
      .filter(e => e.state === 'expanded')
      .flatMap(e => e.detailedEdges)
      .forEach(edge => {
        const sId = ancestorMap.get(edge.sourceUrn)
        const tId = ancestorMap.get(edge.targetUrn)
        if (sId && tId && sId !== tId) {
          addEdgeToGroup(sId, tId, {
            id: edge.id,
            data: { edgeType: edge.edgeType, relationship: edge.edgeType, confidence: edge.confidence }
          }, edge.edgeType)
        }
      })

    // ── Browse-mode meta-bundling ─────────────────────────────────────────
    //
    // Trace-mode bundling already rolls up via `projectToTraceLevel` above.
    // For browse mode we run a separate pass over the per-pair `edgeGroups`:
    // when many distinct visible-leaf pairs share a common COLLAPSED
    // containment parent (e.g. 76 Compliance objects → 109 Finance objects,
    // both layers collapsed), the canvas is hopeless. Roll those pairs up
    // to a single parent-pair bundle so the macro flow is legible.
    //
    // CRITICAL — never collapse to an EXPANDED parent. The user's expansion
    // is an explicit request to see leaf-level detail; rolling those edges
    // back into the parent would make the fine-grained lineage disappear
    // the moment they reveal it. The walk only steps to a parent when that
    // parent is collapsed (i.e. is itself the user's chosen view-level).
    //
    // Disabled in trace mode (trace has its own path) and when the parent
    // map / fan-in config is missing.
    if (
      !isTracing
      && browseBundleEnabled
      && browseBundleParentMap !== undefined
      && edgeGroups.size > 0
    ) {
      // Walk one parent step. Iterate up to 6 passes; each pass collapses
      // the highest-fan-in groupings until the count drops under threshold.
      // 6 is enough to walk leaf → object → schema → domain → layer for
      // every realistic ontology depth.
      for (let pass = 0; pass < 6; pass++) {
        // Group existing keys by their (parent-of-source, parent-of-target),
        // but only consider parents that are NOT currently expanded.
        const parentBuckets = new Map<string, string[]>()
        for (const key of edgeGroups.keys()) {
          const [sId, tId] = key.split('->')
          const rawSP = browseBundleParentMap.get(sId)
          const rawTP = browseBundleParentMap.get(tId)
          // Skip the walk on a side whose parent is expanded — that side
          // stays at the visible leaf level (the user explicitly opened it).
          const sP = rawSP && !expandedNodes.has(rawSP) ? rawSP : undefined
          const tP = rawTP && !expandedNodes.has(rawTP) ? rawTP : undefined
          // Only consider buckets where AT LEAST one endpoint has a
          // collapsed parent available. If both are at user-chosen view
          // level, the key stays as-is.
          if (!sP && !tP) continue
          const parentKey = `${sP ?? sId}->${tP ?? tId}`
          if (parentKey === key) continue
          let bucket = parentBuckets.get(parentKey)
          if (!bucket) { bucket = []; parentBuckets.set(parentKey, bucket) }
          bucket.push(key)
        }

        let collapsedAny = false
        parentBuckets.forEach((childKeys, parentKey) => {
          if (childKeys.length <= browseBundleFanInThreshold) return
          // Collapse: merge every child group's edges into one bundle keyed
          // at the parent pair. Stamp with `isBrowseBundle` so the renderer
          // (and future drill UI) can distinguish from per-pair groupings.
          const [sP, tP] = parentKey.split('->')
          if (sP === tP) return  // self-loop at parent level — skip
          const merged: any[] = edgeGroups.get(parentKey) ?? []
          for (const ck of childKeys) {
            const child = edgeGroups.get(ck)
            if (!child) continue
            // Re-key each edge to the new parent endpoints so finalize()
            // pulls source/target from the merged group consistently.
            for (const e of child) merged.push({ ...e, source: sP, target: tP, _browseBundled: true })
            edgeGroups.delete(ck)
          }
          edgeGroups.set(parentKey, merged)
          collapsedAny = true
        })
        if (!collapsedAny) break
      }
    }

    // Finalize: bundle groups into projected edges (without delegation — applied in separate memo)
    const projected: any[] = []
    edgeGroups.forEach((groupEdges, key) => {
      const distinctTypes = new Set<string>()
      let isGhost = false
      let isAggregated = false
      let isBrowseBundle = false
      let maxConfidence = 0

      const sourceId = groupEdges[0].source
      const targetId = groupEdges[0].target

      if (groupEdges.some((e: any) => e.target !== e.originalTargetId || e.source !== e.originalSourceId)) {
        isGhost = true
      }

      groupEdges.forEach(e => {
        if (e.data?.isAggregated) isAggregated = true
        if (e._browseBundled) isBrowseBundle = true
        if (e.data?.edgeTypes) {
          e.data.edgeTypes.forEach((et: string) => distinctTypes.add(et))
        } else if (e.originalType) {
          distinctTypes.add(e.originalType)
        }
        maxConfidence = Math.max(maxConfidence, e.data?.confidence ?? 1)
      })

      const edgeCount = groupEdges.length
      const typesArray = Array.from(distinctTypes)

      // Reverse-flow annotation: layer-index of target strictly less than
      // source means the edge points back upstream against the canonical
      // left→right flow. Renderer routes these through a dedicated lane.
      let isReverseFlow = false
      if (nodeLayerIndexMap) {
        const sLayer = nodeLayerIndexMap.get(sourceId)
        const tLayer = nodeLayerIndexMap.get(targetId)
        if (typeof sLayer === 'number' && typeof tLayer === 'number' && tLayer < sLayer) {
          isReverseFlow = true
        }
      }

      projected.push({
        id: `bundle-${key}`,
        source: sourceId,
        target: targetId,
        isBundled: edgeCount > 1 || isBrowseBundle,
        isBrowseBundle,
        isGhost,
        edgeCount,
        types: typesArray,
        confidence: maxConfidence,
        isAggregated,
        isReverseFlow,
        isDelegated: false,
        isResidual: false,
        isBidirectional: false,
        data: { edgeTypes: typesArray, confidence: maxConfidence, edgeCount }
      })
    })

    // Bidirectional collapse: when projected groups exist for both A→B and
    // B→A, merge into a single bundle stamped `isBidirectional: true`. The
    // canonical orientation is `min(sourceId, targetId) → max(...)` so the
    // renderer has a stable anchor; the dual-arrowhead is the visual cue
    // for two-way flow. Hover/click can still reveal the underlying per-
    // direction edges from `data.edgeTypes` and counts.
    const byPair = new Map<string, { fwd?: any, rev?: any }>()
    projected.forEach(p => {
      const a = p.source, b = p.target
      if (a === b) return
      const canonical = a < b ? `${a}->${b}` : `${b}->${a}`
      const slot = byPair.get(canonical) ?? {}
      if (a < b) slot.fwd = p
      else slot.rev = p
      byPair.set(canonical, slot)
    })

    const merged: any[] = []
    const consumed = new Set<any>()
    byPair.forEach((slot, canonical) => {
      const { fwd, rev } = slot
      if (fwd && rev) {
        const [s, t] = canonical.split('->')
        const types = new Set<string>()
        ;(fwd.types as string[]).forEach(t => types.add(t))
        ;(rev.types as string[]).forEach(t => types.add(t))
        const edgeCount = (fwd.edgeCount as number) + (rev.edgeCount as number)
        const typesArr = Array.from(types)
        merged.push({
          id: `bundle-bi-${canonical}`,
          source: s,
          target: t,
          isBundled: true,
          isBrowseBundle: fwd.isBrowseBundle || rev.isBrowseBundle,
          isGhost: fwd.isGhost && rev.isGhost,
          edgeCount,
          types: typesArr,
          confidence: Math.max(fwd.confidence, rev.confidence),
          isAggregated: fwd.isAggregated || rev.isAggregated,
          isReverseFlow: false,
          isDelegated: false,
          isResidual: false,
          isBidirectional: true,
          data: { edgeTypes: typesArr, confidence: Math.max(fwd.confidence, rev.confidence), edgeCount },
        })
        consumed.add(fwd)
        consumed.add(rev)
      }
    })

    if (consumed.size === 0) return projected
    return [...projected.filter(p => !consumed.has(p)), ...merged]
  }, [ancestorMap, lineageEdges, edges, aggregatedEdges, displayMap, urnToIdMap, showLineageFlow, isTracing, traceContextSet, isContainmentEdge, expandedNodes, suppressedAggEdgeKeys, traceAddedEdgeIds, traceBundleParentMap, entityTypeLevels, traceFocusLevel, nodeIndex, browseBundleEnabled, browseBundleParentMap, browseBundleFanInThreshold, nodeLayerIndexMap])

  // ── Edge delegation — separate memo so hoveredNodeId changes are O(E) not O(expensive) ──
  //
  // The heavy edge projection above doesn't re-run on hover. This cheap pass
  // stamps isDelegated/isResidual on the already-projected edges.
  const visibleLineageEdgesWithDelegation = useMemo(() => {
    if (projectedEdges.length === 0) return projectedEdges

    // Build expanded parent info
    const expandedParentInfo = new Map<string, { isPartiallyLoaded: boolean }>()
    expandedNodes.forEach(nodeId => {
      const node = displayMap.get(nodeId)
      if (!node) return
      const totalChildCount = (node.data?.childCount as number) || (node.data?._collapsedChildCount as number) || 0
      const loadedChildCount = node.children?.length ?? 0
      if (loadedChildCount > 0) {
        expandedParentInfo.set(nodeId, {
          isPartiallyLoaded: totalChildCount > 0 && loadedChildCount < totalChildCount,
        })
      }
    })

    // If no expanded parents with children, skip the mapping
    if (expandedParentInfo.size === 0) return projectedEdges

    return projectedEdges.map(edge => {
      const sourceExpanded = expandedParentInfo.get(edge.source)
      const targetExpanded = expandedParentInfo.get(edge.target)

      if (!sourceExpanded && !targetExpanded) return edge

      const isEndpointHovered = hoveredNodeId === edge.source || hoveredNodeId === edge.target
      const anyPartial = sourceExpanded?.isPartiallyLoaded || targetExpanded?.isPartiallyLoaded

      return {
        ...edge,
        isDelegated: anyPartial ? false : !isEndpointHovered,
        isResidual: anyPartial ? !isEndpointHovered : false,
      }
    })
  }, [projectedEdges, expandedNodes, displayMap, hoveredNodeId])

  return {
    lineageEdges,
    visibleLineageEdges: visibleLineageEdgesWithDelegation,
    unresolvedAggregatedCount: unresolvedAggregatedRef.current,
  }
}
