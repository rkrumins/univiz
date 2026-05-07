// Re-export HierarchyNode from shared types for backward compatibility
export type { HierarchyNode } from '@/types/hierarchy'
import type { HierarchyNode } from '@/types/hierarchy'

export interface FlatTreeNode {
  node: HierarchyNode
  depth: number
  isLast: boolean
  parentIsLast: boolean[]  // Track which parents are "last" for proper tree lines
  isLoadMore?: boolean
  loadMoreCount?: number
  isSearchBox?: boolean
  isSkeleton?: boolean
  skeletonIndex?: number
  isFailed?: boolean
}

export type OverflowBadge = {
  /** Horizontal center of the badge in the gutter (relative to container) */
  gutterX: number
  direction: 'up' | 'down'
  count: number
  color: string
}

/** A partial edge drawn from a visible node toward the container boundary,
 *  indicating an off-screen connection in that direction. */
export type OverflowEdge = {
  id: string
  /** SVG path for the trailing curve */
  pathD: string
  color: string
  direction: 'up' | 'down'
  /** Unique gradient ID for the fade mask */
  gradientId: string
  /** Start Y (visible node) and end Y (container edge) for gradient coords */
  sy: number
  ey: number
}

export type ComputedEdge = {
  id: string
  source: string
  target: string
  minY: number
  maxY: number
  pathD: string
  color: string
  dynamicStrokeWidth: number
  edgeOpacity: number
  isGhost: boolean
  isBundled: boolean
  edgeCount: number
  sx: number
  sy: number
  tx: number
  ty: number
  // For tooltip display
  types: string[]
  confidence: number
  /** True when an active trace touches either endpoint — drives the cinematic glow + flow animation. */
  isTraceEdge?: boolean
  /** True when an endpoint is the trace focus — emphasised more strongly. */
  isFocusIncident?: boolean
}
