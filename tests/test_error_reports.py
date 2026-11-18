"""End-to-end tests for measurement-error range reports.

Covers:
  * error-settings versioning (revision inserts a new version, identical
    PUT is a no-op) and the two staleness reasons (settings revised,
    source data corrected);
  * exact reachable bounds of the mean B-A difference, independently
    re-checked with a dense grid scan and random interior sampling;
  * the experiment-wide shared gain (a gain two plots do not share would
    give a different, looser bound);
  * non-negativity of true weight and moisture clipped to [0, 1];
  * failed report attempts roll back and never touch older reports;
  * corrections never rewrite stored publications/reports, and new reports
    are computed from one committed publication + settings state.
"""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.analysis import plot_dry_extrema

SIMPLE_IMPORT = {
    "plots": [
        {"block_id": 1, "treatment": "A", "area": 1.0, "fresh_weight": 10.0},
        {"block_id": 1, "treatment": "B", "area": 1.0, "fresh_weight": 12.0},
        {"block_id": 2, "treatment": "A", "area": 2.0, "fresh_weight": 20.0},
        {"block_id": 2, "treatment": "B", "area": 2.0, "fresh_weight": 23.0},
    ]
}

SIMPLE_MOISTURE = {
    "entries": [
        {"block_id": 1, "treatment": "A", "moisture": 0.50},
        {"block_id": 1, "treatment": "B", "moisture": 0.50},
        {"block_id": 2, "treatment": "A", "moisture": 0.40},
        {"block_id": 2, "treatment": "B", "moisture": 0.40},
    ]
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "trial.db"))
    with TestClient(app) as test_client:
        yield test_client


def _seed_publication(client) -> dict:
    assert client.post("/imports", json=SIMPLE_IMPORT).status_code == 200
    assert client.post("/moisture", json=SIMPLE_MOISTURE).status_code == 200
    return client.post("/publications").json()


def _all_plot_keys() -> list[tuple[int, str]]:
    return [(b, t) for b in (1, 2) for t in ("A", "B")]


def _settings_payload(gain_error: float, weight_error: float, moisture_error: float):
    return {
        "gain_error": gain_error,
        "entries": [
            {
                "block_id": b,
                "treatment": t,
                "weight_error": weight_error,
                "moisture_error": moisture_error,
            }
            for b, t in _all_plot_keys()
        ],
    }


# ---------------------------------------------------------------- settings

def test_error_settings_versioning_and_noop(client):
    _seed_publication(client)

    resp = client.put("/error-settings", json=_settings_payload(0.05, 0.5, 0.02))
    assert resp.status_code == 200
    v1 = resp.json()
    assert v1["changed"] is True
    assert v1["id"] == 1
    assert v1["gain_error"] == 0.05
    assert len(v1["entries"]) == 4

    # identical replacement is a no-op: same id, no new version
    resp = client.put("/error-settings", json=_settings_payload(0.05, 0.5, 0.02))
    assert resp.json()["changed"] is False
    assert resp.json()["id"] == v1["id"]

    # any real revision inserts a new version
    resp = client.put("/error-settings", json=_settings_payload(0.08, 0.5, 0.02))
    v2 = resp.json()
    assert v2["changed"] is True
    assert v2["id"] > v1["id"]
    assert client.get("/error-settings").json()["id"] == v2["id"]


def test_error_settings_validation(client):
    _seed_publication(client)

    # gain error must be in [0, 1)
    bad_gain = _settings_payload(1.0, 0.5, 0.02)
    assert client.put("/error-settings", json=bad_gain).status_code == 422
    bad_gain["gain_error"] = -0.1
    assert client.put("/error-settings", json=bad_gain).status_code == 422

    # per-plot errors must be non-negative
    bad_weight = _settings_payload(0.05, -0.1, 0.02)
    assert client.put("/error-settings", json=bad_weight).status_code == 422
    bad_moisture = _settings_payload(0.05, 0.5, -0.01)
    assert client.put("/error-settings", json=bad_moisture).status_code == 422

    # duplicate plot keys rejected
    dup = _settings_payload(0.05, 0.5, 0.02)
    dup["entries"].append(dict(dup["entries"][0]))
    assert client.put("/error-settings", json=dup).status_code == 422

    # settings only accept imported plots
    resp = client.put(
        "/error-settings",
        json={
            "gain_error": 0.05,
            "entries": [
                {"block_id": 9, "treatment": "A", "weight_error": 0.1,
                 "moisture_error": 0.01}
            ],
        },
    )
    assert resp.status_code == 404

    assert client.get("/error-settings").status_code == 404


# ------------------------------------------------------------- report flow

def test_report_lifecycle_staleness_and_corrections(client):
    pub = _seed_publication(client)

    # no settings yet -> 422 and nothing written
    resp = client.post(f"/publications/{pub['id']}/error-reports")
    assert resp.status_code == 422
    assert client.get("/error-reports").json() == []

    settings = client.put(
        "/error-settings", json=_settings_payload(0.05, 0.3, 0.02)
    ).json()

    resp = client.post(f"/publications/{pub['id']}/error-reports")
    assert resp.status_code == 201
    report = resp.json()
    assert report["stale"] is False
    assert report["stale_reasons"] == []
    assert report["publication_id"] == pub["id"]
    assert report["error_settings_id"] == settings["id"]
    assert report["data_version"] == pub["data_version"]
    assert report["n_blocks"] == 2
    assert report["gain_bounds"] == pytest.approx([0.95, 1.05])
    assert [b["block_id"] for b in report["blocks"]] == [1, 2]
    assert report["recorded_mean_diff"] == pytest.approx(pub["mean_diff"])
    # the report retains the full settings snapshot and publication provenance
    assert report["error_settings"]["id"] == settings["id"]
    assert report["publication_provenance"]["plots"] == pub["provenance"]["plots"]
    assert report["publication_provenance"]["source_hash"] == pub["provenance"]["source_hash"]

    # identical settings PUT does not invalidate the report
    client.put("/error-settings", json=_settings_payload(0.05, 0.3, 0.02))
    assert client.get(f"/error-reports/{report['id']}").json()["stale"] is False

    # settings revision -> report stale with the precise reason
    client.put("/error-settings", json=_settings_payload(0.06, 0.3, 0.02))
    stale = client.get(f"/error-reports/{report['id']}").json()
    assert stale["stale"] is True
    assert stale["stale_reasons"] == ["error_settings_revised"]

    resp = client.post(f"/publications/{pub['id']}/error-reports")
    report2 = resp.json()
    assert report2["stale"] is False
    assert report2["id"] > report["id"]

    # correcting source data: old publication AND reports go stale, but stay
    # queryable and their stored content is never rewritten
    client.post(
        "/corrections",
        json={"corrections": [
            {"block_id": 1, "treatment": "B", "fresh_weight": 13.5}
        ]},
    )
    r1 = client.get(f"/error-reports/{report['id']}").json()
    r2 = client.get(f"/error-reports/{report2['id']}").json()
    assert r1["stale_reasons"] == ["source_data_changed", "error_settings_revised"]
    assert r2["stale_reasons"] == ["source_data_changed"]
    old_pub = client.get(f"/publications/{pub['id']}").json()
    assert old_pub["stale"] is True
    block1b = next(
        p for p in old_pub["provenance"]["plots"]
        if p["block_id"] == 1 and p["treatment"] == "B"
    )
    assert block1b["fresh_weight"] == 12.0  # historical value retained

    # a new report against the old publication works (same committed basis)
    # but is immediately stale because source data has moved on
    resp = client.post(f"/publications/{pub['id']}/error-reports")
    report3 = resp.json()
    assert resp.status_code == 201
    assert report3["stale"] is True
    assert report3["stale_reasons"] == ["source_data_changed"]
    assert report3["recorded_mean_diff"] == pytest.approx(pub["mean_diff"])

    # listing, filtering and latest
    ids = [r["id"] for r in client.get("/error-reports").json()]
    assert ids == [report3["id"], report2["id"], report["id"]]
    filtered = client.get(
        f"/error-reports?publication_id={pub['id']}"
    ).json()
    assert {r["id"] for r in filtered} == {report["id"], report2["id"], report3["id"]}
    assert client.get("/error-reports/latest").json()["id"] == report3["id"]
    assert client.get("/error-reports/999").status_code == 404
    assert client.post("/publications/999/error-reports").status_code == 404


def test_failed_report_never_overwrites_old_one(client):
    pub = _seed_publication(client)
    client.put("/error-settings", json=_settings_payload(0.05, 0.3, 0.02))
    good = client.post(f"/publications/{pub['id']}/error-reports").json()

    # replace settings with a version missing block 2 plots
    partial = {
        "gain_error": 0.05,
        "entries": [
            {"block_id": 1, "treatment": "A", "weight_error": 0.3,
             "moisture_error": 0.02},
            {"block_id": 1, "treatment": "B", "weight_error": 0.3,
             "moisture_error": 0.02},
        ],
    }
    client.put("/error-settings", json=partial)
    resp = client.post(f"/publications/{pub['id']}/error-reports")
    assert resp.status_code == 422
    assert "2A" in resp.json()["detail"]["missing"]

    # no new row appeared; the old report is intact (now settings-stale)
    rows = client.get("/error-reports").json()
    assert len(rows) == 1
    assert rows[0]["id"] == good["id"]
    assert rows[0]["stale_reasons"] == ["error_settings_revised"]


# ---------------------------------------------------------------- the math

def test_shared_gain_keeps_sign_that_independent_gains_would_lose(client):
    # B outweighs A by 10%; only the shared gain is uncertain (+-5%).
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 1.0,
                 "fresh_weight": 100.0, "moisture": 0.0},
                {"block_id": 1, "treatment": "B", "area": 1.0,
                 "fresh_weight": 110.0, "moisture": 0.0},
            ]
        },
    )
    pub = client.post("/publications").json()
    assert pub["mean_diff"] == pytest.approx(10.0)
    client.put(
        "/error-settings",
        json={
            "gain_error": 0.05,
            "entries": [
                {"block_id": 1, "treatment": t, "weight_error": 0.0,
                 "moisture_error": 0.0}
                for t in ("A", "B")
            ],
        },
    )
    report = client.post(
        f"/publications/{pub['id']}/error-reports"
    ).json()
    # one shared gain g: diff = 10*g, always strictly positive
    assert report["lower_bound"] == pytest.approx(9.5)
    assert report["upper_bound"] == pytest.approx(10.5)
    assert report["sign"] == "positive"
    assert report["sign_certain"] is True


def test_independent_plot_errors_can_flip_sign(client):
    # recorded diff = +2 per block; +-1 weighing errors on both plots make
    # the reachable interval cross zero.
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 1.0,
                 "fresh_weight": 10.0, "moisture": 0.0},
                {"block_id": 1, "treatment": "B", "area": 1.0,
                 "fresh_weight": 12.0, "moisture": 0.0},
            ]
        },
    )
    pub = client.post("/publications").json()
    client.put(
        "/error-settings",
        json={
            "gain_error": 0.1,
            "entries": [
                {"block_id": 1, "treatment": t, "weight_error": 1.0,
                 "moisture_error": 0.0}
                for t in ("A", "B")
            ],
        },
    )
    report = client.post(
        f"/publications/{pub['id']}/error-reports"
    ).json()
    # max diff at g=1.1: (12*1.1+1) - (10*1.1-1) = 4.2
    # min diff at g=0.9: (12*0.9-1) - (10*0.9+1) = -0.2
    assert report["upper_bound"] == pytest.approx(4.2)
    assert report["lower_bound"] == pytest.approx(-0.2)
    assert report["sign"] == "indeterminate"
    assert report["sign_certain"] is False


def test_moisture_only_errors_and_clipping(client):
    # identical fresh weights; moisture error +-0.1 around 0.5 drives the
    # difference B-A through [-2, 2].
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 1.0,
                 "fresh_weight": 10.0, "moisture": 0.5},
                {"block_id": 1, "treatment": "B", "area": 1.0,
                 "fresh_weight": 10.0, "moisture": 0.5},
            ]
        },
    )
    pub = client.post("/publications").json()
    client.put(
        "/error-settings",
        json={
            "gain_error": 0.0,
            "entries": [
                {"block_id": 1, "treatment": t, "weight_error": 0.0,
                 "moisture_error": 0.1}
                for t in ("A", "B")
            ],
        },
    )
    report = client.post(
        f"/publications/{pub['id']}/error-reports"
    ).json()
    assert report["lower_bound"] == pytest.approx(-2.0)
    assert report["upper_bound"] == pytest.approx(2.0)
    assert report["sign"] == "indeterminate"

    # moisture clipped to [0, 1]: recorded 0.0 +- 0.1 stays in [0, 0.1]
    lo, hi = plot_dry_extrema(
        fresh_weight=10.0, moisture=0.0, area=1.0, gain=1.0,
        weight_error=0.0, moisture_error=0.1,
    )
    assert lo == pytest.approx(9.0)
    assert hi == pytest.approx(10.0)
    # recorded 0.95 +- 0.1 stays in [0.85, 1]
    lo, hi = plot_dry_extrema(
        fresh_weight=10.0, moisture=0.95, area=1.0, gain=1.0,
        weight_error=0.0, moisture_error=0.1,
    )
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(1.5)


def test_true_weight_never_negative(client):
    # fresh weight 0 with a +-2 weighing error: true weight in [0, 2],
    # the dry matter minimum must be exactly 0.
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 1.0,
                 "fresh_weight": 0.0, "moisture": 0.2},
                {"block_id": 1, "treatment": "B", "area": 1.0,
                 "fresh_weight": 5.0, "moisture": 0.2},
            ]
        },
    )
    pub = client.post("/publications").json()
    client.put(
        "/error-settings",
        json={
            "gain_error": 0.5,
            "entries": [
                {"block_id": 1, "treatment": t, "weight_error": 2.0,
                 "moisture_error": 0.0}
                for t in ("A", "B")
            ],
        },
    )
    report = client.post(
        f"/publications/{pub['id']}/error-reports"
    ).json()
    # A can be as light as 0 (never negative); B as light as max(0, 5g-2).
    # B-A minimum: g=0.5 -> B weight 0.5, A weight 2 -> 0.5*0.8 - 2*0.8
    assert report["lower_bound"] == pytest.approx((0.5 - 2.0) * 0.8)
    assert report["lower_bound"] < 0
    # every achievable dry matter is non-negative: dense check below
    # (bounds math uses the clamp directly; assert no negative weights slip in)
    for g in (0.5, 0.75, 1.0, 1.25, 1.5):
        lo_a, _ = plot_dry_extrema(0.0, 0.2, 1.0, g, 2.0, 0.0)
        assert lo_a == 0.0


# ------------------------------------------------- independent brute-force

def _brute_force_bounds(blocks, errors, gain_error, grid=401, seed=7):
    """Independent scan: dense g grid + random interior error draws."""
    rng = random.Random(seed)
    g_values = [1.0 - gain_error + (2.0 * gain_error) * i / (grid - 1)
                for i in range(grid)]
    # add every clamp kink explicitly
    for block in blocks:
        for t in ("A", "B"):
            plot = block["plot_a"] if t == "A" else block["plot_b"]
            spec = errors[(block["block_id"], t)]
            if plot["fresh_weight"] > 0 and spec["weight_error"] > 0:
                g_values.append(spec["weight_error"] / plot["fresh_weight"])

    lo = float("inf")
    hi = float("-inf")
    n = len(blocks)
    for g in g_values:
        if not (1.0 - gain_error - 1e-15 <= g <= 1.0 + gain_error + 1e-15):
            continue
        # exact fixed-g extrema from endpoint combinations
        fixed_lo = fixed_hi = 0.0
        for block in blocks:
            for t in ("A", "B"):
                plot = block["plot_a"] if t == "A" else block["plot_b"]
                spec = errors[(block["block_id"], t)]
                d_lo, d_hi = plot_dry_extrema(
                    plot["fresh_weight"], plot["moisture"], plot["area"], g,
                    spec["weight_error"], spec["moisture_error"],
                )
                fixed_hi += d_hi if t == "B" else -d_lo
                fixed_lo += d_lo if t == "B" else -d_hi
        lo = min(lo, fixed_lo / n)
        hi = max(hi, fixed_hi / n)

        # random interior draws must always stay inside the extrema
        for _ in range(20):
            mean_diff = 0.0
            for block in blocks:
                for t in ("A", "B"):
                    plot = block["plot_a"] if t == "A" else block["plot_b"]
                    spec = errors[(block["block_id"], t)]
                    w = max(
                        0.0,
                        plot["fresh_weight"] * g
                        + rng.uniform(-spec["weight_error"], spec["weight_error"]),
                    )
                    q = min(
                        1.0,
                        max(
                            0.0,
                            plot["moisture"]
                            + rng.uniform(
                                -spec["moisture_error"], spec["moisture_error"]
                            ),
                        ),
                    )
                    dry = w * (1.0 - q) / plot["area"]
                    mean_diff += dry if t == "B" else -dry
            mean_diff /= n
            assert lo - 1e-12 <= mean_diff <= hi + 1e-12
    return lo, hi


def test_bounds_match_brute_force_on_many_configs(client):
    from app.analysis import mean_difference_bounds

    rng = random.Random(42)
    configs = [
        # gain, weight, moisture errors exercised over random two-block trials
        (0.0, 0.0, 0.0),
        (0.05, 0.3, 0.02),
        (0.5, 2.0, 0.25),   # clamps and kinks everywhere
        (0.9, 5.0, 0.5),
        (0.2, 0.0, 0.1),
        (0.2, 1.5, 0.0),
    ]
    for gain_err, w_err, m_err in configs:
        blocks = []
        errors = {}
        for block_id in (1, 2, 3):
            pair = {}
            for t in ("A", "B"):
                plot = {
                    "fresh_weight": rng.choice(
                        [0.0, 0.5, 1.0, 4.0, 10.0, 25.0]
                    ),
                    "moisture": rng.choice([0.0, 0.2, 0.5, 0.9, 0.99]),
                    "area": rng.choice([1.0, 2.0, 3.5]),
                }
                pair[t] = plot
                errors[(block_id, t)] = {
                    "weight_error": w_err,
                    "moisture_error": m_err,
                }
            blocks.append(
                {"block_id": block_id, "plot_a": pair["A"], "plot_b": pair["B"]}
            )
        analytic_lo, analytic_hi = mean_difference_bounds(
            blocks, errors, gain_err
        )
        brute_lo, brute_hi = _brute_force_bounds(blocks, errors, gain_err)
        assert analytic_lo == pytest.approx(brute_lo, abs=1e-12)
        assert analytic_hi == pytest.approx(brute_hi, abs=1e-12)
        assert analytic_lo <= analytic_hi
