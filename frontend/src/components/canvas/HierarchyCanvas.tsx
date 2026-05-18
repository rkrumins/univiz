/**
 * HierarchyCanvas - Hierarchy-style Reference Model View
 * 
 * Displays entities in a hierarchical nested container structure with:
 * - Collapsible containers that roll up children
 * - Left-to-right flow across hierarchy levels
 * - Search to find and expand to any node
 * - Roll-up counts (e.g., "15 tables, 247 columns")
 * - Progressive expansion from root to leaf
 */

import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import { fetchWithTimeout } from '@/services/fetchWithTimeout'
import { useSchemaStore, isContainmentEdgeType } from '@/store/schema'
import { useViewContainmentEdgeTypes, useViewLineageEdgeTypes, useViewRelationshipTypes } from '@/hooks/useViewSchema'
import { useCanvasStore } from '@/store/canvas'
import { useGraphHydration } from '@/hooks/useGraphHydration'
import { useLoadingToast } from '@/components/ui/toast'

// UX-first interaction components (shared across canvases)
import { CanvasContextMenu, type ContextMenuTarget } from './CanvasContextMenu'
import { InlineNodeEditor } from './InlineNodeEditor'
import { QuickCreateNode } from './QuickCreateNode'
import { CommandPalette } from './CommandPalette'
import { useCanvasInteractions } from '@/hooks/useCanvasInteractions'
import { useCanvasKeyboard } from '@/hooks/useCanvasKeyboard'

// Editor components (shared across canvases)
import { EditorToolbar } from './EditorToolbar'
import { NodePalette } from './NodePalette'
import { EntityDrawer } from '../panels/EntityDrawer'
import { TraceToolbar } from './TraceToolbar'
import { useCanvasTrace } from '@/hooks/useCanvasTrace'
import type { HierarchyNode } from '@/types/hierarchy'
import { useContainmentHierarchy } from '@/hooks/useContainmentHierarchy'

interface HierarchyCanvasProps {
  className?: string
}

// Recursive count of all descendants
function countDescendants(node: HierarchyNode): { total: number; byType: Record<string, number> } {
  const byType: Record<string, number> = {}
  let total = 0

  const traverse = (n: HierarchyNode) => {
    total++
    byType[n.typeId] = (byType[n.typeId] ?? 0) + 1
    n.children.forEach(traverse)
  }

  node.children.forEach(traverse)
  return { total, byType }
}

export function HierarchyCanvas({ className }: HierarchyCanvasProps) {
  // Use individual selectors to avoid re-rendering on unrelated store changes
  const nodes = useCanvasStore(s => s.nodes)
  const edges = useCanvasStore(s => s.edges)
  const selectNode = useCanvasStore(s => s.selectNode)
  const selectedNodeIds = useCanvasStore(s => s.selectedNodeIds)
  const selectedNodeId = selectedNodeIds[0] ?? null
  const schema = useSchemaStore((s) => s.schema)
  const containmentEdgeTypes = useViewContainmentEdgeTypes()
  const lineageEdgeTypes = useViewLineageEdgeTypes()
  const { loadChildren, cancelChildLoad, loadingNodes, isLoading: isLoadingChildren } = useGraphHydration()
  useLoadingToast('hier-children', isLoadingChildren, 'Expanding hierarchy')
  const relationshipTypes = useViewRelationshipTypes()
  // Search state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<string[]>([])
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())
  const searchInputRef = useRef<HTMLInputElement>(null)
  const pendingLoadRef = useRef<Set<string>>(new Set())

  // Edit Mode State (shared across canvases)
  const [isPaletteOpen, setPaletteOpen] = useState(false)
  const [activeEdgeType, setActiveEdgeType] = useState<string>('manual')

  // Build containment hierarchy using shared hook (must precede trace + traceContextSet)
  const isContainmentEdge = useCallback(
    (normalizedType: string) => isContainmentEdgeType(normalizedType, containmentEdgeTypes),
    [containmentEdgeTypes]
  )
  const { parentMap, childMap, rootNodes: hierarchyRoots, nodeMap } = useContainmentHierarchy({
    nodes, edges, isContainmentEdge,
  })

  // Unified Trace System (shared hook handles merge + auto-expand)
  const trace = useCanvasTrace({
    nodes, edges, isContainmentEdge, expandedNodes, setExpandedNodes,
  })

  // Build trace context set that includes ancestors of traced nodes
  // This ensures parent containers stay visible when children are in the trace
  const traceContextSet = useMemo(() => {
    const set = new Set<string>()

    if (!trace.isTracing) return set

    // Add all visible trace nodes
    trace.visibleTraceNodes.forEach(id => set.add(id))

    // Add all ancestors of traced nodes so containers stay visible
    trace.visibleTraceNodes.forEach(id => {
      let curr = parentMap.get(id)
      while (curr) {
        set.add(curr)
        curr = parentMap.get(curr)
      }
    })

    return set
  }, [trace.isTracing, trace.visibleTraceNodes, parentMap])

  // ESC-driven trace exit. Mirrors ContextViewCanvas: purges trace-merged
  // edges from the canvas store, clears trace state, and reverts ancestor-
  // chain auto-expansion. Without this, ESC fell through to selection-clear
  // and the trace dock stayed open.
  const exitTrace = useCallback(() => {
    if (!trace.isTracing) return false
    const idsToRemove = Array.from(trace.addedEdgeIds)
    trace.clearTrace()
    if (idsToRemove.length > 0) {
      useCanvasStore.getState().removeEdges(idsToRemove)
    }
    trace.resetAddedEdgeIds()
    setExpandedNodes(new Set())
    return true
  }, [trace])

  // UX-first Canvas Interactions (context menu, inline edit, quick create, command palette)
  const interactions = useCanvasInteractions({
    onTraceNode: (nodeId) => trace.startTrace(nodeId),
    onNodeCreated: (nodeId) => selectNode(nodeId),
    onExitTrace: exitTrace,
  })

  // Keyboard shortcuts
  useCanvasKeyboard({
    enabled: true,
    handlers: interactions.keyboardHandlers,
  })

  // Handle save graph
  const handleSave = useCallback(async () => {
    try {
      const response = await fetchWithTimeout('/api/v1/graph/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes, edges })
      })
      if (!response.ok) throw new Error('Failed to save graph')
      alert('Graph saved successfully!')
    } catch (error) {
      console.error('Error saving graph:', error)
      alert('Failed to save graph')
    }
  }, [nodes, edges])

  // Build hierarchy tree from the shared containment maps
  const hierarchyTree = useMemo(() => {
    if (!nodes.length) return []

    const buildTree = (nodeId: string, depth: number): HierarchyNode | null => {
      const node = nodeMap.get(nodeId)
      if (!node) return null

      const children = (childMap.get(nodeId) ?? [])
        .map((childId) => buildTree(childId, depth + 1))
        .filter((n): n is HierarchyNode => n !== null)
        .sort((a, b) => a.name.localeCompare(b.name))

      return {
        id: node.id,
        typeId: node.data.type as string,
        name: (node.data.label as string) ?? (node.data.businessLabel as string) ?? node.id,
        data: node.data as Record<string, unknown>,
        children,
        depth,
        urn: (node.data.urn as string) ?? node.id,
        entityTypeOption: (node.data.type as string) ?? '',
        tags: (node.data.classifications as string[]) ?? [],
      }
    }

    return hierarchyRoots
      .map((n) => buildTree(n.id, 0))
      .filter((n): n is HierarchyNode => n !== null)
      .sort((a, b) => a.name.localeCompare(b.name))
  }, [nodes.length, hierarchyRoots, childMap, nodeMap])

  // Flatten tree for search
  const flatNodes = useMemo(() => {
    const flat: HierarchyNode[] = []
    const traverse = (node: HierarchyNode, path: string[]) => {
      flat.push({ ...node, data: { ...node.data, _path: [...path, node.id] } })
      node.children.forEach((child) => traverse(child, [...path, node.id]))
    }
    hierarchyTree.forEach((root) => traverse(root, []))
    return flat
  }, [hierarchyTree])

  // Search functionality
  // Guard: only update searchResults when the value actually changes to prevent
  // infinite re-render loops (setSearchResults([]) creates a new reference each time).
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults(prev => prev.length === 0 ? prev : [])
      return
    }

    const query = searchQuery.toLowerCase()
    const results = flatNodes
      .filter((node) =>
        node.name.toLowerCase().includes(query) ||
        node.typeId.toLowerCase().includes(query) ||
        (node.data.urn as string)?.toLowerCase().includes(query)
      )
      .map((n) => n.id)

    setSearchResults(results)
  }, [searchQuery, flatNodes])

  // Expand path to search result
  const expandToNode = useCallback((nodeId: string) => {
    const node = flatNodes.find((n) => n.id === nodeId)
    if (!node) return

    const path = node.data._path as string[] | undefined
    if (path) {
      setExpandedNodes((prev) => {
        const next = new Set(prev)
        path.forEach((id) => next.add(id))
        return next
      })
    }

    // Select and scroll to node
    selectNode(nodeId)

    // Scroll to node after expansion, then pulse so the user sees where
    // they landed (matches the jump-to-node feedback in other canvases).
    setTimeout(() => {
      const element = document.getElementById(`hierarchy-node-${nodeId}`)
      element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      useCanvasStore.getState().pulseNode(nodeId)
    }, 100)
  }, [flatNodes, selectNode])

  // Toggle node expansion with lazy loading
  const toggleNode = useCallback(async (nodeId: string) => {
    // A child load for this node is already in flight. Ignore repeat clicks
    // until it settles: otherwise an impatient second click reads the node
    // as expanded, collapses it, and cancels the in-flight fetch — forcing a
    // third click to actually load. Collapse works once the load completes.
    if (pendingLoadRef.current.has(nodeId)) return

    // 1. Determine local expansion state
    const isCurrentlyExpanded = expandedNodes.has(nodeId)

    // Update state
    setExpandedNodes((prev) => {
      const next = new Set(prev)
      if (isCurrentlyExpanded) {
        next.delete(nodeId)
      } else {
        next.add(nodeId)
      }
      return next
    })


    // If we are collapsing, abort any pending/in-flight load for this node
    // so a slow response doesn't repopulate a collapsed subtree.
    if (isCurrentlyExpanded) {
      cancelChildLoad(nodeId)
      return
    }

    // 2. Load Children using Generic Hook
    pendingLoadRef.current.add(nodeId)
    try {
      await loadChildren(nodeId)
    } finally {
      pendingLoadRef.current.delete(nodeId)
    }
  }, [loadChildren, cancelChildLoad, expandedNodes])

  // Expand/collapse all
  const expandAll = useCallback(() => {
    const allIds = flatNodes.map((n) => n.id)
    setExpandedNodes(new Set(allIds))
  }, [flatNodes])

  const collapseAll = useCallback(() => {
    setExpandedNodes(new Set())
  }, [])

  // Keyboard shortcut for search
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'f') {
        e.preventDefault()
        searchInputRef.current?.focus()
      }
      if (e.key === 'Escape') {
        setSearchQuery('')
        searchInputRef.current?.blur()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  return (
    <div className={cn("h-full w-full flex flex-col overflow-hidden bg-canvas relative", className)}>
      {/* Editor Toolbar - Unified with LineageCanvas */}
      <div className="absolute top-4 left-4 z-30">
        <EditorToolbar
          onAddNode={() => setPaletteOpen(true)}
          onSave={handleSave}
          edgeTypes={relationshipTypes}
          activeEdgeType={activeEdgeType}
          onSelectEdgeType={setActiveEdgeType}
        />
      </div>

      {/* Node Palette - Drag and drop entity creation */}
      <AnimatePresence>
        {isPaletteOpen && (
          <NodePalette
            isOpen={isPaletteOpen}
            onClose={() => setPaletteOpen(false)}
          />
        )}
      </AnimatePresence>

      {/* Header */}
      <div className="flex-shrink-0 bg-canvas-elevated/95 backdrop-blur border-b border-glass-border px-6 py-3">
        <div className="flex items-center gap-4">
          <h2 className="text-lg font-display font-semibold text-ink">Physical Fabric</h2>
          <span className="px-2 py-1 rounded-md bg-accent-lineage/10 text-accent-lineage text-xs font-medium">
            Reference Model View
          </span>
          <div className="flex-1" />

          {/* Search */}
          <div className="relative">
            <LucideIcons.Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-muted" />
            <input
              ref={searchInputRef}
              type="text"
              placeholder="Search entities... (⌘F)"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="input pl-9 pr-8 py-1.5 w-64 text-sm"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-muted hover:text-ink"
              >
                <LucideIcons.X className="w-4 h-4" />
              </button>
            )}
          </div>

          {/* Expand/Collapse All */}
          <div className="flex items-center gap-1">
            <button
              onClick={expandAll}
              className="btn btn-ghost btn-sm"
              title="Expand All"
            >
              <LucideIcons.ChevronsDownUp className="w-4 h-4 rotate-180" />
            </button>
            <button
              onClick={collapseAll}
              className="btn btn-ghost btn-sm"
              title="Collapse All"
            >
              <LucideIcons.ChevronsDownUp className="w-4 h-4" />
            </button>
          </div>

          <div className="flex items-center gap-2 text-sm text-ink-muted">
            <span>Flow: Left → Right</span>
            <LucideIcons.ArrowRight className="w-4 h-4" />
          </div>
        </div>

        {/* Search Results */}
        {searchQuery && searchResults.length > 0 && (
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-ink-muted">
              {searchResults.length} result{searchResults.length !== 1 ? 's' : ''}:
            </span>
            {searchResults.slice(0, 5).map((id) => {
              const node = flatNodes.find((n) => n.id === id)
              return (
                <button
                  key={id}
                  onClick={() => expandToNode(id)}
                  className="px-2 py-1 rounded-md bg-accent-lineage/10 text-accent-lineage text-xs hover:bg-accent-lineage/20 transition-colors"
                >
                  {node?.name}
                </button>
              )
            })}
            {searchResults.length > 5 && (
              <span className="text-xs text-ink-muted">
                +{searchResults.length - 5} more
              </span>
            )}
          </div>
        )}
      </div>

      {/* Hierarchy Content */}
      <div className="flex-1 overflow-auto p-6 custom-scrollbar">
        {hierarchyTree.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="space-y-3">
            {hierarchyTree.map((rootNode) => (
              <HierarchyContainer
                key={rootNode.id}
                node={rootNode}
                schema={schema}
                selectedNodeId={selectedNodeId}
                expandedNodes={expandedNodes}
                searchResults={searchResults}
                onSelect={selectNode}
                onToggle={toggleNode}
                onContextMenu={(e, nodeId) => {
                  interactions.openContextMenu(e, {
                    type: 'node',
                    id: nodeId,
                    data: flatNodes.find(n => n.id === nodeId)?.data || {}
                  })
                }}
                onDoubleClick={(nodeId, e) => {
                  const node = flatNodes.find(n => n.id === nodeId)
                  const element = document.getElementById(`hierarchy-node-${nodeId}`)
                  if (element && node) {
                    const rect = element.getBoundingClientRect()
                    interactions.startInlineEdit(
                      nodeId,
                      node.name,
                      { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
                    )
                  }
                }}
                isTraceActive={trace.isTracing}
                traceContextSet={traceContextSet}
                traceFocusId={trace.focusId}
              />
            ))}
          </div>
        )}
      </div>

      {/* Trace Toolbar */}
      <AnimatePresence>
        {trace.isTracing && (
          <div className="absolute top-20 left-1/2 -translate-x-1/2 z-50">
            <TraceToolbar
              focusNodeName={flatNodes.find(n => n.id === trace.focusId)?.name || trace.focusId || 'Unknown'}
              upstreamCount={trace.upstreamCount}
              downstreamCount={trace.downstreamCount}
              showUpstream={trace.showUpstream}
              showDownstream={trace.showDownstream}
              onToggleUpstream={() => trace.setShowUpstream(!trace.showUpstream)}
              onToggleDownstream={() => trace.setShowDownstream(!trace.showDownstream)}
              onExitTrace={() => trace.clearTrace()}
              onRetrace={trace.retrace}
              onTraceUpstream={() => trace.focusId && trace.traceUpstream(trace.focusId)}
              onTraceDownstream={() => trace.focusId && trace.traceDownstream(trace.focusId)}
              onTraceFullLineage={() => trace.focusId && trace.traceFullLineage(trace.focusId)}
              config={trace.config}
              onConfigChange={trace.setConfig}
              traceResult={trace.result}
              statistics={trace.statistics}
              isLoading={trace.isLoading}
              availableLineageEdgeTypes={lineageEdgeTypes}
              position="top"
            />
          </div>
        )}
      </AnimatePresence>

      {/* Entity Drawer - Unified view & edit */}
      <EntityDrawer
        onTraceUp={(nodeId) => trace.traceUpstream(nodeId)}
        onTraceDown={(nodeId) => trace.traceDownstream(nodeId)}
        onFullTrace={(nodeId) => trace.traceFullLineage(nodeId)}
        onFocusNode={expandToNode}
      />

      {/* === UX-FIRST INTERACTION COMPONENTS (Unified with LineageCanvas) === */}

      {/* Context Menu - Right-click on nodes */}
      <CanvasContextMenu
        isOpen={interactions.state.contextMenu.isOpen}
        position={interactions.state.contextMenu.position}
        target={interactions.state.contextMenu.target}
        onClose={interactions.closeContextMenu}
        onEditNode={interactions.editNode}
        onDuplicateNode={interactions.duplicateNode}
        onDeleteNode={interactions.deleteNode}
        onCreateChild={interactions.createChild}
        onTraceNode={(id) => trace.startTrace(id)}
        onCopyUrn={interactions.copyUrn}
        onEditEdge={interactions.editEdge}
        onDeleteEdge={interactions.deleteEdge}
        onReverseEdge={interactions.reverseEdge}
        onCreateNode={(pos) => interactions.openQuickCreate(pos)}
        onSelectAll={interactions.selectAll}
      />

      {/* Inline Node Editor - Double-click to edit names */}
      <InlineNodeEditor
        nodeId={interactions.state.inlineEdit.nodeId}
        value={interactions.state.inlineEdit.value}
        position={interactions.state.inlineEdit.position}
        onSave={interactions.saveInlineEdit}
        onCancel={interactions.cancelInlineEdit}
      />

      {/* Quick Create - Press 'N' or use context menu */}
      <QuickCreateNode
        isOpen={interactions.state.quickCreate.isOpen}
        position={interactions.state.quickCreate.position}
        parentUrn={interactions.state.quickCreate.parentUrn}
        onClose={interactions.closeQuickCreate}
        onCreated={(nodeId) => selectNode(nodeId)}
        variant="centered"
      />

      {/* Command Palette - Press Cmd+K */}
      <CommandPalette
        isOpen={interactions.state.commandPalette.isOpen}
        onClose={interactions.closeCommandPalette}
        onCreateEntity={(typeId) => {
          interactions.closeCommandPalette()
          interactions.openQuickCreate({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
        }}
        onSelectEntity={(entityId) => selectNode(entityId)}
      />
    </div>
  )
}

interface HierarchyContainerProps {
  node: HierarchyNode
  schema: ReturnType<typeof useSchemaStore.getState>['schema']
  selectedNodeId: string | null
  expandedNodes: Set<string>
  searchResults: string[]
  onSelect: (id: string) => void
  onToggle: (id: string) => void
  onContextMenu?: (e: React.MouseEvent, nodeId: string) => void
  onDoubleClick?: (nodeId: string, event: React.MouseEvent) => void
  depth?: number
  // Trace highlighting props
  isTraceActive?: boolean
  traceContextSet?: Set<string>
  traceFocusId?: string | null
}

function HierarchyContainer({
  node,
  schema,
  selectedNodeId,
  expandedNodes,
  searchResults,
  onSelect,
  onToggle,
  onContextMenu,
  onDoubleClick,
  depth = 0,
  isTraceActive = false,
  traceContextSet = new Set(),
  traceFocusId = null,
}: HierarchyContainerProps) {
  const entityType = schema?.entityTypes.find((et) => et.id === node.typeId)
  const visual = entityType?.visual
  const childCount = (node.data.childCount as number) || 0
  const hasChildren = node.children.length > 0 || childCount > 0
  const isExpanded = expandedNodes.has(node.id)
  const isSelected = selectedNodeId === node.id
  const isSearchResult = searchResults.includes(node.id)

  // Trace highlighting
  const isHighlighted = isTraceActive && traceContextSet.has(node.id)
  const isFocusNode = traceFocusId === node.id
  const isDimmed = isTraceActive && !isHighlighted

  // Calculate roll-up counts
  const rollUpCounts = useMemo(() => {
    if (hasChildren && !isExpanded) {
      return countDescendants(node)
    }
    return null
  }, [node, hasChildren, isExpanded])

  // Format roll-up display
  const rollUpDisplay = useMemo(() => {
    if (!rollUpCounts || !schema) return null

    const parts: string[] = []
    Object.entries(rollUpCounts.byType).forEach(([typeId, count]) => {
      const type = schema.entityTypes.find((et) => et.id === typeId)
      if (type) {
        parts.push(`${count} ${count === 1 ? type.name.toLowerCase() : type.pluralName.toLowerCase()}`)
      }
    })

    return parts.join(', ')
  }, [rollUpCounts, schema])

  // Indentation based on depth
  const indent = depth * 24

  return (
    <div
      id={`hierarchy-node-${node.id}`}
      style={{ marginLeft: indent }}
      className="relative"
    >
      {/* Connector line */}
      {depth > 0 && (
        <div
          className="absolute left-0 top-0 bottom-0 w-px bg-glass-border"
          style={{ left: -12 }}
        />
      )}

      {/* Node Container */}
      <motion.div
        layout
        className={cn(
          "relative rounded-xl border-2 overflow-hidden transition-all duration-200",
          "bg-canvas-elevated",
          isSelected && !isFocusNode && "ring-2 ring-offset-2",
          isSearchResult && !isSelected && "ring-2 ring-amber-400/50 ring-offset-1",
          // Trace styling - consistent across all canvases
          isFocusNode && "ring-4 ring-amber-400 ring-offset-2 shadow-[0_0_30px_rgba(251,191,36,0.5)] scale-[1.02] z-50",
          isHighlighted && !isFocusNode && "ring-2 ring-purple-400 ring-offset-1 shadow-[0_0_15px_rgba(192,132,252,0.3)]",
          isDimmed && "opacity-30 grayscale-[0.6] blur-[0.3px] scale-[0.98]",
        )}
        style={{
          borderColor: isFocusNode ? '#fbbf24' : isHighlighted ? '#c084fc' : visual?.color ?? '#6b7280',
          borderLeftWidth: '4px',
          ['--tw-ring-color' as string]: isFocusNode ? '#fbbf24' : isHighlighted ? '#c084fc' : visual?.color ?? '#6b7280',
        }}
      >
        {/* Header (always visible) */}
        <div
          className={cn(
            "flex items-center gap-3 px-4 py-3 cursor-pointer",
            "hover:bg-black/5 dark:hover:bg-white/5 transition-colors"
          )}
          onClick={() => onSelect(node.id)}
          onDoubleClick={(e) => onDoubleClick?.(node.id, e)}
          onContextMenu={(e) => {
            e.preventDefault()
            onContextMenu?.(e, node.id)
          }}
        >
          {/* Expand/Collapse Button */}
          {hasChildren && (
            <button
              onClick={(e) => {
                e.stopPropagation()
                onToggle(node.id)
              }}
              className={cn(
                "w-6 h-6 rounded-md flex items-center justify-center",
                "hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
              )}
              style={{ color: visual?.color ?? '#6b7280' }}
            >
              <motion.div
                animate={{ rotate: isExpanded ? 90 : 0 }}
                transition={{ duration: 0.15 }}
              >
                <LucideIcons.ChevronRight className="w-4 h-4" />
              </motion.div>
            </button>
          )}

          {/* Spacer if no children */}
          {!hasChildren && <div className="w-6" />}


          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span
                className="text-2xs font-medium uppercase tracking-wider"
                style={{ color: visual?.color ?? '#6b7280' }}
              >
                {entityType?.name ?? node.typeId}
              </span>
              {hasChildren && (
                <span className="px-1.5 py-0.5 rounded text-2xs font-medium bg-black/5 dark:bg-white/10 text-ink-muted">
                  {node.children.length}
                </span>
              )}
            </div>
            <h4 className="text-sm font-medium text-ink truncate">
              {node.name}
            </h4>

            {/* Roll-up counts when collapsed */}
            {rollUpDisplay && !isExpanded && (
              <p className="text-2xs text-ink-muted mt-0.5">
                Contains {rollUpDisplay}
              </p>
            )}
          </div>

          {/* Tags */}
          {node.data.classifications && Array.isArray(node.data.classifications) && (
            <div className="flex items-center gap-1">
              {(node.data.classifications as string[]).filter(Boolean).slice(0, 2).map((tag, idx) => (
                <span
                  key={`${tag}-${idx}`}
                  className="px-1.5 py-0.5 rounded text-2xs font-medium"
                  style={{ backgroundColor: `${visual?.color ?? '#6b7280'}15`, color: visual?.color ?? '#6b7280' }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}

          {/* Expand indicator */}
          {hasChildren && !isExpanded && (
            <div className="flex items-center gap-1 text-ink-muted">
              <LucideIcons.MoreHorizontal className="w-4 h-4" />
            </div>
          )}
        </div>

        {/* Children (collapsible) */}
        <AnimatePresence>
          {hasChildren && isExpanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-4 pb-4 pt-1 space-y-2 border-t border-glass-border/50">
                {node.children.map((child) => (
                  <HierarchyContainer
                    key={child.id}
                    node={child}
                    schema={schema}
                    selectedNodeId={selectedNodeId}
                    expandedNodes={expandedNodes}
                    searchResults={searchResults}
                    onSelect={onSelect}
                    onToggle={onToggle}
                    onContextMenu={onContextMenu}
                    onDoubleClick={onDoubleClick}
                    depth={depth + 1}
                    isTraceActive={isTraceActive}
                    traceContextSet={traceContextSet}
                    traceFocusId={traceFocusId}
                  />
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <div className="w-16 h-16 rounded-2xl bg-black/5 dark:bg-white/5 flex items-center justify-center mb-4">
        <LucideIcons.FolderTree className="w-8 h-8 text-ink-muted" />
      </div>
      <h3 className="text-lg font-medium text-ink">No Hierarchy Data</h3>
      <p className="text-sm text-ink-muted mt-1 max-w-sm">
        This view requires entities with containment relationships.
        Switch to a different view or add parent-child relationships.
      </p>
    </div>
  )
}
