/**
 * versionControlService — typed client for the authored-graph
 * version-control API (/api/v1/{wsId}/graphs/...).
 *
 * Wraps `authFetch`. The backend returns structured error bodies as
 * `{detail: {code, ...}}`; `authFetch` collapses an object detail with
 * no `message` to a JSON string in `Error.message`. So mutation calls
 * here re-parse that and raise *typed* errors the UI branches on:
 *   - RefMovedError       -> the "review & rebase" banner
 *   - GraphValidationError -> inline violation list
 *   - EmptyCommitError     -> "nothing to commit" toast
 */
import { authFetch } from './apiClient'

export interface GraphSummary {
  id: string
  workspace_id: string
  name: string
  description?: string | null
  origin: string
  schema_mode: string
  default_branch: string
  head_commit_id?: string | null
}

export interface CommitResult {
  commit_id: string
  commit_hash: string
  root_hash: string
  delta_summary: Record<string, number>
  branch: string
}

export interface CommitHistoryEntry {
  commit_id: string
  commit_hash: string
  parent_ids: string[]
  author?: string | null
  message?: string | null
  delta_summary: Record<string, number>
  committed_at: string
}

export interface Violation {
  code: string
  message: string
  object_kind: string
  object_id: string
}

export class RefMovedError extends Error {
  currentHead: string | null
  constructor(currentHead: string | null) {
    super('The branch moved — rebase required before committing.')
    this.name = 'RefMovedError'
    this.currentHead = currentHead
  }
}

export class GraphValidationError extends Error {
  violations: Violation[]
  constructor(violations: Violation[]) {
    super(`${violations.length} validation issue(s)`)
    this.name = 'GraphValidationError'
    this.violations = violations
  }
}

export class EmptyCommitError extends Error {
  constructor() {
    super('Nothing to commit.')
    this.name = 'EmptyCommitError'
  }
}

/** Best-effort parse of a structured detail out of an Error thrown by
 * authFetch (it may be a JSON string of the detail object, the
 * `.message` of a structured detail, or a plain string). */
export function parseDetail(err: unknown): any | null {
  if (!(err instanceof Error)) return null
  const msg = err.message
  try {
    const parsed = JSON.parse(msg)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function rethrowTyped(err: unknown): never {
  const d = parseDetail(err)
  if (d) {
    if (d.code === 'ref_moved') throw new RefMovedError(d.current_head ?? null)
    if (d.code === 'empty_commit') throw new EmptyCommitError()
    if (d.code === 'validation' && Array.isArray(d.violations)) {
      throw new GraphValidationError(d.violations as Violation[])
    }
  }
  throw err instanceof Error ? err : new Error(String(err))
}

const base = (ws: string) => `/api/v1/${encodeURIComponent(ws)}/graphs`

export async function createGraph(
  wsId: string,
  body: { name: string; description?: string; schema_mode?: string },
): Promise<GraphSummary> {
  return authFetch<GraphSummary>(`${base(wsId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function listGraphs(wsId: string): Promise<GraphSummary[]> {
  return authFetch<GraphSummary[]>(`${base(wsId)}`)
}

export async function getGraph(wsId: string, graphId: string): Promise<GraphSummary> {
  return authFetch<GraphSummary>(`${base(wsId)}/${graphId}`)
}

export interface StagedChangeInput {
  change_type:
    | 'add_node' | 'update_node' | 'delete_node'
    | 'add_edge' | 'update_edge' | 'delete_edge'
  object_kind: 'node' | 'edge'
  object_id: string
  payload: Record<string, unknown>
  summary?: string
}

export async function stageChanges(
  wsId: string,
  graphId: string,
  branch: string,
  changes: StagedChangeInput[],
): Promise<{ ws_change_version: number }> {
  return authFetch(`${base(wsId)}/${graphId}/branches/${encodeURIComponent(branch)}/stage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ changes }),
  })
}

export async function getWorkingSet(
  wsId: string,
  graphId: string,
  branch: string,
): Promise<{
  base_commit_id: string | null
  ws_change_version: number
  changes: Array<{
    change_type: string; object_kind: string; object_id: string
    summary: string; after: Record<string, unknown> | null
  }>
}> {
  return authFetch(`${base(wsId)}/${graphId}/branches/${encodeURIComponent(branch)}/working-set`)
}

export async function commit(
  wsId: string,
  graphId: string,
  branch: string,
  message: string,
  expectedHeadCommitId: string | null,
): Promise<CommitResult> {
  try {
    return await authFetch<CommitResult>(
      `${base(wsId)}/${graphId}/branches/${encodeURIComponent(branch)}/commits`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          expected_head_commit_id: expectedHeadCommitId,
        }),
      },
    )
  } catch (err) {
    rethrowTyped(err)
  }
}

export async function history(
  wsId: string,
  graphId: string,
  branch: string,
  limit = 50,
): Promise<CommitHistoryEntry[]> {
  return authFetch<CommitHistoryEntry[]>(
    `${base(wsId)}/${graphId}/branches/${encodeURIComponent(branch)}/commits?limit=${limit}`,
  )
}

export async function createBranch(
  wsId: string,
  graphId: string,
  name: string,
  fromCommitId: string | null,
): Promise<{ branch: string; commit_id: string | null }> {
  return authFetch(`${base(wsId)}/${graphId}/branches`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, from_commit_id: fromCommitId }),
  })
}
