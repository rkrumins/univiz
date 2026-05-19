import { beforeEach, describe, expect, it } from 'vitest'
import { useGraphEditorStore } from './graphEditorStore'

const S = () => useGraphEditorStore.getState()

const addNode = (id: string, name = id) => ({
  changeType: 'add_node' as const,
  objectKind: 'node' as const,
  objectId: id,
  payload: { key: id, display_name: name },
  summary: `add ${id}`,
})

describe('graphEditorStore', () => {
  beforeEach(() => S().reset())

  it('starts clean and init pins base commit', () => {
    S().init('g1', 'main', 'gcmt_base')
    expect(S().syncState).toBe('clean')
    expect(S().baseCommitId).toBe('gcmt_base')
    expect(S().ops).toEqual([])
  })

  it('applyOp adds an op and goes dirty', () => {
    S().init('g1', 'main', null)
    S().applyOp(addNode('urn:a'))
    expect(S().ops).toHaveLength(1)
    expect(S().syncState).toBe('dirty')
  })

  it('coalesces repeated edits to the same object (one op, original before kept)', () => {
    S().init('g1', 'main', null)
    S().applyOp({
      changeType: 'update_node', objectKind: 'node', objectId: 'urn:a',
      payload: { key: 'urn:a', display_name: 'v1' },
      before: { display_name: 'committed' }, summary: 'e1',
    })
    S().applyOp({
      changeType: 'update_node', objectKind: 'node', objectId: 'urn:a',
      payload: { key: 'urn:a', display_name: 'v2' },
      before: { display_name: 'IGNORED' }, summary: 'e2',
    })
    expect(S().ops).toHaveLength(1)
    expect(S().ops[0].payload.display_name).toBe('v2')
    // original before preserved across coalescing
    expect(S().ops[0].before).toEqual({ display_name: 'committed' })
  })

  it('add-then-delete of an uncommitted object cancels out', () => {
    S().init('g1', 'main', null)
    S().applyOp(addNode('urn:tmp'))
    S().applyOp({
      changeType: 'delete_node', objectKind: 'node',
      objectId: 'urn:tmp', payload: { key: 'urn:tmp' }, summary: 'del',
    })
    expect(S().ops).toHaveLength(0)
    expect(S().syncState).toBe('clean')
  })

  it('undo/redo move the last op and toggle clean/dirty', () => {
    S().init('g1', 'main', null)
    S().applyOp(addNode('urn:a'))
    expect(S().undo()).toBe(true)
    expect(S().ops).toHaveLength(0)
    expect(S().syncState).toBe('clean')
    expect(S().redo()).toBe(true)
    expect(S().ops).toHaveLength(1)
    expect(S().syncState).toBe('dirty')
    // nothing left to undo past empty
    S().undo()
    expect(S().undo()).toBe(false)
  })

  it('reconcileTempIds rewrites temp ids in objectId and payload.key', () => {
    S().init('g1', 'main', null)
    S().applyOp(addNode('staged_1', 'Fresh'))
    S().reconcileTempIds({ staged_1: 'urn:real' })
    expect(S().ops[0].objectId).toBe('urn:real')
    expect(S().ops[0].payload.key).toBe('urn:real')
  })

  it('onRefMoved enters conflict with the server head', () => {
    S().init('g1', 'main', 'gcmt_old')
    S().applyOp(addNode('urn:a'))
    S().onRefMoved('gcmt_new')
    expect(S().syncState).toBe('conflict')
    expect(S().conflictHead).toBe('gcmt_new')
    // local ops are retained (non-destructive rebase)
    expect(S().ops).toHaveLength(1)
  })

  it('clearAfterCommit empties ops and advances base', () => {
    S().init('g1', 'main', 'gcmt_old')
    S().applyOp(addNode('urn:a'))
    S().clearAfterCommit('gcmt_new')
    expect(S().ops).toEqual([])
    expect(S().baseCommitId).toBe('gcmt_new')
    expect(S().syncState).toBe('clean')
  })

  it('summary counts ops by change type', () => {
    S().init('g1', 'main', null)
    S().applyOp(addNode('urn:a'))
    S().applyOp(addNode('urn:b'))
    S().applyOp({
      changeType: 'add_edge', objectKind: 'edge', objectId: 'e1',
      payload: { key: 'e1' }, summary: 'add e1',
    })
    const sum = S().summary()
    expect(sum.add_node).toBe(2)
    expect(sum.add_edge).toBe(1)
    expect(sum.delete_node).toBe(0)
  })
})
