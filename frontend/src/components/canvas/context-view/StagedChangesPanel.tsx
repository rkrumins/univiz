/**
 * SaveConfirmationModal — review-and-confirm dialog for staged canvas edits.
 *
 * Premium centered modal that opens when the user clicks the pending-changes
 * badge or "Save Blueprint". Lists every staged change grouped by type with
 * drill-in JSON diffs, undo/redo, per-row discard, and a single confirm
 * button that triggers `applyAll → saveToBackend`.
 */

import { useMemo, useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  useStagedChangesStore,
  stagedChangeColor,
  type StagedChange,
  type StagedChangeType,
} from '@/store/stagedChangesStore'

// Section labels — slight tone shift from the change type for human readability.
const TYPE_LABELS: Record<StagedChangeType, string> = {
  create_entity: 'New entities',
  rename_entity: 'Renames',
  update_entity: 'Property edits',
  delete_entity: 'Deletions',
  assign_layer: 'Layer assignments',
  move_to_layer: 'Layer rules',
  create_edge: 'New edges',
  edit_edge: 'Edge edits',
  delete_edge: 'Edge deletions',
  reverse_edge: 'Edge reversals',
}

const TYPE_ICONS: Record<StagedChangeType, keyof typeof LucideIcons> = {
  create_entity: 'PlusCircle',
  rename_entity: 'Pencil',
  update_entity: 'Settings2',
  delete_entity: 'Trash2',
  assign_layer: 'Move',
  move_to_layer: 'ArrowRightLeft',
  create_edge: 'GitBranchPlus',
  edit_edge: 'Cable',
  delete_edge: 'Unlink',
  reverse_edge: 'Repeat',
}

// Color tokens shared between row tints and section headings.
const TONE = {
  green: {
    text: 'text-emerald-300',
    border: 'border-emerald-400/30',
    bg: 'bg-emerald-500/[0.06]',
    glow: 'shadow-[inset_0_0_0_1px_rgba(16,185,129,0.15)]',
  },
  amber: {
    text: 'text-orange-300',
    border: 'border-orange-400/30',
    bg: 'bg-orange-500/[0.06]',
    glow: 'shadow-[inset_0_0_0_1px_rgba(251,146,60,0.15)]',
  },
  red: {
    text: 'text-rose-300',
    border: 'border-rose-400/30',
    bg: 'bg-rose-500/[0.06]',
    glow: 'shadow-[inset_0_0_0_1px_rgba(244,63,94,0.15)]',
  },
} as const

function timeAgo(ts: number): string {
  const seconds = Math.floor((Date.now() - ts) / 1000)
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

export interface SaveConfirmationModalProps {
  /** Called when the user confirms — implementer should run applyAll + saveToBackend. */
  onConfirm: () => void | Promise<void>
}

export function StagedChangesPanel({ onConfirm }: SaveConfirmationModalProps) {
  const isOpen = useStagedChangesStore(s => s.isReviewPanelOpen)
  const close = useStagedChangesStore(s => s.closeReviewPanel)
  const changes = useStagedChangesStore(s => s.changes)
  const discard = useStagedChangesStore(s => s.discard)
  const discardAll = useStagedChangesStore(s => s.discardAll)
  const applyStatus = useStagedChangesStore(s => s.applyStatus)
  const lastApplyResult = useStagedChangesStore(s => s.lastApplyResult)
  const undo = useStagedChangesStore(s => s.undo)
  const redo = useStagedChangesStore(s => s.redo)
  const redoStack = useStagedChangesStore(s => s.redoStack)

  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState('')
  const filterInputRef = useRef<HTMLInputElement>(null)

  const toggleExpanded = (id: string) => setExpandedIds(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  // ESC closes the modal — single, predictable escape.
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        close()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen, close])

  const grouped = useMemo(() => {
    const groups = new Map<StagedChangeType, StagedChange[]>()
    const q = filter.trim().toLowerCase()
    changes
      .filter(c => !q || c.summary.toLowerCase().includes(q) || (c.targetUrn ?? '').toLowerCase().includes(q))
      .forEach(c => {
        const list = groups.get(c.type) ?? []
        list.push(c)
        groups.set(c.type, list)
      })
    // Stable presentation order: creates → edits → moves → relations → deletes.
    const ORDER: StagedChangeType[] = [
      'create_entity', 'create_edge',
      'rename_entity', 'edit_edge', 'reverse_edge',
      'assign_layer', 'move_to_layer',
      'delete_edge', 'delete_entity',
    ]
    return ORDER
      .map(type => [type, groups.get(type) ?? []] as const)
      .filter(([, items]) => items.length > 0)
  }, [changes, filter])

  const total = changes.length
  const failedCount = changes.filter(c => c.error).length

  const summaryStats = useMemo(() => ({
    creates: changes.filter(c => c.type === 'create_entity' || c.type === 'create_edge').length,
    edits: changes.filter(c => c.type === 'rename_entity' || c.type === 'assign_layer' || c.type === 'move_to_layer' || c.type === 'edit_edge' || c.type === 'reverse_edge').length,
    deletes: changes.filter(c => c.type === 'delete_entity' || c.type === 'delete_edge').length,
  }), [changes])

  const handleConfirm = async () => {
    await onConfirm()
  }

  return createPortal(
    <AnimatePresence>
      {isOpen && (
        <motion.div
          key="staged-modal-shell"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-[80] flex items-center justify-center px-4 py-6"
          aria-hidden={!isOpen}
        >
          {/* Backdrop — full coverage, blurred, dismisses on click */}
          <div
            className="absolute inset-0 bg-black/70 backdrop-blur-md"
            onClick={close}
            data-canvas-interactive
          />

          {/* Modal card — centered via flex above; content sized to fit */}
          <motion.div
            key="staged-modal-card"
            initial={{ opacity: 0, scale: 0.96, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ type: 'spring', damping: 28, stiffness: 320 }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="staged-modal-title"
            className="relative w-full max-w-[760px] max-h-[min(820px,100%)] flex flex-col rounded-3xl overflow-hidden shadow-[0_50px_120px_-20px_rgba(0,0,0,0.85),0_0_0_1px_rgba(255,255,255,0.06)]"
            data-canvas-interactive
          >
            {/* Background — premium gradient + subtle noise */}
            <div className="absolute inset-0 bg-gradient-to-br from-[#0e1119] via-[#11141d] to-[#0c0e15]" />
            <div
              className="absolute inset-0 opacity-[0.03] pointer-events-none mix-blend-overlay"
              style={{
                backgroundImage: 'radial-gradient(circle at 30% 20%, rgba(251,191,36,0.15), transparent 50%), radial-gradient(circle at 80% 80%, rgba(168,85,247,0.10), transparent 50%)',
              }}
            />

            {/* Header */}
            <div className="relative flex-shrink-0 px-7 pt-6 pb-5 border-b border-white/[0.06]">
              {/* Top-right close */}
              <button
                onClick={close}
                className="absolute top-5 right-5 w-8 h-8 rounded-lg flex items-center justify-center text-white/40 hover:text-white hover:bg-white/[0.06] transition-all"
                aria-label="Close"
                title="Close (Esc)"
              >
                <LucideIcons.X className="w-4 h-4" />
              </button>

              <div className="flex items-start gap-4">
                <div className="relative w-12 h-12 rounded-2xl bg-gradient-to-br from-amber-400/30 via-orange-500/25 to-rose-500/15 border border-amber-300/30 flex items-center justify-center shadow-lg shadow-amber-500/15 flex-shrink-0">
                  <LucideIcons.GitPullRequestArrow className="w-6 h-6 text-amber-200" strokeWidth={2.2} />
                  {total > 0 && (
                    <span className="absolute -top-1.5 -right-1.5 min-w-[20px] h-5 rounded-full bg-amber-400 text-black text-[10px] font-black tracking-tight flex items-center justify-center px-1.5 shadow-md ring-2 ring-[#11141d]">
                      {total}
                    </span>
                  )}
                </div>

                <div className="flex-1 min-w-0 pr-10">
                  <h3 id="staged-modal-title" className="text-[17px] font-display font-bold text-white tracking-tight leading-tight">
                    Review &amp; Save Changes
                  </h3>
                  <p className="text-[12.5px] text-white/55 mt-0.5">
                    {total === 0
                      ? 'Nothing pending. Make some edits and they\'ll show up here for review.'
                      : <>Confirm <span className="font-semibold text-white/80 tabular-nums">{total}</span> edit{total === 1 ? '' : 's'} before they hit the backend.</>}
                    {failedCount > 0 && (
                      <span className="ml-1.5 inline-flex items-center gap-1 text-rose-300 font-semibold">
                        <LucideIcons.AlertTriangle className="w-3 h-3" />
                        {failedCount} previously failed
                      </span>
                    )}
                  </p>
                </div>
              </div>

              {/* Summary chips + Undo/Redo cluster */}
              {total > 0 && (
                <div className="flex items-center gap-2 mt-5">
                  <div className="flex items-center gap-1.5 flex-1 flex-wrap">
                    {summaryStats.creates > 0 && (
                      <SummaryChip color="emerald" icon="PlusCircle" count={summaryStats.creates} label="created" />
                    )}
                    {summaryStats.edits > 0 && (
                      <SummaryChip color="orange" icon="Pencil" count={summaryStats.edits} label="edited" />
                    )}
                    {summaryStats.deletes > 0 && (
                      <SummaryChip color="rose" icon="Trash2" count={summaryStats.deletes} label="deleted" />
                    )}
                  </div>

                  {/* Premium Undo/Redo cluster */}
                  <UndoRedoCluster
                    canUndo={changes.length > 0}
                    canRedo={redoStack.length > 0}
                    onUndo={undo}
                    onRedo={redo}
                  />
                </div>
              )}

              {/* Filter input */}
              {total > 4 && (
                <div className="relative mt-4">
                  <LucideIcons.Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-white/30" />
                  <input
                    ref={filterInputRef}
                    type="text"
                    value={filter}
                    onChange={e => setFilter(e.target.value)}
                    placeholder="Filter changes by name or URN…"
                    className="w-full pl-9 pr-8 py-2 rounded-lg bg-white/[0.04] border border-white/[0.08] text-[12px] text-white placeholder:text-white/30 focus:outline-none focus:border-amber-300/40 focus:bg-white/[0.06] transition-all"
                  />
                  {filter && (
                    <button
                      onClick={() => setFilter('')}
                      className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded text-white/40 hover:text-white hover:bg-white/[0.08] transition-all"
                      aria-label="Clear filter"
                    >
                      <LucideIcons.X className="w-3 h-3" />
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Body */}
            <div className="relative flex-1 overflow-y-auto px-5 py-4 space-y-5 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:bg-white/10 [&::-webkit-scrollbar-thumb]:rounded-full">
              {grouped.length === 0 ? (
                <EmptyState hasFilter={filter.length > 0} onClearFilter={() => setFilter('')} />
              ) : (
                grouped.map(([type, items]) => {
                  const IconKey = TYPE_ICONS[type]
                  const Icon = (LucideIcons[IconKey] as React.ComponentType<{ className?: string; strokeWidth?: number }>) ?? LucideIcons.Circle
                  const color = stagedChangeColor(type)
                  return (
                    <section key={type}>
                      <div className="flex items-center gap-2 px-2 mb-2">
                        <div className={cn(
                          'w-6 h-6 rounded-md flex items-center justify-center border',
                          TONE[color].bg, TONE[color].border,
                        )}>
                          <Icon className={cn('w-3.5 h-3.5', TONE[color].text)} strokeWidth={2.2} />
                        </div>
                        <h4 className={cn('text-[11px] font-bold uppercase tracking-[0.08em]', TONE[color].text)}>
                          {TYPE_LABELS[type]}
                        </h4>
                        <span className="text-[10px] tabular-nums text-white/40 font-semibold ml-auto">{items.length}</span>
                      </div>
                      <div className="space-y-1.5">
                        {items.map(change => (
                          <ChangeRow
                            key={change.id}
                            change={change}
                            colorKey={color}
                            isExpanded={expandedIds.has(change.id)}
                            onToggleExpanded={() => toggleExpanded(change.id)}
                            onDiscard={() => discard(change.id)}
                          />
                        ))}
                      </div>
                    </section>
                  )
                })
              )}
            </div>

            {/* Footer */}
            <div className="relative flex-shrink-0 px-6 py-4 border-t border-white/[0.06] bg-black/30 flex items-center gap-2">
              {total > 0 ? (
                <>
                  <button
                    onClick={() => {
                      if (confirm(`Discard all ${total} staged change${total === 1 ? '' : 's'}? This cannot be undone.`)) {
                        discardAll()
                        close()
                      }
                    }}
                    className="px-3 py-2 rounded-lg text-[12px] font-medium bg-transparent border border-white/[0.08] text-white/55 hover:bg-rose-500/10 hover:border-rose-400/40 hover:text-rose-200 transition-all"
                  >
                    Discard all
                  </button>
                  <div className="flex-1" />
                  <button
                    onClick={close}
                    className="px-4 py-2 rounded-lg text-[13px] font-medium bg-white/[0.04] border border-white/[0.08] text-white/75 hover:bg-white/[0.08] hover:text-white transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleConfirm}
                    disabled={applyStatus === 'applying'}
                    className={cn(
                      'flex items-center gap-2 px-5 py-2 rounded-lg text-[13px] font-bold transition-all',
                      applyStatus === 'applying'
                        ? 'bg-blue-500/15 border border-blue-400/30 text-blue-200 cursor-wait'
                        : 'bg-gradient-to-br from-amber-300 via-amber-400 to-orange-500 text-black shadow-[0_8px_24px_-8px_rgba(251,191,36,0.6)] hover:shadow-[0_10px_28px_-6px_rgba(251,191,36,0.7)] hover:scale-[1.02] active:scale-[0.98]'
                    )}
                  >
                    {applyStatus === 'applying' ? (
                      <>
                        <LucideIcons.Loader2 className="w-4 h-4 animate-spin" />
                        Saving…
                      </>
                    ) : (
                      <>
                        <LucideIcons.Check className="w-4 h-4" strokeWidth={3} />
                        Save {total} change{total === 1 ? '' : 's'}
                      </>
                    )}
                  </button>
                </>
              ) : (
                <>
                  <div className="flex-1" />
                  <button
                    onClick={close}
                    className="px-5 py-2 rounded-lg text-[13px] font-medium bg-white/[0.06] border border-white/10 text-white hover:bg-white/[0.10] transition-all"
                  >
                    Close
                  </button>
                </>
              )}
            </div>

            {applyStatus === 'partial-error' && lastApplyResult && (
              <div className="absolute bottom-[60px] left-6 right-6 px-3 py-2 rounded-lg bg-rose-500/15 border border-rose-400/40 text-[11px] text-rose-200 flex items-center gap-2">
                <LucideIcons.AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
                Last save: <span className="font-semibold">{lastApplyResult.failed}</span> failed,{' '}
                <span className="font-semibold">{lastApplyResult.ok}</span> succeeded — review &amp; retry above.
              </div>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Subcomponents

function SummaryChip({
  color,
  icon,
  count,
  label,
}: {
  color: 'emerald' | 'orange' | 'rose'
  icon: keyof typeof LucideIcons
  count: number
  label: string
}) {
  const Icon = (LucideIcons[icon] as React.ComponentType<{ className?: string; strokeWidth?: number }>) ?? LucideIcons.Circle
  const styles = {
    emerald: 'bg-emerald-500/12 border-emerald-400/35 text-emerald-200',
    orange:  'bg-orange-500/12  border-orange-400/35  text-orange-200',
    rose:    'bg-rose-500/12    border-rose-400/35    text-rose-200',
  } as const
  return (
    <span className={cn('inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[11px] font-semibold tabular-nums', styles[color])}>
      <Icon className="w-3 h-3" strokeWidth={2.4} />
      <span className="font-bold">{count}</span>
      <span className="opacity-80">{label}</span>
    </span>
  )
}

function UndoRedoCluster({
  canUndo,
  canRedo,
  onUndo,
  onRedo,
}: {
  canUndo: boolean
  canRedo: boolean
  onUndo: () => void
  onRedo: () => void
}) {
  return (
    <div className="flex items-stretch rounded-xl bg-gradient-to-b from-white/[0.06] to-white/[0.02] border border-white/[0.10] shadow-inner overflow-hidden">
      <button
        onClick={onUndo}
        disabled={!canUndo}
        title="Undo last change (⌘Z)"
        className={cn(
          'flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-semibold tracking-tight transition-all',
          canUndo
            ? 'text-white/85 hover:bg-white/[0.08] hover:text-white active:bg-white/[0.12]'
            : 'text-white/25 cursor-not-allowed'
        )}
        aria-label="Undo"
      >
        <LucideIcons.Undo2 className="w-3.5 h-3.5" strokeWidth={2.4} />
        Undo
      </button>
      <div className="w-px bg-white/[0.08]" />
      <button
        onClick={onRedo}
        disabled={!canRedo}
        title="Redo (⌘⇧Z)"
        className={cn(
          'flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-semibold tracking-tight transition-all',
          canRedo
            ? 'text-white/85 hover:bg-white/[0.08] hover:text-white active:bg-white/[0.12]'
            : 'text-white/25 cursor-not-allowed'
        )}
        aria-label="Redo"
      >
        Redo
        <LucideIcons.Redo2 className="w-3.5 h-3.5" strokeWidth={2.4} />
      </button>
    </div>
  )
}

function EmptyState({ hasFilter, onClearFilter }: { hasFilter: boolean; onClearFilter: () => void }) {
  return (
    <div className="px-4 py-16 text-center flex flex-col items-center gap-3">
      <div className="w-16 h-16 rounded-2xl bg-white/[0.03] border border-white/[0.06] flex items-center justify-center">
        <LucideIcons.CheckCircle2 className="w-8 h-8 text-white/20" strokeWidth={1.6} />
      </div>
      <div>
        <p className="text-sm text-white/75 font-semibold">
          {hasFilter ? 'No changes match your filter' : 'No staged changes'}
        </p>
        <p className="text-[12px] text-white/40 mt-1 max-w-[420px] leading-relaxed">
          {hasFilter
            ? 'Try a different search, or clear the filter to see everything pending.'
            : 'Edits to the canvas are staged here for review before being committed to the backend.'}
        </p>
      </div>
      {hasFilter && (
        <button
          onClick={onClearFilter}
          className="mt-1 px-3 py-1.5 text-[11px] font-medium rounded-lg bg-white/[0.04] border border-white/[0.08] text-white/70 hover:bg-white/[0.08] hover:text-white transition-all"
        >
          Clear filter
        </button>
      )}
    </div>
  )
}

function ChangeRow({
  change,
  colorKey,
  isExpanded,
  onToggleExpanded,
  onDiscard,
}: {
  change: StagedChange
  colorKey: 'green' | 'amber' | 'red'
  isExpanded: boolean
  onToggleExpanded: () => void
  onDiscard: () => void
}) {
  const tone = TONE[colorKey]
  return (
    <div
      className={cn(
        'group rounded-xl border px-3 py-2.5 transition-all',
        change.error ? 'bg-rose-500/[0.10] border-rose-500/50' : cn(tone.bg, tone.border, tone.glow),
      )}
    >
      <div className="flex items-start gap-2">
        <button
          onClick={onToggleExpanded}
          className="mt-0.5 p-0.5 rounded text-white/40 hover:text-white/85 hover:bg-white/[0.06] transition-all flex-shrink-0"
          aria-label={isExpanded ? 'Collapse' : 'Expand'}
        >
          <LucideIcons.ChevronRight className={cn('w-3.5 h-3.5 transition-transform duration-150', isExpanded && 'rotate-90')} />
        </button>
        <div className="flex-1 min-w-0">
          <p className="text-[13px] font-medium text-white/95 leading-tight" title={change.summary}>
            {change.summary}
          </p>
          <p className="text-[10.5px] text-white/40 mt-0.5 tabular-nums flex items-center gap-2">
            <span>{timeAgo(change.timestamp)}</span>
            {change.targetUrn && change.targetUrn !== change.targetId && (
              <>
                <span className="text-white/15">·</span>
                <span className="font-mono truncate max-w-[260px]" title={change.targetUrn}>{change.targetUrn}</span>
              </>
            )}
            {change.error && (
              <>
                <span className="text-white/15">·</span>
                <span className="text-rose-300 font-semibold" title={change.error}>failed</span>
              </>
            )}
          </p>

          <AnimatePresence initial={false}>
            {isExpanded && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: 'easeOut' }}
                className="overflow-hidden"
              >
                <div className="mt-2.5 grid grid-cols-2 gap-2">
                  <div className="rounded-md border border-white/[0.06] bg-black/40 p-2 min-w-0">
                    <p className="text-[9px] uppercase tracking-[0.08em] text-white/40 mb-1 font-bold">Before</p>
                    <pre className="text-[10.5px] text-white/65 whitespace-pre-wrap break-all max-h-32 overflow-y-auto font-mono leading-snug">
                      {change.before === undefined ? '—' : JSON.stringify(change.before, null, 2)}
                    </pre>
                  </div>
                  <div className="rounded-md border border-white/[0.06] bg-black/40 p-2 min-w-0">
                    <p className="text-[9px] uppercase tracking-[0.08em] text-white/40 mb-1 font-bold">After</p>
                    <pre className="text-[10.5px] text-white/65 whitespace-pre-wrap break-all max-h-32 overflow-y-auto font-mono leading-snug">
                      {change.after === undefined ? '—' : JSON.stringify(change.after, null, 2)}
                    </pre>
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
        <button
          onClick={onDiscard}
          className="opacity-0 group-hover:opacity-100 p-1.5 rounded-md hover:bg-rose-500/15 text-white/40 hover:text-rose-300 transition-all flex-shrink-0"
          title="Discard this change"
          aria-label="Discard change"
        >
          <LucideIcons.X className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  )
}
