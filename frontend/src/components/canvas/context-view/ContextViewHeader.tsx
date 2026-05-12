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

export interface ContextViewHeaderProps {
  // Search
  searchQuery: string
  onSearchChange: (q: string) => void
  searchResults: HierarchyNode[]
  onSearchResultClick: (node: HierarchyNode) => void

  // Lineage flow
  showLineageFlow: boolean
  onToggleLineageFlow: () => void

  // Edge direction toggle
  showEdgeDirection: boolean
  onToggleEdgeDirection: () => void

  // Trace — global toggle that mirrors the keyboard shortcut. Drawer's
  // per-node trace buttons remain for granular up/down/full control.
  traceActive: boolean
  canTrace: boolean
  onStartTrace: () => void
  onExitTrace: () => void

  // Add entity
  onAddEntity: () => void

  // Title — actual view name + entity-type count, shown in the header.
  // Replaces the previous hardcoded "Context View / Data Flow Blueprint".
  viewName?: string
  entityTypeCount?: number

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
  showEdgeDirection,
  onToggleEdgeDirection,
  traceActive,
  canTrace,
  onStartTrace,
  onExitTrace,
  onAddEntity,
  viewName,
  entityTypeCount,
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
    <div className="flex-shrink-0 bg-gradient-to-r from-canvas-elevated/90 via-canvas-elevated/95 to-canvas-elevated/90 backdrop-blur-xl border-b border-black/[0.08] dark:border-white/[0.06] px-6 py-3 relative">
      {/* Subtle gradient overlay — dark-mode decoration */}
      <div className="absolute inset-0 hidden dark:block bg-gradient-to-r from-accent-lineage/[0.02] via-transparent to-purple-500/[0.02] pointer-events-none" />

      <div className="grid grid-cols-[auto_1fr_auto] items-center gap-4 relative">
        {/* Title */}
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-accent-lineage/20 to-purple-500/20 flex items-center justify-center shadow-lg shadow-accent-lineage/10">
            <LucideIcons.Network className="w-5 h-5 text-accent-lineage" />
          </div>
          <div className="min-w-0">
            <h2 className="text-base font-display font-semibold text-ink tracking-tight truncate" title={viewName ?? 'Context View'}>
              {viewName ?? 'Context View'}
            </h2>
            <p className="text-[10px] text-ink-muted/70 flex items-center gap-1.5">
              <LucideIcons.ArrowRight className="w-3 h-3" />
              {typeof entityTypeCount === 'number'
                ? `${entityTypeCount} type${entityTypeCount === 1 ? '' : 's'} · Context View`
                : 'Context View'}
            </p>
          </div>
        </div>

        {/* Zone 2 — Search */}
        <div className="justify-self-center">
          <div className="relative group">
            <LucideIcons.Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-muted/60 group-focus-within:text-accent-lineage transition-colors" />
            <input
              ref={searchInputRef}
              type="text"
              placeholder="Search entities..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="w-56 pl-9 pr-8 py-2 rounded-xl bg-black/[0.04] dark:bg-white/[0.04] border border-black/[0.10] dark:border-white/[0.08] text-sm text-ink placeholder:text-ink-muted/50 focus:outline-none focus:border-accent-lineage/40 focus:bg-black/[0.05] dark:focus:bg-white/[0.06] transition-all"
            />
            {searchQuery && (
              <button
                onClick={() => onSearchChange('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-lg hover:bg-black/10 dark:hover:bg-white/10 text-ink-muted hover:text-ink transition-all"
              >
                <LucideIcons.X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* Zone 3 — Actions */}
        <div className="flex items-center gap-3">
          {/* Lineage Flow Toggle */}
          <button
            onClick={onToggleLineageFlow}
            className={cn(
              "flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300",
              showLineageFlow
                ? "bg-gradient-to-r from-accent-lineage/15 to-accent-lineage/[0.08] text-accent-lineage shadow-sm shadow-accent-lineage/10 border border-accent-lineage/35 dark:from-accent-lineage/20 dark:to-accent-lineage/10 dark:shadow-lg dark:shadow-accent-lineage/20 dark:border-accent-lineage/30"
                : "bg-black/[0.04] border border-black/[0.10] text-ink-muted hover:bg-black/[0.08] hover:text-ink dark:bg-white/[0.04] dark:border-white/[0.08] dark:hover:bg-white/[0.08]"
            )}
          >
            <motion.div animate={{ rotate: showLineageFlow ? 0 : -180 }} transition={{ duration: 0.3 }}>
              <LucideIcons.GitBranch className="w-4 h-4" />
            </motion.div>
            <span>
              {showLineageFlow
                ? (traceActive ? 'Flow + Trace' : 'Flow Active')
                : 'Show Flow'}
            </span>
            <div className={cn(
              "w-2 h-2 rounded-full transition-colors duration-300",
              showLineageFlow ? "bg-green-500 dark:bg-green-400 dark:shadow-lg dark:shadow-green-400/50" : "bg-ink-muted/30"
            )} />
          </button>

          {/* Show Direction toggle */}
          <button
            onClick={onToggleEdgeDirection}
            title={showEdgeDirection ? 'Hide edge direction' : 'Show edge direction'}
            className={cn(
              "flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium transition-all duration-300",
              showEdgeDirection
                ? "bg-gradient-to-r from-cyan-500/15 to-cyan-500/[0.08] text-cyan-700 border border-cyan-400/40 shadow-sm shadow-cyan-500/10 dark:from-cyan-500/20 dark:to-cyan-500/10 dark:text-cyan-300 dark:border-cyan-400/30 dark:shadow-lg dark:shadow-cyan-400/10"
                : "bg-black/[0.04] border border-black/[0.10] text-ink-muted hover:bg-black/[0.08] hover:text-ink dark:bg-white/[0.04] dark:border-white/[0.08] dark:hover:bg-white/[0.08]"
            )}
          >
            <LucideIcons.MoveRight className="w-3.5 h-3.5" />
            <span>{showEdgeDirection ? 'Direction On' : 'Direction Off'}</span>
          </button>

          <div className="w-px h-6 bg-gradient-to-b from-transparent via-black/15 dark:via-white/10 to-transparent" />

          {/* Trace toggle */}
          {traceActive ? (
            <button
              onClick={onExitTrace}
              title="Exit trace mode"
              className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium bg-gradient-to-r from-rose-500/20 to-rose-500/10 text-rose-700 border border-rose-400/50 hover:from-rose-500/30 hover:to-rose-500/20 hover:border-rose-400/70 dark:text-rose-200 dark:border-rose-400/40 dark:hover:border-rose-300/60 dark:hover:shadow-lg dark:hover:shadow-rose-500/20 transition-all duration-300"
            >
              <LucideIcons.X className="w-4 h-4" strokeWidth={2.4} />
              <span>Exit Trace</span>
              <span className="w-2 h-2 rounded-full bg-rose-500 dark:bg-rose-300 dark:shadow-lg dark:shadow-rose-300/60 animate-pulse" />
            </button>
          ) : (
            <button
              onClick={canTrace ? onStartTrace : undefined}
              disabled={!canTrace}
              title={canTrace ? 'Trace lineage of selected entity' : 'Select a single entity to trace its lineage'}
              className={cn(
                "flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300",
                canTrace
                  ? "bg-gradient-to-r from-accent-lineage/20 to-purple-500/10 text-accent-lineage border border-accent-lineage/40 hover:from-accent-lineage/30 hover:to-purple-500/20 hover:border-accent-lineage/60 dark:hover:shadow-lg dark:hover:shadow-accent-lineage/20"
                  : "bg-black/[0.03] border border-black/[0.06] text-ink-muted/50 dark:bg-white/[0.03] dark:border-white/[0.06] dark:text-ink-muted/40 cursor-not-allowed"
              )}
            >
              <LucideIcons.Workflow className="w-4 h-4" strokeWidth={2.2} />
              <span>Trace Lineage</span>
            </button>
          )}

          <div className="w-px h-6 bg-gradient-to-b from-transparent via-black/15 dark:via-white/10 to-transparent" />

          {/* Add Entity */}
          <button
            onClick={onAddEntity}
            className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium bg-gradient-to-r from-green-500/15 to-emerald-500/[0.08] text-green-700 border border-green-500/40 hover:from-green-500/25 hover:to-emerald-500/15 hover:border-green-500/60 dark:from-green-500/20 dark:to-emerald-500/10 dark:text-green-400 dark:border-green-500/30 dark:hover:shadow-lg dark:hover:shadow-green-500/20 transition-all duration-300"
          >
            <LucideIcons.Plus className="w-4 h-4" />
            <span>Add Entity</span>
          </button>

          <div className="w-px h-6 bg-gradient-to-b from-transparent via-black/15 dark:via-white/10 to-transparent" />

          {/* Blueprint indicator */}
          {activeContextModelName && (
            <div
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-purple-500/[0.08] border border-purple-500/25 dark:border-purple-500/20"
              title={activeContextModelName}
            >
              <LucideIcons.BookMarked className="w-3.5 h-3.5 text-purple-600 dark:text-purple-400 flex-shrink-0" />
              <span className="text-xs font-medium text-purple-700 dark:text-purple-300 truncate max-w-[140px]">
                {activeContextModelName}
              </span>
            </div>
          )}

          {/* Undo/Redo */}
          {(canUndo || canRedo) && (
            <div className="flex items-stretch rounded-xl overflow-hidden bg-black/[0.03] dark:bg-gradient-to-b dark:from-white/[0.06] dark:to-white/[0.02] border border-black/[0.10] dark:border-white/[0.08]">
              <button
                onClick={onUndo}
                disabled={!canUndo}
                title="Undo last change (⌘Z)"
                aria-label="Undo"
                className={cn(
                  "flex items-center gap-1.5 px-3 py-2 text-[11.5px] font-semibold tracking-tight transition-all",
                  canUndo
                    ? "text-ink/85 hover:bg-black/[0.06] hover:text-ink active:bg-black/[0.10] dark:hover:bg-white/[0.06] dark:active:bg-white/[0.10]"
                    : "text-ink-muted/40 dark:text-ink-muted/25 cursor-not-allowed"
                )}
              >
                <LucideIcons.Undo2 className="w-3.5 h-3.5" strokeWidth={2.4} />
                <span>Undo</span>
              </button>
              <div className="w-px bg-black/[0.10] dark:bg-white/[0.08]" />
              <button
                onClick={onRedo}
                disabled={!canRedo}
                title="Redo (⌘⇧Z)"
                aria-label="Redo"
                className={cn(
                  "flex items-center gap-1.5 px-3 py-2 text-[11.5px] font-semibold tracking-tight transition-all",
                  canRedo
                    ? "text-ink/85 hover:bg-black/[0.06] hover:text-ink active:bg-black/[0.10] dark:hover:bg-white/[0.06] dark:active:bg-white/[0.10]"
                    : "text-ink-muted/40 dark:text-ink-muted/25 cursor-not-allowed"
                )}
              >
                <span>Redo</span>
                <LucideIcons.Redo2 className="w-3.5 h-3.5" strokeWidth={2.4} />
              </button>
            </div>
          )}

          {/* Pending changes */}
          {pendingChangeCount > 0 && onOpenStagedChanges && (
            <button
              onClick={onOpenStagedChanges}
              title="Review pending changes"
              className="relative flex items-center gap-2 pl-2.5 pr-3 py-2 rounded-xl bg-gradient-to-br from-amber-300/25 via-amber-400/20 to-orange-500/15 border border-amber-400/60 text-amber-800 hover:from-amber-300/35 hover:to-orange-500/25 hover:border-amber-400/80 transition-all shadow-sm shadow-amber-500/15 hover:shadow-md hover:shadow-amber-500/20 dark:text-amber-100 dark:border-amber-300/50 dark:hover:border-amber-200/70 dark:hover:shadow-lg dark:hover:shadow-amber-500/25"
            >
              <span className="absolute -top-1 -right-1 flex h-3 w-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-500 dark:bg-amber-300 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-500 dark:bg-amber-300 ring-2 ring-canvas-elevated" />
              </span>
              <span className="flex items-center justify-center w-6 h-6 rounded-lg bg-amber-200 border border-amber-300 dark:bg-amber-300/25 dark:border-amber-200/40">
                <LucideIcons.ListChecks className="w-3.5 h-3.5 text-amber-800 dark:text-amber-100" strokeWidth={2.4} />
              </span>
              <span className="text-[12px] font-bold tabular-nums leading-none">{pendingChangeCount}</span>
              <span className="text-[10.5px] uppercase tracking-[0.08em] font-bold leading-none">Pending</span>
            </button>
          )}

          {/* Save */}
          <button
            onClick={onSave}
            disabled={(syncStatus !== 'dirty' && syncStatus !== 'error' && pendingChangeCount === 0) || !activeWorkspaceId}
            className={cn(
              "flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-all duration-300",
              (syncStatus === 'dirty' || pendingChangeCount > 0)
                ? "bg-gradient-to-r from-blue-500/15 to-cyan-500/[0.08] text-blue-700 border border-blue-500/40 hover:from-blue-500/25 hover:to-cyan-500/15 hover:border-blue-500/60 dark:from-blue-500/20 dark:to-cyan-500/10 dark:text-blue-400 dark:border-blue-500/30 dark:hover:shadow-lg dark:hover:shadow-blue-500/20"
                : syncStatus === 'error'
                  ? "bg-gradient-to-r from-red-500/15 to-red-500/[0.08] text-red-700 border border-red-500/40 dark:from-red-500/20 dark:to-red-500/10 dark:text-red-400 dark:border-red-500/30"
                  : "bg-black/[0.03] border border-black/[0.06] text-ink-muted/50 dark:bg-white/[0.03] dark:border-white/[0.06] dark:text-ink-muted/50 cursor-not-allowed"
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
              <div className="w-2 h-2 rounded-full bg-blue-500 dark:bg-blue-400 dark:shadow-lg dark:shadow-blue-400/50" />
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
                className="px-3 py-1.5 rounded-xl bg-black/[0.04] dark:bg-white/[0.04] border border-black/[0.10] dark:border-white/[0.08] text-ink text-xs font-medium hover:bg-accent-lineage/15 hover:border-accent-lineage/40 hover:text-accent-lineage transition-all duration-200 hover:shadow-md hover:shadow-black/5 dark:hover:shadow-lg dark:hover:shadow-accent-lineage/10"
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

