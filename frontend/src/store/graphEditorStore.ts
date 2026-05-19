/**
 * graphEditorStore — the persisted, per-branch working set for the
 * authored-graph editor.
 *
 * Generalizes the proven `stagedChangesStore` patterns (per-object
 * coalescing, undo/redo, temp-id resolution) into a backend-synced,
 * branch-aware, ref-aware store. `stagedChangesStore` is left untouched
 * (still used by the Context View).
 *
 * This module is pure state logic (no React, no fetch) so it is fully
 * Vitest-testable; the page layer wires it to versionControlService +
 * React Query mutations.
 */
import { create } from 'zustand'

export type EditorChangeType =
  | 'add_node' | 'update_node' | 'delete_node'
  | 'add_edge' | 'update_edge' | 'delete_edge'

export type EditorObjectKind = 'node' | 'edge'

export interface EditorOp {
  /** Stable per-op id (also used as React key). */
  opId: string
  changeType: EditorChangeType
  objectKind: EditorObjectKind
  /** Real id, or a `staged_*` temp id for not-yet-committed creates. */
  objectId: string
  /** Resolved final content for add/update; `{ key }` for delete. */
  payload: Record<string, unknown>
  /** First-seen committed value, preserved across coalescing so undo
   * restores the right baseline. */
  before?: Record<string, unknown> | null
  summary: string
  ts: number
}

export type SyncState =
  | 'clean' | 'dirty' | 'committing' | 'conflict' | 'error'

export interface GraphEditorState {
  graphId: string | null
  branch: string
  /** Commit the working set forked from (the optimistic-concurrency
   * token sent on commit). */
  baseCommitId: string | null
  ops: EditorOp[]
  undoStack: EditorOp[]
  redoStack: EditorOp[]
  syncState: SyncState
  /** Set when the server reports the ref moved under us. */
  conflictHead: string | null

  init: (graphId: string, branch: string, baseCommitId: string | null) => void
  applyOp: (
    op: Omit<EditorOp, 'opId' | 'ts'> & { opId?: string },
  ) => void
  undo: () => boolean
  redo: () => boolean
  /** Map server-assigned real ids over `staged_*` temp ids after a
   * draft save / commit. */
  reconcileTempIds: (mapping: Record<string, string>) => void
  onRefMoved: (currentHead: string | null) => void
  /** Commit succeeded: clear ops, advance base, back to clean. */
  clearAfterCommit: (newCommitId: string) => void
  setSyncState: (s: SyncState) => void
  summary: () => Record<EditorChangeType, number>
  reset: () => void
}

let _seq = 0
const nextOpId = () => `op_${Date.now().toString(36)}_${(_seq++).toString(36)}`

const isAdd = (t: EditorChangeType) => t.startsWith('add_')
const isDelete = (t: EditorChangeType) => t.startsWith('delete_')

export const useGraphEditorStore = create<GraphEditorState>((set, get) => ({
  graphId: null,
  branch: 'main',
  baseCommitId: null,
  ops: [],
  undoStack: [],
  redoStack: [],
  syncState: 'clean',
  conflictHead: null,

  init: (graphId, branch, baseCommitId) =>
    set({
      graphId,
      branch,
      baseCommitId,
      ops: [],
      undoStack: [],
      redoStack: [],
      syncState: 'clean',
      conflictHead: null,
    }),

  applyOp: (input) => {
    const ops = [...get().ops]
    const idx = ops.findIndex(
      (o) =>
        o.objectKind === input.objectKind && o.objectId === input.objectId,
    )

    // add-then-delete of a never-committed object cancels out entirely
    // (no net change to commit) — mirrors `git add` then `git rm`.
    if (
      idx >= 0 &&
      isAdd(ops[idx].changeType) &&
      isDelete(input.changeType)
    ) {
      ops.splice(idx, 1)
      set({
        ops,
        syncState: ops.length ? 'dirty' : 'clean',
      })
      return
    }

    if (idx >= 0) {
      // Coalesce: keep opId + the ORIGINAL `before` so undo restores
      // the committed baseline, not an intermediate edit.
      ops[idx] = {
        ...ops[idx],
        changeType: input.changeType,
        payload: input.payload,
        summary: input.summary,
        ts: Date.now(),
      }
    } else {
      ops.push({
        opId: input.opId ?? nextOpId(),
        changeType: input.changeType,
        objectKind: input.objectKind,
        objectId: input.objectId,
        payload: input.payload,
        before: input.before ?? null,
        summary: input.summary,
        ts: Date.now(),
      })
    }
    set({ ops, syncState: 'dirty', redoStack: [] })
  },

  undo: () => {
    const { ops, undoStack, redoStack } = get()
    if (ops.length === 0) return false
    const last = ops[ops.length - 1]
    set({
      ops: ops.slice(0, -1),
      undoStack: [...undoStack, last],
      redoStack: [...redoStack, last],
      syncState: ops.length - 1 ? 'dirty' : 'clean',
    })
    return true
  },

  redo: () => {
    const { redoStack, ops } = get()
    if (redoStack.length === 0) return false
    const op = redoStack[redoStack.length - 1]
    set({
      ops: [...ops, op],
      redoStack: redoStack.slice(0, -1),
      syncState: 'dirty',
    })
    return true
  },

  reconcileTempIds: (mapping) => {
    if (!Object.keys(mapping).length) return
    set({
      ops: get().ops.map((o) => {
        const real = mapping[o.objectId]
        if (!real) return o
        const payload = { ...o.payload }
        if (payload.key === o.objectId) payload.key = real
        return { ...o, objectId: real, payload }
      }),
    })
  },

  onRefMoved: (currentHead) =>
    set({ syncState: 'conflict', conflictHead: currentHead }),

  clearAfterCommit: (newCommitId) =>
    set({
      ops: [],
      undoStack: [],
      redoStack: [],
      baseCommitId: newCommitId,
      syncState: 'clean',
      conflictHead: null,
    }),

  setSyncState: (s) => set({ syncState: s }),

  summary: () => {
    const acc = {
      add_node: 0, update_node: 0, delete_node: 0,
      add_edge: 0, update_edge: 0, delete_edge: 0,
    } as Record<EditorChangeType, number>
    for (const o of get().ops) acc[o.changeType] += 1
    return acc
  },

  reset: () =>
    set({
      graphId: null,
      branch: 'main',
      baseCommitId: null,
      ops: [],
      undoStack: [],
      redoStack: [],
      syncState: 'clean',
      conflictHead: null,
    }),
}))
