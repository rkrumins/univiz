/**
 * CanvasContextMenu - Modern context menu for graph CRUD operations
 * 
 * Provides a unified right-click experience across all canvas views with:
 * - Node operations: Edit, Duplicate, Delete, Create Child
 * - Edge operations: Edit, Delete, Reverse
 * - Canvas operations: Create Node, Paste, Select All
 * - Visual feedback and keyboard shortcut hints
 */

import React, { useCallback, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'

// ============================================
// Types
// ============================================

export type ContextMenuTarget =
    | { type: 'node'; id: string; data?: Record<string, unknown> }
    | { type: 'edge'; id: string; source: string; target: string }
    | { type: 'canvas'; position: { x: number; y: number } }

export interface ContextMenuAction {
    id: string
    label: string
    icon: keyof typeof LucideIcons
    shortcut?: string
    disabled?: boolean
    danger?: boolean
    dividerAfter?: boolean
    onClick: () => void
}

export interface CanvasContextMenuProps {
    /** Whether the menu is open */
    isOpen: boolean
    /** Position of the menu */
    position: { x: number; y: number }
    /** What was right-clicked */
    target: ContextMenuTarget | null
    /** Close handler */
    onClose: () => void
    /** Node actions */
    onEditNode?: (id: string) => void
    onDuplicateNode?: (id: string) => void
    onDeleteNode?: (id: string) => void
    onCreateChild?: (parentId: string) => void
    onTraceNode?: (id: string) => void
    /** Pin/unpin a node as a trace-path endpoint (Pin Lineage). */
    onPinTarget?: (id: string) => void
    /** URNs currently pinned — drives the Pin/Unpin label. */
    pinnedTargetIds?: string[]
    /** Whether a trace is active (Pin Lineage only applies during a trace). */
    traceActive?: boolean
    onCopyUrn?: (id: string) => void
    /** Edge actions */
    onEditEdge?: (id: string) => void
    onDeleteEdge?: (id: string) => void
    onReverseEdge?: (id: string) => void
    /** Canvas actions */
    onCreateNode?: (position: { x: number; y: number }) => void
    onPaste?: (position: { x: number; y: number }) => void
    onSelectAll?: () => void
    /** Additional custom actions */
    customActions?: ContextMenuAction[]
    /** Layer options for moving nodes */
    layers?: Array<{ id: string; name: string; color: string }>
    onMoveToLayer?: (nodeId: string, layerId: string) => void
}

// ============================================
// Component
// ============================================

export function CanvasContextMenu({
    isOpen,
    position,
    target,
    onClose,
    onEditNode,
    onDuplicateNode,
    onDeleteNode,
    onCreateChild,
    onTraceNode,
    onPinTarget,
    pinnedTargetIds = [],
    traceActive = false,
    onCopyUrn,
    onEditEdge,
    onDeleteEdge,
    onReverseEdge,
    onCreateNode,
    onPaste,
    onSelectAll,
    customActions = [],
    layers = [],
    onMoveToLayer,
}: CanvasContextMenuProps) {
    const menuRef = useRef<HTMLDivElement>(null)
    const [showLayerSubmenu, setShowLayerSubmenu] = React.useState(false)

    // Close on click outside
    useEffect(() => {
        if (!isOpen) return

        const handleClickOutside = (e: MouseEvent) => {
            if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
                onClose()
            }
        }

        const handleEscape = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                onClose()
            }
        }

        // Delay to prevent immediate close from the same click
        setTimeout(() => {
            document.addEventListener('mousedown', handleClickOutside)
            document.addEventListener('keydown', handleEscape)
        }, 0)

        return () => {
            document.removeEventListener('mousedown', handleClickOutside)
            document.removeEventListener('keydown', handleEscape)
        }
    }, [isOpen, onClose])

    // Reset submenu when menu closes
    useEffect(() => {
        if (!isOpen) setShowLayerSubmenu(false)
    }, [isOpen])

    // Build actions based on target type
    const actions = useCallback((): ContextMenuAction[] => {
        if (!target) return []

        const result: ContextMenuAction[] = []

        if (target.type === 'node') {
            // Node actions
            if (onEditNode) {
                result.push({
                    id: 'edit',
                    label: 'View & Edit',
                    icon: 'PanelRight',
                    shortcut: 'Enter',
                    onClick: () => { onEditNode(target.id); onClose() }
                })
            }

            if (onTraceNode) {
                result.push({
                    id: 'trace',
                    label: 'Trace Lineage',
                    icon: 'GitBranch',
                    shortcut: 'T',
                    onClick: () => { onTraceNode(target.id); onClose() }
                })
            }

            if (traceActive && onPinTarget) {
                const pinned = pinnedTargetIds.includes(target.id)
                result.push({
                    id: 'pin-trace-path',
                    label: pinned ? 'Unpin from Trace Path' : 'Pin to Trace Path',
                    icon: 'Pin',
                    onClick: () => { onPinTarget(target.id); onClose() }
                })
            }

            if (onCopyUrn) {
                result.push({
                    id: 'copy-urn',
                    label: 'Copy URN',
                    icon: 'Copy',
                    shortcut: '⌘C',
                    dividerAfter: true,
                    onClick: () => { onCopyUrn(target.id); onClose() }
                })
            }

            if (onCreateChild) {
                result.push({
                    id: 'create-child',
                    label: 'Add Child Entity',
                    icon: 'Plus',
                    onClick: () => { onCreateChild(target.id); onClose() }
                })
            }

            if (onDuplicateNode) {
                result.push({
                    id: 'duplicate',
                    label: 'Duplicate',
                    icon: 'Copy',
                    shortcut: '⌘D',
                    onClick: () => { onDuplicateNode(target.id); onClose() }
                })
            }

            if (layers.length > 0 && onMoveToLayer) {
                result.push({
                    id: 'move-to-layer',
                    label: 'Move to Layer',
                    icon: 'Layers',
                    dividerAfter: true,
                    onClick: () => setShowLayerSubmenu(true)
                })
            }

            if (onDeleteNode) {
                result.push({
                    id: 'delete',
                    label: 'Delete',
                    icon: 'Trash2',
                    shortcut: '⌫',
                    danger: true,
                    onClick: () => { onDeleteNode(target.id); onClose() }
                })
            }
        } else if (target.type === 'edge') {
            // Edge actions
            if (onEditEdge) {
                result.push({
                    id: 'edit-edge',
                    label: 'Edit Edge',
                    icon: 'Pencil',
                    onClick: () => { onEditEdge(target.id); onClose() }
                })
            }

            if (onReverseEdge) {
                result.push({
                    id: 'reverse-edge',
                    label: 'Reverse Direction',
                    icon: 'ArrowLeftRight',
                    onClick: () => { onReverseEdge(target.id); onClose() }
                })
            }

            if (onDeleteEdge) {
                result.push({
                    id: 'delete-edge',
                    label: 'Delete Edge',
                    icon: 'Trash2',
                    shortcut: '⌫',
                    danger: true,
                    onClick: () => { onDeleteEdge(target.id); onClose() }
                })
            }
        } else if (target.type === 'canvas') {
            // Canvas actions
            if (onCreateNode) {
                result.push({
                    id: 'create-node',
                    label: 'Create Entity Here',
                    icon: 'Plus',
                    shortcut: 'N',
                    onClick: () => { onCreateNode(target.position); onClose() }
                })
            }

            if (onPaste) {
                result.push({
                    id: 'paste',
                    label: 'Paste',
                    icon: 'Clipboard',
                    shortcut: '⌘V',
                    dividerAfter: true,
                    onClick: () => { onPaste(target.position); onClose() }
                })
            }

            if (onSelectAll) {
                result.push({
                    id: 'select-all',
                    label: 'Select All',
                    icon: 'CheckSquare',
                    shortcut: '⌘A',
                    onClick: () => { onSelectAll(); onClose() }
                })
            }
        }

        // Add custom actions
        result.push(...customActions)

        return result
    }, [target, onEditNode, onDuplicateNode, onDeleteNode, onCreateChild, onTraceNode,
        onPinTarget, pinnedTargetIds, traceActive,
        onCopyUrn, onEditEdge, onDeleteEdge, onReverseEdge, onCreateNode, onPaste,
        onSelectAll, customActions, layers, onMoveToLayer, onClose])

    // Get icon component
    const getIcon = (iconName: keyof typeof LucideIcons) => {
        const IconComponent = LucideIcons[iconName] as React.ComponentType<{ className?: string }>
        return IconComponent ? <IconComponent className="w-4 h-4" /> : null
    }

    // Adjust position to keep menu in viewport
    const adjustedPosition = {
        x: Math.min(position.x, window.innerWidth - 220),
        y: Math.min(position.y, window.innerHeight - 300)
    }

    return (
        <AnimatePresence>
            {isOpen && target && (
                <motion.div
                    ref={menuRef}
                    initial={{ opacity: 0, scale: 0.95, y: -5 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95, y: -5 }}
                    transition={{ duration: 0.1 }}
                    className={cn(
                        "fixed z-[100] min-w-[200px] py-1.5",
                        "bg-canvas-elevated/98 backdrop-blur-xl",
                        "border border-glass-border rounded-xl shadow-lg",
                        "overflow-hidden"
                    )}
                    style={{
                        left: adjustedPosition.x,
                        top: adjustedPosition.y,
                    }}
                >
                    {/* Menu Header */}
                    <div className="px-3 py-1.5 border-b border-glass-border bg-black/5 dark:bg-white/5">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-ink-muted">
                            {target.type === 'node' ? 'Node Actions' :
                                target.type === 'edge' ? 'Edge Actions' : 'Canvas Actions'}
                        </span>
                    </div>

                    {/* Layer Submenu */}
                    {showLayerSubmenu && target.type === 'node' ? (
                        <div className="py-1">
                            <button
                                onClick={() => setShowLayerSubmenu(false)}
                                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-ink-muted hover:bg-black/5 dark:hover:bg-white/5"
                            >
                                <LucideIcons.ChevronLeft className="w-3 h-3" />
                                Back
                            </button>
                            <div className="border-t border-glass-border my-1" />
                            {layers.map(layer => (
                                <button
                                    key={layer.id}
                                    onClick={() => {
                                        onMoveToLayer?.(target.id, layer.id)
                                        onClose()
                                    }}
                                    className="w-full flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-accent-lineage/10 hover:text-accent-lineage transition-colors"
                                >
                                    <div
                                        className="w-3 h-3 rounded-full border border-white/20"
                                        style={{ backgroundColor: layer.color }}
                                    />
                                    {layer.name}
                                </button>
                            ))}
                        </div>
                    ) : (
                        <div className="py-1">
                            {actions().map((action, index) => (
                                <React.Fragment key={action.id}>
                                    <button
                                        onClick={action.onClick}
                                        disabled={action.disabled}
                                        className={cn(
                                            "w-full flex items-center justify-between gap-3 px-3 py-2 text-sm transition-colors",
                                            action.disabled
                                                ? "text-ink-muted cursor-not-allowed opacity-50"
                                                : action.danger
                                                    ? "text-red-500 hover:bg-red-500/10"
                                                    : "text-ink hover:bg-accent-lineage/10 hover:text-accent-lineage"
                                        )}
                                    >
                                        <span className="flex items-center gap-2.5">
                                            {getIcon(action.icon)}
                                            {action.label}
                                        </span>
                                        {action.shortcut && (
                                            <span className="text-[10px] text-ink-muted bg-black/5 dark:bg-white/10 px-1.5 py-0.5 rounded font-mono">
                                                {action.shortcut}
                                            </span>
                                        )}
                                    </button>
                                    {action.dividerAfter && index < actions().length - 1 && (
                                        <div className="border-t border-glass-border my-1" />
                                    )}
                                </React.Fragment>
                            ))}
                        </div>
                    )}
                </motion.div>
            )}
        </AnimatePresence>
    )
}

export default CanvasContextMenu

