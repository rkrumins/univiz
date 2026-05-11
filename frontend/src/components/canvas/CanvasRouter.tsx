/**
 * CanvasRouter - Thin routing shell that switches between canvas types
 *
 * Renders the appropriate canvas component based on the view's layout type:
 * - 'graph' → GraphCanvas (unified React Flow graph, replaces LineageCanvas)
 * - 'hierarchy' | 'tree' → HierarchyCanvas (Hierarchy-style nested view)
 * - 'reference' → ReferenceModelCanvas (context view)
 * - 'layered-lineage' → GraphCanvas (unified React Flow graph, replaces LayeredLineageCanvas)
 *
 * All data loading is handled by useGraphHydration (called here and in canvas components).
 */

import { Suspense, useMemo } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { motion, AnimatePresence } from 'framer-motion'
import { AlertTriangle, Loader2, RefreshCw, WifiOff } from 'lucide-react'
import { useSchemaStore } from '@/store/schema'
import { useGraphProviderContext } from '@/providers/GraphProviderContext'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { useGraphHydration } from '@/hooks/useGraphHydration'
import { useLoadingToast } from '@/components/ui/toast'
import { GraphCanvas } from './GraphCanvas'
import { HierarchyCanvas } from './HierarchyCanvas'
import { ReferenceModelCanvas } from './ReferenceModelCanvas'
import { cn } from '@/lib/utils'

interface CanvasRouterProps {
  className?: string
  /** Override the layout type used for canvas selection.
   *
   * ViewPage passes this directly from useViewNavigation() so the correct
   * canvas renders even when the schema store's activeViewId races with
   * loadFromBackend during a cross-workspace scope transition.
   * Without this prop, CanvasRouter falls back to getActiveView()?.layout.type
   * which may be undefined if loadFromBackend hasn't re-set activeViewId yet.
   */
  layoutType?: string
}

export function CanvasRouter({ className, layoutType: layoutTypeProp }: CanvasRouterProps) {
  const activeView = useSchemaStore((s) => s.getActiveView())
  const { providerVersion } = useGraphProviderContext()
  // Prefer the prop (from navigation pipeline) over the store lookup.
  // This avoids the race where loadFromBackend resets activeViewId to null
  // during a cross-workspace transition, causing a 'graph' fallback.
  const layoutType = layoutTypeProp ?? activeView?.layout.type ?? 'graph'

  // Single source of truth for initial graph data loading.
  // Only CanvasRouter passes hydrate=true — canvas components use the hook
  // without hydration (loadChildren/searchChildren only).
  const { hydrationError, hydrationPhase, isLoading: isHydrating } = useGraphHydration({ hydrate: true })
  const isInitialLoad = isHydrating && hydrationPhase !== 'complete'
  useLoadingToast('hydration', isInitialLoad && !hydrationError, hydrationPhase === 'roots' ? 'Loading graph data' : hydrationPhase === 'edges' ? 'Loading relationships' : 'Preparing view')

  // Memoize canvas selection based on view layout type
  const CanvasComponent = useMemo(() => {
    if (layoutType === 'layered-lineage') return GraphCanvas
    if (layoutType === 'reference') return ReferenceModelCanvas

    switch (layoutType) {
      case 'hierarchy':
      case 'tree':
        return HierarchyCanvas
      case 'graph':
      default:
        return GraphCanvas
    }
  }, [layoutType])

  return (
    <ErrorBoundary
      resetKeys={[activeView?.id, providerVersion]}
      fallback={(error, reset) => <CanvasError error={error} onRetry={reset} />}
    >
    <ReactFlowProvider>
    <div className={cn("relative w-full h-full", className)}>
      <AnimatePresence>
        <motion.div
          key={layoutType}
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.95 }}
          className="absolute inset-0"
        >
          <Suspense fallback={<CanvasLoader />}>
            <CanvasComponent />
          </Suspense>
        </motion.div>
      </AnimatePresence>

      {hydrationError && (
        <ProviderUnavailableOverlay message={hydrationError} />
      )}

      {activeView && layoutType !== 'graph' && (
        <div className="absolute top-4 left-4 z-10 pointer-events-none">
          <ViewBadge
            name={activeView.name}
            layoutType={layoutType}
            entityCount={activeView.content.visibleEntityTypes.length}
          />
        </div>
      )}
    </div>
    </ReactFlowProvider>
    </ErrorBoundary>
  )
}

function ProviderUnavailableOverlay({ message }: { message: string }) {
  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-canvas/80 backdrop-blur-sm pointer-events-none">
      <div className="flex flex-col items-center gap-3 max-w-sm text-center pointer-events-auto">
        <div className="w-10 h-10 rounded-full bg-amber-100 dark:bg-amber-950/40 flex items-center justify-center">
          <WifiOff className="w-5 h-5 text-amber-500" />
        </div>
        <h3 className="text-base font-semibold text-ink">Provider Unavailable</h3>
        <p className="text-sm text-ink-muted">{message}</p>
        <p className="text-xs text-ink-muted/70">
          The canvas will automatically reload when the provider recovers.
        </p>
      </div>
    </div>
  )
}

function CanvasError({ error, onRetry }: { error: Error; onRetry: () => void }) {
  return (
    <div className="w-full h-full flex items-center justify-center bg-canvas">
      <div className="flex flex-col items-center gap-4 max-w-md text-center">
        <div className="w-12 h-12 rounded-full bg-red-100 dark:bg-red-950/40 flex items-center justify-center">
          <AlertTriangle className="w-6 h-6 text-red-500" />
        </div>
        <h3 className="text-lg font-semibold text-ink">This view encountered an error</h3>
        <p className="text-sm text-ink-muted">{error.message}</p>
        <button
          onClick={onRetry}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-lineage text-white text-sm font-medium hover:bg-accent-lineage/90 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
          Retry
        </button>
      </div>
    </div>
  )
}

function CanvasLoader() {
  return (
    <div className="w-full h-full flex items-center justify-center bg-canvas">
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="w-8 h-8 animate-spin text-accent-lineage" />
        <span className="text-sm text-ink-muted">Loading view...</span>
      </div>
    </div>
  )
}

interface ViewBadgeProps {
  name: string
  layoutType: string
  entityCount: number
}

function ViewBadge({ name, layoutType, entityCount }: ViewBadgeProps) {
  const layoutLabels: Record<string, string> = {
    graph: 'Graph',
    hierarchy: 'Hierarchy',
    tree: 'Tree',
    list: 'List',
    grid: 'Grid',
    timeline: 'Timeline',
    'layered-lineage': 'Layered Lineage',
    reference: 'Context View',
  }

  return (
    <div className="flex items-center gap-2">
      <div className="glass-panel-subtle rounded-lg px-3 py-1.5 flex items-center gap-2">
        <span className="text-sm font-medium text-ink">{name}</span>
        <span className="px-1.5 py-0.5 rounded text-2xs font-medium bg-accent-lineage/10 text-accent-lineage">
          {layoutLabels[layoutType] ?? layoutType}
        </span>
        <span className="text-2xs text-ink-muted">
          {entityCount} types
        </span>
      </div>
    </div>
  )
}

export default CanvasRouter
