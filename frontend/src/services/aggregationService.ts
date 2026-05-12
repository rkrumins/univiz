import { authFetch } from './apiClient';

export interface AggregationTriggerRequest {
  ontologyId?: string;
  projectionMode: string;
  batchSize: number;
  maxRetries?: number;
  timeoutSecs?: number;
}

export interface AggregationSkipRequest {
  confirmed: boolean;
}

export interface AggregationScheduleRequest {
  cronExpression: string | null;
}

export interface AggregationJobResponse {
  id: string;
  dataSourceId: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  triggerSource: string;
  progress: number;
  totalEdges: number;
  processedEdges: number;
  createdEdges: number;
  batchSize: number;
  lastCheckpointAt?: string;
  /**
   * Cursor-based resume checkpoint. Non-null implies the worker can resume from this position.
   * BE-1 must expose this field on the API response — currently absent server-side; if missing
   * at runtime the Resume button stays hidden (treated as null).
   */
  lastCursor?: string | null;
  resumable: boolean;
  retryCount: number;
  maxRetries?: number;
  timeoutSecs?: number;
  errorMessage?: string;
  estimatedCompletionAt?: string;
  startedAt?: string;
  completedAt?: string;
  updatedAt?: string;
  createdAt: string;
  // Enrichment fields — populated by global listing endpoint
  workspaceId?: string;
  workspaceName?: string;
  dataSourceLabel?: string;
  projectionMode?: string;
  durationSeconds?: number;
  edgeCoveragePct?: number;
  /**
   * Phase 1.7 — short ID for the currently-active phase of the bulk-rebuild path.
   * One of: 'wiping' | 'scanning' | 'resolving_labels' | 'creating' | 'finalizing'.
   * Null on legacy paths (Neo4j / Spanner / legacy MERGE) — UI falls back to a generic label.
   */
  currentPhase?: string | null;
}

export interface ResumeOverrides {
  batchSize?: number;
  projectionMode?: 'in_source' | 'dedicated';
  maxRetries?: number;
  timeoutSecs?: number;
}

export interface PaginatedJobsResponse {
  items: AggregationJobResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobHistoryFilters {
  status?: string[];
  workspaceId?: string;
  dataSourceId?: string[];
  projectionMode?: string;
  triggerSource?: string;
  dateFrom?: string;
  dateTo?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export interface DataSourceReadinessResponse {
  dataSourceId: string;
  isReady: boolean;
  aggregationStatus: 'none' | 'pending' | 'running' | 'ready' | 'failed' | 'skipped';
  canCreateViews: boolean;
  activeJob?: AggregationJobResponse;
  driftDetected: boolean;
  lastAggregatedAt?: string;
  aggregationEdgeCount: number;
  message: string;
}

export interface DriftCheckResponse {
  driftDetected: boolean;
  currentFingerprint?: string;
  storedFingerprint?: string;
  lastCheckedAt?: string;
}

export interface JobsSummary {
  total: number;
  byStatus: Record<string, number>;
  successRate: number | null;
  avgDurationSeconds: number | null;
}

class AggregationService {
  async getJobsSummary(): Promise<JobsSummary> {
    return authFetch<JobsSummary>('/api/v1/admin/aggregation-jobs/summary');
  }

  async triggerAggregation(
    dataSourceId: string,
    request: AggregationTriggerRequest,
    triggerSource: 'manual' | 'onboarding' = 'manual'
  ): Promise<AggregationJobResponse> {
    return authFetch<AggregationJobResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/aggregation-jobs?triggerSource=${triggerSource}`,
      {
        method: 'POST',
        body: JSON.stringify(request),
      }
    );
  }

  async getReadiness(dataSourceId: string): Promise<DataSourceReadinessResponse> {
    return authFetch<DataSourceReadinessResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/readiness`
    );
  }

  async listJobs(dataSourceId: string, status?: string): Promise<AggregationJobResponse[]> {
    const query = status ? `?status=${status}` : '';
    return authFetch<AggregationJobResponse[]>(
      `/api/v1/admin/data-sources/${dataSourceId}/aggregation-jobs${query}`
    );
  }

  async getJob(dataSourceId: string, jobId: string): Promise<AggregationJobResponse> {
    return authFetch<AggregationJobResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/aggregation-jobs/${jobId}`
    );
  }

  async resumeJob(
    dataSourceId: string,
    jobId: string,
    overrides?: ResumeOverrides,
  ): Promise<AggregationJobResponse> {
    const init: RequestInit = { method: 'POST' };
    if (overrides && Object.keys(overrides).length > 0) {
      init.body = JSON.stringify(overrides);
    }
    return authFetch<AggregationJobResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/aggregation-jobs/${jobId}/resume`,
      init,
    );
  }

  async cancelJob(dataSourceId: string, jobId: string): Promise<AggregationJobResponse> {
    return authFetch<AggregationJobResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/aggregation-jobs/${jobId}/cancel`,
      { method: 'POST' }
    );
  }

  async deleteJob(jobId: string): Promise<void> {
    return authFetch<void>(
      `/api/v1/admin/aggregation-jobs/${jobId}`,
      { method: 'DELETE' }
    );
  }

  /**
   * Queue an asynchronous purge job. Returns immediately with the job
   * row in `running` state — `deletedEdges` is 0 at this point and gets
   * populated once the background task finishes. Frontend should
   * monitor progress via the standard aggregation-jobs endpoints
   * (Job History UI handles this automatically).
   */
  async purgeAggregation(dataSourceId: string): Promise<{
    deletedEdges: number
    dataSourceId: string
    jobId: string
    status: 'running' | 'completed' | 'failed'
  }> {
    return authFetch(
      `/api/v1/admin/data-sources/${dataSourceId}/purge-aggregation`,
      { method: 'POST' }
    );
  }

  async skipAggregation(dataSourceId: string): Promise<DataSourceReadinessResponse> {
    return authFetch<DataSourceReadinessResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/skip-aggregation`,
      {
        method: 'POST',
        body: JSON.stringify({ confirmed: true }),
      }
    );
  }

  async setSchedule(dataSourceId: string, cronExpression: string | null): Promise<void> {
    return authFetch<void>(
      `/api/v1/admin/data-sources/${dataSourceId}/aggregation-schedule`,
      {
        method: 'PUT',
        body: JSON.stringify({ cronExpression }),
      }
    );
  }

  async listJobsGlobal(filters: JobHistoryFilters = {}): Promise<PaginatedJobsResponse> {
    const params = new URLSearchParams();
    if (filters.status?.length) filters.status.forEach(s => params.append('status', s));
    if (filters.workspaceId) params.set('workspaceId', filters.workspaceId);
    if (filters.dataSourceId?.length) filters.dataSourceId.forEach(id => params.append('dataSourceId', id));
    if (filters.projectionMode) params.set('projectionMode', filters.projectionMode);
    if (filters.triggerSource) params.set('triggerSource', filters.triggerSource);
    if (filters.dateFrom) params.set('dateFrom', filters.dateFrom);
    if (filters.dateTo) params.set('dateTo', filters.dateTo);
    if (filters.search) params.set('search', filters.search);
    if (filters.limit) params.set('limit', String(filters.limit));
    if (filters.offset !== undefined) params.set('offset', String(filters.offset));
    const qs = params.toString();
    return authFetch<PaginatedJobsResponse>(
      `/api/v1/admin/aggregation-jobs${qs ? `?${qs}` : ''}`
    );
  }

  async checkDrift(dataSourceId: string): Promise<DriftCheckResponse> {
    return authFetch<DriftCheckResponse>(
      `/api/v1/admin/data-sources/${dataSourceId}/check-drift`
    );
  }
}

export const aggregationService = new AggregationService();
