import { memo, useMemo } from 'react'
import { Handle, Position, type NodeProps, NodeToolbar } from '@xyflow/react'
import * as LucideIcons from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { useSchemaStore } from '@/store/schema'
import { useCanvasStore } from '@/store/canvas'
import { useTraceStore } from '@/hooks/useUnifiedTrace'
import { cn } from '@/lib/utils'
import { generateColorFromType, generateIconFallback } from '@/lib/type-visuals'
import type { EntityInstance, EntityVisualConfig } from '@/types/schema'

// Dynamic icon component
function DynamicIcon({ name, className, style }: { name: string; className?: string; style?: React.CSSProperties }) {
  const IconComponent = (LucideIcons as unknown as Record<string, React.ComponentType<{ className?: string; style?: React.CSSProperties }>>)[name]
  if (!IconComponent) {
    return <LucideIcons.Box className={className} style={style} />
  }
  return <IconComponent className={className} style={style} />
}

interface GenericNodeData extends EntityInstance {
  isExpanded?: boolean
  isLoading?: boolean
  childCount?: number
  // Trace flags
  isTraced?: boolean
  isDimmed?: boolean
  isUpstream?: boolean
  isDownstream?: boolean
  isFocus?: boolean
  // Pin Lineage — this node is a pinned trace-path endpoint
  isPinned?: boolean
  // Persona (business | technical display mode)
  persona?: 'business' | 'technical'
  // Ghost state (unknown/placeholder node)
  isGhost?: boolean
  // Canvas-agnostic callbacks (injected by the canvas host)
  onLoadMore?: () => void
  onToggleExpanded?: (nodeId: string) => void
}

import type { Node } from '@xyflow/react'
type GenericNodeProps = NodeProps<Node<Record<string, unknown>>>

/**
 * GenericNode - Renders any entity type based on schema configuration
 * 
 * This is the single node component that replaces all hardcoded node types.
 * It reads the entity type schema and renders accordingly.
 */
export const GenericNode = memo(function GenericNode({
  id,
  data,
  selected,
  dragging,
  isConnectable,
}: GenericNodeProps) {
  // Pulse-on-arrival ring. Driven by the canvas store's Set so multiple
  // simultaneous reveals (multi-locate) can all pulse at once. Auto-clears
  // per-id after the animation duration (~900ms).
  const isPulsing = useCanvasStore((s) => s.pulseNodeIds.has(id))
  // Handle both nested (data.data) and flat (data) structures
  const rawData = (data as Record<string, unknown>)
  const entityData: GenericNodeData = rawData.data
    ? (rawData.data as GenericNodeData)
    : (rawData as unknown as GenericNodeData)

  const { isTraced, isDimmed, isUpstream, isDownstream, isFocus, isPinned, persona = 'business' } = entityData

  // Pin Lineage is only meaningful while a trace is active. Subscribe to the
  // same trace store GraphCanvas uses so the Pin toolbar button only appears
  // during a trace.
  const isTracingActive = useTraceStore((s) => s.focusId !== null)
  const togglePinTarget = useCanvasStore((s) => s.togglePinTarget)

  // Canvas-agnostic callbacks — injected via data props by the host canvas
  const onLoadMore = entityData.onLoadMore
  const onToggleExpanded = entityData.onToggleExpanded

  const hiddenCount = (entityData as any)._hiddenCount || 0
  const paginationId = (entityData as any)._paginationId

  // Support both typeId and type fields
  const typeId = entityData.typeId || (entityData as unknown as Record<string, unknown>).type as string || 'unknown'

  const getEntityType = useSchemaStore((s) => s.getEntityType)
  const getEntityVisual = useSchemaStore((s) => s.getEntityVisual)

  const entityType = getEntityType(typeId)
  const schemaVisual = getEntityVisual(typeId)

  // Hash-palette fallback for types not in the schema (custom graphs)
  const visual: EntityVisualConfig = schemaVisual ?? {
    icon: generateIconFallback(typeId),
    color: generateColorFromType(typeId),
    shape: 'rounded',
    size: 'md',
    borderStyle: 'solid',
    showInMinimap: true,
  }

  // Get fields to display in the node
  const visibleFields = useMemo(() => {
    if (!entityType) return []
    return entityType.fields
      .filter((f) => f.showInNode)
      .sort((a, b) => a.displayOrder - b.displayOrder)
  }, [entityType])

  // Get the primary label - handle both nested and flat structures
  const entityFields = (entityData.data || entityData) as Record<string, unknown>
  const primaryLabel = entityFields['name'] as string ||
    entityFields['label'] as string ||
    entityFields['businessLabel'] as string ||
    entityData.id || 'Unknown'

  // Secondary label based on persona
  const secondaryLabel: string | undefined = persona === 'technical'
    ? (entityFields['urn'] as string) || (entityFields['qualified_name'] as string)
    : (entityFields['description'] as string)

  // Size classes
  const sizeClasses = {
    xs: 'min-w-[100px] max-w-[140px] px-2 py-1.5',
    sm: 'min-w-[140px] max-w-[180px] px-2.5 py-2',
    md: 'min-w-[180px] max-w-[240px] px-3 py-2.5',
    lg: 'min-w-[220px] max-w-[300px] px-4 py-3',
    xl: 'min-w-[280px] max-w-[380px] px-5 py-4',
  }

  // Shape classes
  const shapeClasses = {
    rectangle: 'rounded-md',
    rounded: 'rounded-xl',
    pill: 'rounded-full',
    diamond: 'rounded-lg rotate-0', // Would need special handling
    hexagon: 'rounded-lg', // Would need clip-path
    circle: 'rounded-full aspect-square',
  }

  // Border style classes
  const borderClasses = {
    solid: 'border-2',
    dashed: 'border-2 border-dashed',
    dotted: 'border-2 border-dotted',
    none: 'border-0',
  }

  // isGhost: use data prop (set by graph loader for placeholder/unknown nodes) rather than hardcoded type check
  const isGhost = entityData.isGhost ?? false
  const isExpandable = entityType.behavior.expandable && ((entityData.childCount ?? 0) > 0)
  // Expansion mode from ontology definition: 'inline' expands in-place, 'graph' expands the canvas
  const expansionMode: 'inline' | 'graph' = (entityType.behavior as any).expansionMode ?? 'graph'

  return (
    <>
      {/* Node Toolbar (appears on selection) */}
      {entityType.behavior.traceable && (
        <NodeToolbar
          isVisible={!!selected}
          position={Position.Top}
          className="flex items-center gap-1 glass-panel-subtle rounded-lg p-1"
        >
          <ToolbarButton icon="ArrowUpRight" label="Trace Upstream" />
          <ToolbarButton icon="ArrowDownLeft" label="Trace Downstream" />
          <div className="w-px h-4 bg-glass-border mx-0.5" />
          {isTracingActive && (
            <ToolbarButton
              icon="Pin"
              label={isPinned ? 'Unpin from trace path' : 'Pin to trace path'}
              active={isPinned}
              onClick={() => togglePinTarget(id)}
            />
          )}
          <ToolbarButton icon="MoreHorizontal" label="More" />
        </NodeToolbar>
      )}

      {/* Input Handle */}
      <Handle
        type="target"
        position={Position.Left}
        isConnectable={isConnectable}
        className={cn(
          "!w-2.5 !h-2.5 !rounded-full !border-2",
          "!bg-canvas-elevated transition-colors",
          isGhost ? "!border-dashed" : ""
        )}
        style={{ borderColor: visual.color }}
      />

      {/* Node Content */}
      <div
        className={cn(
          "relative transition-all duration-200",
          sizeClasses[visual.size],
          shapeClasses[visual.shape],
          borderClasses[visual.borderStyle],
          "bg-canvas-elevated",
          !!selected && !isTraced && "ring-2 ring-offset-2",
          !!dragging && "opacity-80 cursor-grabbing",
          isGhost && "opacity-60",
          // Trace Styling - Consistent across all views
          isDimmed && "opacity-30 grayscale-[0.6] blur-[0.5px] scale-[0.98]",
          // Focus node: Gold ring + pulse
          isFocus && "ring-4 ring-amber-400 ring-offset-2 shadow-[0_0_30px_rgba(251,191,36,0.6)] scale-[1.05] z-[100]",
          // Upstream nodes: Blue tint
          isUpstream && !isFocus && "ring-2 ring-blue-400 ring-offset-1 shadow-[0_0_15px_rgba(96,165,250,0.4)] bg-blue-50 dark:bg-blue-950/30 z-50",
          // Downstream nodes: Green tint
          isDownstream && !isFocus && !isUpstream && "ring-2 ring-green-400 ring-offset-1 shadow-[0_0_15px_rgba(74,222,128,0.4)] bg-green-50 dark:bg-green-950/30 z-50",
          // Generic traced node (neither up nor down but in path)
          isTraced && !isFocus && !isUpstream && !isDownstream && "ring-2 ring-purple-400 ring-offset-1 shadow-[0_0_15px_rgba(192,132,252,0.4)] z-50",
          // Pinned trace-path endpoint — distinct amber ring, sits on top
          isPinned && "ring-4 ring-amber-500 ring-offset-2 shadow-[0_0_22px_rgba(245,158,11,0.6)] z-[90]",
          // Jump-to-node arrival pulse — one-shot ring animation
          isPulsing && "lineage-pulse"
        )}
        style={{
          borderColor: isPinned ? '#f59e0b' : isFocus ? '#fbbf24' : isUpstream ? '#60a5fa' : isDownstream ? '#4ade80' : isTraced ? '#c084fc' : visual.color,
          borderLeftWidth: visual.borderStyle !== 'none' ? '4px' : undefined,
          boxShadow: isFocus
            ? '0 0 30px rgba(251,191,36,0.5)'
            : isUpstream 
              ? '0 0 20px rgba(96,165,250,0.4)'
              : isDownstream
                ? '0 0 20px rgba(74,222,128,0.4)'
                : isTraced
                  ? '0 0 20px rgba(192,132,252,0.4)'
                  : selected
                    ? `0 0 20px ${visual.color}40`
                    : '0 4px 12px rgba(0,0,0,0.1)',
          ['--ring-color' as string]: isPinned ? '#f59e0b' : isFocus ? '#fbbf24' : isUpstream ? '#60a5fa' : isDownstream ? '#4ade80' : isTraced ? '#c084fc' : visual.color,
        }}
        // Add data attributes for testing/debugging
        data-traced={isTraced}
        data-dimmed={isDimmed}
        data-upstream={isUpstream}
        data-downstream={isDownstream}
        data-focus={isFocus}
        data-pinned={isPinned}
      >
        {/* Header */}
        <div className="flex items-start gap-2">
          {/* Icon */}
          <div
            className={cn(
              "flex-shrink-0 rounded-lg flex items-center justify-center",
              visual.size === 'xs' ? 'w-5 h-5' :
                visual.size === 'sm' ? 'w-6 h-6' :
                  visual.size === 'md' ? 'w-8 h-8' :
                    visual.size === 'lg' ? 'w-10 h-10' : 'w-12 h-12'
            )}
            style={{ backgroundColor: `${visual.color}15` }}
          >
            <DynamicIcon
              name={visual.icon}
              className={cn(
                visual.size === 'xs' ? 'w-3 h-3' :
                  visual.size === 'sm' ? 'w-3.5 h-3.5' :
                    visual.size === 'md' ? 'w-4 h-4' :
                      visual.size === 'lg' ? 'w-5 h-5' : 'w-6 h-6'
              )}
              style={{ color: visual.color }}
            />
          </div>

          {/* Content */}
          <div className="flex-1 min-w-0">
            {/* Type Badge */}
            <span
              className="text-2xs font-medium uppercase tracking-wider"
              style={{ color: visual.color }}
            >
              {entityType.name}
            </span>

            {/* Primary Label */}
            <h3 className={cn(
              "font-medium text-ink leading-tight truncate",
              visual.size === 'xs' ? 'text-xs' :
                visual.size === 'sm' ? 'text-sm' : 'text-sm'
            )}>
              {primaryLabel}
            </h3>

            {/* Secondary Label (persona-driven) */}
            {secondaryLabel && visual.size !== 'xs' && (
              <p className="text-2xs text-ink-muted truncate mt-0.5">
                {secondaryLabel}
              </p>
            )}
          </div>

          {/* Expand Button */}
          {isExpandable && onToggleExpanded && (
            <button
              title={expansionMode === 'inline' ? 'Expand inline' : 'Expand on canvas'}
              onClick={(e) => {
                e.stopPropagation()
                onToggleExpanded(id)
              }}
              className={cn(
                "w-5 h-5 rounded flex items-center justify-center",
                "hover:bg-black/5 dark:hover:bg-white/10 transition-colors"
              )}
            >
              <DynamicIcon
                name={entityData.isExpanded
                  ? "ChevronDown"
                  : expansionMode === 'inline' ? "ChevronsDown" : "ChevronRight"}
                className="w-3 h-3 text-ink-muted"
              />
            </button>
          )}
        </div>

        {/* Dynamic Fields */}
        {visibleFields.length > 1 && visual.size !== 'xs' && (
          <div className="mt-2 space-y-1">
            {visibleFields.slice(1).map((field) => (
              <FieldRenderer
                key={field.id}
                field={field}
                value={entityFields[field.id]}
                color={visual.color}
                size={visual.size}
              />
            ))}
          </div>
        )}

        {/* Roll-up Summary */}
        {entityType.hierarchy.rollUpFields.length > 0 && entityData._computed?.rollUps && (
          <div className="mt-2 pt-2 border-t border-glass-border">
            <div className="flex items-center gap-3 text-2xs text-ink-muted">
              {entityType.hierarchy.rollUpFields.map((rollUp) => (
                <span key={rollUp.targetField}>
                  {String(entityData._computed?.rollUps[rollUp.targetField])} {rollUp.label}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Child Count for collapsed nodes */}
        {entityData.childCount && !entityData.isExpanded && (
          <div className="mt-2 text-2xs text-ink-muted">
            {entityData.childCount} {entityData.childCount === 1 ? 'child' : 'children'}
          </div>
        )}

        {/* Loading State */}
        <AnimatePresence>
          {entityData.isLoading && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute inset-0 flex items-center justify-center bg-canvas-elevated/80 rounded-xl"
            >
              <LucideIcons.Loader2 className="w-5 h-5 animate-spin" style={{ color: visual.color }} />
            </motion.div>
          )}
        </AnimatePresence>

        {/* Load More Button */}
        {hiddenCount > 0 && (
          <button
            title={`Load ${hiddenCount} more items`}
            className={cn(
              "absolute -bottom-2 -right-2 w-6 h-6 rounded-full flex items-center justify-center",
              "bg-canvas-elevated shadow-md border-2",
              "hover:scale-110 transition-transform cursor-pointer"
            )}
            style={{ borderColor: visual.color }}
            onClick={(e) => {
              e.stopPropagation()
              if (paginationId) {
                // Trigger layout stabilization anchor via the canvas-injected callback
                onLoadMore?.()
              }
            }}
          >
            <LucideIcons.Plus className="w-3.5 h-3.5" style={{ color: visual.color }} />
          </button>
        )}
      </div>

      {/* Output Handle */}
      <Handle
        type="source"
        position={Position.Right}
        isConnectable={isConnectable}
        className={cn(
          "!w-2.5 !h-2.5 !rounded-full !border-2",
          "!bg-canvas-elevated transition-colors",
          isGhost ? "!border-dashed" : ""
        )}
        style={{ borderColor: visual.color }}
      />
    </>
  )
}, (prev, next) => {
  // Custom comparator: only re-render when fields that affect visuals change
  if (prev.id !== next.id || prev.selected !== next.selected || prev.dragging !== next.dragging) {
    return false
  }

  const prevRaw = prev.data as Record<string, unknown>
  const nextRaw = next.data as Record<string, unknown>
  const prevD = (prevRaw.data ?? prevRaw) as Record<string, unknown>
  const nextD = (nextRaw.data ?? nextRaw) as Record<string, unknown>

  return (
    prevD.label === nextD.label &&
    prevD.name === nextD.name &&
    prevD.typeId === nextD.typeId &&
    (prevD.type as unknown) === (nextD.type as unknown) &&
    prevD.isExpanded === nextD.isExpanded &&
    prevD.isLoading === nextD.isLoading &&
    prevD.isTraced === nextD.isTraced &&
    prevD.isDimmed === nextD.isDimmed &&
    prevD.isUpstream === nextD.isUpstream &&
    prevD.isDownstream === nextD.isDownstream &&
    prevD.isFocus === nextD.isFocus &&
    prevD.isGhost === nextD.isGhost &&
    prevD.childCount === nextD.childCount &&
    prevD.persona === nextD.persona &&
    prevD._hiddenCount === nextD._hiddenCount &&
    prevD.onLoadMore === nextD.onLoadMore &&
    prevD.onToggleExpanded === nextD.onToggleExpanded
  )
})

// Field Renderer Component
interface FieldRendererProps {
  field: { id: string; name: string; type: string; format?: unknown }
  value: unknown
  color: string
  size: string
}

function FieldRenderer({ field, value, color, size }: FieldRendererProps) {
  if (value === undefined || value === null) return null

  switch (field.type) {
    case 'tags':
      const tags = value as string[]
      return (
        <div className="flex flex-wrap gap-1">
          {tags.slice(0, 3).map((tag) => (
            <span
              key={tag}
              className="px-1.5 py-0.5 rounded text-2xs font-medium"
              style={{ backgroundColor: `${color}15`, color }}
            >
              {tag}
            </span>
          ))}
          {tags.length > 3 && (
            <span className="text-2xs text-ink-muted">+{tags.length - 3}</span>
          )}
        </div>
      )

    case 'badge':
      return (
        <span
          className="inline-block px-1.5 py-0.5 rounded text-2xs font-medium"
          style={{ backgroundColor: `${color}15`, color }}
        >
          {String(value)}
        </span>
      )

    case 'progress':
      const progress = Number(value)
      return (
        <div className="space-y-0.5">
          <div className="flex items-center justify-between text-2xs">
            <span className="text-ink-muted">{field.name}</span>
            <span className={cn(
              "font-medium",
              progress >= 80 ? "text-green-500" :
                progress >= 50 ? "text-amber-500" : "text-red-500"
            )}>
              {Math.round(progress)}%
            </span>
          </div>
          {size !== 'xs' && size !== 'sm' && (
            <div className="h-1 bg-black/5 dark:bg-white/5 rounded-full overflow-hidden">
              <div
                className={cn(
                  "h-full rounded-full transition-all",
                  progress >= 80 ? "bg-green-500" :
                    progress >= 50 ? "bg-amber-500" : "bg-red-500"
                )}
                style={{ width: `${progress}%` }}
              />
            </div>
          )}
        </div>
      )

    case 'status':
      const format = field.format as { statusColors?: Record<string, string> }
      const statusColor = format?.statusColors?.[String(value)] || color
      return (
        <div className="flex items-center gap-1.5">
          <div
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: statusColor }}
          />
          <span className="text-2xs font-medium capitalize">{String(value)}</span>
        </div>
      )

    case 'urn':
      return (
        <code className="block text-2xs font-mono text-ink-muted bg-black/5 dark:bg-white/5 px-1.5 py-0.5 rounded truncate">
          {String(value)}
        </code>
      )

    case 'number':
      const numFormat = (field.format as { numberFormat?: string })?.numberFormat
      let displayNum = String(value)
      if (numFormat === 'compact') {
        displayNum = Intl.NumberFormat('en', { notation: 'compact' }).format(Number(value))
      } else if (numFormat === 'percentage') {
        displayNum = `${value}%`
      }
      return (
        <span className="text-2xs text-ink-secondary">{displayNum}</span>
      )

    default:
      return (
        <span className="text-2xs text-ink-secondary truncate">
          {String(value)}
        </span>
      )
  }
}

// Toolbar Button Component
function ToolbarButton({
  icon,
  label,
  onClick,
  active,
}: {
  icon: string
  label: string
  onClick?: () => void
  active?: boolean
}) {
  return (
    <button
      title={label}
      onClick={onClick}
      className={cn(
        "w-7 h-7 rounded-md flex items-center justify-center transition-colors",
        active
          ? "text-amber-600 dark:text-amber-400 bg-amber-400/20"
          : "text-ink-secondary hover:text-ink hover:bg-black/5 dark:hover:bg-white/10"
      )}
    >
      <DynamicIcon name={icon} className="w-3.5 h-3.5" />
    </button>
  )
}

// Fallback Node for unknown types
function FallbackNode({ data, selected }: { data: GenericNodeData; selected: boolean }) {
  return (
    <>
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !bg-gray-400" />
      <div className={cn(
        "px-3 py-2 rounded-lg border-2 border-dashed border-gray-400",
        "bg-canvas-elevated",
        selected && "ring-2 ring-offset-2 ring-gray-400"
      )}>
        <div className="flex items-center gap-2">
          <LucideIcons.HelpCircle className="w-4 h-4 text-gray-400" />
          <span className="text-sm text-gray-500">Unknown: {data.typeId || (data as any).type}</span>
        </div>
      </div>
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !bg-gray-400" />
    </>
  )
}

