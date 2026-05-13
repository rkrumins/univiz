/**
 * edgeApi — thin wrappers around the backend edge mutation endpoints.
 *
 * Backend routes:
 *   PATCH  /api/v1/{wsId}/graph/edges/{edgeId}   → backend/app/api/v1/endpoints/graph.py:1070
 *   DELETE /api/v1/{wsId}/graph/edges/{edgeId}   → backend/app/api/v1/endpoints/graph.py:1080
 *
 * Consumed by the staged-change apply hooks for `edit_edge` and `delete_edge`.
 */

import { authFetch } from './apiClient'

export interface EdgeMutationResult {
  id: string
  source: string
  target: string
  edgeType?: string
  properties?: Record<string, unknown>
}

export async function patchEdge(
  wsId: string,
  edgeId: string,
  properties: Record<string, unknown>,
): Promise<EdgeMutationResult> {
  return authFetch<EdgeMutationResult>(
    `/api/v1/${encodeURIComponent(wsId)}/graph/edges/${encodeURIComponent(edgeId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ properties }),
    },
  )
}

export async function deleteEdge(wsId: string, edgeId: string): Promise<void> {
  await authFetch<void>(
    `/api/v1/${encodeURIComponent(wsId)}/graph/edges/${encodeURIComponent(edgeId)}`,
    { method: 'DELETE' },
  )
}
