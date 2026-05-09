"""
Graph Service provider discovery and connectivity-test endpoints.

This service is **stateless** — it accepts connection params in the request
body rather than reading from the management DB.  Intended for pre-registration
testing (e.g. "does this FalkorDB host exist?") and capability discovery.
"""
import asyncio
import time
from typing import List, Optional
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

router = APIRouter()


# ------------------------------------------------------------------ #
# Provider capability model                                            #
# ------------------------------------------------------------------ #

class ProviderCapabilities(BaseModel):
    name: str
    displayName: str
    supportsMultiGraph: bool
    supportsLineage: bool
    supportsContainment: bool
    supportsWriteBack: bool
    defaultPort: Optional[int] = None


# ------------------------------------------------------------------ #
# Provider catalogue                                                   #
# ------------------------------------------------------------------ #

_PROVIDERS: List[ProviderCapabilities] = [
    ProviderCapabilities(
        name="falkordb",
        displayName="FalkorDB",
        supportsMultiGraph=True,
        supportsLineage=True,
        supportsContainment=True,
        supportsWriteBack=True,
        defaultPort=6379,
    ),
    ProviderCapabilities(
        name="neo4j",
        displayName="Neo4j",
        supportsMultiGraph=True,
        supportsLineage=True,
        supportsContainment=True,
        supportsWriteBack=False,  # read-only in Phase 4
        defaultPort=7687,
    ),
    ProviderCapabilities(
        name="datahub",
        displayName="DataHub",
        supportsMultiGraph=False,
        supportsLineage=True,
        supportsContainment=False,
        supportsWriteBack=False,
        defaultPort=None,
    ),
    ProviderCapabilities(
        name="spanner",
        displayName="Google Spanner Graph",
        supportsMultiGraph=True,        # one Spanner DB can host multiple PROPERTY GRAPHs
        supportsLineage=True,
        supportsContainment=True,
        supportsWriteBack=True,
        defaultPort=None,               # managed gRPC endpoint; project/instance/database addressing
    ),
]


@router.get("", response_model=List[ProviderCapabilities])
async def list_providers():
    """Return the list of supported provider types and their capabilities."""
    return _PROVIDERS


# ------------------------------------------------------------------ #
# FalkorDB                                                            #
# ------------------------------------------------------------------ #

class FalkorDBPingRequest(BaseModel):
    host: str
    port: int = 6379
    graph_name: str = "nexus_lineage"


@router.post("/falkordb/ping")
async def ping_falkordb(req: FalkorDBPingRequest = Body(...)):
    """
    Test connectivity to a FalkorDB instance without registering a connection.
    Returns latency and graph count on success.
    """
    from backend.app.providers.falkordb_provider import FalkorDBProvider
    provider = FalkorDBProvider(host=req.host, port=req.port, graph_name=req.graph_name)
    try:
        t0 = time.perf_counter()
        await asyncio.wait_for(provider.get_stats(), timeout=10)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {"status": "healthy", "latencyMs": latency_ms}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="FalkorDB ping timed out")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FalkorDB ping failed: {exc}")


@router.post("/falkordb/graphs")
async def list_falkordb_graphs(req: FalkorDBPingRequest = Body(...)):
    """
    List named graph keys on a FalkorDB instance without registering a connection.
    Uses GRAPH.LIST internally.
    """
    from backend.app.providers.falkordb_provider import FalkorDBProvider
    provider = FalkorDBProvider(host=req.host, port=req.port, graph_name=req.graph_name)
    try:
        graphs = await asyncio.wait_for(provider.list_graphs(), timeout=10)
        return {"graphs": graphs}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="FalkorDB timed out while listing graphs")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list FalkorDB graphs: {exc}")


# ------------------------------------------------------------------ #
# Neo4j                                                               #
# ------------------------------------------------------------------ #

class Neo4jPingRequest(BaseModel):
    host: str
    port: int = 7687
    username: str = "neo4j"
    password: str = ""
    database: str = "neo4j"
    tls_enabled: bool = False


@router.post("/neo4j/ping")
async def ping_neo4j(req: Neo4jPingRequest = Body(...)):
    """
    Test connectivity to a Neo4j instance without registering a connection.
    Runs ``RETURN 1`` via Bolt.
    """
    from backend.graph.adapters.neo4j_provider import Neo4jProvider
    scheme = "bolt+s" if req.tls_enabled else "bolt"
    provider = Neo4jProvider(
        uri=f"{scheme}://{req.host}:{req.port}",
        username=req.username,
        password=req.password,
        database=req.database,
    )
    try:
        t0 = time.perf_counter()
        await asyncio.wait_for(provider.get_stats(), timeout=10)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {"status": "healthy", "latencyMs": latency_ms}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Neo4j ping timed out")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Neo4j ping failed: {exc}")
    finally:
        await provider.close()


@router.post("/neo4j/databases")
async def list_neo4j_databases(req: Neo4jPingRequest = Body(...)):
    """
    List available Neo4j databases on an instance without registering a connection.
    Uses ``SHOW DATABASES`` on the system DB.
    """
    from backend.graph.adapters.neo4j_provider import Neo4jProvider
    scheme = "bolt+s" if req.tls_enabled else "bolt"
    provider = Neo4jProvider(
        uri=f"{scheme}://{req.host}:{req.port}",
        username=req.username,
        password=req.password,
        database=req.database,
    )
    try:
        databases = await asyncio.wait_for(provider.list_graphs(), timeout=10)
        return {"databases": databases}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Neo4j timed out while listing databases")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list Neo4j databases: {exc}")
    finally:
        await provider.close()


# ------------------------------------------------------------------ #
# DataHub                                                             #
# ------------------------------------------------------------------ #

class DataHubPingRequest(BaseModel):
    base_url: str
    token: Optional[str] = None


@router.post("/datahub/ping")
async def ping_datahub(req: DataHubPingRequest = Body(...)):
    """
    Test connectivity to a DataHub instance without registering a connection.
    Calls the ``{ health { status } }`` GraphQL query.
    """
    from backend.graph.adapters.datahub_provider import DataHubGraphQLProvider
    provider = DataHubGraphQLProvider(base_url=req.base_url, token=req.token)
    try:
        t0 = time.perf_counter()
        result = await asyncio.wait_for(provider.get_stats(), timeout=10)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {"status": result.get("status", "UNKNOWN"), "latencyMs": latency_ms}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="DataHub ping timed out")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"DataHub ping failed: {exc}")
    finally:
        await provider.close()


# ------------------------------------------------------------------ #
# Google Spanner Graph                                                #
# ------------------------------------------------------------------ #

class SpannerPingRequest(BaseModel):
    project_id: str
    instance_id: str
    database_id: str
    graph_name: str = "UniViz"
    use_emulator: bool = False
    service_account_json: Optional[str] = None


@router.post("/spanner/ping")
async def ping_spanner(req: SpannerPingRequest = Body(...)):
    """Test connectivity to a Spanner instance without registering a connection.

    Calls ``preflight`` which performs a single Instance metadata RPC; this
    avoids running schema bootstrap or executing GQL, so the probe stays
    cheap and survives instances that do not (yet) have the property graph.
    Returns the latency on success.
    """
    from backend.graph.adapters.spanner_provider import SpannerProvider, SpannerEditionError
    provider = SpannerProvider(
        project_id=req.project_id,
        instance_id=req.instance_id,
        database_id=req.database_id,
        graph_name=req.graph_name,
        use_emulator=req.use_emulator,
        credentials_json=req.service_account_json,
    )
    try:
        t0 = time.perf_counter()
        result = await asyncio.wait_for(provider.preflight(deadline_s=5.0), timeout=10)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not getattr(result, "ok", False):
            # PreflightResult exposes ``reason`` (canonical contract in
            # backend/common/interfaces/preflight.py). The earlier code
            # read ``.detail`` which doesn't exist on the canonical type;
            # failed pings would surface "unknown" instead of the real
            # reason code.
            reason = getattr(result, "reason", None) or getattr(result, "detail", "unknown")
            raise HTTPException(status_code=502, detail=f"Spanner ping failed: {reason}")
        return {"status": "healthy", "latencyMs": latency_ms}
    except SpannerEditionError as exc:
        # Connection works but the instance is on the wrong edition; surface
        # this as 400 so the wizard can render the dedicated error card.
        raise HTTPException(status_code=400, detail=f"Spanner edition error: {exc}")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Spanner ping timed out")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spanner ping failed: {exc}")
    finally:
        await provider.close()


@router.post("/spanner/databases")
async def list_spanner_databases(req: SpannerPingRequest = Body(...)):
    """List property graphs in the target Spanner database.

    On Spanner the analogue of FalkorDB's GRAPH.LIST / Neo4j's SHOW DATABASES
    is ``INFORMATION_SCHEMA.PROPERTY_GRAPHS``. Returns an empty list when the
    database is reachable but no property graph exists yet (e.g. the wizard
    is connecting before bootstrap), so the UI can offer to create one.
    """
    from backend.graph.adapters.spanner_provider import SpannerProvider, SpannerEditionError
    provider = SpannerProvider(
        project_id=req.project_id,
        instance_id=req.instance_id,
        database_id=req.database_id,
        graph_name=req.graph_name,
        use_emulator=req.use_emulator,
        credentials_json=req.service_account_json,
    )
    try:
        graphs = await asyncio.wait_for(provider.list_graphs(), timeout=15)
        return {"databases": graphs}
    except SpannerEditionError as exc:
        raise HTTPException(status_code=400, detail=f"Spanner edition error: {exc}")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Spanner timed out while listing property graphs")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list Spanner property graphs: {exc}")
    finally:
        await provider.close()
