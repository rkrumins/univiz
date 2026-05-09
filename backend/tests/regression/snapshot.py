"""Provider-contract snapshot helpers.

Used by the FalkorDB and Neo4j regression suites to pin behaviour
before Phase B/C reshape onto the shared base.

Workflow
--------
* First run with ``UPDATE_PROVIDER_SNAPSHOTS=1`` writes the captured
  JSON to ``backend/tests/regression/snapshots/<provider>/<name>.json``.
* Subsequent runs compare against the snapshot and fail on diff.
* The reshape is approved only when the suite passes byte-identically.

Determinism
-----------
* GraphNode/GraphEdge ordering is stabilised on URN / id before
  serialisation so insertion-order or query-plan jitter cannot fail
  the diff.
* Sets are converted to sorted lists.
* TraceResult / LineageResult node + edge collections are sorted before
  comparison.
"""
from __future__ import annotations

import json
import os
from dataclasses import is_dataclass, asdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _snapshot_root(provider: str) -> Path:
    here = Path(__file__).resolve().parent
    target = here / "snapshots" / provider
    target.mkdir(parents=True, exist_ok=True)
    return target


def _stabilize(value: Any) -> Any:
    """Sort lists of dicts on a stable key, walk recursively."""
    if isinstance(value, dict):
        return {k: _stabilize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        items = [_stabilize(v) for v in value]
        # When every element is a dict that has a stable id, sort on it.
        for sort_key in ("urn", "id", "source_urn", "name"):
            if items and all(isinstance(i, dict) and sort_key in i for i in items):
                items = sorted(items, key=lambda d: str(d[sort_key]))
                break
        return items
    if isinstance(value, set):
        return sorted(_stabilize(v) for v in value)
    return value


def _to_jsonable(value: Any) -> Any:
    """Convert pydantic / dataclass / set / GraphNode etc. into plain JSON."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        # by_alias=False so Python field names appear; deterministic.
        return _to_jsonable(value.model_dump(by_alias=False))
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_to_jsonable(v) for v in value)
    # Fallback: stringify (covers e.g. enum values, datetimes).
    return str(value)


def _normalise(value: Any) -> Any:
    return _stabilize(_to_jsonable(value))


def assert_snapshot(
    *,
    provider: str,
    name: str,
    actual: Any,
) -> None:
    """Compare ``actual`` to the stored snapshot, or capture if missing.

    Set ``UPDATE_PROVIDER_SNAPSHOTS=1`` to force a re-capture of every
    snapshot the suite touches in one pass.
    """
    path = _snapshot_root(provider) / f"{name}.json"
    payload = _normalise(actual)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str)

    if os.getenv("UPDATE_PROVIDER_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(encoded + "\n", encoding="utf-8")
        return

    expected_text = path.read_text(encoding="utf-8").rstrip("\n")
    actual_text = encoded
    if expected_text != actual_text:
        raise AssertionError(
            f"Snapshot mismatch for {provider}/{name}.\n"
            f"  Snapshot path: {path}\n"
            f"  To re-capture: UPDATE_PROVIDER_SNAPSHOTS=1 pytest <this-file> -k {name}\n"
            f"  Actual:\n{actual_text}\n"
            f"  Expected:\n{expected_text}\n"
        )
