import React from 'react'
import { cn } from '@/lib/utils'
import type { ViewLayerConfig } from '@/types/schema'

/* Ghost skeleton card — visual stand-in for a root FlatTreeItem while
 * useGraphHydration is still fetching entities. Mirrors the real card's
 * outer dimensions and layer-tinted icon container exactly so when a real
 * node arrives it slides into the ghost's footprint with no reflow. See
 * FlatTreeItem.tsx (root branch, depth === 0). The same component renders
 * as a depth-1 child skeleton inside expanded parents (replaces the inline
 * skeleton previously inlined in LayerColumn).
 *
 * Animations live in globals.css under the "Ghost-state loading visuals"
 * section (.ghost-shimmer, .ghost-icon-shimmer). prefers-reduced-motion is
 * honoured there. */

export const GHOST_COUNT_PER_LAYER = 5
export const GHOST_ROW_HEIGHT_PX = 52
export const GHOST_STAGGER_MS = 120

const GHOST_TITLE_WIDTHS = [72, 58, 80, 64, 76, 66] as const
const GHOST_BADGE_WIDTHS = [38, 28, 44, 32, 40, 30] as const

const NEUTRAL_LAYER_COLOR = '#7d8aa1'

interface GhostFlatTreeItemProps {
  /** Sequential index — drives deterministic width variation + stagger. */
  index: number
  /** Layer whose color is mirrored on the accent border, icon container, and badge dot. */
  layer: Pick<ViewLayerConfig, 'color'>
  /** 0 = root row (used in the empty-layer ghost stack). 1+ = child skeleton. */
  depth?: number
  /** Optional override of the parentIsLast lines, only used for depth > 0. */
  indentPx?: number
}

export const GhostFlatTreeItem = React.memo(function GhostFlatTreeItem({
  index,
  layer,
  depth = 0,
  indentPx,
}: GhostFlatTreeItemProps) {
  const isRoot = depth === 0
  const color = layer.color ?? NEUTRAL_LAYER_COLOR

  const heightClass = isRoot ? 'min-h-[52px]' : 'min-h-[44px]'
  const paddingClass = isRoot ? 'py-3' : 'py-2.5'
  const iconContainerSize = isRoot ? 'w-9 h-9' : 'w-7 h-7'
  const toggleSlotSize = isRoot ? 'w-7 h-7' : 'w-6 h-6'

  const titleWidth = GHOST_TITLE_WIDTHS[index % GHOST_TITLE_WIDTHS.length]
  const badgeWidth = GHOST_BADGE_WIDTHS[index % GHOST_BADGE_WIDTHS.length]
  const delayMs = index * GHOST_STAGGER_MS

  const indent = indentPx ?? depth * 16
  const cssVars = { ['--ghost-delay' as never]: `${delayMs}ms` } as React.CSSProperties

  return (
    <div
      aria-hidden
      data-canvas-ghost="true"
      className={cn(
        'flex items-center gap-2 mx-1 rounded-xl relative z-[1] select-none',
        heightClass,
        paddingClass,
        'bg-canvas-elevated/10 backdrop-blur-sm',
      )}
      style={{
        paddingLeft: 12 + indent,
        ...(isRoot && { borderLeft: `3px solid ${color}40` }),
        ...cssVars,
      }}
    >
      {/* Toggle-button slot — empty but reserves the same horizontal space
          as the real chevron so real cards slide in without reflow. */}
      <div className={cn('flex-shrink-0', toggleSlotSize)} />

      {/* Icon container — identical classes & inline style to FlatTreeItem
          (root branch). The shimmer overlay replaces the glyph. */}
      <div
        className={cn(
          'rounded-xl flex items-center justify-center flex-shrink-0 shadow-sm relative overflow-hidden',
          iconContainerSize,
        )}
        style={{
          background: `linear-gradient(135deg, ${color}25 0%, ${color}10 100%)`,
          boxShadow: `0 2px 4px ${color}15`,
        }}
      >
        <span className="ghost-icon-shimmer" style={cssVars} />
      </div>

      {/* Title + type-badge stack — mirrors the real two-line layout. The
          layer-color dot is real (not shimmer) so layer identity reads at
          a glance even before any data lands. */}
      <div className="flex-1 min-w-0 flex flex-col justify-center gap-1.5">
        <div
          className="h-3.5 rounded-md ghost-shimmer"
          style={{ width: `${titleWidth}%`, ...cssVars }}
        />
        <div className="flex items-center gap-1.5">
          <span
            className="w-1.5 h-1.5 rounded-full flex-shrink-0"
            style={{ backgroundColor: color }}
          />
          <div
            className="h-2.5 rounded ghost-shimmer"
            style={{ width: `${badgeWidth}%`, ...cssVars }}
          />
        </div>
      </div>
    </div>
  )
})
