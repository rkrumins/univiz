"""Lint test — fails CI if the unlabeled-MATCH-in-UNWIND antipattern
is reintroduced into the FalkorDB provider.

The antipattern is ``UNWIND $list AS x MATCH (n {urn: x}) ...``. Without
an unlabeled URN index, the inner MATCH does a full node scan per
UNWIND iteration — O(|graph| * |list|). Three production aggregation
incidents traced back to this exact shape (see plan section
"Discovered During Implementation 2026-05-12"); every place that takes
a list of URNs and looks up their nodes must use the
``MATCH (n) WHERE n.urn IN $list`` form (one seek/scan total).

Deliberately a source-text grep, not an import-and-introspect test, so
it runs without spinning up the full conftest stack (which currently
needs auth-service deps that aren't installed in every CI lane).
"""
from __future__ import annotations

import re
from pathlib import Path


# Match ``UNWIND $foo AS bar`` followed within ~120 chars by ``MATCH (xyz
# {urn: ...})`` — i.e., an unlabeled match keyed on the UNWINDed item's
# urn property. Tolerant to whitespace, line breaks, f-string artifacts.
# Skips ``MATCH (xyz:Label {urn: ...})`` (labeled — fine) and ``MATCH
# (xyz {id: ...})`` (different property — different cost profile).
_ANTIPATTERN_RE = re.compile(
    r"UNWIND\s+\$[A-Za-z_][A-Za-z0-9_]*\s+AS\s+[A-Za-z_][A-Za-z0-9_]*"
    r".{0,160}?"
    r"MATCH\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\{\s*urn\s*:",
    re.DOTALL,
)


PROVIDER_PATH = (
    Path(__file__).resolve().parent.parent
    / "app" / "providers" / "falkordb_provider.py"
)


def test_falkordb_provider_has_no_unlabeled_unwind_match() -> None:
    """Source text of the FalkorDB provider must not contain the
    unlabeled-MATCH-in-UNWIND antipattern. Use
    ``MATCH (n) WHERE n.urn IN $list`` instead — see plan Phase 1.5.
    """
    assert PROVIDER_PATH.exists(), f"provider source not found at {PROVIDER_PATH}"
    src = PROVIDER_PATH.read_text(encoding="utf-8")

    # Strip docstrings so a doc reference to the antipattern (e.g.
    # "the old form was ``UNWIND $urns AS u MATCH (n {urn: u})``")
    # doesn't trigger a false positive.
    src_no_docs = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src_no_docs = re.sub(r"'''(?:.|\n)*?'''", "", src_no_docs)

    # Honor explicit suppressions: lines containing
    # ``# nolint-unlabeled-unwind-match`` exempt a 25-line window
    # following the marker from the antipattern check. Used for places
    # where the UNWIND-with-unlabeled-MATCH is the *intended* semantic
    # (e.g. pair-bounded DELETE/UPDATE in the incremental ingest path —
    # rewriting to WHERE-IN would Cartesian-broaden the operation).
    lines = src_no_docs.split("\n")
    masked: list[str] = []
    skip_remaining = 0
    for line in lines:
        if "nolint-unlabeled-unwind-match" in line:
            skip_remaining = 25
            masked.append("# <masked-by-nolint>")
            continue
        if skip_remaining > 0:
            masked.append("# <masked-by-nolint>")
            skip_remaining -= 1
            continue
        masked.append(line)
    src_no_docs = "\n".join(masked)

    # Strip remaining full-line comments. (Per-line trailing-comment
    # stripping is fragile because `#` can appear inside strings; the
    # docstring + nolint pass above is the main signal-cleaner.)
    src_no_docs = re.sub(r"(?m)^\s*#.*$", "", src_no_docs)

    matches = list(_ANTIPATTERN_RE.finditer(src_no_docs))
    if matches:
        # Report locations as line numbers in the cleaned source. Not
        # perfectly aligned with original-source lines (docstring strip
        # shifts offsets) but precise enough to find the offender.
        hits: list[str] = []
        for m in matches:
            line_no = src_no_docs.count("\n", 0, m.start()) + 1
            snippet = src_no_docs[m.start(): min(m.end(), m.start() + 200)]
            hits.append(f"  line ~{line_no}: {snippet!r}")
        raise AssertionError(
            "Unlabeled-MATCH-in-UNWIND antipattern detected in "
            "falkordb_provider.py. Rewrite to "
            "`MATCH (n) WHERE n.urn IN $list` form. See plan Phase 1.5. "
            "Offending location(s):\n" + "\n".join(hits)
        )
