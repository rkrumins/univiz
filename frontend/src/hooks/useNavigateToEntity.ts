/**
 * useNavigateToEntity — central recipe for "jump to a search hit", shared
 * across the three canvases.
 *
 * Given an `EntitySearchHit` (node + ancestor chain + match info) and a
 * canvas-specific context, this hook:
 *   1. Merges the hit + ancestor nodes into the canvas store (idempotent).
 *   2. Synthesises and merges containment edges along the chain so layer
 *      assignment / hierarchy hooks can place the new nodes correctly.
 *   3. Tells the canvas to expand every ancestor in the chain.
 *   4. Selects the target node so the EntityDrawer opens.
 *   5. Centres the canvas on the target — DOM `scrollIntoView` for the
 *      column-based canvases, ReactFlow `setCenter` for the graph canvas.
 *
 * Each canvas constructs its own `NavigateToEntityContext` and calls
 * `navigate(hit, ctx)`. The hook itself is stateless beyond the store
 * actions it calls, so it can be safely held across renders.
 */
import { useCallback } from 'react'
import type { ReactFlowInstance } from '@xyflow/react'
import { useCanvasStore } from '@/store/canvas'
import { toCanvasNode } from '@/hooks/useGraphHydration'
import { synthesizeContainmentEdges } from '@/lib/synthesizeContainmentEdges'
import type { EntitySearchHit } from '@/providers/GraphDataProvider'

export type NavigateStrategy = 'context-view' | 'hierarchy' | 'graph'

export interface NavigateToEntityContext {
  strategy: NavigateStrategy
  /** Containment edge type to synthesise — usually the canvas's primary one. */
  containmentEdgeType: string
  /** Updates the canvas's expanded-set so ancestor containers reveal the target. */
  setExpandedNodes: (updater: (prev: Set<string>) => Set<string>) => void
  /** Required when `strategy === 'graph'` — used to centre on the target. */
  rfInstance?: ReactFlowInstance | null
}

/**
 * Brief delay between merging nodes and centring the viewport. Layer
 * assignment / ELK layout / animation passes need a render tick to place
 * the new node under its ancestor before `getBoundingClientRect` returns
 * a useful rect.
 */
const POST_MERGE_DELAY_MS = 80

export function useNavigateToEntity() {
  const addNodes = useCanvasStore(s => s.addNodes)
  const addEdges = useCanvasStore(s => s.addEdges)
  const selectNode = useCanvasStore(s => s.selectNode)

  return useCallback(
    async (hit: EntitySearchHit, ctx: NavigateToEntityContext): Promise<void> => {
      const allNodes = [...hit.ancestorChain, hit.node]
      addNodes(allNodes.map(n => toCanvasNode(n)))

      const synthEdges = synthesizeContainmentEdges(
        hit.ancestorChain,
        hit.node,
        ctx.containmentEdgeType,
      )
      // Convert to LineageEdge shape inline (small enough not to warrant a util).
      addEdges(synthEdges.map(e => ({
        id: e.id,
        source: e.sourceUrn,
        target: e.targetUrn,
        data: { edgeType: e.edgeType, relationship: e.edgeType },
      })))

      ctx.setExpandedNodes(prev => {
        const next = new Set(prev)
        for (const ancestor of hit.ancestorChain) next.add(ancestor.urn)
        return next
      })

      selectNode(hit.node.urn)

      // Wait one paint so DOM ids exist + ELK has placed graph nodes.
      await new Promise(resolve => setTimeout(resolve, POST_MERGE_DELAY_MS))

      switch (ctx.strategy) {
        case 'context-view': {
          const el = document.getElementById(`layer-node-${hit.node.urn}`)
          el?.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' })
          break
        }
        case 'hierarchy': {
          const el = document.getElementById(`hierarchy-node-${hit.node.urn}`)
          el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
          break
        }
        case 'graph': {
          const rf = ctx.rfInstance
          if (!rf) break
          // ReactFlow's getNode returns the live, layouted node — its position
          // is what we need; the GraphNode coords from the backend are zeros.
          const rfNode = rf.getNode(hit.node.urn)
          if (rfNode) {
            const x = rfNode.position.x + (rfNode.measured?.width ?? rfNode.width ?? 200) / 2
            const y = rfNode.position.y + (rfNode.measured?.height ?? rfNode.height ?? 80) / 2
            rf.setCenter(x, y, { zoom: 1.2, duration: 400 })
          } else {
            // Fallback: best-effort fit.
            rf.fitView({ padding: 0.3, duration: 400, nodes: [{ id: hit.node.urn }] })
          }
          break
        }
      }
    },
    [addNodes, addEdges, selectNode],
  )
}
