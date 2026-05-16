/**
 * AdminOverview — The global health dashboard.
 * Merges the navigational landing page with system-wide Insights.
 */
import { useState, useEffect, useCallback } from 'react'
import { fetchEnveloped } from '@/services/cacheEnvelope'
import { useNavigate } from 'react-router-dom'
import {
    CircleDot, ArrowRightLeft, Database, Layers, Server,
    Loader2, Activity, Plus, Shield
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { workspaceService, type WorkspaceResponse } from '@/services/workspaceService'
import { providerService } from '@/services/providerService'

function compactNum(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(0)}k`
    return String(n)
}

const KPI_CONFIG = [
    { key: 'nodes', label: 'Total Nodes', icon: CircleDot, gradient: 'from-indigo-500/20 to-indigo-500/0', accent: 'text-indigo-600 dark:text-indigo-400', iconBg: 'bg-indigo-500/10 text-indigo-500 border-indigo-500/20' },
    { key: 'edges', label: 'Total Edges', icon: ArrowRightLeft, gradient: 'from-violet-500/20 to-violet-500/0', accent: 'text-violet-600 dark:text-violet-400', iconBg: 'bg-violet-500/10 text-violet-500 border-violet-500/20' },
    { key: 'sources', label: 'Data Sources', icon: Database, gradient: 'from-emerald-500/20 to-emerald-500/0', accent: 'text-emerald-600 dark:text-emerald-400', iconBg: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20' },
    { key: 'types', label: 'Entity Types', icon: Layers, gradient: 'from-amber-500/20 to-amber-500/0', accent: 'text-amber-600 dark:text-amber-400', iconBg: 'bg-amber-500/10 text-amber-500 border-amber-500/20' },
]

interface WsInsight {
    ws: WorkspaceResponse
    nodes: number
    edges: number
    sources: number
    types: Set<string>
}

export function AdminOverview() {
    const navigate = useNavigate()
    const [insights, setInsights] = useState<WsInsight[]>([])
    const [providerCount, setProviderCount] = useState(0)
    const [isLoading, setIsLoading] = useState(true)

    const loadInsights = useCallback(async () => {
        setIsLoading(true)
        try {
            const [workspaces, providers] = await Promise.all([
                workspaceService.list(),
                providerService.list(),
            ])
            setProviderCount(providers.length)

            // Parallel fan-out over (workspace, datasource) pairs. The
            // previous nested ``for ... of`` issued requests one at a
            // time, so a page with N workspaces × M datasources took
            // N*M serialised round-trips (and was visibly the worst
            // offender behind the "lots of cached-stats requests"
            // symptom). ``fetchEnveloped`` retains its per-(ws, ds)
            // circuit breaker so a slow datasource still fails-fast
            // without dragging down the rest.
            type DsStats = {
                wsId: string
                nodes: number
                edges: number
                types: string[]
            }
            const tasks: Promise<DsStats | null>[] = []
            for (const ws of workspaces) {
                for (const ds of ws.dataSources || []) {
                    tasks.push((async () => {
                        const data = await fetchEnveloped<{
                            nodeCount?: number
                            edgeCount?: number
                            entityTypeCounts?: Record<string, number>
                        }>(
                            `/api/v1/admin/workspaces/${ws.id}/datasources/${ds.id}/cached-stats`,
                            { circuitScope: { workspaceId: ws.id, dataSourceId: ds.id } },
                        )
                        if (!data) return null
                        return {
                            wsId: ws.id,
                            nodes: data.nodeCount ?? 0,
                            edges: data.edgeCount ?? 0,
                            types: Object.keys(data.entityTypeCounts ?? {}),
                        }
                    })())
                }
            }
            const settled = await Promise.allSettled(tasks)

            // Reduce flat results back into per-workspace aggregates.
            const byWs = new Map<string, { nodes: number; edges: number; types: Set<string> }>()
            for (const r of settled) {
                if (r.status !== 'fulfilled' || r.value === null) continue
                const { wsId, nodes, edges, types } = r.value
                const agg = byWs.get(wsId) ?? { nodes: 0, edges: 0, types: new Set<string>() }
                agg.nodes += nodes
                agg.edges += edges
                for (const t of types) agg.types.add(t)
                byWs.set(wsId, agg)
            }

            const results: WsInsight[] = workspaces.map(ws => {
                const agg = byWs.get(ws.id) ?? { nodes: 0, edges: 0, types: new Set<string>() }
                return {
                    ws,
                    nodes: agg.nodes,
                    edges: agg.edges,
                    sources: ws.dataSources?.length || 0,
                    types: agg.types,
                }
            })
            setInsights(results)
        } catch (err) {
            console.error('Failed to load insights', err)
        } finally {
            setIsLoading(false)
        }
    }, [])

    useEffect(() => { loadInsights() }, [loadInsights])

    // Aggregate totals
    const totals = insights.reduce((acc, i) => ({
        nodes: acc.nodes + i.nodes,
        edges: acc.edges + i.edges,
        sources: acc.sources + i.sources,
        types: new Set([...acc.types, ...i.types]),
    }), { nodes: 0, edges: 0, sources: 0, types: new Set<string>() })

    const kpiValues: Record<string, number> = {
        nodes: totals.nodes,
        edges: totals.edges,
        sources: totals.sources,
        types: totals.types.size,
    }

    if (isLoading) {
        return (
            <div className="flex items-center justify-center h-full"><Loader2 className="w-6 h-6 animate-spin text-ink-muted" /></div>
        )
    }

    return (
        <div className="max-w-6xl mx-auto p-8 animate-in fade-in duration-500">
            {/* Header */}
            <div className="flex items-center justify-between mb-10">
                <div className="flex items-center gap-3">
                    <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-lg shadow-indigo-500/20">
                        <Shield className="w-6 h-6 text-white" />
                    </div>
                    <div>
                        <h1 className="text-3xl font-bold tracking-tight text-ink">Global Overview</h1>
                        <p className="text-sm text-ink-muted mt-1">
                            System health, graph scale, and cross-workspace analytics.
                        </p>
                    </div>
                </div>

                <div className="flex gap-3">
                    <button
                        onClick={() => navigate('/ingestion?tab=providers')}
                        className="px-4 py-2 border border-glass-border bg-canvas-elevated hover:bg-black/5 dark:hover:bg-white/5 rounded-xl font-medium text-sm text-ink transition-colors flex items-center gap-2"
                    >
                        <Server className="w-4 h-4" /> Register Connection
                    </button>
                    <button
                        onClick={() => navigate('/workspaces')}
                        className="px-4 py-2 bg-indigo-500 hover:bg-indigo-600 text-white rounded-xl font-medium text-sm transition-colors flex items-center gap-2"
                    >
                        <Plus className="w-4 h-4" /> Create Workspace
                    </button>
                </div>
            </div>

            {/* KPI Cards */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
                {KPI_CONFIG.map(kpi => {
                    const Icon = kpi.icon
                    return (
                        <div key={kpi.key} className={cn(
                            "relative overflow-hidden border border-glass-border rounded-xl p-5 bg-canvas-elevated",
                            "hover:shadow-lg transition-all duration-200"
                        )}>
                            <div className={cn("absolute inset-0 bg-gradient-to-br", kpi.gradient)} />
                            <div className="relative">
                                <div className={cn("w-9 h-9 rounded-lg border flex items-center justify-center mb-3", kpi.iconBg)}>
                                    <Icon className="w-4.5 h-4.5" />
                                </div>
                                <p className={cn("text-2xl font-bold", kpi.accent)}>
                                    {compactNum(kpiValues[kpi.key] || 0)}
                                </p>
                                <p className="text-xs text-ink-muted mt-1">{kpi.label}</p>
                            </div>
                        </div>
                    )
                })}
            </div>

            {/* System Breakdown */}
            <div className="flex items-center gap-4 mb-6">
                <div className="flex items-center gap-2 px-4 py-2 rounded-xl bg-black/5 dark:bg-white/5 border border-glass-border">
                    <Server className="w-4 h-4 text-ink-muted" />
                    <span className="text-sm text-ink"><span className="font-bold">{providerCount}</span> Physical Connections</span>
                </div>
                <div className="flex items-center gap-2 px-4 py-2 rounded-xl bg-black/5 dark:bg-white/5 border border-glass-border">
                    <Activity className="w-4 h-4 text-ink-muted" />
                    <span className="text-sm text-ink"><span className="font-bold">{insights.length}</span> Isolated Workspaces</span>
                </div>
            </div>

            {/* Per-workspace breakdown table */}
            {insights.length > 0 && (
                <div className="border border-glass-border rounded-xl bg-canvas-elevated overflow-hidden shadow-sm">
                    <table className="w-full">
                        <thead className="bg-black/5 dark:bg-white/5">
                            <tr className="border-b border-glass-border">
                                <th className="text-left text-xs font-semibold text-ink-muted uppercase tracking-wider px-5 py-3">Workspace</th>
                                <th className="text-right text-xs font-semibold text-ink-muted uppercase tracking-wider px-5 py-3">Sources</th>
                                <th className="text-right text-xs font-semibold text-ink-muted uppercase tracking-wider px-5 py-3">Nodes</th>
                                <th className="text-right text-xs font-semibold text-ink-muted uppercase tracking-wider px-5 py-3">Edges</th>
                                <th className="text-right text-xs font-semibold text-ink-muted uppercase tracking-wider px-5 py-3">Entity Types</th>
                            </tr>
                        </thead>
                        <tbody>
                            {insights.sort((a, b) => b.nodes - a.nodes).map((insight, i) => (
                                <tr key={insight.ws.id} onClick={() => navigate(`/workspaces/${insight.ws.id}`)} className={cn("border-b last:border-b-0 border-glass-border hover:bg-black/[0.05] dark:hover:bg-white/[0.05] cursor-pointer transition-colors", i % 2 === 0 && "bg-black/[0.01] dark:bg-white/[0.01]")}>
                                    <td className="px-5 py-3">
                                        <div className="flex items-center gap-2">
                                            <span className="text-sm font-semibold text-ink">{insight.ws.name}</span>
                                            {insight.ws.isDefault && (
                                                <span className="px-1.5 py-0.5 text-[9px] font-bold rounded bg-indigo-500/10 text-indigo-500">DEFAULT</span>
                                            )}
                                        </div>
                                    </td>
                                    <td className="px-5 py-3 text-right text-sm text-ink-secondary font-medium">{insight.sources}</td>
                                    <td className="px-5 py-3 text-right">
                                        <span className="text-sm font-bold text-indigo-500">{compactNum(insight.nodes)}</span>
                                    </td>
                                    <td className="px-5 py-3 text-right">
                                        <span className="text-sm font-bold text-violet-500">{compactNum(insight.edges)}</span>
                                    </td>
                                    <td className="px-5 py-3 text-right text-sm text-ink-secondary font-medium">{insight.types.size}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>

                    {/* Entity type aggregate */}
                    <div className="px-5 py-4 border-t border-glass-border bg-canvas-elevated">
                        <h4 className="text-xs font-semibold text-ink-muted uppercase tracking-wider mb-3 flex items-center gap-2"><Layers className="w-3.5 h-3.5 text-amber-500" /> Enterprise Data Model</h4>
                        <div className="flex flex-wrap gap-1.5">
                            {[...totals.types].sort().map(type => (
                                <span key={type} className="px-2.5 py-1 text-[11px] font-medium rounded-full bg-amber-500/5 text-amber-600 dark:text-amber-400 border border-amber-500/20">
                                    {type}
                                </span>
                            ))}
                            {totals.types.size === 0 && <span className="text-xs text-ink-muted">No entity types discovered</span>}
                        </div>
                    </div>
                </div>
            )}
        </div>
    )
}
