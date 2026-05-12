import { memo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
    Loader2, AlertCircle, ChevronRight, RotateCcw, StopCircle, Play, Trash2,
    AlertTriangle, Server, FolderOpen,
} from 'lucide-react'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { cn } from '@/lib/utils'
import type { AggregationJobResponse } from '@/services/aggregationService'
import { useJob } from '@/hooks/useJob'
import { getProviderLogo } from '../ProviderLogos'
import { formatDuration, timeAgo, STATUS_CONFIG, type DataSourceMeta } from './shared'

// Phase 1.7 — UI phase visibility. Maps the backend's short phase IDs
// (emitted by FalkorDBProvider's bulk-rebuild path) to operator-
// readable status labels. ``null`` / unrecognized values fall back to
// the generic "Processing lineage edges" string so legacy / non-
// FalkorDB paths keep the old UX.
const PHASE_LABELS: Record<string, string> = {
    wiping: 'Wiping previous aggregated edges',
    scanning: 'Scanning lineage edges',
    resolving_labels: 'Resolving entity labels',
    creating: 'Creating aggregated edges in graph',
    finalizing: 'Finalizing bookkeeping',
}

function phaseLabel(currentPhase: string | null | undefined): string {
    if (currentPhase && PHASE_LABELS[currentPhase]) {
        return PHASE_LABELS[currentPhase]
    }
    return 'Processing lineage edges'
}

// ── Tooltip ──────────────────────────────────────────────────────────

export function Tip({ children, label }: { children: React.ReactNode; label: string }) {
    return (
        <TooltipPrimitive.Provider delayDuration={300}>
            <TooltipPrimitive.Root>
                <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
                <TooltipPrimitive.Portal>
                    <TooltipPrimitive.Content
                        side="top"
                        sideOffset={6}
                        className="z-50 px-2.5 py-1.5 rounded-lg bg-ink text-canvas text-[11px] font-medium shadow-lg animate-in fade-in zoom-in-95 duration-150"
                    >
                        {label}
                        <TooltipPrimitive.Arrow className="fill-ink" />
                    </TooltipPrimitive.Content>
                </TooltipPrimitive.Portal>
            </TooltipPrimitive.Root>
        </TooltipPrimitive.Provider>
    )
}

// ── StatCell ─────────────────────────────────────────────────────────

export function StatCell({ label, value, capitalize }: { label: string; value: React.ReactNode; capitalize?: boolean }) {
    return (
        <div className="rounded-lg bg-black/[0.02] dark:bg-white/[0.02] px-3 py-2">
            <span className="block text-[9px] text-ink-muted/60 uppercase tracking-wider font-bold mb-1">{label}</span>
            <span className={cn('text-[12px] font-semibold text-ink tabular-nums', capitalize && 'capitalize')}>{value}</span>
        </div>
    )
}

// ── KpiCard ──────────────────────────────────────────────────────────

export function KpiCard({ icon: Icon, label, value, accent, iconBg }: {
    icon: typeof AlertCircle; label: string; value: string; accent: string; iconBg: string
}) {
    return (
        <div className="group relative rounded-xl border border-glass-border/60 bg-canvas px-4 py-3 flex items-center gap-3 overflow-hidden transition-all hover:border-glass-border hover:shadow-sm">
            <div className={cn('absolute inset-0 opacity-[0.03] group-hover:opacity-[0.06] transition-opacity', iconBg.replace('/10', '/100'))} />
            <div className={cn('relative w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0', iconBg)}>
                <Icon className="w-4 h-4" />
            </div>
            <div className="relative">
                <p className={cn('text-xl font-bold tabular-nums leading-none tracking-tight', accent)}>{value}</p>
                <p className="text-[10px] text-ink-muted/70 uppercase tracking-wider font-bold mt-1">{label}</p>
            </div>
        </div>
    )
}

// ── Job Row ──────────────────────────────────────────────────────────

export interface JobRowProps {
    job: AggregationJobResponse
    meta?: DataSourceMeta
    expanded: boolean
    // Takes the row's job id so the parent can pass a stable callback that
    // doesn't re-create per row (which would defeat React.memo on JobRow).
    onToggle: (jobId: string) => void
    onCancel: (job: AggregationJobResponse) => void
    onResume: (job: AggregationJobResponse) => void
    onRetrigger: (job: AggregationJobResponse) => void
    onDelete: (job: AggregationJobResponse) => void
    onPurge: (job: AggregationJobResponse) => void
    purgeConfirm: string | null
    setPurgeConfirm: (id: string | null) => void
    actionLoading: boolean
    compact?: boolean
    previousJob?: AggregationJobResponse
}

export const JobRow = memo(function JobRow({ job: jobFromList, meta, expanded, onToggle, onCancel, onResume, onRetrigger, onDelete, onPurge, purgeConfirm, setPurgeConfirm, actionLoading, compact, previousJob }: JobRowProps) {
    // Open the SSE stream only for actively-running jobs so terminal
    // rows (the bulk of Job History) don't open dead EventSources.
    // Phase 3's useJobsLive(scope) consolidates this to one connection
    // per workspace; for Phase 1 we accept N connections per visible
    // running row (HTTP/1.1 caps at 6, sufficient in practice).
    const isActive = jobFromList.status === 'running' || jobFromList.status === 'pending'
    const liveOverlay = useJob(
        jobFromList.dataSourceId,
        jobFromList.id,
        isActive,
    )

    // Merge the live snapshot onto the polling-derived job so the
    // rest of the component reads from a single ``job`` object. The
    // snapshot only carries fields that landed in events; any field
    // it doesn't touch falls through to the polling value. After a
    // ``terminal`` event lands, defer entirely to the polling-fetched
    // row (DB is the source of truth post-terminal).
    const job: AggregationJobResponse =
        isActive && !liveOverlay.terminal && Object.keys(liveOverlay.snapshot).length > 0
            ? {
                ...jobFromList,
                processedEdges: liveOverlay.snapshot.processed_edges ?? jobFromList.processedEdges,
                totalEdges: liveOverlay.snapshot.total_edges ?? jobFromList.totalEdges,
                createdEdges: liveOverlay.snapshot.created_edges ?? jobFromList.createdEdges,
                progress: liveOverlay.snapshot.progress ?? jobFromList.progress,
                lastCursor: liveOverlay.snapshot.last_cursor ?? jobFromList.lastCursor,
                lastCheckpointAt: liveOverlay.snapshot.last_heartbeat_at ?? jobFromList.lastCheckpointAt,
            }
            : jobFromList

    const cfg = STATUS_CONFIG[job.status] ?? STATUS_CONFIG.pending
    const StatusIcon = cfg.icon
    const isRunning = job.status === 'running'
    const isPending = job.status === 'pending'
    const canCancel = isPending || isRunning
    const canResume = job.resumable
    const isTerminal = job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
    const isPurging = purgeConfirm === job.id
    const dsName = meta?.label || job.dataSourceLabel || job.dataSourceId
    const wsName = meta?.workspaceName || job.workspaceName
    const provType = meta?.providerType
    const ProviderLogoIcon = getProviderLogo(provType ?? '')

    // Diff-to-previous computations
    const edgeDelta = previousJob && job.status === 'completed' && previousJob.status === 'completed'
        ? job.createdEdges - previousJob.createdEdges : null
    const durationDelta = previousJob && job.durationSeconds != null && previousJob.durationSeconds != null
        ? job.durationSeconds - previousJob.durationSeconds : null

    const colSpan = compact ? 8 : 9

    return (
        <>
            <tr
                onClick={() => onToggle(jobFromList.id)}
                className={cn(
                    'group border-b border-glass-border/40 cursor-pointer transition-all duration-200',
                    'hover:bg-gradient-to-r hover:from-transparent hover:via-black/[0.02] hover:to-transparent',
                    'dark:hover:via-white/[0.02]',
                    expanded && 'bg-black/[0.025] dark:bg-white/[0.025]',
                    isRunning && 'border-l-2 border-l-indigo-500/60',
                    isPending && 'border-l-2 border-l-amber-500/60',
                    job.status === 'failed' && 'border-l-2 border-l-red-500/40',
                    isTerminal && job.status !== 'failed' && 'border-l-2 border-l-transparent',
                )}
            >
                {/* Status */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    <div className="flex items-center gap-2">
                        <motion.span
                            animate={{ rotate: expanded ? 90 : 0 }}
                            transition={{ duration: 0.15 }}
                            className="flex items-center justify-center w-4 h-4 text-ink-muted/50 group-hover:text-ink-muted transition-colors"
                        >
                            <ChevronRight className="w-3 h-3" />
                        </motion.span>
                        <span className={cn(
                            'inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[11px] font-semibold',
                            cfg.bg, cfg.color,
                        )}>
                            <StatusIcon className={cn('w-3 h-3', isRunning && 'animate-spin')} />
                            {cfg.label}
                        </span>
                    </div>
                </td>

                {/* Data Source + Provider + Workspace — hidden in compact mode */}
                {!compact && (
                    <td className="px-4 py-3">
                        <div className="space-y-1.5">
                            <div className="flex items-center gap-1.5">
                                {provType && <ProviderLogoIcon className="w-3.5 h-3.5 flex-shrink-0" />}
                                <span className="text-[13px] font-semibold text-ink leading-none truncate">
                                    {dsName}
                                </span>
                            </div>
                            <div className="flex items-center gap-1.5 text-[10px] text-ink-muted">
                                {meta?.providerName && (
                                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/[0.03] dark:bg-white/[0.04]">
                                        <Server className="w-2.5 h-2.5 text-ink-muted/50" />
                                        <span className="font-medium truncate max-w-[100px]">{meta.providerName}</span>
                                    </span>
                                )}
                                {wsName && wsName !== meta?.providerName && (
                                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/[0.03] dark:bg-white/[0.04]">
                                        <FolderOpen className="w-2.5 h-2.5 text-ink-muted/50" />
                                        <span className="truncate max-w-[100px]">{wsName}</span>
                                    </span>
                                )}
                            </div>
                        </div>
                    </td>
                )}

                {/* Mode */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    {(job.projectionMode ?? meta?.projectionMode) ? (
                        <span className="text-[11px] text-ink-muted font-medium">
                            {(job.projectionMode ?? meta?.projectionMode) === 'in_source' ? 'In-Source' : 'Dedicated'}
                        </span>
                    ) : <span className="text-[10px] text-ink-muted/40">{'\u2014'}</span>}
                </td>

                {/* Trigger */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    {job.triggerSource === 'purge' ? (
                        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-red-400">
                            <Trash2 className="w-3 h-3" /> Purge
                        </span>
                    ) : (
                        <span className="text-[11px] text-ink-muted capitalize">{job.triggerSource}</span>
                    )}
                </td>

                {/* Progress */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    {job.triggerSource === 'purge' ? (
                        <span className="text-[11px] text-ink-muted/40">{'\u2014'}</span>
                    ) : isRunning && job.totalEdges > 0 ? (
                        <div className="w-20">
                            <div className="flex items-center justify-between mb-0.5">
                                <span className="text-[10px] font-bold text-indigo-400 tabular-nums">{job.progress}%</span>
                            </div>
                            <div className="w-full h-1.5 bg-indigo-500/10 rounded-full overflow-hidden">
                                <motion.div
                                    className="h-full bg-gradient-to-r from-indigo-500 to-violet-500 rounded-full"
                                    animate={{ width: `${Math.min(100, job.progress)}%` }}
                                    transition={{ duration: 0.6, ease: 'easeOut' }}
                                />
                            </div>
                        </div>
                    ) : job.edgeCoveragePct != null ? (
                        <span className={cn(
                            'text-[11px] font-semibold tabular-nums',
                            job.edgeCoveragePct >= 100 ? 'text-emerald-500' : job.edgeCoveragePct >= 50 ? 'text-amber-500' : 'text-ink-muted',
                        )}>
                            {job.edgeCoveragePct}%
                        </span>
                    ) : <span className="text-[11px] text-ink-muted/40">{'\u2014'}</span>}
                </td>

                {/* Edges */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    {job.triggerSource === 'purge' ? (
                        <span className="text-[11px] text-red-400/80 font-medium tabular-nums">
                            {job.processedEdges.toLocaleString()} purged
                        </span>
                    ) : (
                        <div className="space-y-0.5">
                            <span className="text-[11px] text-ink tabular-nums font-medium block">
                                {job.processedEdges.toLocaleString()}{job.totalEdges > 0 ? ` / ${job.totalEdges.toLocaleString()}` : ''}
                            </span>
                            {job.status === 'completed' && job.createdEdges > 0 && (
                                <span className="text-[10px] text-emerald-500 font-semibold block tabular-nums">
                                    +{job.createdEdges.toLocaleString()} materialized
                                </span>
                            )}
                        </div>
                    )}
                </td>

                {/* Duration */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    <span className="text-[11px] text-ink-muted tabular-nums">{formatDuration(job.durationSeconds)}</span>
                </td>

                {/* Started */}
                <td className={cn('px-4', compact ? 'py-2' : 'py-3')}>
                    <span className="text-[11px] text-ink-muted" title={job.startedAt ? new Date(job.startedAt).toLocaleString() : job.createdAt}>
                        {timeAgo(job.startedAt ?? job.createdAt)}
                    </span>
                </td>

                {/* Actions */}
                <td className={cn('px-4 text-right', compact ? 'py-1.5' : 'py-2.5')}>
                    <div className="flex items-center justify-end gap-0.5" onClick={e => e.stopPropagation()}>
                        {canCancel && (
                            <Tip label="Cancel job">
                                <button onClick={() => onCancel(job)} disabled={actionLoading}
                                    className="p-1.5 rounded-lg text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40">
                                    {actionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <StopCircle className="w-3.5 h-3.5" />}
                                </button>
                            </Tip>
                        )}
                        {canResume && (
                            <Tip label="Resume from checkpoint">
                                <button onClick={() => onResume(job)} disabled={actionLoading}
                                    className="p-1.5 rounded-lg text-indigo-400 hover:bg-indigo-500/10 transition-colors disabled:opacity-40">
                                    {actionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />}
                                </button>
                            </Tip>
                        )}
                        {isTerminal && (
                            <>
                                <Tip label="Re-trigger aggregation">
                                    <button onClick={() => onRetrigger(job)} disabled={actionLoading}
                                        className="p-1.5 rounded-lg text-emerald-400 hover:bg-emerald-500/10 transition-colors disabled:opacity-40">
                                        {actionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                                    </button>
                                </Tip>
                                <Tip label="Delete from history">
                                    <button onClick={() => onDelete(job)} disabled={actionLoading}
                                        className="p-1.5 rounded-lg text-ink-muted hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40">
                                        {actionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                                    </button>
                                </Tip>
                            </>
                        )}
                    </div>
                </td>
            </tr>

            {/* Expanded detail panel */}
            <AnimatePresence>
                {expanded && (
                    <tr>
                        <td colSpan={colSpan} className="p-0">
                            <motion.div
                                initial={{ opacity: 0, height: 0 }}
                                animate={{ opacity: 1, height: 'auto' }}
                                exit={{ opacity: 0, height: 0 }}
                                transition={{ duration: 0.25, ease: [0.4, 0, 0.2, 1] }}
                                className="overflow-hidden"
                            >
                                <div className={cn(
                                    'mx-3 my-2 rounded-2xl border overflow-hidden',
                                    'bg-gradient-to-b from-canvas to-canvas-elevated',
                                    job.status === 'failed' ? 'border-red-500/20' :
                                    isRunning ? 'border-indigo-500/20' :
                                    job.status === 'completed' ? 'border-emerald-500/15' :
                                    'border-glass-border/60',
                                )}>
                                    <div className={cn(
                                        'h-0.5',
                                        job.status === 'failed' ? 'bg-gradient-to-r from-red-500/80 via-red-500/40 to-transparent' :
                                        isRunning ? 'bg-gradient-to-r from-indigo-500/80 via-violet-500/40 to-transparent' :
                                        job.status === 'completed' ? 'bg-gradient-to-r from-emerald-500/80 via-emerald-500/40 to-transparent' :
                                        'bg-gradient-to-r from-zinc-500/30 to-transparent',
                                    )} />

                                    <div className="p-5 space-y-4">
                                        {/* Header row */}
                                        <div className="flex items-start justify-between">
                                            <div className="space-y-1.5">
                                                <div className="flex items-center gap-1.5">
                                                    <ProviderLogoIcon className="w-4 h-4 flex-shrink-0" />
                                                    <h3 className="text-sm font-bold text-ink">{dsName}</h3>
                                                </div>
                                                <div className="flex items-center gap-1.5 text-[10px] text-ink-muted">
                                                    {meta?.providerName && (
                                                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/[0.03] dark:bg-white/[0.04]">
                                                            <Server className="w-2.5 h-2.5 text-ink-muted/50" />
                                                            <span className="font-medium">{meta.providerName}</span>
                                                        </span>
                                                    )}
                                                    {wsName && wsName !== meta?.providerName && (
                                                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/[0.03] dark:bg-white/[0.04]">
                                                            <FolderOpen className="w-2.5 h-2.5 text-ink-muted/50" />
                                                            <span>{wsName}</span>
                                                        </span>
                                                    )}
                                                    {meta?.graphName && (
                                                        <span className="font-mono text-ink-muted/50">{meta.graphName}</span>
                                                    )}
                                                </div>
                                            </div>
                                            <span className="font-mono text-[10px] text-ink-muted/50 select-all">{job.id}</span>
                                        </div>

                                        {/* Progress bar (running / pending) */}
                                        {(isRunning || isPending) && job.totalEdges > 0 && (
                                            <div className="space-y-2">
                                                <div className="flex items-center justify-between">
                                                    <div className="flex items-center gap-2">
                                                        <span className="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-pulse" />
                                                        <span className="text-[11px] font-semibold text-ink">
                                                            {isRunning ? phaseLabel(job.currentPhase) : 'Queued'}
                                                        </span>
                                                    </div>
                                                    <span className="text-[12px] font-bold text-indigo-400 tabular-nums">
                                                        {job.progress}%
                                                    </span>
                                                </div>
                                                <div className="w-full h-2 bg-indigo-500/[0.07] rounded-full overflow-hidden">
                                                    <motion.div
                                                        className="h-full rounded-full bg-gradient-to-r from-indigo-500 via-violet-500 to-indigo-400"
                                                        initial={{ width: 0 }}
                                                        animate={{ width: `${Math.min(100, job.progress)}%` }}
                                                        transition={{ duration: 0.8, ease: 'easeOut' }}
                                                    />
                                                </div>
                                                <div className="flex items-center justify-between text-[10px] text-ink-muted">
                                                    <span className="tabular-nums">
                                                        {job.processedEdges.toLocaleString()} / {job.totalEdges.toLocaleString()} edges
                                                        {job.createdEdges > 0 && (
                                                            <span className="text-emerald-500 ml-1.5">
                                                                ({job.createdEdges.toLocaleString()} materialized)
                                                            </span>
                                                        )}
                                                    </span>
                                                    {job.estimatedCompletionAt && (
                                                        <span>ETA {new Date(job.estimatedCompletionAt).toLocaleTimeString()}</span>
                                                    )}
                                                </div>
                                            </div>
                                        )}

                                        {/* Stat grid */}
                                        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
                                            <StatCell
                                                label="Trigger"
                                                value={job.triggerSource === 'purge' ? 'Purge' : job.triggerSource}
                                                capitalize
                                            />
                                            <StatCell label="Batch Size" value={
                                                job.triggerSource === 'purge' ? '\u2014' : job.batchSize.toLocaleString()
                                            } />
                                            <StatCell label="Duration" value={
                                                <span className="flex items-center gap-1">
                                                    {formatDuration(job.durationSeconds)}
                                                    {durationDelta != null && durationDelta !== 0 && (
                                                        <span className={cn('text-[9px] font-bold', durationDelta > 0 ? 'text-red-400' : 'text-emerald-400')}>
                                                            {durationDelta > 0 ? '+' : ''}{formatDuration(durationDelta)}
                                                        </span>
                                                    )}
                                                </span>
                                            } />
                                            {job.triggerSource !== 'purge' && (
                                                <StatCell label="Retries" value={
                                                    <span className="flex items-center gap-1.5">
                                                        {job.retryCount}
                                                        {job.resumable && (
                                                            <span className="px-1 py-0.5 rounded bg-indigo-500/10 text-[8px] font-bold text-indigo-400 uppercase leading-none">
                                                                resumable
                                                            </span>
                                                        )}
                                                    </span>
                                                } />
                                            )}
                                            <StatCell
                                                label={job.triggerSource === 'purge' ? 'Purged' : 'Materialized'}
                                                value={
                                                    job.triggerSource === 'purge'
                                                        ? <span className="text-red-400">{job.processedEdges.toLocaleString()}</span>
                                                        : job.createdEdges > 0
                                                            ? <span className="flex items-center gap-1">
                                                                <span className="text-emerald-400">{job.createdEdges.toLocaleString()}</span>
                                                                {edgeDelta != null && edgeDelta !== 0 && (
                                                                    <span className={cn('text-[9px] font-bold', edgeDelta > 0 ? 'text-emerald-400' : 'text-red-400')}>
                                                                        {edgeDelta > 0 ? '+' : ''}{edgeDelta.toLocaleString()}
                                                                    </span>
                                                                )}
                                                              </span>
                                                            : job.status === 'completed' ? '0' : '\u2014'
                                                }
                                            />
                                        </div>

                                        {/* Timeline */}
                                        <div className="flex items-center gap-3 text-[10px] text-ink-muted border-t border-glass-border/30 pt-3">
                                            <span>Created {new Date(job.createdAt).toLocaleString()}</span>
                                            {job.startedAt && (
                                                <>
                                                    <span className="text-ink-muted/20">{'\u2192'}</span>
                                                    <span>Started {new Date(job.startedAt).toLocaleString()}</span>
                                                </>
                                            )}
                                            {job.completedAt && (
                                                <>
                                                    <span className="text-ink-muted/20">{'\u2192'}</span>
                                                    <span>
                                                        {job.status === 'completed' ? 'Completed' :
                                                         job.status === 'failed' ? 'Failed' : 'Ended'}{' '}
                                                        {new Date(job.completedAt).toLocaleString()}
                                                    </span>
                                                </>
                                            )}
                                            {job.lastCheckpointAt && isRunning && (
                                                <span className="ml-auto flex items-center gap-1">
                                                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                                                    Checkpoint {timeAgo(job.lastCheckpointAt)}
                                                </span>
                                            )}
                                        </div>

                                        {/* Error */}
                                        {job.errorMessage && (
                                            <div className="rounded-xl bg-red-500/[0.04] border border-red-500/10 p-4">
                                                <div className="flex items-center gap-2 mb-2">
                                                    <AlertCircle className="w-3.5 h-3.5 text-red-400" />
                                                    <span className="text-[10px] font-bold text-red-400/80 uppercase tracking-wider">Error Detail</span>
                                                </div>
                                                <pre className="text-[11px] font-mono text-red-400/80 break-words whitespace-pre-wrap leading-relaxed">
                                                    {job.errorMessage}
                                                </pre>
                                                {job.errorMessage.includes('Max retries exceeded') && (
                                                    <p className="mt-2 text-[10px] text-amber-400/80 flex items-center gap-1.5">
                                                        <AlertTriangle className="w-3 h-3 flex-shrink-0" />
                                                        Likely caused by server restarts during processing, not a job logic failure.
                                                    </p>
                                                )}
                                            </div>
                                        )}

                                        {/* Purge action */}
                                        {isTerminal && job.createdEdges > 0 && job.triggerSource !== 'purge' && (
                                            <div className="border-t border-glass-border/30 pt-3">
                                                {!isPurging ? (
                                                    <button
                                                        onClick={() => setPurgeConfirm(job.id)}
                                                        className="flex items-center gap-1.5 text-[11px] font-medium text-ink-muted/60 hover:text-red-400 transition-colors duration-200"
                                                    >
                                                        <Trash2 className="w-3 h-3" />
                                                        Purge {job.createdEdges.toLocaleString()} aggregated edges from graph
                                                    </button>
                                                ) : (
                                                    <motion.div
                                                        initial={{ opacity: 0, y: -4 }}
                                                        animate={{ opacity: 1, y: 0 }}
                                                        className="flex items-center gap-3 p-3 rounded-xl bg-red-500/[0.04] border border-red-500/15"
                                                    >
                                                        <AlertTriangle className="w-4 h-4 text-red-400 flex-shrink-0" />
                                                        <span className="text-[11px] text-red-400 flex-1">
                                                            Remove all materialized edges? This cannot be undone.
                                                        </span>
                                                        <button
                                                            onClick={() => onPurge(job)}
                                                            disabled={actionLoading}
                                                            className="px-3 py-1.5 rounded-lg text-[11px] font-bold bg-red-500 text-white hover:bg-red-600 transition-all shadow-sm shadow-red-500/20 disabled:opacity-40"
                                                        >
                                                            {actionLoading ? <Loader2 className="w-3 h-3 animate-spin" /> : 'Confirm Purge'}
                                                        </button>
                                                        <button onClick={() => setPurgeConfirm(null)} className="text-[11px] text-ink-muted hover:text-ink transition-colors">
                                                            Cancel
                                                        </button>
                                                    </motion.div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            </motion.div>
                        </td>
                    </tr>
                )}
            </AnimatePresence>
        </>
    )
})
