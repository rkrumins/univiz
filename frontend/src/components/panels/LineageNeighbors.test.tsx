/**
 * LineageNeighbors — RTL tests for the entity-drawer lineage section.
 *
 * Covers the contract the component is responsible for:
 *   • Counts reflect lineage edges only (containment filtered out)
 *   • Self-loops never produce a neighbor entry
 *   • `visibleEdges` (canvas-projected set) wins over raw `edges` when present
 *   • Summary cards show the right count and disable when empty
 *   • Click-to-expand reveals the detail panel; only one side at a time
 *   • Search and entity-type chips narrow the list
 *   • Clicking a neighbor swaps the drawer (store) and fires onFocusNode
 *
 * Framer-motion is mocked because its mount-time `height: 0` would hide
 * the expanded panel from queries; the tests care about content and
 * structure, not animation timing.
 */

import React from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { LineageNeighbors } from './LineageNeighbors'
import { useCanvasStore, type LineageEdge, type LineageNode } from '@/store/canvas'
import { useSchemaStore } from '@/store/schema'
import type { WorkspaceSchema } from '@/types/schema'

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

// Framer-motion: render plain elements + render children synchronously so
// AnimatePresence-wrapped detail panels appear in the DOM immediately.
// CRITICAL: cache the passthrough component per tag. A naive
// `new Proxy({}, { get: () => passthrough(tag) })` returns a fresh
// `forwardRef` instance on every access — React treats each render as a
// new component type, unmounting and remounting children. That destroys
// local state inside any motion-wrapped descendant (e.g. the
// `lastSelectedId` anchor inside ExpandedDetail).
vi.mock('framer-motion', () => {
  const cache = new Map<string, React.ComponentType<unknown>>()
  const passthrough = (tag: string) => {
    let cmp = cache.get(tag)
    if (!cmp) {
      cmp = React.forwardRef<HTMLElement, React.HTMLAttributes<HTMLElement>>(
        function MotionStub(props, ref) {
          return React.createElement(tag, { ...props, ref })
        },
      ) as unknown as React.ComponentType<unknown>
      cache.set(tag, cmp)
    }
    return cmp
  }
  return {
    motion: new Proxy(
      {},
      {
        get: (_target, key: string) => passthrough(key),
      },
    ),
    AnimatePresence: ({ children }: { children: React.ReactNode }) => (
      <>{children}</>
    ),
  }
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FOCAL = 'urn:demo:table:focal'
const UPSTREAM_A = 'urn:demo:table:upstream_a'
const UPSTREAM_B = 'urn:demo:column:upstream_b'
const DOWNSTREAM_A = 'urn:demo:table:downstream_a'
const PARENT = 'urn:demo:domain:parent'

function makeNode(
  id: string,
  type: string,
  label: string,
  extra: Partial<LineageNode['data']> = {},
): LineageNode {
  return {
    id,
    position: { x: 0, y: 0 },
    data: { label, urn: id, type, ...extra },
  } as LineageNode
}

function makeEdge(
  id: string,
  source: string,
  target: string,
  edgeType: string,
): LineageEdge {
  return {
    id,
    source,
    target,
    data: { edgeType },
  } as LineageEdge
}

const baseNodes: LineageNode[] = [
  makeNode(FOCAL, 'table', 'Focal Table', {
    businessLabel: 'Customer Orders',
  }),
  makeNode(UPSTREAM_A, 'table', 'Upstream Source'),
  makeNode(UPSTREAM_B, 'column', 'order_id'),
  makeNode(DOWNSTREAM_A, 'table', 'Downstream Report'),
  makeNode(PARENT, 'domain', 'Sales Domain'),
]

const minimalSchema: WorkspaceSchema = {
  id: 'test',
  name: 'Test Schema',
  version: '1',
  entityTypes: [
    {
      id: 'table',
      name: 'Table',
      pluralName: 'Tables',
      visual: {
        icon: 'Table',
        color: '#3b82f6',
        shape: 'rounded',
        size: 'md',
        borderStyle: 'solid',
        showInMinimap: true,
      },
      fields: [],
      hierarchy: {
        level: 1,
        canContain: [],
        canBeContainedBy: [],
        defaultExpanded: false,
        rollUpFields: [],
      },
      behavior: { selectable: true } as any,
    },
    {
      id: 'column',
      name: 'Column',
      pluralName: 'Columns',
      visual: {
        icon: 'Columns',
        color: '#a855f7',
        shape: 'rounded',
        size: 'sm',
        borderStyle: 'solid',
        showInMinimap: false,
      },
      fields: [],
      hierarchy: {
        level: 2,
        canContain: [],
        canBeContainedBy: [],
        defaultExpanded: false,
        rollUpFields: [],
      },
      behavior: { selectable: true } as any,
    },
    {
      id: 'domain',
      name: 'Domain',
      pluralName: 'Domains',
      visual: {
        icon: 'Globe',
        color: '#f59e0b',
        shape: 'rounded',
        size: 'lg',
        borderStyle: 'solid',
        showInMinimap: true,
      },
      fields: [],
      hierarchy: {
        level: 0,
        canContain: [],
        canBeContainedBy: [],
        defaultExpanded: false,
        rollUpFields: [],
      },
      behavior: { selectable: true } as any,
    },
  ],
  relationshipTypes: [],
  views: [],
  defaultViewId: 'default',
  globalVisuals: {} as any,
  containmentEdgeTypes: ['CONTAINS'],
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

function seedCanvas(
  edges: LineageEdge[],
  opts: { nodes?: LineageNode[]; visibleEdges?: LineageEdge[] } = {},
) {
  useCanvasStore.setState({
    nodes: opts.nodes ?? baseNodes,
    edges,
    visibleEdges: opts.visibleEdges ?? [],
    drawerNodeId: null,
  })
}

beforeEach(() => {
  // Cold-start both stores so each test sees a deterministic state.
  useCanvasStore.setState({
    nodes: [],
    edges: [],
    visibleEdges: [],
    drawerNodeId: null,
  })
  useSchemaStore.setState({ schema: minimalSchema })
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('LineageNeighbors — counts', () => {
  it('shows 0 for both directions when no edges touch the node', () => {
    seedCanvas([])
    render(<LineageNeighbors nodeId={FOCAL} />)

    expect(screen.getByText('Data Sources')).toBeInTheDocument()
    expect(screen.getByText('Data Consumers')).toBeInTheDocument()

    const sourcesCard = screen.getByText('Data Sources').closest('button')
    const consumersCard = screen.getByText('Data Consumers').closest('button')
    expect(within(sourcesCard!).getByText('0')).toBeInTheDocument()
    expect(within(consumersCard!).getByText('0')).toBeInTheDocument()
    // Empty cards must be inert — no chevron, button disabled.
    expect(sourcesCard).toBeDisabled()
    expect(consumersCard).toBeDisabled()
  })

  it('counts incoming vs outgoing edges separately', () => {
    seedCanvas([
      makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e2', UPSTREAM_B, FOCAL, 'REFERENCES'),
      makeEdge('e3', FOCAL, DOWNSTREAM_A, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)

    const sourcesCard = screen.getByText('Data Sources').closest('button')!
    const consumersCard = screen.getByText('Data Consumers').closest('button')!
    expect(within(sourcesCard).getByText('2')).toBeInTheDocument()
    expect(within(consumersCard).getByText('1')).toBeInTheDocument()
    expect(screen.getByText(/3 connections/)).toBeInTheDocument()
  })

  it('excludes containment edges from the lineage counts', () => {
    seedCanvas([
      makeEdge('e-contains', PARENT, FOCAL, 'CONTAINS'),
      makeEdge('e-lineage', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)

    // Only the FLOWS_TO edge is counted — CONTAINS is structural and
    // belongs in the schema's containmentEdgeTypes list.
    const sourcesCard = screen.getByText('Data Sources').closest('button')!
    expect(within(sourcesCard).getByText('1')).toBeInTheDocument()
  })

  it('skips self-loops', () => {
    seedCanvas([
      makeEdge('e-loop', FOCAL, FOCAL, 'FLOWS_TO'),
      makeEdge('e-real', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)

    const sourcesCard = screen.getByText('Data Sources').closest('button')!
    const consumersCard = screen.getByText('Data Consumers').closest('button')!
    expect(within(sourcesCard).getByText('1')).toBeInTheDocument()
    expect(within(consumersCard).getByText('0')).toBeInTheDocument()
  })

  it('prefers visibleEdges (canvas-projected set) over raw edges', () => {
    // Raw edges go between leaves; visible edges are the rolled-up parents.
    // The section must mirror what the canvas is actually rendering.
    seedCanvas(
      [makeEdge('raw', UPSTREAM_A, FOCAL, 'FLOWS_TO')], // raw — ignored
      {
        visibleEdges: [
          makeEdge('vis-1', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
          makeEdge('vis-2', UPSTREAM_B, FOCAL, 'FLOWS_TO'),
        ],
      },
    )
    render(<LineageNeighbors nodeId={FOCAL} />)

    const sourcesCard = screen.getByText('Data Sources').closest('button')!
    expect(within(sourcesCard).getByText('2')).toBeInTheDocument()
  })
})

describe('LineageNeighbors — expansion and detail', () => {
  it('clicking a summary card reveals the neighbor list', async () => {
    const user = userEvent.setup()
    seedCanvas([
      makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e2', UPSTREAM_B, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)

    // Detail panel hidden initially.
    expect(screen.queryByPlaceholderText(/search by name/i)).not.toBeInTheDocument()

    await user.click(screen.getByText('Data Sources'))

    // After expand: search input and neighbor labels are visible.
    expect(screen.getByPlaceholderText(/search by name/i)).toBeInTheDocument()
    expect(screen.getByText('Upstream Source')).toBeInTheDocument()
    expect(screen.getByText('order_id')).toBeInTheDocument()
  })

  it('expanding one direction collapses the other', async () => {
    const user = userEvent.setup()
    seedCanvas([
      makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e2', FOCAL, DOWNSTREAM_A, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)

    await user.click(screen.getByText('Data Sources'))
    expect(screen.getByText('Upstream Source')).toBeInTheDocument()

    await user.click(screen.getByText('Data Consumers'))
    // Sources detail gone, consumers detail now visible.
    expect(screen.queryByText('Upstream Source')).not.toBeInTheDocument()
    expect(screen.getByText('Downstream Report')).toBeInTheDocument()
  })

  it('disabled summary card does not expand on click', async () => {
    const user = userEvent.setup()
    seedCanvas([makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO')])
    render(<LineageNeighbors nodeId={FOCAL} />)

    // Consumers count is 0 — card is disabled.
    await user.click(screen.getByText('Data Consumers'))
    expect(
      screen.queryByPlaceholderText(/search by name/i),
    ).not.toBeInTheDocument()
  })
})

describe('LineageNeighbors — filtering', () => {
  it('filters the neighbor list by search text', async () => {
    const user = userEvent.setup()
    seedCanvas([
      makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e2', UPSTREAM_B, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)
    await user.click(screen.getByText('Data Sources'))

    expect(screen.getByText('Upstream Source')).toBeInTheDocument()
    expect(screen.getByText('order_id')).toBeInTheDocument()

    await user.type(screen.getByPlaceholderText(/search by name/i), 'order')

    expect(screen.queryByText('Upstream Source')).not.toBeInTheDocument()
    expect(screen.getByText('order_id')).toBeInTheDocument()
  })

  it('toggles an entity-type filter chip to narrow the list', async () => {
    const user = userEvent.setup()
    seedCanvas([
      makeEdge('e-table', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e-column', UPSTREAM_B, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)
    await user.click(screen.getByText('Data Sources'))

    // Two facets — Table (1) and Column (1). Chip labels include the
    // ontology's pluralName/name; click the "Column" chip.
    const columnChip = screen
      .getAllByRole('button')
      .find((btn) => /^Column\s*1$/.test(btn.textContent ?? ''))
    expect(columnChip).toBeTruthy()
    await user.click(columnChip!)

    expect(screen.getByText(/1 filter active/i)).toBeInTheDocument()
    expect(screen.queryByText('Upstream Source')).not.toBeInTheDocument()
    expect(screen.getByText('order_id')).toBeInTheDocument()
  })

  it('Clear all resets active filters', async () => {
    const user = userEvent.setup()
    seedCanvas([
      makeEdge('e-table', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e-column', UPSTREAM_B, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} />)
    await user.click(screen.getByText('Data Sources'))

    const columnChip = screen
      .getAllByRole('button')
      .find((btn) => /^Column\s*1$/.test(btn.textContent ?? ''))
    await user.click(columnChip!)
    expect(screen.getByText(/1 filter active/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /clear all/i }))
    expect(screen.queryByText(/filter active/i)).not.toBeInTheDocument()
    expect(screen.getByText('Upstream Source')).toBeInTheDocument()
    expect(screen.getByText('order_id')).toBeInTheDocument()
  })
})

describe('LineageNeighbors — neighbor click', () => {
  it('opens the drawer for the clicked neighbor and fires onFocusNode', async () => {
    const user = userEvent.setup()
    const onFocusNode = vi.fn()
    seedCanvas([makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO')])
    render(<LineageNeighbors nodeId={FOCAL} onFocusNode={onFocusNode} />)
    await user.click(screen.getByText('Data Sources'))

    await user.click(screen.getByText('Upstream Source'))

    expect(useCanvasStore.getState().drawerNodeId).toBe(UPSTREAM_A)
    expect(onFocusNode).toHaveBeenCalledTimes(1)
    expect(onFocusNode).toHaveBeenCalledWith(UPSTREAM_A)
  })

  it('works without an onFocusNode prop (drawer-swap still fires)', async () => {
    const user = userEvent.setup()
    seedCanvas([makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO')])
    render(<LineageNeighbors nodeId={FOCAL} />)
    await user.click(screen.getByText('Data Sources'))

    await user.click(screen.getByText('Upstream Source'))
    expect(useCanvasStore.getState().drawerNodeId).toBe(UPSTREAM_A)
  })

  it('shows a spinner on the clicked row while onFocusNode is pending', async () => {
    const user = userEvent.setup()
    let resolveReveal: () => void = () => {}
    const onFocusNode = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveReveal = resolve
        }),
    )
    seedCanvas([makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO')])
    render(<LineageNeighbors nodeId={FOCAL} onFocusNode={onFocusNode} />)
    await user.click(screen.getByText('Data Sources'))

    await user.click(screen.getByText('Upstream Source'))

    // Spinner visible while the promise is pending.
    expect(screen.getByTestId('reveal-spinner')).toBeInTheDocument()

    resolveReveal()
    await waitFor(() =>
      expect(screen.queryByTestId('reveal-spinner')).not.toBeInTheDocument(),
    )
  })
})

describe('LineageNeighbors — multi-select', () => {
  it('hides row checkboxes by default when nothing is selected (hover-reveal)', async () => {
    const user = userEvent.setup()
    seedCanvas([makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO')])
    render(<LineageNeighbors nodeId={FOCAL} onLocateMany={vi.fn()} />)
    await user.click(screen.getByText('Data Sources'))

    // Checkbox is rendered (so it can fade in on hover) but starts at
    // opacity-0 — the hover-reveal contract.
    const checkbox = screen.getByRole('button', { name: /select neighbor/i })
    expect(checkbox).toHaveClass('opacity-0')
    expect(checkbox).toHaveClass('group-hover:opacity-100')
  })

  it('Select all toggles every visible neighbor, then deselects on a second click', async () => {
    const user = userEvent.setup()
    seedCanvas([
      makeEdge('e1', UPSTREAM_A, FOCAL, 'FLOWS_TO'),
      makeEdge('e2', UPSTREAM_B, FOCAL, 'FLOWS_TO'),
    ])
    render(<LineageNeighbors nodeId={FOCAL} onLocateMany={vi.fn()} />)
    await user.click(screen.getByText('Data Sources'))

    // Target the panel-wide select-all button via its specific aria-label
    // ("…visible neighbors") so we don't collide with per-group checkboxes
    // whose accessible name is "Select all <Type>".
    const selectAllBtn = screen.getByRole('button', {
      name: /select all .* visible neighbors/i,
    })
    await user.click(selectAllBtn)

    // Action bar appears with the count.
    expect(screen.getByText('2 selected')).toBeInTheDocument()

    // Button flipped to "Deselect all visible neighbors".
    const deselectAllBtn = screen.getByRole('button', {
      name: /deselect all visible neighbors/i,
    })
    await user.click(deselectAllBtn)
    expect(screen.queryByText('2 selected')).not.toBeInTheDocument()
  })

  it('shift-click on a second row selects the range between them', async () => {
    const user = userEvent.setup()
    // Three neighbors in the same group (all "table") so they sit
    // contiguously in visible order.
    seedCanvas([
      makeEdge('e1', 'urn:demo:table:a', FOCAL, 'FLOWS_TO'),
      makeEdge('e2', 'urn:demo:table:b', FOCAL, 'FLOWS_TO'),
      makeEdge('e3', 'urn:demo:table:c', FOCAL, 'FLOWS_TO'),
    ], {
      nodes: [
        ...baseNodes,
        makeNode('urn:demo:table:a', 'table', 'Table A'),
        makeNode('urn:demo:table:b', 'table', 'Table B'),
        makeNode('urn:demo:table:c', 'table', 'Table C'),
      ],
    })
    render(<LineageNeighbors nodeId={FOCAL} onLocateMany={vi.fn()} />)
    await user.click(screen.getByText('Data Sources'))

    // Click first checkbox normally.
    const firstClick = screen.getAllByRole('button', { name: /select neighbor/i })
    expect(firstClick).toHaveLength(3)
    await user.click(firstClick[0])
    // Confirm the anchor click landed.
    await waitFor(() =>
      expect(screen.getByText('1 selected')).toBeInTheDocument(),
    )

    // Re-query after the click (the action bar mounted + React re-rendered
    // row checkboxes). user-event v14 holds the shift modifier via the
    // `{Shift>}` syntax; the next click() inherits the modifier and the
    // resulting React synthetic event sees `shiftKey: true`.
    const afterFirst = screen.getAllByRole(
      'button',
      { name: /select neighbor|deselect neighbor/i },
    )
    // Dispatch a real MouseEvent with shiftKey set — fireEvent's options
    // shorthand goes through jsdom's Event constructor which doesn't
    // carry modifier flags reliably. The explicit `new MouseEvent` with
    // shiftKey in the init dictionary propagates correctly to React's
    // SyntheticEvent.
    afterFirst[2].dispatchEvent(
      new MouseEvent('click', {
        bubbles: true,
        cancelable: true,
        shiftKey: true,
      }),
    )

    // All three rows should be selected (range A→C inclusive).
    await waitFor(() =>
      expect(screen.getByText('3 selected')).toBeInTheDocument(),
    )
  })
})

