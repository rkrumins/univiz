/**
 * LineageNeighbors — 1-hop incoming/outgoing neighbor preview for the entity
 * drawer.
 *
 * Default state: two compact summary rows showing the upstream/downstream
 * counts (same shape as the previous "3 Data Sources / 7 Data Consumers"
 * placeholder). Clicking a row expands an inline detail panel with grouped
 * neighbors, entity-type chips, edge-type chips and free-text search.
 *
 * Data source: `canvas.edges` directly — same source the canvas renders
 * from. Deliberately bypasses `useNodeEdges` because that hook funnels
 * through the global edge-type filter (so when filters are toggled, this
 * section would lie about lineage). Decoupled from Trace Lineage: counts
 * reflect whatever edges currently touch this node, regardless of whether
 * a trace has been run.
 *
 * Click a neighbor row → swap drawer (openNodeDrawer) and, when wired,
 * center the canvas (onFocusNode prop).
 */

import { useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import * as LucideIcons from 'lucide-react'
import { useCanvasStore, type LineageNode, type LineageEdge } from '@/store/canvas'
import {
  useSchemaStore,
  normalizeEdgeType,
  isContainmentEdgeType,
  useContainmentEdgeTypes,
} from '@/store/schema'
import { generateColorFromType, generateEdgeColorFromType } from '@/lib/type-visuals'
import { cn } from '@/lib/utils'

interface LineageNeighborsProps {
  nodeId: string
  onFocusNode?: (nodeId: string) => void
}

type Direction = 'incoming' | 'outgoing'

interface NeighborRecord {
  edge: LineageEdge
  neighborId: string
  neighborNode: LineageNode | undefined
  direction: Direction
  edgeTypeNorm: string // upper-case
}

export function LineageNeighbors({ nodeId, onFocusNode }: LineageNeighborsProps) {
  const rawEdges = useCanvasStore((s) => s.edges)
  const visibleEdges = useCanvasStore((s) => s.visibleEdges)
  // Prefer the canvas's projected/aggregated visible-edge set so the section
  // mirrors exactly what the user sees on canvas (aggregated parent-level
  // edges, projected leaf edges, etc.). Falls back to raw store edges when
  // no canvas has published a visible set (e.g. before mount, on a canvas
  // variant that doesn't project).
  const edges = visibleEdges.length > 0 ? visibleEdges : rawEdges
  const nodes = useCanvasStore((s) => s.nodes)
  const openNodeDrawer = useCanvasStore((s) => s.openNodeDrawer)
  const containmentEdgeTypes = useContainmentEdgeTypes()

  const [expanded, setExpanded] = useState<Direction | null>(null)

  const nodeMap = useMemo(() => {
    const m = new Map<string, LineageNode>()
    for (const n of nodes) m.set(n.id, n)
    return m
  }, [nodes])

  // Lineage-only neighbors. Containment edges (parent ↔ child structural
  // relationships) are filtered out — the user wants flow lineage, not
  // hierarchy. Edge type is normalised to upper-case before checking against
  // the loaded ontology's containment set.
  const { incomingRecords, outgoingRecords } = useMemo(() => {
    const incoming: NeighborRecord[] = []
    const outgoing: NeighborRecord[] = []
    for (const e of edges) {
      const isIn = e.target === nodeId && e.source !== nodeId
      const isOut = e.source === nodeId && e.target !== nodeId
      if (!isIn && !isOut) continue
      const edgeTypeNorm = normalizeEdgeType(e)
      if (isContainmentEdgeType(edgeTypeNorm, containmentEdgeTypes)) continue
      const record: NeighborRecord = {
        edge: e,
        neighborId: isIn ? e.source : e.target,
        neighborNode: nodeMap.get(isIn ? e.source : e.target),
        direction: isIn ? 'incoming' : 'outgoing',
        edgeTypeNorm,
      }
      if (isIn) incoming.push(record)
      else outgoing.push(record)
    }
    return { incomingRecords: incoming, outgoingRecords: outgoing }
  }, [edges, nodeMap, nodeId, containmentEdgeTypes])

  const incomingCount = incomingRecords.length
  const outgoingCount = outgoingRecords.length

  const handleNeighborClick = (neighborId: string) => {
    openNodeDrawer(neighborId)
    onFocusNode?.(neighborId)
  }

  const toggle = (dir: Direction) =>
    setExpanded((prev) => (prev === dir ? null : dir))

  return (
    <div className="px-5 py-4">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          <LucideIcons.GitBranch className="w-4 h-4 text-ink-muted" />
          <h3 className="text-xs font-semibold text-ink-muted uppercase tracking-wider">
            Lineage
          </h3>
        </div>
      </div>

      <div className="space-y-2">
        <SummaryRow
          direction="incoming"
          label="Data Sources"
          subLabel="Upstream connections"
          count={incomingCount}
          expanded={expanded === 'incoming'}
          onToggle={() => toggle('incoming')}
        />
        <AnimatePresence initial={false}>
          {expanded === 'incoming' && (
            <ExpandedDetail
              records={incomingRecords}
              direction="incoming"
              onNeighborClick={handleNeighborClick}
            />
          )}
        </AnimatePresence>

        <SummaryRow
          direction="outgoing"
          label="Data Consumers"
          subLabel="Downstream connections"
          count={outgoingCount}
          expanded={expanded === 'outgoing'}
          onToggle={() => toggle('outgoing')}
        />
        <AnimatePresence initial={false}>
          {expanded === 'outgoing' && (
            <ExpandedDetail
              records={outgoingRecords}
              direction="outgoing"
              onNeighborClick={handleNeighborClick}
            />
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}

// ============================================
// Summary row — collapsed default
// ============================================

interface SummaryRowProps {
  direction: Direction
  label: string
  subLabel: string
  count: number
  expanded: boolean
  onToggle: () => void
}

function SummaryRow({
  direction,
  label,
  subLabel,
  count,
  expanded,
  onToggle,
}: SummaryRowProps) {
  const isIncoming = direction === 'incoming'
  const ArrowIcon = isIncoming
    ? LucideIcons.ArrowDownLeft
    : LucideIcons.ArrowUpRight
  const accent = isIncoming ? 'text-blue-500' : 'text-green-500'
  const accentBg = isIncoming ? 'bg-blue-500/10' : 'bg-green-500/10'
  const accentBorder = isIncoming
    ? 'border-blue-500/20'
    : 'border-green-500/20'

  const disabled = count === 0

  return (
    <button
      onClick={onToggle}
      disabled={disabled}
      className={cn(
        'w-full flex items-center gap-3 p-3 rounded-xl border transition-colors duration-150 text-left',
        expanded
          ? cn(accentBg, accentBorder)
          : 'bg-black/5 dark:bg-white/5 border-transparent hover:bg-black/10 dark:hover:bg-white/10',
        disabled && 'opacity-50 cursor-default hover:bg-black/5 dark:hover:bg-white/5',
      )}
    >
      <div
        className={cn(
          'w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0',
          accentBg,
        )}
      >
        <ArrowIcon className={cn('w-4 h-4', accent)} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className={cn('text-lg font-semibold tabular-nums', accent)}>
            {count}
          </span>
          <span className="text-sm font-medium text-ink truncate">
            {label}
          </span>
        </div>
        <span className="text-[11px] text-ink-muted">{subLabel}</span>
      </div>
      {count > 0 && (
        <LucideIcons.ChevronDown
          className={cn(
            'w-4 h-4 text-ink-muted transition-transform duration-150',
            expanded && 'rotate-180',
          )}
        />
      )}
    </button>
  )
}

// ============================================
// Expanded detail panel
// ============================================

interface ExpandedDetailProps {
  records: NeighborRecord[]
  direction: Direction
  onNeighborClick: (neighborId: string) => void
}

function ExpandedDetail({
  records,
  direction,
  onNeighborClick,
}: ExpandedDetailProps) {
  const [activeEntityTypes, setActiveEntityTypes] = useState<Set<string>>(
    new Set(),
  )
  const [activeEdgeTypes, setActiveEdgeTypes] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())

  const entityTypeFacets = useMemo(() => {
    const counts = new Map<string, number>()
    for (const r of records) {
      const t = r.neighborNode?.data.type ?? 'unknown'
      counts.set(t, (counts.get(t) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [records])

  const edgeTypeFacets = useMemo(() => {
    const counts = new Map<string, number>()
    for (const r of records) {
      counts.set(r.edgeTypeNorm, (counts.get(r.edgeTypeNorm) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [records])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return records.filter((r) => {
      if (
        activeEntityTypes.size > 0 &&
        !activeEntityTypes.has(r.neighborNode?.data.type ?? 'unknown')
      )
        return false
      if (activeEdgeTypes.size > 0 && !activeEdgeTypes.has(r.edgeTypeNorm))
        return false
      if (q) {
        const d = r.neighborNode?.data
        const hay = [
          d?.label,
          d?.businessLabel,
          d?.technicalLabel,
          d?.urn,
          r.neighborId,
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase()
        if (!hay.includes(q)) return false
      }
      return true
    })
  }, [records, activeEntityTypes, activeEdgeTypes, search])

  const grouped = useMemo(() => {
    const g = new Map<string, NeighborRecord[]>()
    for (const r of filtered) {
      const t = r.neighborNode?.data.type ?? 'unknown'
      const bucket = g.get(t) ?? []
      bucket.push(r)
      g.set(t, bucket)
    }
    return [...g.entries()].sort((a, b) => b[1].length - a[1].length)
  }, [filtered])

  const toggleSet = (
    set: Set<string>,
    setter: (s: Set<string>) => void,
    key: string,
  ) => {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setter(next)
  }

  const showSearch = records.length > 5
  const isFilteredEmpty = filtered.length === 0
  const unloadedCount = filtered.filter((r) => !r.neighborNode).length

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.18 }}
      className="overflow-hidden"
    >
      <div className="pt-2 pb-1 pl-2 space-y-3">
        {showSearch && (
          <div className="relative">
            <LucideIcons.Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-muted pointer-events-none" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter by name or URN…"
              className="w-full pl-9 pr-3 py-2 text-xs rounded-lg bg-white/5 border border-white/10 focus:border-accent-lineage/40 focus:bg-white/8 outline-none transition-colors duration-150"
            />
          </div>
        )}

        {entityTypeFacets.length > 1 && (
          <FilterChipRow
            label="Entity types"
            facets={entityTypeFacets}
            active={activeEntityTypes}
            onToggle={(k) => toggleSet(activeEntityTypes, setActiveEntityTypes, k)}
            variant="entity"
          />
        )}

        {edgeTypeFacets.length > 1 && (
          <FilterChipRow
            label="Edge types"
            facets={edgeTypeFacets}
            active={activeEdgeTypes}
            onToggle={(k) => toggleSet(activeEdgeTypes, setActiveEdgeTypes, k)}
            variant="edge"
          />
        )}

        <div className="space-y-1.5">
          {isFilteredEmpty ? (
            <EmptyState
              icon={LucideIcons.Filter}
              title="No matches"
              hint="Try clearing filters or the search."
            />
          ) : (
            grouped.map(([type, rows]) => (
              <EntityTypeGroup
                key={type}
                type={type}
                rows={rows}
                direction={direction}
                collapsed={collapsedGroups.has(type)}
                onToggleCollapse={() =>
                  toggleSet(collapsedGroups, setCollapsedGroups, type)
                }
                onNeighborClick={onNeighborClick}
              />
            ))
          )}

          {unloadedCount > 0 && !isFilteredEmpty && (
            <div className="text-[11px] text-ink-muted/70 italic px-1 pt-1">
              {unloadedCount} neighbor{unloadedCount === 1 ? '' : 's'} not loaded in view — expand the graph or use Trace to fetch.
            </div>
          )}
        </div>
      </div>
    </motion.div>
  )
}

// ============================================
// Filter chips
// ============================================

interface FilterChipRowProps {
  label: string
  facets: Array<[string, number]>
  active: Set<string>
  onToggle: (key: string) => void
  variant: 'entity' | 'edge'
}

function FilterChipRow({
  label,
  facets,
  active,
  onToggle,
  variant,
}: FilterChipRowProps) {
  return (
    <div>
      <div className="text-[10px] font-semibold text-ink-muted/70 uppercase tracking-wider mb-1.5">
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {facets.map(([key, count]) => (
          <FilterChip
            key={key}
            entryKey={key}
            count={count}
            active={active.has(key)}
            onClick={() => onToggle(key)}
            variant={variant}
          />
        ))}
      </div>
    </div>
  )
}

interface FilterChipProps {
  entryKey: string
  count: number
  active: boolean
  onClick: () => void
  variant: 'entity' | 'edge'
}

function FilterChip({
  entryKey,
  count,
  active,
  onClick,
  variant,
}: FilterChipProps) {
  const schema = useSchemaStore((s) => s.schema)

  let color = '#6b7280'
  let displayName = entryKey
  if (variant === 'entity') {
    const et = schema?.entityTypes.find((t) => t.id === entryKey)
    color = et?.visual.color ?? generateColorFromType(entryKey)
    displayName = et?.name ?? entryKey
  } else {
    const rt = schema?.relationshipTypes.find(
      (r) => r.id.toUpperCase() === entryKey.toUpperCase(),
    )
    color = rt?.visual.strokeColor ?? generateEdgeColorFromType(entryKey)
    displayName = rt?.name ?? entryKey.toLowerCase()
  }

  return (
    <button
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors duration-150 border',
        active
          ? 'border-transparent'
          : 'border-white/10 hover:border-white/20 text-ink-muted hover:text-ink',
      )}
      style={
        active
          ? { backgroundColor: `${color}26`, color }
          : undefined
      }
    >
      <span
        className="w-1.5 h-1.5 rounded-full flex-shrink-0"
        style={{ backgroundColor: color }}
      />
      <span className="truncate max-w-[140px]">{displayName}</span>
      <span className="text-[10px] opacity-70 tabular-nums">{count}</span>
    </button>
  )
}

// ============================================
// Grouped neighbor list
// ============================================

interface EntityTypeGroupProps {
  type: string
  rows: NeighborRecord[]
  direction: Direction
  collapsed: boolean
  onToggleCollapse: () => void
  onNeighborClick: (neighborId: string) => void
}

function EntityTypeGroup({
  type,
  rows,
  direction,
  collapsed,
  onToggleCollapse,
  onNeighborClick,
}: EntityTypeGroupProps) {
  const schema = useSchemaStore((s) => s.schema)
  const entityType = schema?.entityTypes.find((t) => t.id === type)
  const color = entityType?.visual.color ?? generateColorFromType(type)
  const displayName =
    entityType?.pluralName ?? entityType?.name ?? type
  const IconCmp =
    (entityType?.visual.icon &&
      ((LucideIcons as unknown as Record<string, unknown>)[
        entityType.visual.icon
      ] as React.ComponentType<{ className?: string }> | undefined)) ||
    undefined

  return (
    <div className="rounded-lg bg-black/[0.04] dark:bg-white/[0.04] overflow-hidden">
      <button
        onClick={onToggleCollapse}
        className="w-full flex items-center gap-2 px-2.5 py-1.5 hover:bg-white/5 transition-colors duration-150"
      >
        <LucideIcons.ChevronDown
          className={cn(
            'w-3 h-3 text-ink-muted transition-transform duration-150',
            collapsed && '-rotate-90',
          )}
        />
        <span
          className="w-1.5 h-1.5 rounded-full flex-shrink-0"
          style={{ backgroundColor: color }}
        />
        {IconCmp && <IconCmp className="w-3 h-3 text-ink-muted" />}
        <span className="text-[11px] font-semibold text-ink uppercase tracking-wide">
          {displayName}
        </span>
        <span className="ml-auto text-[10px] text-ink-muted tabular-nums">
          {rows.length}
        </span>
      </button>
      {!collapsed && (
        <div className="divide-y divide-white/5">
          {rows.map((r) => (
            <NeighborRow
              key={r.edge.id + ':' + r.direction}
              record={r}
              direction={direction}
              onClick={() => onNeighborClick(r.neighborId)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function NeighborRow({
  record,
  direction,
  onClick,
}: {
  record: NeighborRecord
  direction: Direction
  onClick: () => void
}) {
  const { neighborNode, edgeTypeNorm, neighborId } = record
  const isIncoming = direction === 'incoming'
  const arrowColor = isIncoming ? 'text-blue-500' : 'text-green-500'
  const ArrowIcon = isIncoming
    ? LucideIcons.ArrowDownLeft
    : LucideIcons.ArrowUpRight

  const data = neighborNode?.data
  const label =
    data?.label || data?.businessLabel || data?.urn || neighborId
  const secondary = data?.urn ?? neighborId

  return (
    <button
      onClick={onClick}
      className="w-full flex items-center gap-2.5 px-3 py-1.5 hover:bg-white/[0.06] transition-colors duration-150 group text-left"
      title={secondary}
    >
      <ArrowIcon className={cn('w-3 h-3 flex-shrink-0', arrowColor)} />
      <div className="flex-1 min-w-0">
        <div className="text-[12px] text-ink truncate">{label}</div>
        {neighborNode ? (
          secondary !== label && (
            <div className="text-[10px] text-ink-muted truncate font-mono">
              {secondary}
            </div>
          )
        ) : (
          <div className="text-[10px] text-amber-500/80">
            Not loaded in view
          </div>
        )}
      </div>
      <EdgeTypeChip edgeType={edgeTypeNorm} />
      <LucideIcons.ChevronRight className="w-3 h-3 text-ink-muted opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0" />
    </button>
  )
}

function EdgeTypeChip({ edgeType }: { edgeType: string }) {
  const schema = useSchemaStore((s) => s.schema)
  const rt = schema?.relationshipTypes.find(
    (r) => r.id.toUpperCase() === edgeType.toUpperCase(),
  )
  const color = rt?.visual.strokeColor ?? generateEdgeColorFromType(edgeType)
  const displayName = rt?.name ?? edgeType.toLowerCase()

  return (
    <span
      className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
      style={{ backgroundColor: `${color}1a`, color }}
    >
      {displayName}
    </span>
  )
}

function EmptyState({
  icon: Icon,
  title,
  hint,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  hint: string
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-5 px-4 rounded-lg bg-black/[0.03] dark:bg-white/[0.03]">
      <Icon className="w-4 h-4 text-ink-muted/60 mb-1.5" />
      <p className="text-[11px] font-medium text-ink-muted">{title}</p>
      <p className="text-[10px] text-ink-muted/70 mt-0.5">{hint}</p>
    </div>
  )
}
