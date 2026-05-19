"""Measure on-disk size of the JSON columns in ``data_source_stats``.

Decides whether the WS-2 ``TEXT(JSON) → JSONB`` migration is worth the
risk. The migration's main benefit is removing ``json.loads`` of these
blobs from the request hot path; that benefit scales with blob size:

* < 10 KB p95   → JSONB win is below the noise floor; skip migration
* 10-100 KB p95 → modest win (~5-15ms per request under contention)
* > 100 KB p95  → real win; migration pays for itself

Usage::

    # Against local dev (defaults to MANAGEMENT_DB_URL from .env.dev):
    export MANAGEMENT_DB_URL=postgresql+asyncpg://synodic:synodic@localhost:5432/synodic
    python -m backend.scripts.profile_stats_cache_sizes

    # Or pass a URL explicitly:
    python -m backend.scripts.profile_stats_cache_sizes --url postgresql://...

Read-only — only runs SELECT and EXPLAIN-style queries. Safe to run
against a populated production replica.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import List, Tuple

import asyncpg


# Columns we suspect are TEXT-encoded JSON in the hot path. Order
# matters only for the report's readability.
COLUMNS: List[str] = [
    "entity_type_counts",
    "edge_type_counts",
    "schema_stats",
    "ontology_metadata",
    "graph_schema",
]

# Quick-look classification thresholds (bytes). Used only to print a
# recommendation at the end — the raw numbers are the actual signal.
NOISE_BYTES = 10 * 1024            # < this → JSONB win is below noise
MODEST_BYTES = 100 * 1024          # < this → modest win, above this real


def _normalise_url(url: str) -> str:
    """asyncpg.connect() wants a plain postgresql:// URL — strip the
    SQLAlchemy-flavoured ``+asyncpg`` if present so the script works
    against the same env var the app uses."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _profile_column(conn: asyncpg.Connection, table: str, col: str) -> dict:
    """Return size stats for one JSON-ish column.

    ``pg_column_size`` reports the on-disk size (post-TOAST). For
    inline columns this is roughly len() + a small header; for TOASTed
    columns it's the compressed size, which is what JSON.parse actually
    has to materialise from.
    """
    row = await conn.fetchrow(
        f"""
        SELECT
            count(*) FILTER (WHERE {col} IS NOT NULL AND {col} <> '{{}}'::text) AS populated,
            count(*) FILTER (WHERE {col} IS NULL OR {col} = '{{}}'::text)       AS empty,
            min(pg_column_size({col}))                                          AS min_bytes,
            percentile_cont(0.5)  WITHIN GROUP (ORDER BY pg_column_size({col})) AS p50_bytes,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY pg_column_size({col})) AS p95_bytes,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY pg_column_size({col})) AS p99_bytes,
            max(pg_column_size({col}))                                          AS max_bytes,
            sum(pg_column_size({col}))                                          AS total_bytes
        FROM {table}
        """
    )
    return dict(row) if row else {}


def _human(n: float | int | None) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.2f}MB"


def _classify(p95: float | None, max_b: float | None) -> Tuple[str, str]:
    """One-line recommendation derived from p95 + max."""
    if p95 is None and max_b is None:
        return "no-data", "Column is empty — nothing to migrate."
    p95 = int(p95 or 0)
    max_b = int(max_b or 0)
    if max_b < NOISE_BYTES:
        return "skip", (
            "Both p95 and max are well under 10KB. JSONB win on this column "
            "is below the noise floor (1-3ms). Not worth the migration."
        )
    if p95 < NOISE_BYTES and max_b < MODEST_BYTES:
        return "borderline", (
            "Typical rows are small but the tail goes into double-digit KB. "
            "JSONB helps rare large rows; small gain on average."
        )
    if p95 < MODEST_BYTES:
        return "modest-win", (
            "p95 is 10-100KB. JSONB saves ~5-15ms per request on this column "
            "under contention. Migration is justifiable."
        )
    return "real-win", (
        "p95 is >100KB. json.loads on this column is meaningful event-loop "
        "blocking under concurrency. Migration recommended."
    )


async def main(database_url: str, table: str) -> int:
    url = _normalise_url(database_url)
    print(f"Connecting to {url.split('@', 1)[-1]} (table: {table})\n")

    try:
        conn = await asyncpg.connect(url)
    except Exception as e:
        print(f"ERROR: could not connect: {e}", file=sys.stderr)
        print(
            "\nIf the table is on a different DB, pass --url. "
            "If the local stack is down, run `./dev.sh infra` first.",
            file=sys.stderr,
        )
        return 2

    try:
        # Top-line: does the table exist and how many rows are there?
        try:
            total_rows = await conn.fetchval(f"SELECT count(*) FROM {table}")
        except asyncpg.UndefinedTableError:
            print(f"ERROR: table {table!r} does not exist in this DB.", file=sys.stderr)
            return 2
        if total_rows == 0:
            print(
                f"Table {table!r} has 0 rows. Run the stats backfill first, "
                "or point at a populated environment.",
                file=sys.stderr,
            )
            return 0

        print(f"Total rows: {total_rows}\n")
        header = f"{'column':<22} {'pop/empty':>11} {'min':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>10} {'total':>10}"
        print(header)
        print("-" * len(header))

        recommendations: List[Tuple[str, str, str]] = []
        for col in COLUMNS:
            stats = await _profile_column(conn, table, col)
            populated = stats.get("populated") or 0
            empty = stats.get("empty") or 0
            print(
                f"{col:<22} "
                f"{populated:>5d}/{empty:<5d} "
                f"{_human(stats.get('min_bytes')):>8} "
                f"{_human(stats.get('p50_bytes')):>8} "
                f"{_human(stats.get('p95_bytes')):>8} "
                f"{_human(stats.get('p99_bytes')):>8} "
                f"{_human(stats.get('max_bytes')):>10} "
                f"{_human(stats.get('total_bytes')):>10}"
            )
            verdict, note = _classify(stats.get("p95_bytes"), stats.get("max_bytes"))
            recommendations.append((col, verdict, note))

        # Single-line recommendation per column at the bottom, where the
        # operator's eye lands after reading the size table.
        print("\nRecommendation (per column):")
        for col, verdict, note in recommendations:
            print(f"  [{verdict:<11}] {col:<22}  {note}")

        # Roll-up: should we ship the JSONB migration at all?
        any_real_win = any(v == "real-win" for _, v, _ in recommendations)
        any_modest = any(v in ("real-win", "modest-win") for _, v, _ in recommendations)
        print("\nOverall:")
        if any_real_win:
            print(
                "  GO  — at least one column has p95 > 100KB. JSONB migration "
                "is justified; the json.loads on these blobs is on the request hot path."
            )
        elif any_modest:
            print(
                "  MAYBE  — biggest column is in the 10-100KB p95 band. "
                "Migration gives a modest win; weigh against migration risk + "
                "the fact that WS-5 already addressed the request-volume problem."
            )
        else:
            print(
                "  SKIP  — every column is under 10KB p95. JSONB win is below "
                "the noise floor. Ship ETag/304 alone; skip the migration."
            )
    finally:
        await conn.close()
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        default=os.getenv("MANAGEMENT_DB_URL")
                 or os.getenv("DATABASE_URL")
                 or "postgresql://synodic:synodic@localhost:5432/synodic",
        help="Postgres URL. Defaults to MANAGEMENT_DB_URL / DATABASE_URL / dev fallback.",
    )
    parser.add_argument(
        "--table",
        default="data_source_stats",
        help="Table name (default: data_source_stats).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(args.url, args.table)))
