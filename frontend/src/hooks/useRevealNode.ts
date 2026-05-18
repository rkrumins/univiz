/**
 * useRevealNode — orchestrates "jump to node" from the entity drawer.
 *
 * Given a target node id (URN), this hook:
 *   1. If the target isn't in `canvas.nodes`, calls `provider.getAncestors`
 *      to fetch the chain root→target.parent, adds those ancestor nodes to
 *      the store, then sequentially calls `loadChildren` for each ancestor
 *      (this populates containment edges and the next level's siblings —
 *      including, at the deepest call, the target itself).
 *   2. Walks `parentMap` from the target up, collecting every ancestor not
 *      already in `expandedNodes`, and adds them all in one setState so
 *      layout re-runs once.
 *   3. Waits for layout to settle (two requestAnimationFrames) so node
 *      positions are populated.
 *   4. Calls the canvas-specific `focus(id)` adapter (setCenter on
 *      ReactFlow, scrollIntoView on DOM-based canvases).
 *
 * Errors during getAncestors / loadChildren are logged but swallowed —
 * the drawer-swap already happened, and we don't want a partial reveal to
 * surface as an uncaught error. Out-of-store ids (e.g., synthetic
 * aggregated-edge endpoints) fall through gracefully.
 */

import { useCallback, useRef } from 'react'
import { useCanvasStore } from '@/store/canvas'
import { toCanvasNode } from '@/hooks/useGraphHydration'
import type { GraphDataProvider } from '@/providers/GraphDataProvider'

export interface UseRevealNodeOptions {
  /** Map of childId → parentId (containment). Built by useContainmentHierarchy. */
  parentMap: Map<string, string>
  /** Setter for the canvas's local `expandedNodes` state. */
  setExpandedNodes: React.Dispatch<React.SetStateAction<Set<string>>>
  /** Fetch a single parent's children + containment edges into the store. */
  loadChildren: (parentId: string) => Promise<void>
  /** Canvas-specific pan/scroll adapter. */
  focus: (nodeId: string) => void
  /** Backend lookup for the deep-hidden case. */
  provider: GraphDataProvider
}

export interface RevealOptions {
  /** Skip the canvas-specific focus call. Used by batch flows
   *  (multi-select "Locate on canvas") where the caller will do a single
   *  fitView/scroll at the end instead of N competing per-node scrolls. */
  skipFocus?: boolean
}

export function useRevealNode(
  opts: UseRevealNodeOptions,
): (nodeId: string, revealOpts?: RevealOptions) => Promise<void> {
  // Stash latest opts in a ref so the returned reveal function has a stable
  // identity yet always sees the current parentMap / setters. Without this
  // the callback would re-create every time the parent canvas re-runs the
  // memo that builds parentMap, churning every consumer that captured it.
  const optsRef = useRef(opts)
  optsRef.current = opts

  return useCallback(async (nodeId: string, revealOpts?: RevealOptions) => {
    const { setExpandedNodes, loadChildren, focus, provider } = optsRef.current

    // ── 1. Make sure the target node exists in the store ──────────────────
    const inStore = (id: string) =>
      useCanvasStore.getState().nodes.some((n) => n.id === id)

    if (!inStore(nodeId)) {
      try {
        const ancestors = await provider.getAncestors(nodeId) // root → target.parent
        for (const a of ancestors) {
          if (!inStore(a.urn)) {
            // Drop the ancestor in with a placeholder position so it's
            // available for the containment-edge wiring on the loadChildren
            // call below. Layout will reposition it.
            useCanvasStore.getState().addNodes([toCanvasNode(a)])
          }
          // Fetch this level's children — pulls in the next ancestor (or
          // the target itself, at the deepest call) plus the containment
          // edges that populate parentMap.
          try {
            await loadChildren(a.urn)
          } catch (err) {
            console.warn('[useRevealNode] loadChildren failed for', a.urn, err)
          }
        }
      } catch (err) {
        console.warn('[useRevealNode] getAncestors failed for', nodeId, err)
      }
    }

    // If the target STILL isn't in the store, the chain fetch failed or
    // the id is synthetic (aggregated-edge endpoint). Bail before focus —
    // the drawer-swap already fired, that's the useful side effect.
    if (!inStore(nodeId)) return

    // ── 2. Expand every collapsed ancestor in one update ──────────────────
    // Re-read the freshly-updated parentMap from optsRef in case loadChildren
    // mutated it (it doesn't actually re-render this callback, but the next
    // render's parentMap is what we want anyway — defer to setExpandedNodes's
    // updater to read whatever's current).
    const liveParentMap = optsRef.current.parentMap
    const ancestorIds: string[] = []
    let cursor: string | undefined = liveParentMap.get(nodeId)
    while (cursor) {
      ancestorIds.push(cursor)
      cursor = liveParentMap.get(cursor)
    }

    if (ancestorIds.length > 0) {
      setExpandedNodes((prev) => {
        // Skip the setState if nothing new — avoids a redundant render +
        // layout pass when the chain is already fully expanded.
        let added = false
        const next = new Set(prev)
        for (const id of ancestorIds) {
          if (!next.has(id)) {
            next.add(id)
            added = true
          }
        }
        return added ? next : prev
      })
    }

    // ── 3. Wait for layout to settle ─────────────────────────────────────
    // GraphCanvas's `layoutSignature` effect (elk) and ContextView's edge
    // projection both fire on `expandedNodes` changes. Two rAFs gives them
    // a chance to commit new positions/projections before we pan.
    await new Promise<void>((r) => requestAnimationFrame(() => r()))
    await new Promise<void>((r) => requestAnimationFrame(() => r()))

    // ── 4. Hand off to the canvas-specific focus implementation ──────────
    // Skipped by batch flows ("Locate N on canvas") that prefer a single
    // fitView/scrollTo at the end over N competing per-node scrolls.
    if (!revealOpts?.skipFocus) {
      focus(nodeId)
    }

    // ── 5. Pulse the target so the user sees where they landed ──────────
    // Always fires — for single reveals it marks the freshly-centered
    // node; for batch reveals it marks each one in place so users can
    // spot them after the trailing fitView/scrollTo settles.
    useCanvasStore.getState().pulseNode(nodeId)
  }, [])
}
