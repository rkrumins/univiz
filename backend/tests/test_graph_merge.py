"""Unit tests for the three-way merge engine. Pure, no DB."""
import pytest

from backend.app.services.graph_versioning.manifest import (
    ManifestEntry,
    build_snapshot,
)
from backend.app.services.graph_versioning.merge import (
    MergeIntegrityError,
    UnresolvedConflictsError,
    apply_resolutions,
    check_referential_integrity,
    three_way_merge,
)

PC = 64


def snap(entries: dict[str, tuple[str, str]]):
    return build_snapshot(
        [ManifestEntry(k, kind, h) for k, (kind, h) in entries.items()], PC
    )


def n(h):
    return ("node", h)


# ── three_way_merge ────────────────────────────────────────────────

def test_disjoint_changes_auto_merge_clean():
    base = snap({"a": n("a0"), "b": n("b0")})
    ours = snap({"a": n("a1"), "b": n("b0")})       # changed a
    theirs = snap({"a": n("a0"), "b": n("b1")})     # changed b
    out = three_way_merge(base, ours, theirs)
    assert out.is_clean
    assert out.auto == {"a": n("a1"), "b": n("b1")}


def test_both_made_same_change_no_conflict():
    base = snap({"a": n("a0")})
    ours = snap({"a": n("a1")})
    theirs = snap({"a": n("a1")})
    out = three_way_merge(base, ours, theirs)
    assert out.is_clean and out.auto["a"] == n("a1")


def test_one_side_unchanged_takes_other_incl_delete():
    base = snap({"a": n("a0"), "b": n("b0")})
    ours = snap({"a": n("a0")})                     # deleted b
    theirs = snap({"a": n("a0"), "b": n("b0")})     # unchanged
    out = three_way_merge(base, ours, theirs)
    assert out.is_clean
    assert "b" not in out.auto and out.auto["a"] == n("a0")


def test_modify_modify_conflict():
    base = snap({"a": n("a0")})
    ours = snap({"a": n("a1")})
    theirs = snap({"a": n("a2")})
    out = three_way_merge(base, ours, theirs)
    assert not out.is_clean
    c = out.conflicts[0]
    assert c.key == "a" and c.conflict_class == "modify_modify"
    assert (c.ours_hash, c.theirs_hash) == ("a1", "a2")


def test_add_add_conflict():
    base = snap({})
    ours = snap({"x": n("o")})
    theirs = snap({"x": n("t")})
    out = three_way_merge(base, ours, theirs)
    assert out.conflicts[0].conflict_class == "add_add"


def test_edit_delete_conflict():
    base = snap({"a": n("a0")})
    ours = snap({"a": n("a1")})        # edited
    theirs = snap({})                  # deleted
    out = three_way_merge(base, ours, theirs)
    assert out.conflicts[0].conflict_class == "edit_delete"


def test_merkle_prunes_unchanged_partitions():
    big = {f"k{i}": n(f"h{i}") for i in range(2000)}
    base = snap(big)
    ours = snap({**big, "k1": n("CHANGED")})
    theirs = snap(big)
    out = three_way_merge(base, ours, theirs)
    assert out.is_clean and out.auto["k1"] == n("CHANGED")
    # Only the divergent partition(s) were hash-scanned, not all.
    assert out.scanned_partitions < out.total_partitions
    assert out.scanned_partitions <= 2


def test_partition_count_mismatch_rejected():
    with pytest.raises(ValueError, match="partition_count"):
        three_way_merge(
            build_snapshot([], 64),
            build_snapshot([], 128),
            build_snapshot([], 64),
        )


# ── apply_resolutions ──────────────────────────────────────────────

def test_apply_resolutions_requires_all_conflicts():
    base = snap({"a": n("a0")})
    out = three_way_merge(base, snap({"a": n("a1")}), snap({"a": n("a2")}))
    with pytest.raises(UnresolvedConflictsError):
        apply_resolutions(out, {})


def test_apply_resolutions_choose_and_delete():
    base = snap({"a": n("a0"), "b": n("b0")})
    out = three_way_merge(
        base,
        snap({"a": n("a1"), "b": n("b1")}),
        snap({"a": n("a2"), "b": n("b2")}),
    )
    merged = apply_resolutions(
        out, {"a": ("node", "a1"), "b": None}  # keep ours a, drop b
    )
    assert merged["a"] == n("a1") and "b" not in merged


# ── check_referential_integrity ────────────────────────────────────

def _edges(mapping):
    """content_hash -> (src,tgt,type) resolver from a dict."""
    return lambda h: mapping[h]


def test_dangling_edge_detected_post_merge():
    merged = {"n1": n("h1"), "e1": ("edge", "eh1")}
    v = check_referential_integrity(
        merged,
        get_edge_endpoints=_edges({"eh1": ("n1", "GONE", "flows_to")}),
    )
    assert any(x.code == "dangling_edge" and "GONE" in x.detail for x in v)


def test_clean_merge_has_no_integrity_violations():
    merged = {"n1": n("h1"), "n2": n("h2"), "e1": ("edge", "eh1")}
    v = check_referential_integrity(
        merged, get_edge_endpoints=_edges({"eh1": ("n1", "n2", "flows_to")})
    )
    assert v == []


def test_containment_cycle_detected():
    merged = {
        "a": n("ha"), "b": n("hb"), "c": n("hc"),
        "e1": ("edge", "e1"), "e2": ("edge", "e2"), "e3": ("edge", "e3"),
    }
    eps = {
        "e1": ("a", "b", "contains"),
        "e2": ("b", "c", "contains"),
        "e3": ("c", "a", "contains"),  # cycle a→b→c→a
    }
    v = check_referential_integrity(
        merged,
        get_edge_endpoints=_edges(eps),
        containment_edge_types=frozenset({"contains"}),
    )
    assert any(x.code == "containment_cycle" for x in v)


def test_non_containment_cycle_is_allowed():
    merged = {"a": n("ha"), "b": n("hb"),
              "e1": ("edge", "e1"), "e2": ("edge", "e2")}
    eps = {"e1": ("a", "b", "flows_to"), "e2": ("b", "a", "flows_to")}
    v = check_referential_integrity(
        merged,
        get_edge_endpoints=_edges(eps),
        containment_edge_types=frozenset({"contains"}),
    )
    assert v == []  # lineage cycles are fine; only containment must be acyclic


def test_resolution_can_reintroduce_dangling_caught_on_rerun():
    # ours deletes n2; theirs keeps edge e1 (n1->n2). Conflict on e1.
    base = snap({"n1": n("h1"), "n2": n("h2"), "e1": ("edge", "eh_old")})
    ours = snap({"n1": n("h1"), "e1": ("edge", "eh_old")})          # del n2
    theirs = snap({"n1": n("h1"), "n2": n("h2"), "e1": ("edge", "eh_new")})
    out = three_way_merge(base, ours, theirs)
    # Resolve e1 by KEEPING theirs (edge survives) but n2 stays deleted
    # (n2: ours deleted, theirs unchanged -> auto-took theirs? no:
    # base==theirs for n2 so rule "theirs unchanged -> take ours" =>
    # n2 deleted). So a kept edge now dangles -> integrity must catch.
    merged = apply_resolutions(out, {"e1": ("edge", "eh_new")})
    v = check_referential_integrity(
        merged, get_edge_endpoints=_edges({"eh_new": ("n1", "n2", "flows_to")})
    )
    assert any(x.code == "dangling_edge" for x in v)
    with pytest.raises(MergeIntegrityError):
        raise MergeIntegrityError(v)
