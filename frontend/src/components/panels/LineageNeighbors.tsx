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
  /** Reveal a set of neighbors at once and fit the canvas around them.
   *  Used by the multi-select action bar. Implementations may run each
   *  reveal in parallel; the drawer doesn't swap when this fires. */
  onLocateMany?: (nodeIds: string[]) => void | Promise<void>
}

type SortMode = 'default' | 'name-asc' | 'name-desc'

type Direction = 'incoming' | 'outgoing'

interface NeighborRecord {
  edge: LineageEdge
  neighborId: string
  neighborNode: LineageNode | undefined
  direction: Direction
  edgeTypeNorm: string
}

export function LineageNeighbors({ nodeId, onFocusNode, onLocateMany }: LineageNeighborsProps) {
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
          onLocateMany={onLocateMany}
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
          onLocateMany={onLocateMany}
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
  onLocateMany?: (nodeIds: string[]) => void | Promise<void>
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
  onLocateMany,
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
                onLocateMany={onLocateMany}
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
  onLocateMany?: (nodeIds: string[]) => void | Promise<void>
}

function ExpandedDetail({
  records,
  direction,
  onNeighborClick,
  onLocateMany,
}: ExpandedDetailProps) {
  const [activeEntityTypes, setActiveEntityTypes] = useState<Set<string>>(
    new Set(),
  )
  const [activeEdgeTypes, setActiveEdgeTypes] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())
  const [sortMode, setSortMode] = useState<SortMode>('default')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [locateBusy, setLocateBusy] = useState(false)
  // Range-select anchor: id of the last single-toggled row. Shift-clicking
  // another row selects everything between them in visible order.
  const [lastSelectedId, setLastSelectedId] = useState<string | null>(null)

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
    // Sort rows within each group per the selected SortMode. Groups
    // themselves stay ordered by descending size — keeps the most-relevant
    // entity types at the top regardless of internal row order.
    if (sortMode !== 'default') {
      const labelOf = (r: NeighborRecord) => {
        const d = r.neighborNode?.data
        return (d?.businessLabel || d?.label || d?.urn || r.neighborId).toLowerCase()
      }
      const cmp = sortMode === 'name-asc'
        ? (a: NeighborRecord, b: NeighborRecord) => labelOf(a).localeCompare(labelOf(b))
        : (a: NeighborRecord, b: NeighborRecord) => labelOf(b).localeCompare(labelOf(a))
      for (const [, rows] of g) rows.sort(cmp)
    }
    return [...g.entries()].sort((a, b) => b[1].length - a[1].length)
  }, [filtered, sortMode])

  // Multi-select helpers ------------------------------------------------
  // Visible-order id list (post filter + sort + group). Drives both
  // shift-click range-select and the "Select all" button.
  const flatVisibleIds = useMemo(
    () => grouped.flatMap(([, rows]) => rows.map((r) => r.neighborId)),
    [grouped],
  )

  // Row checkbox click. With shift held, selects every row between the
  // anchor and this row (inclusive) in visible order. Without shift,
  // toggles single + updates the anchor. The set-union semantics match
  // Finder / VSCode shift-click.
  const handleRowSelectClick = (id: string, shiftKey: boolean) => {
    if (shiftKey && lastSelectedId && lastSelectedId !== id) {
      const anchorIdx = flatVisibleIds.indexOf(lastSelectedId)
      const targetIdx = flatVisibleIds.indexOf(id)
      if (anchorIdx >= 0 && targetIdx >= 0) {
        const [lo, hi] =
          anchorIdx < targetIdx
            ? [anchorIdx, targetIdx]
            : [targetIdx, anchorIdx]
        const range = flatVisibleIds.slice(lo, hi + 1)
        setSelectedIds(new Set([...selectedIds, ...range]))
        setLastSelectedId(id)
        return
      }
    }
    // Single-toggle fallback (no anchor, anchor filtered out, or no shift).
    const next = new Set(selectedIds)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelectedIds(next)
    setLastSelectedId(id)
  }

  // Group-level toggle: if every row in the group is selected, remove
  // them all; otherwise add the missing ones. Three-state UI (none /
  // partial / all) collapses to two-state interaction.
  const handleToggleGroup = (groupIds: string[], allSelected: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (allSelected) groupIds.forEach((g) => next.delete(g))
      else groupIds.forEach((g) => next.add(g))
      return next
    })
  }

  // Whole-list toggle for the "Select all" affordance. Operates on the
  // filtered view so users don't end up with hidden selected items.
  const allVisibleSelected =
    flatVisibleIds.length > 0 &&
    flatVisibleIds.every((id) => selectedIds.has(id))
  const toggleAllVisible = () => {
    if (allVisibleSelected) {
      // Remove only the visible ones — preserves any selections that may
      // exist outside the current filter (defensive; in practice we prune
      // those via the useEffect below).
      setSelectedIds((prev) => {
        const next = new Set(prev)
        flatVisibleIds.forEach((id) => next.delete(id))
        return next
      })
    } else {
      setSelectedIds((prev) => new Set([...prev, ...flatVisibleIds]))
    }
  }

  const clearSelected = () => {
    setSelectedIds(new Set())
    setLastSelectedId(null)
  }
  // Drop any selected ids that fall out of the visible filtered set so
  // the action-bar count never misrepresents what "Locate" will act on.
  useEffect(() => {
    if (selectedIds.size === 0) return
    const visible = new Set(filtered.map((r) => r.neighborId))
    let changed = false
    const next = new Set<string>()
    for (const id of selectedIds) {
      if (visible.has(id)) next.add(id)
      else changed = true
    }
    if (changed) setSelectedIds(next)
  }, [filtered, selectedIds])

  const handleLocateMany = async () => {
    if (selectedIds.size === 0 || !onLocateMany || locateBusy) return
    setLocateBusy(true)
    try {
      await onLocateMany([...selectedIds])
    } finally {
      setLocateBusy(false)
    }
  }

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
      {/* Search + sort row — paired so users can re-order while keeping
          search context. Sort menu is hidden when there's only one row. */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
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
        {records.length > 1 && (
          <SortMenu value={sortMode} onChange={setSortMode} />
        )}
        {onLocateMany && flatVisibleIds.length > 0 && (
          <button
            type="button"
            onClick={toggleAllVisible}
            aria-pressed={allVisibleSelected}
            // Explicit aria-label so this button's accessible name doesn't
            // collide with the per-group "Select all <Type>" checkbox.
            aria-label={
              allVisibleSelected
                ? 'Deselect all visible neighbors'
                : `Select all ${flatVisibleIds.length} visible neighbors`
            }
            className={cn(
              'inline-flex items-center gap-1 px-2 py-2 rounded-lg text-[11px] font-medium border transition-colors duration-150 whitespace-nowrap',
              allVisibleSelected
                ? 'text-accent-lineage bg-accent-lineage/10 border-accent-lineage/30'
                : 'text-ink-muted bg-white/[0.04] border-white/10 hover:text-ink hover:border-white/20',
            )}
          >
            {allVisibleSelected ? (
              <LucideIcons.CheckSquare className="w-3.5 h-3.5" />
            ) : (
              <LucideIcons.Square className="w-3.5 h-3.5" />
            )}
            <span className="hidden sm:inline">
              {allVisibleSelected ? 'Deselect all' : `Select all (${flatVisibleIds.length})`}
            </span>
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
              selectionEnabled={!!onLocateMany}
              selectionActive={selectedIds.size > 0}
              selectedIds={selectedIds}
              onToggleRow={handleRowSelectClick}
              onToggleGroup={handleToggleGroup}
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

      {/* Multi-select action bar — only renders when at least one row is
          checked. Slides in from the bottom so it's evident but doesn't
          steal vertical space when unused. */}
      <AnimatePresence>
        {onLocateMany && selectedIds.size > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.15 }}
            className="flex items-center justify-between gap-2 px-3 py-2 rounded-xl bg-accent-lineage/10 border border-accent-lineage/30"
          >
            <span className="text-[12px] font-medium text-ink">
              {selectedIds.size} selected
            </span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={clearSelected}
                className="px-2.5 py-1 rounded-md text-[11px] font-medium text-ink-muted hover:text-ink hover:bg-white/[0.06] transition-colors duration-150"
              >
                Clear
              </button>
              <button
                type="button"
                onClick={handleLocateMany}
                disabled={locateBusy}
                className={cn(
                  'px-3 py-1 rounded-md text-[11px] font-semibold bg-accent-lineage text-white shadow-sm hover:brightness-110 transition-all duration-150 flex items-center gap-1.5',
                  locateBusy && 'opacity-70 cursor-progress',
                )}
              >
                {locateBusy ? (
                  <LucideIcons.Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <LucideIcons.Crosshair className="w-3 h-3" />
                )}
                Locate {selectedIds.size} on canvas
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ============================================
// Sort menu
// ============================================

function SortMenu({
  value,
  onChange,
}: {
  value: SortMode
  onChange: (mode: SortMode) => void
}) {
  const [open, setOpen] = useState(false)
  // Close on outside click. Simpler than wiring a Radix Popover for the
  // three options this menu currently exposes.
  useEffect(() => {
    if (!open) return
    const onDocClick = () => setOpen(false)
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  const labels: Record<SortMode, string> = {
    'default': 'Default',
    'name-asc': 'Name A → Z',
    'name-desc': 'Name Z → A',
  }
  const ariaLabel = `Sort: ${labels[value]}`

  return (
    <div className="relative">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          setOpen((o) => !o)
        }}
        aria-label={ariaLabel}
        title={ariaLabel}
        className={cn(
          'inline-flex items-center gap-1 px-2 py-2 rounded-lg text-[11px] font-medium border transition-colors duration-150',
          value === 'default'
            ? 'text-ink-muted bg-white/[0.04] border-white/10 hover:text-ink hover:border-white/20'
            : 'text-accent-lineage bg-accent-lineage/10 border-accent-lineage/30',
        )}
      >
        <LucideIcons.ArrowUpDown className="w-3.5 h-3.5" />
        {value !== 'default' && (
          <span className="hidden sm:inline">{labels[value]}</span>
        )}
      </button>
      {open && (
        <div
          className="absolute right-0 top-full mt-1 z-20 min-w-[140px] rounded-lg border border-white/10 bg-canvas-elevated/98 backdrop-blur-2xl shadow-lg overflow-hidden"
          onMouseDown={(e) => e.stopPropagation()}
        >
          {(Object.keys(labels) as SortMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => {
                onChange(mode)
                setOpen(false)
              }}
              className={cn(
                'w-full flex items-center gap-2 px-3 py-1.5 text-[11px] text-left transition-colors duration-150',
                value === mode
                  ? 'bg-accent-lineage/15 text-accent-lineage'
                  : 'text-ink-muted hover:bg-white/[0.06] hover:text-ink',
              )}
            >
              {value === mode ? (
                <LucideIcons.Check className="w-3 h-3" />
              ) : (
                <span className="w-3" />
              )}
              {labels[mode]}
            </button>
          ))}
        </div>
      )}
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
  selectionEnabled: boolean
  /** True when any row across the whole panel is selected. Drives the
   *  hover-vs-always-on behaviour of the per-row checkbox. */
  selectionActive: boolean
  selectedIds: Set<string>
  onToggleRow: (id: string, shiftKey: boolean) => void
  onToggleGroup: (groupIds: string[], allSelected: boolean) => void
}

function EntityTypeGroup({
  type,
  rows,
  direction,
  collapsed,
  onToggleCollapse,
  onNeighborClick,
  selectionEnabled,
  selectionActive,
  selectedIds,
  onToggleRow,
  onToggleGroup,
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

  // Group selection: tri-state derived from the rows' selection status.
  // "Partial" means some-but-not-all are selected — shown as a dash icon
  // to mirror standard tri-state checkbox UX (macOS Finder, Gmail).
  const groupIds = useMemo(() => rows.map((r) => r.neighborId), [rows])
  const allGroupSelected = groupIds.every((id) => selectedIds.has(id))
  const someGroupSelected =
    !allGroupSelected && groupIds.some((id) => selectedIds.has(id))

  return (
    <div className="rounded-xl bg-white/[0.02] border border-white/[0.06] overflow-hidden">
      <div className="group/header flex items-center gap-2 px-3 py-2 hover:bg-white/[0.04] transition-colors duration-150">
        {selectionEnabled && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onToggleGroup(groupIds, allGroupSelected)
            }}
            aria-label={
              allGroupSelected
                ? `Deselect all ${displayName}`
                : `Select all ${displayName}`
            }
            aria-pressed={allGroupSelected}
            className={cn(
              'w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 transition-all duration-150',
              allGroupSelected
                ? 'bg-accent-lineage border-accent-lineage text-white'
                : someGroupSelected
                  ? 'bg-accent-lineage/30 border-accent-lineage/60 text-accent-lineage'
                  : 'border-white/20 hover:border-accent-lineage/60',
              // Mirror the row-checkbox hover-reveal logic so the group
              // checkbox doesn't feel like a separate affordance.
              !allGroupSelected && !someGroupSelected && !selectionActive
                ? 'opacity-0 group-hover/header:opacity-100'
                : 'opacity-100',
            )}
          >
            {allGroupSelected ? (
              <LucideIcons.Check className="w-2.5 h-2.5" />
            ) : someGroupSelected ? (
              <LucideIcons.Minus className="w-2.5 h-2.5" />
            ) : null}
          </button>
        )}
        <button
          type="button"
          onClick={onToggleCollapse}
          className="flex items-center gap-2 flex-1 min-w-0 text-left"
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
            className="text-[11px] font-semibold uppercase tracking-wide truncate"
            style={{ color }}
          >
            {displayName}
          </span>
          <span className="ml-auto inline-flex items-center justify-center min-w-[20px] h-[18px] px-1.5 rounded-full bg-white/[0.06] text-[10px] font-medium text-ink-muted tabular-nums">
            {rows.length}
          </span>
        </button>
      </div>
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
                  selectionEnabled={selectionEnabled}
                  selectionActive={selectionActive}
                  selected={selectedIds.has(r.neighborId)}
                  onToggleSelected={(shiftKey) =>
                    onToggleRow(r.neighborId, shiftKey)
                  }
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
  selectionEnabled,
  selectionActive,
  selected,
  onToggleSelected,
}: {
  record: NeighborRecord
  direction: Direction
  onClick: () => void | Promise<void>
  selectionEnabled: boolean
  /** True when any row in the panel is currently selected. Keeps the
   *  per-row checkbox visible so the user can extend the selection
   *  without hunting for the hover-reveal target. */
  selectionActive: boolean
  selected: boolean
  /** Receives the shift modifier so the parent can implement
   *  shift-click range-select semantics. */
  onToggleSelected: (shiftKey: boolean) => void
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
    <div
      className={cn(
        'group relative w-full flex items-center gap-2.5 px-3 py-2 transition-colors duration-150',
        selected
          ? 'bg-accent-lineage/10 hover:bg-accent-lineage/15'
          : 'hover:bg-white/[0.05]',
        busy && 'cursor-progress',
      )}
    >
      {/* Multi-select checkbox — visibility follows the hover-reveal
          contract:
          - Selected rows: always visible (filled).
          - Any-row-selected anywhere in panel: always visible (hollow).
          - Idle: hidden, fades in on row hover.
          stopPropagation keeps checkbox clicks from also firing the
          row's reveal navigation. */}
      {selectionEnabled && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            onToggleSelected(e.shiftKey)
          }}
          aria-label={selected ? 'Deselect neighbor' : 'Select neighbor'}
          aria-pressed={selected}
          className={cn(
            'w-4 h-4 rounded border flex items-center justify-center flex-shrink-0',
            'transition-opacity duration-150',
            selected
              ? 'bg-accent-lineage border-accent-lineage text-white opacity-100'
              : selectionActive
                ? 'border-white/20 hover:border-accent-lineage/60 opacity-100'
                : 'border-white/20 hover:border-accent-lineage/60 opacity-0 group-hover:opacity-100',
          )}
        >
          {selected && <LucideIcons.Check className="w-2.5 h-2.5" />}
        </button>
      )}
      <button
        type="button"
        onClick={handleClick}
        disabled={busy}
        className="flex-1 flex items-center gap-2.5 min-w-0 text-left"
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
    </div>
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
