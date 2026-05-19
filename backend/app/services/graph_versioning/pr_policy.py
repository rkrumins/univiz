"""Pull-request merge policy — pure decision logic.

Encodes the plan's PR edge cases independently of the DB so they are
exhaustively unit-testable:

* **Latest-review-per-reviewer wins** — an earlier ``changes_requested``
  is cleared by a later ``approved`` from the same reviewer (and vice
  versa). ``commented`` reviews never gate.
* **Approval is gated on a clean review state** — mergeable requires at
  least one current ``approved`` and **no** current
  ``changes_requested``.
* **Stale-approval invalidation** — an ``approved`` verdict only counts
  if it was given against the *current* base head **and** the current
  fork/head commit. If either side advanced since, the approval is
  stale and re-approval is required (the plan's "approval invalidated
  if the base or fork head changed").
* **Status gating** — only an ``open``/``approved`` PR can merge;
  ``merged``/``closed`` cannot; an empty PR (no delta) is not
  mergeable.

RBAC (``workspace:graph:merge`` on the *base* workspace, re-checked at
merge time, fail-closed) is enforced in the service layer, not here —
this module is pure and side-effect free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class ReviewRecord:
    reviewer: str
    state: str                       # approved | changes_requested | commented
    created_at: str                  # ISO; lexically sortable
    reviewed_base_commit_id: str | None
    reviewed_head_commit_id: str | None


@dataclass(frozen=True)
class MergeabilityResult:
    mergeable: bool
    reason: str
    stale_reviewers: list[str] = field(default_factory=list)


def latest_per_reviewer(reviews: Iterable[ReviewRecord]) -> dict[str, ReviewRecord]:
    """Most recent *gating* review per reviewer. ``commented`` is not a
    verdict and is ignored for gating."""
    latest: dict[str, ReviewRecord] = {}
    for r in reviews:
        if r.state == "commented":
            continue
        cur = latest.get(r.reviewer)
        if cur is None or r.created_at > cur.created_at:
            latest[r.reviewer] = r
    return latest


def evaluate_mergeability(
    *,
    status: str,
    reviews: Iterable[ReviewRecord],
    base_head_commit_id: str | None,
    head_head_commit_id: str | None,
    has_changes: bool,
) -> MergeabilityResult:
    """Decide whether a PR may merge right now.

    ``base_head_commit_id`` / ``head_head_commit_id`` are the *current*
    tip commits of the base branch and the fork/head ref.
    ``has_changes`` is False when the three-way merge yields no delta
    (empty PR).
    """
    if status in ("merged", "closed"):
        return MergeabilityResult(False, f"PR is {status}")
    if not has_changes:
        return MergeabilityResult(False, "PR has no changes to merge (empty)")

    latest = latest_per_reviewer(reviews)
    if any(r.state == "changes_requested" for r in latest.values()):
        return MergeabilityResult(False, "changes requested by a reviewer")

    approvals = [r for r in latest.values() if r.state == "approved"]
    if not approvals:
        return MergeabilityResult(False, "no approving review")

    # An approval is only valid against the heads it was given for.
    fresh, stale = [], []
    for a in approvals:
        if (
            a.reviewed_base_commit_id == base_head_commit_id
            and a.reviewed_head_commit_id == head_head_commit_id
        ):
            fresh.append(a)
        else:
            stale.append(a.reviewer)

    if not fresh:
        return MergeabilityResult(
            False,
            "all approvals are stale — base or fork advanced; re-approval "
            "required",
            stale_reviewers=sorted(set(stale)),
        )
    return MergeabilityResult(
        True, "approved", stale_reviewers=sorted(set(stale))
    )


def is_approval_stale(
    review: ReviewRecord,
    *,
    base_head_commit_id: str | None,
    head_head_commit_id: str | None,
) -> bool:
    """True if *review* (an approval) no longer matches current heads."""
    if review.state != "approved":
        return False
    return not (
        review.reviewed_base_commit_id == base_head_commit_id
        and review.reviewed_head_commit_id == head_head_commit_id
    )


__all__ = [
    "ReviewRecord",
    "MergeabilityResult",
    "latest_per_reviewer",
    "evaluate_mergeability",
    "is_approval_stale",
]
