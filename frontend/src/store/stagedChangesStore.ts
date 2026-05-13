/**
 * StagedChangesStore — Centralized review-before-save layer for canvas edits.
 *
 * Every user-driven edit on the Context View canvas (rename, delete, create,
 * layer assignment, edge edits, move-to-layer rule changes) is staged here
 * before being committed to the backend. The single Save Blueprint button
 * applies them atomically.
 *
 * Why a separate store from the canvas/referenceModel stores?
 * - Canvas/referenceModel mutations happen for instant visual feedback. Their
 *   "before/after" state, summary, and apply intent live here so the user can
 *   review and discard with provenance.
 * - The auto-dirty middleware on referenceModelStore already tracks "is the
 *   blueprint config dirty?" — this store complements it with per-edit
 *   granularity ("which 23 edits are pending?").
 *
 * Apply order:
 *   1) deletes (edges first, then entities — avoids dangling references)
 *   2) updates (renames, edge edits, assignments, layer rule changes)
 *   3) creates (entities, then any edges between them)
 * Within each group changes apply in chronological order.
 */

import { create } from 'zustand'
import { generateId } from '@/lib/utils'
import type { GraphDataProvider } from '@/providers/GraphDataProvider'

export type StagedChangeType =
  | 'create_entity'
  | 'rename_entity'
  | 'update_entity'
  | 'delete_entity'
  | 'assign_layer'
  | 'move_to_layer'
  | 'create_edge'
  | 'edit_edge'
  | 'delete_edge'
  | 'reverse_edge'

/** A single user-staged change awaiting Save. */
export interface StagedChange {
  id: string
  type: StagedChangeType
  /** Entity or edge ID being changed. For new entities/edges, a 'staged-...' temp ID. */
  targetId: string
  /** Optional URN — useful for cross-store lookups (e.g. when targetId is a temp ID). */
  targetUrn?: string
  /** State snapshot prior to the change — used to restore on discard or to render diffs. */
  before?: unknown
  /** Proposed new state. */
  after: unknown
  /** Human-readable one-liner: "Rename 'orders' → 'Orders v2'". */
  summary: string
  /** Per-change apply hook — runs during applyAll. Receives the live provider/wsId. */
  apply?: (ctx: ApplyContext) => Promise<void>
  /** Per-change discard hook — restores `before` state in whatever store owns it. */
  discard?: () => void
  timestamp: number
  /** Set on apply failure so retry can target only failing changes. */
  error?: string
}

export interface ApplyContext {
  provider: GraphDataProvider
  wsId: string
  /**
   * Resolves a temp ID to the real backend ID after a create_entity applies.
   * Subsequent changes that reference the temp ID can patch their target via this.
   */
  resolveTempId: (tempId: string) => string | undefined
  registerTempIdResolution: (tempId: string, realId: string) => void
}

interface StagedChangesState {
  changes: StagedChange[]
  isReviewPanelOpen: boolean
  applyStatus: 'idle' | 'applying' | 'partial-error'
  lastApplyResult: { ok: number; failed: number } | null

  /** Most-recently-discarded changes (for Undo). Multiple-step undo via stack. */
  redoStack: StagedChange[]

  stage: (input: Omit<StagedChange, 'id' | 'timestamp'>) => string
  /** Replace an existing change for the same target — useful when the user renames the same node twice. */
  stageOrReplace: (
    matcher: (c: StagedChange) => boolean,
    input: Omit<StagedChange, 'id' | 'timestamp'>,
  ) => string
  discard: (changeId: string) => void
  discardAll: () => void
  applyAll: (provider: GraphDataProvider, wsId: string) => Promise<{ ok: number; failed: number }>
  openReviewPanel: () => void
  closeReviewPanel: () => void

  /** Undo the most recent staged change — pops it from changes, runs its
   *  discard hook (which restores the canvas), and pushes onto redoStack. */
  undo: () => boolean
  /** Re-stage the most recently undone change. */
  redo: () => boolean
  canUndo: () => boolean
  canRedo: () => boolean

  hasChange: (targetId: string) => boolean
  getChangesForTarget: (targetId: string) => StagedChange[]
  countByType: () => Record<StagedChangeType, number>
}

const APPLY_ORDER_GROUP: Record<StagedChangeType, number> = {
  delete_edge: 1,
  delete_entity: 2,
  rename_entity: 3,
  update_entity: 3,
  edit_edge: 3,
  reverse_edge: 3,
  assign_layer: 3,
  move_to_layer: 3,
  create_entity: 4,
  create_edge: 5,
}

export const useStagedChangesStore = create<StagedChangesState>((set, get) => ({
  changes: [],
  isReviewPanelOpen: false,
  applyStatus: 'idle',
  lastApplyResult: null,
  redoStack: [],

  stage: (input) => {
    const id = generateId('staged')
    const change: StagedChange = { ...input, id, timestamp: Date.now() }
    set((s) => ({
      changes: [...s.changes, change],
      // A new edit invalidates the redo stack — once you make a fresh change,
      // we can't redo a previously-discarded one cleanly anymore.
      redoStack: [],
    }))
    return id
  },

  stageOrReplace: (matcher, input) => {
    const existing = get().changes.find(matcher)
    if (existing) {
      // Preserve the original `before` (the very first state before any edits)
      // but update `after` and `summary` to reflect the latest intent.
      set((s) => ({
        changes: s.changes.map((c) =>
          c.id === existing.id
            ? { ...c, ...input, before: existing.before, id: existing.id, timestamp: Date.now() }
            : c,
        ),
      }))
      return existing.id
    }
    return get().stage(input)
  },

  discard: (changeId) => {
    const change = get().changes.find((c) => c.id === changeId)
    if (!change) return
    try {
      change.discard?.()
    } catch (err) {
      console.error('[StagedChanges] discard hook failed', err)
    }
    set((s) => ({
      changes: s.changes.filter((c) => c.id !== changeId),
      redoStack: [...s.redoStack, change],
    }))
  },

  discardAll: () => {
    const { changes } = get()
    // Discard in reverse order so dependent changes (e.g. rename after create)
    // restore correctly.
    for (let i = changes.length - 1; i >= 0; i--) {
      try {
        changes[i].discard?.()
      } catch (err) {
        console.error('[StagedChanges] discard hook failed', err)
      }
    }
    set({ changes: [], redoStack: [], applyStatus: 'idle', lastApplyResult: null })
  },

  undo: () => {
    const { changes } = get()
    if (changes.length === 0) return false
    const last = changes[changes.length - 1]
    try {
      last.discard?.()
    } catch (err) {
      console.error('[StagedChanges] undo discard hook failed', err)
    }
    set((s) => ({
      changes: s.changes.slice(0, -1),
      redoStack: [...s.redoStack, last],
    }))
    return true
  },

  redo: () => {
    const { redoStack } = get()
    if (redoStack.length === 0) return false
    const change = redoStack[redoStack.length - 1]
    // Re-staging requires the apply hook to be intact, which it is — but the
    // change's "before" state may no longer match the canvas (the user might
    // have edited around it). We push it back without re-running side effects;
    // the user is responsible for re-applying any visual mutations through
    // the same UI path. In practice this is safe for delete/rename which keep
    // their state in `before` and use it on apply.
    set((s) => ({
      changes: [...s.changes, change],
      redoStack: s.redoStack.slice(0, -1),
    }))
    return true
  },

  canUndo: () => get().changes.length > 0,
  canRedo: () => get().redoStack.length > 0,

  applyAll: async (provider, wsId) => {
    const { changes } = get()
    if (changes.length === 0) return { ok: 0, failed: 0 }

    set({ applyStatus: 'applying' })

    const tempIdMap = new Map<string, string>()
    const ctx: ApplyContext = {
      provider,
      wsId,
      resolveTempId: (tempId) => tempIdMap.get(tempId),
      registerTempIdResolution: (tempId, realId) => tempIdMap.set(tempId, realId),
    }

    const sorted = [...changes].sort((a, b) => {
      const ga = APPLY_ORDER_GROUP[a.type]
      const gb = APPLY_ORDER_GROUP[b.type]
      if (ga !== gb) return ga - gb
      return a.timestamp - b.timestamp
    })

    let ok = 0
    let failed = 0
    const remaining: StagedChange[] = []

    for (const change of sorted) {
      if (!change.apply) {
        // No backend apply hook — treat as a local-only change that's already
        // committed to its owning store. Drop it from the staging list.
        ok++
        continue
      }
      try {
        await change.apply(ctx)
        ok++
      } catch (err) {
        failed++
        const errMsg = err instanceof Error ? err.message : String(err)
        console.error('[StagedChanges] apply failed for', change.type, change.targetId, err)
        remaining.push({ ...change, error: errMsg })
      }
    }

    set({
      changes: remaining,
      applyStatus: failed > 0 ? 'partial-error' : 'idle',
      lastApplyResult: { ok, failed },
    })

    return { ok, failed }
  },

  openReviewPanel: () => set({ isReviewPanelOpen: true }),
  closeReviewPanel: () => set({ isReviewPanelOpen: false }),

  hasChange: (targetId) => get().changes.some((c) => c.targetId === targetId),
  getChangesForTarget: (targetId) =>
    get().changes.filter((c) => c.targetId === targetId),

  countByType: () => {
    const counts: Record<StagedChangeType, number> = {
      create_entity: 0,
      rename_entity: 0,
      update_entity: 0,
      delete_entity: 0,
      assign_layer: 0,
      move_to_layer: 0,
      create_edge: 0,
      edit_edge: 0,
      delete_edge: 0,
      reverse_edge: 0,
    }
    get().changes.forEach((c) => {
      counts[c.type]++
    })
    return counts
  },
}))

// ============================================
// Selector Hooks (memo-friendly slices)
// ============================================

export const useStagedChanges = () => useStagedChangesStore((s) => s.changes)
export const useStagedChangeCount = () => useStagedChangesStore((s) => s.changes.length)
export const useIsReviewPanelOpen = () => useStagedChangesStore((s) => s.isReviewPanelOpen)
export const useHasStagedChange = (targetId: string) =>
  useStagedChangesStore((s) => s.changes.some((c) => c.targetId === targetId))

/**
 * Returns the latest staged change for a target, or undefined.
 * The "latest" determines the visual indicator color (green/amber/red).
 */
export const useLatestStagedChange = (targetId: string) =>
  useStagedChangesStore((s) => {
    const matches = s.changes.filter((c) => c.targetId === targetId)
    return matches.length > 0 ? matches[matches.length - 1] : undefined
  })

/**
 * Color for a staged-change badge based on the change type.
 * - green: creation
 * - red: deletion
 * - amber: any modification
 */
export function stagedChangeColor(type: StagedChangeType): 'green' | 'amber' | 'red' {
  switch (type) {
    case 'create_entity':
    case 'create_edge':
      return 'green'
    case 'delete_entity':
    case 'delete_edge':
      return 'red'
    default:
      return 'amber'
  }
}
