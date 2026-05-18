/**
 * LineageNeighbors — 1-hop incoming/outgoing neighbor preview for the entity
 * drawer.
 *
 * Default state: two card-style summary rows showing upstream/downstream
 * counts. Clicking a card expands an inline panel with grouped neighbors,
 * always-on search, and entity-type / edge-type filter chips.
 *
 * Data source: `canvas.visibleEdges` (the projected/aggregated edge set the
 * canvas renders) with a fallback to raw `canvas.edges`. Containment edges
 * are filtered out via the schema's containment set. Fully decoupled from
 * Trace Lineage — counts reflect whatever lineage edges currently touch
 * this node.
 *
 * Click a neighbor row → swap drawer (openNodeDrawer) and, when wired,
 * center the canvas (onFocusNode prop).
 */

import { useEffect, useMemo, useState } from 'react'
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
  /** Reveal the target on canvas (expand ancestors, pan/scroll). May
   *  return a promise — the clicked row shows an inline spinner until it
   *  resolves. */
  onFocusNode?: (nodeId: string) => void | Promise<void>
}

type Direction = 'incoming' | 'outgoing'

interface NeighborRecord {
  edge: LineageEdge
  neighborId: string
  neighborNode: LineageNode | undefined
  direction: Direction
  edgeTypeNorm: string
}

export function LineageNeighbors({ nodeId, onFocusNode }: LineageNeighborsProps) {
  const rawEdges = useCanvasStore((s) => s.edges)
  const visibleEdges = useCanvasStore((s) => s.visibleEdges)
  // Mirror the canvas: prefer the projected/aggregated visible set; fall
  // back to raw edges when no canvas has published one yet.
  const edges = visibleEdges.length > 0 ? visibleEdges : rawEdges
  const nodes = useCanvasStore((s) => s.nodes)
  const openNodeDrawer = useCanvasStore((s) => s.openNodeDrawer)
  const selectNode = useCanvasStore((s) => s.selectNode)
  const containmentEdgeTypes = useContainmentEdgeTypes()

  const [expanded, setExpanded] = useState<Direction | null>(null)

  // Reset expansion when the drawer's focal entity changes. Without this,
  // navigating from one entity to a neighbor leaves the previous entity's
  // expand state in place — the new entity's "Data Sources" looks already
  // expanded, so the user's intended-to-expand click reads as a collapse
  // ("first click does nothing"). Children's filter/search state lives
  // inside ExpandedDetail which unmounts on collapse, so it resets here too.
  useEffect(() => {
    setExpanded(null)
  }, [nodeId])

  const nodeMap = useMemo(() => {
    const m = new Map<string, LineageNode>()
    for (const n of nodes) m.set(n.id, n)
    return m
  }, [nodes])

  // Lineage-only neighbors. Containment edges (structural parent ↔ child) are
  // filtered out — the section is about flow lineage.
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
  const totalCount = incomingCount + outgoingCount

  const handleNeighborClick = async (neighborId: string) => {
    // Drawer-swap first (instant, no awaiting). selectNode so the canvas's
    // selection-driven highlight (useHighlightState in GraphCanvas, the
    // selectedNodeId styling in ContextView) lights up the target after the
    // reveal pans to it.
    openNodeDrawer(neighborId)
    selectNode(neighborId)
    await onFocusNode?.(neighborId)
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
        {totalCount > 0 && (
          <span className="text-[10px] font-medium text-ink-muted/80 tabular-nums">
            {totalCount} connection{totalCount === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div className="space-y-2">
        <DirectionCard
          direction="incoming"
          label="Data Sources"
          subLabel="Upstream connections"
          count={incomingCount}
          records={incomingRecords}
          expanded={expanded === 'incoming'}
          onToggle={() => toggle('incoming')}
          onNeighborClick={handleNeighborClick}
        />
        <DirectionCard
          direction="outgoing"
          label="Data Consumers"
          subLabel="Downstream connections"
          count={outgoingCount}
          records={outgoingRecords}
          expanded={expanded === 'outgoing'}
          onToggle={() => toggle('outgoing')}
          onNeighborClick={handleNeighborClick}
        />
      </div>
    </div>
  )
}

// ============================================
// Direction card — summary + expandable detail in a single container
// ============================================

interface DirectionCardProps {
  direction: Direction
  label: string
  subLabel: string
  count: number
  records: NeighborRecord[]
  expanded: boolean
  onToggle: () => void
  onNeighborClick: (neighborId: string) => void | Promise<void>
}

function DirectionCard({
  direction,
  label,
  subLabel,
  count,
  records,
  expanded,
  onToggle,
  onNeighborClick,
}: DirectionCardProps) {
  const isIncoming = direction === 'incoming'
  const ArrowIcon = isIncoming
    ? LucideIcons.ArrowDownLeft
    : LucideIcons.ArrowUpRight

  // Colour tokens — kept locally so the gradient/border/text accents
  // stay coherent without spreading classnames through the tree.
  const tokens = isIncoming
    ? {
        accent: 'text-blue-500',
        bg: 'bg-blue-500/10',
        bgHover: 'group-hover:bg-blue-500/15',
        ring: 'border-blue-500/20',
        ringExpanded: 'border-blue-500/40',
        gradient:
          'bg-[linear-gradient(135deg,rgba(59,130,246,0.08)_0%,transparent_55%)]',
      }
    : {
        accent: 'text-green-500',
        bg: 'bg-green-500/10',
        bgHover: 'group-hover:bg-green-500/15',
        ring: 'border-green-500/20',
        ringExpanded: 'border-green-500/40',
        gradient:
          'bg-[linear-gradient(135deg,rgba(34,197,94,0.08)_0%,transparent_55%)]',
      }

  const disabled = count === 0

  return (
    <div
      className={cn(
        'rounded-2xl border transition-colors duration-200 overflow-hidden',
        expanded ? tokens.ringExpanded : tokens.ring,
        disabled && 'opacity-60',
      )}
    >
      {/* Summary header */}
      <button
        type="button"
        onClick={onToggle}
        disabled={disabled}
        className={cn(
          'group relative w-full flex items-center gap-3 p-3.5 text-left transition-colors duration-200',
          tokens.gradient,
          !disabled && 'hover:bg-white/[0.02] cursor-pointer',
          disabled && 'cursor-default',
        )}
      >
        <div
          className={cn(
            'w-11 h-11 rounded-xl flex items-center justify-center flex-shrink-0 transition-colors duration-200',
            tokens.bg,
            !disabled && tokens.bgHover,
          )}
        >
          <ArrowIcon className={cn('w-5 h-5', tokens.accent)} />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2">
            <span
              className={cn(
                'text-2xl font-display font-semibold tabular-nums leading-none',
                tokens.accent,
              )}
            >
              {count}
            </span>
            <span className="text-sm font-medium text-ink truncate">
              {label}
            </span>
          </div>
          <div className="text-[11px] text-ink-muted mt-0.5">{subLabel}</div>
        </div>

        {!disabled && (
          <div
            className={cn(
              'w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 transition-colors duration-200',
              expanded
                ? cn(tokens.bg, tokens.accent)
                : 'text-ink-muted group-hover:bg-white/10 group-hover:text-ink',
            )}
          >
            <LucideIcons.ChevronDown
              className={cn(
                'w-4 h-4 transition-transform duration-200',
                expanded && 'rotate-180',
              )}
            />
          </div>
        )}
      </button>

      <AnimatePresence initial={false}>
        {expanded && !disabled && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="border-t border-white/[0.06]">
              <ExpandedDetail
                records={records}
                direction={direction}
                onNeighborClick={onNeighborClick}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ============================================
// Expanded detail
// ============================================

interface ExpandedDetailProps {
  records: NeighborRecord[]
  direction: Direction
  onNeighborClick: (neighborId: string) => void | Promise<void>
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

  const activeFilterCount = activeEntityTypes.size + activeEdgeTypes.size
  const clearAllFilters = () => {
    setActiveEntityTypes(new Set())
    setActiveEdgeTypes(new Set())
    setSearch('')
  }

  const isFilteredEmpty = filtered.length === 0
  const unloadedCount = filtered.filter((r) => !r.neighborNode).length

  return (
    <div className="p-3 space-y-3">
      {/* Search — always visible when expanded so the filter affordance is
          discoverable even with few results. */}
      <div className="relative">
        <LucideIcons.Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-muted pointer-events-none" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name or URN…"
          className="w-full pl-9 pr-8 py-2 text-xs rounded-lg bg-black/10 dark:bg-white/[0.04] border border-white/10 focus:border-accent-lineage/40 focus:bg-white/[0.06] outline-none transition-colors duration-150 placeholder:text-ink-muted/70"
        />
        {search && (
          <button
            type="button"
            onClick={() => setSearch('')}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-md text-ink-muted hover:text-ink hover:bg-white/10 transition-colors duration-150"
            title="Clear search"
          >
            <LucideIcons.X className="w-3 h-3" />
          </button>
        )}
      </div>

      {/* Active filter strip — quick visibility of what's narrowing the list,
          plus a one-click escape hatch. */}
      {activeFilterCount > 0 && (
        <div className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg bg-accent-lineage/5 border border-accent-lineage/20">
          <span className="text-[11px] text-accent-lineage font-medium">
            {activeFilterCount} filter{activeFilterCount === 1 ? '' : 's'} active
          </span>
          <button
            type="button"
            onClick={clearAllFilters}
            className="text-[10px] font-medium text-accent-lineage/80 hover:text-accent-lineage uppercase tracking-wide"
          >
            Clear all
          </button>
        </div>
      )}

      {/* Filter facets — only render when there's something to choose. */}
      {entityTypeFacets.length > 1 && (
        <FilterChipRow
          label="Entity type"
          icon={LucideIcons.Tag}
          facets={entityTypeFacets}
          active={activeEntityTypes}
          onToggle={(k) => toggleSet(activeEntityTypes, setActiveEntityTypes, k)}
          variant="entity"
        />
      )}
      {edgeTypeFacets.length > 1 && (
        <FilterChipRow
          label="Relationship"
          icon={LucideIcons.Link2}
          facets={edgeTypeFacets}
          active={activeEdgeTypes}
          onToggle={(k) => toggleSet(activeEdgeTypes, setActiveEdgeTypes, k)}
          variant="edge"
        />
      )}

      {/* Results */}
      <div className="space-y-1.5">
        {isFilteredEmpty ? (
          <EmptyState
            icon={
              activeFilterCount > 0 || search
                ? LucideIcons.SearchX
                : LucideIcons.Unlink
            }
            title={
              activeFilterCount > 0 || search
                ? 'No matching connections'
                : 'No connections in this direction'
            }
            hint={
              activeFilterCount > 0 || search
                ? 'Try clearing filters or the search.'
                : undefined
            }
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
          <div className="flex items-start gap-1.5 text-[11px] text-ink-muted/70 px-2 pt-1.5">
            <LucideIcons.Info className="w-3 h-3 flex-shrink-0 mt-0.5" />
            <span>
              {unloadedCount} neighbor{unloadedCount === 1 ? '' : 's'} not
              currently rendered on canvas — expand the graph to see details.
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================
// Filter chips
// ============================================

interface FilterChipRowProps {
  label: string
  icon: React.ComponentType<{ className?: string }>
  facets: Array<[string, number]>
  active: Set<string>
  onToggle: (key: string) => void
  variant: 'entity' | 'edge'
}

function FilterChipRow({
  label,
  icon: Icon,
  facets,
  active,
  onToggle,
  variant,
}: FilterChipRowProps) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1.5">
        <Icon className="w-3 h-3 text-ink-muted/70" />
        <span className="text-[10px] font-semibold text-ink-muted/70 uppercase tracking-wider">
          {label}
        </span>
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
      type="button"
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 pl-1.5 pr-2 py-1 rounded-full text-[11px] font-medium transition-all duration-150 border',
        active
          ? 'border-transparent shadow-sm'
          : 'border-white/10 bg-white/[0.03] hover:bg-white/[0.06] hover:border-white/20 text-ink-muted hover:text-ink',
      )}
      style={
        active
          ? { backgroundColor: `${color}28`, color, borderColor: `${color}40` }
          : undefined
      }
    >
      <span
        className="w-2 h-2 rounded-full flex-shrink-0"
        style={{ backgroundColor: color }}
      />
      <span className="truncate max-w-[120px]">{displayName}</span>
      <span
        className={cn(
          'text-[10px] tabular-nums px-1 py-px rounded-full',
          active ? 'bg-white/10' : 'bg-white/[0.04]',
        )}
        style={active ? { color } : undefined}
      >
        {count}
      </span>
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
  onNeighborClick: (neighborId: string) => void | Promise<void>
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
    <div className="rounded-xl bg-white/[0.02] border border-white/[0.06] overflow-hidden">
      <button
        type="button"
        onClick={onToggleCollapse}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-white/[0.04] transition-colors duration-150"
      >
        <LucideIcons.ChevronDown
          className={cn(
            'w-3.5 h-3.5 text-ink-muted/70 transition-transform duration-200',
            collapsed && '-rotate-90',
          )}
        />
        <div
          className="w-5 h-5 rounded-md flex items-center justify-center flex-shrink-0"
          style={{ backgroundColor: `${color}1f` }}
        >
          {IconCmp ? (
            <IconCmp className="w-3 h-3" />
          ) : (
            <span
              className="w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: color }}
            />
          )}
        </div>
        <span
          className="text-[11px] font-semibold uppercase tracking-wide"
          style={{ color }}
        >
          {displayName}
        </span>
        <span className="ml-auto inline-flex items-center justify-center min-w-[20px] h-[18px] px-1.5 rounded-full bg-white/[0.06] text-[10px] font-medium text-ink-muted tabular-nums">
          {rows.length}
        </span>
      </button>
      <AnimatePresence initial={false}>
        {!collapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="border-t border-white/[0.04] divide-y divide-white/[0.04]">
              {rows.map((r) => (
                <NeighborRow
                  key={r.edge.id + ':' + r.direction}
                  record={r}
                  direction={direction}
                  onClick={() => onNeighborClick(r.neighborId)}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
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
  onClick: () => void | Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const { neighborNode, edgeTypeNorm, neighborId } = record
  const isIncoming = direction === 'incoming'
  const accent = isIncoming ? 'text-blue-500' : 'text-green-500'
  const accentBg = isIncoming ? 'bg-blue-500/10' : 'bg-green-500/10'
  const ArrowIcon = isIncoming
    ? LucideIcons.ArrowDownLeft
    : LucideIcons.ArrowUpRight

  const data = neighborNode?.data
  const label =
    data?.businessLabel || data?.label || data?.urn || neighborId
  const technical = data?.technicalLabel
  const secondary =
    technical && technical !== label ? technical : data?.urn ?? neighborId
  const showSecondary = secondary && secondary !== label

  // Reveal lifecycle: while the parent's `onClick` (the canvas's reveal
  // cascade) is in flight, swap the trailing chevron for a spinner and
  // disable the row to avoid double-triggers.
  const handleClick = async () => {
    if (busy) return
    setBusy(true)
    try {
      await onClick()
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={busy}
      className={cn(
        'group relative w-full flex items-center gap-2.5 px-3 py-2 hover:bg-white/[0.05] transition-colors duration-150 text-left',
        busy && 'cursor-progress',
      )}
      title={data?.urn ?? neighborId}
    >
      <div
        className={cn(
          'w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0 transition-colors duration-150',
          accentBg,
          !busy && 'group-hover:scale-105',
        )}
      >
        <ArrowIcon className={cn('w-3 h-3', accent)} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-[12.5px] text-ink truncate font-medium leading-snug">
          {label}
        </div>
        {neighborNode ? (
          showSecondary && (
            <div className="text-[10px] text-ink-muted truncate font-mono leading-tight">
              {secondary}
            </div>
          )
        ) : (
          <div className="text-[10px] text-amber-500/90 flex items-center gap-1 leading-tight">
            <LucideIcons.AlertCircle className="w-2.5 h-2.5" />
            Not rendered on canvas
          </div>
        )}
      </div>
      <EdgeTypeChip edgeType={edgeTypeNorm} />
      {busy ? (
        <LucideIcons.Loader2
          data-testid="reveal-spinner"
          className={cn('w-3.5 h-3.5 animate-spin flex-shrink-0', accent)}
        />
      ) : (
        <LucideIcons.ArrowRight
          className={cn(
            'w-3.5 h-3.5 text-ink-muted/70 transition-all duration-150 flex-shrink-0',
            'opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0',
          )}
        />
      )}
    </button>
  )
}

function EdgeTypeChip({ edgeType }: { edgeType: string }) {
  const schema = useSchemaStore((s) => s.schema)
  const rt = schema?.relationshipTypes.find(
    (r) => r.id.toUpperCase() === edgeType.toUpperCase(),
  )
  const color = rt?.visual.strokeColor ?? generateEdgeColorFromType(edgeType)
  const displayName = rt?.name ?? edgeType.toLowerCase().replace(/_/g, ' ')

  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9.5px] font-semibold uppercase tracking-wide whitespace-nowrap"
      style={{ backgroundColor: `${color}1a`, color }}
    >
      <span
        className="w-1 h-1 rounded-full"
        style={{ backgroundColor: color }}
      />
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
  hint?: string
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-6 px-4 rounded-xl bg-black/[0.02] dark:bg-white/[0.02] border border-dashed border-white/[0.08]">
      <div className="w-9 h-9 rounded-full bg-white/[0.04] flex items-center justify-center mb-2">
        <Icon className="w-4 h-4 text-ink-muted/60" />
      </div>
      <p className="text-[11.5px] font-medium text-ink-muted">{title}</p>
      {hint && (
        <p className="text-[10.5px] text-ink-muted/70 mt-0.5">{hint}</p>
      )}
    </div>
  )
}
