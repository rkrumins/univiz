/**
 * SchemaReviewStep — gate between Semantic and Review.
 *
 * Per catalog item, runs the ontology-resolution gate against the
 * suggested-or-selected ontology and the source's introspected stats.
 * The user must classify every unclassified relationship as
 * containment / lineage / neither before the wizard can submit.
 * Hierarchy gaps surface as advisory warnings (yellow) but do not block.
 *
 * Published ontologies are immutable — saving classifications creates a
 * new draft version (POST .../new-version) and re-points the wizard
 * to the draft.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Database, Loader2, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { CatalogItemResponse } from '@/services/catalogService'
import { providerService } from '@/services/providerService'
import {
    ontologyResolutionService,
    type OntologyResolutionResponse,
    type OntologyResolutionRelGap,
} from '@/services/ontologyResolutionService'
import { ontologyDefinitionService } from '@/services/ontologyDefinitionService'
import type { OnboardingFormData } from '../AssetOnboardingWizard'

// ─── Types ──────────────────────────────────────────────────────────────

export type Classification = 'containment' | 'lineage' | 'neither'

export interface SchemaReviewSourceStatus {
    /** Ontology id at the time of the most recent check. May differ
     *  from formData.ontologySelections[itemId].ontologyId if the user
     *  saved classifications and we auto-created a draft. */
    ontologyId: string
    /** Latest gate report. null = not yet checked. */
    resolution: OntologyResolutionResponse | null
    /** User's pending classifications, keyed by relationship id. */
    classifications: Record<string, Classification>
    /** Loading flags. */
    loading: boolean
    saving: boolean
    /** Last error string if a save / refresh failed. */
    error: string | null
}

export type SchemaReviewStatusMap = Record<string, SchemaReviewSourceStatus>

interface Props {
    formData: OnboardingFormData
    updateFormData: (updates: Partial<OnboardingFormData> | ((prev: OnboardingFormData) => Partial<OnboardingFormData>)) => void
    catalogItems: CatalogItemResponse[]
    providerId: string
    ontologyNames: Record<string, string>
    /** Stays in sync with the parent so canProceed can read it. */
    statusMap: SchemaReviewStatusMap
    onStatusChange: (next: SchemaReviewStatusMap) => void
}

// ─── Helpers ────────────────────────────────────────────────────────────

const REASON_LABELS: Record<string, string> = {
    missing_entity_types: 'Missing entity types',
    missing_edge_types: 'Missing edge types',
    unclassified_relationships: 'Unclassified relationships',
    no_lineage: 'No lineage relationship',
    ontology_not_assigned: 'No ontology assigned',
}

function transformStatsForCheck(raw: any): Record<string, unknown> {
    // ``providerService.getAssetStats`` returns the cache envelope shape
    // — ``{ nodeCount, edgeCount, entityTypeCounts, edgeTypeCounts }``.
    // The /resolution-check endpoint expects ``GraphSchemaStats``:
    // ``{ totalNodes, totalEdges, entityTypeStats[], edgeTypeStats[] }``.
    // Mirror the transform that SemanticStep applies before calling
    // ``ontologyDefinitionService.suggest``.
    return {
        totalNodes: raw?.nodeCount ?? 0,
        totalEdges: raw?.edgeCount ?? 0,
        entityTypeStats: Object.entries(raw?.entityTypeCounts ?? {}).map(
            ([name, count]) => ({ id: name, name, count: count as number, sampleNames: [] }),
        ),
        edgeTypeStats: Object.entries(raw?.edgeTypeCounts ?? {}).map(
            ([name, count]) => ({ id: name, name, count: count as number, sourceTypes: [], targetTypes: [] }),
        ),
        tagStats: [],
    }
}

/** Build a minimal entity type definition for an introspected label.
 *  Hierarchy fields are populated with safe defaults (level 0, empty
 *  arrays) so the gate's hierarchy warnings don't immediately trip on
 *  freshly-added types. */
function buildSeedEntityDef(id: string): Record<string, unknown> {
    const human = id.includes('_')
        ? id.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase()).join(' ')
        : id.charAt(0).toUpperCase() + id.slice(1)
    return {
        name: human,
        plural_name: human + 's',
        description: `Entity type discovered in graph: ${id}`,
        visual: { icon: 'Box', color: '#6366f1' },
        hierarchy: { level: 0, can_contain: [], can_be_contained_by: [] },
        behavior: {},
        fields: [],
    }
}

/** Seed a relationship type definition without classification flags so
 *  the gate keeps it as ``unclassified`` and the user is forced to pick
 *  containment / lineage / neither via the radio. */
function buildSeedRelDef(id: string): Record<string, unknown> {
    const human = id.includes('_')
        ? id.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase()).join(' ')
        : id.charAt(0).toUpperCase() + id.slice(1)
    return {
        name: human,
        description: `Relationship type discovered in graph: ${id}`,
        category: 'association',
        // Intentionally NOT setting is_containment / is_lineage — leaves
        // the relationship in the "unclassified" bucket so the gate
        // forces the user to pick via the inline radio.
        source_types: [],
        target_types: [],
        visual: { stroke_color: '#6366f1', stroke_width: 2 },
    }
}

async function ensureDraftFor(
    ontologyId: string,
): Promise<{ id: string; isPublished: boolean }> {
    const orig = await ontologyDefinitionService.get(ontologyId)
    if (!orig.isPublished) return { id: orig.id, isPublished: false }
    try {
        const draft = await ontologyDefinitionService.createNewVersion(ontologyId)
        return { id: draft.id, isPublished: false }
    } catch (e) {
        // 409 on createNewVersion means a draft already exists for the
        // schema lineage — switch to it instead of failing.
        const versions = await ontologyDefinitionService.listVersions(ontologyId)
        const existingDraft = versions.find(v => !v.isPublished)
        if (existingDraft) return { id: existingDraft.id, isPublished: false }
        throw e
    }
}

// ─── Component ──────────────────────────────────────────────────────────

export function SchemaReviewStep({
    formData,
    updateFormData,
    catalogItems,
    providerId,
    ontologyNames,
    statusMap,
    onStatusChange,
}: Props) {
    const [expanded, setExpanded] = useState<Record<string, boolean>>({})
    const inFlightRef = useRef<Set<string>>(new Set())

    const itemsWithOntology = useMemo(
        () => catalogItems.filter(c => formData.ontologySelections[c.id]?.ontologyId),
        [catalogItems, formData.ontologySelections],
    )
    const itemsSkipped = useMemo(
        () => catalogItems.filter(c => !formData.ontologySelections[c.id]?.ontologyId),
        [catalogItems, formData.ontologySelections],
    )

    // ─── Resolution check (per item) ──────────────────────────────────

    const runCheck = useCallback(async (item: CatalogItemResponse) => {
        const ontologyId = formData.ontologySelections[item.id]?.ontologyId
        if (!ontologyId) return
        if (inFlightRef.current.has(item.id)) return
        inFlightRef.current.add(item.id)

        const next: SchemaReviewStatusMap = {
            ...statusMap,
            [item.id]: {
                ontologyId,
                resolution: statusMap[item.id]?.resolution ?? null,
                classifications: statusMap[item.id]?.classifications ?? {},
                loading: true,
                saving: false,
                error: null,
            },
        }
        onStatusChange(next)

        try {
            const assetName = item.sourceIdentifier || item.name
            const envelope = await providerService.getAssetStats(providerId, assetName)
            if (!envelope.data) {
                throw new Error(
                    envelope.meta.status === 'computing'
                        ? 'Graph stats are still being computed. Click Refresh in a few seconds.'
                        : envelope.meta.status === 'unavailable'
                            ? 'Background refresh queue is unreachable. Retry shortly.'
                            : 'No graph stats available yet.',
                )
            }
            const resolution = await ontologyResolutionService.previewForOntology(
                ontologyId,
                transformStatsForCheck(envelope.data),
            )
            const seeded: Record<string, Classification> = {
                ...(statusMap[item.id]?.classifications ?? {}),
            }
            for (const rel of resolution.unclassifiedRelationships) {
                if (!(rel.id in seeded)) {
                    // Default to "neither" so the user must intentionally pick.
                    seeded[rel.id] = 'neither'
                }
            }
            onStatusChange({
                ...next,
                [item.id]: {
                    ontologyId,
                    resolution,
                    classifications: seeded,
                    loading: false,
                    saving: false,
                    error: null,
                },
            })
        } catch (err) {
            onStatusChange({
                ...next,
                [item.id]: {
                    ...next[item.id],
                    loading: false,
                    error: err instanceof Error ? err.message : String(err),
                },
            })
        } finally {
            inFlightRef.current.delete(item.id)
        }
    }, [formData.ontologySelections, providerId, statusMap, onStatusChange])

    // Auto-check on first mount / ontology change.
    useEffect(() => {
        for (const item of itemsWithOntology) {
            const current = statusMap[item.id]
            const ontologyId = formData.ontologySelections[item.id]?.ontologyId
            if (!current || current.ontologyId !== ontologyId) {
                runCheck(item)
            }
        }
        // We intentionally exclude statusMap from deps — runCheck mutates it
        // and re-running would loop. The ontologyId guard above is enough.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [itemsWithOntology, formData.ontologySelections])

    const toggle = useCallback((id: string) => {
        setExpanded(prev => ({ ...prev, [id]: !prev[id] }))
    }, [])

    const setClassification = useCallback((itemId: string, relId: string, choice: Classification) => {
        const current = statusMap[itemId]
        if (!current) return
        onStatusChange({
            ...statusMap,
            [itemId]: {
                ...current,
                classifications: { ...current.classifications, [relId]: choice },
            },
        })
    }, [statusMap, onStatusChange])

    // ─── Auto-fix: add missing types to the assigned ontology ────────

    const addMissingTypes = useCallback(async (item: CatalogItemResponse) => {
        const status = statusMap[item.id]
        if (!status || !status.resolution) return
        const { missingEntityTypes, missingEdgeTypes } = status.resolution
        if (missingEntityTypes.length === 0 && missingEdgeTypes.length === 0) return

        const next: SchemaReviewStatusMap = {
            ...statusMap,
            [item.id]: { ...status, saving: true, error: null },
        }
        onStatusChange(next)

        try {
            const draftRef = await ensureDraftFor(status.ontologyId)
            // If the source ontology was published, we now own a draft
            // version. Re-point the wizard's selection so subsequent
            // saves write to the same draft (and avoids re-cloning).
            if (draftRef.id !== status.ontologyId) {
                updateFormData(prev => ({
                    ontologySelections: {
                        ...prev.ontologySelections,
                        [item.id]: {
                            ...prev.ontologySelections[item.id],
                            ontologyId: draftRef.id,
                        },
                    },
                }))
            }
            const draft = await ontologyDefinitionService.get(draftRef.id)
            const entityDefs = { ...(draft.entityTypeDefinitions as Record<string, any>) }
            const relDefs = { ...(draft.relationshipTypeDefinitions as Record<string, any>) }
            for (const eid of missingEntityTypes) {
                if (!entityDefs[eid]) entityDefs[eid] = buildSeedEntityDef(eid)
            }
            for (const rid of missingEdgeTypes) {
                if (!relDefs[rid]) relDefs[rid] = buildSeedRelDef(rid)
            }
            await ontologyDefinitionService.update(draftRef.id, {
                entityTypeDefinitions: entityDefs,
                relationshipTypeDefinitions: relDefs,
            })
            // Re-run the gate against the updated ontology so the
            // unclassifiedRelationships list reflects the newly-added
            // edges and the user can classify them in one go.
            const assetName = item.sourceIdentifier || item.name
            const envelope = await providerService.getAssetStats(providerId, assetName)
            const refreshed = await ontologyResolutionService.previewForOntology(
                draftRef.id,
                transformStatsForCheck(envelope.data ?? {}),
            )
            const seeded: Record<string, Classification> = { ...status.classifications }
            for (const rel of refreshed.unclassifiedRelationships) {
                if (!(rel.id in seeded)) seeded[rel.id] = 'neither'
            }
            onStatusChange({
                ...next,
                [item.id]: {
                    ontologyId: draftRef.id,
                    resolution: refreshed,
                    classifications: seeded,
                    loading: false,
                    saving: false,
                    error: null,
                },
            })
        } catch (err) {
            onStatusChange({
                ...next,
                [item.id]: {
                    ...next[item.id],
                    saving: false,
                    error: err instanceof Error ? err.message : String(err),
                },
            })
        }
    }, [statusMap, onStatusChange, updateFormData, providerId])

    // ─── Save classifications for one source ─────────────────────────

    const saveClassifications = useCallback(async (item: CatalogItemResponse) => {
        const status = statusMap[item.id]
        if (!status || !status.resolution) return

        const next: SchemaReviewStatusMap = {
            ...statusMap,
            [item.id]: { ...status, saving: true, error: null },
        }
        onStatusChange(next)

        try {
            // If the assigned ontology is published, create a fresh draft
            // version first and re-point the wizard at it. The
            // /new-version endpoint enforces "one draft per schema_id"
            // so we get the existing draft if one is already open.
            let workingId = status.ontologyId
            if (status.resolution.ontologyIsPublished) {
                const draft = await ontologyDefinitionService.createNewVersion(workingId)
                workingId = draft.id
                updateFormData(prev => ({
                    ontologySelections: {
                        ...prev.ontologySelections,
                        [item.id]: {
                            ...prev.ontologySelections[item.id],
                            ontologyId: workingId,
                        },
                    },
                }))
            }

            // Patch the relationship_type_definitions JSON in place.
            // We only touch the rels the user just classified — leave
            // the rest of the ontology alone.
            const draft = await ontologyDefinitionService.get(workingId)
            const relDefs = { ...(draft.relationshipTypeDefinitions as Record<string, any>) }
            for (const [relId, choice] of Object.entries(status.classifications)) {
                if (!relDefs[relId]) continue
                const isContainment = choice === 'containment'
                const isLineage = choice === 'lineage'
                relDefs[relId] = {
                    ...relDefs[relId],
                    is_containment: isContainment,
                    is_lineage: isLineage,
                    isContainment,
                    isLineage,
                }
            }
            await ontologyDefinitionService.update(workingId, {
                relationshipTypeDefinitions: relDefs,
            })

            // Re-run the gate to refresh resolved/blockingReasons.
            const assetName = item.sourceIdentifier || item.name
            const envelope = await providerService.getAssetStats(providerId, assetName)
            const refreshed = await ontologyResolutionService.previewForOntology(
                workingId,
                transformStatsForCheck(envelope.data ?? {}),
            )
            onStatusChange({
                ...next,
                [item.id]: {
                    ontologyId: workingId,
                    resolution: refreshed,
                    classifications: status.classifications,
                    loading: false,
                    saving: false,
                    error: null,
                },
            })
        } catch (err) {
            onStatusChange({
                ...next,
                [item.id]: {
                    ...next[item.id],
                    saving: false,
                    error: err instanceof Error ? err.message : String(err),
                },
            })
        }
    }, [statusMap, onStatusChange, updateFormData, providerId])

    const allResolved = itemsWithOntology.every(c => statusMap[c.id]?.resolution?.resolved)

    // ─── Render ───────────────────────────────────────────────────────

    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <h3 className="text-lg font-semibold text-slate-900 dark:text-white">
                    Schema Review
                </h3>
                <p className="text-sm text-slate-500 dark:text-slate-400">
                    Confirm every introspected entity and edge type is defined,
                    and that each relationship is classified as containment
                    or lineage. Aggregation cannot run until the gate passes.
                </p>
            </div>

            {itemsSkipped.length > 0 && (
                <div className="px-4 py-3 rounded-lg bg-slate-500/[0.04] border border-slate-500/10 text-xs text-slate-500">
                    {itemsSkipped.length} source{itemsSkipped.length !== 1 ? 's' : ''} skipped (no ontology selected) — aggregation will be skipped for these too.
                </div>
            )}

            <div className="space-y-3">
                {itemsWithOntology.map(item => {
                    const status = statusMap[item.id]
                    const resolution = status?.resolution
                    const ontologyId = formData.ontologySelections[item.id]?.ontologyId || ''
                    const ontologyName = ontologyNames[ontologyId] || ontologyId
                    const isOpen = expanded[item.id] ?? !resolution?.resolved
                    const blockingCount = resolution?.blockingReasons.length ?? 0

                    return (
                        <div
                            key={item.id}
                            className={cn(
                                "rounded-xl border bg-white dark:bg-slate-800/40 shadow-sm",
                                resolution?.resolved
                                    ? "border-emerald-500/20"
                                    : status?.error
                                        ? "border-red-500/30"
                                        : "border-amber-500/25",
                            )}
                        >
                            <button
                                type="button"
                                onClick={() => toggle(item.id)}
                                className="w-full flex items-center gap-3 px-4 py-3 text-left"
                            >
                                {isOpen ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
                                <Database className="w-4 h-4 text-slate-400" />
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2">
                                        <span className="text-sm font-medium text-slate-900 dark:text-white truncate">
                                            {item.name}
                                        </span>
                                        <span className="text-[11px] text-slate-400 truncate">
                                            → {ontologyName}{resolution?.ontologyIsPublished ? ' (published)' : ''}
                                        </span>
                                    </div>
                                </div>
                                {status?.loading ? (
                                    <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
                                ) : !resolution ? (
                                    <span className="flex items-center gap-1.5 text-xs text-slate-400">
                                        Evaluating…
                                    </span>
                                ) : resolution.resolved ? (
                                    <span className="flex items-center gap-1.5 text-xs text-emerald-500">
                                        <CheckCircle2 className="w-4 h-4" /> Resolved
                                    </span>
                                ) : (
                                    <span className="flex items-center gap-1.5 text-xs text-amber-500">
                                        <AlertTriangle className="w-4 h-4" />
                                        {blockingCount} issue{blockingCount !== 1 ? 's' : ''}
                                    </span>
                                )}
                            </button>

                            {isOpen && (
                                <div className="border-t border-slate-200 dark:border-slate-700 px-4 py-3 space-y-3">
                                    {status?.error && (
                                        <div className="px-3 py-2 rounded-md bg-red-500/[0.08] border border-red-500/20 text-xs text-red-500">
                                            {status.error}
                                        </div>
                                    )}

                                    {resolution && resolution.blockingReasons.length > 0 && (
                                        <div className="flex flex-wrap gap-1.5">
                                            {resolution.blockingReasons.map((reason, idx) => {
                                                // Defensive coercion — even though the API
                                                // contract is List[str], we never want a
                                                // stray object to surface as "[object Object]".
                                                const key = typeof reason === 'string' ? reason : String(idx)
                                                const label = typeof reason === 'string'
                                                    ? (REASON_LABELS[reason] ?? reason)
                                                    : 'Unknown issue'
                                                return (
                                                    <span
                                                        key={key}
                                                        className="text-[10px] uppercase tracking-wide font-semibold px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-500 border border-amber-500/20"
                                                    >
                                                        {label}
                                                    </span>
                                                )
                                            })}
                                        </div>
                                    )}

                                    {((resolution?.missingEntityTypes?.length ?? 0) > 0 ||
                                        (resolution?.missingEdgeTypes?.length ?? 0) > 0) && (
                                        <div className="space-y-2">
                                            {resolution!.missingEntityTypes.length > 0 && (
                                                <Section title="Missing entity types">
                                                    <ChipList items={resolution!.missingEntityTypes} tone="red" />
                                                </Section>
                                            )}
                                            {resolution!.missingEdgeTypes.length > 0 && (
                                                <Section title="Missing edge types">
                                                    <ChipList items={resolution!.missingEdgeTypes} tone="red" />
                                                </Section>
                                            )}
                                            <button
                                                type="button"
                                                onClick={() => addMissingTypes(item)}
                                                disabled={status?.saving}
                                                className={cn(
                                                    "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium",
                                                    status?.saving
                                                        ? "bg-slate-200 dark:bg-slate-700 text-slate-400 cursor-not-allowed"
                                                        : "bg-emerald-600 text-white hover:bg-emerald-700",
                                                )}
                                                title={resolution!.ontologyIsPublished
                                                    ? 'Creates a new draft version of the published ontology and adds the missing types.'
                                                    : 'Adds the missing entity / edge types to the ontology with safe defaults. New edge types will land as unclassified — pick containment / lineage below.'}
                                            >
                                                {status?.saving ? (
                                                    <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Adding…</>
                                                ) : (
                                                    <>Add missing types to ontology</>
                                                )}
                                            </button>
                                        </div>
                                    )}

                                    {resolution?.unclassifiedRelationships && resolution.unclassifiedRelationships.length > 0 && (
                                        <Section title={`Classify ${resolution.unclassifiedRelationships.length} relationship${resolution.unclassifiedRelationships.length !== 1 ? 's' : ''}`}>
                                            <div className="space-y-2">
                                                {resolution.unclassifiedRelationships.map(rel => (
                                                    <RelClassifier
                                                        key={rel.id}
                                                        rel={rel}
                                                        choice={status?.classifications[rel.id] ?? 'neither'}
                                                        onChange={(choice) => setClassification(item.id, rel.id, choice)}
                                                    />
                                                ))}
                                            </div>
                                        </Section>
                                    )}

                                    {resolution?.hierarchyWarnings && resolution.hierarchyWarnings.length > 0 && (
                                        <Section title="Hierarchy warnings (advisory)">
                                            <ul className="text-xs text-amber-500 space-y-0.5">
                                                {resolution.hierarchyWarnings.map((w, i) => (
                                                    <li key={`${w.entityType}-${w.missingField}-${i}`}>
                                                        <span className="font-mono">{w.entityType}</span> is missing <span className="font-mono">{w.missingField}</span>
                                                    </li>
                                                ))}
                                            </ul>
                                        </Section>
                                    )}

                                    {resolution && (resolution.advisoryWarnings ?? []).includes('no_containment_edges') && (
                                        <div className="px-3 py-2 rounded-md bg-amber-500/[0.08] border border-amber-500/20 text-xs text-amber-500 space-y-0.5">
                                            <div className="font-medium">No relationship is flagged as Containment</div>
                                            <div className="opacity-90">
                                                Aggregation will run but AGGREGATED edges will not propagate up the
                                                containment tree (only direct lineage endpoints will be linked).
                                                Mark at least one relationship as Containment above (or in the Schema page)
                                                to enable cross-tier roll-up.
                                            </div>
                                        </div>
                                    )}

                                    {resolution && (
                                        <div className="flex items-center justify-end gap-2 pt-1">
                                            <button
                                                type="button"
                                                onClick={() => runCheck(item)}
                                                disabled={status?.loading}
                                                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
                                            >
                                                <RefreshCw className={cn("w-3.5 h-3.5", status?.loading && "animate-spin")} />
                                                Refresh
                                            </button>
                                            {resolution.unclassifiedRelationships.length > 0 && (
                                                <button
                                                    type="button"
                                                    onClick={() => saveClassifications(item)}
                                                    disabled={status?.saving || !Object.values(status?.classifications ?? {}).some(v => v !== 'neither')}
                                                    className={cn(
                                                        "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium",
                                                        status?.saving
                                                            ? "bg-slate-200 dark:bg-slate-700 text-slate-400 cursor-not-allowed"
                                                            : "bg-indigo-600 text-white hover:bg-indigo-700",
                                                    )}
                                                >
                                                    {status?.saving ? (
                                                        <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Saving</>
                                                    ) : (
                                                        <>Save classifications</>
                                                    )}
                                                </button>
                                            )}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    )
                })}
            </div>

            {itemsWithOntology.length > 0 && (
                <div className={cn(
                    "px-4 py-3 rounded-lg border text-sm",
                    allResolved
                        ? "bg-emerald-500/[0.06] border-emerald-500/20 text-emerald-600 dark:text-emerald-400"
                        : "bg-amber-500/[0.06] border-amber-500/20 text-amber-500",
                )}>
                    {allResolved
                        ? 'All sources resolved — ready to submit.'
                        : 'Resolve every blocking issue above before continuing.'}
                </div>
            )}
        </div>
    )
}

// ─── Sub-components ─────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
    return (
        <div className="space-y-1.5">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                {title}
            </h4>
            {children}
        </div>
    )
}

function ChipList({ items, tone }: { items: string[]; tone: 'red' | 'amber' }) {
    const cls = tone === 'red'
        ? 'bg-red-500/10 text-red-500 border-red-500/20'
        : 'bg-amber-500/10 text-amber-500 border-amber-500/20'
    return (
        <div className="flex flex-wrap gap-1.5">
            {items.map(it => (
                <span key={it} className={cn('text-[11px] px-2 py-0.5 rounded-full border font-mono', cls)}>
                    {it}
                </span>
            ))}
        </div>
    )
}

function RelClassifier({
    rel,
    choice,
    onChange,
}: {
    rel: OntologyResolutionRelGap
    choice: Classification
    onChange: (c: Classification) => void
}) {
    const options: { value: Classification; label: string }[] = [
        { value: 'containment', label: 'Containment' },
        { value: 'lineage', label: 'Lineage' },
        { value: 'neither', label: 'Neither' },
    ]
    return (
        <div className="flex items-center gap-3 px-3 py-2 rounded-md bg-slate-50 dark:bg-slate-900/40 border border-slate-200 dark:border-slate-700">
            <div className="flex-1 min-w-0">
                <div className="text-xs font-mono text-slate-700 dark:text-slate-200 truncate">{rel.id}</div>
                <div className="text-[11px] text-slate-500 dark:text-slate-400 truncate">{rel.name}</div>
            </div>
            <div className="flex gap-1">
                {options.map(opt => (
                    <button
                        key={opt.value}
                        type="button"
                        onClick={() => onChange(opt.value)}
                        className={cn(
                            'px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors',
                            choice === opt.value
                                ? 'bg-indigo-600 text-white'
                                : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-700',
                        )}
                    >
                        {opt.label}
                    </button>
                ))}
            </div>
        </div>
    )
}
