"""
Workspace-scoped asset endpoints: assignment rule-sets.
Mounted at /v1/{ws_id}/assets/

Note: View endpoints have been consolidated into context_models.view_router
mounted at /api/v1/views.
"""
from typing import List
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import requires
from backend.app.db.engine import get_db_session
from backend.app.db.repositories import workspace_repo, assignment_repo
from backend.common.models.management import (
    RuleSetCreateRequest,
    RuleSetResponse,
)

router = APIRouter()

# The router-level dependency in api.py enforces
# ``workspace:datasource:read`` for every assets route; mutating routes
# additionally require ``workspace:datasource:manage``.
require_ws_manage = requires("workspace:datasource:manage", workspace="ws_id")


async def _require_workspace(session: AsyncSession, ws_id: str) -> None:
    if not await workspace_repo.get_workspace(session, ws_id):
        raise HTTPException(status_code=404, detail=f"Workspace '{ws_id}' not found")


# ------------------------------------------------------------------ #
# Assignment Rule Sets                                                 #
# ------------------------------------------------------------------ #

@router.get("/rule-sets", response_model=List[RuleSetResponse])
async def list_rule_sets(
    ws_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """List all assignment rule sets for this workspace."""
    await _require_workspace(session, ws_id)
    return await assignment_repo.list_rule_sets_by_workspace(session, ws_id)


@router.post("/rule-sets", response_model=RuleSetResponse, status_code=201)
async def create_rule_set(
    ws_id: str = Path(...),
    req: RuleSetCreateRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_ws_manage),
):
    """Create a new assignment rule set for this workspace."""
    await _require_workspace(session, ws_id)
    return await assignment_repo.create_rule_set_for_workspace(session, ws_id, req)


@router.get("/rule-sets/{rule_set_id}", response_model=RuleSetResponse)
async def get_rule_set(
    ws_id: str = Path(...),
    rule_set_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single assignment rule set."""
    await _require_workspace(session, ws_id)
    rs = await assignment_repo.get_rule_set(session, rule_set_id)
    if not rs:
        raise HTTPException(status_code=404, detail=f"Rule set '{rule_set_id}' not found")
    return rs


@router.delete("/rule-sets/{rule_set_id}", status_code=204)
async def delete_rule_set(
    ws_id: str = Path(...),
    rule_set_id: str = Path(...),
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_ws_manage),
):
    """Delete an assignment rule set."""
    await _require_workspace(session, ws_id)
    deleted = await assignment_repo.delete_rule_set(session, rule_set_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Rule set '{rule_set_id}' not found")
