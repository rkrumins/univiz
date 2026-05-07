import { authFetch } from './apiClient';

export interface OntologyResolutionRelGap {
  id: string;
  name: string;
  isContainment: boolean | null;
  isLineage: boolean | null;
}

export interface OntologyResolutionHierarchyGap {
  entityType: string;
  missingField: 'level' | 'can_contain' | 'can_be_contained_by' | 'root_membership';
}

export interface OntologyResolutionResponse {
  resolved: boolean;
  ontologyId: string | null;
  ontologyVersion: number | null;
  ontologyIsPublished: boolean;
  missingEntityTypes: string[];
  missingEdgeTypes: string[];
  unclassifiedRelationships: OntologyResolutionRelGap[];
  hasLineage: boolean;
  /** True when at least one relationship has ``is_containment=true``.
   *  When false, aggregation runs but cannot propagate AGGREGATED
   *  edges up the containment tree — leaves only edges between
   *  direct lineage endpoints. Surfaced as ``advisoryWarnings:
   *  ["no_containment_edges"]``. */
  hasContainment: boolean;
  hierarchyWarnings: OntologyResolutionHierarchyGap[];
  /** Non-blocking advisories. Currently:
   *  - ``no_containment_edges``: no relationship is is_containment=true. */
  advisoryWarnings: string[];
  blockingReasons: string[];
  fingerprint: string | null;
}

export const ontologyResolutionService = {
  /**
   * Run the ontology-resolution gate for a data source. Drives the
   * AssetOnboardingWizard SchemaReviewStep gate (post-onboarding) and
   * any pre-flight check before triggering aggregation manually. The
   * gate is also enforced by the backend trigger endpoint — this is
   * the read-only inspector.
   */
  async getResolution(dataSourceId: string): Promise<OntologyResolutionResponse> {
    return authFetch<OntologyResolutionResponse>(
      `/api/v1/admin/data-sources/${encodeURIComponent(dataSourceId)}/ontology-resolution`,
    );
  },

  /**
   * Run the gate against an ontology + arbitrary introspected stats.
   * Used by the wizard before data sources exist (the data-source path
   * has nothing to read yet).
   *
   * `stats` should match the ``GraphSchemaStats`` schema returned by
   * ``providerService.getAssetStats``.
   */
  async previewForOntology(
    ontologyId: string,
    stats: unknown,
  ): Promise<OntologyResolutionResponse> {
    return authFetch<OntologyResolutionResponse>(
      `/api/v1/admin/ontologies/${encodeURIComponent(ontologyId)}/resolution-check`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(stats),
      },
    );
  },
};
