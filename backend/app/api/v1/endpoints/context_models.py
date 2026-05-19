"""
Context Model endpoints.

Workspace-scoped: CRUD for context models (how to organize graph into logical flows).
Admin: CRUD for reusable Quick Start Templates.
"""
from typing import List, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import requires
from backend.app.db.engine import get_db_session
from backend.app.db.repositories import context_model_repo, view_repo
from backend.common.models.management import (
    ContextModelCreateRequest,
    ContextModelUpdateRequest,
    ContextModelResponse,
    InstantiateTemplateRequest,
    ViewResponse,
)

# ------------------------------------------------------------------ #
# Workspace-scoped router                                              #
# ------------------------------------------------------------------ #

router = APIRouter()

# The router-level dependency in api.py enforces
# ``workspace:datasource:read`` for every workspace context-model route;
# mutating routes additionally require ``workspace:datasource:manage``.
require_ws_manage = requires("workspace:datasource:manage", workspace="ws_id")


@router.get("", response_model=List[ContextModelResponse])
async def list_context_models(
    ws_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """List all context models for this workspace."""
    return await context_model_repo.list_context_models(session, workspace_id=ws_id)


@router.post("", response_model=ContextModelResponse, status_code=201)
async def create_context_model(
    ws_id: str = Path(...),
    req: ContextModelCreateRequest = Body(...),
    data_source_id: Optional[str] = Query(None, alias="dataSourceId"),
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_ws_manage),
):
    """Create (Save Blueprint) a context model for this workspace."""
    return await context_model_repo.create_context_model(
        session, req, workspace_id=ws_id, data_source_id=data_source_id
    )


@router.get("/{context_model_id}", response_model=ContextModelResponse)
async def get_context_model(
    ws_id: str = Path(...),
    context_model_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single context model."""
    cm = await context_model_repo.get_context_model(session, context_model_id)
    if not cm:
        raise HTTPException(status_code=404, detail=f"Context model '{context_model_id}' not found")
    return cm


@router.put("/{context_model_id}", response_model=ContextModelResponse)
async def update_context_model(
    ws_id: str = Path(...),
    context_model_id: str = Path(...),
    req: ContextModelUpdateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_ws_manage),
):
    """Update (Save Blueprint) an existing context model."""
    cm = await context_model_repo.update_context_model(session, context_model_id, req)
    if not cm:
        raise HTTPException(status_code=404, detail=f"Context model '{context_model_id}' not found")
    return cm


@router.delete("/{context_model_id}", status_code=204)
async def delete_context_model(
    ws_id: str = Path(...),
    context_model_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_ws_manage),
):
    """Delete a context model."""
    deleted = await context_model_repo.delete_context_model(session, context_model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Context model '{context_model_id}' not found")


@router.post("/instantiate", response_model=ContextModelResponse, status_code=201)
async def instantiate_template(
    ws_id: str = Path(...),
    req: InstantiateTemplateRequest = Body(...),
    data_source_id: Optional[str] = Query(None, alias="dataSourceId"),
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_ws_manage),
):
    """Create a workspace context model from a Quick Start Template."""
    cm = await context_model_repo.instantiate_template(
        session, req.template_id, ws_id, req.name, data_source_id=data_source_id
    )
    if not cm:
        raise HTTPException(status_code=404, detail=f"Template '{req.template_id}' not found")
    return cm


# ------------------------------------------------------------------ #
# Admin template router                                                #
# ------------------------------------------------------------------ #

template_router = APIRouter()


@template_router.get("", response_model=List[ContextModelResponse])
async def list_templates(
    category: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """List all Quick Start Templates."""
    models = await context_model_repo.list_context_models(session, templates_only=True)
    if category:
        models = [m for m in models if m.category == category]
    return models


@template_router.post("", response_model=ContextModelResponse, status_code=201)
async def create_template(
    req: ContextModelCreateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new Quick Start Template (global, no workspace)."""
    req.is_template = True
    return await context_model_repo.create_context_model(session, req)


@template_router.get("/{template_id}", response_model=ContextModelResponse)
async def get_template(
    template_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single template."""
    cm = await context_model_repo.get_context_model(session, template_id)
    if not cm or not cm.is_template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return cm


@template_router.put("/{template_id}", response_model=ContextModelResponse)
async def update_template(
    template_id: str = Path(...),
    req: ContextModelUpdateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a template."""
    cm = await context_model_repo.update_context_model(session, template_id, req)
    if not cm:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return cm


@template_router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a template."""
    deleted = await context_model_repo.delete_context_model(session, template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")


# ------------------------------------------------------------------ #
# Context Model → Views (1:N relationship)                             #
# ------------------------------------------------------------------ #

@router.get("/{context_model_id}/views", response_model=List[ViewResponse])
async def list_views_for_context_model(
    ws_id: str = Path(...),
    context_model_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """List all views referencing a given context model."""
    cm = await context_model_repo.get_context_model(session, context_model_id)
    if not cm:
        raise HTTPException(status_code=404, detail=f"Context model '{context_model_id}' not found")
    return await view_repo.list_views_for_context_model(session, context_model_id)
