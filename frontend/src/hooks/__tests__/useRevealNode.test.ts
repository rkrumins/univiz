/**
 * useRevealNode — orchestration tests for the jump-to-node cascade.
 *
 * Covers the four paths the hook is responsible for:
 *   • Fast path  — target already in store: no fetch, expand ancestors, focus
 *   • Deep path  — target missing: getAncestors + per-ancestor loadChildren,
 *                  then expand + focus
 *   • Failure    — getAncestors throws: caught + warned, focus is NOT called
 *                  because target stayed missing
 *   • Pulse      — pulseNode(id) is fired on the canvas store after focus
 *
 * We render the hook via React Testing Library's renderHook and inject fakes
 * for the canvas store, provider, parentMap, etc. The hook reads from
 * useCanvasStore directly for the in-store check + pulseNode dispatch, so
 * we seed the actual store rather than mocking it.
 */

import { renderHook, act } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// main.tsx eagerly calls ReactDOM.createRoot when imported, which fails in
// jsdom without a real root container. useGraphHydration → providers →
// workspaces store → workspaceSwitchCleanup → main, so importing
// useRevealNode (which uses toCanvasNode) drags in that chain. Stub
// `@/main` to keep the import tree dead-end for the test.
vi.mock('@/main', () => ({
  getQueryClient: () => ({}),
}))

import { useRevealNode } from '../useRevealNode'
import { useCanvasStore, type LineageNode } from '@/store/canvas'
import type { GraphDataProvider, GraphNode } from '@/providers/GraphDataProvider'

// ---------------------------------------------------------------------------
// rAF stub — the hook awaits two requestAnimationFrames after expand to let
// layout settle. Vitest's jsdom defaults are slow/flaky here, so we make rAF
// synchronous in the test runtime.
// ---------------------------------------------------------------------------
beforeEach(() => {
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(
    (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    },
  )
  // Reset store between tests.
  useCanvasStore.setState({
    nodes: [],
    edges: [],
    visibleEdges: [],
    pulseNodeIds: new Set(),
  })
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TARGET = 'urn:demo:column:target'
const PARENT = 'urn:demo:table:parent'
const GRANDPARENT = 'urn:demo:domain:gp'

function makeLineageNode(id: string): LineageNode {
  return {
    id,
    position: { x: 0, y: 0 },
    data: { label: id, urn: id, type: 'generic' },
  } as LineageNode
}

function makeGraphNode(urn: string): GraphNode {
  return {
    urn,
    entityType: 'generic',
    displayName: urn,
    properties: {},
  }
}

function makeProviderStub(opts?: {
  ancestors?: GraphNode[]
  ancestorsThrows?: boolean
}): GraphDataProvider {
  return {
    getAncestors: opts?.ancestorsThrows
      ? vi.fn().mockRejectedValue(new Error('500'))
      : vi.fn().mockResolvedValue(opts?.ancestors ?? []),
  } as unknown as GraphDataProvider
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useRevealNode — fast path (target already in store)', () => {
  it('skips getAncestors/loadChildren, expands collapsed ancestors, then focuses', async () => {
    // Seed: target + ancestors are all in the store; only PARENT is unexpanded.
    useCanvasStore.setState({
      nodes: [
        makeLineageNode(TARGET),
        makeLineageNode(PARENT),
        makeLineageNode(GRANDPARENT),
      ],
    })
    const parentMap = new Map<string, string>([
      [TARGET, PARENT],
      [PARENT, GRANDPARENT],
    ])
    const setExpandedNodes = vi.fn()
    const loadChildren = vi.fn().mockResolvedValue(undefined)
    const focus = vi.fn()
    const provider = makeProviderStub()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap,
        setExpandedNodes,
        loadChildren,
        focus,
        provider,
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(provider.getAncestors).not.toHaveBeenCalled()
    expect(loadChildren).not.toHaveBeenCalled()
    // Expanded ancestors should include both PARENT and GRANDPARENT.
    expect(setExpandedNodes).toHaveBeenCalledTimes(1)
    const updater = setExpandedNodes.mock.calls[0][0] as (
      prev: Set<string>,
    ) => Set<string>
    expect([...updater(new Set())].sort()).toEqual(
      [PARENT, GRANDPARENT].sort(),
    )
    expect(focus).toHaveBeenCalledWith(TARGET)
  })

  it('skips setExpandedNodes when chain is already fully expanded', async () => {
    useCanvasStore.setState({
      nodes: [makeLineageNode(TARGET), makeLineageNode(PARENT)],
    })
    const parentMap = new Map([[TARGET, PARENT]])
    const setExpandedNodes = vi.fn()
    const focus = vi.fn()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap,
        setExpandedNodes,
        loadChildren: vi.fn(),
        focus,
        provider: makeProviderStub(),
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    // setExpandedNodes is invoked once with an updater, but when the updater
    // sees the already-expanded set, it returns the same reference so React
    // skips the state update. We assert this by simulating the updater.
    expect(setExpandedNodes).toHaveBeenCalledTimes(1)
    const updater = setExpandedNodes.mock.calls[0][0] as (
      prev: Set<string>,
    ) => Set<string>
    const prev = new Set([PARENT])
    expect(updater(prev)).toBe(prev) // same ref → no-op
    expect(focus).toHaveBeenCalled()
  })

  it('top-level target with no ancestors panes straight to focus', async () => {
    useCanvasStore.setState({ nodes: [makeLineageNode(TARGET)] })
    const setExpandedNodes = vi.fn()
    const focus = vi.fn()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap: new Map(), // no parents
        setExpandedNodes,
        loadChildren: vi.fn(),
        focus,
        provider: makeProviderStub(),
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(setExpandedNodes).not.toHaveBeenCalled()
    expect(focus).toHaveBeenCalledWith(TARGET)
  })
})

describe('useRevealNode — deep path (target not in store)', () => {
  it('fetches ancestors, calls loadChildren per ancestor, then expands + focuses', async () => {
    // Target not in store initially; provider returns the chain.
    const provider = makeProviderStub({
      ancestors: [makeGraphNode(GRANDPARENT), makeGraphNode(PARENT)],
    })

    // loadChildren simulates the cascade: each call adds the next level to
    // the store. The deepest call adds the target itself.
    const loadChildren = vi.fn(async (parentId: string) => {
      if (parentId === GRANDPARENT) {
        useCanvasStore.setState((s) => ({
          nodes: [...s.nodes, makeLineageNode(PARENT)],
        }))
      } else if (parentId === PARENT) {
        useCanvasStore.setState((s) => ({
          nodes: [...s.nodes, makeLineageNode(TARGET)],
        }))
      }
    })

    const setExpandedNodes = vi.fn()
    const focus = vi.fn()
    const parentMap = new Map([
      [TARGET, PARENT],
      [PARENT, GRANDPARENT],
    ])

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap,
        setExpandedNodes,
        loadChildren,
        focus,
        provider,
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(provider.getAncestors).toHaveBeenCalledWith(TARGET)
    // loadChildren called once per ancestor (root → target's parent).
    expect(loadChildren).toHaveBeenCalledTimes(2)
    expect(loadChildren).toHaveBeenNthCalledWith(1, GRANDPARENT)
    expect(loadChildren).toHaveBeenNthCalledWith(2, PARENT)
    // Target now in store; ancestors expanded; focus fired.
    expect(setExpandedNodes).toHaveBeenCalled()
    expect(focus).toHaveBeenCalledWith(TARGET)
  })

  it('does not focus when target still missing after cascade', async () => {
    // Provider returns ancestors but loadChildren never actually loads the
    // target (e.g. backend says child set is empty).
    const provider = makeProviderStub({
      ancestors: [makeGraphNode(PARENT)],
    })
    const focus = vi.fn()
    const setExpandedNodes = vi.fn()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap: new Map(),
        setExpandedNodes,
        loadChildren: vi.fn().mockResolvedValue(undefined),
        focus,
        provider,
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(focus).not.toHaveBeenCalled()
    // Drawer-swap already happened upstream; the hook bails silently here.
    expect(setExpandedNodes).not.toHaveBeenCalled()
    warn.mockRestore()
  })
})

describe('useRevealNode — failure paths', () => {
  it('swallows getAncestors errors and does not focus', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const provider = makeProviderStub({ ancestorsThrows: true })
    const focus = vi.fn()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap: new Map(),
        setExpandedNodes: vi.fn(),
        loadChildren: vi.fn(),
        focus,
        provider,
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(provider.getAncestors).toHaveBeenCalledWith(TARGET)
    expect(focus).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('getAncestors failed'),
      TARGET,
      expect.any(Error),
    )
    warn.mockRestore()
  })

  it('continues the cascade when one loadChildren call fails', async () => {
    // getAncestors returns the chain. The first loadChildren fails but
    // the second succeeds (still places the target in the store).
    const provider = makeProviderStub({
      ancestors: [makeGraphNode(GRANDPARENT), makeGraphNode(PARENT)],
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const loadChildren = vi
      .fn()
      .mockRejectedValueOnce(new Error('boom'))
      .mockImplementationOnce(async () => {
        useCanvasStore.setState((s) => ({
          nodes: [...s.nodes, makeLineageNode(TARGET)],
        }))
      })
    const focus = vi.fn()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap: new Map(),
        setExpandedNodes: vi.fn(),
        loadChildren,
        focus,
        provider,
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(loadChildren).toHaveBeenCalledTimes(2)
    expect(focus).toHaveBeenCalledWith(TARGET)
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('loadChildren failed'),
      GRANDPARENT,
      expect.any(Error),
    )
    warn.mockRestore()
  })
})

describe('useRevealNode — pulse on arrival', () => {
  it('fires pulseNode on the canvas store after focus', async () => {
    useCanvasStore.setState({ nodes: [makeLineageNode(TARGET)] })
    const focus = vi.fn()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap: new Map(),
        setExpandedNodes: vi.fn(),
        loadChildren: vi.fn(),
        focus,
        provider: makeProviderStub(),
      }),
    )

    await act(async () => {
      await result.current(TARGET)
    })

    expect(focus).toHaveBeenCalled()
    expect(useCanvasStore.getState().pulseNodeIds.has(TARGET)).toBe(true)
  })

  it('skipFocus: true suppresses focus() but still pulses', async () => {
    // Batch flows (multi-select "Locate N on canvas") use this option so
    // a single fitView at the end replaces N competing per-node scrolls.
    // The pulse must still fire per-node so users can spot each target
    // after the trailing fitView settles.
    useCanvasStore.setState({ nodes: [makeLineageNode(TARGET)] })
    const focus = vi.fn()

    const { result } = renderHook(() =>
      useRevealNode({
        parentMap: new Map(),
        setExpandedNodes: vi.fn(),
        loadChildren: vi.fn(),
        focus,
        provider: makeProviderStub(),
      }),
    )

    await act(async () => {
      await result.current(TARGET, { skipFocus: true })
    })

    expect(focus).not.toHaveBeenCalled()
    expect(useCanvasStore.getState().pulseNodeIds.has(TARGET)).toBe(true)
  })
})
