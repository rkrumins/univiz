import React, { useState, useLayoutEffect, useRef, useCallback } from 'react'
import type { ViewLayerConfig } from '@/types/schema'
import {
  EXTREMITY_EDGE_GUTTER_PX,
  SAME_COLUMN_LANE_START,
} from './LineageFlowOverlay'
import { GHOST_STAGGER_MS } from './GhostFlatTreeItem'

/* Ghost-edge overlay — dashed pulsing connectors drawn between ghost cards
 * in adjacent layer columns while the canvas is hydrating. Anchored to the
 * real DOM rects of the ghost cards (queried via [data-canvas-ghost]) so
 * the lines land in the same vertical band where real edges will appear.
 * Uses the same EXTREMITY_EDGE_GUTTER_PX inset as the real overlay so the
 * curves enter/exit columns at the same offset real edges will use.
 *
 * No JS animation loop — opacity pulse + dashoffset march are CSS keyframes
 * defined in globals.css (.ghost-edge-path). prefers-reduced-motion is
 * honoured there. */

interface GhostEdgePath {
  d: string
  gradientId: string
  fromColor: string
  toColor: string
  delayMs: number
}

interface GhostLineageOverlayProps {
  /** Sorted layers, in render order (left → right). */
  layers: ViewLayerConfig[]
  /** Ref to the horizontal scroll container that holds the layer columns. */
  containerRef: React.RefObject<HTMLElement | null>
}

const NEUTRAL_LAYER_COLOR = '#7d8aa1'

export const GhostLineageOverlay = React.memo(function GhostLineageOverlay({
  layers,
  containerRef,
}: GhostLineageOverlayProps) {
  const [paths, setPaths] = useState<GhostEdgePath[]>([])
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  const rafRef = useRef<number | null>(null)

  const recompute = useCallback(() => {
    const container = containerRef.current
    if (!container) return

    const containerRect = container.getBoundingClientRect()
    setSize({
      w: container.scrollWidth,
      h: container.scrollHeight,
    })

    // Group ghost-card rects by their owning layer column.
    // GhostFlatTreeItem sets [data-canvas-ghost="true"]; the LayerColumn root
    // sets [data-layer-id="..."]. We query inside the container so we don't
    // pick up ghosts from other canvases that might coexist.
    const byLayer = new Map<string, DOMRect[]>()
    const ghostNodes = container.querySelectorAll<HTMLElement>('[data-canvas-ghost="true"]')
    ghostNodes.forEach((el) => {
      const column = el.closest<HTMLElement>('[data-layer-id]')
      if (!column) return
      const id = column.dataset.layerId
      if (!id) return
      const arr = byLayer.get(id) ?? []
      arr.push(el.getBoundingClientRect())
      byLayer.set(id, arr)
    })

    if (byLayer.size < 2) {
      setPaths([])
      return
    }

    const scrollLeft = container.scrollLeft
    const scrollTop = container.scrollTop
    const toLocal = (rect: DOMRect) => ({
      left: rect.left - containerRect.left + scrollLeft,
      right: rect.right - containerRect.left + scrollLeft,
      midY: rect.top + rect.height / 2 - containerRect.top + scrollTop,
    })

    const nextPaths: GhostEdgePath[] = []

    for (let i = 0; i < layers.length - 1; i++) {
      const fromLayer = layers[i]
      const toLayer = layers[i + 1]
      const fromRects = byLayer.get(fromLayer.id) ?? []
      const toRects = byLayer.get(toLayer.id) ?? []
      if (fromRects.length === 0 || toRects.length === 0) continue

      const pairCount = Math.min(fromRects.length, toRects.length)
      for (let r = 0; r < pairCount; r++) {
        const from = toLocal(fromRects[r])
        const to = toLocal(toRects[r])
        // Entry/exit gutter — same constant the real overlay uses so when
        // a real edge replaces this ghost it enters/exits at the same x.
        const startX = from.right + SAME_COLUMN_LANE_START
        const endX = to.left - SAME_COLUMN_LANE_START
        const startY = from.midY
        const endY = to.midY
        const dx = Math.max(40, (endX - startX) * 0.45)
        const c1x = startX + dx
        const c2x = endX - dx
        const d = `M ${startX.toFixed(1)} ${startY.toFixed(1)} C ${c1x.toFixed(1)} ${startY.toFixed(1)}, ${c2x.toFixed(1)} ${endY.toFixed(1)}, ${endX.toFixed(1)} ${endY.toFixed(1)}`

        const fromColor = fromLayer.color ?? NEUTRAL_LAYER_COLOR
        const toColor = toLayer.color ?? NEUTRAL_LAYER_COLOR
        nextPaths.push({
          d,
          gradientId: `ghost-edge-${fromLayer.id}-${toLayer.id}-${r}`,
          fromColor,
          toColor,
          delayMs: r * GHOST_STAGGER_MS,
        })
      }
    }

    setPaths(nextPaths)
  }, [containerRef, layers])

  const scheduleRecompute = useCallback(() => {
    if (rafRef.current != null) return
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null
      recompute()
    })
  }, [recompute])

  useLayoutEffect(() => {
    recompute()
    const container = containerRef.current
    if (!container) return

    const ro = new ResizeObserver(scheduleRecompute)
    ro.observe(container)
    container.querySelectorAll<HTMLElement>('[data-layer-id]').forEach((el) => ro.observe(el))

    container.addEventListener('scroll', scheduleRecompute, { passive: true })
    window.addEventListener('resize', scheduleRecompute)

    // Ghost cards mount/unmount as hydration proceeds — re-measure briefly
    // after mount so the lines catch up to the actual DOM.
    const t1 = window.setTimeout(scheduleRecompute, 50)
    const t2 = window.setTimeout(scheduleRecompute, 250)

    return () => {
      ro.disconnect()
      container.removeEventListener('scroll', scheduleRecompute)
      window.removeEventListener('resize', scheduleRecompute)
      window.clearTimeout(t1)
      window.clearTimeout(t2)
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
    }
  }, [recompute, scheduleRecompute, containerRef, layers.length])

  if (paths.length === 0 || size.w === 0) return null

  return (
    <svg
      className="absolute inset-0 pointer-events-none"
      style={{ width: size.w, height: size.h, zIndex: 1 }}
      width={size.w}
      height={size.h}
      aria-hidden
    >
      <defs>
        {paths.map((p) => (
          <linearGradient key={p.gradientId} id={p.gradientId} gradientUnits="userSpaceOnUse">
            <stop offset="0%" stopColor={p.fromColor} stopOpacity="0.40" />
            <stop offset="100%" stopColor={p.toColor} stopOpacity="0.40" />
          </linearGradient>
        ))}
      </defs>
      <g style={{ paddingLeft: EXTREMITY_EDGE_GUTTER_PX }}>
        {paths.map((p, i) => (
          <path
            key={`${p.gradientId}-${i}`}
            d={p.d}
            stroke={`url(#${p.gradientId})`}
            className="ghost-edge-path"
            style={{ ['--ghost-delay' as never]: `${p.delayMs}ms` } as React.CSSProperties}
          />
        ))}
      </g>
    </svg>
  )
})
