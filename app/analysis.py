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


# ------------------------------------------------------- measurement errors

SIGN_TOLERANCE = 1e-12


def plot_dry_extrema(
    fresh_weight: float,
    moisture: float,
    area: float,
    gain: float,
    weight_error: float,
    moisture_error: float,
) -> tuple[float, float]:
    """Min/max achievable dry matter of one plot for a *fixed* shared gain.

    True fresh weight is ``fresh_weight * gain + delta`` with
    ``|delta| <= weight_error`` and must stay non-negative; true moisture is
    the recorded moisture plus an error of at most ``moisture_error``,
    clipped to [0, 1].  Dry matter is monotone in both free quantities, so
    both extrema sit at interval endpoints.
    """
    weight_lo = max(0.0, fresh_weight * gain - weight_error)
    weight_hi = fresh_weight * gain + weight_error
    moisture_lo = max(0.0, moisture - moisture_error)
    moisture_hi = min(1.0, moisture + moisture_error)
    dry_lo = weight_lo * (1.0 - moisture_hi) / area
    dry_hi = weight_hi * (1.0 - moisture_lo) / area
    return dry_lo, dry_hi


def mean_difference_bounds(
    blocks: Iterable[Mapping[str, Any]],
    errors: Mapping[tuple[int, str], Mapping[str, float]],
    gain_error: float,
) -> tuple[float, float]:
    """Exact reachable [lower, upper] bounds of the mean difference B - A.

    ``blocks`` are the publication's complete blocks; each carries the two
    source plots (``plot_a``/``plot_b`` with area, fresh_weight, moisture).
    ``errors`` maps (block_id, treatment) to weight_error/moisture_error.

    One gain variable ``g in [1-e, 1+e]`` is shared by every plot.  For a
    fixed g the per-plot variables are independent, so each block's
    difference extremum is a combination of plot endpoint values; the result
    is piecewise linear in g with kinks only where the non-negativity clamp
    activates (g = weight_error / fresh_weight).  A piecewise-linear
    function reaches its extrema at interval endpoints and kinks, hence
    evaluating all candidate g values gives the exact global bounds.
    """
    block_list = list(blocks)
    n = len(block_list)
    if n == 0:
        raise ValueError("at least one complete block is required")

    g_lo, g_hi = 1.0 - gain_error, 1.0 + gain_error
    candidates = {g_lo, g_hi}
    for block in block_list:
        for treatment in TREATMENTS:
            plot = block["plot_a"] if treatment == "A" else block["plot_b"]
            spec = errors[(block["block_id"], treatment)]
            weight = float(plot["fresh_weight"])
            if weight > 0.0 and spec["weight_error"] > 0.0:
                kink = spec["weight_error"] / weight
                if g_lo < kink < g_hi:
                    candidates.add(kink)

    global_lo = float("inf")
    global_hi = float("-inf")
    for g in candidates:
        mean_max = 0.0
        mean_min = 0.0
        for block in block_list:
            block_id = block["block_id"]
            for treatment in TREATMENTS:
                plot = block["plot_a"] if treatment == "A" else block["plot_b"]
                spec = errors[(block_id, treatment)]
                dry_lo, dry_hi = plot_dry_extrema(
                    float(plot["fresh_weight"]),
                    float(plot["moisture"]),
                    float(plot["area"]),
                    g,
                    float(spec["weight_error"]),
                    float(spec["moisture_error"]),
                )
                if treatment == "B":
                    mean_max += dry_hi
                    mean_min += dry_lo
                else:
                    mean_max -= dry_lo
                    mean_min -= dry_hi
        global_hi = max(global_hi, mean_max / n)
        global_lo = min(global_lo, mean_min / n)
    return global_lo, global_hi


def classify_sign(lower: float, upper: float) -> tuple[str, bool]:
    """Whether the reachable interval keeps one sign, allowing round-off."""
    if lower > SIGN_TOLERANCE:
        return "positive", True
    if upper < -SIGN_TOLERANCE:
        return "negative", True
    return "indeterminate", False
