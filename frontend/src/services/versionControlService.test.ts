import { beforeEach, describe, expect, it, vi } from 'vitest'

// Mock the shared fetch wrapper so we drive error/success bodies.
vi.mock('./apiClient', () => ({
  authFetch: vi.fn(),
}))

import { authFetch } from './apiClient'
import {
  EmptyCommitError,
  GraphValidationError,
  commit,
  createGraph,
  history,
  parseDetail,
} from './versionControlService'

const mockFetch = authFetch as unknown as ReturnType<typeof vi.fn>

describe('versionControlService', () => {
  beforeEach(() => mockFetch.mockReset())

  it('createGraph posts to the workspace-scoped url', async () => {
    mockFetch.mockResolvedValueOnce({ id: 'g_1', name: 'G' })
    const g = await createGraph('ws 1', { name: 'G' })
    expect(g.id).toBe('g_1')
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('/api/v1/ws%201/graphs')
    expect(init.method).toBe('POST')
  })

  it('commit returns the CommitResult on success', async () => {
    mockFetch.mockResolvedValueOnce({
      commit_id: 'gcmt_1', commit_hash: 'h', root_hash: 'r',
      delta_summary: { nodes_added: 1 }, branch: 'main',
    })
    const r = await commit('ws', 'g_1', 'main', 'msg', null)
    expect(r.commit_id).toBe('gcmt_1')
  })

  it('maps a structured ref_moved 409 to RefMovedError', async () => {
    // authFetch stringifies an object detail with no `message`.
    mockFetch.mockRejectedValueOnce(
      new Error(JSON.stringify({ code: 'ref_moved', current_head: 'gcmt_new' })),
    )
    await expect(commit('ws', 'g', 'main', 'm', 'gcmt_old')).rejects.toMatchObject(
      { name: 'RefMovedError', currentHead: 'gcmt_new' },
    )
  })

  it('maps an empty_commit 409 to EmptyCommitError', async () => {
    mockFetch.mockRejectedValueOnce(
      new Error(JSON.stringify({ code: 'empty_commit' })),
    )
    await expect(commit('ws', 'g', 'main', 'm', null)).rejects.toBeInstanceOf(
      EmptyCommitError,
    )
  })

  it('maps a validation 422 to GraphValidationError with violations', async () => {
    mockFetch.mockRejectedValueOnce(
      new Error(
        JSON.stringify({
          code: 'validation',
          violations: [
            { code: 'edge_dangling_target', message: 'x', object_kind: 'edge', object_id: 'e1' },
          ],
        }),
      ),
    )
    try {
      await commit('ws', 'g', 'main', 'm', null)
      throw new Error('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(GraphValidationError)
      expect((e as GraphValidationError).violations[0].code).toBe(
        'edge_dangling_target',
      )
    }
  })

  it('passes through an unrecognised error unchanged', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Session expired'))
    await expect(commit('ws', 'g', 'main', 'm', null)).rejects.toThrow(
      'Session expired',
    )
  })

  it('history hits the commits endpoint with limit', async () => {
    mockFetch.mockResolvedValueOnce([])
    await history('ws', 'g_1', 'main', 25)
    expect(mockFetch.mock.calls[0][0]).toBe(
      '/api/v1/ws/graphs/g_1/branches/main/commits?limit=25',
    )
  })

  it('parseDetail returns null for non-JSON messages', () => {
    expect(parseDetail(new Error('plain text'))).toBeNull()
    expect(parseDetail(new Error('{"code":"x"}'))).toEqual({ code: 'x' })
    expect(parseDetail('not an error')).toBeNull()
  })
})
