import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./apiClient', () => ({ authFetch: vi.fn() }))

import { authFetch } from './apiClient'
import { useGraphEditorStore } from '../store/graphEditorStore'
import type { StagedChange, StagedChangeType } from '../store/stagedChangesStore'
import {
  defaultExtract,
  mirrorToEditorStore,
  stagedChangeToInput,
  stagedChangesToInputs,
  syncStagedChangesToVersionControl,
} from './versionControlBridge'

const mockFetch = authFetch as unknown as ReturnType<typeof vi.fn>

function sc(
  type: StagedChangeType,
  over: Partial<StagedChange> = {},
): StagedChange {
  return {
    id: `c_${type}`,
    type,
    targetId: 'urn:a',
    summary: `${type} summary`,
    after: {},
    timestamp: 1,
    ...over,
  }
}

describe('versionControlBridge mapping', () => {
  it('maps create_entity -> add_node with extracted content', () => {
    const out = stagedChangeToInput(
      sc('create_entity', {
        targetId: 'staged-1',
        after: {
          urn: 'urn:new',
          entityType: 'Table',
          displayName: 'Orders',
          position: { x: 1, y: 2 },
          properties: { owner: 'fin' },
          tags: ['pii'],
        },
      }),
    )!
    expect(out.change_type).toBe('add_node')
    expect(out.object_kind).toBe('node')
    expect(out.object_id).toBe('staged-1')
    expect(out.payload).toMatchObject({
      key: 'staged-1',
      entity_type: 'Table',
      display_name: 'Orders',
      position: { x: 1, y: 2 },
      properties: { owner: 'fin' },
      tags: ['pii'],
    })
  })

  it('maps rename_entity -> update_node', () => {
    const out = stagedChangeToInput(
      sc('rename_entity', { after: { displayName: 'New' } }),
    )!
    expect(out.change_type).toBe('update_node')
    expect(out.payload.display_name).toBe('New')
  })

  it('maps delete_entity -> delete_node with only the key', () => {
    const out = stagedChangeToInput(sc('delete_entity', { targetId: 'urn:x' }))!
    expect(out.change_type).toBe('delete_node')
    expect(out.payload).toEqual({ key: 'urn:x' })
  })

  it('maps layer ops -> update_node', () => {
    expect(stagedChangeToInput(sc('assign_layer'))!.change_type).toBe('update_node')
    expect(stagedChangeToInput(sc('move_to_layer'))!.change_type).toBe('update_node')
  })

  it('maps edge ops incl. reverse_edge -> update_edge', () => {
    const c = stagedChangeToInput(
      sc('create_edge', {
        targetId: 'e1',
        after: { sourceUrn: 'urn:a', targetUrn: 'urn:b', edgeType: 'flows_to' },
      }),
    )!
    expect(c).toMatchObject({
      change_type: 'add_edge',
      object_kind: 'edge',
      object_id: 'e1',
    })
    expect(c.payload).toMatchObject({
      source_key: 'urn:a',
      target_key: 'urn:b',
      edge_type: 'flows_to',
    })
    expect(stagedChangeToInput(sc('reverse_edge', { targetId: 'e1' }))!.change_type).toBe(
      'update_edge',
    )
    expect(stagedChangeToInput(sc('delete_edge', { targetId: 'e1' }))!.payload).toEqual({
      key: 'e1',
    })
  })

  it('prefers targetUrn as the stable key when present', () => {
    const out = stagedChangeToInput(
      sc('rename_entity', { targetId: 'staged-9', targetUrn: 'urn:real' }),
    )!
    expect(out.object_id).toBe('urn:real')
  })

  it('drops unmappable changes in batch translation', () => {
    const inputs = stagedChangesToInputs([
      sc('create_entity', { after: { urn: 'urn:a' } }),
      { ...sc('create_entity'), type: 'totally_unknown' as StagedChangeType },
    ])
    expect(inputs).toHaveLength(1)
  })

  it('custom extractor overrides defaults', () => {
    const out = stagedChangeToInput(sc('create_entity'), {
      extract: () => ({ key: 'k', display_name: 'CUSTOM' }),
    })!
    expect(out.payload.display_name).toBe('CUSTOM')
  })

  it('defaultExtract reads edge endpoints from multiple shapes', () => {
    const e = defaultExtract(
      sc('create_edge', { after: { from: 's', to: 't', relationship: 'r' } }),
      'edge',
    )
    expect(e).toMatchObject({ source_key: 's', target_key: 't', edge_type: 'r' })
  })
})

describe('mirrorToEditorStore', () => {
  beforeEach(() => useGraphEditorStore.getState().reset())

  it('reflects Context View changes into the editor store', () => {
    mirrorToEditorStore([
      sc('create_entity', { targetId: 'urn:a', after: { urn: 'urn:a' } }),
      sc('delete_edge', { targetId: 'e1' }),
    ])
    const ops = useGraphEditorStore.getState().ops
    expect(ops.map((o) => o.changeType).sort()).toEqual([
      'add_node',
      'delete_edge',
    ])
  })

  it('rebuilds from scratch so discards propagate', () => {
    mirrorToEditorStore([sc('create_entity', { targetId: 'urn:a' })])
    expect(useGraphEditorStore.getState().ops).toHaveLength(1)
    mirrorToEditorStore([]) // user discarded everything in Context View
    expect(useGraphEditorStore.getState().ops).toHaveLength(0)
  })
})

describe('syncStagedChangesToVersionControl', () => {
  beforeEach(() => mockFetch.mockReset())

  it('stages mapped changes to the backend and returns the count', async () => {
    mockFetch.mockResolvedValueOnce({ ws_change_version: 1 })
    const n = await syncStagedChangesToVersionControl(
      { wsId: 'ws', graphId: 'g_1', branch: 'main' },
      [
        sc('create_entity', { targetId: 'urn:a', after: { urn: 'urn:a' } }),
        sc('rename_entity', { targetId: 'urn:a', after: { displayName: 'X' } }),
      ],
    )
    expect(n).toBe(2)
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('/api/v1/ws/graphs/g_1/branches/main/stage')
    const body = JSON.parse(init.body)
    expect(body.changes).toHaveLength(2)
    expect(body.changes[0].change_type).toBe('add_node')
  })

  it('no-ops (no network) when nothing maps', async () => {
    const n = await syncStagedChangesToVersionControl(
      { wsId: 'ws', graphId: 'g', branch: 'main' },
      [{ ...sc('create_entity'), type: 'x' as StagedChangeType }],
    )
    expect(n).toBe(0)
    expect(mockFetch).not.toHaveBeenCalled()
  })

  it('propagates backend errors to the Save UI', async () => {
    mockFetch.mockRejectedValueOnce(new Error('boom'))
    await expect(
      syncStagedChangesToVersionControl(
        { wsId: 'ws', graphId: 'g', branch: 'main' },
        [sc('create_entity', { after: { urn: 'urn:a' } })],
      ),
    ).rejects.toThrow('boom')
  })
})
