/**
 * AssetOnboardingWizard — Multi-step onboarding wizard that follows the ViewWizard pattern.
 * Triggered after registering catalog items in RegistryAssets.
 * Steps: Workspace Allocation → Aggregation Strategy → Semantic Layer → Review & Confirm.
 *
 * Architecture mirrors ViewWizard.tsx: centralized formData, canProceed via useMemo,
 * spring animations, AnimatePresence step transitions, previousSteps stack.
 *
 * Enhancements: keyboard navigation, step summary pills, toast micro-feedback,
 * unsaved changes warning, structured error recovery, live aggregation tracking.
 */
import { useState, useMemo, useCallback, useEffect, useRef, startTransition } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Database, Settings, BookOpen, Check, ChevronLeft, ChevronRight, Loader2, X, Wand2, AlertTriangle, ShieldCheck } from 'lucide-react'
import { cn } from '@/lib/utils'
import { workspaceService } from '@/services/workspaceService'
import { catalogService, type CatalogItemResponse } from '@/services/catalogService'
import type { ProviderResponse } from '@/services/providerService'
import { useWorkspacesStore } from '@/store/workspaces'
import { useToast } from '@/components/ui/toast'

import { WorkspaceStep } from './steps/WorkspaceStep'
import { AggregationStep } from './steps/AggregationStep'
import { SemanticStep } from './steps/SemanticStep'
import { SchemaReviewStep, type SchemaReviewStatusMap } from './steps/SchemaReviewStep'
import { ReviewStep, type NavigationDestination } from './steps/ReviewStep'
import { aggregationService } from '@/services/aggregationService'
import { useWizardKeyboard } from './hooks/useWizardKeyboard'

// ─── Types ────────────────────────────────────────────────────────────────────

export interface OnboardingFormData {
    allocations: Record<string, {
        workspaceId: string      // '' = unselected, 'new' = create new
        newWorkspaceName: string
        newWorkspaceDescription: string
    }>
    projectionMode: 'in_source' | 'dedicated' | 'skip'
    dedicatedStrategy: 'full_copy' | 'containment_only'
    dedicatedGraphName: string
    advancedConfig: {
        batchSize: number       // 100–50,000, default 5000
        maxRetries: number      // 0–10, default 3
        timeoutMinutes: number | null  // null = 2hr default
    }
    ontologySelections: Record<string, {
        ontologyId: string       // '' = unselected
        suggestedOntology: any | null
        coverageStats: any | null
    }>
}

type WizardStep = 'workspace' | 'aggregation' | 'semantic' | 'schemaReview' | 'review'

interface AssetOnboardingWizardProps {
    provider: ProviderResponse
    catalogItems: CatalogItemResponse[]
    isOpen: boolean
    onComplete: () => void
    onClose: () => void
}

// ─── Step Config ──────────────────────────────────────────────────────────────

const STEPS: { id: WizardStep; title: string; icon: typeof Database }[] = [
    { id: 'workspace', title: 'Workspace', icon: Database },
    { id: 'aggregation', title: 'Aggregation', icon: Settings },
    { id: 'semantic', title: 'Semantic Layer', icon: BookOpen },
    { id: 'schemaReview', title: 'Schema Review', icon: ShieldCheck },
    { id: 'review', title: 'Review', icon: Check },
]

// ─── Component ────────────────────────────────────────────────────────────────

export function AssetOnboardingWizard({
    provider,
    catalogItems,
    isOpen,
    onComplete,
    onClose,
}: AssetOnboardingWizardProps) {
    const navigate = useNavigate()
    const { setActiveWorkspace, setActiveDataSource } = useWorkspacesStore()
    const { showToast } = useToast()
    const modalRef = useRef<HTMLDivElement>(null)

    // ─── Form State ───────────────────────────────────────────────────────────
    const [formData, setFormData] = useState<OnboardingFormData>(() => ({
        allocations: Object.fromEntries(
            catalogItems.map(c => [c.id, { workspaceId: '', newWorkspaceName: '', newWorkspaceDescription: '' }])
        ),
        projectionMode: 'in_source',
        dedicatedStrategy: 'full_copy',
        dedicatedGraphName: '',
        advancedConfig: { batchSize: 1000, maxRetries: 3, timeoutMinutes: null },
        ontologySelections: Object.fromEntries(
            catalogItems.map(c => [c.id, { ontologyId: '', suggestedOntology: null, coverageStats: null }])
        ),
    }))

    // ─── Navigation State ─────────────────────────────────────────────────────
    const [currentStep, setCurrentStep] = useState<WizardStep>('workspace')
    const [previousSteps, setPreviousSteps] = useState<WizardStep[]>([])
    const [isSubmitting, setIsSubmitting] = useState(false)
    const [submitError, setSubmitError] = useState<string | null>(null)
    const [wizardPhase, setWizardPhase] = useState<'steps' | 'success'>('steps')
    const [showCloseConfirm, setShowCloseConfirm] = useState(false)

    // Direction tracking for step transition animations (1 = forward, -1 = back)
    const [stepDirection, setStepDirection] = useState(1)

    // Track created workspace/ds IDs for success screen navigation
    const [createdContext, setCreatedContext] = useState<{ wsId: string; dsId: string } | null>(null)
    const [createdDataSourceIds, setCreatedDataSourceIds] = useState<string[]>([])

    // Workspace + ontology name maps for ReviewStep / SemanticStep display
    const [workspaceNames, setWorkspaceNames] = useState<Record<string, string>>({})
    const [ontologyNames, setOntologyNames] = useState<Record<string, string>>({})

    // SchemaReview gate state — driven by SchemaReviewStep, read by canProceed.
    const [schemaReviewStatus, setSchemaReviewStatus] = useState<SchemaReviewStatusMap>({})

    // Loaded workspace names (from WorkspaceStep's API call) — used throughout wizard
    const [loadedWorkspaceNames, setLoadedWorkspaceNames] = useState<Record<string, string>>({})

    // ─── Dirty tracking for unsaved changes warning ───────────────────────────
    const isDirty = useMemo(() => {
        const hasAllocations = Object.values(formData.allocations).some(a => a.workspaceId !== '')
        const hasOntologies = Object.values(formData.ontologySelections).some(s => s.ontologyId !== '')
        const changedProjection = formData.projectionMode !== 'in_source'
        return hasAllocations || hasOntologies || changedProjection
    }, [formData])

    // Reset state when wizard opens
    useEffect(() => {
        if (isOpen) {
            setCurrentStep('workspace')
            setPreviousSteps([])
            setIsSubmitting(false)
            setSubmitError(null)
            setWizardPhase('steps')
            setCreatedContext(null)
            setCreatedDataSourceIds([])
            setShowCloseConfirm(false)
            setSchemaReviewStatus({})
            setFormData({
                allocations: Object.fromEntries(
                    catalogItems.map(c => [c.id, { workspaceId: '', newWorkspaceName: '', newWorkspaceDescription: '' }])
                ),
                projectionMode: 'in_source',
                dedicatedStrategy: 'full_copy',
                dedicatedGraphName: '',
                advancedConfig: { batchSize: 1000, maxRetries: 3, timeoutMinutes: null },
                ontologySelections: Object.fromEntries(
                    catalogItems.map(c => [c.id, { ontologyId: '', suggestedOntology: null, coverageStats: null }])
                ),
            })
        }
    }, [isOpen, catalogItems])

    // ─── Form Data Update ─────────────────────────────────────────────────────
    const updateFormData = useCallback((updates: Partial<OnboardingFormData> | ((prev: OnboardingFormData) => Partial<OnboardingFormData>)) => {
        setFormData(prev => {
            const resolved = typeof updates === 'function' ? updates(prev) : updates
            return { ...prev, ...resolved }
        })
    }, [])

    // ─── Validation ───────────────────────────────────────────────────────────
    const canProceed = useMemo(() => {
        switch (currentStep) {
            case 'workspace':
                return Object.values(formData.allocations).every(a =>
                    a.workspaceId !== '' && (a.workspaceId !== 'new' || a.newWorkspaceName.trim().length > 0)
                )
            case 'aggregation':
                return true // default 'in_source' always selected
            case 'semantic':
                // Allow proceeding if at least one ontology is set OR all are explicitly left empty (skipped)
                return Object.values(formData.ontologySelections).some(s => s.ontologyId !== '') ||
                    Object.values(formData.ontologySelections).every(s => s.ontologyId === '')
            case 'schemaReview': {
                // Every catalog item that has an ontology assigned must
                // pass the resolution gate. Items without an ontology
                // (skipped) bypass aggregation entirely so they don't
                // need to be resolved. Hierarchy warnings stay advisory.
                const required = catalogItems.filter(c => formData.ontologySelections[c.id]?.ontologyId)
                if (required.length === 0) return true
                return required.every(c => schemaReviewStatus[c.id]?.resolution?.resolved === true)
            }
            case 'review':
                return true
        }
    }, [currentStep, formData, catalogItems, schemaReviewStatus])

    // ─── Step Warnings (inline validation) ────────────────────────────────────
    const stepWarnings = useMemo((): string[] => {
        switch (currentStep) {
            case 'workspace':
                return catalogItems
                    .filter(c => {
                        const a = formData.allocations[c.id]
                        return !a || a.workspaceId === '' || (a.workspaceId === 'new' && !a.newWorkspaceName.trim())
                    })
                    .map(c => `"${c.name}" is not yet assigned`)
            case 'semantic':
                return catalogItems
                    .filter(c => !formData.ontologySelections[c.id]?.ontologyId)
                    .map(c => `"${c.name}" has no ontology selected`)
            case 'schemaReview':
                return catalogItems
                    .filter(c => {
                        const ontologyId = formData.ontologySelections[c.id]?.ontologyId
                        if (!ontologyId) return false
                        return !schemaReviewStatus[c.id]?.resolution?.resolved
                    })
                    .map(c => `"${c.name}" has unresolved ontology gaps`)
            default:
                return []
        }
    }, [currentStep, formData, catalogItems, schemaReviewStatus])

    // ─── Step Summary (mini-text under completed step pills) ────────────────
    const getStepSummary = useCallback((stepId: WizardStep): string | null => {
        switch (stepId) {
            case 'workspace': {
                const uniqueWs = new Set(
                    Object.values(formData.allocations)
                        .map(a => a.workspaceId === 'new' ? `new:${a.newWorkspaceName}` : a.workspaceId)
                        .filter(Boolean)
                )
                if (uniqueWs.size === 0) return null
                if (uniqueWs.size === 1) {
                    const first = Object.values(formData.allocations).find(a => a.workspaceId)
                    const label = first?.workspaceId === 'new' ? first.newWorkspaceName : (loadedWorkspaceNames[first?.workspaceId || ''] || '')
                    return label ? `${catalogItems.length} → ${label}` : `${uniqueWs.size} workspace`
                }
                return `${uniqueWs.size} workspaces`
            }
            case 'aggregation':
                return formData.projectionMode === 'in_source' ? 'In-Source'
                    : formData.projectionMode === 'dedicated' ? 'Dedicated'
                    : 'Skipped'
            case 'semantic': {
                const configured = Object.values(formData.ontologySelections).filter(s => s.ontologyId).length
                return `${configured}/${catalogItems.length} configured`
            }
            case 'schemaReview': {
                const required = catalogItems.filter(c => formData.ontologySelections[c.id]?.ontologyId)
                if (required.length === 0) return 'No sources to review'
                const resolved = required.filter(c => schemaReviewStatus[c.id]?.resolution?.resolved).length
                return `${resolved}/${required.length} resolved`
            }
            default:
                return null
        }
    }, [formData, catalogItems, loadedWorkspaceNames, schemaReviewStatus])

    // ─── Navigation ───────────────────────────────────────────────────────────
    const currentStepIndex = STEPS.findIndex(s => s.id === currentStep)

    const goNext = useCallback(() => {
        if (!canProceed) return
        const nextIndex = currentStepIndex + 1
        if (nextIndex < STEPS.length) {
            // Toast micro-feedback for completed step
            const stepId = currentStep
            if (stepId === 'workspace') {
                showToast('success', 'Workspace allocation saved')
            } else if (stepId === 'aggregation') {
                showToast('success', `Aggregation: ${formData.projectionMode === 'in_source' ? 'In-source' : formData.projectionMode === 'dedicated' ? 'Dedicated' : 'Skipped'} selected`)
            } else if (stepId === 'semantic') {
                const count = Object.values(formData.ontologySelections).filter(s => s.ontologyId !== '').length
                showToast('success', `Semantic layer configured for ${count} source${count !== 1 ? 's' : ''}`)
            } else if (stepId === 'schemaReview') {
                const required = catalogItems.filter(c => formData.ontologySelections[c.id]?.ontologyId).length
                showToast('success', `Schema review passed for ${required} source${required !== 1 ? 's' : ''}`)
            }

            // startTransition keeps the click responsive while the next step
            // mounts. Without this, INP on the Next button hits 300 ms+ on
            // heavy steps because mount work runs synchronously before paint.
            startTransition(() => {
                setStepDirection(1)
                setPreviousSteps(prev => [...prev, currentStep])
                setCurrentStep(STEPS[nextIndex].id)
            })
        }
    }, [canProceed, currentStepIndex, currentStep, formData, showToast, catalogItems])

    const goBack = useCallback(() => {
        if (previousSteps.length > 0) {
            const prev = previousSteps[previousSteps.length - 1]
            startTransition(() => {
                setStepDirection(-1)
                setPreviousSteps(ps => ps.slice(0, -1))
                setCurrentStep(prev)
            })
        }
    }, [previousSteps])

    const goToStep = useCallback((stepId: WizardStep) => {
        const targetIndex = STEPS.findIndex(s => s.id === stepId)
        if (targetIndex < currentStepIndex) {
            startTransition(() => {
                setStepDirection(-1)
                setPreviousSteps(prev => prev.slice(0, targetIndex))
                setCurrentStep(stepId)
            })
        }
    }, [currentStepIndex])

    // ─── Close with unsaved changes warning ───────────────────────────────────
    const handleClose = useCallback(() => {
        if (wizardPhase === 'success') {
            onComplete()
            return
        }
        if (isDirty) {
            setShowCloseConfirm(true)
        } else {
            onClose()
        }
    }, [wizardPhase, isDirty, onClose, onComplete])

    const confirmClose = useCallback(() => {
        setShowCloseConfirm(false)
        onClose()
    }, [onClose])

    // ─── Submit ───────────────────────────────────────────────────────────────
    const handleSubmit = useCallback(async () => {
        setIsSubmitting(true)
        setSubmitError(null)
        try {
            // Step 1: Register catalog items (idempotent — backend returns existing if duplicate)
            const realCatalogItems: CatalogItemResponse[] = await Promise.all(
                catalogItems.map(placeholder =>
                    catalogService.create({
                        providerId: provider.id,
                        sourceIdentifier: placeholder.sourceIdentifier || placeholder.name,
                        name: placeholder.name,
                        permittedWorkspaces: ['*'],
                    })
                )
            )

            // Build a map from placeholder id → real catalog item
            const placeholderToReal = new Map<string, CatalogItemResponse>()
            catalogItems.forEach((placeholder, i) => {
                placeholderToReal.set(placeholder.id, realCatalogItems[i])
            })

            // Step 2: Group real catalog items by workspace destination
            const groups = new Map<string, { items: CatalogItemResponse[]; placeholderIds: string[]; alloc: typeof formData.allocations[string] }>()
            for (const placeholder of catalogItems) {
                const alloc = formData.allocations[placeholder.id]
                const real = placeholderToReal.get(placeholder.id)!
                const key = alloc.workspaceId === 'new' ? `new:${alloc.newWorkspaceName}` : alloc.workspaceId
                if (!groups.has(key)) groups.set(key, { items: [], placeholderIds: [], alloc })
                const group = groups.get(key)!
                group.items.push(real)
                group.placeholderIds.push(placeholder.id)
            }

            let firstWsId = ''
            let firstDsId = ''
            const wsNameMap: Record<string, string> = {}
            const allCreatedDsIds: string[] = []

            // Step 3: Create workspaces / add data sources
            for (const [key, group] of groups) {
                const isNew = key.startsWith('new:')
                let wsId: string

                if (isNew) {
                    const ws = await workspaceService.create({
                        name: group.alloc.newWorkspaceName.trim(),
                        description: group.alloc.newWorkspaceDescription.trim() || undefined,
                        dataSources: group.items.map((c, i) => ({
                            catalogItemId: c.id,
                            ontologyId: formData.ontologySelections[group.placeholderIds[i]]?.ontologyId || undefined,
                            label: c.name || c.sourceIdentifier || undefined,
                        })),
                    })
                    wsId = ws.id
                    wsNameMap[wsId] = ws.name
                    for (const ds of ws.dataSources) {
                        allCreatedDsIds.push(ds.id)
                    }
                    if (!firstWsId) {
                        firstWsId = ws.id
                        firstDsId = ws.dataSources[0]?.id || ''
                    }
                    if (formData.projectionMode === 'dedicated') {
                        for (const ds of ws.dataSources) {
                            await workspaceService.setProjectionMode(wsId, ds.id, 'dedicated')
                            if (formData.dedicatedGraphName) {
                                await workspaceService.updateDataSource(wsId, ds.id, {
                                    dedicatedGraphName: formData.dedicatedGraphName,
                                })
                            }
                        }
                    }
                } else {
                    wsId = group.alloc.workspaceId
                    // Resolve name for existing workspaces (from loaded names or fetch)
                    if (!wsNameMap[wsId] && loadedWorkspaceNames[wsId]) {
                        wsNameMap[wsId] = loadedWorkspaceNames[wsId]
                    }
                    for (let i = 0; i < group.items.length; i++) {
                        const c = group.items[i]
                        const placeholderId = group.placeholderIds[i]
                        const ds = await workspaceService.addDataSource(wsId, {
                            catalogItemId: c.id,
                            ontologyId: formData.ontologySelections[placeholderId]?.ontologyId || undefined,
                            label: c.name || c.sourceIdentifier || undefined,
                        })
                        allCreatedDsIds.push(ds.id)
                        if (!firstWsId) {
                            firstWsId = wsId
                            firstDsId = ds.id
                        }
                        if (formData.projectionMode === 'dedicated') {
                            await workspaceService.setProjectionMode(wsId, ds.id, 'dedicated')
                            if (formData.dedicatedGraphName) {
                                await workspaceService.updateDataSource(wsId, ds.id, {
                                    dedicatedGraphName: formData.dedicatedGraphName,
                                })
                            }
                        }
                    }
                }
            }

            // Step 4: Aggregation — skip or trigger depending on mode
            for (const [key, group] of groups) {
                const wsId = key.startsWith('new:')
                    ? (Object.keys(wsNameMap).find(id => wsNameMap[id] === group.alloc.newWorkspaceName) || '')
                    : group.alloc.workspaceId

                if (!wsId) continue

                try {
                    const ws = await workspaceService.get(wsId)
                    for (let i = 0; i < group.items.length; i++) {
                        const catalogId = group.items[i].id
                        const ds = ws.dataSources.find(d => d.catalogItemId === catalogId)
                        if (ds) {
                            if (formData.projectionMode === 'skip') {
                                await aggregationService.skipAggregation(ds.id)
                            } else {
                                await aggregationService.triggerAggregation(ds.id, {
                                    projectionMode: formData.projectionMode,
                                    batchSize: formData.advancedConfig.batchSize,
                                    maxRetries: formData.advancedConfig.maxRetries,
                                    timeoutSecs: formData.advancedConfig.timeoutMinutes
                                        ? formData.advancedConfig.timeoutMinutes * 60
                                        : undefined,
                                }, 'onboarding')
                            }
                        }
                    }
                } catch (aggErr) {
                    console.error('Failed to trigger/skip aggregation:', aggErr)
                }
            }

            setCreatedContext({ wsId: firstWsId, dsId: firstDsId })
            setCreatedDataSourceIds(allCreatedDsIds)
            setWorkspaceNames(wsNameMap)
            setWizardPhase('success')
        } catch (err) {
            console.error('Onboarding failed:', err)
            const message = err instanceof Error ? err.message : 'Unknown error'
            const detailMatch = message.match(/\{"detail":"(.+?)"\}/)
            setSubmitError(detailMatch ? detailMatch[1] : message)
        } finally {
            setIsSubmitting(false)
        }
    }, [catalogItems, formData, provider.id, loadedWorkspaceNames])

    // ─── Success Navigation ───────────────────────────────────────────────────
    const handleNavigate = useCallback((destination: NavigationDestination) => {
        if (createdContext) {
            if (destination === 'explorer' || destination === 'schema') {
                setActiveWorkspace(createdContext.wsId)
                setActiveDataSource(createdContext.dsId)
            }
        }
        onComplete()

        switch (destination) {
            case 'explorer':
                navigate(`/explorer?workspace=${createdContext?.wsId}`)
                break
            case 'schema':
                navigate(`/schema?workspaceId=${createdContext?.wsId}&dataSourceId=${createdContext?.dsId}`)
                break
            case 'aggregation-jobs':
                navigate('/ingestion?tab=jobs')
                break
            case 'configure-more':
                navigate('/ingestion?tab=assets')
                break
            case 'workspaces':
                navigate('/workspaces')
                break
            case 'dismiss':
                // Stay on current page — just close
                break
        }
    }, [createdContext, navigate, onComplete, setActiveWorkspace, setActiveDataSource])

    // ─── Keyboard Navigation ──────────────────────────────────────────────────
    useWizardKeyboard({
        containerRef: modalRef,
        onClose: handleClose,
        onNext: goNext,
        onSubmit: handleSubmit,
        canProceed: !!canProceed,
        isLastStep: currentStepIndex === STEPS.length - 1,
        isSubmitting,
        isSuccess: wizardPhase === 'success',
        isOpen,
    })

    // ─── Render ───────────────────────────────────────────────────────────────
    if (!isOpen) return null

    const isLast = currentStepIndex === STEPS.length - 1

    return (
        <AnimatePresence>
            <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60"
            >
                <motion.div
                    ref={modalRef}
                    initial={{ scale: 0.95, opacity: 0, y: 20 }}
                    animate={{ scale: 1, opacity: 1, y: 0 }}
                    exit={{ scale: 0.95, opacity: 0, y: 20 }}
                    transition={{ duration: 0.12 }}
                    className="w-full max-w-4xl mx-4 bg-white dark:bg-slate-900 rounded-2xl shadow-lg overflow-hidden flex flex-col max-h-[90vh]"
                >
                    {/* Header — aligned with ViewWizard */}
                    <div className="flex items-center justify-between px-8 py-5 border-b border-slate-200 dark:border-slate-700 bg-gradient-to-r from-slate-50 to-white dark:from-slate-800 dark:to-slate-900 shrink-0">
                        <div className="flex items-center gap-4">
                            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-white shadow-md flex-shrink-0">
                                <Wand2 className="w-6 h-6" />
                            </div>
                            <div>
                                <h2 className="text-xl font-bold text-slate-900 dark:text-white">Asset Onboarding</h2>
                                <p className="text-sm text-slate-500">
                                    {wizardPhase === 'steps'
                                        ? `Step ${currentStepIndex + 1} of ${STEPS.length}: ${STEPS[currentStepIndex]?.title}`
                                        : `${catalogItems.length} data source${catalogItems.length !== 1 ? 's' : ''} from ${provider.name}`
                                    }
                                </p>
                            </div>
                        </div>
                        <button
                            onClick={handleClose}
                            className="p-2 rounded-lg bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors"
                            title={wizardPhase === 'success' ? 'Close' : 'Cancel onboarding'}
                        >
                            <X className="w-5 h-5 text-slate-500" />
                        </button>
                    </div>

                    {/* Progress Steps — aligned with ViewWizard */}
                    {wizardPhase === 'steps' && (
                        <div className="px-8 py-4 bg-slate-50 dark:bg-slate-800/50 border-b border-slate-200 dark:border-slate-700 shrink-0">
                            <div className="flex items-center gap-2">
                                {STEPS.map((step, i) => {
                                    const isComplete = i < currentStepIndex
                                    const isCurrent = i === currentStepIndex
                                    const isClickable = isComplete || isCurrent
                                    const summary = isComplete ? getStepSummary(step.id) : null
                                    return (
                                        <div key={step.id} className="flex items-center">
                                            <div className="flex flex-col items-center">
                                                <button
                                                    onClick={() => isComplete ? goToStep(step.id) : undefined}
                                                    disabled={!isClickable}
                                                    className={cn(
                                                        "flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-medium transition-colors duration-150",
                                                        isCurrent
                                                            ? "bg-indigo-600 text-white shadow-md ring-2 ring-indigo-100 dark:ring-indigo-900"
                                                            : isComplete
                                                                ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-900/20 dark:text-emerald-400 hover:bg-emerald-100 dark:hover:bg-emerald-900/30 cursor-pointer"
                                                                : "text-slate-400 dark:text-slate-500 cursor-not-allowed",
                                                    )}
                                                >
                                                    {isComplete
                                                        ? <Check className="w-4 h-4" />
                                                        : (
                                                            <span className={cn(
                                                                "w-4 h-4 flex items-center justify-center rounded-full text-[10px] font-bold border",
                                                                isCurrent ? "border-transparent bg-white/20" : "border-slate-300 dark:border-slate-600",
                                                            )}>
                                                                {i + 1}
                                                            </span>
                                                        )}
                                                    {step.title}
                                                </button>
                                                {summary && (
                                                    <span className="text-[9px] text-emerald-600/70 dark:text-emerald-400/60 font-medium mt-0.5 max-w-[120px] truncate">
                                                        {summary}
                                                    </span>
                                                )}
                                            </div>
                                            {i < STEPS.length - 1 && (
                                                <div className={cn(
                                                    "w-8 h-px mx-2",
                                                    isComplete ? "bg-emerald-400" : "bg-slate-200 dark:bg-slate-700",
                                                )} />
                                            )}
                                        </div>
                                    )
                                })}
                            </div>
                        </div>
                    )}

                    {/* Step Content */}
                    <div className="flex-1 overflow-y-auto min-h-[520px]">
                        <AnimatePresence mode="wait">
                            <motion.div
                                key={wizardPhase === 'success' ? 'success' : currentStep}
                                initial={{ opacity: 0, x: stepDirection * 20 }}
                                animate={{ opacity: 1, x: 0 }}
                                exit={{ opacity: 0, x: stepDirection * -20 }}
                                transition={{ duration: 0.06 }}
                                className="p-8"
                            >
                                {wizardPhase === 'success' ? (
                                    <ReviewStep
                                        formData={formData}
                                        catalogItems={catalogItems}
                                        phase="success"
                                        onNavigate={handleNavigate}
                                        workspaceNames={{ ...loadedWorkspaceNames, ...workspaceNames }}
                                        ontologyNames={ontologyNames}
                                        createdDataSourceIds={createdDataSourceIds}
                                    />
                                ) : currentStep === 'workspace' ? (
                                    <WorkspaceStep
                                        formData={formData}
                                        updateFormData={updateFormData}
                                        catalogItems={catalogItems}
                                        onWorkspacesLoaded={setLoadedWorkspaceNames}
                                    />
                                ) : currentStep === 'aggregation' ? (
                                    <AggregationStep
                                        formData={formData}
                                        updateFormData={updateFormData}
                                        catalogItems={catalogItems}
                                    />
                                ) : currentStep === 'semantic' ? (
                                    <SemanticStep
                                        formData={formData}
                                        updateFormData={updateFormData}
                                        catalogItems={catalogItems}
                                        providerId={provider.id}
                                        workspaceNames={loadedWorkspaceNames}
                                        onOntologiesLoaded={setOntologyNames}
                                    />
                                ) : currentStep === 'schemaReview' ? (
                                    <SchemaReviewStep
                                        formData={formData}
                                        updateFormData={updateFormData}
                                        catalogItems={catalogItems}
                                        providerId={provider.id}
                                        ontologyNames={ontologyNames}
                                        statusMap={schemaReviewStatus}
                                        onStatusChange={setSchemaReviewStatus}
                                    />
                                ) : currentStep === 'review' ? (
                                    <ReviewStep
                                        formData={formData}
                                        catalogItems={catalogItems}
                                        phase="review"
                                        onNavigate={handleNavigate}
                                        workspaceNames={{ ...loadedWorkspaceNames, ...workspaceNames }}
                                        ontologyNames={ontologyNames}
                                    />
                                ) : null}
                            </motion.div>
                        </AnimatePresence>
                    </div>

                    {/* Step warnings (inline validation) */}
                    {wizardPhase === 'steps' && stepWarnings.length > 0 && stepWarnings.length <= 3 && (
                        <div className="mx-8 mb-2 px-4 py-2.5 rounded-lg bg-amber-500/[0.06] border border-amber-500/15 space-y-1">
                            {stepWarnings.map((w, i) => (
                                <div key={i} className="flex items-center gap-2 text-[11px] text-amber-500">
                                    <AlertTriangle className="w-3 h-3 flex-shrink-0" />
                                    <span>{w}</span>
                                </div>
                            ))}
                        </div>
                    )}

                    {/* Error banner */}
                    {submitError && wizardPhase === 'steps' && (
                        <div className="mx-8 mb-2 px-4 py-3 rounded-lg bg-red-500/10 border border-red-500/20 text-sm text-red-600 dark:text-red-400 flex items-center justify-between">
                            <span>{submitError}</span>
                            <div className="flex items-center gap-2 ml-3">
                                <button
                                    onClick={handleSubmit}
                                    className="text-xs font-medium text-red-400 hover:text-red-300 underline transition-colors"
                                >
                                    Retry
                                </button>
                                <button
                                    onClick={() => setSubmitError(null)}
                                    className="text-red-400 hover:text-red-600 dark:hover:text-red-300"
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            </div>
                        </div>
                    )}

                    {/* Footer — aligned with ViewWizard */}
                    {wizardPhase === 'steps' && (
                        <div className="flex items-center justify-between px-8 py-5 border-t border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 shrink-0">
                            <button
                                onClick={goBack}
                                disabled={currentStepIndex === 0}
                                className={cn(
                                    "flex items-center gap-2 px-5 py-2.5 rounded-xl font-medium transition-colors duration-150",
                                    currentStepIndex > 0
                                        ? "text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700"
                                        : "text-slate-400 dark:text-slate-500 cursor-not-allowed",
                                )}
                            >
                                <ChevronLeft className="w-4 h-4" />
                                Back
                            </button>

                            <div className="flex items-center gap-3">
                                <button
                                    onClick={handleClose}
                                    className="px-5 py-2.5 rounded-xl font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors duration-150"
                                >
                                    Cancel
                                </button>

                                <button
                                    onClick={isLast ? handleSubmit : goNext}
                                    disabled={!canProceed || isSubmitting}
                                    className={cn(
                                        "flex items-center gap-2 px-6 py-2.5 rounded-xl font-medium transition-colors duration-150",
                                        canProceed && !isSubmitting
                                            ? "bg-gradient-to-r from-indigo-600 to-violet-600 text-white hover:from-indigo-700 hover:to-violet-700 shadow-md"
                                            : "bg-slate-200 dark:bg-slate-700 text-slate-400 cursor-not-allowed",
                                    )}
                                >
                                    {isSubmitting ? (
                                        <><Loader2 className="w-4 h-4 animate-spin" /> Setting up...</>
                                    ) : isLast ? (
                                        <><Check className="w-4 h-4" /> Complete Setup</>
                                    ) : (
                                        <>Next <ChevronRight className="w-4 h-4" /></>
                                    )}
                                </button>
                            </div>
                        </div>
                    )}
                </motion.div>

                {/* Unsaved changes confirmation overlay */}
                <AnimatePresence>
                    {showCloseConfirm && (
                        <motion.div
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40"
                        >
                            <motion.div
                                initial={{ scale: 0.95, opacity: 0 }}
                                animate={{ scale: 1, opacity: 1 }}
                                exit={{ scale: 0.95, opacity: 0 }}
                                className="bg-canvas-elevated border border-glass-border rounded-xl shadow-lg p-6 max-w-sm mx-4 space-y-4"
                            >
                                <div className="flex items-start gap-3">
                                    <div className="w-9 h-9 rounded-lg bg-amber-500/10 flex items-center justify-center flex-shrink-0">
                                        <AlertTriangle className="w-5 h-5 text-amber-500" />
                                    </div>
                                    <div>
                                        <h3 className="text-sm font-semibold text-ink">Discard onboarding progress?</h3>
                                        <p className="text-xs text-ink-muted mt-1 leading-relaxed">
                                            You have unsaved onboarding progress. Closing will discard all selections.
                                        </p>
                                    </div>
                                </div>
                                <div className="flex items-center justify-end gap-3">
                                    <button
                                        onClick={() => setShowCloseConfirm(false)}
                                        className="px-4 py-2 rounded-lg text-sm font-medium text-ink-secondary hover:text-ink hover:bg-black/5 dark:hover:bg-white/5 transition-colors"
                                    >
                                        Continue Editing
                                    </button>
                                    <button
                                        onClick={confirmClose}
                                        className="px-4 py-2 rounded-lg text-sm font-semibold text-white bg-red-500 hover:bg-red-600 transition-colors"
                                    >
                                        Discard & Close
                                    </button>
                                </div>
                            </motion.div>
                        </motion.div>
                    )}
                </AnimatePresence>
            </motion.div>
        </AnimatePresence>
    )
}

export default AssetOnboardingWizard
