/**
 * ContextViewHeader - Toolbar, search, and authoring controls for Context View.
 *
 * Receives all state as props from ContextViewCanvas — no store access here.
 * Keeps the orchestrator lean and makes the header independently testable.
 *
 * The header is INTENTIONALLY trace-agnostic. Trace UI lives in the
 * `TraceBottomDock` mounted inside ContextViewCanvas's canvas-body, in a
 * separate layout slot from the right-rail EntityDrawer. The header here
 * is purely about authoring + display-mode controls.
 */

import { useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import type { HierarchyNode } from './types'

/** Minimal entity type shape needed for the granularity selector. */
export interface GranularityOption {
  id: string
  name: string
  level: number
}

export interface ContextViewHeaderProps {
  // Search
  searchQuery: string
  onSearchChange: (q: string) => void
  searchResults: HierarchyNode[]
  onSearchResultClick: (node: HierarchyNode) => void

  // Lineage flow
  showLineageFlow: boolean
  onToggleLineageFlow: () => void
  /** Current granularity: entity type ID, or null for no aggregation. */
  lineageGranularity: string | null
  onGranularityChange: (g: string | null) => void
  /** Entity types from the active ontology, used to populate the granularity picker. */
  granularityOptions: GranularityOption[]

  // Edge direction toggle
  showEdgeDirection: boolean
  onToggleEdgeDirection: () => void

  // Add entity
  onAddEntity: () => void

  // Blueprint
  activeWorkspaceId: string | null
  activeContextModelName: string | null
  syncStatus: 'idle' | 'dirty' | 'saving' | 'synced' | 'error'
  onSave: () => void
  /** Number of staged changes pending review/save — drives the badge + label. */
  pendingChangeCount?: number
  /** Optional click handler for the staged-changes badge (opens review panel). */
  onOpenStagedChanges?: () => void

  // Undo / Redo
  canUndo?: boolean
  canRedo?: boolean
  onUndo?: () => void
  onRedo?: () => void
}

export function ContextViewHeader({
  searchQuery,
  onSearchChange,
  searchResults,
  onSearchResultClick,
  showLineageFlow,
  onToggleLineageFlow,
  lineageGranularity,
  onGranularityChange,
  granularityOptions,
  showEdgeDirection,
  onToggleEdgeDirection,
  onAddEntity,
  activeWorkspaceId,
  activeContextModelName,
  syncStatus,
  onSave,
  pendingChangeCount = 0,
  onOpenStagedChanges,
  canUndo = false,
  canRedo = false,
  onUndo,
  onRedo,
}: ContextViewHeaderProps) {
  const searchInputRef = useRef<HTMLInputElement>(null)

  return (
    <div className="flex-shrink-0 bg-gradient-to-r from-canvas-elevated/90 via-canvas-elevated/95 to-canvas-elevated/90 backdrop-blur-xl border-b border-white/[0.06] px-6 py-3 relative">
      {/* Subtle gradient overlay */}
      <div className="absolute inset-0 bg-gradient-to-r from-accent-lineage/[0.02] via-transparent to-purple-500/[0.02] pointer-events-none" />

      <div className="flex items-center gap-4 relative">
        {/* Title */}
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-accent-lineage/20 to-purple-500/20 flex items-center justify-center shadow-lg shadow-accent-lineage/10">
            <LucideIcons.Network className="w-5 h-5 text-accent-lineage" />
          </div>
          <div>
            <h2 className="text-base font-display font-semibold text-ink tracking-tight">Context View</h2>
            <p className="text-[10px] text-ink-muted/60 flex items-center gap-1.5">
              <LucideIcons.ArrowRight className="w-3 h-3" />
              Data Flow Blueprint
            </p>
          </div>
        </div>

        <div className="flex-1" />

        {/* Search */}
        <div className="relative group">
          <div className="absolute inset-0 bg-gradient-to-r from-accent-lineage/10 to-purple-500/10 rounded-xl opacity-0 group-focus-within:opacity-100 blur-xl transition-opacity" />
          <div className="relative">
            <LucideIcons.Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-muted/50 group-focus-within:text-accent-lineage transition-colors" />
            <input
              ref={searchInputRef}
              type="text"
              placeholder="Search entities..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="w-52 pl-9 pr-8 py-2 rounded-xl bg-white/[0.04] border border-white/[0.08] text-sm text-ink placeholder:text-ink-muted/40 focus:outline-none focus:border-accent-lineage/40 focus:bg-white/[0.06] transition-all"
            />
            {searchQuery && (
              <button
                onClick={() => onSearchChange('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-lg hover:bg-white/10 text-ink-muted hover:text-ink transition-all"
              >
                <LucideIcons.X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        <div className="w-px h-6 bg-gradient-to-b from-transparent via-white/10 to-transparent" />

        {/* Lineage Flow Toggle */}
        <button
          onClick={onToggleLineageFlow}
          className={cn(
            "flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300",
            showLineageFlow
              ? "bg-gradient-to-r from-accent-lineage/20 to-accent-lineage/10 text-accent-lineage shadow-lg shadow-accent-lineage/20 border border-accent-lineage/30"
              : "bg-white/[0.04] border border-white/[0.08] text-ink-muted hover:bg-white/[0.08] hover:text-ink"
          )}
        >
          <motion.div animate={{ rotate: showLineageFlow ? 0 : -180 }} transition={{ duration: 0.3 }}>
            <LucideIcons.GitBranch className="w-4 h-4" />
          </motion.div>
          <span>{showLineageFlow ? 'Flow Active' : 'Show Flow'}</span>
          <div className={cn(
            "w-2 h-2 rounded-full transition-colors duration-300",
            showLineageFlow ? "bg-green-400 shadow-lg shadow-green-400/50" : "bg-ink-muted/30"
          )} />
        </button>

        {/* Granularity Selector */}
        <AnimatePresence>
          {showLineageFlow && (
            <motion.div
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: 'auto' }}
              exit={{ opacity: 0, width: 0 }}
              className="overflow-hidden"
            >
              <select
                value={lineageGranularity ?? ''}
                onChange={(e) => onGranularityChange(e.target.value || null)}
                className="px-3 py-2 rounded-xl text-xs font-medium bg-white/[0.04] border border-white/[0.08] text-ink cursor-pointer hover:bg-white/[0.08] focus:outline-none focus:border-accent-lineage/40 transition-all appearance-none pr-8 bg-no-repeat bg-right"
                style={{ backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%239ca3af'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'%3E%3C/path%3E%3C/svg%3E")`, backgroundSize: '16px', backgroundPosition: 'right 8px center' }}
              >
                {[...granularityOptions]
                  .sort((a, b) => a.level - b.level)
                  .map(opt => (
                    <option key={opt.id} value={opt.id}>{opt.name} level</option>
                  ))
                }
              </select>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Show Direction toggle — controls arrowheads + animated mid-edge chevron */}
        <button
          onClick={onToggleEdgeDirection}
          title={showEdgeDirection ? 'Hide edge direction' : 'Show edge direction'}
          className={cn(
            "flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium transition-all duration-300",
            showEdgeDirection
              ? "bg-gradient-to-r from-cyan-500/20 to-cyan-500/10 text-cyan-300 border border-cyan-400/30 shadow-lg shadow-cyan-400/10"
              : "bg-white/[0.04] border border-white/[0.08] text-ink-muted hover:bg-white/[0.08] hover:text-ink"
          )}
        >
          <LucideIcons.MoveRight className="w-3.5 h-3.5" />
          <span>{showEdgeDirection ? 'Direction On' : 'Direction Off'}</span>
        </button>

        <div className="w-px h-6 bg-gradient-to-b from-transparent via-white/10 to-transparent" />

        {/* Add Entity */}
        <button
          onClick={onAddEntity}
          className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium bg-gradient-to-r from-green-500/20 to-emerald-500/10 text-green-400 border border-green-500/30 hover:from-green-500/30 hover:to-emerald-500/20 hover:shadow-lg hover:shadow-green-500/20 transition-all duration-300 hover:scale-[1.02] active:scale-[0.98]"
        >
          <LucideIcons.Plus className="w-4 h-4" />
          <span>Add Entity</span>
        </button>

        <div className="w-px h-6 bg-gradient-to-b from-transparent via-white/10 to-transparent" />

        {/* Blueprint indicator + Save */}
        <div className="flex items-center gap-2">
          {activeContextModelName && (
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-purple-500/[0.08] border border-purple-500/20">
              <LucideIcons.BookMarked className="w-3.5 h-3.5 text-purple-400 flex-shrink-0" />
              <span className="text-xs font-medium text-purple-300 truncate max-w-[140px]" title={activeContextModelName}>
                {activeContextModelName}
              </span>
            </div>
          )}
          {(canUndo || canRedo) && (
            <div className="flex items-stretch rounded-xl overflow-hidden bg-gradient-to-b from-white/[0.06] to-white/[0.02] border border-white/[0.08] shadow-sm shadow-black/20">
              <button
                onClick={onUndo}
                disabled={!canUndo}
                title="Undo last change (⌘Z)"
                aria-label="Undo"
                className={cn(
                  "flex items-center gap-1.5 px-3 py-2 text-[11.5px] font-semibold tracking-tight transition-all",
                  canUndo
                    ? "text-ink/85 hover:bg-white/[0.06] hover:text-ink active:bg-white/[0.10]"
                    : "text-ink-muted/25 cursor-not-allowed"
                )}
              >
                <LucideIcons.Undo2 className="w-3.5 h-3.5" strokeWidth={2.4} />
                <span>Undo</span>
              </button>
              <div className="w-px bg-white/[0.08]" />
              <button
                onClick={onRedo}
                disabled={!canRedo}
                title="Redo (⌘⇧Z)"
                aria-label="Redo"
                className={cn(
                  "flex items-center gap-1.5 px-3 py-2 text-[11.5px] font-semibold tracking-tight transition-all",
                  canRedo
                    ? "text-ink/85 hover:bg-white/[0.06] hover:text-ink active:bg-white/[0.10]"
                    : "text-ink-muted/25 cursor-not-allowed"
                )}
              >
                <span>Redo</span>
                <LucideIcons.Redo2 className="w-3.5 h-3.5" strokeWidth={2.4} />
              </button>
            </div>
          )}

          {pendingChangeCount > 0 && onOpenStagedChanges && (
            <button
              onClick={onOpenStagedChanges}
              title="Review pending changes"
              className="relative flex items-center gap-2 pl-2.5 pr-3 py-2 rounded-xl bg-gradient-to-br from-amber-300/25 via-amber-400/20 to-orange-500/15 border border-amber-300/50 text-amber-100 hover:from-amber-300/35 hover:to-orange-500/25 hover:border-amber-200/70 transition-all shadow-md shadow-amber-500/15 hover:shadow-lg hover:shadow-amber-500/25 hover:scale-[1.02] active:scale-[0.98]"
            >
              <span className="absolute -top-1 -right-1 flex h-3 w-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-300 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-300 ring-2 ring-canvas-elevated" />
              </span>
              <span className="flex items-center justify-center w-6 h-6 rounded-lg bg-amber-300/25 border border-amber-200/40">
                <LucideIcons.ListChecks className="w-3.5 h-3.5 text-amber-100" strokeWidth={2.4} />
              </span>
              <span className="text-[12px] font-bold tabular-nums leading-none">{pendingChangeCount}</span>
              <span className="text-[10.5px] uppercase tracking-[0.08em] font-bold leading-none">Pending</span>
            </button>
          )}
          <button
            onClick={onSave}
            disabled={(syncStatus !== 'dirty' && syncStatus !== 'error' && pendingChangeCount === 0) || !activeWorkspaceId}
            className={cn(
              "flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300",
              (syncStatus === 'dirty' || pendingChangeCount > 0)
                ? "bg-gradient-to-r from-blue-500/20 to-cyan-500/10 text-blue-400 border border-blue-500/30 hover:from-blue-500/30 hover:to-cyan-500/20 hover:shadow-lg hover:shadow-blue-500/20 hover:scale-[1.02] active:scale-[0.98]"
                : syncStatus === 'error'
                  ? "bg-gradient-to-r from-red-500/20 to-red-500/10 text-red-400 border border-red-500/30"
                  : "bg-white/[0.03] border border-white/[0.06] text-ink-muted/50 cursor-not-allowed"
            )}
            title={
              !activeWorkspaceId ? 'No workspace selected'
                : pendingChangeCount > 0 ? `Apply ${pendingChangeCount} pending change${pendingChangeCount === 1 ? '' : 's'} and save`
                : syncStatus === 'dirty' ? 'Save changes to backend'
                  : syncStatus === 'error' ? 'Save failed — click to retry'
                    : 'All changes saved'
            }
          >
            {syncStatus === 'saving'
              ? <LucideIcons.Loader2 className="w-4 h-4 animate-spin" />
              : syncStatus === 'error'
                ? <LucideIcons.AlertCircle className="w-4 h-4" />
                : syncStatus === 'synced' && pendingChangeCount === 0
                  ? <LucideIcons.CheckCircle className="w-4 h-4" />
                  : <LucideIcons.Save className="w-4 h-4" />
            }
            <span>
              {syncStatus === 'saving' ? 'Saving...'
                : syncStatus === 'error' ? 'Retry Save'
                  : pendingChangeCount > 0 ? `Save ${pendingChangeCount} change${pendingChangeCount === 1 ? '' : 's'}`
                  : syncStatus === 'synced' ? 'Saved'
                    : 'Save Blueprint'}
            </span>
            {(syncStatus === 'dirty' || pendingChangeCount > 0) && (
              <div className="w-2 h-2 rounded-full bg-blue-400 shadow-lg shadow-blue-400/50" />
            )}
          </button>
        </div>
      </div>

      {/* Search Results */}
      <AnimatePresence>
        {searchResults.length > 0 && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-3 flex items-center gap-2 flex-wrap relative"
          >
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-amber-500/10 border border-amber-500/20">
              <LucideIcons.Search className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-xs font-medium text-amber-500">{searchResults.length} found</span>
            </div>
            {searchResults.slice(0, 5).map((node, idx) => (
              <motion.button
                key={node.id}
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ delay: idx * 0.05 }}
                onClick={() => onSearchResultClick(node)}
                className="px-3 py-1.5 rounded-xl bg-white/[0.04] border border-white/[0.08] text-ink text-xs font-medium hover:bg-accent-lineage/15 hover:border-accent-lineage/30 hover:text-accent-lineage transition-all duration-200 hover:shadow-lg hover:shadow-accent-lineage/10"
              >
                {node.name}
              </motion.button>
            ))}
            {searchResults.length > 5 && (
              <span className="px-2 py-1 text-xs text-ink-muted/60">+{searchResults.length - 5} more</span>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
