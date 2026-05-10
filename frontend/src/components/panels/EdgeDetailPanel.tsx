/**
 * EdgeDetailPanel - Advanced edge inspection and filtering panel
 * 
 * Features:
 * - Filter by edge type (transforms, produces, consumes, contains)
 * - Filter by direction (incoming, outgoing, upstream, downstream)
 * - Edge highlighting on canvas
 * - Isolate mode to show only selected edges
 * - Node-centric edge exploration
 */

import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
    X,
    GitBranch,
    ArrowRight,
    ArrowDownLeft,
    ArrowUpRight,
    ChevronDown,
    ChevronRight,
    Filter,
    Eye,
    EyeOff,
    Info,
    Workflow,
    Package,
    Database,
    Table2,
    Highlighter,
    Focus,
    Network,
    GitMerge,
    Sparkles,
    Zap,
    Check,
} from 'lucide-react'
import { useCanvasStore, type LineageEdge, type LineageNode } from '@/store/canvas'
import {
    useEdgeFiltersStore,
    useNodeEdges,
    EDGE_DIRECTION_FILTERS,
    type EdgeDirection,
} from '@/hooks/useEdgeFilters'
import { useSchemaStore, useContainmentEdgeTypes, useEdgeTypeMetadataMap, useRelationshipTypes } from '@/store/schema'
import { getAllEdgeTypeDefinitions, normalizeEdgeType } from '@/utils/edgeTypeUtils'
import { useEdgeVisual } from '@/hooks/useEntityVisual'
import { cn } from '@/lib/utils'

// ============================================
// Types
// ============================================

export interface EdgeTypeFilter {
    type: string
    label: string
    color: string
    enabled: boolean
}

/**
 * Generate edge type filters dynamically from discovered edge types
 */
export function generateEdgeTypeFilters(
    edges: LineageEdge[],
    relationshipTypes: any[],
    containmentEdgeTypes: string[],
    ontologyMetadata?: any
): EdgeTypeFilter[] {
    const definitions = getAllEdgeTypeDefinitions(
        edges,
        relationshipTypes,
        containmentEdgeTypes,
        ontologyMetadata ? { edgeTypeMetadata: ontologyMetadata.edgeTypeMetadata } : undefined
    )

    return definitions.map(def => ({
        type: def.type.toLowerCase(), // Use lowercase for filter matching
        label: def.label,
        color: def.color,
        enabled: true, // Default to enabled
    }))
}

// ============================================
// EdgeDetailPanel Component
// ============================================

interface EdgeDetailPanelProps {
    isOpen: boolean
    onClose: () => void
    onToggleFilter?: (type: string) => void
    edgeFilters?: EdgeTypeFilter[]
    className?: string
}

export function EdgeDetailPanel({
    isOpen,
    onClose,
    onToggleFilter,
    edgeFilters: providedFilters,
    className,
}: EdgeDetailPanelProps) {
    const nodes = useCanvasStore((s) => s.nodes)
    const edges = useCanvasStore((s) => s.edges)
    const selectedNodeIds = useCanvasStore((s) => s.selectedNodeIds)
    const selectedEdgeIds = useCanvasStore((s) => s.selectedEdgeIds)
    const selectEdge = useCanvasStore((s) => s.selectEdge)
    const clearSelection = useCanvasStore((s) => s.clearSelection)
    const relationshipTypes = useRelationshipTypes()
    const containmentEdgeTypes = useContainmentEdgeTypes()
    const edgeTypeMetadata = useEdgeTypeMetadataMap()
    const ontologyMetadata = useMemo(() => ({ edgeTypeMetadata }), [edgeTypeMetadata])
    const panelRef = useRef<HTMLDivElement>(null)

    // Click-outside to close panel
    useEffect(() => {
        if (!isOpen) return
        const handleMouseDown = (e: MouseEvent) => {
            if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
                onClose()
            }
        }
        document.addEventListener('mousedown', handleMouseDown)
        return () => document.removeEventListener('mousedown', handleMouseDown)
    }, [isOpen, onClose])

    // Generate filters dynamically if not provided
    const edgeFilters = useMemo(() => {
        if (providedFilters) {
            return providedFilters
        }
        return generateEdgeTypeFilters(
            edges,
            relationshipTypes,
            containmentEdgeTypes,
            ontologyMetadata
        )
    }, [providedFilters, edges, relationshipTypes, containmentEdgeTypes, ontologyMetadata])

    // Edge filtering store
    const directionFilter = useEdgeFiltersStore((s) => s.directionFilter)
    const setDirectionFilter = useEdgeFiltersStore((s) => s.setDirectionFilter)
    const focusedNodeId = useEdgeFiltersStore((s) => s.focusedNodeId)
    const setFocusedNode = useEdgeFiltersStore((s) => s.setFocusedNode)
    const highlightedEdgeIds = useEdgeFiltersStore((s) => s.highlightedEdgeIds)
    const toggleHighlightEdge = useEdgeFiltersStore((s) => s.toggleHighlightEdge)
    const setHighlightedEdges = useEdgeFiltersStore((s) => s.setHighlightedEdges)
    const clearHighlightedEdges = useEdgeFiltersStore((s) => s.clearHighlightedEdges)
    const isolateMode = useEdgeFiltersStore((s) => s.isolateMode)
    const toggleIsolateMode = useEdgeFiltersStore((s) => s.toggleIsolateMode)
    const highlightMode = useEdgeFiltersStore((s) => s.highlightMode)
    const setHighlightMode = useEdgeFiltersStore((s) => s.setHighlightMode)

    const [expandedEdgeId, setExpandedEdgeId] = useState<string | null>(null)
    const [showFilters, setShowFilters] = useState(false)
    const [showDirectionFilters, setShowDirectionFilters] = useState(false)
    const [activeTab, setActiveTab] = useState<'all' | 'selected' | 'highlighted'>('all')

    // Auto-focus on selected node
    const effectiveFocusedNodeId = focusedNodeId || selectedNodeIds[0] || null

    // Get node-centric edges if we have a focused node
    const nodeEdges = useNodeEdges(effectiveFocusedNodeId)

    // Create node lookup map
    const nodeMap = useMemo(() => {
        const map = new Map<string, LineageNode>()
        nodes.forEach(n => map.set(n.id, n))
        return map
    }, [nodes])

    // Get selected edges with details
    const selectedEdges = useMemo(() => {
        return selectedEdgeIds
            .map(id => edges.find(e => e.id === id))
            .filter((e): e is LineageEdge => e !== undefined)
    }, [edges, selectedEdgeIds])

    // Get highlighted edges
    const highlightedEdges = useMemo(() => {
        return edges.filter(e => highlightedEdgeIds.has(e.id))
    }, [edges, highlightedEdgeIds])

    // Get all visible edges based on filters and direction
    const visibleEdges = useMemo(() => {
        const enabledTypes = new Set(
            edgeFilters.filter(f => f.enabled).map(f => f.type)
        )

        let filtered = edges.filter(edge => {
            const normalized = normalizeEdgeType(edge).toLowerCase()
            return enabledTypes.has(normalized) || enabledTypes.has(normalizeEdgeType(edge))
        })

        // Apply direction filter if we have a focused node
        if (effectiveFocusedNodeId && directionFilter !== 'all') {
            switch (directionFilter) {
                case 'incoming':
                    filtered = nodeEdges.incomingEdges
                    break
                case 'outgoing':
                    filtered = nodeEdges.outgoingEdges
                    break
                case 'upstream':
                    filtered = nodeEdges.upstreamEdges
                    break
                case 'downstream':
                    filtered = nodeEdges.downstreamEdges
                    break
            }
        }

        return filtered
    }, [edges, edgeFilters, directionFilter, effectiveFocusedNodeId, nodeEdges])

    // Get edge statistics (normalized for matching)
    const edgeStats = useMemo(() => {
        const stats: Record<string, number> = {}
        edges.forEach(edge => {
            const normalized = normalizeEdgeType(edge).toLowerCase()
            stats[normalized] = (stats[normalized] || 0) + 1
            // Also count by original type for backward compatibility
            const originalType = edge.data?.edgeType || edge.data?.relationship || 'unknown'
            if (originalType.toLowerCase() !== normalized) {
                stats[originalType.toLowerCase()] = (stats[originalType.toLowerCase()] || 0) + 1
            }
        })
        return stats
    }, [edges])

    // Handle highlighting all visible edges
    const highlightAllVisible = useCallback(() => {
        setHighlightedEdges(visibleEdges.map(e => e.id))
    }, [visibleEdges, setHighlightedEdges])

    // Handle setting focus to selected node
    const handleFocusOnSelected = useCallback(() => {
        if (selectedNodeIds.length > 0) {
            setFocusedNode(selectedNodeIds[0])
        }
    }, [selectedNodeIds, setFocusedNode])

    if (!isOpen) return null

    const focusedNode = effectiveFocusedNodeId ? nodeMap.get(effectiveFocusedNodeId) : null

    return (
        <motion.div
            ref={panelRef}
            data-panel="edge-detail-panel"
            initial={{ x: 300, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 300, opacity: 0 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            className={cn(
                "absolute right-0 top-0 bottom-0 w-[clamp(400px,28vw,520px)] z-20",
                "bg-canvas-elevated/95 backdrop-blur-lg border-l border-glass-border",
                "flex flex-col shadow-lg",
                className
            )}
        >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-glass-border">
                <div className="flex items-center gap-2">
                    <GitBranch className="w-4 h-4 text-accent-lineage" />
                    <h3 className="font-semibold text-ink">Edge Explorer</h3>
                </div>
                <div className="flex items-center gap-1">
                    <button
                        onClick={() => setShowDirectionFilters(!showDirectionFilters)}
                        className={cn(
                            "w-8 h-8 rounded-lg flex items-center justify-center transition-colors",
                            showDirectionFilters
                                ? "bg-cyan-500/10 text-cyan-500"
                                : "text-ink-muted hover:text-ink hover:bg-black/5 dark:hover:bg-white/10"
                        )}
                        title="Direction filters"
                    >
                        <Network className="w-4 h-4" />
                    </button>
                    <button
                        onClick={() => setShowFilters(!showFilters)}
                        className={cn(
                            "w-8 h-8 rounded-lg flex items-center justify-center transition-colors",
                            showFilters
                                ? "bg-accent-lineage/10 text-accent-lineage"
                                : "text-ink-muted hover:text-ink hover:bg-black/5 dark:hover:bg-white/10"
                        )}
                        title="Type filters"
                    >
                        <Filter className="w-4 h-4" />
                    </button>
                    <button
                        onClick={onClose}
                        className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink hover:bg-black/5 dark:hover:bg-white/10 transition-colors"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>
            </div>

            {/* Focused Node Indicator */}
            {focusedNode && (
                <div className="px-4 py-2 border-b border-glass-border bg-cyan-500/5">
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <Focus className="w-3.5 h-3.5 text-cyan-500" />
                            <span className="text-xs text-ink-secondary">Focus:</span>
                            <span className="text-xs font-medium text-ink truncate max-w-[150px]">
                                {focusedNode.data.label}
                            </span>
                        </div>
                        <button
                            onClick={() => setFocusedNode(null)}
                            className="text-xs text-cyan-500 hover:text-cyan-600"
                        >
                            Clear
                        </button>
                    </div>
                    {/* Node edge stats */}
                    <div className="flex items-center gap-3 mt-1.5 text-2xs text-ink-muted">
                        <span>↘ {nodeEdges.edgeStats.incomingCount} in</span>
                        <span>↗ {nodeEdges.edgeStats.outgoingCount} out</span>
                        <span>⬆ {nodeEdges.edgeStats.upstreamCount} up</span>
                        <span>⬇ {nodeEdges.edgeStats.downstreamCount} down</span>
                    </div>
                </div>
            )}

            {/* Direction Filters */}
            <AnimatePresence>
                {showDirectionFilters && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden border-b border-glass-border"
                    >
                        <div className="p-3 space-y-2">
                            <div className="flex items-center justify-between">
                                <span className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                                    Direction
                                </span>
                                {!focusedNode && selectedNodeIds.length > 0 && (
                                    <button
                                        onClick={handleFocusOnSelected}
                                        className="text-2xs text-cyan-500 hover:text-cyan-600"
                                    >
                                        Focus on selected
                                    </button>
                                )}
                            </div>
                            <div className="grid grid-cols-2 gap-1.5">
                                {EDGE_DIRECTION_FILTERS.map(filter => (
                                    <button
                                        key={filter.id}
                                        onClick={() => setDirectionFilter(filter.id)}
                                        disabled={!focusedNode && filter.id !== 'all'}
                                        className={cn(
                                            "flex items-center gap-2 px-2.5 py-2 rounded-lg transition-colors duration-150 text-left",
                                            directionFilter === filter.id
                                                ? "bg-cyan-500/10 border border-cyan-500/30 text-cyan-600 dark:text-cyan-400"
                                                : "border border-transparent hover:bg-black/5 dark:hover:bg-white/5",
                                            !focusedNode && filter.id !== 'all' && "opacity-40 cursor-not-allowed"
                                        )}
                                    >
                                        <DirectionIcon id={filter.id} className="w-3.5 h-3.5" />
                                        <div className="flex-1 min-w-0">
                                            <div className="text-xs font-medium truncate">{filter.label}</div>
                                        </div>
                                        {directionFilter === filter.id && (
                                            <Check className="w-3 h-3 text-cyan-500" />
                                        )}
                                    </button>
                                ))}
                            </div>
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>

            {/* Type Filters */}
            <AnimatePresence>
                {showFilters && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden border-b border-glass-border"
                    >
                        <div className="p-3 space-y-2">
                            <div className="text-xs font-medium text-ink-muted uppercase tracking-wider">
                                Edge Type
                            </div>
                            {edgeFilters.map(filter => (
                                <button
                                    key={filter.type}
                                    onClick={() => onToggleFilter?.(filter.type)}
                                    className={cn(
                                        "w-full flex items-center justify-between px-3 py-2 rounded-lg transition-colors",
                                        filter.enabled
                                            ? "bg-black/5 dark:bg-white/10"
                                            : "opacity-50"
                                    )}
                                >
                                    <div className="flex items-center gap-2">
                                        <div
                                            className="w-3 h-3 rounded-full"
                                            style={{ backgroundColor: filter.color }}
                                        />
                                        <span className="text-sm text-ink">{filter.label}</span>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <span className="text-xs text-ink-muted">
                                            {edgeStats[filter.type] || edgeStats[filter.type.toUpperCase()] || 0}
                                        </span>
                                        {filter.enabled ? (
                                            <Eye className="w-3.5 h-3.5 text-ink-muted" />
                                        ) : (
                                            <EyeOff className="w-3.5 h-3.5 text-ink-muted" />
                                        )}
                                    </div>
                                </button>
                            ))}
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>

            {/* Highlight Controls */}
            <div className="px-4 py-2 border-b border-glass-border bg-black/3 dark:bg-white/3">
                <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                        <Highlighter className="w-3.5 h-3.5 text-amber-500" />
                        <span className="text-xs text-ink-muted">
                            {highlightedEdgeIds.size} highlighted
                        </span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            onClick={highlightAllVisible}
                            className="text-2xs text-amber-500 hover:text-amber-600 px-2 py-1"
                            title="Highlight all visible"
                        >
                            <Sparkles className="w-3 h-3" />
                        </button>
                        <button
                            onClick={clearHighlightedEdges}
                            className="text-2xs text-ink-muted hover:text-ink px-2 py-1"
                            title="Clear highlights"
                        >
                            Clear
                        </button>
                        <button
                            onClick={toggleIsolateMode}
                            className={cn(
                                "text-2xs px-2 py-1 rounded transition-colors",
                                isolateMode
                                    ? "bg-amber-500/10 text-amber-500"
                                    : "text-ink-muted hover:text-ink"
                            )}
                            title="Isolate mode - only show highlighted"
                        >
                            <Zap className="w-3 h-3" />
                        </button>
                    </div>
                </div>
                {/* Highlight mode selector */}
                <div className="flex items-center gap-1 mt-1.5">
                    <span className="text-2xs text-ink-muted mr-1">Style:</span>
                    {(['glow', 'pulse', 'bold'] as const).map(mode => (
                        <button
                            key={mode}
                            onClick={() => setHighlightMode(mode)}
                            className={cn(
                                "text-2xs px-2 py-0.5 rounded capitalize transition-colors",
                                highlightMode === mode
                                    ? "bg-amber-500/10 text-amber-500"
                                    : "text-ink-muted hover:text-ink"
                            )}
                        >
                            {mode}
                        </button>
                    ))}
                </div>
            </div>

            {/* Statistics */}
            <div className="px-4 py-2 border-b border-glass-border">
                <div className="flex items-center justify-between text-xs">
                    <span className="text-ink-muted">
                        {visibleEdges.length} of {edges.length} edges
                    </span>
                    <span className="text-ink-muted">
                        {selectedEdgeIds.length} selected
                    </span>
                </div>
            </div>

            {/* Tabs */}
            <div className="flex border-b border-glass-border">
                {[
                    { id: 'all' as const, label: 'All', count: visibleEdges.length },
                    { id: 'selected' as const, label: 'Selected', count: selectedEdgeIds.length },
                    { id: 'highlighted' as const, label: 'Highlighted', count: highlightedEdgeIds.size },
                ].map(tab => (
                    <button
                        key={tab.id}
                        onClick={() => setActiveTab(tab.id)}
                        className={cn(
                            "flex-1 px-3 py-2 text-xs font-medium transition-colors",
                            activeTab === tab.id
                                ? "bg-black/5 dark:bg-white/5 text-ink border-b-2 border-accent-lineage"
                                : "text-ink-muted hover:text-ink"
                        )}
                    >
                        {tab.label} ({tab.count})
                    </button>
                ))}
            </div>

            {/* Edge List */}
            <div className="flex-1 overflow-y-auto custom-scrollbar">
                {activeTab === 'selected' && selectedEdges.length > 0 && (
                    <div className="p-3 space-y-2">
                        {selectedEdges.map(edge => (
                            <EdgeCard
                                key={edge.id}
                                edge={edge}
                                nodeMap={nodeMap}
                                isExpanded={expandedEdgeId === edge.id}
                                isHighlighted={highlightedEdgeIds.has(edge.id)}
                                onToggleExpand={() => setExpandedEdgeId(
                                    expandedEdgeId === edge.id ? null : edge.id
                                )}
                                onToggleHighlight={() => toggleHighlightEdge(edge.id)}
                                onDeselect={() => {
                                    if (selectedEdgeIds.length === 1) {
                                        clearSelection()
                                    } else {
                                        selectEdge(edge.id, true)
                                    }
                                }}
                            />
                        ))}
                    </div>
                )}

                {activeTab === 'highlighted' && highlightedEdges.length > 0 && (
                    <div className="p-3 space-y-2">
                        {highlightedEdges.map(edge => (
                            <EdgeCard
                                key={edge.id}
                                edge={edge}
                                nodeMap={nodeMap}
                                isExpanded={expandedEdgeId === edge.id}
                                isHighlighted={true}
                                onToggleExpand={() => setExpandedEdgeId(
                                    expandedEdgeId === edge.id ? null : edge.id
                                )}
                                onToggleHighlight={() => toggleHighlightEdge(edge.id)}
                                onSelect={() => selectEdge(edge.id)}
                            />
                        ))}
                    </div>
                )}

                {activeTab === 'all' && visibleEdges.length > 0 && (
                    <div className="p-3 space-y-2">
                        {visibleEdges.slice(0, 100).map(edge => (
                            <EdgeCard
                                key={edge.id}
                                edge={edge}
                                nodeMap={nodeMap}
                                isExpanded={expandedEdgeId === edge.id}
                                isHighlighted={highlightedEdgeIds.has(edge.id)}
                                onToggleExpand={() => setExpandedEdgeId(
                                    expandedEdgeId === edge.id ? null : edge.id
                                )}
                                onToggleHighlight={() => toggleHighlightEdge(edge.id)}
                                onSelect={() => selectEdge(edge.id)}
                                compact
                            />
                        ))}
                        {visibleEdges.length > 100 && (
                            <div className="text-xs text-center text-ink-muted py-2">
                                + {visibleEdges.length - 100} more edges
                            </div>
                        )}
                    </div>
                )}

                {/* Empty States */}
                {activeTab === 'all' && visibleEdges.length === 0 && (
                    <EmptyState message="No edges match the current filters" />
                )}
                {activeTab === 'selected' && selectedEdges.length === 0 && (
                    <EmptyState message="Click on an edge to select it" />
                )}
                {activeTab === 'highlighted' && highlightedEdges.length === 0 && (
                    <EmptyState message="Click the highlight icon on edges to add them" />
                )}
            </div>
        </motion.div>
    )
}

// ============================================
// Direction Icon Component
// ============================================

function DirectionIcon({ id, className }: { id: EdgeDirection; className?: string }) {
    switch (id) {
        case 'all':
            return <Network className={className} />
        case 'incoming':
            return <ArrowDownLeft className={className} />
        case 'outgoing':
            return <ArrowUpRight className={className} />
        case 'upstream':
            return <GitMerge className={className} />
        case 'downstream':
            return <GitBranch className={className} />
        default:
            return <Network className={className} />
    }
}

// ============================================
// Empty State Component
// ============================================

function EmptyState({ message }: { message: string }) {
    return (
        <div className="p-6 text-center">
            <Info className="w-8 h-8 mx-auto mb-3 text-ink-muted opacity-40" />
            <p className="text-sm text-ink-muted">{message}</p>
        </div>
    )
}

// ============================================
// EdgeCard Component
// ============================================

interface EdgeCardProps {
    edge: LineageEdge
    nodeMap: Map<string, LineageNode>
    isExpanded: boolean
    isHighlighted: boolean
    onToggleExpand: () => void
    onToggleHighlight: () => void
    onSelect?: () => void
    onDeselect?: () => void
    compact?: boolean
}

function EdgeCard({
    edge,
    nodeMap,
    isExpanded,
    isHighlighted,
    onToggleExpand,
    onToggleHighlight,
    onSelect,
    onDeselect,
    compact = false,
}: EdgeCardProps) {
    const sourceNode = nodeMap.get(edge.source)
    const targetNode = nodeMap.get(edge.target)

    const edgeType = edge.data?.edgeType || edge.data?.relationship || 'unknown'
    const confidence = edge.data?.confidence
    const label = edge.data?.label

    // Resolve color from schema via hook (falls back to #6366f1 for unknown types)
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const edgeVisual = useEdgeVisual(edgeType)
    const color = edgeVisual.strokeColor
    const EdgeIcon = GitBranch  // Generic fallback; icon resolution TBD in Phase 4d

    return (
        <motion.div
            layout
            className={cn(
                "rounded-lg border transition-colors duration-150 cursor-pointer",
                "bg-canvas hover:shadow-sm",
                isHighlighted && "ring-2 ring-amber-500/50",
                compact ? "border-glass-border" : "border-l-2"
            )}
            style={{ borderLeftColor: compact ? undefined : color }}
            onClick={onSelect ?? onToggleExpand}
        >
            {/* Header */}
            <div className="flex items-center gap-2 px-3 py-2">
                <button
                    onClick={(e) => {
                        e.stopPropagation()
                        onToggleExpand()
                    }}
                    className="w-5 h-5 flex items-center justify-center flex-shrink-0"
                >
                    {isExpanded ? (
                        <ChevronDown className="w-3.5 h-3.5 text-ink-muted" />
                    ) : (
                        <ChevronRight className="w-3.5 h-3.5 text-ink-muted" />
                    )}
                </button>

                <div
                    className="w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0"
                    style={{ backgroundColor: `${color}15` }}
                >
                    <EdgeIcon className="w-3 h-3" style={{ color }} />
                </div>

                <div className="flex-1 min-w-0">
                    <span
                        className="text-xs font-medium uppercase tracking-wider"
                        style={{ color }}
                    >
                        {edgeType}
                    </span>
                    {label && (
                        <div className="text-2xs text-ink-muted truncate">{label}</div>
                    )}
                </div>

                {confidence !== undefined && (
                    <span className="text-2xs text-ink-muted px-1.5 py-0.5 rounded bg-black/5 dark:bg-white/10">
                        {Math.round(confidence * 100)}%
                    </span>
                )}

                {/* Highlight button */}
                <button
                    onClick={(e) => {
                        e.stopPropagation()
                        onToggleHighlight()
                    }}
                    className={cn(
                        "w-6 h-6 flex items-center justify-center rounded transition-colors",
                        isHighlighted
                            ? "bg-amber-500/10 text-amber-500"
                            : "text-ink-muted hover:text-amber-500 hover:bg-amber-500/5"
                    )}
                    title={isHighlighted ? "Remove highlight" : "Highlight edge"}
                >
                    <Highlighter className="w-3 h-3" />
                </button>

                {onDeselect && (
                    <button
                        onClick={(e) => {
                            e.stopPropagation()
                            onDeselect()
                        }}
                        className="w-5 h-5 flex items-center justify-center text-ink-muted hover:text-ink"
                    >
                        <X className="w-3 h-3" />
                    </button>
                )}
            </div>

            {/* Source → Target */}
            <div className="px-3 pb-2 flex items-center gap-2 text-xs min-w-0">
                <span className="truncate flex-1 min-w-0 text-ink-secondary" title={sourceNode?.data.label as string ?? edge.source}>
                    {sourceNode?.data.label || edge.source}
                </span>
                <ArrowRight className="w-3 h-3 text-ink-muted flex-shrink-0" />
                <span className="truncate flex-1 min-w-0 text-ink-secondary" title={targetNode?.data.label as string ?? edge.target}>
                    {targetNode?.data.label || edge.target}
                </span>
            </div>

            {/* Expanded Details */}
            <AnimatePresence>
                {isExpanded && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden"
                    >
                        <div className="px-3 pb-3 pt-1 border-t border-glass-border/50 space-y-2">
                            {/* Edge ID */}
                            <div className="text-2xs">
                                <span className="text-ink-muted">ID: </span>
                                <code className="text-ink-secondary font-mono bg-black/5 dark:bg-white/5 px-1 rounded break-all">
                                    {edge.id}
                                </code>
                            </div>

                            {/* Source Details */}
                            <div className="text-2xs">
                                <span className="text-ink-muted">Source: </span>
                                <span className="text-ink-secondary break-all">
                                    {sourceNode?.data.urn || edge.source}
                                </span>
                            </div>

                            {/* Target Details */}
                            <div className="text-2xs">
                                <span className="text-ink-muted">Target: </span>
                                <span className="text-ink-secondary break-all">
                                    {targetNode?.data.urn || edge.target}
                                </span>
                            </div>

                            {/* Confidence */}
                            {confidence !== undefined && (
                                <div className="text-2xs">
                                    <span className="text-ink-muted">Confidence: </span>
                                    <span className="text-ink-secondary">{(confidence * 100).toFixed(1)}%</span>
                                    <div className="mt-1 h-1.5 rounded-full bg-black/10 dark:bg-white/10 overflow-hidden">
                                        <div
                                            className="h-full rounded-full transition-colors duration-150"
                                            style={{
                                                width: `${confidence * 100}%`,
                                                backgroundColor: color
                                            }}
                                        />
                                    </div>
                                </div>
                            )}

                            {/* Aggregated Edge Info */}
                            {edge.data?.isAggregated && (
                                <div className="text-2xs bg-amber-500/10 text-amber-600 dark:text-amber-400 px-2 py-1 rounded">
                                    Aggregated from {edge.data.sourceEdgeCount} edges
                                </div>
                            )}

                            {/* Additional Metadata */}
                            {edge.data && Object.keys(edge.data).filter(k =>
                                !['confidence', 'edgeType', 'relationship', 'animated', 'isAggregated', 'sourceEdgeCount', 'sourceEdges', 'label'].includes(k)
                            ).length > 0 && (
                                    <div className="text-2xs">
                                        <span className="text-ink-muted">Properties: </span>
                                        <div className="mt-1 font-mono bg-black/5 dark:bg-white/5 p-2 rounded text-ink-secondary overflow-x-auto text-2xs">
                                            {Object.entries(edge.data)
                                                .filter(([k]) => !['confidence', 'edgeType', 'relationship', 'animated', 'isAggregated', 'sourceEdgeCount', 'sourceEdges', 'label'].includes(k))
                                                .map(([k, v]) => (
                                                    <div key={k}>{k}: {JSON.stringify(v)}</div>
                                                ))
                                            }
                                        </div>
                                    </div>
                                )}
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>
        </motion.div>
    )
}

export default EdgeDetailPanel
