"""Assignment compute endpoint.

Routes through the workspace-scoped ContextEngine (via get_context_engine)
so the ontology is resolved before any provider call that needs containment
edge types — fixing the intermittent ProviderConfigurationError that occurred
when the cache was cold and `_get_containment_edge_types()` was called before
`set_containment_edge_types()` had been injected by ontology resolution.
"""
from fastapi import APIRouter, Body, Depends, HTTPException

from backend.app.api.v1.endpoints.graph import get_context_engine, require_ws_manage
from backend.app.models.assignment import LayerAssignmentRequest, LayerAssignmentResult
from backend.app.services.assignment_engine import assignment_engine
from backend.app.services.context_engine import ContextEngine

router = APIRouter()


@router.post("/compute", response_model=LayerAssignmentResult)
async def compute_assignments(
    request: LayerAssignmentRequest = Body(..., embed=False),
    engine: ContextEngine = Depends(get_context_engine),
    _: object = Depends(require_ws_manage),
):
    """Compute layer assignments using the workspace-scoped ContextEngine.

    The previous implementation used the global singleton context_engine,
    which caused an intermittent ProviderConfigurationError: whenever the
    ontology cache was cold, `get_nodes()` internally called
    `_get_containment_edge_types()` before the ontology had been resolved
    and `set_containment_edge_types()` injected. The fix: pass the
    workspace-scoped engine (which resolves the ontology on first use) to
    `compute_assignments` so the provider is always warm before any query.
    """
    try:
        return await assignment_engine.compute_assignments(request, engine=engine)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
