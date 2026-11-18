"""Agronomic computations for the randomized complete block trial.

Dry matter yield per plot:
    dry = fresh_weight * (1 - moisture) / area

Within-block treatment difference (B - A) is computed only for blocks where
both plots have area, fresh weight and moisture.  The two-sided exact
randomization test enumerates all 2^n within-block A/B label swaps and
returns the proportion whose absolute mean difference is not smaller than
the observed one.
"""

from __future__ import annotations

from itertools import product
from typing import Any, Iterable, Mapping

REQUIRED_FIELDS = ("area", "fresh_weight", "moisture")
TREATMENTS = ("A", "B")


def dry_matter(fresh_weight: float, moisture: float, area: float) -> float:
    """Dry matter yield per unit area."""
    return fresh_weight * (1.0 - moisture) / area


def evaluate_blocks(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split imported blocks into complete (analysable) and pending ones.

    Returns (complete, pending):
      complete: [{"block_id", "dry_a", "dry_b", "diff"}]  (diff = B - A)
      pending:  [{"block_id", "missing": [reasons...]}]
    Blocks with no imported plots at all are not part of the trial and are
    omitted from both lists.
    """
    by_block: dict[int, dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        by_block.setdefault(int(row["block_id"]), {})[row["treatment"]] = row

    complete: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for block_id in sorted(by_block):
        plots = by_block[block_id]
        missing: list[str] = []
        for treatment in TREATMENTS:
            plot = plots.get(treatment)
            if plot is None:
                missing.append(f"{treatment}: plot not imported")
                continue
            for field in REQUIRED_FIELDS:
                if plot[field] is None:
                    missing.append(f"{treatment}.{field} missing")
        if missing:
            pending.append({"block_id": block_id, "missing": missing})
            continue
        plot_a, plot_b = plots["A"], plots["B"]
        dry_a = dry_matter(plot_a["fresh_weight"], plot_a["moisture"], plot_a["area"])
        dry_b = dry_matter(plot_b["fresh_weight"], plot_b["moisture"], plot_b["area"])
        complete.append(
            {"block_id": block_id, "dry_a": dry_a, "dry_b": dry_b, "diff": dry_b - dry_a}
        )
    return complete, pending


def exact_randomization_p(diffs: list[float]) -> tuple[float, int]:
    """Two-sided exact randomization (sign-permutation) p-value.

    Enumerates all 2^n within-block A/B swaps.  The p-value is the share of
    permutations whose absolute mean difference is not smaller than the
    observed absolute mean difference.  The observed assignment itself is
    included, so p >= 1 / 2^n.
    """
    n = len(diffs)
    if n == 0:
        raise ValueError("at least one complete block is required")
    observed = sum(diffs) / n
    abs_observed = abs(observed)
    total = 1 << n
    hits = 0
    tolerance = 1e-9  # guards float round-off on exact ties
    for signs in product((1.0, -1.0), repeat=n):
        statistic = sum(sign * d for sign, d in zip(signs, diffs)) / n
        if abs(statistic) >= abs_observed - tolerance:
            hits += 1
    return hits / total, total
