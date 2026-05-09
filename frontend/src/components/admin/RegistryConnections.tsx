import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
    Server, Plus, RefreshCw, Wifi, WifiOff, Edit2, Trash2, Zap,
    Shield, Globe, ChevronDown, ChevronUp, Loader2, AlertTriangle, Sparkles
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { providerService, type ConnectionTestResult, type ProviderImpactResponse, type ProviderResponse } from '@/services/providerService'
import { ProviderAdmissionEditor } from '@/components/insights/ProviderAdmissionEditor'
import { StatusChip } from '@/components/insights/StatusChip'
import type { InsightsMeta, ProviderHealth as InsightsProviderHealth } from '@/types/insights'
import { useProviderHealthSweep } from '@/hooks/useProviderHealthSweep'
import { useProviderStatusStore } from '@/store/providerStatus'
import { DeleteProviderDialog } from './DeleteProviderDialog'
import { FirstRunHero } from './FirstRunHero'
import { ProviderOnboardingWizard } from './ProviderOnboardingWizard'
import { Neo4jLogo, FalkorDBLogo, DataHubLogo, SpannerLogo } from './ProviderLogos'

const PROVIDER_TYPES = [
    { type: 'falkordb' as const, label: 'FalkorDB', Logo: FalkorDBLogo, color: 'text-amber-500 bg-amber-500/10 border-amber-500/20', desc: 'High-performance graph database' },
    { type: 'neo4j' as const, label: 'Neo4j', Logo: Neo4jLogo, color: 'text-blue-500 bg-blue-500/10 border-blue-500/20', desc: 'The original graph database' },
    { type: 'datahub' as const, label: 'DataHub', Logo: DataHubLogo, color: 'text-emerald-500 bg-emerald-500/10 border-emerald-500/20', desc: 'LinkedIn metadata platform' },
    { type: 'spanner' as const, label: 'Google Spanner Graph', Logo: SpannerLogo, color: 'text-sky-500 bg-sky-500/10 border-sky-500/20', desc: 'Globally-distributed property graph on Spanner' },
]

function getProviderConfig(type: string) {
    return PROVIDER_TYPES.find(p => p.type === type) || PROVIDER_TYPES[0]
}

type HealthStatus = 'checking' | 'healthy' | 'unhealthy' | 'unknown'
interface ProviderHealth { status: HealthStatus; latencyMs?: number; error?: string }

/**
 * Synthesise a universal insights envelope `meta` from the connectivity
 * sweep so we can render a single <StatusChip> that matches the look of
 * RegistryAssets / ViewWizard banners. The sweep doesn't have a real
 * cache row, so envelope status is `unavailable` when the provider is
 * down (no fresh data to show) and `fresh` when reachable; the
 * provider_health field carries the sweep's actual verdict.
 */
function syntheticMetaFromSweep(
    health: ProviderHealth, providerId: string,
): InsightsMeta {
    let providerHealth: InsightsProviderHealth = 'unknown'
    // Default for the "we don't know yet" / "awaiting backend warmup"
    // state is ``computing`` — NOT ``unavailable``. ``unavailable`` maps
    // to a yellow "Paused" badge in StatusChip and gives the impression
    // every provider is broken on a fresh page load before the backend
    // warmup loop has had a chance to publish a result.
    let status: InsightsMeta['status'] = 'computing'
    if (health.status === 'healthy') {
        providerHealth = 'ok'
        status = 'fresh'
    } else if (health.status === 'unhealthy') {
        providerHealth = 'down'
        status = 'unavailable'
    } else if (health.status === 'checking') {
        providerHealth = 'unknown'
        status = 'computing'
    }
    return {
        status,
        source: health.status === 'healthy' ? 'cache' : 'none',
        updated_at: null,
        staleness_secs: null,
        ttl_seconds: null,
        refreshing: health.status === 'checking' || health.status === 'unknown',
        job_id: null,
        poll_url: null,
        provider_health: providerHealth,
        last_error: health.error ?? null,
        provider_id: providerId,
    }
}

function ConnectionCard({ provider, health, onTest, onEdit, onDelete, onScan }: { provider: ProviderResponse; health: ProviderHealth; onTest: () => void; onEdit: () => void; onDelete: () => void; onScan: () => void }) {
    const config = getProviderConfig(provider.providerType)
    const [expanded, setExpanded] = useState(false)
    const statusDot = { checking: 'bg-amber-400 animate-pulse', healthy: 'bg-emerald-400', unhealthy: 'bg-red-400', unknown: 'bg-gray-400' }[health.status]

    return (
        <div className={cn("group border border-glass-border rounded-xl bg-canvas-elevated hover:shadow-lg transition-colors duration-150 duration-200", health.status === 'healthy' && "hover:border-emerald-500/30", health.status === 'unhealthy' && "border-red-500/20")}>
            <div className="p-5">
                <div className="flex items-start justify-between mb-4">
                    <div className="flex items-center gap-3">
                        <div className={cn("w-10 h-10 rounded-xl border flex items-center justify-center", config.color)}><config.Logo className="w-5 h-5" /></div>
                        <div>
                            <h3 className="text-sm font-bold text-ink">{provider.name}</h3>
                            <p className="text-xs text-ink-muted">{config.label}</p>
                        </div>
                    </div>
                    <div className="flex items-center gap-2">
                        <div className={cn("w-2.5 h-2.5 rounded-full", statusDot)} title={health.status} />
                        {health.latencyMs !== undefined && <span className="text-[10px] font-mono text-ink-muted">{Math.round(health.latencyMs)}ms</span>}
                        <StatusChip meta={syntheticMetaFromSweep(health, provider.id)} compact />
                    </div>
                </div>
                <div className="flex items-center gap-4 text-xs text-ink-muted mb-4">
                    {provider.host && <div className="flex items-center gap-1.5"><Globe className="w-3 h-3" /><span className="font-mono">{provider.host}:{provider.port || '—'}</span></div>}
                    {provider.tlsEnabled && <div className="flex items-center gap-1 text-emerald-500"><Shield className="w-3 h-3" /><span>TLS</span></div>}
                </div>
                {health.status === 'unhealthy' && health.error && (
                    <div className="mb-4 p-3 rounded-xl bg-red-500/5 border border-red-500/15 text-sm text-red-600 dark:text-red-400 leading-relaxed">
                        <div className="flex items-start gap-2">
                            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                            <span>{health.error}</span>
                        </div>
                    </div>
                )}
                <div className="flex items-center gap-2">
                    <button onClick={onTest} disabled={health.status === 'checking'} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-black/5 dark:bg-white/5 hover:bg-black/10 dark:hover:bg-white/10 text-ink-secondary hover:text-ink transition-colors disabled:opacity-50">
                        {health.status === 'checking' ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />} Test
                    </button>
                    <button onClick={onScan} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-600 dark:text-indigo-400 transition-colors"><RefreshCw className="w-3 h-3" /> Discover Sources</button>
                    <button onClick={onEdit} className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-black/5 dark:bg-white/5 hover:bg-black/10 dark:hover:bg-white/10 text-ink-secondary hover:text-ink transition-colors"><Edit2 className="w-3 h-3" /> Edit</button>
                    <button
                        onClick={onDelete}
                        aria-label={`Delete provider ${provider.name}`}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg text-red-500 hover:bg-red-500/10 transition-colors ml-auto"
                    >
                        <Trash2 className="w-3 h-3" />
                    </button>
                    <button onClick={() => setExpanded(!expanded)} className="p-1.5 rounded-lg hover:bg-black/5 dark:hover:bg-white/5 text-ink-muted transition-colors">
                        {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                    </button>
                </div>
            </div>
            {expanded && (
                <div className="px-5 pb-5 pt-3 border-t border-glass-border animate-in slide-in-from-top-2 fade-in duration-200 space-y-4">
                    <dl className="grid grid-cols-2 gap-3 text-xs">
                        <div><dt className="text-ink-muted font-medium">Provider ID</dt><dd className="font-mono text-ink mt-0.5 truncate">{provider.id}</dd></div>
                        <div><dt className="text-ink-muted font-medium">Status</dt><dd className={cn("mt-0.5 font-semibold", provider.isActive ? "text-emerald-500" : "text-red-500")}>{provider.isActive ? 'Active' : 'Inactive'}</dd></div>
                        <div><dt className="text-ink-muted font-medium">Created</dt><dd className="text-ink mt-0.5">{new Date(provider.createdAt).toLocaleDateString()}</dd></div>
                        <div><dt className="text-ink-muted font-medium">Last Updated</dt><dd className="text-ink mt-0.5">{new Date(provider.updatedAt).toLocaleDateString()}</dd></div>
                        {health.error && <div className="col-span-2"><dt className="text-red-500 font-medium">Error</dt><dd className="text-red-400 mt-0.5 font-mono text-[11px] break-all">{health.error}</dd></div>}
                    </dl>
                    <ProviderAdmissionEditor providerId={provider.id} />
                </div>
            )}
        </div>
    )
}

export function RegistryConnections() {
    const navigate = useNavigate()
    const [providers, setProviders] = useState<ProviderResponse[]>([])
    const { healthMap, testOne, refresh: refreshHealth, setHealth } = useProviderHealthSweep(providers)
    // Backend-published per-provider status — populated by the global
    // ``providerStatus`` poll of /admin/providers/status (cache-only,
    // backed by the warmup loop). Used as the FALLBACK status when the
    // user has not personally clicked Test on a provider yet, so the
    // initial render shows the truth observed by the platform instead
    // of a stale "Paused" placeholder.
    const backendStatuses = useProviderStatusStore(s => s.statuses)
    const [isLoading, setIsLoading] = useState(true)
    const [showWizard, setShowWizard] = useState(false)
    const [editingProvider, setEditingProvider] = useState<ProviderResponse | null>(null)
    const [deleteTarget, setDeleteTarget] = useState<ProviderResponse | null>(null)
    const [deleteImpact, setDeleteImpact] = useState<ProviderImpactResponse | null>(null)
    const [loadingImpact, setLoadingImpact] = useState(false)

    const loadProviders = useCallback(async () => {
        setIsLoading(true)
        try {
            const data = await providerService.list()
            setProviders(data)
        } catch (err) { console.error('Failed to load providers', err) }
        finally { setIsLoading(false) }
    }, [])

    useEffect(() => { loadProviders() }, [loadProviders])

    const handleDeleteClick = async (p: ProviderResponse) => {
        setDeleteTarget(p)
        setLoadingImpact(true)
        try {
            const impact = await providerService.getImpact(p.id)
            setDeleteImpact(impact)
        } catch (err) {
            console.error('Failed to load impact', err)
        } finally {
            setLoadingImpact(false)
        }
    }

    const deleteProvider = async () => {
        if (!deleteTarget) return
        await providerService.delete(deleteTarget.id)
        await loadProviders()
    }

    const handleEditProvider = (p: ProviderResponse) => {
        setEditingProvider(p)
        setShowWizard(true)
    }

    const handleProviderCreated = async (
        createdProvider: ProviderResponse,
        health: ConnectionTestResult,
    ) => {
        await loadProviders()
        setHealth(createdProvider.id, {
            status: health.success ? 'healthy' : 'unhealthy',
            latencyMs: health.latencyMs,
            error: health.error,
        })
    }

    const handleProviderUpdated = async () => {
        await loadProviders()
    }

    // Counts merge backend-published status with the local user-gesture
    // sweep so the header reflects the truth even on cold load (before
    // any user has clicked Test). Local takes precedence when present.
    const healthyCount = providers.filter(p => {
        const local = healthMap[p.id]
        if (local) return local.status === 'healthy'
        return backendStatuses[p.id]?.status === 'ready'
    }).length
    const unhealthyCount = providers.filter(p => {
        const local = healthMap[p.id]
        if (local) return local.status === 'unhealthy'
        return backendStatuses[p.id]?.status === 'unavailable'
    }).length

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            {/* Header / Actions */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-xl font-bold text-ink">Providers</h2>
                    <p className="text-sm text-ink-muted mt-1">Manage database providers and catalog availability.</p>
                </div>
                <button onClick={() => { setEditingProvider(null); setShowWizard(true) }} className="flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-500 hover:bg-indigo-600 text-white text-sm font-semibold transition-colors">
                    <Plus className="w-4 h-4" /> Register Provider
                </button>
            </div>

            {/* Health Summary */}
            {providers.length > 0 && (
                <div className="flex items-center gap-4">
                    <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 text-xs font-medium">
                        <Wifi className="w-3 h-3" /> {healthyCount} Connected
                    </div>
                    {unhealthyCount > 0 && (
                        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-red-500/10 text-red-500 text-xs font-medium">
                            <WifiOff className="w-3 h-3" /> {unhealthyCount} Disconnected
                        </div>
                    )}
                    <button onClick={() => { void refreshHealth() }} className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium text-ink-muted hover:text-ink transition-colors ml-auto">
                        <RefreshCw className="w-3 h-3" /> Re-test All
                    </button>
                </div>
            )}

            {/* Grid */}
            {isLoading ? (
                <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin text-ink-muted" /></div>
            ) : providers.length === 0 ? (
                <div className="space-y-6">
                    <FirstRunHero embedded />

                    <div className="overflow-hidden rounded-3xl border border-glass-border bg-gradient-to-br from-slate-50 via-white to-indigo-50/60 dark:from-slate-900 dark:via-slate-900 dark:to-indigo-950/20">
                        <div className="grid gap-0 md:grid-cols-[1.3fr,0.9fr]">
                            <div className="p-8 md:p-10">
                                <div className="mb-4 inline-flex items-center gap-2 rounded-full bg-indigo-500/10 px-4 py-2 text-sm font-medium text-indigo-600 dark:text-indigo-400">
                                    <Sparkles className="h-4 w-4" />
                                    Provider onboarding
                                </div>
                                <h3 className="text-2xl font-bold text-ink">Connect your first provider</h3>
                                <p className="mt-3 max-w-xl text-sm leading-relaxed text-ink-muted">
                                    Providers are the infrastructure layer behind data source onboarding. Once a provider is connected,
                                    you can discover assets, scope them into workspaces, and keep outages isolated to only the places
                                    that use that connection.
                                </p>
                                <div className="mt-6 flex flex-wrap items-center gap-3">
                                    <button
                                        onClick={() => {
                                            setEditingProvider(null)
                                            setShowWizard(true)
                                        }}
                                        className="flex items-center gap-2 rounded-xl bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-600"
                                    >
                                        <Plus className="h-4 w-4" />
                                        Start provider onboarding
                                    </button>
                                    <span className="text-xs text-ink-muted">You can test the connection before moving to data sources.</span>
                                </div>
                            </div>

                            <div className="border-t border-glass-border bg-black/[0.02] p-8 dark:bg-white/[0.02] md:border-l md:border-t-0">
                                <div className="space-y-4">
                                    <div className="flex items-start gap-3">
                                        <div className="mt-0.5 flex h-8 w-8 items-center justify-center rounded-xl bg-indigo-500/10 text-indigo-500">
                                            <Server className="h-4 w-4" />
                                        </div>
                                        <div>
                                            <p className="text-sm font-semibold text-ink">1. Register infrastructure</p>
                                            <p className="mt-1 text-sm text-ink-muted">Choose FalkorDB, Neo4j, or DataHub and add connection details.</p>
                                        </div>
                                    </div>
                                    <div className="flex items-start gap-3">
                                        <div className="mt-0.5 flex h-8 w-8 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-500">
                                            <Zap className="h-4 w-4" />
                                        </div>
                                        <div>
                                            <p className="text-sm font-semibold text-ink">2. Validate connectivity</p>
                                            <p className="mt-1 text-sm text-ink-muted">Synodic checks the provider before you continue into asset discovery.</p>
                                        </div>
                                    </div>
                                    <div className="flex items-start gap-3">
                                        <div className="mt-0.5 flex h-8 w-8 items-center justify-center rounded-xl bg-violet-500/10 text-violet-500">
                                            <Globe className="h-4 w-4" />
                                        </div>
                                        <div>
                                            <p className="text-sm font-semibold text-ink">3. Discover sources</p>
                                            <p className="mt-1 text-sm text-ink-muted">Move straight into asset onboarding with the new provider selected.</p>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {providers.map(p => {
                        // Resolution order:
                        //   1. Local user-gesture sweep (Test button result) — freshest truth.
                        //   2. Backend-published warmup status from /admin/providers/status.
                        //   3. Fallback: 'unknown' (waiting for backend warmup cycle).
                        const local = healthMap[p.id]
                        const backend = backendStatuses[p.id]
                        let resolved: ProviderHealth
                        if (local) {
                            resolved = local
                        } else if (backend) {
                            const status: HealthStatus =
                                backend.status === 'ready' ? 'healthy'
                                : backend.status === 'unavailable' ? 'unhealthy'
                                : 'unknown'
                            resolved = { status, error: backend.error }
                        } else {
                            resolved = { status: 'unknown' }
                        }
                        return (
                            <ConnectionCard
                                key={p.id}
                                provider={p}
                                health={resolved}
                                onTest={() => { void testOne(p.id) }}
                                onEdit={() => handleEditProvider(p)}
                                onDelete={() => handleDeleteClick(p)}
                                onScan={() => navigate(`/ingestion?tab=assets&provider=${p.id}`)}
                            />
                        )
                    })}
                </div>
            )}

            <ProviderOnboardingWizard
                isOpen={showWizard}
                mode={editingProvider ? 'edit' : 'create'}
                provider={editingProvider}
                providers={providers}
                onClose={() => {
                    setShowWizard(false)
                    setEditingProvider(null)
                }}
                onCreated={handleProviderCreated}
                onUpdated={handleProviderUpdated}
            />

            <DeleteProviderDialog
                provider={deleteTarget}
                impact={deleteImpact}
                loadingImpact={loadingImpact}
                isOpen={!!deleteTarget}
                onClose={() => { setDeleteTarget(null); setDeleteImpact(null) }}
                onConfirm={deleteProvider}
            />

        </div>
    )
}

