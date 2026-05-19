/**
 * TraceToolbar - Reusable trace controls component
 * 
 * Provides a floating toolbar for trace operations including:
 * - Quick preset buttons (Upstream Only, Downstream Only, Full Trace)
 * - Depth slider (1-99 hops)
 * - Direction toggles with counts
 * - Statistics panel (total nodes, edge types, impact summary)
 * - Re-trace button when config changes
 * - Quick actions (copy URNs, export, pin)
 */

import React, { useState, useCallback, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import {
    useTraceStore,
    type TraceConfig,
    type TraceResult,
    type TraceStatistics
} from '@/hooks/useUnifiedTrace'

// ============================================
// Types
// ============================================

interface TraceToolbarProps {
    /** Name of the focused node */
    focusNodeName?: string
    /** Upstream node count */
    upstreamCount: number
    /** Downstream node count */
    downstreamCount: number
    /** Whether upstream is visible */
    showUpstream: boolean
    /** Whether downstream is visible */
    showDownstream: boolean
    /** Toggle upstream visibility */
    onToggleUpstream: () => void
    /** Toggle downstream visibility */
    onToggleDownstream: () => void
    /** Exit/clear trace */
    onExitTrace: () => void
    /** Re-trace with current settings */
    onRetrace?: () => void
    /** Quick preset: upstream only */
    onTraceUpstream?: () => void
    /** Quick preset: downstream only */
    onTraceDownstream?: () => void
    /** Quick preset: full trace */
    onTraceFullLineage?: () => void
    /** Current configuration */
    config: TraceConfig
    /** Update configuration */
    onConfigChange: (config: Partial<TraceConfig>) => void
    /** Trace result for export functionality */
    traceResult?: TraceResult | null
    /** Trace statistics */
    statistics?: TraceStatistics
    /** Is currently loading */
    isLoading?: boolean
    /** Available lineage edge types from ontology (for filtering) */
    availableLineageEdgeTypes?: string[]
    /** Additional className */
    className?: string
    /** Position variant */
    position?: 'top' | 'bottom' | 'floating'
    /** Pin Lineage — number of pinned trace-path endpoints */
    pinnedCount?: number
    /** Pin Lineage — how off-path elements are shown */
    pinDisplayMode?: 'hide' | 'dim'
    /** Pin Lineage — switch off-path display mode */
    onSetPinDisplayMode?: (mode: 'hide' | 'dim') => void
    /** Pin Lineage — clear all pins */
    onClearPins?: () => void
}

// ============================================
// Component
// ============================================

export function TraceToolbar({
    focusNodeName = 'Unknown Node',
    upstreamCount,
    downstreamCount,
    showUpstream,
    showDownstream,
    onToggleUpstream,
    onToggleDownstream,
    onExitTrace,
    onRetrace,
    onTraceUpstream,
    onTraceDownstream,
    onTraceFullLineage,
    config,
    onConfigChange,
    traceResult,
    statistics,
    isLoading = false,
    availableLineageEdgeTypes = [],
    className,
    position = 'floating',
    pinnedCount = 0,
    pinDisplayMode = 'hide',
    onSetPinDisplayMode,
    onClearPins,
}: TraceToolbarProps) {
    const [isExpanded, setIsExpanded] = useState(false)
    const [showStats, setShowStats] = useState(false)
    const [copiedMessage, setCopiedMessage] = useState<string | null>(null)
    const [configChanged, setConfigChanged] = useState(false)
    const prevConfigRef = useRef(config)

    // Detect config changes for re-trace prompt
    useEffect(() => {
        const prev = prevConfigRef.current
        if (
            prev.upstreamDepth !== config.upstreamDepth ||
            prev.downstreamDepth !== config.downstreamDepth ||
            prev.includeColumnLineage !== config.includeColumnLineage ||
            prev.excludeContainmentEdges !== config.excludeContainmentEdges ||
            JSON.stringify(prev.lineageEdgeTypes) !== JSON.stringify(config.lineageEdgeTypes)
        ) {
            setConfigChanged(true)
        }
        prevConfigRef.current = config
    }, [config])

    // Copy URNs to clipboard
    const handleCopyUrns = useCallback(async () => {
        if (!traceResult) return

        const urns = Array.from(traceResult.traceNodes).join('\n')
        try {
            await navigator.clipboard.writeText(urns)
            setCopiedMessage('URNs copied!')
            setTimeout(() => setCopiedMessage(null), 2000)
        } catch {
            setCopiedMessage('Failed to copy')
            setTimeout(() => setCopiedMessage(null), 2000)
        }
    }, [traceResult])

    // Export trace as JSON
    const handleExport = useCallback(() => {
        if (!traceResult || !traceResult.lineageResult) return

        const exportData = {
            focusId: traceResult.focusId,
            timestamp: new Date().toISOString(),
            config,
            nodes: traceResult.lineageResult.nodes,
            edges: traceResult.lineageResult.edges,
            upstream: Array.from(traceResult.upstreamNodes),
            downstream: Array.from(traceResult.downstreamNodes),
        }

        const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `trace-${traceResult.focusId.replace(/[^a-zA-Z0-9]/g, '_')}.json`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
    }, [traceResult, config])

    // Position styles
    const positionClasses = {
        top: 'absolute top-4 left-1/2 -translate-x-1/2',
        bottom: 'absolute bottom-4 left-1/2 -translate-x-1/2',
        floating: 'fixed top-16 left-1/2 -translate-x-1/2',
    }

    return (
        <motion.div
            initial={{ y: -20, opacity: 0, scale: 0.95 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: -20, opacity: 0, scale: 0.95 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            className={cn(
                "z-50 glass-panel border border-accent-lineage/30 shadow-lg shadow-accent-lineage/10 rounded-2xl",
                positionClasses[position],
                className
            )}
        >
            {/* Main Toolbar Row */}
            <div className="flex items-center gap-2 px-4 py-2">
                {/* Loading indicator or Focus Indicator */}
                {isLoading ? (
                    <div className="flex items-center gap-2 text-sm font-medium text-ink">
                        <LucideIcons.Loader2 className="w-4 h-4 animate-spin text-accent-lineage" />
                        <span className="text-ink-muted">Tracing...</span>
                    </div>
                ) : (
                    <div className="flex items-center gap-2 text-sm font-medium text-ink">
                        <motion.span
                            className="w-2 h-2 rounded-full bg-accent-lineage"
                            animate={{ scale: [1, 1.2, 1] }}
                            transition={{ duration: 1.5, repeat: Infinity }}
                        />
                        <span className="text-ink-muted text-xs">Tracing</span>
                        <span className="font-bold text-accent-lineage max-w-[150px] truncate">
                            {focusNodeName}
                        </span>
                    </div>
                )}

                {/* Divider */}
                <div className="h-4 w-[1px] bg-glass-border" />

                {/* Quick Preset Buttons */}
                {(onTraceUpstream || onTraceDownstream || onTraceFullLineage) && (
                    <>
                        <div className="flex items-center gap-1">
                            {onTraceUpstream && (
                                <button
                                    onClick={onTraceUpstream}
                                    className="px-2 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-600 dark:text-blue-400 hover:bg-blue-500/20 transition-all flex items-center gap-1"
                                    title="Upstream only (Root Cause)"
                                >
                                    <LucideIcons.ArrowUpLeft className="w-3 h-3" />
                                    Root Cause
                                </button>
                            )}
                            {onTraceDownstream && (
                                <button
                                    onClick={onTraceDownstream}
                                    className="px-2 py-1 rounded-md text-xs font-medium bg-green-500/10 text-green-600 dark:text-green-400 hover:bg-green-500/20 transition-all flex items-center gap-1"
                                    title="Downstream only (Impact)"
                                >
                                    <LucideIcons.ArrowDownRight className="w-3 h-3" />
                                    Impact
                                </button>
                            )}
                            {onTraceFullLineage && (
                                <button
                                    onClick={onTraceFullLineage}
                                    className="px-2 py-1 rounded-md text-xs font-medium bg-purple-500/10 text-purple-600 dark:text-purple-400 hover:bg-purple-500/20 transition-all flex items-center gap-1"
                                    title="Full lineage (both)"
                                >
                                    <LucideIcons.GitBranch className="w-3 h-3" />
                                    Full
                                </button>
                            )}
                        </div>
                        <div className="h-4 w-[1px] bg-glass-border" />
                    </>
                )}

                {/* Direction Toggles */}
                <div className="flex items-center gap-1 bg-black/5 dark:bg-white/5 rounded-lg p-0.5">
                    <button
                        onClick={onToggleUpstream}
                        className={cn(
                            "p-1.5 rounded-md transition-all text-xs font-medium flex items-center gap-1",
                            showUpstream
                                ? "bg-blue-500 text-white shadow-sm"
                                : "hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted"
                        )}
                        title={`${showUpstream ? 'Hide' : 'Show'} upstream (${upstreamCount})`}
                    >
                        <LucideIcons.ArrowLeft className="w-3.5 h-3.5" />
                        <span className="min-w-[16px]">{upstreamCount}</span>
                    </button>
                    <button
                        onClick={onToggleDownstream}
                        className={cn(
                            "p-1.5 rounded-md transition-all text-xs font-medium flex items-center gap-1",
                            showDownstream
                                ? "bg-green-500 text-white shadow-sm"
                                : "hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted"
                        )}
                        title={`${showDownstream ? 'Hide' : 'Show'} downstream (${downstreamCount})`}
                    >
                        <span className="min-w-[16px]">{downstreamCount}</span>
                        <LucideIcons.ArrowRight className="w-3.5 h-3.5" />
                    </button>
                </div>

                {/* Divider */}
                <div className="h-4 w-[1px] bg-glass-border" />

                {/* Statistics Button */}
                {statistics && (
                    <button
                        onClick={() => setShowStats(!showStats)}
                        className={cn(
                            "p-1.5 rounded-md transition-all flex items-center gap-1",
                            showStats
                                ? "bg-accent-lineage/10 text-accent-lineage"
                                : "hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted"
                        )}
                        title="Trace statistics"
                    >
                        <LucideIcons.BarChart3 className="w-4 h-4" />
                    </button>
                )}

                {/* Expand/Settings Button */}
                <button
                    onClick={() => setIsExpanded(!isExpanded)}
                    className={cn(
                        "p-1.5 rounded-md transition-all",
                        isExpanded
                            ? "bg-accent-lineage/10 text-accent-lineage"
                            : "hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted"
                    )}
                    title="Trace settings"
                >
                    <LucideIcons.Settings className="w-4 h-4" />
                </button>

                {/* Re-trace Button (shown when config changed) */}
                {configChanged && onRetrace && (
                    <motion.button
                        initial={{ scale: 0 }}
                        animate={{ scale: 1 }}
                        onClick={() => {
                            onRetrace()
                            setConfigChanged(false)
                        }}
                        className="px-2 py-1 rounded-md text-xs font-medium bg-amber-500/10 text-amber-600 dark:text-amber-400 hover:bg-amber-500/20 transition-all flex items-center gap-1 border border-amber-500/30"
                        title="Re-trace with new settings"
                    >
                        <LucideIcons.RefreshCw className="w-3 h-3" />
                        Re-trace
                    </motion.button>
                )}

                {/* Pin Lineage — isolate the focus↔pin sub-lineage */}
                {pinnedCount > 0 && (
                    <>
                        <div className="h-4 w-[1px] bg-glass-border" />
                        <div className="flex items-center gap-1">
                            <span
                                className="flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium bg-amber-500/10 text-amber-600 dark:text-amber-400"
                                title="Pinned trace-path endpoints"
                            >
                                <LucideIcons.Pin className="w-3 h-3" />
                                {pinnedCount} pinned
                            </span>
                            {onSetPinDisplayMode && (
                                <div className="flex items-center bg-black/5 dark:bg-white/5 rounded-lg p-0.5">
                                    <button
                                        onClick={() => onSetPinDisplayMode('hide')}
                                        className={cn(
                                            "px-2 py-1 rounded-md text-2xs font-medium transition-all",
                                            pinDisplayMode === 'hide'
                                                ? "bg-amber-500 text-white shadow-sm"
                                                : "text-ink-muted hover:bg-black/5 dark:hover:bg-white/10"
                                        )}
                                        title="Hide everything off the pinned path"
                                    >
                                        Isolate
                                    </button>
                                    <button
                                        onClick={() => onSetPinDisplayMode('dim')}
                                        className={cn(
                                            "px-2 py-1 rounded-md text-2xs font-medium transition-all",
                                            pinDisplayMode === 'dim'
                                                ? "bg-amber-500 text-white shadow-sm"
                                                : "text-ink-muted hover:bg-black/5 dark:hover:bg-white/10"
                                        )}
                                        title="Keep full graph as dimmed context"
                                    >
                                        Dim
                                    </button>
                                </div>
                            )}
                            {onClearPins && (
                                <button
                                    onClick={onClearPins}
                                    className="p-1.5 rounded-md hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted transition-all"
                                    title="Clear all pins"
                                >
                                    <LucideIcons.PinOff className="w-4 h-4" />
                                </button>
                            )}
                        </div>
                    </>
                )}

                {/* Quick Actions */}
                <div className="flex items-center gap-1">
                    <button
                        onClick={handleCopyUrns}
                        className="p-1.5 rounded-md hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted transition-all"
                        title="Copy URNs"
                    >
                        <LucideIcons.Copy className="w-4 h-4" />
                    </button>
                    <button
                        onClick={handleExport}
                        className="p-1.5 rounded-md hover:bg-black/5 dark:hover:bg-white/10 text-ink-muted transition-all"
                        title="Export trace"
                    >
                        <LucideIcons.Download className="w-4 h-4" />
                    </button>
                </div>

                {/* Divider */}
                <div className="h-4 w-[1px] bg-glass-border" />

                {/* Exit Button */}
                <button
                    onClick={onExitTrace}
                    className="text-xs font-semibold text-ink-muted hover:text-ink flex items-center gap-1 transition-colors"
                >
                    <LucideIcons.X className="w-3.5 h-3.5" />
                    Exit
                </button>
            </div>

            {/* Statistics Panel */}
            <AnimatePresence>
                {showStats && statistics && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden border-t border-glass-border"
                    >
                        <div className="p-3">
                            <div className="grid grid-cols-4 gap-3">
                                {/* Total Nodes */}
                                <div className="text-center p-2 rounded-lg bg-black/5 dark:bg-white/5">
                                    <div className="text-lg font-bold text-ink">{statistics.totalNodes}</div>
                                    <div className="text-2xs text-ink-muted">Total Nodes</div>
                                </div>

                                {/* Upstream */}
                                <div className="text-center p-2 rounded-lg bg-blue-500/10">
                                    <div className="text-lg font-bold text-blue-600 dark:text-blue-400">{statistics.upstreamCount}</div>
                                    <div className="text-2xs text-blue-600/70 dark:text-blue-400/70">Upstream</div>
                                </div>

                                {/* Downstream */}
                                <div className="text-center p-2 rounded-lg bg-green-500/10">
                                    <div className="text-lg font-bold text-green-600 dark:text-green-400">{statistics.downstreamCount}</div>
                                    <div className="text-2xs text-green-600/70 dark:text-green-400/70">Downstream</div>
                                </div>

                                {/* Edges */}
                                <div className="text-center p-2 rounded-lg bg-black/5 dark:bg-white/5">
                                    <div className="text-lg font-bold text-ink">{statistics.totalEdges}</div>
                                    <div className="text-2xs text-ink-muted">Edges</div>
                                </div>
                            </div>

                            {/* Edge Types */}
                            {statistics.edgeTypes.length > 0 && (
                                <div className="mt-3 flex items-center gap-2 flex-wrap">
                                    <span className="text-2xs text-ink-muted">Edge types:</span>
                                    {statistics.edgeTypes.map(type => (
                                        <span
                                            key={type}
                                            className="px-2 py-0.5 rounded text-2xs font-medium bg-accent-lineage/10 text-accent-lineage"
                                        >
                                            {type}
                                        </span>
                                    ))}
                                </div>
                            )}

                            {/* Inherited Notice */}
                            {statistics.isInherited && (
                                <div className="mt-2 flex items-center gap-2 text-xs text-amber-600 dark:text-amber-400">
                                    <LucideIcons.Info className="w-3 h-3" />
                                    <span>Lineage inherited from parent: {statistics.inheritedFrom}</span>
                                </div>
                            )}
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>

            {/* Expanded Settings Panel */}
            <AnimatePresence>
                {isExpanded && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden border-t border-glass-border"
                    >
                        <div className="p-4 space-y-4">
                            {/* Depth Controls */}
                            <div className="grid grid-cols-2 gap-4">
                                {/* Upstream Depth */}
                                <div className="space-y-2">
                                    <div className="flex items-center justify-between">
                                        <label className="text-xs font-medium text-ink-muted flex items-center gap-1">
                                            <LucideIcons.ArrowUpLeft className="w-3 h-3 text-blue-500" />
                                            Upstream Depth
                                        </label>
                                        <span className="text-xs font-bold text-blue-500">
                                            {config.upstreamDepth}
                                        </span>
                                    </div>
                                    <input
                                        type="range"
                                        min={0}
                                        max={20}
                                        value={config.upstreamDepth}
                                        onChange={(e) => onConfigChange({ upstreamDepth: parseInt(e.target.value) })}
                                        className="w-full h-1.5 rounded-full bg-black/10 dark:bg-white/10 appearance-none cursor-pointer accent-blue-500"
                                    />
                                    <div className="flex justify-between text-2xs text-ink-muted">
                                        <span>0</span>
                                        <span>20</span>
                                    </div>
                                </div>

                                {/* Downstream Depth */}
                                <div className="space-y-2">
                                    <div className="flex items-center justify-between">
                                        <label className="text-xs font-medium text-ink-muted flex items-center gap-1">
                                            <LucideIcons.ArrowDownRight className="w-3 h-3 text-green-500" />
                                            Downstream Depth
                                        </label>
                                        <span className="text-xs font-bold text-green-500">
                                            {config.downstreamDepth}
                                        </span>
                                    </div>
                                    <input
                                        type="range"
                                        min={0}
                                        max={20}
                                        value={config.downstreamDepth}
                                        onChange={(e) => onConfigChange({ downstreamDepth: parseInt(e.target.value) })}
                                        className="w-full h-1.5 rounded-full bg-black/10 dark:bg-white/10 appearance-none cursor-pointer accent-green-500"
                                    />
                                    <div className="flex justify-between text-2xs text-ink-muted">
                                        <span>0</span>
                                        <span>20</span>
                                    </div>
                                </div>
                            </div>

                            {/* Toggle Options */}
                            <div className="grid grid-cols-2 gap-3">
                                <label className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                                    <input
                                        type="checkbox"
                                        checked={config.includeColumnLineage}
                                        onChange={(e) => onConfigChange({ includeColumnLineage: e.target.checked })}
                                        className="rounded text-accent-lineage"
                                    />
                                    <div>
                                        <span className="text-xs text-ink block">Include column lineage</span>
                                        <span className="text-2xs text-ink-muted">Show field-level dependencies</span>
                                    </div>
                                </label>

                                <label className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                                    <input
                                        type="checkbox"
                                        checked={config.excludeContainmentEdges}
                                        onChange={(e) => onConfigChange({ excludeContainmentEdges: e.target.checked })}
                                        className="rounded text-accent-lineage"
                                    />
                                    <div>
                                        <span className="text-xs text-ink block">Exclude containment</span>
                                        <span className="text-2xs text-ink-muted">Hide structural edges</span>
                                    </div>
                                </label>

                                <label className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                                    <input
                                        type="checkbox"
                                        checked={config.includeInheritedLineage}
                                        onChange={(e) => onConfigChange({ includeInheritedLineage: e.target.checked })}
                                        className="rounded text-accent-lineage"
                                    />
                                    <div>
                                        <span className="text-xs text-ink block">Inherit from parent</span>
                                        <span className="text-2xs text-ink-muted">Use parent lineage if none</span>
                                    </div>
                                </label>

                                <label className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                                    <input
                                        type="checkbox"
                                        checked={config.pathOnly}
                                        onChange={(e) => onConfigChange({ pathOnly: e.target.checked })}
                                        className="rounded text-accent-lineage"
                                    />
                                    <div>
                                        <span className="text-xs text-ink block">Path only</span>
                                        <span className="text-2xs text-ink-muted">Hide non-path context</span>
                                    </div>
                                </label>

                                <label className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                                    <input
                                        type="checkbox"
                                        checked={config.autoExpandAncestors}
                                        onChange={(e) => onConfigChange({ autoExpandAncestors: e.target.checked })}
                                        className="rounded text-accent-lineage"
                                    />
                                    <div>
                                        <span className="text-xs text-ink block">Auto-expand ancestors</span>
                                        <span className="text-2xs text-ink-muted">Reveal path containers</span>
                                    </div>
                                </label>

                                <label className="flex items-center gap-2 cursor-pointer p-2 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                                    <input
                                        type="checkbox"
                                        checked={config.autoSyncToStore}
                                        onChange={(e) => onConfigChange({ autoSyncToStore: e.target.checked })}
                                        className="rounded text-accent-lineage"
                                    />
                                    <div>
                                        <span className="text-xs text-ink block">Auto-sync to canvas</span>
                                        <span className="text-2xs text-ink-muted">Add traced nodes to view</span>
                                    </div>
                                </label>
                            </div>

                            {/* Lineage Edge Type Filter */}
                            {availableLineageEdgeTypes.length > 0 && (
                                <div className="space-y-2">
                                    <div className="flex items-center justify-between">
                                        <label className="text-xs font-medium text-ink-muted flex items-center gap-1">
                                            <LucideIcons.Filter className="w-3 h-3 text-accent-lineage" />
                                            Lineage Edge Types
                                        </label>
                                        <button
                                            onClick={() => {
                                                const allSelected = config.lineageEdgeTypes.length === availableLineageEdgeTypes.length
                                                onConfigChange({
                                                    lineageEdgeTypes: allSelected ? [] : [...availableLineageEdgeTypes]
                                                })
                                            }}
                                            className="text-2xs text-accent-lineage hover:underline"
                                        >
                                            {config.lineageEdgeTypes.length === availableLineageEdgeTypes.length ? 'Clear all' : 'Select all'}
                                        </button>
                                    </div>
                                    <div className="flex flex-wrap gap-1.5">
                                        {availableLineageEdgeTypes.map(edgeType => {
                                            const isSelected = config.lineageEdgeTypes.length === 0 || config.lineageEdgeTypes.includes(edgeType)
                                            return (
                                                <button
                                                    key={edgeType}
                                                    onClick={() => {
                                                        let newTypes: string[]
                                                        if (config.lineageEdgeTypes.length === 0) {
                                                            // Currently "all" → deselect this one
                                                            newTypes = availableLineageEdgeTypes.filter(t => t !== edgeType)
                                                        } else if (isSelected) {
                                                            newTypes = config.lineageEdgeTypes.filter(t => t !== edgeType)
                                                        } else {
                                                            newTypes = [...config.lineageEdgeTypes, edgeType]
                                                        }
                                                        // If all selected, reset to empty (meaning "all")
                                                        if (newTypes.length === availableLineageEdgeTypes.length) {
                                                            newTypes = []
                                                        }
                                                        onConfigChange({ lineageEdgeTypes: newTypes })
                                                    }}
                                                    className={cn(
                                                        "px-2 py-1 rounded-md text-2xs font-medium transition-all border",
                                                        isSelected
                                                            ? "bg-accent-lineage/15 text-accent-lineage border-accent-lineage/30"
                                                            : "bg-black/5 dark:bg-white/5 text-ink-muted border-transparent hover:border-glass-border"
                                                    )}
                                                >
                                                    {edgeType}
                                                </button>
                                            )
                                        })}
                                    </div>
                                    <p className="text-2xs text-ink-muted">
                                        {config.lineageEdgeTypes.length === 0
                                            ? 'Tracing all lineage edge types'
                                            : `Tracing ${config.lineageEdgeTypes.length} of ${availableLineageEdgeTypes.length} types`}
                                    </p>
                                </div>
                            )}
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>

            {/* Copy Notification */}
            <AnimatePresence>
                {copiedMessage && (
                    <motion.div
                        initial={{ opacity: 0, y: 10 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: 10 }}
                        className="absolute -bottom-10 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-lg bg-green-500 text-white text-xs font-medium shadow-lg"
                    >
                        {copiedMessage}
                    </motion.div>
                )}
            </AnimatePresence>
        </motion.div>
    )
}

// ============================================
// Compact Variant
// ============================================

interface CompactTraceToolbarProps {
    focusNodeName?: string
    onExitTrace: () => void
    upstreamCount: number
    downstreamCount: number
    className?: string
}

export function CompactTraceToolbar({
    focusNodeName = 'Unknown',
    onExitTrace,
    upstreamCount,
    downstreamCount,
    className,
}: CompactTraceToolbarProps) {
    return (
        <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.9 }}
            className={cn(
                "inline-flex items-center gap-2 px-3 py-1.5 rounded-full glass-panel border border-accent-lineage/30 shadow-md",
                className
            )}
        >
            <motion.span
                className="w-1.5 h-1.5 rounded-full bg-accent-lineage"
                animate={{ opacity: [1, 0.5, 1] }}
                transition={{ duration: 1.5, repeat: Infinity }}
            />
            <span className="text-xs font-medium text-ink truncate max-w-[120px]">
                {focusNodeName}
            </span>
            <span className="text-2xs text-ink-muted">
                ↑{upstreamCount} ↓{downstreamCount}
            </span>
            <button
                onClick={onExitTrace}
                className="p-0.5 rounded hover:bg-black/10 dark:hover:bg-white/10 text-ink-muted"
            >
                <LucideIcons.X className="w-3 h-3" />
            </button>
        </motion.div>
    )
}

export default TraceToolbar

