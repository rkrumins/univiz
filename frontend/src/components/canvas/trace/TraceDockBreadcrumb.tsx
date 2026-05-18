import { ChevronRight, Home } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TraceV2Result } from '@/providers/GraphDataProvider'
import type { DrilldownKey } from '@/hooks/useUnifiedTrace'
import type { HierarchyNode } from '@/types/hierarchy'

export interface TraceDockBreadcrumbProps {
  focusId: string | null
  focusName: string
  drilldowns: Map<DrilldownKey, TraceV2Result>
  displayMap: Map<string, HierarchyNode>
  /** Collapses one drill. Caller invokes once per descendant key to cascade. */
  onCollapse: (key: DrilldownKey) => void
}

interface ParsedKey {
  sourceUrn: string
  targetUrn: string
  level: number
}

function parseKey(key: DrilldownKey): ParsedKey | null {
  const at = key.lastIndexOf('@')
  if (at < 0) return null
  const pair = key.slice(0, at)
  const arrow = pair.indexOf('->')
  if (arrow < 0) return null
  return {
    sourceUrn: pair.slice(0, arrow),
    targetUrn: pair.slice(arrow + 2),
    level: Number(key.slice(at + 1)),
  }
}

function labelForUrn(urn: string, displayMap: Map<string, HierarchyNode>): string {
  for (const node of displayMap.values()) {
    if (node.urn === urn) return node.name
  }
  // Fall back to last URN segment when no hydrated entity matches.
  const seg = urn.split(/[:/]/).pop() ?? urn
  return seg.length > 24 ? `${seg.slice(0, 22)}…` : seg
}

/**
 * Drill-stack breadcrumb. Rendered just below the dock title bar whenever
 * one or more drills are active. Clicking the focus root collapses every
 * drill; clicking any drill step collapses every drill *after* it,
 * preserving the click target itself. Map insertion order = chronological
 * drill order, which we rely on for "what comes after this step".
 */
export function TraceDockBreadcrumb({
  focusId,
  focusName,
  drilldowns,
  displayMap,
  onCollapse,
}: TraceDockBreadcrumbProps) {
  if (drilldowns.size === 0) return null

  const entries = Array.from(drilldowns.entries())

  const collapseAll = () => {
    for (const [k] of entries) onCollapse(k)
  }

  const collapseAfter = (idx: number) => {
    for (let i = idx + 1; i < entries.length; i++) {
      onCollapse(entries[i][0])
    }
  }

  return (
    <div
      role="navigation"
      aria-label="Trace drill breadcrumb"
      className={cn(
        'flex items-center gap-1 px-4 py-1.5 overflow-x-auto',
        'border-t border-white/[0.04]',
        'bg-canvas-elevated/40',
        'text-[11px] text-ink-muted',
      )}
    >
      <button
        type="button"
        onClick={collapseAll}
        disabled={!focusId}
        className={cn(
          'flex items-center gap-1 px-1.5 py-0.5 rounded-md',
          'hover:bg-accent-lineage/15 hover:text-ink',
          'transition-colors',
        )}
        title="Collapse all drills back to focus"
      >
        <Home className="w-3 h-3" strokeWidth={2.4} />
        <span className="font-medium max-w-[140px] truncate">{focusName}</span>
      </button>

      {entries.map(([key], idx) => {
        const parsed = parseKey(key)
        if (!parsed) return null
        const tgtLabel = labelForUrn(parsed.targetUrn, displayMap)
        const isLast = idx === entries.length - 1
        return (
          <div key={key} className="flex items-center gap-1 flex-shrink-0">
            <ChevronRight className="w-3 h-3 text-ink-muted/60" strokeWidth={2.4} />
            <button
              type="button"
              onClick={() => collapseAfter(idx)}
              disabled={isLast}
              className={cn(
                'flex items-center gap-1.5 px-1.5 py-0.5 rounded-md tabular-nums',
                isLast
                  ? 'text-ink cursor-default'
                  : 'hover:bg-accent-lineage/15 hover:text-ink transition-colors',
              )}
              title={isLast ? 'Current drill level' : `Collapse drills after L${parsed.level}`}
            >
              <span className="text-accent-lineage/80 font-semibold">L{parsed.level}</span>
              <span className="max-w-[120px] truncate">{tgtLabel}</span>
            </button>
          </div>
        )
      })}
    </div>
  )
}
