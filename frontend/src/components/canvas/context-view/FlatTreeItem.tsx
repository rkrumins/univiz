import React, { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import { DynamicIcon } from '@/components/ui/DynamicIcon'
import type { HierarchyNode } from './types'
import type { ViewLayerConfig } from '@/types/schema'
import { useSchemaStore } from '@/store/schema'
import { useCanvasStore } from '@/store/canvas'
import { generateIconFallback } from '@/lib/type-visuals'
import { useStagedChangesStore, stagedChangeColor } from '@/store/stagedChangesStore'

interface FlatTreeItemProps {
  node: HierarchyNode
  depth: number
  isLast: boolean
  parentIsLast: boolean[]
  layer: ViewLayerConfig
  schema: ReturnType<typeof useSchemaStore.getState>['schema']
  isSelected: boolean
  isExpanded: boolean
  isLoading?: boolean
  isSearchResult: boolean
  isHighlighted: boolean
  isFocusNode: boolean
  isClickHighlighted?: boolean
  isHoverHighlighted?: boolean
  isDimmedByHighlight?: boolean
  isFocused?: boolean
  isTracing?: boolean
  onSelect: (id: string) => void
  onToggle: (id: string) => void
  onContextMenu: (e: React.MouseEvent, id: string) => void
  onDoubleClick: (id: string, event?: React.MouseEvent) => void
  onAddChild?: (parentId: string) => void
  onFocus: (node: HierarchyNode) => void
  onToggleSearch?: (id: string) => void
  isSearchVisible?: boolean
}

export const FlatTreeItem = React.memo(function FlatTreeItem({
  node,
  depth,
  isLast,
  parentIsLast,
  layer,
  schema,
  isSelected,
  isExpanded,
  isLoading = false,
  isSearchResult,
  isHighlighted,
  isFocusNode,
  isClickHighlighted = false,
  isHoverHighlighted = false,
  isDimmedByHighlight = false,
  isFocused = false,
  isTracing = false,
  onSelect,
  onToggle,
  onContextMenu,
  onDoubleClick,
  onAddChild,
  onFocus,
  onToggleSearch,
  isSearchVisible = false,
}: FlatTreeItemProps) {
  const itemRef = useRef<HTMLDivElement>(null)
  const [isHovered, setIsHovered] = useState(false)
  const isLogical = node.isLogical === true

  // Staged-change indicator: a *direct* match wins, but if any descendant is
  // staged the row also tints (lighter) so the user can spot pending work
  // anywhere in the tree without expanding every container.
  const directChange = useStagedChangesStore(s => {
    const matches = s.changes.filter(c => c.targetId === node.id || c.targetUrn === node.urn)
    return matches.length > 0 ? matches[matches.length - 1] : undefined
  })
  // Cascade detection — check if any descendant URN is staged.
  const hasDescendantChange = useStagedChangesStore(s => {
    if (directChange) return false
    if (!node.children || node.children.length === 0) return false
    const descendantIds = new Set<string>()
    const collect = (n: HierarchyNode) => {
      descendantIds.add(n.id)
      if (n.urn) descendantIds.add(n.urn)
      n.children?.forEach(collect)
    }
    node.children.forEach(collect)
    return s.changes.some(c => descendantIds.has(c.targetId) || (c.targetUrn ? descendantIds.has(c.targetUrn) : false))
  })

  // Pulse-on-arrival from a jump-to-node reveal. Auto-clears via the
  // store's setTimeout (~700ms).
  const isPulsing = useCanvasStore((s) => s.pulseNodeId === node.id)

  const stagedColor = directChange ? stagedChangeColor(directChange.type) : (hasDescendantChange ? 'cascade' : null)
  const stagedSummary = directChange?.summary
    ?? (hasDescendantChange ? 'Contains staged changes' : undefined)

  // Strong, full-width background tint — the user wanted the ENTIRE row to
  // glow in the change color so the canvas reads as a heatmap of pending edits.
  // Direct changes get saturated tints; cascade indicates child changes with a
  // muted left-bar treatment so it's spottable but not overpowering.
  const stagedRowClass = (() => {
    switch (stagedColor) {
      case 'green':
        return 'bg-gradient-to-r from-green-500/25 via-green-500/15 to-green-500/5 ring-2 ring-green-400/70 shadow-lg shadow-green-500/20'
      case 'red':
        return 'bg-gradient-to-r from-rose-500/30 via-rose-500/20 to-rose-500/8 ring-2 ring-rose-400/80 shadow-lg shadow-rose-500/25 opacity-90'
      case 'amber':
        return 'bg-gradient-to-r from-orange-500/25 via-orange-500/15 to-orange-500/5 ring-2 ring-orange-400/70 shadow-lg shadow-orange-500/20'
      case 'cascade':
        // Indicate that a descendant has a staged change with a soft amber edge stripe.
        return 'border-l-[3px] border-l-amber-400/50'
      default:
        return ''
    }
  })()
  const entityType = schema?.entityTypes.find((et) => et.id === node.typeId)
  const visual = entityType?.visual
  const nodeColor = visual?.color ?? layer.color
  // Logical nodes use a folder/group icon instead of entity type icon
  const logicalIcon = isLogical
    ? (entityType?.visual?.icon ?? generateIconFallback(node.typeId))
    : undefined

  const childCount = (node.data.childCount as number) || (node.data._collapsedChildCount as number) || 0
  const hasChildren = node.children.length > 0 || childCount > 0
  // In trace mode, useTraceFilteredHierarchy already prunes node.children to
  // the trace context, so children.length reflects what the user will see on
  // expand. The graph-wide childCount would mislead them with siblings the
  // trace filter immediately hides.
  const descendantCount = hasChildren && !isExpanded
    ? (isTracing ? node.children.length : (childCount || node.children.length))
    : 0

  // IMPROVED SIZING - Keep items readable at ALL depths
  // Root items are slightly larger, but children remain very readable
  const isRoot = depth === 0
  const heightClass = isRoot ? 'min-h-[52px]' : 'min-h-[44px]'
  const paddingClass = isRoot ? 'py-3' : 'py-2.5'
  const textClass = isRoot ? 'text-sm' : 'text-[13px]'
  const iconSize = isRoot ? 'w-5 h-5' : 'w-4 h-4'
  const iconContainerSize = isRoot ? 'w-9 h-9' : 'w-7 h-7'

  // Dimming applies only to the click-highlight feature now. Trace mode used
  // to dim non-traced nodes here, but ContextViewCanvas's
  // `useTraceFilteredHierarchy` removes them from the render tree entirely
  // — so anything that reaches FlatTreeItem during trace IS in the trace
  // context and should render at full opacity.
  const isDimmed = isDimmedByHighlight

  // Tree line indent - reduced to save horizontal space
  const indentWidth = depth * 16

  // 4.3 Drag-and-drop — only root-level nodes (depth === 0, no parentId) may
  // be re-assigned between layers. Children live inside their parent's
  // containment scope; moving a column without its table would break the
  // ontology. Attach native events via ref (avoids type conflict).
  const isLayerDraggable = depth === 0 && !node.parentId && !isLogical
  useEffect(() => {
    const el = itemRef.current
    if (!el) return

    if (!isLayerDraggable) {
      el.removeAttribute('draggable')
      return
    }

    el.setAttribute('draggable', 'true')

    const onDragStart = (e: DragEvent) => {
      if (!e.dataTransfer) return
      e.dataTransfer.effectAllowed = 'move'
      e.dataTransfer.setData('text/x-entity-id', node.id)
      e.dataTransfer.setData('text/x-entity-name', node.name)
      e.dataTransfer.setDragImage(el, 20, 20)
    }
    const onDragEnd = () => {
      delete document.documentElement.dataset.hoveredNode
    }

    el.addEventListener('dragstart', onDragStart)
    el.addEventListener('dragend', onDragEnd)
    return () => {
      el.removeEventListener('dragstart', onDragStart)
      el.removeEventListener('dragend', onDragEnd)
    }
  }, [node.id, node.name, isLayerDraggable])

  return (
    <div
      ref={itemRef}
      id={`layer-node-${node.id}`}
      data-canvas-interactive
      data-trace-focus={isFocusNode ? 'true' : 'false'}
      className={cn(
        "flex items-center gap-2 mx-1 rounded-xl cursor-pointer transition-all duration-200 group/item relative z-[2]",
        heightClass,
        paddingClass,
        // Subtle backdrop-blur on the card body — visually invisible
        // (matches the glassy translucent design) but blurs anything
        // painted behind so cross-column edges don't read as solid lines
        // bleeding through the node. Same technique the layer header uses
        // (`backdrop-blur-xl` at LayerColumn.tsx:508). The bg tint is kept
        // near-zero so the airy feel of the original cards is preserved;
        // hover / selected gradients below paint over this without conflict.
        "bg-canvas-elevated/10 backdrop-blur-sm",
        // Base hover state with gradient
        "hover:bg-gradient-to-r hover:from-white/[0.06] hover:to-transparent",
        // Selected state with accent glow
        isSelected && "bg-gradient-to-r from-accent-lineage/15 via-accent-lineage/10 to-transparent shadow-[inset_0_0_0_1px_rgba(var(--accent-lineage-rgb),0.3)]",
        // Search result highlight
        isSearchResult && !isSelected && "bg-gradient-to-r from-amber-500/15 to-transparent shadow-[inset_0_0_0_1px_rgba(245,158,11,0.3)]",
        // Focus node (trace target)
        isFocusNode && "ring-2 ring-accent-lineage/60 ring-offset-1 ring-offset-canvas shadow-lg shadow-accent-lineage/20",
        // Highlighted in trace
        isHighlighted && !isFocusNode && "bg-gradient-to-r from-accent-lineage/10 to-transparent",
        // Click-highlight: subtle glow on connected nodes
        isClickHighlighted && !isSelected && "ring-1 ring-blue-400/40 bg-gradient-to-r from-blue-500/10 to-transparent",
        // Hover-highlight: lighter ephemeral glow on connected nodes
        isHoverHighlighted && !isSelected && !isClickHighlighted && "bg-gradient-to-r from-blue-500/[0.05] to-transparent ring-1 ring-blue-400/15 dark:from-blue-400/[0.06] dark:ring-blue-400/12",
        // Keyboard focus ring (4.5)
        isFocused && !isSelected && "ring-2 ring-accent-lineage/40 bg-gradient-to-r from-accent-lineage/[0.06] to-transparent",
        // Staged-change row treatment — full-row color tint per change type
        stagedRowClass,
        // Dimmed when not in trace path or not connected to highlighted node
        isDimmed && "opacity-40",
        // Jump-to-node arrival pulse — one-shot ring animation
        isPulsing && "lineage-pulse"
      )}
      style={{
        paddingLeft: 12 + indentWidth,
        // Subtle left border accent for root items
        ...(depth === 0 && {
          borderLeft: `3px solid ${nodeColor}40`,
        }),
      }}
      onClick={(e) => {
        e.stopPropagation()
        onSelect(node.id)
      }}
      onDoubleClick={(e) => {
        e.stopPropagation()
        onDoubleClick(node.id, e)
      }}
      onContextMenu={(e) => onContextMenu(e, node.id)}
      onMouseEnter={() => {
        setIsHovered(true)
        document.documentElement.dataset.hoveredNode = node.id
      }}
      onMouseLeave={() => {
        setIsHovered(false)
        delete document.documentElement.dataset.hoveredNode
      }}
    >
      {/* Modern Tree Lines with gradient effect */}
      <div className="flex items-center absolute left-3" style={{ width: indentWidth }}>
        {parentIsLast.map((pIsLast, idx) => (
          <div key={idx} className="w-5 h-full flex justify-center">
            {!pIsLast && (
              <div className="w-px h-full bg-gradient-to-b from-white/[0.08] via-white/[0.12] to-white/[0.08]" />
            )}
          </div>
        ))}
        {depth > 0 && (
          <div className="w-5 h-full relative">
            {/* Vertical line with gradient */}
            <div className={cn(
              "absolute left-1/2 -translate-x-1/2 w-px",
              isLast ? "top-0 h-1/2" : "top-0 bottom-0"
            )} style={{ background: 'linear-gradient(to bottom, transparent, rgba(255,255,255,0.12), transparent)' }} />
            {/* Horizontal connector with dot */}
            <div className="absolute left-1/2 top-1/2 -translate-y-1/2 flex items-center">
              <div className="w-3 h-px bg-gradient-to-r from-white/[0.12] to-white/[0.06]" />
              <div
                className="w-1.5 h-1.5 rounded-full -ml-0.5"
                style={{ backgroundColor: `${nodeColor}40` }}
              />
            </div>
          </div>
        )}
      </div>

      {/* Expand/Collapse Toggle - Modern circular button with loading state */}
      <button
        onClick={(e) => {
          e.stopPropagation()
          onToggle(node.id)
        }}
        className={cn(
          "flex-shrink-0 rounded-lg transition-all duration-200",
          hasChildren
            ? "hover:bg-white/[0.1] hover:scale-110 active:scale-95"
            : "opacity-0 pointer-events-none",
          isRoot ? "w-7 h-7" : "w-6 h-6"
        )}
      >
        {hasChildren && (
          <AnimatePresence mode="wait">
            {isLoading ? (
              <motion.div
                key="spinner"
                initial={{ opacity: 0, scale: 0.5 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.5 }}
                transition={{ duration: 0.15 }}
                className="w-full h-full flex items-center justify-center"
              >
                <LucideIcons.Loader2
                  className={cn(
                    "animate-spin",
                    isRoot ? "w-4 h-4" : "w-3.5 h-3.5"
                  )}
                  style={{ color: nodeColor }}
                />
              </motion.div>
            ) : (
              <motion.div
                key="chevron"
                initial={{ opacity: 0, scale: 0.5 }}
                animate={{ opacity: 1, scale: 1, rotate: isExpanded ? 90 : 0 }}
                exit={{ opacity: 0, scale: 0.5 }}
                transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
                className="w-full h-full flex items-center justify-center"
              >
                <LucideIcons.ChevronRight
                  className={cn(
                    "transition-colors",
                    isHovered ? "text-ink" : "text-ink-muted/60",
                    isRoot ? "w-4 h-4" : "w-4 h-4"
                  )}
                />
              </motion.div>
            )}
          </AnimatePresence>
        )}
      </button>

      {/* Entity Icon - Glass morphism container */}
      <div
        className={cn(
          "rounded-xl flex items-center justify-center flex-shrink-0 transition-all duration-200 shadow-sm relative",
          iconContainerSize,
          isSelected && "scale-110 shadow-md",
          isHovered && "scale-105"
        )}
        style={{
          background: `linear-gradient(135deg, ${nodeColor}25 0%, ${nodeColor}10 100%)`,
          boxShadow: isSelected ? `0 4px 12px ${nodeColor}30` : `0 2px 4px ${nodeColor}15`,
          ...(isLogical && { border: `1px dashed ${nodeColor}50` }),
        }}
      >
        <DynamicIcon
          name={logicalIcon ?? visual?.icon ?? 'Box'}
          className={cn(iconSize, "transition-transform duration-200")}
          style={{ color: nodeColor }}
        />
      </div>

      {/* Name - IMPROVED: Better visibility with tooltip */}
      <div className="flex-1 min-w-0 flex flex-col justify-center" title={stagedSummary ?? node.name}>
        <span className={cn(
          "font-medium tracking-tight transition-colors duration-200",
          textClass,
          isHighlighted ? "text-accent-lineage" : isSelected ? "text-ink" : "text-ink/90",
          isHovered && !isSelected && "text-ink",
          // Strikethrough for pending-delete makes the destruction intent unmissable
          stagedColor === 'red' && "line-through decoration-rose-300/80 decoration-2",
          // Allow text to wrap to 2 lines for better readability
          "line-clamp-2"
        )}>
          {node.name}
        </span>
        {/* Type badge - show for all items to help identify entity types */}
        <span className={cn(
          "text-[10px] text-ink-muted/60 truncate mt-0.5 flex items-center gap-1",
          isRoot && "text-[11px]"
        )}>
          <span
            className="w-1.5 h-1.5 rounded-full flex-shrink-0"
            style={{ backgroundColor: nodeColor }}
          />
          {isLogical ? `${node.typeId.charAt(0).toUpperCase()}${node.typeId.slice(1)} (group)` : (entityType?.name ?? node.typeId)}
        </span>
      </div>

      {/* Badges - Descendant count */}
      <AnimatePresence>
        {descendantCount > 0 && (
          <motion.span
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            className="text-[11px] px-2 py-1 rounded-lg bg-white/[0.06] border border-white/[0.08] text-ink-muted font-semibold tabular-nums flex-shrink-0"
          >
            +{descendantCount}
          </motion.span>
        )}
      </AnimatePresence>

      {/* Action buttons - Glass morphism style, appear on hover */}
      <motion.div
        initial={false}
        animate={{ opacity: isHovered ? 1 : 0, x: isHovered ? 0 : 8 }}
        transition={{ duration: 0.15 }}
        className="flex items-center gap-1 flex-shrink-0"
      >
        {/* Focus/Drill button */}
        {hasChildren && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onFocus(node)
            }}
            className="p-1.5 rounded-lg bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 hover:text-blue-300 transition-all duration-200 hover:scale-110 active:scale-95"
            title="Focus on this subtree"
          >
            <LucideIcons.Maximize2 className="w-3 h-3" />
          </button>
        )}

        {/* Search children button */}
        {hasChildren && onToggleSearch && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onToggleSearch(node.id)
            }}
            className={cn(
              "p-1.5 rounded-lg transition-all duration-200 hover:scale-110 active:scale-95",
              isSearchVisible
                ? "bg-amber-500/20 text-amber-400"
                : "bg-white/[0.06] hover:bg-white/[0.12] text-ink-muted/80 hover:text-ink-muted"
            )}
            title="Search children"
          >
            <LucideIcons.Search className="w-3 h-3" />
          </button>
        )}

        {/* Add child button */}
        {entityType?.hierarchy?.canContain && entityType.hierarchy.canContain.length > 0 && onAddChild && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onAddChild(node.id)
            }}
            className="p-1.5 rounded-lg bg-green-500/10 hover:bg-green-500/20 text-green-400 hover:text-green-300 transition-all duration-200 hover:scale-110 active:scale-95"
            title="Add child entity"
          >
            <LucideIcons.Plus className="w-3 h-3" />
          </button>
        )}
      </motion.div>

      {/* Hover indicator line */}
      <motion.div
        className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] rounded-r-full"
        style={{ backgroundColor: nodeColor }}
        initial={false}
        animate={{
          height: isSelected ? '70%' : isHovered ? '50%' : '0%',
          opacity: isSelected ? 1 : isHovered ? 0.6 : 0
        }}
        transition={{ duration: 0.2 }}
      />
    </div>
  )
})
