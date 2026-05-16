"""Post-run SLO assertions against Locust's CSV output.

Locust writes per-endpoint stats to ``<prefix>_stats.csv`` when invoked
with ``--csv <prefix>``. This module parses that file and checks the
numbers against a set of thresholds defined in the perf plan, exiting
non-zero on violation so CI can fail the build.

The default thresholds match the plan's end-to-end verification gate:

* Aggregate ``p95 < 500 ms``
* Per-endpoint ``views:list p95 < 150 ms`` (WS-1 success gate)
* ``cached-stats:get p95 < 300 ms`` (WS-2 success gate)
* Failure rate ``< 0.1%`` across the run (no 5xx storm)

Run as a standalone script::

    python -m lib.slo results/run_stats.csv
    # exit code 0 = all SLOs met; 1 = at least one violation

For short local smoke runs against a dev backend, pass ``--smoke``:

    python -m lib.slo --smoke results/smoke/views/run_stats.csv

Smoke mode uses relaxed thresholds (a single slow request shouldn't blow
p95) and skips SLOs for endpoints that weren't exercised by the run (so
per-scenario smoke runs don't fail on the absent endpoints from other
scenarios).

Or import :func:`assert_slos` from a test harness.
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SLO:
    """A single performance SLO.

    ``name`` matches the Locust request name (e.g. ``"views:list"``);
    use ``"Aggregated"`` to assert against the run-wide totals row.
    A value of ``None`` skips that check.
    """

    name: str
    p95_ms_max: Optional[float] = None
    p99_ms_max: Optional[float] = None
    failure_rate_max: Optional[float] = None  # 0.0–1.0
    min_request_count: Optional[int] = None


# Default SLOs from the plan. Override via JSON config later if needed
# (keep this minimal — the operator can edit this list when targets shift).
DEFAULT_SLOS: List[SLO] = [
    SLO(name="Aggregated", p95_ms_max=500.0, failure_rate_max=0.001),
    SLO(name="views:list", p95_ms_max=150.0, min_request_count=100),
    SLO(name="views:popular", p95_ms_max=150.0),
    SLO(name="cached-stats:get", p95_ms_max=300.0),
    SLO(name="announcements:list", p95_ms_max=100.0),
]

# Relaxed thresholds for short smoke runs (~20–30 s at low concurrency
# against a cold dev backend). The intent is "did each scenario do real
# work and not 5xx-storm" — not "does this build meet the perf plan's
# production targets". Failure-rate ceiling is the load-bearing check;
# p95 is loose enough that a single slow request doesn't trip it.
SMOKE_SLOS: List[SLO] = [
    SLO(name="Aggregated", p95_ms_max=2000.0, failure_rate_max=0.05),
    SLO(name="views:list", p95_ms_max=1000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="views:popular", p95_ms_max=1000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="cached-stats:get", p95_ms_max=1500.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="announcements:list", p95_ms_max=1000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="aggregation-jobs:list", p95_ms_max=2000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="graph-schema:get", p95_ms_max=2000.0, min_request_count=1, failure_rate_max=0.05),
    # Graph traversal endpoints — FalkorDB-bound, more variance than the
    # PG-bound endpoints above. p95 ceilings are deliberately loose for
    # smoke runs; the tiered SLOs are where real gating happens.
    SLO(name="graph-trace:v2", p95_ms_max=5000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="graph-ancestors:get", p95_ms_max=3000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="graph-descendants:get", p95_ms_max=3000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="graph-children:get", p95_ms_max=2000.0, min_request_count=1, failure_rate_max=0.05),
    SLO(name="graph-children-edges:get", p95_ms_max=3000.0, min_request_count=1, failure_rate_max=0.05),
]

# Tier-aware SLOs for the concurrency sweep (`make sweep`). The
# thresholds scale with user count so the gate encodes a realistic
# degradation curve: the same endpoint can be 150 ms at 10 users and
# 800 ms at 1000 users and still be considered "scaling acceptably".
# Failure-rate ceiling rises gently — a 0.1% rate at 10 users is the
# perf plan's target, but at 1000 users a 1% rate is more honest about
# the long tail of pool exhaustion / transient backpressure.
#
# These are STARTING POINTS, not gospel. Re-tune from your own
# baseline once you have CSVs at each tier.
TIER_SLOS: Dict[int, List[SLO]] = {
    10: [
        SLO(name="Aggregated", p95_ms_max=500.0, failure_rate_max=0.001),
        SLO(name="views:list", p95_ms_max=150.0, failure_rate_max=0.01),
        SLO(name="views:popular", p95_ms_max=150.0, failure_rate_max=0.01),
        SLO(name="cached-stats:get", p95_ms_max=300.0, failure_rate_max=0.01),
        SLO(name="announcements:list", p95_ms_max=100.0, failure_rate_max=0.01),
        SLO(name="aggregation-jobs:list", p95_ms_max=500.0, failure_rate_max=0.01),
        SLO(name="graph-schema:get", p95_ms_max=500.0, failure_rate_max=0.01),
        SLO(name="graph-trace:v2", p95_ms_max=1500.0, failure_rate_max=0.01),
        SLO(name="graph-ancestors:get", p95_ms_max=800.0, failure_rate_max=0.01),
        SLO(name="graph-descendants:get", p95_ms_max=800.0, failure_rate_max=0.01),
        SLO(name="graph-children:get", p95_ms_max=400.0, failure_rate_max=0.01),
        SLO(name="graph-children-edges:get", p95_ms_max=600.0, failure_rate_max=0.01),
    ],
    100: [
        SLO(name="Aggregated", p95_ms_max=800.0, failure_rate_max=0.005),
        SLO(name="views:list", p95_ms_max=300.0, failure_rate_max=0.01),
        SLO(name="views:popular", p95_ms_max=300.0, failure_rate_max=0.01),
        SLO(name="cached-stats:get", p95_ms_max=500.0, failure_rate_max=0.01),
        SLO(name="announcements:list", p95_ms_max=200.0, failure_rate_max=0.01),
        SLO(name="aggregation-jobs:list", p95_ms_max=1000.0, failure_rate_max=0.01),
        SLO(name="graph-schema:get", p95_ms_max=1000.0, failure_rate_max=0.01),
        SLO(name="graph-trace:v2", p95_ms_max=3000.0, failure_rate_max=0.01),
        SLO(name="graph-ancestors:get", p95_ms_max=1500.0, failure_rate_max=0.01),
        SLO(name="graph-descendants:get", p95_ms_max=1500.0, failure_rate_max=0.01),
        SLO(name="graph-children:get", p95_ms_max=800.0, failure_rate_max=0.01),
        SLO(name="graph-children-edges:get", p95_ms_max=1200.0, failure_rate_max=0.01),
    ],
    500: [
        SLO(name="Aggregated", p95_ms_max=1500.0, failure_rate_max=0.01),
        SLO(name="views:list", p95_ms_max=600.0, failure_rate_max=0.02),
        SLO(name="views:popular", p95_ms_max=600.0, failure_rate_max=0.02),
        SLO(name="cached-stats:get", p95_ms_max=1000.0, failure_rate_max=0.02),
        SLO(name="announcements:list", p95_ms_max=400.0, failure_rate_max=0.02),
        SLO(name="aggregation-jobs:list", p95_ms_max=2500.0, failure_rate_max=0.02),
        SLO(name="graph-schema:get", p95_ms_max=2500.0, failure_rate_max=0.02),
        SLO(name="graph-trace:v2", p95_ms_max=6000.0, failure_rate_max=0.02),
        SLO(name="graph-ancestors:get", p95_ms_max=3000.0, failure_rate_max=0.02),
        SLO(name="graph-descendants:get", p95_ms_max=3000.0, failure_rate_max=0.02),
        SLO(name="graph-children:get", p95_ms_max=1500.0, failure_rate_max=0.02),
        SLO(name="graph-children-edges:get", p95_ms_max=2500.0, failure_rate_max=0.02),
    ],
    1000: [
        SLO(name="Aggregated", p95_ms_max=3000.0, failure_rate_max=0.02),
        SLO(name="views:list", p95_ms_max=1200.0, failure_rate_max=0.03),
        SLO(name="views:popular", p95_ms_max=1200.0, failure_rate_max=0.03),
        SLO(name="cached-stats:get", p95_ms_max=2000.0, failure_rate_max=0.03),
        SLO(name="announcements:list", p95_ms_max=800.0, failure_rate_max=0.03),
        SLO(name="aggregation-jobs:list", p95_ms_max=5000.0, failure_rate_max=0.03),
        SLO(name="graph-schema:get", p95_ms_max=5000.0, failure_rate_max=0.03),
        SLO(name="graph-trace:v2", p95_ms_max=12000.0, failure_rate_max=0.03),
        SLO(name="graph-ancestors:get", p95_ms_max=6000.0, failure_rate_max=0.03),
        SLO(name="graph-descendants:get", p95_ms_max=6000.0, failure_rate_max=0.03),
        SLO(name="graph-children:get", p95_ms_max=3000.0, failure_rate_max=0.03),
        SLO(name="graph-children-edges:get", p95_ms_max=5000.0, failure_rate_max=0.03),
    ],
}


def slos_for_tier(n_users: int) -> List[SLO]:
    """Pick the SLO list for a given concurrency tier.

    Falls back to the next-lower tier if ``n_users`` isn't an exact
    match (e.g. ``--tier 200`` uses the 100-user thresholds — the
    next-higher tier would be too lax and hide regressions).
    """
    available = sorted(TIER_SLOS.keys())
    chosen = available[0]
    for t in available:
        if t <= n_users:
            chosen = t
    return TIER_SLOS[chosen]


def _parse_stats_csv(path: str) -> Dict[str, Dict[str, str]]:
    """Read Locust's per-endpoint CSV into ``{name: row}``.

    Locust column names are stable across the 2.x series: ``Name``,
    ``Request Count``, ``Failure Count``, ``95%``, ``99%``, etc.
    """
    rows: Dict[str, Dict[str, str]] = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows[row.get("Name", "")] = row
    return rows


def _float(row: Dict[str, str], key: str) -> float:
    raw = (row.get(key) or "0").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _int(row: Dict[str, str], key: str) -> int:
    raw = (row.get(key) or "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def assert_slos(
    csv_path: str,
    slos: Optional[List[SLO]] = None,
    skip_missing: bool = False,
) -> List[str]:
    """Check a Locust CSV against a list of SLOs.

    Returns a list of human-readable violation messages. Empty list
    means everything passed. Missing endpoints (named in an SLO but
    absent from the CSV) normally count as violations — usually means
    the test didn't actually exercise the endpoint. Pass
    ``skip_missing=True`` for per-scenario smoke runs where most of the
    DEFAULT_SLOS endpoints are intentionally not exercised.
    """
    slos = slos if slos is not None else DEFAULT_SLOS
    rows = _parse_stats_csv(csv_path)
    violations: List[str] = []

    for slo in slos:
        row = rows.get(slo.name)
        if row is None:
            if skip_missing:
                continue
            violations.append(f"[{slo.name}] missing from CSV (was the endpoint hit?)")
            continue

        count = _int(row, "Request Count")
        failures = _int(row, "Failure Count")
        p95 = _float(row, "95%")
        p99 = _float(row, "99%")
        failure_rate = (failures / count) if count else 0.0

        if slo.min_request_count is not None and count < slo.min_request_count:
            violations.append(
                f"[{slo.name}] only {count} requests "
                f"(need ≥{slo.min_request_count}); test may be too short or unbalanced"
            )
        if slo.p95_ms_max is not None and p95 > slo.p95_ms_max:
            violations.append(
                f"[{slo.name}] p95 = {p95:.0f} ms (> {slo.p95_ms_max:.0f} ms SLO)"
            )
        if slo.p99_ms_max is not None and p99 > slo.p99_ms_max:
            violations.append(
                f"[{slo.name}] p99 = {p99:.0f} ms (> {slo.p99_ms_max:.0f} ms SLO)"
            )
        if slo.failure_rate_max is not None and failure_rate > slo.failure_rate_max:
            violations.append(
                f"[{slo.name}] failure rate = {failure_rate:.2%} "
                f"(> {slo.failure_rate_max:.2%} SLO; {failures}/{count})"
            )

    return violations


def main(argv: List[str]) -> int:
    args = argv[1:]
    smoke = False
    tier: Optional[int] = None
    positional: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--smoke":
            smoke = True
        elif a == "--tier":
            i += 1
            if i >= len(args):
                print("Usage: python -m lib.slo [--smoke | --tier N] <stats.csv>", file=sys.stderr)
                return 2
            try:
                tier = int(args[i])
            except ValueError:
                print(f"--tier expects an integer, got {args[i]!r}", file=sys.stderr)
                return 2
        elif a.startswith("--tier="):
            try:
                tier = int(a.split("=", 1)[1])
            except ValueError:
                print(f"--tier expects an integer, got {a!r}", file=sys.stderr)
                return 2
        elif a.startswith("--"):
            print(f"Unknown flag: {a}", file=sys.stderr)
            return 2
        else:
            positional.append(a)
        i += 1

    if len(positional) != 1:
        print("Usage: python -m lib.slo [--smoke | --tier N] <stats.csv>", file=sys.stderr)
        return 2
    if smoke and tier is not None:
        print("--smoke and --tier are mutually exclusive", file=sys.stderr)
        return 2

    csv_path = positional[0]
    if tier is not None:
        slos = slos_for_tier(tier)
        mode = f"tier-{tier}"
        skip_missing = True  # heavy scenarios may not run at every tier
    elif smoke:
        slos = SMOKE_SLOS
        mode = "smoke"
        skip_missing = True
    else:
        slos = DEFAULT_SLOS
        mode = "production"
        skip_missing = False

    violations = assert_slos(csv_path, slos=slos, skip_missing=skip_missing)
    if not violations:
        print(f"SLO check passed against {csv_path} ({mode} thresholds)")
        return 0
    print(f"SLO violations ({mode} thresholds):", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
