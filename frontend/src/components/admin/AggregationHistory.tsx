import { useState, useEffect, useCallback, memo } from 'react'
import { aggregationService, type AggregationJobResponse } from '@/services/aggregationService'
import { Loader2, CheckCircle2, AlertCircle, Clock, PlayCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { POLLING_INTERVALS } from '@/config/polling'
import { usePolling } from '@/hooks/usePolling'

interface AggregationHistoryProps {
    dataSourceId: string;
}

export function AggregationHistory({ dataSourceId }: AggregationHistoryProps) {
    const [jobs, setJobs] = useState<AggregationJobResponse[]>([])
    const [isLoading, setIsLoading] = useState(true)

    const fetchJobs = useCallback(async () => {
        try {
            const result = await aggregationService.listJobs(dataSourceId)
            setJobs(result)
        } catch (err) {
            console.error('Failed to fetch aggregation jobs', err)
        } finally {
            setIsLoading(false)
        }
    }, [dataSourceId])

    useEffect(() => { fetchJobs() }, [fetchJobs])

    // Active-only polling: only fires while a job is pending or running.
    // ``usePolling`` adds jitter + Page Visibility pause for free, so 1000
    // simultaneous viewers no longer fire in lockstep and backgrounded
    // tabs stop paying any cost. ``fireOnMount: false`` because the
    // initial fetch above already loaded the first snapshot.
    const isPolling = jobs.some(j => j.status === 'pending' || j.status === 'running')
    usePolling(fetchJobs, POLLING_INTERVALS.aggregationHistoryActive, {
        enabled: isPolling,
        fireOnMount: false,
    })

    if (isLoading && jobs.length === 0) {
        return (
            <div className="flex items-center justify-center p-4 text-ink-muted">
                <Loader2 className="w-5 h-5 animate-spin" />
            </div>
        )
    }

    if (jobs.length === 0) {
        return (
            <div className="text-center py-6 px-4">
                <p className="text-xs text-ink-muted">No aggregation history found.</p>
                <p className="text-[10px] text-ink-muted/70 mt-1">Jobs will appear here once aggregation is triggered.</p>
            </div>
        )
    }

    return (
        <div className="space-y-4">
            <h4 className="text-xs font-bold text-ink-muted uppercase tracking-wider">Aggregation History</h4>
            <div className="space-y-3">
                {jobs.map(job => (
                    <JobCard key={job.id} job={job} />
                ))}
            </div>
        </div>
    )
}

const JobCard = memo(function JobCard({ job }: { job: AggregationJobResponse }) {
    const progressPercent = Math.round(job.progress || 0)
    
    // Status visual mapping
    let icon, statusColor, bgColor, statusText;
    switch (job.status) {
        case 'completed':
            icon = <CheckCircle2 className="w-4 h-4 text-emerald-500" />
            statusColor = 'text-emerald-600 dark:text-emerald-400'
            bgColor = 'bg-emerald-500/10 border-emerald-500/20'
            statusText = 'Completed'
            break;
        case 'failed':
            icon = <AlertCircle className="w-4 h-4 text-red-500" />
            statusColor = 'text-red-600 dark:text-red-400'
            bgColor = 'bg-red-500/10 border-red-500/20'
            statusText = 'Failed'
            break;
        case 'running':
            icon = <Loader2 className="w-4 h-4 text-indigo-500 animate-spin" />
            statusColor = 'text-indigo-600 dark:text-indigo-400'
            bgColor = 'bg-indigo-500/10 border-indigo-500/20'
            statusText = 'Running'
            break;
        case 'pending':
            icon = <Clock className="w-4 h-4 text-amber-500" />
            statusColor = 'text-amber-600 dark:text-amber-400'
            bgColor = 'bg-amber-500/10 border-amber-500/20'
            statusText = 'Pending'
            break;
        default:
            icon = <PlayCircle className="w-4 h-4 text-ink-muted" />
            statusColor = 'text-ink-secondary'
            bgColor = 'bg-glass-panel border-glass-border'
            statusText = job.status.charAt(0).toUpperCase() + job.status.slice(1)
    }

    return (
        <div className={cn("p-3 rounded-xl border flex flex-col gap-2 transition-colors", bgColor)}>
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    {icon}
                    <span className={cn("text-xs font-semibold", statusColor)}>
                        {statusText}
                    </span>
                    <span className="text-[10px] text-ink-muted/80 font-mono hidden sm:inline-block ml-1">
                        {job.id.slice(-8)}
                    </span>
                </div>
                <div className="text-[10px] text-ink-muted text-right">
                    {new Date(job.createdAt).toLocaleString()}
                </div>
            </div>

            {/* Running exact Progress Tracking */}
            {job.status === 'running' && (
                <div className="mt-1 flex items-center justify-between gap-3">
                    <div className="w-full h-1.5 bg-black/10 dark:bg-white/10 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-indigo-500 rounded-full transition-all duration-500"
                            style={{ width: `${Math.min(100, progressPercent)}%` }}
                        />
                    </div>
                    <span className="text-[10px] font-bold text-indigo-500 w-8 text-right">
                        {progressPercent}%
                    </span>
                </div>
            )}
            
            {/* Extended Status Metadata */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-1">
                {job.totalEdges > 0 && (
                    <span className="text-[10px] text-ink-muted">
                        <strong className="text-ink-secondary font-medium">Edges:</strong> {job.processedEdges} / {job.totalEdges}
                    </span>
                )}
                {job.errorMessage && (
                    <span className="text-[10px] text-red-500 break-words w-full mt-1 font-mono">
                        Error: {job.errorMessage}
                    </span>
                )}
            </div>
        </div>
    )
})
