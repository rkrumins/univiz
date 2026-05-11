"""PROFILE every trace hot-path query and assert no AllNodeScan operator.

CI gate per the skeleton-first trace acceptance criteria: an AllNodeScan
anywhere on the trace path means the planner is missing an index — that's
the "1 second" trip-wire from the Golden Rule. This script runs the
canonical queries through `PROFILE` and fails the build if any plan
contains an AllNodeScan node.

Usage:
    python backend/scripts/check_trace_query_plans.py --workspace dev

Connection params come from FALKORDB_HOST / FALKORDB_PORT env (or via
the workspace's data source config when --workspace is given).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List, Tuple


# Canonical hot-path queries. The `params` block exercises each planner
# path on representative shapes — the values are placeholders but the
# planner's choice of index seek vs node scan is structural, not data-
# dependent, so the assertion stays meaningful.
HOT_PATH_QUERIES: List[Tuple[str, str, Dict[str, Any]]] = [
    (
        "trace_at_level_skeleton_aggregated",
        # AGGREGATED expansion via the level-pair composite index. This is
        # the single most-executed query in a trace. AllNodeScan here =
        # the index didn't apply = catastrophic.
        "UNWIND $frontier AS u "
        "MATCH (f {urn: u})-[r:AGGREGATED]->(other) "
        "WHERE r.sourceLevel = $level AND r.targetLevel = $level "
        "RETURN f.urn, other.urn, r.weight LIMIT $limit",
        {"frontier": ["urn:test:domain:0"], "level": 0, "limit": 100},
    ),
    (
        "resolve_root_anchor",
        "MATCH (focus {urn: $urn}) "
        "OPTIONAL MATCH path = (focus)<-[c*0..10]-(anc) "
        "WHERE ALL(rel IN c WHERE type(rel) IN $ctypes) "
        "RETURN COALESCE(anc.urn, focus.urn) AS urn LIMIT 1",
        {"urn": "urn:test:field:0", "ctypes": ["CONTAINS"]},
    ),
    (
        "containment_ancestors_per_level",
        # Per-level loop replacement for the variable-length walk.
        "MATCH (c {urn: $urn})<-[r:CONTAINS]-(p) "
        "WHERE p.level = $level RETURN p.urn LIMIT 1",
        {"urn": "urn:test:column:0", "level": 2},
    ),
    (
        "top_level_skeleton",
        # The level index seek that powers `level=0` skeleton trace.
        "MATCH (n) WHERE n.level = $level RETURN n.urn LIMIT $limit",
        {"level": 0, "limit": 50},
    ),
]


async def check_plan(
    db: Any, name: str, cypher: str, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """Run PROFILE and return (passed, summary)."""
    try:
        result = await db.query(f"PROFILE {cypher}", params)
    except Exception as exc:
        # PROFILE itself shouldn't fail on a syntactically valid query.
        # If it does, that's a separate kind of breakage we want to know
        # about — fail closed.
        return False, f"{name}: PROFILE execution failed: {exc}"

    plan_text = ""
    if hasattr(result, "result_set"):
        plan_text = "\n".join(str(row) for row in (result.result_set or []))
    elif hasattr(result, "plan"):
        plan_text = str(result.plan)
    else:
        plan_text = str(result)

    if "AllNodeScan" in plan_text or "All Node Scan" in plan_text:
        return False, (
            f"{name}: AllNodeScan detected — planner is not using an index.\n"
            f"  Cypher: {cypher}\n"
            f"  Plan:\n{plan_text}"
        )
    return True, f"{name}: OK"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.getenv("WORKSPACE_ID", "dev"))
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on first failure (default: report all and exit).")
    args = parser.parse_args()

    # Lazy import — keep the module importable in environments that don't
    # have the full backend available (e.g. lint).
    from backend.app.providers.manager import provider_manager
    from backend.app.providers.falkordb_provider import FalkorDBProvider

    provider = provider_manager.get_active_provider(args.workspace)
    if not isinstance(provider, FalkorDBProvider):
        print(
            f"check_trace_query_plans: workspace {args.workspace} is not "
            f"FalkorDB-backed ({type(provider).__name__}); skipping.",
            file=sys.stderr,
        )
        return 0

    await provider._ensure_connected()

    failures: List[str] = []
    for name, cypher, params in HOT_PATH_QUERIES:
        ok, msg = await check_plan(provider._graph, name, cypher, params)
        print(msg)
        if not ok:
            failures.append(msg)
            if args.strict:
                break

    if failures:
        print(
            f"\n{len(failures)} trace hot-path queries have AllNodeScan. "
            f"Fix the indices or rewrite the Cypher.",
            file=sys.stderr,
        )
        return 1

    print(f"\nAll {len(HOT_PATH_QUERIES)} trace hot-path queries pass — zero AllNodeScans.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
