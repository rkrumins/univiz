/**
 * LayerColumn - Single column in the Context View representing a data layer
 *
 * Features:
 * - Collapsible with vertical text
 * - Breadcrumb navigation for focused subtrees
 * - Virtualized flat tree rendering with expand/collapse (scales to 1000+ items)
 * - Inline search and load-more support
 * - Keyboard navigation (arrow keys, home/end, enter)
 */

import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useVirtualizer } from '@tanstack/react-virtual'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import { DynamicIcon } from '@/components/ui/DynamicIcon'
import { useSchemaStore } from '@/store/schema'
import type { ViewLayerConfig } from '@/types/schema'
import type { HierarchyNode, FlatTreeNode } from './types'
import { FlatTreeItem } from './FlatTreeItem'
import { SearchBoxItem } from './SearchBoxItem'

interface LayerColumnProps {
  layer: ViewLayerConfig
  nodes: HierarchyNode[]
  schema: ReturnType<typeof useSchemaStore.getState>['schema']
  selectedNodeId: string | null
  expandedNodes: Set<string>
  searchResults: string[]
  onSelect: (id: string) => void
  onToggle: (id: string) => void
  onContextMenu: (e: React.MouseEvent, id: string) => void
  onDoubleClick: (id: string, event?: React.MouseEvent) => void
  onAddChild?: (parentId: string) => void
  onAddToLayer?: (layerId: string) => void
  traceFocusId: string | null
  traceNodes: Set<string>
  traceContextSet: Set<string>
  isTracing?: boolean
  highlightedNodes?: Set<string>
  isHighlightActive?: boolean
  isHoverHighlight?: boolean
  onAnimationComplete?: () => void
  onLoadMore?: (parentId: string) => void
  onSearchChildren?: (parentId: string, query: string) => void
  isLoadingChildren?: boolean
  loadingNodes?: Set<string>
  failedNodes?: Set<string>
  onScroll?: () => void
  onAssignToLayer?: (entityId: string) => void
}

// Stable key for each flat tree item (used by virtualizer for measurement cache stability)
function getItemKey(item: FlatTreeNode, _index: number): string {
  if (item.isSkeleton) return `skeleton-${item.node.id}-${item.skeletonIndex}`
  if (item.isSearchBox) return `search-${item.node.id}`
  if (item.isLoadMore) return `loadmore-${item.node.id}`
  if (item.isFailed) return `error-${item.node.id}`
  return item.node.id
}

export const LayerColumn = React.memo(function LayerColumn({
  layer,
  nodes,
  schema,
  selectedNodeId,
  expandedNodes,
  searchResults,
  onSelect,
  onToggle,
  onContextMenu,
  onDoubleClick,
  onAddChild,
  onAddToLayer,
  traceFocusId,
  traceNodes: _traceNodes,
  traceContextSet,
  isTracing = false,
  highlightedNodes,
  isHighlightActive = false,
  isHoverHighlight = false,
  onAnimationComplete: _onAnimationComplete,
  onLoadMore,
  onSearchChildren,
  isLoadingChildren,
  loadingNodes,
  failedNodes,
  onScroll,
  onAssignToLayer,
}: LayerColumnProps) {
  const [localFocusId, setLocalFocusId] = useState<string | null>(null)
  const [breadcrumb, setBreadcrumb] = useState<HierarchyNode[]>([])
  const [isCollapsed, setIsCollapsed] = useState(false)
  const [childSearchQueries, setChildSearchQueries] = useState<Record<string, string>>({})
  const [activeSearchNodes, setActiveSearchNodes] = useState<Set<string>>(new Set())
  const [isDragOver, setIsDragOver] = useState(false)
  const [focusIndex, setFocusIndex] = useState(-1)
  const scrollContainerRef = useRef<HTMLDivElement>(null)

  const toggleSearchNode = useCallback((nodeId: string) => {
    setActiveSearchNodes(prev => {
      const next = new Set(prev)
      if (next.has(nodeId)) {
        next.delete(nodeId)
        // Also optionally clear the search query if closed
        setChildSearchQueries(q => {
          const newQ = { ...q }
          delete newQ[nodeId]
          return newQ
        })
      } else {
        next.add(nodeId)
      }
      return next
    })

    // Auto-expand the node so the user immediately sees the search box drop down
    if (!activeSearchNodes.has(nodeId) && !expandedNodes.has(nodeId)) {
      onToggle(nodeId)
    }
  }, [activeSearchNodes, expandedNodes, onToggle])

  // Build flat tree from hierarchy (visible items only)
  const flatTree = useMemo(() => {
    const result: FlatTreeNode[] = []

    // Iterative findNode — prevents stack overflow on deep hierarchies
    let rootNodes = nodes
    if (localFocusId) {
      const findStack = [...nodes]
      rootNodes = []
      while (findStack.length > 0) {
        const n = findStack.pop()!
        if (n.id === localFocusId) { rootNodes = [n]; break }
        for (let i = n.children.length - 1; i >= 0; i--) findStack.push(n.children[i])
      }
    }

    // Iterative flat-tree builder using explicit stack
    type FrameItem =
      | { kind: 'node'; node: HierarchyNode; depth: number; isLast: boolean; parentIsLast: boolean[] }
      | { kind: 'loadMore'; parent: HierarchyNode; depth: number; parentIsLast: boolean[]; count: number }

    const stack: FrameItem[] = []
    // Push root nodes in reverse so first root is processed first
    for (let i = rootNodes.length - 1; i >= 0; i--) {
      stack.push({ kind: 'node', node: rootNodes[i], depth: 0, isLast: i === rootNodes.length - 1, parentIsLast: [] })
    }

    while (stack.length > 0) {
      const frame = stack.pop()!

      if (frame.kind === 'loadMore') {
        result.push({
          node: frame.parent,
          depth: frame.depth,
          isLast: true,
          parentIsLast: frame.parentIsLast,
          isLoadMore: true,
          loadMoreCount: frame.count,
        })
        continue
      }

      const { node, depth, isLast, parentIsLast } = frame
      result.push({ node, depth, isLast, parentIsLast: [...parentIsLast] })

      // Only expand children if node is expanded
      if (!expandedNodes.has(node.id) || (node.children.length === 0 && !((node.data.childCount as number) || 0))) continue

      const childCount = (node.data.childCount as number) || (node.data._collapsedChildCount as number) || node.children.length
      const isNodeLoading = loadingNodes?.has(node.id) ?? false
      const childParentIsLast = [...parentIsLast, isLast]

      // Inline search box
      if (activeSearchNodes.has(node.id)) {
        result.push({
          node,
          depth: depth + 1,
          isLast: node.children.length === 0 && !isNodeLoading,
          parentIsLast: childParentIsLast,
          isSearchBox: true,
        })
      }

      // Error row
      const isNodeFailed = (failedNodes?.has(node.id) ?? false) && !isNodeLoading && node.children.length === 0
      if (isNodeFailed) {
        result.push({
          node,
          depth: depth + 1,
          isLast: true,
          parentIsLast: childParentIsLast,
          isFailed: true,
        })
      }
      // Skeleton placeholders
      else if (isNodeLoading && node.children.length === 0) {
        const skeletonCount = Math.min(childCount || 3, 4)
        for (let i = 0; i < skeletonCount; i++) {
          result.push({
            node,
            depth: depth + 1,
            isLast: i === skeletonCount - 1,
            parentIsLast: childParentIsLast,
            isSkeleton: true,
            skeletonIndex: i,
          })
        }
      } else {
        // Push children onto stack in reverse order (+ optional loadMore at bottom)
        const displayChildren = node.children
        const activeQuery = childSearchQueries[node.id]?.trim().toLowerCase()
        // In trace mode the trace API already returns the complete set of
        // trace-relevant nodes; pulling more siblings just produces noise that
        // useTraceFilteredHierarchy hides anyway. Suppress the "X more" pill.
        const hasMore = !isTracing && node.children.length < childCount && !activeQuery

        if (hasMore) {
          stack.push({ kind: 'loadMore', parent: node, depth: depth + 1, parentIsLast: childParentIsLast, count: childCount - node.children.length })
        }

        for (let i = displayChildren.length - 1; i >= 0; i--) {
          stack.push({
            kind: 'node',
            node: displayChildren[i],
            depth: depth + 1,
            isLast: i === displayChildren.length - 1 && !hasMore,
            parentIsLast: childParentIsLast,
          })
        }
      }
    }

    return result
  }, [nodes, expandedNodes, localFocusId, activeSearchNodes, childSearchQueries, loadingNodes, failedNodes, isTracing])

  // Count total including nested
  const totalCount = useMemo(() => {
    const count = (n: HierarchyNode): number =>
      1 + n.children.reduce((acc, c) => acc + count(c), 0)
    return nodes.reduce((acc, n) => acc + count(n), 0)
  }, [nodes])

  // Handle focus (zoom into subtree)
  const handleFocus = useCallback((node: HierarchyNode | null) => {
    if (!node) {
      setLocalFocusId(null)
      setBreadcrumb([])
      return
    }

    // Build breadcrumb trail
    const trail: HierarchyNode[] = []
    const findPath = (n: HierarchyNode, target: string, path: HierarchyNode[]): boolean => {
      if (n.id === target) {
        trail.push(...path, n)
        return true
      }
      for (const child of n.children) {
        if (findPath(child, target, [...path, n])) return true
      }
      return false
    }

    nodes.forEach(root => findPath(root, node.id, []))

    setLocalFocusId(node.id)
    setBreadcrumb(trail.slice(0, -1)) // Exclude current node from breadcrumb

    // Auto-expand the focused node
    if (!expandedNodes.has(node.id)) {
      onToggle(node.id)
    }
  }, [nodes, expandedNodes, onToggle])

  // Navigate breadcrumb
  const handleBreadcrumbClick = useCallback((node: HierarchyNode | null) => {
    if (!node) {
      handleFocus(null)
    } else {
      handleFocus(node)
    }
  }, [handleFocus])

  // ── 4.5 Keyboard Navigation ───────────────────────────────────────────────
  // Only the real FlatTreeItem rows (no skeletons, errors, search boxes, load-more)
  const navigableItems = useMemo(
    () => flatTree.filter(item => !item.isSearchBox && !item.isSkeleton && !item.isFailed && !item.isLoadMore),
    [flatTree]
  )

  // O(1) lookup: node ID → navigable index
  const navigableIndexMap = useMemo(() => {
    const map = new Map<string, number>()
    navigableItems.forEach((item, idx) => map.set(item.node.id, idx))
    return map
  }, [navigableItems])

  // O(1) lookup: node ID → flatTree index (for virtualizer.scrollToIndex)
  const nodeToFlatIndexMap = useMemo(() => {
    const map = new Map<string, number>()
    flatTree.forEach((item, idx) => {
      if (!item.isSkeleton && !item.isSearchBox && !item.isFailed && !item.isLoadMore) {
        map.set(item.node.id, idx)
      }
    })
    return map
  }, [flatTree])

  // ── Animation batching: track which items are newly appeared (cap at 20) ──
  const prevFlatTreeKeysRef = useRef<Set<string>>(new Set())
  const newItemKeys = useMemo(() => {
    const currentKeys = new Set(flatTree.map((item, idx) => getItemKey(item, idx)))
    const prevKeys = prevFlatTreeKeysRef.current
    const newKeys = new Set<string>()
    for (const key of currentKeys) {
      if (!prevKeys.has(key)) {
        newKeys.add(key)
        if (newKeys.size >= 20) break // Cap animation batch for perf
      }
    }
    prevFlatTreeKeysRef.current = currentKeys
    return newKeys
  }, [flatTree])

  // Track tree structure changes — enable glide transition briefly after expand/collapse,
  // but NOT during scroll (which also updates translateY on virtual items).
  const isGlidingRef = useRef(false)
  const glideTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => {
    isGlidingRef.current = true
    clearTimeout(glideTimerRef.current)
    glideTimerRef.current = setTimeout(() => { isGlidingRef.current = false }, 300)
    return () => clearTimeout(glideTimerRef.current)
  }, [flatTree.length])

  // Reset focus when tree content changes
  useEffect(() => {
    setFocusIndex(-1)
  }, [nodes, localFocusId])

  // ── Virtualizer ───────────────────────────────────────────────────────────
  const virtualizer = useVirtualizer({
    count: flatTree.length,
    getScrollElement: () => scrollContainerRef.current,
    estimateSize: (index) => {
      const item = flatTree[index]
      if (item.isSearchBox) return 40
      if (item.isSkeleton) return 44
      if (item.isFailed) return 40
      if (item.isLoadMore) return 40
      return item.depth === 0 ? 52 : 44
    },
    overscan: 15,
    getItemKey: (index) => getItemKey(flatTree[index], index),
  })

  // Auto-scroll keyboard-focused row into view via virtualizer
  const focusedNodeId = navigableItems[focusIndex]?.node.id ?? null
  useEffect(() => {
    if (!focusedNodeId) return
    const flatIndex = nodeToFlatIndexMap.get(focusedNodeId)
    if (flatIndex !== undefined) {
      virtualizer.scrollToIndex(flatIndex, { align: 'auto', behavior: 'smooth' })
    }
  }, [focusedNodeId, nodeToFlatIndexMap, virtualizer])

  // Auto-scroll trace focus node into view — runs ONCE per focus change.
  // Without the ref guard the effect re-fires every time nodeToFlatIndexMap
  // re-memoizes (which happens during scroll-driven virtualizer reflows),
  // snapping the user back to the focus and preventing them from scrolling
  // up or down through the lineage. With the guard the focus is centered
  // when a trace starts; afterwards the user's scroll position is theirs.
  const lastCenteredFocusRef = useRef<string | null>(null)
  useEffect(() => {
    if (!traceFocusId) {
      lastCenteredFocusRef.current = null
      return
    }
    if (lastCenteredFocusRef.current === traceFocusId) return
    const flatIndex = nodeToFlatIndexMap.get(traceFocusId)
    if (flatIndex === undefined) return
    const timer = setTimeout(() => {
      virtualizer.scrollToIndex(flatIndex, { align: 'center', behavior: 'smooth' })
      lastCenteredFocusRef.current = traceFocusId
    }, 100)
    return () => clearTimeout(timer)
  }, [traceFocusId, nodeToFlatIndexMap, virtualizer])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    const count = navigableItems.length
    if (count === 0) return
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        setFocusIndex(i => Math.min(i + 1, count - 1))
        break
      case 'ArrowUp':
        e.preventDefault()
        setFocusIndex(i => (i <= 0 ? 0 : i - 1))
        break
      case 'ArrowRight': {
        e.preventDefault()
        const item = navigableItems[focusIndex]
        if (item && !expandedNodes.has(item.node.id)) onToggle(item.node.id)
        break
      }
      case 'ArrowLeft': {
        e.preventDefault()
        const item = navigableItems[focusIndex]
        if (item && expandedNodes.has(item.node.id)) onToggle(item.node.id)
        break
      }
      case 'Enter': {
        const item = navigableItems[focusIndex]
        if (item) onSelect(item.node.id)
        break
      }
      case 'Home':
        e.preventDefault()
        setFocusIndex(0)
        break
      case 'End':
        e.preventDefault()
        setFocusIndex(count - 1)
        break
    }
  }, [navigableItems, focusIndex, expandedNodes, onToggle, onSelect])

  // Get total items at current level
  const visibleCount = flatTree.length

  // ── Overflow chips: track scroll position so we can show accurate
  // "↑ N above / ↓ N below" indicators that respond to user scroll. ─────────
  const [scrollTick, setScrollTick] = useState(0)
  const handleScroll = useCallback(() => {
    onScroll?.()
    // Bump tick so the memoized counts re-derive from fresh scrollTop/clientHeight.
    setScrollTick(t => (t + 1) & 0xffff)
  }, [onScroll])

  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setScrollTick(t => (t + 1) & 0xffff))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const isRealRow = useCallback((it: FlatTreeNode) =>
    !it.isSkeleton && !it.isSearchBox && !it.isFailed && !it.isLoadMore
  , [])

  const overflowCounts = useMemo(() => {
    void scrollTick
    const el = scrollContainerRef.current
    if (!el || flatTree.length === 0) return { above: 0, below: 0 }
    const scrollTop = el.scrollTop
    const viewportBottom = scrollTop + el.clientHeight
    const items = virtualizer.getVirtualItems()
    if (items.length === 0) return { above: 0, below: 0 }

    let firstVisibleFlatIndex = -1
    let lastVisibleFlatIndex = -1
    for (const it of items) {
      const startsBeforeBottom = it.start < viewportBottom - 1
      const endsAfterTop = it.end > scrollTop + 1
      if (startsBeforeBottom && endsAfterTop) {
        if (firstVisibleFlatIndex === -1) firstVisibleFlatIndex = it.index
        lastVisibleFlatIndex = it.index
      }
    }
    if (firstVisibleFlatIndex === -1) return { above: 0, below: 0 }

    let above = 0
    for (let i = 0; i < firstVisibleFlatIndex; i++) {
      if (isRealRow(flatTree[i])) above++
    }
    let below = 0
    for (let i = lastVisibleFlatIndex + 1; i < flatTree.length; i++) {
      if (isRealRow(flatTree[i])) below++
    }
    return { above, below }
  }, [scrollTick, flatTree, virtualizer, isRealRow])

  const scrollToFlatIndex = useCallback((index: number, align: 'start' | 'end') => {
    if (index < 0 || index >= flatTree.length) return
    virtualizer.scrollToIndex(index, { align, behavior: 'smooth' })
  }, [virtualizer, flatTree.length])

  return (
    <motion.div
      data-layer-id={layer.id}
      className={cn(
        "flex flex-col relative group/column transition-all duration-300",
        isCollapsed ? "min-w-[60px] max-w-[60px]" : "flex-1 min-w-[320px] max-w-[480px]"
      )}
      layout
    >
      {/* Subtle column separator line with gradient fade */}
      <div className="absolute right-0 top-0 bottom-0 w-px bg-gradient-to-b from-transparent via-glass-border/50 to-transparent" />

      {/* Layer Header - Glass morphism style + drag target (4.3) */}
      <div
        className={cn(
          "flex-shrink-0 sticky top-0 z-10 backdrop-blur-xl border-b cursor-pointer transition-all duration-200",
          isCollapsed ? "px-2 py-4" : "px-4 py-3",
          isDragOver
            ? "border-white/30"
            : "border-white/[0.08] dark:border-white/[0.05]"
        )}
        style={{
          background: `linear-gradient(135deg, ${layer.color}12 0%, ${layer.color}05 100%)`,
          boxShadow: isDragOver ? `inset 0 0 0 2px ${layer.color}80, 0 0 20px ${layer.color}20` : undefined,
        }}
        onClick={() => isCollapsed && setIsCollapsed(false)}
        onDragOver={(e) => {
          e.preventDefault()
          e.dataTransfer.dropEffect = 'move'
          setIsDragOver(true)
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node)) setIsDragOver(false)
        }}
        onDrop={(e) => {
          e.preventDefault()
          setIsDragOver(false)
          const entityId = e.dataTransfer.getData('text/x-entity-id')
          if (entityId && onAssignToLayer) onAssignToLayer(entityId)
        }}
      >
        {/* Drop hint overlay */}
        {isDragOver && (
          <div
            className="absolute inset-0 flex items-center justify-center rounded-sm pointer-events-none"
            style={{ backgroundColor: `${layer.color}15` }}
          >
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-black/40 border border-white/20 backdrop-blur-sm">
              <LucideIcons.MoveRight className="w-3.5 h-3.5" style={{ color: layer.color }} />
              <span className="text-xs font-medium" style={{ color: layer.color }}>
                Move to {layer.name}
              </span>
            </div>
          </div>
        )}
        <div className={cn(
          "flex items-center",
          isCollapsed ? "flex-col gap-3" : "gap-3"
        )}>
          {/* Collapse/Expand Toggle + Icon Container */}
          <div className="flex items-center gap-2">
            {!isCollapsed && (
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  setIsCollapsed(true)
                }}
                className="p-1 rounded-lg hover:bg-white/[0.1] text-ink-muted hover:text-ink transition-all"
                title="Collapse layer"
              >
                <LucideIcons.PanelLeftClose className="w-4 h-4" />
              </button>
            )}
            <div
              className={cn(
                "rounded-xl flex items-center justify-center flex-shrink-0 shadow-sm transition-all duration-300",
                isCollapsed ? "w-10 h-10" : "w-9 h-9 group-hover/column:scale-105 group-hover/column:shadow-md"
              )}
              style={{
                background: `linear-gradient(145deg, ${layer.color}25 0%, ${layer.color}15 100%)`,
                boxShadow: `0 2px 8px ${layer.color}20`
              }}
            >
              <DynamicIcon
                name={layer.icon ?? 'Layers'}
                className={cn(
                  "transition-transform duration-300",
                  isCollapsed ? "w-5 h-5" : "w-4 h-4 group-hover/column:scale-110"
                )}
                style={{ color: layer.color }}
              />
            </div>
          </div>

          {/* Collapsed state - vertical text */}
          {isCollapsed ? (
            <div className="flex flex-col items-center gap-2">
              <span
                className="text-xs font-semibold writing-mode-vertical transform rotate-180"
                style={{ color: layer.color, writingMode: 'vertical-rl' }}
              >
                {layer.name}
              </span>
              <div
                className="px-1.5 py-1 rounded-full text-[10px] font-semibold"
                style={{ backgroundColor: `${layer.color}20`, color: layer.color }}
              >
                {totalCount}
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  setIsCollapsed(false)
                }}
                className="p-1.5 rounded-lg hover:bg-white/[0.1] text-ink-muted hover:text-ink transition-all mt-2"
                title="Expand layer"
              >
                <LucideIcons.PanelLeftOpen className="w-4 h-4" />
              </button>
            </div>
          ) : (
            <>
              <div className="flex-1 min-w-0">
                <h3
                  className="text-sm font-semibold truncate tracking-tight"
                  style={{ color: layer.color }}
                >
                  {layer.name}
                </h3>
                {layer.description && (
                  <p className="text-[10px] text-ink-muted/70 truncate mt-0.5">{layer.description}</p>
                )}
              </div>
              <div className="flex items-center gap-2">
                {/* Entity count pill */}
                <div className="flex items-center gap-1 px-2 py-1 rounded-full bg-white/[0.06] dark:bg-white/[0.04] backdrop-blur-sm border border-white/[0.08]">
                  <span className="text-[10px] font-semibold text-ink" style={{ color: layer.color }}>
                    {visibleCount}
                  </span>
                  <span className="text-[9px] text-ink-muted/60">/</span>
                  <span className="text-[10px] text-ink-muted/60">{totalCount}</span>
                </div>
                {onAddToLayer && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      onAddToLayer(layer.id)
                    }}
                    className="p-1.5 rounded-lg bg-green-500/10 hover:bg-green-500/20 text-green-500 transition-all duration-200 hover:scale-110 active:scale-95"
                    title={`Add entity to ${layer.name}`}
                  >
                    <LucideIcons.Plus className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            </>
          )}
        </div>

        {/* Breadcrumb Navigation - Modern pill style (hidden when collapsed) */}
        {!isCollapsed && (
          <AnimatePresence>
            {breadcrumb.length > 0 && (
              <motion.div
                initial={{ opacity: 0, height: 0, marginTop: 8 }}
                animate={{ opacity: 1, height: 'auto', marginTop: 8 }}
                exit={{ opacity: 0, height: 0, marginTop: 0 }}
                className="flex items-center gap-1 overflow-x-auto no-scrollbar"
              >
                <button
                  onClick={() => handleBreadcrumbClick(null)}
                  className="flex items-center gap-1 px-2 py-1 rounded-lg bg-white/[0.06] hover:bg-white/[0.12] border border-white/[0.08] text-ink-muted hover:text-ink transition-all duration-200 flex-shrink-0"
                >
                  <LucideIcons.Home className="w-3 h-3" />
                  <span className="text-[10px] font-medium">Root</span>
                </button>
                {breadcrumb.map((node) => (
                  <React.Fragment key={node.id}>
                    <LucideIcons.ChevronRight className="w-3 h-3 text-ink-muted/40 flex-shrink-0" />
                    <button
                      onClick={() => handleBreadcrumbClick(node)}
                      className="px-2 py-1 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.06] text-ink-muted hover:text-ink transition-all duration-200 truncate max-w-[100px] flex-shrink-0 text-[10px] font-medium"
                      title={node.name}
                    >
                      {node.name}
                    </button>
                  </React.Fragment>
                ))}
                <LucideIcons.ChevronRight className="w-3 h-3 text-ink-muted/40 flex-shrink-0" />
                <span
                  className="px-2 py-1 rounded-lg text-[10px] font-semibold truncate"
                  style={{ backgroundColor: `${layer.color}20`, color: layer.color }}
                >
                  Current
                </span>
              </motion.div>
            )}
          </AnimatePresence>
        )}
      </div>

      {/* Flat Tree Content - Virtualized, hidden when collapsed */}
      {!isCollapsed && (
        <div className="flex-1 relative flex flex-col min-h-0">
          {/* Top overflow chip — shown when items are clipped above the viewport.
              Lives outside the scroll container so it stays anchored to the
              viewport edge instead of scrolling with content. */}
          <AnimatePresence>
            {overflowCounts.above > 0 && (
              <motion.button
                key="above"
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.15, ease: [0.4, 0, 0.2, 1] }}
                onClick={() => scrollToFlatIndex(0, 'start')}
                className="absolute top-2 left-1/2 -translate-x-1/2 z-20 flex items-center gap-1.5 px-2.5 py-1 rounded-full backdrop-blur-md border border-white/10 shadow-md text-[11px] font-medium pointer-events-auto hover:scale-105 active:scale-95 transition-transform"
                style={{
                  backgroundColor: `${layer.color}22`,
                  color: layer.color,
                  boxShadow: `0 4px 14px ${layer.color}25`,
                }}
                title="Scroll to top"
              >
                <LucideIcons.ChevronUp className="w-3 h-3" />
                <span className="tabular-nums">{overflowCounts.above} above</span>
              </motion.button>
            )}
          </AnimatePresence>

          {/* Bottom overflow chip — shown when items are clipped below the viewport. */}
          <AnimatePresence>
            {overflowCounts.below > 0 && (
              <motion.button
                key="below"
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 6 }}
                transition={{ duration: 0.15, ease: [0.4, 0, 0.2, 1] }}
                onClick={() => scrollToFlatIndex(flatTree.length - 1, 'end')}
                className="absolute bottom-2 left-1/2 -translate-x-1/2 z-20 flex items-center gap-1.5 px-2.5 py-1 rounded-full backdrop-blur-md border border-white/10 shadow-md text-[11px] font-medium pointer-events-auto hover:scale-105 active:scale-95 transition-transform"
                style={{
                  backgroundColor: `${layer.color}22`,
                  color: layer.color,
                  boxShadow: `0 4px 14px ${layer.color}25`,
                }}
                title="Scroll to bottom"
              >
                <LucideIcons.ChevronDown className="w-3 h-3" />
                <span className="tabular-nums">{overflowCounts.below} below</span>
              </motion.button>
            )}
          </AnimatePresence>

          <div
            ref={scrollContainerRef}
            onScroll={handleScroll}
            onKeyDown={handleKeyDown}
            tabIndex={0}
            className="flex-1 overflow-y-auto overflow-x-hidden custom-scrollbar relative outline-none focus-visible:ring-1 focus-visible:ring-accent-lineage/30 focus-visible:ring-inset"
          >
          {/* Subtle top fade for scroll indication — slimmer now that the
              floating chip handles the indicator role. */}
          <div className="absolute top-0 left-0 right-0 h-3 bg-gradient-to-b from-canvas/80 to-transparent pointer-events-none z-10" />

          {flatTree.length === 0 ? (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex flex-col items-center justify-center py-16 px-4"
            >
              <div
                className="w-16 h-16 rounded-2xl flex items-center justify-center mb-4"
                style={{ backgroundColor: `${layer.color}10` }}
              >
                <LucideIcons.FolderOpen
                  className="w-8 h-8"
                  style={{ color: `${layer.color}40` }}
                />
              </div>
              <p className="text-sm font-medium text-ink-muted/60">No entities yet</p>
              <p className="text-xs text-ink-muted/40 mt-1">Click + to add entities</p>
            </motion.div>
          ) : (
            <div
              className="py-2 px-1 w-full"
              style={{
                height: `${virtualizer.getTotalSize()}px`,
                position: 'relative',
              }}
            >
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const item = flatTree[virtualRow.index]
                const itemKey = getItemKey(item, virtualRow.index)
                const isNew = newItemKeys.has(itemKey)

                // Shared absolute positioning for the measured container
                // Glide transition only during expand/collapse (not scroll)
                const virtualStyle: React.CSSProperties = {
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  transform: `translateY(${virtualRow.start}px)`,
                  ...(isGlidingRef.current && !isNew && {
                    transition: 'transform 0.15s ease-out',
                  }),
                }

                // Inner animation wrapper style — applied INSIDE the measured div
                // so scale/opacity don't affect virtualizer measurements
                const animStyle: React.CSSProperties | undefined = isNew ? {
                  animation: `flatTreeSlideIn 0.2s cubic-bezier(0.25, 0.46, 0.45, 0.94) backwards`,
                  animationDelay: `${Math.min(virtualRow.index * 0.02, 0.3)}s`,
                  transformOrigin: 'left center',
                } : undefined

                // Error row — shown when loadChildren failed
                if (item.isFailed) {
                  const indentWidth = item.depth * 16
                  return (
                    <div
                      key={itemKey}
                      data-index={virtualRow.index}
                      ref={virtualizer.measureElement}
                      style={virtualStyle}
                    >
                      <div
                        style={animStyle}
                        className="flex items-center gap-2 mx-1 rounded-xl px-3 py-2 cursor-pointer group/error"
                        onClick={() => onLoadMore && onLoadMore(item.node.id)}
                      >
                        <div style={{ paddingLeft: 12 + indentWidth }} className="flex items-center gap-2">
                          <div className="w-6 h-6 flex-shrink-0 flex items-center justify-center">
                            <LucideIcons.AlertCircle className="w-3.5 h-3.5 text-red-400/70" />
                          </div>
                          <span className="text-xs text-red-400/70 group-hover/error:text-red-400 transition-colors">
                            Failed to load — click to retry
                          </span>
                        </div>
                      </div>
                    </div>
                  )
                }

                // Skeleton loading placeholder
                if (item.isSkeleton) {
                  const indentWidth = item.depth * 16
                  const skeletonAnimStyle: React.CSSProperties | undefined = isNew ? {
                    animation: `flatTreeSkeletonGrow 0.22s cubic-bezier(0.25, 0.46, 0.45, 0.94) backwards`,
                    animationDelay: `${(item.skeletonIndex ?? 0) * 0.06}s`,
                    transformOrigin: 'left top',
                    overflow: 'hidden',
                  } : undefined
                  return (
                    <div
                      key={itemKey}
                      data-index={virtualRow.index}
                      ref={virtualizer.measureElement}
                      style={virtualStyle}
                    >
                      <div style={skeletonAnimStyle} className="mx-1 rounded-xl overflow-hidden">
                        <div style={{ paddingLeft: 12 + indentWidth }} className="flex items-center gap-2 w-full py-2">
                          {/* Skeleton chevron area */}
                          <div className="w-6 h-6 flex-shrink-0" />
                          {/* Skeleton icon */}
                          <div
                            className="w-7 h-7 rounded-xl flex-shrink-0 animate-pulse"
                            style={{ backgroundColor: `${layer.color}15` }}
                          />
                          {/* Skeleton text lines */}
                          <div className="flex-1 min-w-0 flex flex-col gap-1.5">
                            <div
                              className="h-3.5 rounded-lg animate-pulse"
                              style={{ backgroundColor: `${layer.color}12`, width: `${55 + ((item.skeletonIndex ?? 0) * 13) % 35}%` }}
                            />
                            <div
                              className="h-2.5 rounded-md animate-pulse w-16"
                              style={{ backgroundColor: `${layer.color}08` }}
                            />
                          </div>
                        </div>
                      </div>
                    </div>
                  )
                }

                if (item.isSearchBox) {
                  return (
                    <div
                      key={itemKey}
                      data-index={virtualRow.index}
                      ref={virtualizer.measureElement}
                      style={virtualStyle}
                    >
                      <div style={isNew ? {
                        animation: `flatTreeFadeIn 0.15s cubic-bezier(0.25, 0.46, 0.45, 0.94) backwards`,
                      } : undefined}>
                        <SearchBoxItem
                          parentId={item.node.id}
                          depth={item.depth}
                          parentIsLast={item.parentIsLast}
                          value={childSearchQueries[item.node.id] || ''}
                          onChange={(val) => {
                            setChildSearchQueries(prev => ({ ...prev, [item.node.id]: val }))
                            if (val.trim()) {
                              onSearchChildren && onSearchChildren(item.node.id, val)
                            } else {
                              // If search is cleared, refetch the original children
                              onLoadMore && onLoadMore(item.node.id)
                            }
                          }}
                          isLoading={isLoadingChildren}
                          layer={layer}
                        />
                      </div>
                    </div>
                  )
                }

                if (item.isLoadMore) {
                  return (
                    <div
                      key={itemKey}
                      data-index={virtualRow.index}
                      ref={virtualizer.measureElement}
                      style={virtualStyle}
                    >
                      <AutoLoadSentinel
                        nodeId={item.node.id}
                        depth={item.depth}
                        parentIsLast={item.parentIsLast}
                        remainingCount={item.loadMoreCount!}
                        onLoadMore={() => onLoadMore && onLoadMore(item.node.id)}
                        isLoading={loadingNodes?.has(item.node.id) ?? false}
                        layerColor={layer.color ?? '#6b7280'}
                      />
                    </div>
                  )
                }

                // Regular FlatTreeItem — animation wrapper inside measured container
                const { node, depth, isLast, parentIsLast } = item
                const navIdx = navigableIndexMap.get(node.id) ?? -1
                return (
                  <div
                    key={itemKey}
                    data-index={virtualRow.index}
                    ref={virtualizer.measureElement}
                    style={virtualStyle}
                  >
                    <div style={animStyle}>
                      <FlatTreeItem
                        node={node}
                        depth={depth}
                        isLast={isLast}
                        parentIsLast={parentIsLast}
                        layer={layer}
                        schema={schema}
                        isSelected={selectedNodeId === node.id}
                        isExpanded={expandedNodes.has(node.id)}
                        isLoading={loadingNodes?.has(node.id) ?? false}
                        isSearchResult={searchResults.includes(node.id)}
                        isHighlighted={traceContextSet.has(node.id)}
                        isFocusNode={traceFocusId === node.id}
                        isTracing={isTracing}
                        isClickHighlighted={isHighlightActive && !isHoverHighlight && (highlightedNodes?.has(node.id) ?? false)}
                        isHoverHighlighted={isHighlightActive && isHoverHighlight && (highlightedNodes?.has(node.id) ?? false)}
                        isDimmedByHighlight={isHighlightActive && !(highlightedNodes?.has(node.id) ?? false)}
                        isFocused={focusIndex >= 0 && navIdx === focusIndex}
                        onSelect={onSelect}
                        onToggle={onToggle}
                        onContextMenu={onContextMenu}
                        onDoubleClick={onDoubleClick}
                        onAddChild={onAddChild}
                        onFocus={handleFocus}
                        onToggleSearch={toggleSearchNode}
                        isSearchVisible={activeSearchNodes.has(node.id)}
                      />
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* Bottom fade — slimmer now that the floating chip handles the
              indicator role. Sits beneath the bottom chip as a soft mask. */}
          <div className="absolute bottom-0 left-0 right-0 h-3 bg-gradient-to-t from-canvas/80 to-transparent pointer-events-none z-10" />
          </div>
        </div>
      )}
    </motion.div>
  )
})

// ─────────────────────────────────────────────────────────────────────────────
// AutoLoadSentinel — replaces the click-based LoadMoreItem (Phase 3.4)
// Invisible div that triggers onLoadMore automatically when scrolled into view.
// Falls back to a subtle "Load N more" button when the observer isn't firing
// (e.g., very tall columns where the sentinel is already in view on expand).
// ─────────────────────────────────────────────────────────────────────────────

function AutoLoadSentinel({
  nodeId,
  depth,
  parentIsLast: _parentIsLast,
  remainingCount,
  onLoadMore,
  isLoading,
  layerColor,
}: {
  nodeId: string
  depth: number
  parentIsLast: boolean[]
  remainingCount: number
  onLoadMore: () => void
  isLoading: boolean
  layerColor: string
}) {
  const sentinelRef = useRef<HTMLDivElement>(null)
  const firedRef = useRef(false)

  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    firedRef.current = false

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !firedRef.current && !isLoading) {
          firedRef.current = true
          onLoadMore()
        }
      },
      { rootMargin: '120px', threshold: 0 }
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [nodeId, onLoadMore, isLoading])

  const indentWidth = depth * 16

  return (
    <div
      ref={sentinelRef}
      className="flex items-center gap-2 mx-1 px-3 py-2"
      style={{ paddingLeft: 12 + indentWidth }}
    >
      {isLoading ? (
        // Loading indicator — matches skeleton colour palette
        <div className="flex items-center gap-2 w-full">
          <LucideIcons.Loader2
            className="w-3.5 h-3.5 animate-spin flex-shrink-0"
            style={{ color: layerColor }}
          />
          <div
            className="h-2 rounded animate-pulse flex-1 max-w-[120px]"
            style={{ backgroundColor: `${layerColor}18` }}
          />
        </div>
      ) : (
        // Subtle "load more" pill — visible if user reaches it before observer fires
        <button
          onClick={onLoadMore}
          className="flex items-center gap-1.5 text-[11px] text-ink-muted/50 hover:text-ink-muted transition-colors group"
        >
          <LucideIcons.ChevronsDown className="w-3 h-3 group-hover:translate-y-0.5 transition-transform" />
          {remainingCount} more
        </button>
      )}
    </div>
  )
}
