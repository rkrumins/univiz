/**
 * versionControlBridge — integrates the existing Context View
 * `stagedChangesStore` with the new authored-graph version control.
 *
 * Design constraints (deliberate, per CLAUDE.md surgical rule):
 *  - The existing Context View provider-write path (`applyAll`) is NOT
 *    touched. This bridge is *additive*: it translates the same
 *    `StagedChange`s into version-control inputs and, optionally,
 *    mirrors them live into `graphEditorStore` so the new versioning
 *    UI reflects what the user adds/updates/deletes in Context View.
 *  - `after` on a `StagedChange` is domain-shaped `unknown`. Content
 *    extraction is therefore defensive and pluggable (inject a custom
 *    extractor for unusual payloads); unmappable changes return null
 *    rather than throwing, so a partial mapping never blocks Save.
 *
 * Target-graph resolution: a Context View edits a *connected* graph.
 * Mapping a data source to its versioned `graph_id` is the Phase-2
 * adoption/genesis-import path; until then `syncStagedChangesToVersion
 * Control` requires the caller to pass the resolved target explicitly.
 */
import type { StagedChange, StagedChangeType } from '../store/stagedChangesStore'
import { useGraphEditorStore } from '../store/graphEditorStore'
import type { EditorChangeType } from '../store/graphEditorStore'
import { stageChanges, type StagedChangeInput } from './versionControlService'

/** How a Context View change type maps to a version-control op. */
const TYPE_MAP: Record<
  StagedChangeType,
  { ct: StagedChangeInput['change_type']; kind: 'node' | 'edge' } | null
> = {
  create_entity: { ct: 'add_node', kind: 'node' },
  rename_entity: { ct: 'update_node', kind: 'node' },
  delete_entity: { ct: 'delete_node', kind: 'node' },
  // Layer assignment is a node property change in the versioned model.
  assign_layer: { ct: 'update_node', kind: 'node' },
  move_to_layer: { ct: 'update_node', kind: 'node' },
  create_edge: { ct: 'add_edge', kind: 'edge' },
  edit_edge: { ct: 'update_edge', kind: 'edge' },
  delete_edge: { ct: 'delete_edge', kind: 'edge' },
  // Reversing endpoints is an edge content change (new edge version).
  reverse_edge: { ct: 'update_edge', kind: 'edge' },
}

type AnyRec = Record<string, unknown>

const pick = (o: AnyRec, ...keys: string[]): unknown => {
  for (const k of keys) if (o && o[k] != null) return o[k]
  return undefined
}

/** Default best-effort content extraction from a StagedChange.after. */
export function defaultExtract(
  change: StagedChange,
  kind: 'node' | 'edge',
): AnyRec {
  const a = (change.after ?? {}) as AnyRec
  const key =
    change.targetUrn ||
    (typeof change.targetId === 'string' ? change.targetId : '') ||
    String(pick(a, 'urn', 'id', 'key') ?? '')

  if (kind === 'node') {
    return {
      key,
      entity_type: pick(a, 'entityType', 'entity_type', 'type') ?? null,
      display_name:
        pick(a, 'displayName', 'display_name', 'label', 'name') ?? null,
      position: pick(a, 'position') ?? null,
      properties: (pick(a, 'properties', 'props') as AnyRec) ?? {},
      tags: (pick(a, 'tags') as unknown[]) ?? [],
    }
  }
  return {
    key,
    source_key: String(pick(a, 'sourceUrn', 'source_key', 'source', 'from') ?? ''),
    target_key: String(pick(a, 'targetUrn', 'target_key', 'target', 'to') ?? ''),
    edge_type: pick(a, 'edgeType', 'edge_type', 'relationship', 'type') ?? null,
    confidence: pick(a, 'confidence') ?? null,
    properties: (pick(a, 'properties', 'props') as AnyRec) ?? {},
  }
}

export interface BridgeOptions {
  /** Override content extraction for non-standard `after` shapes. */
  extract?: (change: StagedChange, kind: 'node' | 'edge') => AnyRec
}

/** Translate one StagedChange → a version-control stage input, or null
 * if it does not map to graph content. Pure. */
export function stagedChangeToInput(
  change: StagedChange,
  opts: BridgeOptions = {},
): StagedChangeInput | null {
  const m = TYPE_MAP[change.type]
  if (!m) return null
  const extract = opts.extract ?? defaultExtract
  const isDelete = m.ct.startsWith('delete_')
  const content = isDelete ? {} : extract(change, m.kind)
  const objectId =
    change.targetUrn ||
    change.targetId ||
    String((content as AnyRec).key ?? '')
  if (!objectId) return null
  return {
    change_type: m.ct,
    object_kind: m.kind,
    object_id: objectId,
    payload: isDelete ? { key: objectId } : { key: objectId, ...content },
    summary: change.summary,
  }
}

/** Translate a batch, dropping unmappable changes (order preserved). */
export function stagedChangesToInputs(
  changes: StagedChange[],
  opts: BridgeOptions = {},
): StagedChangeInput[] {
  const out: StagedChangeInput[] = []
  for (const c of changes) {
    const m = stagedChangeToInput(c, opts)
    if (m) out.push(m)
  }
  return out
}

/** Live-mirror the current Context View staged changes into the new
 * `graphEditorStore` so the versioning UI reflects them. Replaces the
 * editor working set with the mapped set (idempotent). */
export function mirrorToEditorStore(
  changes: StagedChange[],
  opts: BridgeOptions = {},
): void {
  const inputs = stagedChangesToInputs(changes, opts)
  const store = useGraphEditorStore.getState()
  // Rebuild from a clean slate so discards in Context View propagate.
  store.reset()
  for (const i of inputs) {
    store.applyOp({
      changeType: i.change_type as EditorChangeType,
      objectKind: i.object_kind,
      objectId: i.object_id,
      payload: i.payload,
      summary: i.summary ?? '',
    })
  }
}

/**
 * Push the Context View staged changes to the version-control backend
 * (stage them onto the working set). The caller resolves the target
 * versioned graph (Phase-2 adoption maps a data source → graph_id;
 * until then it is passed explicitly). Returns the number of mapped
 * changes staged. Errors from the service (incl. typed RefMoved /
 * Validation) propagate so the Save UI can react.
 */
export async function syncStagedChangesToVersionControl(
  target: { wsId: string; graphId: string; branch: string },
  changes: StagedChange[],
  opts: BridgeOptions = {},
): Promise<number> {
  const inputs = stagedChangesToInputs(changes, opts)
  if (inputs.length === 0) return 0
  await stageChanges(target.wsId, target.graphId, target.branch, inputs)
  return inputs.length
}

/**
 * Opt-in, additive subscription: whenever Context View staged changes
 * change, mirror them into the editor store. Returns an unsubscribe
 * fn. No side effects on import — the Context View calls this on mount
 * if the new versioning surface is enabled (so existing behaviour is
 * untouched when it is not).
 */
export function attachStagedChangesMirror(
  store: {
    subscribe: (cb: (s: { changes: StagedChange[] }) => void) => () => void
    getState: () => { changes: StagedChange[] }
  },
  opts: BridgeOptions = {},
): () => void {
  mirrorToEditorStore(store.getState().changes, opts)
  return store.subscribe((s) => mirrorToEditorStore(s.changes, opts))
}
