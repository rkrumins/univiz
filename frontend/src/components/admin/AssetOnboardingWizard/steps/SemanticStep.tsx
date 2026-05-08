/**
 * SemanticStep — Per-source ontology assignment with rich recommendation flow.
 *
 * Each data source gets an inline 3-phase analysis flow (mirroring SuggestConfirmDialog):
 * 1. Initial — "Analyze Graph" button with explanation
 * 2. Analyzing — spinner while scanning schema
 * 3. Recommendations — sorted match cards with CoverageRing, MiniBar, badges
 *
 * Also supports: "Analyze All" bulk action, "Apply best to all" global action,
 * "Skip for now" per source, and "Create from Physical Graph" new draft flow.
 */
import { memo, useState, useEffect, useCallback, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
    BookOpen, Sparkles, Check, X, Loader2, ChevronDown, Search, Zap,
    Box, GitBranch, Shield, CheckCircle2, PenLine, Database, Info,
    Clock, AlertTriangle,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { ontologyDefinitionService } from '@/services/ontologyDefinitionService'
import type {
    OntologyDefinitionResponse,
    OntologyMatchResult,
    OntologySuggestResponse,
} from '@/services/ontologyDefinitionService'
import { providerService } from '@/services/providerService'
import {
    ontologyResolutionService,
    type OntologyResolutionResponse,
} from '@/services/ontologyResolutionService'
import type { CatalogItemResponse } from '@/services/catalogService'
import type { OnboardingFormData } from '../AssetOnboardingWizard'
import { useToast } from '@/components/ui/toast'
import { CoverageRing, MiniBar, coverageColor, coverageBarClass } from './CoverageVisuals'

// ─── Types ───────────────────────────────────────────────────────────────────

interface SemanticStepProps {
    formData: OnboardingFormData
    updateFormData: (updates: Partial<OnboardingFormData> | ((prev: OnboardingFormData) => Partial<OnboardingFormData>)) => void
    catalogItems: CatalogItemResponse[]
    providerId: string
    /** Map of workspace ID → workspace name for display */
    workspaceNames?: Record<string, string>
    /** Called when ontologies are loaded — parent uses this to build name maps for ReviewStep */
    onOntologiesLoaded?: (nameMap: Record<string, string>) => void
}

type SourcePhase = 'initial' | 'analyzing' | 'recommendations'

interface SourceState {
    phase: SourcePhase
    matches: OntologyMatchResult[]
    graphCounts: { entities: number; rels: number }
    suggestResponse: OntologySuggestResponse | null
    selectedId: string | null // ontology ID, '__create_from_graph__', or null
    draftName: string
    error: string | null
    search: string
    skipped: boolean
    isCreatingDraft: boolean
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Convert raw provider stats → format expected by suggest endpoint */
function transformStatsForSuggest(raw: any): Record<string, unknown> {
    return {
        totalNodes: raw.nodeCount ?? 0,
        totalEdges: raw.edgeCount ?? 0,
        entityTypeStats: Object.entries(raw.entityTypeCounts ?? {}).map(
            ([name, count]) => ({ id: name, name, count: count as number, sampleNames: [] })
        ),
        edgeTypeStats: Object.entries(raw.edgeTypeCounts ?? {}).map(
            ([name, count]) => ({ id: name, name, count: count as number, sourceTypes: [], targetTypes: [] })
        ),
        tagStats: [],
    }
}

function getInitialSourceState(): SourceState {
    return {
        phase: 'initial',
        matches: [],
        graphCounts: { entities: 0, rels: 0 },
        suggestResponse: null,
        selectedId: null,
        draftName: 'Graph Schema',
        error: null,
        search: '',
        skipped: false,
        isCreatingDraft: false,
    }
}

function getConfidence(match: OntologyMatchResult | undefined): {
    level: 'high' | 'medium' | 'low' | 'none'
    pct: number
    label: string
    color: string
    bgColor: string
    borderColor: string
} {
    if (!match) return { level: 'none', pct: 0, label: 'No Match', color: 'text-red-400', bgColor: 'bg-red-500/10', borderColor: 'border-red-500/20' }
    const entityTotal = match.coveredEntityTypes.length + match.uncoveredEntityTypes.length
    const relTotal = match.coveredRelationshipTypes.length + match.uncoveredRelationshipTypes.length
    const totalAll = entityTotal + relTotal
    const totalCovered = match.coveredEntityTypes.length + match.coveredRelationshipTypes.length
    const pct = totalAll > 0 ? Math.round((totalCovered / totalAll) * 100) : 0

    if (pct > 80) return { level: 'high', pct, label: `${pct}%`, color: 'text-emerald-500', bgColor: 'bg-emerald-500/10', borderColor: 'border-emerald-500/20' }
    if (pct >= 50) return { level: 'medium', pct, label: `${pct}%`, color: 'text-amber-500', bgColor: 'bg-amber-500/10', borderColor: 'border-amber-500/20' }
    return { level: 'low', pct, label: `${pct}%`, color: 'text-red-400', bgColor: 'bg-red-500/10', borderColor: 'border-red-500/20' }
}

// ─── Component ───────────────────────────────────────────────────────────────

export function SemanticStep({
    formData,
    updateFormData,
    catalogItems,
    providerId,
    workspaceNames = {},
    onOntologiesLoaded,
}: SemanticStepProps) {
    const { showToast } = useToast()
    const [ontologies, setOntologies] = useState<OntologyDefinitionResponse[]>([])
    const [loadingOntologies, setLoadingOntologies] = useState(true)
    const [sourceStates, setSourceStates] = useState<Record<string, SourceState>>(() =>
        Object.fromEntries(catalogItems.map(c => [c.id, getInitialSourceState()]))
    )
    const [expandedItems, setExpandedItems] = useState<Set<string>>(() => new Set(catalogItems.map(c => c.id)))
    const [isAnalyzingAll, setIsAnalyzingAll] = useState(false)
    const [analysisProgress, setAnalysisProgress] = useState<Record<string, 'pending' | 'analyzing' | 'done' | 'error'>>({})
    const [showBulkSummary, setShowBulkSummary] = useState(false)

    // Load ontologies on mount
    useEffect(() => {
        let cancelled = false
        setLoadingOntologies(true)
        ontologyDefinitionService.list().then(all => {
            if (cancelled) return
            setOntologies(all.sort((a, b) => (b.isPublished ? 1 : 0) - (a.isPublished ? 1 : 0)))
            setLoadingOntologies(false)
            // Report ontology names to parent for ReviewStep display
            if (onOntologiesLoaded) {
                const nameMap: Record<string, string> = {}
                for (const o of all) nameMap[o.id] = o.name
                onOntologiesLoaded(nameMap)
            }
        }).catch(() => {
            if (!cancelled) setLoadingOntologies(false)
        })
        return () => { cancelled = true }
    }, [])

    // Activate bulk summary when all sources are analyzed
    useEffect(() => {
        const allAnalyzed = catalogItems.every(c => {
            const s = sourceStates[c.id]
            return s.phase === 'recommendations' || s.skipped
        })
        const someAnalyzed = catalogItems.some(c => sourceStates[c.id]?.phase === 'recommendations')
        if (allAnalyzed && someAnalyzed) {
            setShowBulkSummary(true)
        }
    }, [sourceStates, catalogItems])

    const getOntology = useCallback((id: string) => ontologies.find(o => o.id === id), [ontologies])

    const updateSource = useCallback((itemId: string, updates: Partial<SourceState>) => {
        setSourceStates(prev => ({
            ...prev,
            [itemId]: { ...prev[itemId], ...updates },
        }))
    }, [])

    const updateOntologySelection = useCallback((itemId: string, ontologyId: string, coverageStats?: OntologyMatchResult | null) => {
        updateFormData(prev => ({
            ontologySelections: {
                ...prev.ontologySelections,
                [itemId]: {
                    ...prev.ontologySelections[itemId],
                    ontologyId,
                    coverageStats: coverageStats ?? prev.ontologySelections[itemId]?.coverageStats ?? null,
                },
            },
        }))
    }, [updateFormData])

    // ─── Analyze single source ──────────────────────────────────────────

    const analyzeSource = useCallback(async (item: CatalogItemResponse, options?: { bulk?: boolean }) => {
        const isBulk = options?.bulk === true
        updateSource(item.id, { phase: 'analyzing', error: null })
        setAnalysisProgress(prev => ({ ...prev, [item.id]: 'analyzing' }))

        try {
            const assetName = item.sourceIdentifier || item.name
            // getAssetStats returns the universal insights envelope. On a cold
            // cache (`computing` / `unavailable`) `data` is null; surface a
            // retriable error so the user can re-run analyze once the
            // background scan completes (typically within ~30s).
            const envelope = await providerService.getAssetStats(providerId, assetName)
            if (!envelope.data) {
                throw new Error(
                    envelope.meta.status === 'computing'
                        ? 'Asset metadata is still being computed in the background. Please retry analysis in a few seconds.'
                        : envelope.meta.status === 'unavailable'
                            ? 'Background refresh queue is unreachable. Please retry shortly.'
                            : 'No asset metadata available yet.'
                )
            }
            const rawStats = envelope.data
            const stats = transformStatsForSuggest(rawStats)
            const response = await ontologyDefinitionService.suggest(stats)

            const matches = response.matchingOntologies
            const entities = (stats.entityTypeStats as any[])?.length ?? 0
            const rels = (stats.edgeTypeStats as any[])?.length ?? 0

            // Sort: system first, then by Jaccard desc
            const sorted = [...matches].sort((a, b) => {
                const aSystem = getOntology(a.ontologyId)?.isSystem ? 1 : 0
                const bSystem = getOntology(b.ontologyId)?.isSystem ? 1 : 0
                if (aSystem !== bSystem) return bSystem - aSystem
                return b.jaccardScore - a.jaccardScore
            })

            // Auto-select best match
            const bestId = sorted.length > 0 ? sorted[0].ontologyId : null
            const bestMatch = bestId ? sorted.find(m => m.ontologyId === bestId) ?? null : null

            updateSource(item.id, {
                phase: 'recommendations',
                matches: sorted,
                graphCounts: { entities, rels },
                suggestResponse: response,
                selectedId: bestId,
                draftName: `${item.name} Schema`,
                error: null,
            })

            // Store in formData
            if (bestId) {
                updateOntologySelection(item.id, bestId, bestMatch)
            }

            // In bulk mode, collapse high-confidence sources synchronously with the phase
            // transition so the heavy SourceRecommendations never mounts for them. Single-source
            // analyze always expands so the user sees the result they triggered.
            if (isBulk) {
                const isHighConfidence = (() => {
                    if (!bestMatch) return false
                    const entityTotal = bestMatch.coveredEntityTypes.length + bestMatch.uncoveredEntityTypes.length
                    const relTotal = bestMatch.coveredRelationshipTypes.length + bestMatch.uncoveredRelationshipTypes.length
                    const totalAll = entityTotal + relTotal
                    if (totalAll === 0) return false
                    const totalCovered = bestMatch.coveredEntityTypes.length + bestMatch.coveredRelationshipTypes.length
                    return Math.round((totalCovered / totalAll) * 100) >= 50
                })()
                if (isHighConfidence) {
                    setExpandedItems(prev => {
                        if (!prev.has(item.id)) return prev
                        const next = new Set(prev)
                        next.delete(item.id)
                        return next
                    })
                } else {
                    setExpandedItems(prev => prev.has(item.id) ? prev : new Set(prev).add(item.id))
                }
            } else {
                setExpandedItems(prev => prev.has(item.id) ? prev : new Set(prev).add(item.id))
            }
            setAnalysisProgress(prev => ({ ...prev, [item.id]: 'done' }))
        } catch (err) {
            updateSource(item.id, {
                phase: 'initial',
                error: err instanceof Error ? err.message : 'Analysis failed',
            })
            setAnalysisProgress(prev => ({ ...prev, [item.id]: 'error' }))
        }
    }, [providerId, getOntology, updateSource, updateOntologySelection])

    // ─── Analyze all sources ────────────────────────────────────────────

    const analyzeAll = useCallback(async () => {
        setIsAnalyzingAll(true)
        const unanalyzed = catalogItems.filter(c => {
            const s = sourceStates[c.id]
            return s.phase === 'initial' && !s.skipped
        })

        // Initialize progress for all unanalyzed sources
        setAnalysisProgress(prev => {
            const next = { ...prev }
            for (const item of unanalyzed) next[item.id] = 'pending'
            return next
        })

        await Promise.allSettled(unanalyzed.map(item => analyzeSource(item, { bulk: true })))
        setIsAnalyzingAll(false)
    }, [catalogItems, sourceStates, analyzeSource])

    // ─── Select ontology for a source ───────────────────────────────────

    const selectOntology = useCallback((itemId: string, ontologyId: string) => {
        const state = sourceStates[itemId]
        const match = state.matches.find(m => m.ontologyId === ontologyId) ?? null
        updateSource(itemId, { selectedId: ontologyId, skipped: false })
        updateOntologySelection(itemId, ontologyId, match)
    }, [sourceStates, updateSource, updateOntologySelection])

    const selectCreateFromGraph = useCallback((itemId: string) => {
        updateSource(itemId, { selectedId: '__create_from_graph__', skipped: false })
    }, [updateSource])

    // ─── Create draft from graph ────────────────────────────────────────

    const createDraft = useCallback(async (itemId: string) => {
        const state = sourceStates[itemId]
        if (!state.suggestResponse) return

        updateSource(itemId, { isCreatingDraft: true })
        try {
            const createReq = {
                ...state.suggestResponse.suggested,
                name: state.draftName.trim() || `${catalogItems.find(c => c.id === itemId)?.name} Schema`,
            }
            const created = await ontologyDefinitionService.create(createReq)
            // Update ontologies list
            setOntologies(prev => {
                const updated = [created, ...prev]
                // Report updated name map to parent
                if (onOntologiesLoaded) {
                    const nameMap: Record<string, string> = {}
                    for (const o of updated) nameMap[o.id] = o.name
                    onOntologiesLoaded(nameMap)
                }
                return updated
            })
            updateSource(itemId, {
                selectedId: created.id,
                isCreatingDraft: false,
            })
            updateOntologySelection(itemId, created.id, null)
        } catch (err) {
            updateSource(itemId, {
                isCreatingDraft: false,
                error: err instanceof Error ? err.message : 'Failed to create draft',
            })
        }
    }, [sourceStates, catalogItems, updateSource, updateOntologySelection])

    // ─── Skip source ────────────────────────────────────────────────────

    const skipSource = useCallback((itemId: string) => {
        updateSource(itemId, { skipped: true, selectedId: null })
        updateOntologySelection(itemId, '')
    }, [updateSource, updateOntologySelection])

    const unskipSource = useCallback((itemId: string) => {
        updateSource(itemId, { skipped: false })
    }, [updateSource])

    // ─── "Apply best to all" logic ──────────────────────────────────────

    const analyzedSources = catalogItems.filter(c => sourceStates[c.id]?.phase === 'recommendations')
    const bestOntologyIds = analyzedSources
        .map(c => sourceStates[c.id]?.matches[0]?.ontologyId)
        .filter(Boolean)
    const sharedBestId = bestOntologyIds.length >= 2 && new Set(bestOntologyIds).size === 1
        ? bestOntologyIds[0]
        : null
    const sharedBestName = sharedBestId ? getOntology(sharedBestId)?.name ?? sharedBestId : null

    const applyBestToAll = useCallback(() => {
        if (!sharedBestId) return
        for (const item of catalogItems) {
            const state = sourceStates[item.id]
            if (state.phase === 'recommendations' && !state.skipped) {
                const match = state.matches.find(m => m.ontologyId === sharedBestId) ?? null
                updateSource(item.id, { selectedId: sharedBestId })
                updateOntologySelection(item.id, sharedBestId, match)
            }
        }
    }, [sharedBestId, catalogItems, sourceStates, updateSource, updateOntologySelection])

    // ─── Expand/collapse ────────────────────────────────────────────────

    const toggleExpand = (itemId: string) => {
        setExpandedItems(prev => {
            const next = new Set(prev)
            if (next.has(itemId)) next.delete(itemId)
            else next.add(itemId)
            return next
        })
    }

    const getWorkspaceName = (itemId: string): string => {
        const alloc = formData.allocations[itemId]
        if (!alloc) return 'Unassigned'
        if (alloc.workspaceId === 'new') return alloc.newWorkspaceName || 'New Workspace'
        if (alloc.workspaceId) return workspaceNames[alloc.workspaceId] || alloc.workspaceId
        return 'Unassigned'
    }

    // Count of unanalyzed, non-skipped sources
    const unanalyzedCount = catalogItems.filter(c => {
        const s = sourceStates[c.id]
        return s.phase === 'initial' && !s.skipped
    }).length

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500/20 to-purple-500/20 border border-indigo-500/10 flex items-center justify-center flex-shrink-0">
                    <BookOpen className="w-5 h-5 text-indigo-400" />
                </div>
                <div>
                    <h3 className="text-lg font-semibold text-ink">Configure Semantic Layer</h3>
                    <p className="text-sm text-ink-muted mt-0.5">
                        Assign an ontology to each data source. Use <strong className="text-ink-secondary">Analyze Graph</strong> to
                        auto-detect the best match from existing semantic layers.
                    </p>
                </div>
            </div>

            {/* Info banner */}
            <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05 }}
                className="glass-panel-subtle rounded-xl border border-indigo-500/20 bg-indigo-500/5 px-4 py-3 flex items-start gap-3"
            >
                <Info className="w-4 h-4 text-indigo-400 mt-0.5 flex-shrink-0" />
                <p className="text-sm text-ink-secondary leading-relaxed">
                    Analysis scans entity and relationship types in each data source,
                    checks existing layers for coverage, and recommends the best fit.
                    You can also create a new draft directly from the graph schema.
                </p>
            </motion.div>

            {/* Bulk actions bar */}
            <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05 }}
            >
                {/* Hero CTA when ALL sources are unanalyzed */}
                {unanalyzedCount === catalogItems.length && !isAnalyzingAll && (
                    <motion.div className="rounded-2xl border-2 border-dashed border-indigo-500/30 bg-gradient-to-r from-indigo-500/[0.04] to-purple-500/[0.04] p-6 text-center">
                        <div className="flex flex-col items-center gap-3">
                            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-indigo-500/20 to-purple-500/20 border border-indigo-500/10 flex items-center justify-center">
                                <Sparkles className="w-6 h-6 text-indigo-400" />
                            </div>
                            <div>
                                <h4 className="text-sm font-bold text-ink">Analyze All Sources at Once</h4>
                                <p className="text-xs text-ink-muted mt-1 max-w-sm mx-auto leading-relaxed">
                                    Scan all {catalogItems.length} source{catalogItems.length !== 1 ? 's' : ''} to auto-detect
                                    the best semantic layer match for each
                                </p>
                            </div>
                            <button
                                type="button"
                                onClick={analyzeAll}
                                disabled={isAnalyzingAll || loadingOntologies}
                                className={cn(
                                    'flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold transition-colors duration-150 shadow-sm',
                                    'bg-gradient-to-r from-indigo-500 to-purple-500 text-white',
                                    'hover:from-indigo-600 hover:to-purple-600 shadow-indigo-500/25',
                                    'disabled:opacity-50 disabled:cursor-not-allowed',
                                )}
                            >
                                <Sparkles className="w-4 h-4" />
                                Analyze All Sources
                            </button>
                        </div>
                    </motion.div>
                )}

                {/* Inline buttons when SOME sources are analyzed */}
                {unanalyzedCount > 0 && unanalyzedCount < catalogItems.length && !isAnalyzingAll && (
                    <div className="flex items-center gap-3">
                        <button
                            type="button"
                            onClick={analyzeAll}
                            disabled={isAnalyzingAll || loadingOntologies}
                            className={cn(
                                'flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium',
                                'bg-gradient-to-r from-indigo-500/10 to-purple-500/10 border border-indigo-500/20',
                                'hover:from-indigo-500/15 hover:to-purple-500/15 hover:border-indigo-500/30',
                                'text-indigo-500 transition-colors duration-150',
                                'disabled:opacity-50 disabled:cursor-not-allowed',
                            )}
                        >
                            <Sparkles className="w-4 h-4" />
                            Analyze Remaining ({unanalyzedCount})
                        </button>

                        {sharedBestId && sharedBestName && (
                            <motion.button
                                initial={{ opacity: 0, scale: 0.95 }}
                                animate={{ opacity: 1, scale: 1 }}
                                type="button"
                                onClick={applyBestToAll}
                                className="flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium bg-emerald-500/10 border border-emerald-500/20 text-emerald-500 hover:bg-emerald-500/15 transition-colors duration-150"
                            >
                                <Zap className="w-4 h-4" />
                                Apply &ldquo;{sharedBestName}&rdquo; to all
                            </motion.button>
                        )}
                    </div>
                )}

                {/* Apply best to all — when no unanalyzed remain */}
                {unanalyzedCount === 0 && !isAnalyzingAll && sharedBestId && sharedBestName && (
                    <div className="flex items-center gap-3">
                        <motion.button
                            initial={{ opacity: 0, scale: 0.95 }}
                            animate={{ opacity: 1, scale: 1 }}
                            type="button"
                            onClick={applyBestToAll}
                            className="flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium bg-emerald-500/10 border border-emerald-500/20 text-emerald-500 hover:bg-emerald-500/15 transition-colors duration-150"
                        >
                            <Zap className="w-4 h-4" />
                            Apply &ldquo;{sharedBestName}&rdquo; to all
                        </motion.button>
                    </div>
                )}

                {/* Per-source progress indicators during bulk analysis */}
                {isAnalyzingAll && (
                    <motion.div
                        initial={{ opacity: 0, y: 6 }}
                        animate={{ opacity: 1, y: 0 }}
                        className="glass-panel rounded-xl p-4 space-y-2"
                    >
                        <p className="text-xs font-semibold text-ink mb-2">Analyzing {catalogItems.length} sources...</p>
                        {catalogItems.map(item => {
                            const progress = analysisProgress[item.id]
                            return (
                                <div key={item.id} className="flex items-center gap-2.5 text-xs">
                                    {progress === 'analyzing' && <Loader2 className="w-3.5 h-3.5 text-indigo-500 animate-spin" />}
                                    {progress === 'done' && <Check className="w-3.5 h-3.5 text-emerald-500" />}
                                    {progress === 'error' && <X className="w-3.5 h-3.5 text-red-400" />}
                                    {progress === 'pending' && <Clock className="w-3.5 h-3.5 text-ink-muted" />}
                                    {!progress && <Clock className="w-3.5 h-3.5 text-ink-muted" />}
                                    <span className={cn('flex-1 truncate', progress === 'done' ? 'text-ink' : 'text-ink-muted')}>{item.name}</span>
                                    {progress === 'done' && (() => {
                                        const state = sourceStates[item.id]
                                        const best = state?.matches[0]
                                        const conf = getConfidence(best)
                                        return <span className={cn('text-[10px] font-bold px-1.5 py-0.5 rounded-full', conf.bgColor, conf.color)}>{conf.label}</span>
                                    })()}
                                </div>
                            )
                        })}
                    </motion.div>
                )}
            </motion.div>

            {/* Bulk confirmation summary */}
            {showBulkSummary && !isAnalyzingAll && (
                <BulkConfirmationSummary
                    catalogItems={catalogItems}
                    sourceStates={sourceStates}
                    formData={formData}
                    getOntology={getOntology}
                    onConfirmAll={() => {
                        setExpandedItems(new Set())
                        setShowBulkSummary(false)
                        const count = Object.values(formData.ontologySelections).filter(s => s.ontologyId).length
                        showToast('success', `Semantic layer configured for ${count} source${count !== 1 ? 's' : ''}`)
                    }}
                    onChangeSource={(itemId) => {
                        setExpandedItems(prev => new Set(prev).add(itemId))
                    }}
                />
            )}

            {/* Skeleton loaders while ontologies load */}
            {loadingOntologies && (
                <div className="space-y-4">
                    {catalogItems.map((item, index) => (
                        <motion.div
                            key={item.id}
                            initial={{ opacity: 0, y: 12 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ delay: Math.min(index * 0.01, 0.05) }}
                            className="glass-panel rounded-xl overflow-hidden"
                        >
                            <div className="px-5 py-3.5 flex items-center gap-3">
                                <div className="w-4 h-4 rounded bg-ink-muted/10 animate-pulse flex-shrink-0" />
                                <div className="flex-1 space-y-2">
                                    <div className="h-4 w-1/3 rounded bg-ink-muted/10 animate-pulse" />
                                </div>
                                <div className="h-4 w-16 rounded-full bg-ink-muted/10 animate-pulse" />
                            </div>
                            <div className="px-5 pb-5 pt-1 border-t border-glass-border/50 space-y-3">
                                <div className="rounded-xl border border-glass-border bg-black/[0.02] dark:bg-white/[0.02] p-4 space-y-2.5">
                                    <div className="h-3 w-24 rounded bg-ink-muted/10 animate-pulse" />
                                    <div className="space-y-2">
                                        <div className="flex items-center gap-2.5">
                                            <div className="w-3.5 h-3.5 rounded bg-ink-muted/10 animate-pulse flex-shrink-0" />
                                            <div className="h-3 w-3/4 rounded bg-ink-muted/10 animate-pulse" />
                                        </div>
                                        <div className="flex items-center gap-2.5">
                                            <div className="w-3.5 h-3.5 rounded bg-ink-muted/10 animate-pulse flex-shrink-0" />
                                            <div className="h-3 w-2/3 rounded bg-ink-muted/10 animate-pulse" />
                                        </div>
                                        <div className="flex items-center gap-2.5">
                                            <div className="w-3.5 h-3.5 rounded bg-ink-muted/10 animate-pulse flex-shrink-0" />
                                            <div className="h-3 w-1/2 rounded bg-ink-muted/10 animate-pulse" />
                                        </div>
                                    </div>
                                </div>
                                <div className="flex items-center gap-3">
                                    <div className="h-10 w-36 rounded-xl bg-ink-muted/10 animate-pulse" />
                                    <div className="h-4 w-28 rounded bg-ink-muted/10 animate-pulse" />
                                </div>
                            </div>
                        </motion.div>
                    ))}
                </div>
            )}

            {/* Per-source ontology cards */}
            {!loadingOntologies && <div className="space-y-4">
                {catalogItems.map((item, index) => {
                    const state = sourceStates[item.id] ?? getInitialSourceState()
                    const isExpanded = expandedItems.has(item.id)
                    const itemOntologyId = formData.ontologySelections[item.id]?.ontologyId ?? ''

                    return (
                        <motion.div
                            key={item.id}
                            initial={{ opacity: 0, y: 12 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ delay: Math.min(index * 0.01, 0.05) }}
                            className={cn(
                                'glass-panel rounded-xl overflow-hidden',
                                state.phase === 'recommendations' && (() => {
                                    const best = state.matches[0]
                                    const conf = getConfidence(best)
                                    if (conf.level === 'low') return 'border-l-2 border-l-red-400'
                                    if (conf.level === 'none') return 'border-l-2 border-l-red-500'
                                    return ''
                                })()
                            )}
                        >
                            {/* Item header */}
                            <button
                                type="button"
                                onClick={() => toggleExpand(item.id)}
                                className="flex items-center gap-3 w-full px-5 py-3.5 text-left hover:bg-black/[0.02] dark:hover:bg-white/[0.02] transition-colors"
                            >
                                <ChevronDown className={cn(
                                    'w-4 h-4 text-ink-muted transition-transform duration-200 flex-shrink-0',
                                    isExpanded && 'rotate-180'
                                )} />
                                <span className="text-sm font-medium text-ink truncate flex-1">{item.name}</span>
                                <span className="text-ink-secondary mx-1 flex-shrink-0">&rarr;</span>
                                <span className="text-xs text-ink-muted truncate max-w-[120px]">{getWorkspaceName(item.id)}</span>

                                <div className="flex items-center gap-2 shrink-0 ml-2">
                                    {state.skipped ? (
                                        <span className="text-xs font-medium text-ink-muted bg-black/[0.04] dark:bg-white/[0.06] px-2 py-0.5 rounded-full">Skipped</span>
                                    ) : itemOntologyId ? (
                                        <div className="flex items-center gap-1.5 text-emerald-400">
                                            <Check className="w-3.5 h-3.5" />
                                            <span className="text-xs font-medium">Configured</span>
                                        </div>
                                    ) : (
                                        <span className="text-xs font-medium text-amber-400">Pending</span>
                                    )}
                                    {itemOntologyId && !state.skipped && state.phase === 'recommendations' && (() => {
                                        const selectedMatch = state.matches.find(m => m.ontologyId === itemOntologyId)
                                        const confidence = getConfidence(selectedMatch)
                                        return (
                                            <span className={cn('text-[10px] font-bold px-1.5 py-0.5 rounded-full border', confidence.bgColor, confidence.color, confidence.borderColor)}>
                                                {confidence.label}
                                            </span>
                                        )
                                    })()}
                                </div>
                            </button>

                            {/* Expanded content */}
                            <AnimatePresence>
                                {isExpanded && (
                                    <motion.div
                                        initial={{ opacity: 0, height: 0 }}
                                        animate={{ opacity: 1, height: 'auto' }}
                                        exit={{ opacity: 0, height: 0 }}
                                        transition={{ duration: 0.12 }}
                                        className="overflow-hidden"
                                    >
                                        <div className="px-5 pb-5 pt-1 space-y-4 border-t border-glass-border/50">
                                            {/* Skipped state */}
                                            {state.skipped && (
                                                <div className="flex items-center justify-between py-3">
                                                    <p className="text-sm text-ink-muted">Ontology configuration skipped for this source.</p>
                                                    <button
                                                        type="button"
                                                        onClick={() => unskipSource(item.id)}
                                                        className="text-xs font-medium text-indigo-400 hover:text-indigo-300 transition-colors"
                                                    >
                                                        Configure now
                                                    </button>
                                                </div>
                                            )}

                                            {/* Phase 1: Initial — Analyze button */}
                                            {!state.skipped && state.phase === 'initial' && (
                                                <div className="space-y-3">
                                                    <div className="rounded-xl border border-glass-border bg-black/[0.02] dark:bg-white/[0.02] p-4">
                                                        <p className="text-[10px] text-ink-muted uppercase tracking-wider font-bold mb-2.5">What happens next</p>
                                                        <div className="space-y-2">
                                                            <div className="flex items-center gap-2.5">
                                                                <Box className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />
                                                                <span className="text-xs text-ink-secondary">Detect entity and relationship types from your graph</span>
                                                            </div>
                                                            <div className="flex items-center gap-2.5">
                                                                <Shield className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />
                                                                <span className="text-xs text-ink-secondary">Check existing layers for coverage</span>
                                                            </div>
                                                            <div className="flex items-center gap-2.5">
                                                                <Sparkles className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />
                                                                <span className="text-xs text-ink-secondary">Recommend the best fit or generate a new draft</span>
                                                            </div>
                                                        </div>
                                                    </div>

                                                    {state.error && (
                                                        <div className="flex items-center gap-2 text-sm text-red-400">
                                                            <X className="w-4 h-4 flex-shrink-0" />
                                                            <span>{state.error}</span>
                                                        </div>
                                                    )}

                                                    <div className="flex items-center gap-3">
                                                        <button
                                                            type="button"
                                                            onClick={() => analyzeSource(item)}
                                                            disabled={loadingOntologies}
                                                            className={cn(
                                                                'flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors duration-150 shadow-sm',
                                                                'bg-gradient-to-r from-indigo-500 to-purple-500 text-white',
                                                                'hover:from-indigo-600 hover:to-purple-600 shadow-indigo-500/25',
                                                                'disabled:opacity-50',
                                                            )}
                                                        >
                                                            <Sparkles className="w-4 h-4" />
                                                            Analyze Graph
                                                        </button>
                                                        <button
                                                            type="button"
                                                            onClick={() => skipSource(item.id)}
                                                            className="text-xs font-medium text-ink-muted hover:text-ink-secondary transition-colors"
                                                        >
                                                            Skip — configure later
                                                        </button>
                                                    </div>
                                                </div>
                                            )}

                                            {/* Phase 2: Analyzing */}
                                            {!state.skipped && state.phase === 'analyzing' && (
                                                <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-indigo-50/50 dark:bg-indigo-950/20 border border-indigo-200/50 dark:border-indigo-800/30">
                                                    <Loader2 className="w-4 h-4 text-indigo-500 animate-spin flex-shrink-0" />
                                                    <p className="text-xs text-indigo-600 dark:text-indigo-400 font-medium">
                                                        Analyzing graph schema and checking existing layers...
                                                    </p>
                                                </div>
                                            )}

                                            {/* Phase 3: Recommendations */}
                                            {!state.skipped && state.phase === 'recommendations' && (
                                                <SourceRecommendations
                                                    state={state}
                                                    item={item}
                                                    getOntology={getOntology}
                                                    selectOntology={selectOntology}
                                                    selectCreateFromGraph={selectCreateFromGraph}
                                                    createDraft={createDraft}
                                                    updateSource={updateSource}
                                                    skipSource={skipSource}
                                                    analyzeSource={analyzeSource}
                                                />
                                            )}

                                            {/* Resolution preview banner — surfaces unclassified
                                                relationships and missing types the moment an
                                                ontology is selected, so the user sees the gap
                                                before reaching the SchemaReview step. */}
                                            {!state.skipped && state.phase === 'recommendations' && itemOntologyId && itemOntologyId !== '__create_from_graph__' && (
                                                <ResolutionPreviewBanner
                                                    ontologyId={itemOntologyId}
                                                    providerId={providerId}
                                                    assetName={item.sourceIdentifier || item.name}
                                                />
                                            )}
                                        </div>
                                    </motion.div>
                                )}
                            </AnimatePresence>
                        </motion.div>
                    )
                })}
            </div>}
        </div>
    )
}

// ─── Recommendation Sub-Component ────────────────────────────────────────────

interface SourceRecommendationsProps {
    state: SourceState
    item: CatalogItemResponse
    getOntology: (id: string) => OntologyDefinitionResponse | undefined
    selectOntology: (itemId: string, ontologyId: string) => void
    selectCreateFromGraph: (itemId: string) => void
    createDraft: (itemId: string) => void
    updateSource: (itemId: string, updates: Partial<SourceState>) => void
    skipSource: (itemId: string) => void
    analyzeSource: (item: CatalogItemResponse) => void
}

const SourceRecommendations = memo(function SourceRecommendations({
    state,
    item,
    getOntology,
    selectOntology,
    selectCreateFromGraph,
    createDraft,
    updateSource,
    skipSource,
    analyzeSource,
}: SourceRecommendationsProps) {
    const itemId = item.id
    const onSelect = useCallback((ontologyId: string) => selectOntology(itemId, ontologyId), [selectOntology, itemId])
    const onSelectCreate = useCallback(() => selectCreateFromGraph(itemId), [selectCreateFromGraph, itemId])
    const onCreateDraft = useCallback(() => createDraft(itemId), [createDraft, itemId])
    const onDraftNameChange = useCallback((name: string) => updateSource(itemId, { draftName: name }), [updateSource, itemId])
    const onSearchChange = useCallback((search: string) => updateSource(itemId, { search }), [updateSource, itemId])
    const onSkip = useCallback(() => skipSource(itemId), [skipSource, itemId])
    const onReanalyze = useCallback(() => analyzeSource(item), [analyzeSource, item])
    const { matches, graphCounts, selectedId, draftName, search, error, isCreatingDraft } = state

    // Filter by search query and pre-compute coverage percentages once per
    // [matches, search] change so the map below is a pure render.
    const filteredMatches = useMemo(() => {
        const q = search.trim().toLowerCase()
        const filtered = q
            ? matches.filter(m => {
                const ont = getOntology(m.ontologyId)
                return (
                    m.ontologyName.toLowerCase().includes(q) ||
                    ont?.description?.toLowerCase().includes(q)
                )
            })
            : matches
        return filtered.map(match => {
            const entityTotal = match.coveredEntityTypes.length + match.uncoveredEntityTypes.length
            const relTotal = match.coveredRelationshipTypes.length + match.uncoveredRelationshipTypes.length
            const totalAll = entityTotal + relTotal
            const totalCovered = match.coveredEntityTypes.length + match.coveredRelationshipTypes.length
            return {
                match,
                entityPct: entityTotal > 0 ? Math.round((match.coveredEntityTypes.length / entityTotal) * 100) : 0,
                relPct: relTotal > 0 ? Math.round((match.coveredRelationshipTypes.length / relTotal) * 100) : 0,
                overallPct: totalAll > 0 ? Math.round((totalCovered / totalAll) * 100) : 0,
            }
        })
    }, [matches, search, getOntology])

    const selectedMatch = selectedId && selectedId !== '__create_from_graph__'
        ? matches.find(m => m.ontologyId === selectedId)
        : null

    return (
        <div className="space-y-4">
            {/* Graph stats banner */}
            <div className="flex items-center gap-1 px-4 py-2.5 rounded-xl bg-gradient-to-r from-indigo-500/[0.06] to-purple-500/[0.06] border border-indigo-500/10">
                <Database className="w-4 h-4 text-indigo-400 mr-2 flex-shrink-0" />
                <span className="text-xs text-ink-secondary">Your graph contains</span>
                <span className="text-xs font-bold text-ink mx-0.5">{graphCounts.entities}</span>
                <span className="text-xs text-ink-secondary mr-1">entity type{graphCounts.entities !== 1 ? 's' : ''}</span>
                <span className="text-xs text-ink-muted mx-1">and</span>
                <span className="text-xs font-bold text-ink mx-0.5">{graphCounts.rels}</span>
                <span className="text-xs text-ink-secondary">relationship{graphCounts.rels !== 1 ? 's' : ''}</span>
            </div>

            {/* Error display */}
            {error && (
                <div className="flex items-center gap-2 text-sm text-red-400">
                    <X className="w-4 h-4 flex-shrink-0" />
                    <span>{error}</span>
                </div>
            )}

            {/* Match cards */}
            {matches.length > 0 && (
                <div>
                    <div className="flex items-center justify-between mb-2.5">
                        <div>
                            <p className="text-xs font-semibold text-ink">Recommended Semantic Layers</p>
                            <p className="text-[11px] text-ink-muted mt-0.5">Select a layer to use, or create a new draft</p>
                        </div>
                    </div>

                    {/* Search bar */}
                    {matches.length > 3 && (
                        <div className="relative mb-3">
                            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-muted/60" />
                            <input
                                type="text"
                                value={search}
                                onChange={e => onSearchChange(e.target.value)}
                                placeholder="Search semantic layers..."
                                className="w-full pl-9 pr-8 py-2 rounded-xl bg-black/[0.03] dark:bg-white/[0.04] border border-glass-border/60 text-xs text-ink placeholder:text-ink-muted/50 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500/30 transition-colors duration-150"
                            />
                            {search && (
                                <button
                                    onClick={() => onSearchChange('')}
                                    className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-black/10 dark:hover:bg-white/10 text-ink-muted/50 hover:text-ink-muted transition-colors"
                                >
                                    <X className="w-3 h-3" />
                                </button>
                            )}
                        </div>
                    )}

                    {search && (
                        <p className="text-[11px] text-ink-muted mb-2">
                            {filteredMatches.length} of {matches.length} match{matches.length !== 1 ? 'es' : ''}
                        </p>
                    )}

                    <div className="space-y-2.5">
                        {filteredMatches.map(({ match, entityPct, relPct, overallPct }, idx) => {
                            const ont = getOntology(match.ontologyId)
                            const isSystem = ont?.isSystem ?? false
                            const isPublished = ont?.isPublished ?? false
                            const isBest = !search && idx === 0
                            const isSelected = selectedId === match.ontologyId

                            const entityTotal = match.coveredEntityTypes.length + match.uncoveredEntityTypes.length
                            const relTotal = match.coveredRelationshipTypes.length + match.uncoveredRelationshipTypes.length

                            return (
                                <button
                                    key={match.ontologyId}
                                    type="button"
                                    onClick={() => onSelect(match.ontologyId)}
                                    className={cn(
                                        'w-full text-left rounded-xl border-2 transition-colors duration-150',
                                        isSelected
                                            ? 'border-indigo-500 bg-indigo-50/30 dark:bg-indigo-950/20 shadow-sm shadow-indigo-500/10 ring-1 ring-indigo-500/20'
                                            : isBest
                                                ? 'border-indigo-500/30 bg-indigo-50/10 dark:bg-indigo-950/5 hover:border-indigo-500/50'
                                                : 'border-glass-border hover:border-glass-border-hover hover:bg-black/[0.01] dark:hover:bg-white/[0.01]',
                                    )}
                                >
                                    <div className="px-4 py-3">
                                        <div className="flex items-start gap-3">
                                            {/* Radio + Coverage Ring */}
                                            <div className="flex flex-col items-center gap-2 pt-0.5">
                                                <div className={cn(
                                                    'w-[18px] h-[18px] rounded-full border-2 flex items-center justify-center flex-shrink-0 transition-colors duration-150',
                                                    isSelected
                                                        ? 'border-indigo-500 bg-indigo-500'
                                                        : 'border-glass-border-hover',
                                                )}>
                                                    {isSelected && <Check className="w-2.5 h-2.5 text-white" />}
                                                </div>
                                                <CoverageRing percent={overallPct} size={44} stroke={4} color={coverageColor(overallPct)} />
                                            </div>

                                            {/* Content */}
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2 flex-wrap mb-0.5">
                                                    <p className="text-sm font-bold text-ink truncate">{match.ontologyName}</p>
                                                    {isBest && (
                                                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-bold rounded-full bg-gradient-to-r from-indigo-500 to-purple-500 text-white shadow-sm">
                                                            BEST MATCH
                                                        </span>
                                                    )}
                                                </div>

                                                {ont?.description && (
                                                    <p className="text-[11px] text-ink-secondary leading-snug mb-1.5 line-clamp-1">
                                                        {ont.description}
                                                    </p>
                                                )}

                                                {/* Badges */}
                                                <div className="flex items-center gap-1.5 flex-wrap mb-2">
                                                    <span className="text-[10px] text-ink-muted font-medium">v{match.version}</span>
                                                    {isSystem && (
                                                        <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border border-indigo-500/20">
                                                            <Shield className="w-2.5 h-2.5" />System
                                                        </span>
                                                    )}
                                                    {isPublished && !isSystem && (
                                                        <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">
                                                            <CheckCircle2 className="w-2.5 h-2.5" />Published
                                                        </span>
                                                    )}
                                                    {!isPublished && !isSystem && (
                                                        <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
                                                            <PenLine className="w-2.5 h-2.5" />Draft
                                                        </span>
                                                    )}
                                                </div>

                                                {/* Type counts */}
                                                <div className="flex items-center gap-3 mb-2">
                                                    <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted">
                                                        <Box className="w-3 h-3 text-indigo-400" />
                                                        {match.totalEntityTypes} entity type{match.totalEntityTypes !== 1 ? 's' : ''}
                                                    </span>
                                                    <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted">
                                                        <GitBranch className="w-3 h-3 text-purple-400" />
                                                        {match.totalRelationshipTypes} relationship{match.totalRelationshipTypes !== 1 ? 's' : ''}
                                                    </span>
                                                </div>

                                                {/* Coverage bars */}
                                                <div className="flex gap-4">
                                                    <MiniBar
                                                        covered={match.coveredEntityTypes.length}
                                                        total={entityTotal}
                                                        label="Entity Types"
                                                        colorClass={coverageBarClass(entityPct)}
                                                    />
                                                    <MiniBar
                                                        covered={match.coveredRelationshipTypes.length}
                                                        total={relTotal}
                                                        label="Relationships"
                                                        colorClass={coverageBarClass(relPct)}
                                                    />
                                                </div>

                                                {/* Uncovered types (when selected) */}
                                                {isSelected && (match.uncoveredEntityTypes.length > 0 || match.uncoveredRelationshipTypes.length > 0) && (
                                                    <div className="mt-2.5 px-3 py-2 rounded-lg bg-amber-50/50 dark:bg-amber-950/10 border border-amber-200/30 dark:border-amber-800/20">
                                                        {match.uncoveredEntityTypes.length > 0 && (
                                                            <p className="text-[11px] text-amber-700 dark:text-amber-400">
                                                                <span className="font-semibold">Missing entity types:</span>{' '}
                                                                {match.uncoveredEntityTypes.slice(0, 5).join(', ')}
                                                                {match.uncoveredEntityTypes.length > 5 && (
                                                                    <span className="text-amber-600/70 dark:text-amber-500/70"> +{match.uncoveredEntityTypes.length - 5} more</span>
                                                                )}
                                                            </p>
                                                        )}
                                                        {match.uncoveredRelationshipTypes.length > 0 && (
                                                            <p className={cn('text-[11px] text-amber-700 dark:text-amber-400', match.uncoveredEntityTypes.length > 0 && 'mt-1')}>
                                                                <span className="font-semibold">Missing relationships:</span>{' '}
                                                                {match.uncoveredRelationshipTypes.slice(0, 5).join(', ')}
                                                                {match.uncoveredRelationshipTypes.length > 5 && (
                                                                    <span className="text-amber-600/70 dark:text-amber-500/70"> +{match.uncoveredRelationshipTypes.length - 5} more</span>
                                                                )}
                                                            </p>
                                                        )}
                                                    </div>
                                                )}

                                                {/* Full coverage callout */}
                                                {isSelected && match.uncoveredEntityTypes.length === 0 && match.uncoveredRelationshipTypes.length === 0 && (
                                                    <div className="mt-2.5 px-3 py-2 rounded-lg bg-emerald-50/50 dark:bg-emerald-950/10 border border-emerald-200/30 dark:border-emerald-800/20">
                                                        <p className="text-[11px] text-emerald-700 dark:text-emerald-400 font-semibold flex items-center gap-1.5">
                                                            <CheckCircle2 className="w-3 h-3" />
                                                            Full coverage — this layer covers every type in your graph
                                                        </p>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                </button>
                            )
                        })}
                    </div>

                    {/* No search results */}
                    {search && filteredMatches.length === 0 && (
                        <div className="text-center py-4">
                            <p className="text-xs text-ink-muted">No semantic layers match your search</p>
                        </div>
                    )}
                </div>
            )}

            {/* "Create from Physical Graph" option */}
            <div>
                {matches.length > 0 && (
                    <div className="flex items-center gap-2 mb-3">
                        <div className="flex-1 h-px bg-glass-border/60" />
                        <span className="text-[10px] font-medium text-ink-muted/50 uppercase tracking-wider">Or start fresh</span>
                        <div className="flex-1 h-px bg-glass-border/60" />
                    </div>
                )}

                <button
                    type="button"
                    onClick={onSelectCreate}
                    className={cn(
                        'w-full text-left rounded-xl border-2 transition-colors duration-150',
                        selectedId === '__create_from_graph__'
                            ? 'border-indigo-500 bg-indigo-50/30 dark:bg-indigo-950/20 shadow-sm shadow-indigo-500/10 ring-1 ring-indigo-500/20'
                            : 'border-dashed border-glass-border hover:border-indigo-500/30 hover:bg-black/[0.01] dark:hover:bg-white/[0.01]',
                    )}
                >
                    <div className="px-4 py-3">
                        <div className="flex items-start gap-3">
                            <div className="flex flex-col items-center gap-2 pt-0.5">
                                <div className={cn(
                                    'w-[18px] h-[18px] rounded-full border-2 flex items-center justify-center flex-shrink-0 transition-colors duration-150',
                                    selectedId === '__create_from_graph__'
                                        ? 'border-indigo-500 bg-indigo-500'
                                        : 'border-glass-border-hover',
                                )}>
                                    {selectedId === '__create_from_graph__' && <Check className="w-2.5 h-2.5 text-white" />}
                                </div>
                                <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500/15 to-purple-500/15 border border-indigo-500/10 flex items-center justify-center">
                                    <Sparkles className="w-5 h-5 text-indigo-500" />
                                </div>
                            </div>
                            <div className="flex-1 min-w-0">
                                <p className="text-sm font-bold text-ink mb-0.5">Create from Physical Graph</p>
                                <p className="text-[11px] text-ink-secondary leading-snug mb-2">
                                    Generate a new semantic layer draft from the {graphCounts.entities} entity type{graphCounts.entities !== 1 ? 's' : ''}{' '}
                                    and {graphCounts.rels} relationship{graphCounts.rels !== 1 ? 's' : ''} detected.
                                </p>
                                <div className="flex items-center gap-3">
                                    <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted">
                                        <Box className="w-3 h-3 text-indigo-400" />{graphCounts.entities} entities
                                    </span>
                                    <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted">
                                        <GitBranch className="w-3 h-3 text-purple-400" />{graphCounts.rels} relationships
                                    </span>
                                    <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
                                        <PenLine className="w-2.5 h-2.5" />New Draft
                                    </span>
                                </div>
                            </div>
                        </div>
                    </div>
                </button>
            </div>

            {/* Create from graph — name input + confirm (shown when selected) */}
            {selectedId === '__create_from_graph__' && (
                <motion.div
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="rounded-xl bg-indigo-50/40 dark:bg-indigo-950/15 border border-indigo-500/15 p-3 space-y-2.5"
                >
                    <label className="text-[10px] font-semibold text-ink-muted uppercase tracking-wider block">
                        Name your schema
                    </label>
                    <input
                        type="text"
                        value={draftName}
                        onChange={e => onDraftNameChange(e.target.value)}
                        placeholder="Enter schema name..."
                        className="w-full px-3 py-1.5 rounded-lg bg-white dark:bg-black/20 border border-glass-border text-sm text-ink placeholder:text-ink-muted/50 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500/30 transition-colors duration-150"
                    />
                    <button
                        type="button"
                        onClick={onCreateDraft}
                        disabled={isCreatingDraft || !draftName.trim()}
                        className={cn(
                            'flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold transition-colors duration-150 shadow-sm',
                            'bg-gradient-to-r from-indigo-500 to-purple-500 text-white',
                            'hover:from-indigo-600 hover:to-purple-600 shadow-indigo-500/25',
                            (isCreatingDraft || !draftName.trim()) && 'opacity-60',
                        )}
                    >
                        {isCreatingDraft ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                        {isCreatingDraft ? 'Creating...' : 'Create from Graph'}
                    </button>
                </motion.div>
            )}

            {/* Selection summary */}
            {selectedMatch && selectedId !== '__create_from_graph__' && (
                <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-50/40 dark:bg-indigo-950/15 border border-indigo-500/15">
                    <Check className="w-3.5 h-3.5 text-indigo-500 flex-shrink-0" />
                    <span className="text-[11px] text-ink-secondary">
                        Selected: <span className="font-semibold text-ink">{selectedMatch.ontologyName}</span>
                        <span className="text-ink-muted ml-1">v{selectedMatch.version}</span>
                    </span>
                </div>
            )}

            {/* Actions row */}
            <div className="flex items-center justify-between pt-1">
                <button
                    type="button"
                    onClick={onSkip}
                    className="text-xs font-medium text-ink-muted hover:text-ink-secondary transition-colors"
                >
                    Skip — configure later
                </button>
                <button
                    type="button"
                    onClick={onReanalyze}
                    className="text-xs font-medium text-indigo-400 hover:text-indigo-300 transition-colors"
                >
                    Re-analyze
                </button>
            </div>
        </div>
    )
})

// ─── Bulk Confirmation Summary Sub-Component ────────────────────────────────

function BulkConfirmationSummary({
    catalogItems, sourceStates, formData, getOntology,
    onConfirmAll, onChangeSource,
}: {
    catalogItems: CatalogItemResponse[]
    sourceStates: Record<string, SourceState>
    formData: OnboardingFormData
    getOntology: (id: string) => OntologyDefinitionResponse | undefined
    onConfirmAll: () => void
    onChangeSource: (itemId: string) => void
}) {
    const analyzedItems = catalogItems.filter(c => sourceStates[c.id]?.phase === 'recommendations')

    return (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="glass-panel rounded-xl overflow-hidden">
            <div className="px-5 py-3.5 border-b border-glass-border/50 flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                    <h4 className="text-sm font-semibold text-ink">Auto-Assignment Summary</h4>
                </div>
                <button onClick={onConfirmAll} className="flex items-center gap-2 px-4 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/10 text-emerald-500 border border-emerald-500/20 hover:bg-emerald-500/20 transition-colors">
                    <Check className="w-3.5 h-3.5" />
                    Confirm All
                </button>
            </div>
            <div className="divide-y divide-glass-border/30">
                {analyzedItems.map(item => {
                    const state = sourceStates[item.id]
                    const selectedId = formData.ontologySelections[item.id]?.ontologyId
                    const selectedMatch = state.matches.find(m => m.ontologyId === selectedId)
                    const confidence = getConfidence(selectedMatch)
                    const ontName = selectedMatch?.ontologyName || getOntology(selectedId || '')?.name || 'No match'
                    const needsAttention = confidence.level === 'low' || confidence.level === 'none'

                    return (
                        <div key={item.id} className={cn('flex items-center gap-3 px-5 py-2.5 text-xs', needsAttention && 'bg-amber-500/[0.03]')}>
                            {needsAttention && <AlertTriangle className="w-3.5 h-3.5 text-amber-500 flex-shrink-0" />}
                            <span className="font-medium text-ink truncate flex-1 min-w-0">{item.name}</span>
                            <span className="text-ink-muted flex-shrink-0">&rarr;</span>
                            <span className="font-medium text-ink-secondary truncate max-w-[180px]">{ontName}</span>
                            <span className={cn('text-[10px] font-bold px-1.5 py-0.5 rounded-full border flex-shrink-0', confidence.bgColor, confidence.color, confidence.borderColor)}>{confidence.label}</span>
                            <button onClick={() => onChangeSource(item.id)} className="text-[10px] font-medium text-indigo-400 hover:text-indigo-300 transition-colors flex-shrink-0">Change</button>
                        </div>
                    )
                })}
            </div>
        </motion.div>
    )
}


// ─── Resolution Preview Banner ────────────────────────────────────────────
//
// Renders inline beneath an ontology selection. Calls the gate
// preview endpoint and surfaces the count of unclassified
// relationships, missing entity types, and missing edge types so
// the user sees the gap before reaching SchemaReviewStep. This is
// purely informational at this step — the gate is enforced (and
// fixed) on the next step.

function ResolutionPreviewBanner({
    ontologyId,
    providerId,
    assetName,
}: {
    ontologyId: string
    providerId: string
    assetName: string
}) {
    const [resolution, setResolution] = useState<OntologyResolutionResponse | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)

    useEffect(() => {
        let cancelled = false
        async function run() {
            setLoading(true)
            setError(null)
            try {
                const envelope = await providerService.getAssetStats(providerId, assetName)
                if (cancelled) return
                if (!envelope.data) {
                    setResolution(null)
                    return
                }
                // The /resolution-check endpoint expects GraphSchemaStats
                // with camelCase keys. providerService.getAssetStats returns
                // the canonical envelope which already has that shape after
                // transformation in SemanticStep — but the raw data here is
                // the entityTypeCounts/edgeTypeCounts dict. Transform it.
                const stats = transformStatsForSuggest(envelope.data)
                const report = await ontologyResolutionService.previewForOntology(ontologyId, stats)
                if (!cancelled) setResolution(report)
            } catch (e) {
                if (!cancelled) setError(e instanceof Error ? e.message : String(e))
            } finally {
                if (!cancelled) setLoading(false)
            }
        }
        run()
        return () => { cancelled = true }
    }, [ontologyId, providerId, assetName])

    if (loading || error || !resolution) return null
    const noContainment = (resolution.advisoryWarnings ?? []).includes('no_containment_edges')
    if (
        resolution.resolved &&
        resolution.hierarchyWarnings.length === 0 &&
        !noContainment
    ) return null

    const unclassifiedCount = resolution.unclassifiedRelationships.length
    const missingEntityCount = resolution.missingEntityTypes.length
    const missingEdgeCount = resolution.missingEdgeTypes.length
    const hasBlocking = !resolution.resolved

    const parts: string[] = []
    if (unclassifiedCount > 0) {
        parts.push(`${unclassifiedCount} relationship${unclassifiedCount !== 1 ? 's' : ''} not classified as containment or lineage`)
    }
    if (missingEntityCount > 0) {
        parts.push(`${missingEntityCount} entity type${missingEntityCount !== 1 ? 's' : ''} missing from the ontology`)
    }
    if (missingEdgeCount > 0) {
        parts.push(`${missingEdgeCount} edge type${missingEdgeCount !== 1 ? 's' : ''} missing from the ontology`)
    }
    if (resolution.hierarchyWarnings.length > 0) {
        parts.push(`${resolution.hierarchyWarnings.length} hierarchy warning${resolution.hierarchyWarnings.length !== 1 ? 's' : ''}`)
    }
    if (noContainment) {
        parts.push('No relationship is flagged as Containment — AGGREGATED edges will not propagate up the containment tree (only direct lineage endpoints)')
    }

    return (
        <div className={cn(
            'flex items-start gap-2.5 px-3.5 py-2.5 rounded-lg border text-xs',
            hasBlocking
                ? 'bg-amber-500/[0.06] border-amber-500/25 text-amber-500'
                : 'bg-blue-500/[0.05] border-blue-500/20 text-blue-400',
        )}>
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
            <div className="space-y-0.5">
                <div className="font-medium">
                    {hasBlocking
                        ? 'This ontology has gaps that will block aggregation'
                        : 'Hierarchy advisory'}
                </div>
                <ul className="text-[11px] opacity-90 space-y-0.5">
                    {parts.map((p, i) => <li key={i}>• {p}</li>)}
                </ul>
                {hasBlocking && (
                    <div className="text-[11px] opacity-80 pt-0.5">
                        Resolve in the next step (Schema Review) before completing onboarding.
                    </div>
                )}
            </div>
        </div>
    )
}
