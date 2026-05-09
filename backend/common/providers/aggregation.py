"""Idempotent AGGREGATED edge materialiser.

When a leaf-level lineage edge ``s -[t]-> d`` is created, every
(ancestor_of_s, ancestor_of_d) pair gains an AGGREGATED edge that
summarises all such leaf edges between their respective subtrees.
Removing the leaf edge decrements (or removes) the AGGREGATED edge.

The bookkeeping has two parts:

1. **Pair generation** -- given ancestor chains for source and target,
   produce every cross-pair (skipping (x, x) self-loops).
2. **Idempotency** -- multiple leaf edges may contribute to the same
   AGGREGATED pair; we must dedupe on edge_id, both on add and remove.

This module owns the algorithm. The provider supplies an
``IdempotencyBackend`` (Redis Set, Neo4j list-property, Spanner JSON
array, ...) and a ``MaterializationBackend`` (single MERGE/INSERT
batch, the actual count, the actual delete loop).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Protocol, Set, Tuple

logger = logging.getLogger(__name__)


PairKey = Tuple[str, str]  # (source_ancestor_urn, target_ancestor_urn)


# ---------------------------------------------------------------------------
# Backend Protocols
# ---------------------------------------------------------------------------

class IdempotencyBackend(Protocol):
    """Per-pair set of contributing leaf edge ids.

    Contracts:
    * ``add(pair, edge_id)`` returns True if the edge was newly added,
      False if it was already present.
    * ``remove(pair, edge_id)`` returns True if the edge was present and
      now removed, False if it was already absent.
    * ``count(pair)`` returns the current number of contributing edges.
    * ``edge_types(pair)`` returns the set of edge_types observed in
      the contributing leaves (used to populate ``r.sourceEdgeTypes``).
    """

    async def add(self, pair: PairKey, edge_id: str, edge_type: str) -> bool: ...

    async def remove(self, pair: PairKey, edge_id: str) -> bool: ...

    async def count(self, pair: PairKey) -> int: ...

    async def edge_types(self, pair: PairKey) -> List[str]: ...

    async def purge_namespace(self) -> None:
        """Drop ALL bookkeeping (paired with provider's purge_aggregated_edges)."""
        ...


class MaterializationBackend(Protocol):
    """Provider-specific AGGREGATED edge writes."""

    async def upsert_aggregated_edges(
        self,
        pairs: List[Tuple[PairKey, int, List[str]]],
    ) -> None:
        """Idempotently MERGE/INSERT one AGGREGATED edge per pair with the
        given weight and source_edge_types. Caller has already deduped.
        """
        ...

    async def delete_aggregated_edge(self, pair: PairKey) -> None: ...

    async def update_aggregated_edge(
        self,
        pair: PairKey,
        weight: int,
        source_edge_types: List[str],
    ) -> None: ...

    async def count_all(self) -> int: ...

    async def purge_all(
        self,
        *,
        batch_size: int,
        progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int: ...


class AncestorResolver(Protocol):
    """Provides the source/target ancestor chains used for pair generation."""

    async def chain(self, urn: str) -> List[str]: ...

    async def chains(self, urns: List[str]) -> Dict[str, List[str]]: ...


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------

@dataclass
class MaterializerConfig:
    batch_size: int = 1000
    log_every_n_batches: int = 10


class AggregatedEdgeMaterializer:
    """Drives the AGGREGATED-edge lifecycle for one provider."""

    def __init__(
        self,
        *,
        idempotency: IdempotencyBackend,
        materialization: MaterializationBackend,
        ancestors: AncestorResolver,
        config: Optional[MaterializerConfig] = None,
    ) -> None:
        self._idem = idempotency
        self._mat = materialization
        self._anc = ancestors
        self._cfg = config or MaterializerConfig()

    # ----- write path -------------------------------------------------------

    async def materialize_lineage_edge(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
        edge_type: str,
    ) -> None:
        """Called from ``on_lineage_edge_written``."""
        s_chain, t_chain = await asyncio.gather(
            self._anc.chain(source_urn),
            self._anc.chain(target_urn),
        )
        s_chain = s_chain or [source_urn]
        t_chain = t_chain or [target_urn]
        pairs = list(_cross_pairs(s_chain, t_chain))
        if not pairs:
            return

        # Dedupe via idempotency backend; gather pair → (was_new, edge_types).
        new_pairs: List[Tuple[PairKey, int, List[str]]] = []
        for pair in pairs:
            was_new = await self._idem.add(pair, edge_id, edge_type)
            if was_new:
                count = await self._idem.count(pair)
                types = await self._idem.edge_types(pair)
                new_pairs.append((pair, count, types))

        if not new_pairs:
            return

        # Batch-write to the graph.
        for chunk in _chunks(new_pairs, self._cfg.batch_size):
            await self._mat.upsert_aggregated_edges(chunk)

    async def remove_lineage_edge(
        self,
        source_urn: str,
        target_urn: str,
        edge_id: str,
    ) -> None:
        """Called from ``on_lineage_edge_deleted``."""
        s_chain, t_chain = await asyncio.gather(
            self._anc.chain(source_urn),
            self._anc.chain(target_urn),
        )
        s_chain = s_chain or [source_urn]
        t_chain = t_chain or [target_urn]
        for pair in _cross_pairs(s_chain, t_chain):
            removed = await self._idem.remove(pair, edge_id)
            if not removed:
                continue
            remaining = await self._idem.count(pair)
            if remaining <= 0:
                await self._mat.delete_aggregated_edge(pair)
            else:
                types = await self._idem.edge_types(pair)
                await self._mat.update_aggregated_edge(pair, remaining, types)

    # ----- read path / lifecycle -------------------------------------------

    async def count(self) -> int:
        return await self._mat.count_all()

    async def purge_all(
        self,
        *,
        batch_size: int = 10_000,
        progress_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int:
        deleted = await self._mat.purge_all(
            batch_size=batch_size,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        # Drop bookkeeping after the graph delete completes; otherwise a
        # crash mid-delete leaves orphan idempotency entries that would
        # cause future leaf-edge writes to no-op against the (now empty)
        # AGGREGATED edges.
        try:
            await self._idem.purge_namespace()
        except Exception as exc:
            logger.warning("aggregation purge: idempotency purge failed: %s", exc)
        return deleted


# ---------------------------------------------------------------------------
# Pair generation
# ---------------------------------------------------------------------------

def _cross_pairs(s_chain: List[str], t_chain: List[str]) -> Iterable[PairKey]:
    """Yield every (s_anc, t_anc) cross-pair excluding self-loops.

    The chains include the leaf URN at index 0 (or wherever the
    provider's chain function placed it). Order does not matter for
    cross-product; we de-dup via the idempotency backend.
    """
    seen: Set[PairKey] = set()
    for s in s_chain:
        for t in t_chain:
            if s == t:
                continue
            pair = (s, t)
            if pair in seen:
                continue
            seen.add(pair)
            yield pair


def _chunks(items: List, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
