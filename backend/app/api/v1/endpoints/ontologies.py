"""
Admin Ontology endpoints — CRUD for ontology definitions.
Ontologies are standalone, versioned, reusable semantic configurations.
Published ontologies are immutable; updates create new versions.
System ontologies (is_system=True) cannot be deleted.
"""
from typing import List, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.engine import get_db_session
from backend.app.db.repositories import ontology_definition_repo
from backend.app.ontology.adapters.sqlalchemy_repo import SQLAlchemyOntologyRepository
from backend.app.ontology.resolver import (
    parse_entity_definitions,
    parse_relationship_definitions,
    validate_ontology,
)
from backend.app.ontology.service import LocalOntologyService
from backend.common.models.management import (
    OntologyCreateRequest,
    OntologyUpdateRequest,
    OntologyDefinitionResponse,
    OntologyCoverageResponse,
    OntologyMatchResult,
    OntologyResolutionResponse,
    OntologyResolutionRelGap,
    OntologyResolutionHierarchyGap,
    OntologySuggestResponse,
    OntologyValidationIssue,
    OntologyValidationResponse,
    OntologyAuditEntry,
    OntologyImportRequest,
    OntologyImportResponse,
)
from backend.common.models.graph import GraphSchemaStats
from backend.app.ontology import gate as ontology_gate

router = APIRouter()


async def _invalidate_ontology_caches(
    session: AsyncSession, ontology_id: Optional[str] = None,
) -> None:
    """Eagerly invalidate any cached aggregation idempotency replays
    that pin to ``ontology_id``.

    The 60-minute idempotency replay in ``AggregationService.trigger``
    short-circuits when ``AggregationJobORM.ontology_fingerprint``
    matches the current ontology. After a successful PUT we proactively
    clear that fingerprint to NULL on prior jobs for this ontology so
    the very next trigger falls through to a fresh resolve. The
    trigger-time check remains the authoritative defense; this hook
    just makes invalidation explicit and observable.

    No-op when ``ontology_id`` is None (e.g. seeding) or when the
    aggregation schema isn't loaded (test contexts).
    """
    if not ontology_id:
        return None
    try:
        from backend.app.services.aggregation.models import AggregationJobORM
    except ImportError:
        return None
    from sqlalchemy import update

    await session.execute(
        update(AggregationJobORM)
        .where(AggregationJobORM.ontology_id == ontology_id)
        .where(AggregationJobORM.ontology_fingerprint.isnot(None))
        .values(ontology_fingerprint=None)
    )


@router.get("", response_model=List[OntologyDefinitionResponse])
async def list_ontologies(
    all_versions: bool = False,
    include_deleted: bool = Query(False, description="Include soft-deleted ontologies"),
    session: AsyncSession = Depends(get_db_session),
):
    """List ontologies. By default returns only the latest version of each."""
    if all_versions:
        return await ontology_definition_repo.list_ontologies(session, include_deleted=include_deleted)
    return await ontology_definition_repo.list_latest_ontologies(session, include_deleted=include_deleted)


@router.post("", response_model=OntologyDefinitionResponse, status_code=201)
async def create_ontology(
    req: OntologyCreateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new ontology (starts at version 1, unpublished)."""
    result = await ontology_definition_repo.create_ontology(session, req)
    await _invalidate_ontology_caches(session, getattr(result, "id", None))
    return result


@router.get("/{ontology_id}/versions", response_model=List[OntologyDefinitionResponse])
async def list_ontology_versions(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """List all versions of an ontology (grouped by schema_id)."""
    orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not orm:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    schema_id = getattr(orm, 'schema_id', None) or orm.id
    return await ontology_definition_repo.list_versions_by_schema(session, schema_id)


@router.get("/{ontology_id}", response_model=OntologyDefinitionResponse)
async def get_ontology(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a specific ontology by ID."""
    ontology = await ontology_definition_repo.get_ontology(session, ontology_id)
    if not ontology:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    return ontology


@router.get("/{ontology_id}/export")
async def export_ontology(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Export a full ontology definition as a downloadable JSON file.
    Returns the complete definition including entity types, relationship types,
    hierarchy, containment, lineage, and all metadata.
    """
    import json as _json
    from fastapi.responses import Response

    orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not orm:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")

    export_data = {
        "id": orm.id,
        "name": orm.name,
        "description": orm.description,
        "version": orm.version,
        "scope": orm.scope or "universal",
        "evolutionPolicy": getattr(orm, "evolution_policy", "reject") or "reject",
        "isPublished": orm.is_published,
        "isSystem": orm.is_system,
        "createdAt": str(orm.created_at) if orm.created_at else None,
        "updatedAt": str(orm.updated_at) if orm.updated_at else None,
        "entityTypeDefinitions": _json.loads(orm.entity_type_definitions or "{}"),
        "relationshipTypeDefinitions": _json.loads(orm.relationship_type_definitions or "{}"),
        "containmentEdgeTypes": _json.loads(orm.containment_edge_types or "[]"),
        "lineageEdgeTypes": _json.loads(orm.lineage_edge_types or "[]"),
        "edgeTypeMetadata": _json.loads(orm.edge_type_metadata or "{}"),
        "entityTypeHierarchy": _json.loads(orm.entity_type_hierarchy or "{}"),
        "rootEntityTypes": _json.loads(orm.root_entity_types or "[]"),
    }

    filename = f"{orm.name.replace(' ', '_')}_v{orm.version}.json"
    return Response(
        content=_json.dumps(export_data, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put("/{ontology_id}", response_model=OntologyDefinitionResponse)
async def update_ontology(
    ontology_id: str = Path(...),
    req: OntologyUpdateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Update an ontology. If published, creates a new version instead.
    Returns the updated or newly created ontology.
    """
    ontology = await ontology_definition_repo.update_ontology(session, ontology_id, req)
    if not ontology:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    # Invalidate idempotency replays pinned to either the source ID
    # (in-place update) or the freshly minted version ID (published →
    # new-version path inside ``update_ontology``).
    await _invalidate_ontology_caches(session, ontology_id)
    new_id = getattr(ontology, "id", None)
    if new_id and new_id != ontology_id:
        await _invalidate_ontology_caches(session, new_id)
    return ontology


@router.delete("/{ontology_id}", status_code=204)
async def delete_ontology(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete an ontology. Rejects if data sources still reference it or if it's a system ontology."""
    orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not orm:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    if orm.is_system:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete a system ontology. Use the factory reset endpoint to restore defaults.",
        )
    if await ontology_definition_repo.has_data_sources(session, ontology_id):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete ontology: one or more data sources still reference it.",
        )
    await ontology_definition_repo.delete_ontology(session, ontology_id)
    await _invalidate_ontology_caches(session, ontology_id)


@router.post("/{ontology_id}/restore", response_model=OntologyDefinitionResponse)
async def restore_ontology(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Restore a soft-deleted ontology."""
    restored = await ontology_definition_repo.restore_ontology(session, ontology_id)
    if not restored:
        raise HTTPException(status_code=404, detail=f"No deleted ontology '{ontology_id}' found to restore")
    await _invalidate_ontology_caches(session, ontology_id)
    return restored


@router.post("/{ontology_id}/publish", response_model=OntologyDefinitionResponse)
async def publish_ontology(
    ontology_id: str = Path(...),
    force: bool = Query(False, description="Bypass evolution_policy check (admin only)."),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Mark an ontology as published (immutable after this).

    Runs an impact check first. If the evolution_policy is 'reject' and the
    publish would remove existing types, the request is blocked with HTTP 409.
    Pass ?force=true to skip this guard (use with caution).
    """
    if not force:
        impact = await get_ontology_impact(ontology_id, session)
        if not impact["allowed"]:
            raise HTTPException(
                status_code=409,
                detail=impact["reason"],
            )

    ontology = await ontology_definition_repo.publish_ontology(session, ontology_id)
    if not ontology:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    await _invalidate_ontology_caches(session, ontology_id)
    return ontology


@router.post("/{ontology_id}/clone", response_model=OntologyDefinitionResponse, status_code=201)
async def clone_ontology(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Clone an existing ontology into a new editable draft.
    Useful for creating workspace-scoped customisations of the system default.
    """
    source = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not source:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")

    import json
    req = OntologyCreateRequest(
        name=f"{source.name} (copy)",
        version=1,
        scope="universal",
        containmentEdgeTypes=json.loads(source.containment_edge_types or "[]"),
        lineageEdgeTypes=json.loads(source.lineage_edge_types or "[]"),
        edgeTypeMetadata=json.loads(source.edge_type_metadata or "{}"),
        entityTypeHierarchy=json.loads(source.entity_type_hierarchy or "{}"),
        rootEntityTypes=json.loads(source.root_entity_types or "[]"),
        entityTypeDefinitions=json.loads(source.entity_type_definitions or "{}"),
        relationshipTypeDefinitions=json.loads(source.relationship_type_definitions or "{}"),
    )
    result = await ontology_definition_repo.create_ontology(session, req)
    await _invalidate_ontology_caches(session, getattr(result, "id", None))
    return result


@router.post("/{ontology_id}/new-version", response_model=OntologyDefinitionResponse, status_code=201)
async def create_new_version(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Create a new draft version of an existing ontology within the same schema lineage.

    The source ontology must be published or system. Copies all definitions into a new
    draft with version = max + 1 and the same schema_id. Returns 409 if a draft already
    exists for this schema (edit that draft instead).
    """
    source = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not source:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    if not source.is_published and not source.is_system:
        raise HTTPException(
            status_code=409,
            detail="Only published or system ontologies can spawn new versions. Edit the existing draft instead.",
        )
    schema_id = source.schema_id or source.id
    existing_draft = await ontology_definition_repo.get_draft_for_schema(session, schema_id)
    if existing_draft:
        raise HTTPException(
            status_code=409,
            detail=f"A draft version (v{existing_draft.version}) already exists for this schema. Edit it instead.",
        )
    result = await ontology_definition_repo.create_new_version_from_source(session, source)
    await _invalidate_ontology_caches(session, getattr(result, "id", None))
    return result


@router.post("/{ontology_id}/validate", response_model=OntologyValidationResponse)
async def validate_ontology_endpoint(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Validate an ontology's entity and relationship definitions.
    Checks for containment cycles, unknown type references, missing names.
    Returns a list of validation issues (errors and warnings).
    """
    orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not orm:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")

    import json
    entity_defs = parse_entity_definitions(json.loads(orm.entity_type_definitions or "{}"))
    rel_defs = parse_relationship_definitions(json.loads(orm.relationship_type_definitions or "{}"))
    issues = validate_ontology(entity_defs, rel_defs)

    return OntologyValidationResponse(
        isValid=not any(i.severity == "error" for i in issues),
        issues=[
            OntologyValidationIssue(
                severity=i.severity, code=i.code, message=i.message, affected=i.affected
            )
            for i in issues
        ],
    )


@router.post("/{ontology_id}/coverage", response_model=OntologyCoverageResponse)
async def get_ontology_coverage(
    ontology_id: str = Path(...),
    stats: GraphSchemaStats = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Analyse coverage of this ontology against a graph's schema stats.
    The caller provides GraphSchemaStats (from the /schema/stats endpoint).
    Returns which entity and relationship types are covered vs. uncovered.
    """
    repo = SQLAlchemyOntologyRepository(session)
    svc = LocalOntologyService(repo)
    report = await svc.check_coverage(ontology_id, stats)
    if report.coverage_percent == 0.0 and not report.covered_entity_types:
        orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
        if not orm:
            raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")

    return OntologyCoverageResponse(
        coveragePercent=report.coverage_percent,
        coveredEntityTypes=report.covered_entity_types,
        uncoveredEntityTypes=report.uncovered_entity_types,
        extraEntityTypes=report.extra_entity_types,
        coveredRelationshipTypes=report.covered_relationship_types,
        uncoveredRelationshipTypes=report.uncovered_relationship_types,
    )


@router.post("/{ontology_id}/resolution-check", response_model=OntologyResolutionResponse)
async def check_ontology_resolution(
    ontology_id: str = Path(...),
    stats: GraphSchemaStats = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Run the ontology-resolution gate against an arbitrary set of
    introspected graph stats.

    Used by the AssetOnboardingWizard SchemaReviewStep before any data
    source has been created. The data-source-keyed counterpart
    (``GET /admin/data-sources/{ds_id}/ontology-resolution``) reuses the
    same gate against the cached stats already attached to the data
    source.
    """
    import json as _json

    orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not orm:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")

    introspected_entity_ids = [s.id for s in stats.entity_type_stats if getattr(s, "id", None)]
    introspected_edge_ids = [s.id for s in stats.edge_type_stats if getattr(s, "id", None)]

    report = ontology_gate.check_resolution(
        ontology_id=orm.id,
        ontology_version=orm.version,
        ontology_is_published=bool(orm.is_published),
        ontology_revision=getattr(orm, "revision", 0) or 0,
        entity_type_definitions_raw=_json.loads(orm.entity_type_definitions or "{}"),
        relationship_type_definitions_raw=_json.loads(orm.relationship_type_definitions or "{}"),
        introspected_entity_ids=introspected_entity_ids,
        introspected_edge_ids=introspected_edge_ids,
    )

    return OntologyResolutionResponse(
        resolved=report.resolved,
        ontologyId=report.ontology_id,
        ontologyVersion=report.ontology_version,
        ontologyIsPublished=report.ontology_is_published,
        missingEntityTypes=report.missing_entity_types,
        missingEdgeTypes=report.missing_edge_types,
        unclassifiedRelationships=[
            OntologyResolutionRelGap(
                id=g.id,
                name=g.name,
                isContainment=g.is_containment,
                isLineage=g.is_lineage,
            )
            for g in report.unclassified_relationships
        ],
        hasLineage=report.has_lineage,
        hasContainment=report.has_containment,
        hierarchyWarnings=[
            OntologyResolutionHierarchyGap(
                entityType=g.entity_type,
                missingField=g.missing_field,
            )
            for g in report.hierarchy_warnings
        ],
        advisoryWarnings=report.advisory_warnings,
        blockingReasons=report.blocking_reasons,
        fingerprint=report.fingerprint,
    )


@router.get("/{ontology_id}/impact", response_model=dict)
async def get_ontology_impact(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Simulate the impact of publishing this ontology version.

    Compares the draft to the previously published version of the same ontology
    name and returns:
    - added entity / relationship types
    - removed entity / relationship types
    - changed definitions
    - whether publishing is allowed given the evolution_policy
    - the reason if it is blocked

    A 200 response does NOT publish — call /{id}/publish to commit.
    """
    import json

    draft_row = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not draft_row:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    if draft_row.is_published:
        raise HTTPException(status_code=409, detail="Ontology is already published.")

    # Find the latest published version of the same ontology (by schema_id)
    from sqlalchemy import select
    from backend.app.db.models import OntologyORM
    schema_id = getattr(draft_row, 'schema_id', None) or draft_row.id
    result = await session.execute(
        select(OntologyORM)
        .where(OntologyORM.schema_id == schema_id)
        .where(OntologyORM.is_published == True)  # noqa: E712
        .order_by(OntologyORM.version.desc())
        .limit(1)
    )
    prev_row = result.scalar_one_or_none()

    draft_entities = set(json.loads(draft_row.entity_type_definitions or "{}").keys())
    draft_rels = set(json.loads(draft_row.relationship_type_definitions or "{}").keys())

    if prev_row is None:
        # First publish — no breaking changes possible
        return {
            "allowed": True,
            "reason": None,
            "addedEntityTypes": sorted(draft_entities),
            "removedEntityTypes": [],
            "addedRelationshipTypes": sorted(draft_rels),
            "removedRelationshipTypes": [],
        }

    prev_entities = set(json.loads(prev_row.entity_type_definitions or "{}").keys())
    prev_rels = set(json.loads(prev_row.relationship_type_definitions or "{}").keys())

    removed_entities = sorted(prev_entities - draft_entities)
    removed_rels = sorted(prev_rels - draft_rels)
    has_breaking = bool(removed_entities or removed_rels)

    policy = getattr(draft_row, "evolution_policy", "reject") or "reject"
    allowed = True
    reason = None

    if has_breaking and policy == "reject":
        allowed = False
        reason = (
            f"Evolution policy is 'reject' and publishing would remove "
            f"{len(removed_entities)} entity type(s) and "
            f"{len(removed_rels)} relationship type(s). "
            "Change the evolution_policy to 'deprecate' or 'migrate', "
            "or restore the removed types."
        )

    return {
        "allowed": allowed,
        "reason": reason,
        "evolutionPolicy": policy,
        "addedEntityTypes": sorted(draft_entities - prev_entities),
        "removedEntityTypes": removed_entities,
        "addedRelationshipTypes": sorted(draft_rels - prev_rels),
        "removedRelationshipTypes": removed_rels,
        # EXTENSION POINT: include per-field TypeDiff and affected data sources/views
        # when publish-confirmation UX needs richer blast-radius detail.
    }


@router.get("/{ontology_id}/assignments")
async def get_ontology_assignments(
    ontology_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    List all data sources (across all workspaces) currently assigned to this ontology.
    Returns [{workspaceId, workspaceName, dataSourceId, dataSourceLabel}].
    """
    row = await ontology_definition_repo.get_ontology(session, ontology_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    return await ontology_definition_repo.get_assignments(session, ontology_id)


@router.get("/{ontology_id}/audit", response_model=List[OntologyAuditEntry])
async def get_ontology_audit_log(
    ontology_id: str = Path(...),
    action: Optional[str] = Query(None, description="Filter by action type (created, updated, published, deleted, restored, cloned)"),
    limit: int = Query(100, ge=1, le=500, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Return the audit trail for an ontology (all versions sharing the same schema_id).
    Includes create, update, publish, delete, restore, and clone events with
    change diffs and actor information. Paginated, newest first.
    """
    orm = await ontology_definition_repo.get_ontology_orm(session, ontology_id)
    if not orm:
        raise HTTPException(status_code=404, detail=f"Ontology '{ontology_id}' not found")
    schema_id = getattr(orm, "schema_id", None) or orm.id
    return await ontology_definition_repo.get_audit_log(
        session, schema_id, action=action, limit=limit, offset=offset,
    )


@router.post("/import", response_model=OntologyImportResponse, status_code=200)
async def import_ontology_new(
    req: OntologyImportRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Import a semantic layer from exported JSON, creating a new draft.
    Validates the JSON structure against the export format.
    """
    try:
        result = await ontology_definition_repo.import_ontology(session, req, target_id=None)
        await _invalidate_ontology_caches(session, getattr(result, "ontology_id", None))
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{ontology_id}/import", response_model=OntologyImportResponse, status_code=200)
async def import_ontology_into(
    ontology_id: str = Path(...),
    req: OntologyImportRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Import a semantic layer from exported JSON into an existing ontology.

    Behavior:
    - Draft target → updates in-place (same version), records audit trail.
    - Published target → creates a new draft version with the imported changes.
    - Deleted target → rejected (restore first).
    - System target → rejected (clone first).
    - No changes detected → returns status="no_changes" without modification.
    """
    try:
        result = await ontology_definition_repo.import_ontology(session, req, target_id=ontology_id)
        await _invalidate_ontology_caches(session, ontology_id)
        target_after = getattr(result, "ontology_id", None)
        if target_after and target_after != ontology_id:
            await _invalidate_ontology_caches(session, target_after)
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/suggest", response_model=OntologySuggestResponse, status_code=200)
async def suggest_ontology(
    stats: GraphSchemaStats = Body(...),
    base_ontology_id: Optional[str] = None,
    session: AsyncSession = Depends(get_db_session),
):
    """
    Suggest an ontology definition from graph schema stats.
    If base_ontology_id is provided, extends that ontology with new types found in the graph.
    The result includes a draft OntologyCreateRequest and matching existing ontologies.
    Call POST /admin/ontologies to save the suggestion.
    """
    from backend.app.registry.provider_registry import provider_registry

    repo = SQLAlchemyOntologyRepository(session)
    svc = LocalOntologyService(repo)

    # We need OntologyMetadata for the suggest call — build a minimal one from stats
    from backend.common.models.graph import OntologyMetadata
    introspected = OntologyMetadata(
        containmentEdgeTypes=[],
        lineageEdgeTypes=[],
        edgeTypeMetadata={},
        entityTypeHierarchy={},
        rootEntityTypes=[],
    )

    suggestion = await svc.suggest_from_introspection(
        introspected_stats=stats,
        introspected_ontology=introspected,
        base_ontology_id=base_ontology_id,
    )

    # Find matching existing ontologies
    graph_entity_ids = {s.id for s in stats.entity_type_stats}
    graph_rel_ids = {s.id.upper() for s in stats.edge_type_stats}
    graph_types = graph_entity_ids | graph_rel_ids

    matches = []
    if graph_types:
        all_ontologies = await ontology_definition_repo.list_latest_ontologies(session)
        for ont in all_ontologies:
            ont_entity_ids = set((ont.entity_type_definitions or {}).keys())
            ont_rel_ids = set((ont.relationship_type_definitions or {}).keys())
            ont_types = ont_entity_ids | ont_rel_ids

            intersection = graph_types & ont_types
            union = graph_types | ont_types
            jaccard = len(intersection) / len(union) if union else 0.0

            if jaccard > 0.1:
                matches.append(OntologyMatchResult(
                    ontologyId=ont.id,
                    ontologyName=ont.name,
                    version=ont.version,
                    jaccardScore=round(jaccard, 3),
                    coveredEntityTypes=sorted(graph_entity_ids & ont_entity_ids),
                    uncoveredEntityTypes=sorted(graph_entity_ids - ont_entity_ids),
                    coveredRelationshipTypes=sorted(graph_rel_ids & ont_rel_ids),
                    uncoveredRelationshipTypes=sorted(graph_rel_ids - ont_rel_ids),
                    totalEntityTypes=len(ont_entity_ids),
                    totalRelationshipTypes=len(ont_rel_ids),
                ))

        matches.sort(key=lambda m: m.jaccard_score, reverse=True)

    return OntologySuggestResponse(
        suggested=suggestion,
        matchingOntologies=matches[:5],
    )
