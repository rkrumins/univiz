import { useState, useMemo, useCallback, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Database,
  Globe,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Scan,
  Server,
  Shield,
  Sparkles,
  X,
  Zap,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  providerService,
  type ConnectionTestResult,
  type ProviderCreateRequest,
  type ProviderResponse,
  type ProviderType,
  type ProviderUpdateRequest,
  type SchemaDiscoveryResult,
} from '@/services/providerService'
import { useToast } from '@/components/ui/toast'
import { useWizardKeyboard } from './AssetOnboardingWizard/hooks/useWizardKeyboard'
import { DataHubLogo, FalkorDBLogo, Neo4jLogo, SpannerLogo } from './ProviderLogos'

type ProviderWizardStep = 'type' | 'connection' | 'schema' | 'review'
type WizardPhase = 'steps' | 'success'
type WizardMode = 'create' | 'edit'
type ConnectivityState = 'idle' | 'checking' | 'success' | 'failure'

interface SchemaMappingState {
  identityField: string
  displayNameField: string
  qualifiedNameField: string
  descriptionField: string
  tagsField: string
  entityTypeStrategy: 'label' | 'property'
  entityTypeField: string
}

interface SpannerFormState {
  projectId: string
  instanceId: string
  databaseId: string
  graphName: string
  serviceAccountJson: string
  useEmulator: boolean
}

interface ProviderOnboardingFormData {
  providerType: ProviderType | ''
  name: string
  host: string
  port: number
  tlsEnabled: boolean
  username: string
  password: string
  schemaMappingEnabled: boolean
  schemaMapping: SchemaMappingState
  // Spanner uses project/instance/database identifiers rather than host/port.
  // Field is optional because non-Spanner providers ignore it.
  spanner?: SpannerFormState
}

interface ConnectivityCheck {
  state: ConnectivityState
  fingerprint: string | null
  result: ConnectionTestResult | null
}

interface ProviderOnboardingWizardProps {
  isOpen: boolean
  mode?: WizardMode
  provider?: ProviderResponse | null
  providers: ProviderResponse[]
  onClose: () => void
  onCreated?: (provider: ProviderResponse, health: ConnectionTestResult) => Promise<void> | void
  onUpdated?: (provider: ProviderResponse) => Promise<void> | void
}

const PROVIDER_TYPES: Array<{
  type: ProviderType
  label: string
  Logo: typeof FalkorDBLogo
  color: string
  desc: string
}> = [
  {
    type: 'falkordb',
    label: 'FalkorDB',
    Logo: FalkorDBLogo,
    color: 'text-amber-500 bg-amber-500/10 border-amber-500/20',
    desc: 'High-performance graph database',
  },
  {
    type: 'neo4j',
    label: 'Neo4j',
    Logo: Neo4jLogo,
    color: 'text-blue-500 bg-blue-500/10 border-blue-500/20',
    desc: 'The original graph database',
  },
  {
    type: 'datahub',
    label: 'DataHub',
    Logo: DataHubLogo,
    color: 'text-emerald-500 bg-emerald-500/10 border-emerald-500/20',
    desc: 'LinkedIn metadata platform',
  },
  {
    type: 'spanner',
    label: 'Google Spanner Graph',
    Logo: SpannerLogo,
    color: 'text-sky-500 bg-sky-500/10 border-sky-500/20',
    desc: 'Cloud-native distributed property graph (GQL). Requires Enterprise edition.',
  },
]

// The cloud-spanner-emulator is a developer-only tool; surfacing the
// toggle in production builds invites accidental misconfiguration that
// silently routes a real provider at localhost:9010. Hide the UI in prod
// AND scrub the value from any submitted payload as defense in depth.
const IS_PROD_BUILD = Boolean(import.meta.env.PROD)

const DEFAULT_SPANNER_STATE: SpannerFormState = {
  projectId: '',
  instanceId: '',
  databaseId: '',
  graphName: 'UniViz',
  serviceAccountJson: '',
  useEmulator: false,
}

const DEFAULT_SCHEMA_MAPPING: SchemaMappingState = {
  identityField: 'urn',
  displayNameField: 'displayName',
  qualifiedNameField: 'qualifiedName',
  descriptionField: 'description',
  tagsField: 'tags',
  entityTypeStrategy: 'label',
  entityTypeField: 'entityType',
}

function getProviderConfig(type: string) {
  return PROVIDER_TYPES.find((provider) => provider.type === type) ?? PROVIDER_TYPES[0]
}

function defaultPortForProvider(type: ProviderType | ''): number {
  if (type === 'neo4j') return 7687
  if (type === 'datahub') return 8080
  // Spanner has no port concept (managed gRPC); the form hides the
  // port field when type === 'spanner'. We still return a sentinel so
  // ``ProviderOnboardingFormData.port`` stays a number.
  if (type === 'spanner') return 0
  return 6379
}

function isSpanner(type: ProviderType | ''): boolean {
  return type === 'spanner'
}

function buildInitialFormData(provider?: ProviderResponse | null): ProviderOnboardingFormData {
  const schemaMapping = provider?.extraConfig?.schemaMapping
  const extra = provider?.extraConfig ?? {}
  const isSpannerProvider = provider?.providerType === 'spanner'

  return {
    providerType: provider?.providerType ?? '',
    name: provider?.name ?? '',
    host: provider?.host ?? '',
    port: provider?.port ?? defaultPortForProvider(provider?.providerType ?? ''),
    tlsEnabled: provider?.tlsEnabled ?? false,
    username: '',
    password: '',
    schemaMappingEnabled: Boolean(schemaMapping),
    schemaMapping: {
      identityField: schemaMapping?.identity_field ?? DEFAULT_SCHEMA_MAPPING.identityField,
      displayNameField: schemaMapping?.display_name_field ?? DEFAULT_SCHEMA_MAPPING.displayNameField,
      qualifiedNameField: schemaMapping?.qualified_name_field ?? DEFAULT_SCHEMA_MAPPING.qualifiedNameField,
      descriptionField: schemaMapping?.description_field ?? DEFAULT_SCHEMA_MAPPING.descriptionField,
      tagsField: schemaMapping?.tags_field ?? DEFAULT_SCHEMA_MAPPING.tagsField,
      entityTypeStrategy: schemaMapping?.entity_type_strategy ?? DEFAULT_SCHEMA_MAPPING.entityTypeStrategy,
      entityTypeField: schemaMapping?.entity_type_field ?? DEFAULT_SCHEMA_MAPPING.entityTypeField,
    },
    spanner: isSpannerProvider
      ? {
          projectId: extra.projectId ?? '',
          instanceId: extra.instanceId ?? '',
          databaseId: extra.databaseId ?? '',
          graphName: extra.graphName ?? DEFAULT_SPANNER_STATE.graphName,
          // Service-account JSON is not echoed back from the API for security.
          serviceAccountJson: '',
          useEmulator: Boolean(extra.useEmulator),
        }
      : { ...DEFAULT_SPANNER_STATE },
  }
}

function buildExtraConfig(formData: ProviderOnboardingFormData) {
  const out: Record<string, any> = {}

  if (formData.schemaMappingEnabled) {
    out.schemaMapping = {
      identity_field: formData.schemaMapping.identityField,
      display_name_field: formData.schemaMapping.displayNameField,
      qualified_name_field: formData.schemaMapping.qualifiedNameField,
      description_field: formData.schemaMapping.descriptionField,
      tags_field: formData.schemaMapping.tagsField,
      entity_type_strategy: formData.schemaMapping.entityTypeStrategy,
      entity_type_field: formData.schemaMapping.entityTypeField,
    }
  }

  if (isSpanner(formData.providerType) && formData.spanner) {
    const s = formData.spanner
    if (s.projectId) out.projectId = s.projectId
    if (s.instanceId) out.instanceId = s.instanceId
    if (s.databaseId) out.databaseId = s.databaseId
    if (s.graphName) out.graphName = s.graphName
    if (!IS_PROD_BUILD && s.useEmulator) out.useEmulator = true
  }

  return Object.keys(out).length > 0 ? out : undefined
}

function buildConnectivityRequest(formData: ProviderOnboardingFormData): ProviderCreateRequest {
  // Spanner doesn't use host/port/username/password; build credentials and
  // skip host/port for that branch. Other providers stay on the legacy shape.
  const isSpannerType = isSpanner(formData.providerType)

  const credentials = isSpannerType
    ? (
        formData.spanner?.serviceAccountJson || formData.spanner?.projectId
          ? {
              project_id: formData.spanner?.projectId || undefined,
              service_account_json: formData.spanner?.serviceAccountJson || undefined,
            }
          : undefined
      )
    : (formData.username || formData.password)
      ? { username: formData.username || undefined, password: formData.password || undefined }
      : undefined

  return {
    name: formData.name.trim() || 'Connectivity Check',
    providerType: formData.providerType as ProviderType,
    host: isSpannerType ? undefined : (formData.host || undefined),
    port: isSpannerType ? undefined : (formData.port || undefined),
    tlsEnabled: formData.tlsEnabled,
    credentials,
    extraConfig: buildExtraConfig(formData),
  }
}

function isMeaningfullyDirty(
  formData: ProviderOnboardingFormData,
  initialState: ProviderOnboardingFormData | null,
): boolean {
  if (!initialState) return false

  return JSON.stringify(formData) !== JSON.stringify(initialState)
}

function StepWarnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl border border-amber-500/20 bg-amber-500/8 px-4 py-3"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" />
        <div className="space-y-1 text-sm text-amber-700 dark:text-amber-300">
          {warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      </div>
    </motion.div>
  )
}

function ConfirmCloseDialog({
  isOpen,
  onCancel,
  onConfirm,
}: {
  isOpen: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/50 px-4">
      <motion.div
        initial={{ opacity: 0, y: 12, scale: 0.96 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        className="w-full max-w-md rounded-2xl border border-glass-border bg-canvas-elevated p-6 shadow-lg"
      >
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-amber-500/10 text-amber-500">
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <h3 className="text-lg font-semibold text-ink">Discard provider setup?</h3>
            <p className="mt-1 text-sm text-ink-muted">
              Your unsaved changes will be lost if you close the wizard now.
            </p>
          </div>
        </div>
        <div className="mt-6 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-xl border border-glass-border px-4 py-2 text-sm font-medium text-ink-secondary transition-colors hover:bg-black/5 dark:hover:bg-white/5"
          >
            Keep editing
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-xl bg-red-500 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-red-600"
          >
            Discard
          </button>
        </div>
      </motion.div>
    </div>
  )
}

export function ProviderOnboardingWizard({
  isOpen,
  mode = 'create',
  provider = null,
  providers,
  onClose,
  onCreated,
  onUpdated,
}: ProviderOnboardingWizardProps) {
  const navigate = useNavigate()
  const { showToast } = useToast()
  const modalRef = useRef<HTMLDivElement>(null)

  const [formData, setFormData] = useState<ProviderOnboardingFormData>(() => buildInitialFormData(provider))
  const [schemaDiscovery, setSchemaDiscovery] = useState<SchemaDiscoveryResult | null>(null)
  const [schemaLoading, setSchemaLoading] = useState(false)
  const [schemaError, setSchemaError] = useState<string | null>(null)
  const [currentStep, setCurrentStep] = useState<ProviderWizardStep>(mode === 'edit' ? 'connection' : 'type')
  const [previousSteps, setPreviousSteps] = useState<ProviderWizardStep[]>([])
  const [wizardPhase, setWizardPhase] = useState<WizardPhase>('steps')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [showCloseConfirm, setShowCloseConfirm] = useState(false)
  const [createdProvider, setCreatedProvider] = useState<ProviderResponse | null>(null)
  const [connectionResult, setConnectionResult] = useState<ConnectionTestResult | null>(null)
  const [connectivityCheck, setConnectivityCheck] = useState<ConnectivityCheck>({
    state: 'idle',
    fingerprint: null,
    result: null,
  })

  const initialStateRef = useRef<ProviderOnboardingFormData | null>(null)

  const steps = useMemo(() => {
    const flow: Array<{ id: ProviderWizardStep; title: string; icon: typeof Server }> = []

    if (mode === 'create') {
      flow.push({ id: 'type', title: 'Provider Type', icon: Server })
    }

    flow.push({ id: 'connection', title: 'Connection', icon: Globe })

    // Schema-mapping step appears for any non-canonical-schema provider.
    // FalkorDB and DataHub use the canonical Synodic property names; Neo4j
    // and Spanner can map foreign properties via SchemaMapping.
    if (formData.providerType === 'neo4j' || formData.providerType === 'spanner') {
      flow.push({ id: 'schema', title: 'Schema Mapping', icon: Scan })
    }

    flow.push({ id: 'review', title: 'Review', icon: Shield })
    return flow
  }, [formData.providerType, mode])

  const currentStepIndex = steps.findIndex((step) => step.id === currentStep)
  const isLastStep = currentStepIndex === steps.length - 1

  const nameDuplicate = useMemo(() => {
    const normalized = formData.name.trim().toLowerCase()
    if (!normalized) return false

    return providers.some((existing) => {
      if (mode === 'edit' && existing.id === provider?.id) return false
      return existing.name.toLowerCase() === normalized
    })
  }, [formData.name, mode, provider?.id, providers])

  const canProceed = useMemo(() => {
    switch (currentStep) {
      case 'type':
        return Boolean(formData.providerType)
      case 'connection': {
        if (!formData.name.trim() || nameDuplicate) return false
        // Spanner needs project + instance + database before we can probe.
        if (isSpanner(formData.providerType)) {
          const s = formData.spanner
          if (!s) return false
          if (!s.projectId.trim() || !s.instanceId.trim() || !s.databaseId.trim()) return false
          // In emulator mode the service-account JSON is optional.
          if (!s.useEmulator && !s.serviceAccountJson.trim()) return false
        }
        return true
      }
      case 'schema':
        return true
      case 'review':
        return true
    }
  }, [currentStep, formData.name, formData.providerType, nameDuplicate])

  const stepWarnings = useMemo(() => {
    if (currentStep === 'connection') {
      const warnings: string[] = []
      if (!formData.name.trim()) warnings.push('Provider name is required.')
      if (nameDuplicate) warnings.push(`A provider named "${formData.name.trim()}" already exists.`)
      return warnings
    }

    if (currentStep === 'schema' && formData.schemaMappingEnabled && !schemaDiscovery) {
      return ['Optional: use schema discovery to auto-suggest a mapping before continuing.']
    }

    return []
  }, [currentStep, formData.name, formData.schemaMappingEnabled, nameDuplicate, schemaDiscovery])

  const isDirty = useMemo(() => isMeaningfullyDirty(formData, initialStateRef.current), [formData])
  const connectivityFingerprint = useMemo(() => JSON.stringify({
    providerType: formData.providerType,
    host: formData.host,
    port: formData.port,
    tlsEnabled: formData.tlsEnabled,
    username: formData.username,
    password: formData.password,
    schemaMappingEnabled: formData.schemaMappingEnabled,
    schemaMapping: formData.schemaMapping,
  }), [formData])

  useEffect(() => {
    if (!isOpen) return

    const nextState = buildInitialFormData(provider)
    setFormData(nextState)
    initialStateRef.current = nextState
    setSchemaDiscovery(null)
    setSchemaLoading(false)
    setSchemaError(null)
    setSubmitError(null)
    setShowCloseConfirm(false)
    setIsSubmitting(false)
    setWizardPhase('steps')
    setCreatedProvider(null)
    setConnectionResult(null)
    setConnectivityCheck({
      state: 'idle',
      fingerprint: null,
      result: null,
    })
    setPreviousSteps([])
    setCurrentStep(mode === 'edit' ? 'connection' : 'type')
  }, [isOpen, mode, provider])

  useEffect(() => {
    setConnectivityCheck((previous) => {
      if (previous.state === 'idle' && previous.fingerprint === connectivityFingerprint) {
        return previous
      }
      if (previous.fingerprint === connectivityFingerprint && previous.state !== 'idle') {
        return previous
      }
      return {
        state: 'idle',
        fingerprint: connectivityFingerprint,
        result: null,
      }
    })
  }, [connectivityFingerprint])

  const updateFormData = useCallback((updates: Partial<ProviderOnboardingFormData>) => {
    setFormData((previous) => ({ ...previous, ...updates }))
  }, [])

  const goNext = useCallback(() => {
    if (!canProceed) return
    const nextIndex = currentStepIndex + 1
    if (nextIndex >= steps.length) return

    setPreviousSteps((previous) => [...previous, currentStep])
    setCurrentStep(steps[nextIndex].id)
  }, [canProceed, currentStep, currentStepIndex, steps])

  const goBack = useCallback(() => {
    if (previousSteps.length === 0) return
    const previous = previousSteps[previousSteps.length - 1]
    setPreviousSteps((stack) => stack.slice(0, -1))
    setCurrentStep(previous)
  }, [previousSteps])

  const handleClose = useCallback(() => {
    if (wizardPhase === 'success') {
      onClose()
      return
    }

    if (isDirty) {
      setShowCloseConfirm(true)
      return
    }

    onClose()
  }, [isDirty, onClose, wizardPhase])

  const confirmClose = useCallback(() => {
    setShowCloseConfirm(false)
    onClose()
  }, [onClose])

  const handleDiscoverSchema = useCallback(async () => {
    setSchemaLoading(true)
    setSchemaError(null)

    try {
      const tempReq: ProviderCreateRequest = {
        name: `_temp_discovery_${Date.now()}`,
        providerType: 'neo4j',
        host: formData.host || 'localhost',
        port: formData.port || 7687,
        tlsEnabled: formData.tlsEnabled,
        credentials: {
          username: formData.username || undefined,
          password: formData.password || undefined,
        },
      }

      const tempProvider = await providerService.create(tempReq)
      try {
        const result = await providerService.discoverSchema(tempProvider.id)
        setSchemaDiscovery(result)

        if (result.suggestedMapping) {
          const mapping = result.suggestedMapping
          setFormData((previous) => ({
            ...previous,
            schemaMapping: {
              ...previous.schemaMapping,
              identityField: mapping.identity_field || previous.schemaMapping.identityField,
              displayNameField: mapping.display_name_field || previous.schemaMapping.displayNameField,
              qualifiedNameField: mapping.qualified_name_field || previous.schemaMapping.qualifiedNameField,
              descriptionField: mapping.description_field || previous.schemaMapping.descriptionField,
              entityTypeStrategy: mapping.entity_type_strategy || previous.schemaMapping.entityTypeStrategy,
            },
          }))
        }
      } finally {
        await providerService.delete(tempProvider.id).catch(() => undefined)
      }
    } catch (error) {
      setSchemaError(error instanceof Error ? error.message : 'Failed to discover schema')
    } finally {
      setSchemaLoading(false)
    }
  }, [formData.host, formData.password, formData.port, formData.tlsEnabled, formData.username])

  const handleTestConnection = useCallback(async () => {
    const request = buildConnectivityRequest(formData)

    setSubmitError(null)
    setConnectivityCheck({
      state: 'checking',
      fingerprint: connectivityFingerprint,
      result: null,
    })

    try {
      const result = await providerService.testConnection(request, { timeoutMs: 10_000 })
      setConnectivityCheck({
        state: result.success ? 'success' : 'failure',
        fingerprint: connectivityFingerprint,
        result,
      })
    } catch (error) {
      setConnectivityCheck({
        state: 'failure',
        fingerprint: connectivityFingerprint,
        result: {
          success: false,
          error: error instanceof Error ? error.message : 'Connection test failed',
        },
      })
    }
  }, [connectivityFingerprint, formData])

  const handleSubmit = useCallback(async () => {
    if (mode === 'create' && connectivityCheck.state === 'idle') {
      setSubmitError('Run a connection test before creating the provider.')
      return
    }

    setIsSubmitting(true)
    setSubmitError(null)

    try {
      if (mode === 'edit' && provider) {
        const req: ProviderUpdateRequest = {
          name: formData.name.trim(),
          host: formData.host || undefined,
          port: formData.port || undefined,
          tlsEnabled: formData.tlsEnabled,
          credentials: (formData.username || formData.password)
            ? { username: formData.username || undefined, password: formData.password || undefined }
            : undefined,
          extraConfig: buildExtraConfig(formData),
        }
        const updated = await providerService.update(provider.id, req)
        await onUpdated?.(updated)
        showToast('success', `Updated ${updated.name}`)
        onClose()
        return
      }

      const req: ProviderCreateRequest = {
        ...buildConnectivityRequest(formData),
        name: formData.name.trim(),
      }

      const created = await providerService.create(req)
      const health = connectivityCheck.result && connectivityCheck.state !== 'idle'
        ? connectivityCheck.result
        : await providerService.test(created.id)
      await onCreated?.(created, health)

      setCreatedProvider(created)
      setConnectionResult(health)
      setWizardPhase('success')
      showToast(
        health.success ? 'success' : 'warning',
        health.success
          ? `${created.name} connected successfully`
          : `${created.name} was created, but its connection needs attention`,
      )
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : 'Failed to save provider')
    } finally {
      setIsSubmitting(false)
    }
  }, [connectivityCheck.result, connectivityCheck.state, formData, mode, onClose, onCreated, onUpdated, provider, showToast])

  const requiresConnectivityTest = mode === 'create' && currentStep === 'review'
  const shouldRunConnectivityTest = requiresConnectivityTest && connectivityCheck.state === 'idle'
  const primaryAction = shouldRunConnectivityTest ? handleTestConnection : handleSubmit

  useWizardKeyboard({
    containerRef: modalRef,
    onClose: handleClose,
    onNext: goNext,
    onSubmit: primaryAction,
    canProceed,
    isLastStep,
    isSubmitting,
    isSuccess: wizardPhase === 'success',
    isOpen,
  })

  if (!isOpen) return null

  const activeStep = steps[currentStepIndex]
  const currentConfig = getProviderConfig(formData.providerType || provider?.providerType || 'falkordb')

  const renderTypeStep = () => (
    <div className="space-y-6">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-start gap-3"
      >
        <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl border border-indigo-500/10 bg-gradient-to-br from-indigo-500/20 to-violet-500/20">
          <Server className="h-5 w-5 text-indigo-500" />
        </div>
        <div>
          <h3 className="text-lg font-semibold text-ink">Choose your provider type</h3>
          <p className="mt-0.5 text-sm text-ink-muted">
            Start by choosing the infrastructure you want Synodic to connect to.
          </p>
        </div>
      </motion.div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        {PROVIDER_TYPES.map((providerOption, index) => (
          <motion.button
            key={providerOption.type}
            type="button"
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.06 }}
            onClick={() => updateFormData({
              providerType: providerOption.type,
              port: defaultPortForProvider(providerOption.type),
            })}
            className={cn(
              'rounded-2xl border p-5 text-left transition-[colors,transform,box-shadow] duration-150 hover:-translate-y-0.5 hover:shadow-md',
              formData.providerType === providerOption.type
                ? 'border-indigo-500 bg-indigo-500/8 shadow-md'
                : 'border-glass-border bg-canvas-elevated hover:border-indigo-500/30',
            )}
          >
            <div className={cn('mb-4 flex h-11 w-11 items-center justify-center rounded-xl border', providerOption.color)}>
              <providerOption.Logo className="h-6 w-6" />
            </div>
            <div className="flex items-center justify-between gap-3">
              <h4 className="text-base font-semibold text-ink">{providerOption.label}</h4>
              {formData.providerType === providerOption.type && (
                <div className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-500 text-white">
                  <Check className="h-3.5 w-3.5" />
                </div>
              )}
            </div>
            <p className="mt-2 text-sm leading-relaxed text-ink-muted">{providerOption.desc}</p>
          </motion.button>
        ))}
      </div>
    </div>
  )

  const renderConnectionStep = () => (
    <div className="space-y-6">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-start gap-3"
      >
        <div className={cn('flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl border', currentConfig.color)}>
          <currentConfig.Logo className="h-5 w-5" />
        </div>
        <div className="flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-semibold text-ink">
              {mode === 'edit' ? 'Update provider details' : 'Connect your provider'}
            </h3>
            <span className="rounded-full bg-black/5 px-2.5 py-1 text-xs font-semibold text-ink-muted dark:bg-white/5">
              {currentConfig.label}
            </span>
          </div>
          <p className="mt-0.5 text-sm text-ink-muted">
            Add the infrastructure details Synodic needs in order to connect and validate access.
          </p>
        </div>
      </motion.div>

      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="space-y-4 rounded-2xl border border-glass-border bg-canvas-elevated p-5">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-ink">Provider name</label>
            <input
              value={formData.name}
              onChange={(event) => updateFormData({ name: event.target.value })}
              placeholder="e.g. Production Lineage Graph"
              className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
            />
          </div>

          {isSpanner(formData.providerType) ? (
            <>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">GCP Project ID</label>
                  <input
                    value={formData.spanner?.projectId ?? ''}
                    onChange={(event) =>
                      updateFormData({
                        spanner: { ...(formData.spanner ?? DEFAULT_SPANNER_STATE), projectId: event.target.value },
                      })
                    }
                    placeholder="my-gcp-project"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">Instance ID</label>
                  <input
                    value={formData.spanner?.instanceId ?? ''}
                    onChange={(event) =>
                      updateFormData({
                        spanner: { ...(formData.spanner ?? DEFAULT_SPANNER_STATE), instanceId: event.target.value },
                      })
                    }
                    placeholder="uniViz-instance"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">Database ID</label>
                  <input
                    value={formData.spanner?.databaseId ?? ''}
                    onChange={(event) =>
                      updateFormData({
                        spanner: { ...(formData.spanner ?? DEFAULT_SPANNER_STATE), databaseId: event.target.value },
                      })
                    }
                    placeholder="uniViz"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">Property graph name</label>
                  <input
                    value={formData.spanner?.graphName ?? ''}
                    onChange={(event) =>
                      updateFormData({
                        spanner: { ...(formData.spanner ?? DEFAULT_SPANNER_STATE), graphName: event.target.value },
                      })
                    }
                    placeholder="UniViz"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
              </div>
              {!IS_PROD_BUILD && (
                <label className="flex items-center justify-between rounded-xl border border-glass-border bg-black/5 px-4 py-3 dark:bg-white/5">
                  <div>
                    <p className="text-sm font-medium text-ink">Use cloud-spanner-emulator (development only)</p>
                    <p className="text-xs text-ink-muted">
                      Routes the client to <code>localhost:9010</code>. The emulator does not implement GQL —
                      schema bootstrap and queries succeed, but property-graph DDL fails. Hidden in production builds.
                    </p>
                  </div>
                  <input
                    type="checkbox"
                    checked={Boolean(formData.spanner?.useEmulator)}
                    onChange={(event) =>
                      updateFormData({
                        spanner: { ...(formData.spanner ?? DEFAULT_SPANNER_STATE), useEmulator: event.target.checked },
                      })
                    }
                    className="h-4 w-4 rounded border-glass-border text-indigo-500 focus:ring-indigo-500/50"
                  />
                </label>
              )}
              <div>
                <label className="mb-1.5 block text-sm font-medium text-ink">
                  Service account JSON
                  {formData.spanner?.useEmulator ? <span className="text-ink-muted"> (optional in emulator mode)</span> : null}
                </label>
                <textarea
                  value={formData.spanner?.serviceAccountJson ?? ''}
                  onChange={(event) =>
                    updateFormData({
                      spanner: { ...(formData.spanner ?? DEFAULT_SPANNER_STATE), serviceAccountJson: event.target.value },
                    })
                  }
                  placeholder='{"type":"service_account","project_id":"...", ...}'
                  rows={6}
                  className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 font-mono text-xs text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                />
              </div>
            </>
          ) : (
            <>
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <label className="mb-1.5 block text-sm font-medium text-ink">Host</label>
                  <input
                    value={formData.host}
                    onChange={(event) => updateFormData({ host: event.target.value })}
                    placeholder="localhost"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">Port</label>
                  <input
                    type="number"
                    value={formData.port}
                    onChange={(event) => updateFormData({ port: Number(event.target.value) })}
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">Username</label>
                  <input
                    value={formData.username}
                    onChange={(event) => updateFormData({ username: event.target.value })}
                    placeholder="optional"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-ink">Password</label>
                  <input
                    type="password"
                    value={formData.password}
                    onChange={(event) => updateFormData({ password: event.target.value })}
                    placeholder="optional"
                    className="w-full rounded-xl border border-glass-border bg-black/5 px-4 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                  />
                </div>
              </div>
            </>
          )}
        </div>

        <div className="space-y-4 rounded-2xl border border-glass-border bg-gradient-to-br from-slate-50 to-white p-5 dark:from-slate-800 dark:to-slate-900">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-500/10 text-indigo-500">
              <Shield className="h-5 w-5" />
            </div>
            <div>
              <h4 className="text-sm font-semibold text-ink">Connection guidance</h4>
              <p className="text-xs text-ink-muted">These details are stored as infrastructure settings only.</p>
            </div>
          </div>

          <ul className="space-y-3 text-sm text-ink-muted">
            <li className="flex items-start gap-2">
              <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-500" />
              Use a clear provider name so it’s easy to identify later in data source onboarding.
            </li>
            <li className="flex items-start gap-2">
              <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-500" />
              Credentials are optional unless your provider requires authentication.
            </li>
            <li className="flex items-start gap-2">
              <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-500" />
              After saving, Synodic will test the connection before you move on to data sources.
            </li>
          </ul>

          <label className="flex items-center justify-between rounded-xl border border-glass-border bg-black/5 px-4 py-3 dark:bg-white/5">
            <div>
              <p className="text-sm font-medium text-ink">Use TLS</p>
              <p className="text-xs text-ink-muted">Enable secure transport when your provider expects it.</p>
            </div>
            <input
              type="checkbox"
              checked={formData.tlsEnabled}
              onChange={(event) => updateFormData({ tlsEnabled: event.target.checked })}
              className="h-4 w-4 rounded border-glass-border text-indigo-500 focus:ring-indigo-500/50"
            />
          </label>
        </div>
      </div>
    </div>
  )

  const renderSchemaStep = () => (
    <div className="space-y-6">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-start gap-3"
      >
        <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl border border-violet-500/10 bg-gradient-to-br from-violet-500/20 to-indigo-500/20">
          <BookOpen className="h-5 w-5 text-violet-500" />
        </div>
        <div>
          <h3 className="text-lg font-semibold text-ink">Optional schema mapping</h3>
          <p className="mt-0.5 text-sm text-ink-muted">
            If your Neo4j graph uses custom property names, map them now so later ingestion steps feel native.
          </p>
        </div>
      </motion.div>

      <div className="rounded-2xl border border-glass-border bg-canvas-elevated p-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h4 className="text-sm font-semibold text-ink">Enable custom mapping</h4>
            <p className="mt-1 text-xs text-ink-muted">
              Skip this if your graph already follows Synodic’s default schema conventions.
            </p>
          </div>
          <label className="relative inline-flex cursor-pointer items-center">
            <input
              type="checkbox"
              checked={formData.schemaMappingEnabled}
              onChange={(event) => updateFormData({ schemaMappingEnabled: event.target.checked })}
              className="peer sr-only"
            />
            <div className="h-5 w-9 rounded-full bg-black/10 transition-colors after:absolute after:left-[2px] after:top-0.5 after:h-4 after:w-4 after:rounded-full after:bg-white after:transition-transform after:content-[''] peer-checked:bg-indigo-500 peer-checked:after:translate-x-full dark:bg-white/10" />
          </label>
        </div>

        <AnimatePresence initial={false}>
          {formData.schemaMappingEnabled ? (
            <motion.div
              key="schema-enabled"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              className="mt-5 space-y-4"
            >
              <button
                type="button"
                onClick={handleDiscoverSchema}
                disabled={schemaLoading || !formData.host}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-500/10 px-4 py-2.5 text-sm font-semibold text-indigo-600 transition-colors hover:bg-indigo-500/20 disabled:opacity-50 dark:text-indigo-400"
              >
                {schemaLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Scan className="h-4 w-4" />}
                {schemaLoading ? 'Discovering schema...' : 'Auto-discover mapping'}
              </button>

              {schemaError && (
                <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-500">
                  {schemaError}
                </div>
              )}

              {schemaDiscovery && (
                <div className="rounded-2xl border border-glass-border bg-black/[0.02] p-4 dark:bg-white/[0.02]">
                  <div className="mb-3 flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-indigo-500" />
                    <h5 className="text-xs font-bold uppercase tracking-wider text-ink-muted">Discovered schema</h5>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {schemaDiscovery.labels.map((label) => (
                      <span key={label} className="rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-[11px] font-medium text-blue-600 dark:text-blue-400">
                        {label}
                      </span>
                    ))}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {schemaDiscovery.relationshipTypes.map((relationshipType) => (
                      <span key={relationshipType} className="rounded-full border border-violet-500/20 bg-violet-500/10 px-2 py-0.5 text-[11px] font-medium text-violet-600 dark:text-violet-400">
                        {relationshipType}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="space-y-3">
                {([
                  ['identityField', 'Identity (URN)', 'The property used as the stable unique identifier'],
                  ['displayNameField', 'Display Name', 'Human-readable label for the entity'],
                  ['qualifiedNameField', 'Qualified Name', 'The full hierarchical or technical path name'],
                  ['descriptionField', 'Description', 'Description or notes field'],
                  ['tagsField', 'Tags', 'Tags or classifications field'],
                ] as const).map(([fieldKey, label, hint]) => (
                  <div key={fieldKey} className="grid grid-cols-1 gap-2 md:grid-cols-5 md:items-center">
                    <div className="md:col-span-2">
                      <label className="text-xs font-medium text-ink">{label}</label>
                      <p className="mt-0.5 text-[10px] leading-tight text-ink-muted">{hint}</p>
                    </div>
                    <div className="hidden justify-center text-ink-muted md:col-span-1 md:flex">
                      <ArrowRight className="h-3 w-3" />
                    </div>
                    <div className="md:col-span-2">
                      <input
                        value={formData.schemaMapping[fieldKey]}
                        onChange={(event) => setFormData((previous) => ({
                          ...previous,
                          schemaMapping: {
                            ...previous.schemaMapping,
                            [fieldKey]: event.target.value,
                          },
                        }))}
                        className="w-full rounded-lg border border-glass-border bg-black/5 px-3 py-2 text-xs font-mono text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-white/5"
                      />
                    </div>
                  </div>
                ))}
              </div>

              <div className="rounded-xl border border-glass-border bg-black/5 p-4 dark:bg-white/5">
                <label className="mb-2 block text-xs font-medium text-ink">Entity type resolution</label>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setFormData((previous) => ({
                      ...previous,
                      schemaMapping: { ...previous.schemaMapping, entityTypeStrategy: 'label' },
                    }))}
                    className={cn(
                      'flex-1 rounded-lg border px-3 py-2 text-xs font-semibold transition-colors',
                      formData.schemaMapping.entityTypeStrategy === 'label'
                        ? 'border-indigo-500/30 bg-indigo-500/10 text-indigo-600'
                        : 'border-glass-border text-ink-muted hover:text-ink',
                    )}
                  >
                    Use label
                  </button>
                  <button
                    type="button"
                    onClick={() => setFormData((previous) => ({
                      ...previous,
                      schemaMapping: { ...previous.schemaMapping, entityTypeStrategy: 'property' },
                    }))}
                    className={cn(
                      'flex-1 rounded-lg border px-3 py-2 text-xs font-semibold transition-colors',
                      formData.schemaMapping.entityTypeStrategy === 'property'
                        ? 'border-indigo-500/30 bg-indigo-500/10 text-indigo-600'
                        : 'border-glass-border text-ink-muted hover:text-ink',
                    )}
                  >
                    Use property
                  </button>
                </div>
                {formData.schemaMapping.entityTypeStrategy === 'property' && (
                  <input
                    value={formData.schemaMapping.entityTypeField}
                    onChange={(event) => setFormData((previous) => ({
                      ...previous,
                      schemaMapping: { ...previous.schemaMapping, entityTypeField: event.target.value },
                    }))}
                    placeholder="entityType"
                    className="mt-3 w-full rounded-lg border border-glass-border bg-white/60 px-3 py-2 text-xs font-mono text-ink focus:outline-none focus:ring-2 focus:ring-indigo-500/50 dark:bg-black/10"
                  />
                )}
              </div>
            </motion.div>
          ) : (
            <motion.div
              key="schema-default"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              className="mt-5 rounded-xl border border-emerald-500/20 bg-emerald-500/8 px-4 py-4 text-sm text-emerald-700 dark:text-emerald-300"
            >
              Synodic will assume the default property names such as <code className="rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-xs">urn</code>, <code className="rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-xs">displayName</code>, and <code className="rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-xs">entityType</code>.
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  )

  const renderReviewStep = () => (
    <div className="mx-auto w-full max-w-2xl space-y-8">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="text-center"
      >
        <div className="mb-4 inline-flex items-center gap-2 rounded-full bg-indigo-500/10 px-4 py-2 text-sm font-medium text-indigo-600 dark:text-indigo-400">
          <Sparkles className="h-4 w-4" />
          {mode === 'edit' ? 'Ready to save changes' : 'Ready to register provider'}
        </div>
        <h3 className="text-2xl font-bold text-slate-900 dark:text-white">
          Review your provider configuration
        </h3>
        <p className="mt-2 text-slate-500">
          Confirm the infrastructure details below before Synodic validates the connection.
        </p>
      </motion.div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.08 }}
        className="mx-auto w-full overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-50 to-white shadow-sm dark:border-slate-700 dark:from-slate-800 dark:to-slate-900"
      >
        <div className="divide-y divide-slate-200 dark:divide-slate-700">
          <div className="p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className={cn('flex h-11 w-11 items-center justify-center rounded-xl border shadow-sm', currentConfig.color)}>
                <currentConfig.Logo className="h-5 w-5" />
              </div>
              <div className="flex-1">
                <p className="text-sm font-medium uppercase tracking-wide text-slate-500">Provider</p>
                <p className="font-semibold text-slate-800 dark:text-slate-200">{formData.name || 'Unnamed provider'}</p>
              </div>
              <Check className="h-5 w-5 text-green-500" />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800">
                {currentConfig.label}
              </span>
              <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-sm font-mono dark:border-slate-700 dark:bg-slate-800">
                {formData.host || 'localhost'}:{formData.port}
              </span>
              {formData.tlsEnabled && (
                <span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-600 dark:text-emerald-400">
                  TLS enabled
                </span>
              )}
            </div>
          </div>

          <div className="p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-100 text-indigo-600 shadow-sm dark:bg-indigo-900/30 dark:text-indigo-400">
                <Globe className="h-5 w-5" />
              </div>
              <div className="flex-1">
                <p className="text-sm font-medium uppercase tracking-wide text-slate-500">Access</p>
                <p className="font-semibold text-slate-800 dark:text-slate-200">
                  {formData.username ? 'Credentials supplied' : 'Anonymous / host-only access'}
                </p>
              </div>
              <Check className="h-5 w-5 text-green-500" />
            </div>
            <p className="text-sm text-slate-500">
              {formData.username
                ? `The provider will be created with username ${formData.username}.`
                : 'No credentials were entered. Synodic will connect with the host and port settings only.'}
            </p>
          </div>

          <div className="p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className={cn(
                'flex h-11 w-11 items-center justify-center rounded-xl shadow-sm',
                connectivityCheck.state === 'success'
                  ? 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-400'
                  : connectivityCheck.state === 'failure'
                    ? 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400'
                    : 'bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-400',
              )}>
                {connectivityCheck.state === 'success' ? (
                  <CheckCircle2 className="h-5 w-5" />
                ) : connectivityCheck.state === 'failure' ? (
                  <AlertTriangle className="h-5 w-5" />
                ) : (
                  <Zap className="h-5 w-5" />
                )}
              </div>
              <div className="flex-1">
                <p className="text-sm font-medium uppercase tracking-wide text-slate-500">Connectivity</p>
                <p className="font-semibold text-slate-800 dark:text-slate-200">
                  {connectivityCheck.state === 'success'
                    ? 'Connected successfully'
                    : connectivityCheck.state === 'failure'
                      ? 'Unable to connect'
                      : connectivityCheck.state === 'checking'
                        ? 'Testing connection...'
                        : mode === 'create'
                          ? 'Connection check required before creating this provider.'
                          : 'Connection test not run in this session.'}
                </p>
              </div>
              {connectivityCheck.result?.latencyMs !== undefined && (
                <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-mono text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  {connectivityCheck.result.latencyMs}ms
                </span>
              )}
            </div>
            <div className={cn(
              'rounded-xl border px-4 py-3.5 text-sm leading-relaxed',
              connectivityCheck.state === 'success'
                ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                : connectivityCheck.state === 'failure'
                  ? 'border-red-500/20 bg-red-500/10 text-red-600 dark:text-red-300'
                  : 'border-amber-500/20 bg-amber-500/10 text-amber-700 dark:text-amber-300',
            )}>
              {connectivityCheck.state === 'success'
                ? 'The provider responded to a live connectivity probe. You can create it safely now.'
                : connectivityCheck.state === 'failure'
                  ? connectivityCheck.result?.error || 'Connection test failed.'
                  : connectivityCheck.state === 'checking'
                    ? 'Synodic is probing the provider now. This should only take a few seconds.'
                    : mode === 'create'
                      ? 'Run a live connection test before creating the provider so you know these settings are reachable.'
                      : 'Save changes as-is, or re-test later from the provider management flow if you need to validate connectivity.'}
            </div>
            {mode === 'create' && connectivityCheck.state !== 'idle' && (
              <div className="mt-3 flex justify-end">
                <button
                  type="button"
                  onClick={handleTestConnection}
                  disabled={connectivityCheck.state === 'checking'}
                  className="inline-flex items-center gap-2 rounded-lg border border-glass-border bg-white/70 px-3 py-2 text-sm font-medium text-ink-secondary transition-colors hover:bg-white dark:bg-slate-900/40 dark:hover:bg-slate-900/70"
                >
                  {connectivityCheck.state === 'checking' ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4" />
                  )}
                  {connectivityCheck.state === 'failure' ? 'Retry connection test' : 'Test again'}
                </button>
              </div>
            )}
          </div>

          {formData.providerType === 'neo4j' && (
            <div className="p-6">
              <div className="mb-4 flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-violet-100 text-violet-600 shadow-sm dark:bg-violet-900/30 dark:text-violet-400">
                  <BookOpen className="h-5 w-5" />
                </div>
                <div className="flex-1">
                  <p className="text-sm font-medium uppercase tracking-wide text-slate-500">Schema Mapping</p>
                  <p className="font-semibold text-slate-800 dark:text-slate-200">
                    {formData.schemaMappingEnabled ? 'Custom mapping enabled' : 'Default Synodic schema'}
                  </p>
                </div>
                <Check className="h-5 w-5 text-green-500" />
              </div>
              {formData.schemaMappingEnabled ? (
                <div className="grid gap-2 text-sm text-slate-500">
                  <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-800">
                    <span>Identity</span>
                    <code className="font-mono text-slate-800 dark:text-slate-200">{formData.schemaMapping.identityField}</code>
                  </div>
                  <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-800">
                    <span>Display name</span>
                    <code className="font-mono text-slate-800 dark:text-slate-200">{formData.schemaMapping.displayNameField}</code>
                  </div>
                  <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-800">
                    <span>Entity type resolution</span>
                    <code className="font-mono text-slate-800 dark:text-slate-200">{formData.schemaMapping.entityTypeStrategy}</code>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-slate-500">
                  The default Synodic property names will be used for this provider.
                </p>
              )}
            </div>
          )}
        </div>
      </motion.div>
    </div>
  )

  const renderSuccessPhase = () => {
    if (!createdProvider || !connectionResult) return null

    const healthy = connectionResult.success
    return (
      <div className="max-w-2xl space-y-8">
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center"
        >
          <div className={cn(
            'mb-4 inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-medium',
            healthy
              ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
          )}>
            {healthy ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
            {healthy ? 'Provider connected' : 'Provider created with warnings'}
          </div>
          <h3 className="text-2xl font-bold text-slate-900 dark:text-white">
            {healthy ? `${createdProvider.name} is ready` : `${createdProvider.name} needs attention`}
          </h3>
          <p className="mt-2 text-slate-500">
            {healthy
              ? 'Continue straight into data source discovery, or stay on the providers screen to manage more infrastructure.'
              : connectionResult.error || 'The provider was saved, but the connection test did not pass.'}
          </p>
        </motion.div>

        <div className="grid gap-3 md:grid-cols-2">
          <button
            type="button"
            onClick={() => {
              onClose()
              navigate(`/ingestion?tab=assets&provider=${createdProvider.id}&onboarding=true`)
            }}
            className={cn(
              'rounded-2xl border p-5 text-left transition-[colors,transform,box-shadow] duration-150 hover:-translate-y-0.5 hover:shadow-md',
              healthy
                ? 'border-indigo-500/30 bg-indigo-500/8'
                : 'border-glass-border bg-canvas-elevated opacity-60',
            )}
            disabled={!healthy}
          >
            <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-500/10 text-indigo-500">
              <Database className="h-5 w-5" />
            </div>
            <h4 className="text-base font-semibold text-ink">Discover data sources</h4>
            <p className="mt-2 text-sm text-ink-muted">
              Move directly into asset onboarding for this provider.
            </p>
          </button>

          <button
            type="button"
            onClick={onClose}
            className="rounded-2xl border border-glass-border bg-canvas-elevated p-5 text-left transition-[colors,transform,box-shadow] duration-150 hover:-translate-y-0.5 hover:shadow-md"
          >
            <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-black/5 text-ink dark:bg-white/5">
              <Pencil className="h-5 w-5" />
            </div>
            <h4 className="text-base font-semibold text-ink">Back to providers</h4>
            <p className="mt-2 text-sm text-ink-muted">
              Stay on the registry page and continue managing provider infrastructure.
            </p>
          </button>
        </div>

        {!healthy && (
          <div className="rounded-2xl border border-amber-500/20 bg-amber-500/10 px-5 py-4 text-sm text-amber-700 dark:text-amber-300">
            You can edit this provider from the registry once you’ve checked its host, port, credentials, or TLS settings.
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 px-4 py-6 animate-in fade-in duration-200">
        <div
          ref={modalRef}
          className="flex max-h-[88vh] w-full max-w-4xl flex-col overflow-hidden rounded-3xl border border-glass-border bg-canvas-elevated shadow-lg animate-in zoom-in-95 duration-200"
        >
          <div className="flex items-center justify-between border-b border-glass-border px-6 py-4">
            <div>
              <h2 className="text-lg font-bold text-ink">
                {mode === 'edit' ? `Edit ${provider?.name ?? 'Provider'}` : 'Provider Onboarding'}
              </h2>
              <p className="mt-0.5 text-sm text-ink-muted">
                {wizardPhase === 'success'
                  ? 'Provider created'
                  : activeStep?.title ?? 'Provider setup'}
              </p>
            </div>
            <button
              type="button"
              onClick={handleClose}
              className="rounded-lg p-2 text-ink-muted transition-colors hover:bg-black/5 hover:text-ink dark:hover:bg-white/5"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {wizardPhase === 'steps' && (
            <div className="flex items-center gap-2 border-b border-glass-border px-6 py-3">
              {steps.map((step, index) => {
                const StepIcon = step.icon
                const isComplete = index < currentStepIndex
                const isCurrent = index === currentStepIndex

                return (
                  <div key={step.id} className="flex items-center gap-2">
                    {index > 0 && (
                      <div className={cn(
                        'h-0.5 w-8 rounded-full',
                        isComplete ? 'bg-indigo-500' : 'bg-glass-border',
                      )} />
                    )}
                    <button
                      type="button"
                      onClick={() => {
                        if (index < currentStepIndex) {
                          setPreviousSteps(steps.slice(0, index).map((item) => item.id))
                          setCurrentStep(step.id)
                        }
                      }}
                      className={cn(
                        'flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium transition-colors duration-150',
                        isComplete
                          ? 'bg-indigo-500/10 text-indigo-500'
                          : isCurrent
                            ? 'bg-indigo-500 text-white shadow-md shadow-indigo-500/25'
                            : 'bg-black/5 text-ink-muted dark:bg-white/5',
                      )}
                    >
                      {isComplete ? <Check className="h-3 w-3" /> : <StepIcon className="h-3 w-3" />}
                      <span className="hidden sm:inline">{step.title}</span>
                    </button>
                  </div>
                )
              })}
            </div>
          )}

          <div className="flex-1 overflow-y-auto px-6 py-5 min-h-[480px]">
            {wizardPhase === 'steps' && <StepWarnings warnings={stepWarnings} />}

            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={wizardPhase === 'success' ? 'success' : currentStep}
                initial={{ opacity: 0, x: 18 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -18 }}
                transition={{ duration: 0.2 }}
                className="mt-4"
              >
                {wizardPhase === 'success'
                  ? renderSuccessPhase()
                  : currentStep === 'type'
                    ? renderTypeStep()
                    : currentStep === 'connection'
                      ? renderConnectionStep()
                      : currentStep === 'schema'
                        ? renderSchemaStep()
                        : renderReviewStep()}
              </motion.div>
            </AnimatePresence>

            {submitError && (
              <div className="mt-4 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-500">
                {submitError}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between border-t border-glass-border px-6 py-4">
            {wizardPhase === 'steps' ? (
              <>
                <button
                  type="button"
                  onClick={currentStepIndex === 0 ? handleClose : goBack}
                  className="flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium text-ink-secondary transition-colors hover:bg-black/5 hover:text-ink dark:hover:bg-white/5"
                >
                  <ChevronLeft className="h-4 w-4" />
                  {currentStepIndex === 0 ? 'Cancel' : 'Back'}
                </button>

                <button
                  type="button"
                  onClick={isLastStep ? primaryAction : goNext}
                  disabled={!canProceed || isSubmitting || (shouldRunConnectivityTest && connectivityCheck.state === 'checking')}
                  className={cn(
                    'flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-semibold transition-[colors,box-shadow] duration-150',
                    isLastStep
                      ? 'bg-gradient-to-r from-indigo-500 to-violet-600 text-white shadow-md hover:shadow-lg disabled:opacity-50'
                      : 'bg-indigo-500/10 text-indigo-500 hover:bg-indigo-500/20 disabled:opacity-50',
                  )}
                >
                  {isSubmitting ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : isLastStep ? (
                    <>
                      {shouldRunConnectivityTest ? (
                        connectivityCheck.state === 'failure' ? (
                          <RefreshCw className="h-4 w-4" />
                        ) : (
                          <Zap className="h-4 w-4" />
                        )
                      ) : (
                        <Zap className="h-4 w-4" />
                      )}
                      {mode === 'edit'
                        ? 'Save changes'
                        : shouldRunConnectivityTest
                          ? 'Test connection'
                          : 'Create provider'}
                    </>
                  ) : (
                    <>
                      Next
                      <ChevronRight className="h-4 w-4" />
                    </>
                  )}
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-lg px-4 py-2 text-sm font-medium text-ink-secondary transition-colors hover:bg-black/5 hover:text-ink dark:hover:bg-white/5"
                >
                  Done
                </button>
                {connectionResult?.success && createdProvider && (
                  <button
                    type="button"
                    onClick={() => {
                      onClose()
                      navigate(`/ingestion?tab=assets&provider=${createdProvider.id}&onboarding=true`)
                    }}
                    className="flex items-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-5 py-2.5 text-sm font-semibold text-white shadow-md transition-[colors,box-shadow] duration-150 hover:shadow-lg"
                  >
                    <Plus className="h-4 w-4" />
                    Continue to data sources
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      <ConfirmCloseDialog
        isOpen={showCloseConfirm}
        onCancel={() => setShowCloseConfirm(false)}
        onConfirm={confirmClose}
      />
    </>
  )
}
