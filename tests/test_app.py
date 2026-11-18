"""End-to-end tests for the fertilizer RBD trial API.

Each test runs against a fresh temporary SQLite database (DB_PATH is read
at connect time).  Expected p-values are checked two ways: an exact
Fraction-based enumeration over the response's own block differences, and
a handcrafted dataset with a known ground-truth p-value.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from itertools import product

import pytest
from fastapi.testclient import TestClient

from app.main import app

IMPORT_6_BLOCKS = {
    "plots": [
        {"block_id": 1, "treatment": "A", "area": 30.0, "fresh_weight": 42.5},
        {"block_id": 1, "treatment": "B", "area": 30.0, "fresh_weight": 47.8},
        {"block_id": 2, "treatment": "A", "area": 28.5, "fresh_weight": 39.2},
        {"block_id": 2, "treatment": "B", "area": 28.5, "fresh_weight": 44.6},
        {"block_id": 3, "treatment": "A", "area": 32.0, "fresh_weight": 45.1},
        {"block_id": 3, "treatment": "B", "area": 32.0, "fresh_weight": 49.3},
        {"block_id": 4, "treatment": "A", "area": 30.5, "fresh_weight": 41.0},
        {"block_id": 4, "treatment": "B", "area": 30.5, "fresh_weight": 44.9},
        {"block_id": 5, "treatment": "A", "area": 29.0, "fresh_weight": 40.2},
        {"block_id": 5, "treatment": "B", "area": 29.0, "fresh_weight": 45.5},
        {"block_id": 6, "treatment": "A", "area": 31.5, "fresh_weight": 43.8},
        {"block_id": 6, "treatment": "B", "area": 31.5, "fresh_weight": 46.0},
    ]
}

MOISTURE_BLOCKS_1_4 = {
    "entries": [
        {"block_id": 1, "treatment": "A", "moisture": 0.62},
        {"block_id": 1, "treatment": "B", "moisture": 0.63},
        {"block_id": 2, "treatment": "A", "moisture": 0.60},
        {"block_id": 2, "treatment": "B", "moisture": 0.61},
        {"block_id": 3, "treatment": "A", "moisture": 0.65},
        {"block_id": 3, "treatment": "B", "moisture": 0.64},
        {"block_id": 4, "treatment": "A", "moisture": 0.58},
        {"block_id": 4, "treatment": "B", "moisture": 0.59},
    ]
}

MOISTURE_BLOCKS_5_6 = {
    "entries": [
        {"block_id": 5, "treatment": "A", "moisture": 0.61},
        {"block_id": 5, "treatment": "B", "moisture": 0.62},
        {"block_id": 6, "treatment": "A", "moisture": 0.63},
        {"block_id": 6, "treatment": "B", "moisture": 0.64},
    ]
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "trial.db"))
    with TestClient(app) as test_client:
        yield test_client


def exact_p_fraction(diffs: list[float]) -> Fraction:
    """Independent exact check: enumerate all 2^n sign assignments."""
    n = len(diffs)
    fracs = [Fraction(d) for d in diffs]
    observed = abs(sum(fracs) / n)
    hits = sum(
        1
        for signs in product((1, -1), repeat=n)
        if abs(sum(s * d for s, d in zip(signs, fracs)) / n) >= observed
    )
    return Fraction(hits, 2**n)


def expected_diffs(client) -> list[float]:
    """Recompute B-A dry matter differences from the raw /plots data."""
    plots = client.get("/plots").json()["plots"]
    by_block: dict[int, dict[str, dict]] = {}
    for plot in plots:
        by_block.setdefault(plot["block_id"], {})[plot["treatment"]] = plot
    diffs = []
    for block_id in sorted(by_block):
        pair = by_block[block_id]
        if any(pair[t]["moisture"] is None for t in ("A", "B")):
            continue
        dry = {
            t: pair[t]["fresh_weight"] * (1 - pair[t]["moisture"]) / pair[t]["area"]
            for t in ("A", "B")
        }
        diffs.append(dry["B"] - dry["A"])
    return diffs


def assert_publication_matches_data(client, publication: dict) -> None:
    diffs = expected_diffs(client)
    assert len(publication["blocks"]) == len(diffs)
    for block, diff in zip(publication["blocks"], diffs):
        assert block["diff"] == pytest.approx(diff, abs=1e-12)
    assert publication["mean_diff"] == pytest.approx(sum(diffs) / len(diffs), abs=1e-12)
    assert publication["n_permutations"] == 2 ** len(diffs)
    assert Fraction(publication["p_value"]).limit_denominator(10**9) == exact_p_fraction(
        [b["diff"] for b in publication["blocks"]]
    )


# ---------------------------------------------------------------- workflow

def test_full_workflow_import_moisture_publish_late_correction(client):
    # 1. import 6 blocks; with no moisture at all, publish must fail
    resp = client.post("/imports", json=IMPORT_6_BLOCKS)
    assert resp.status_code == 200
    assert resp.json()["changed"] == 12

    resp = client.post("/publications")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"].startswith("no complete block")
    assert client.get("/publications").json() == []

    # 2. moisture for blocks 1-4 -> blocks 5 and 6 stay pending
    resp = client.post("/moisture", json=MOISTURE_BLOCKS_1_4)
    assert resp.status_code == 200
    status = client.get("/blocks").json()
    assert [b["block_id"] for b in status["complete"]] == [1, 2, 3, 4]
    assert [b["block_id"] for b in status["pending"]] == [5, 6]

    resp = client.post("/publications")
    assert resp.status_code == 201
    pub1 = resp.json()
    assert pub1["n_blocks"] == 4
    assert pub1["stale"] is False
    assert [p["block_id"] for p in pub1["pending"]] == [5, 6]
    assert_publication_matches_data(client, pub1)

    # 3. late moisture for blocks 5 and 6 -> pub1 becomes stale
    client.post("/moisture", json=MOISTURE_BLOCKS_5_6)
    assert client.get(f"/publications/{pub1['id']}").json()["stale"] is True

    resp = client.post("/publications")
    pub2 = resp.json()
    assert pub2["n_blocks"] == 6
    assert pub2["stale"] is False
    assert pub2["data_version"] > pub1["data_version"]
    assert_publication_matches_data(client, pub2)

    # 4. correction of one moisture -> pub2 stale, republish same-version
    resp = client.post(
        "/corrections",
        json={"corrections": [{"block_id": 3, "treatment": "B", "moisture": 0.70}]},
    )
    assert resp.status_code == 200
    assert client.get(f"/publications/{pub2['id']}").json()["stale"] is True

    resp = client.post("/publications")
    pub3 = resp.json()
    assert_publication_matches_data(client, pub3)
    assert pub3["p_value"] != pub2["p_value"] or pub3["mean_diff"] != pub2["mean_diff"]

    # provenance is same-version: it mirrors the current raw data exactly
    current = client.get("/plots").json()
    assert pub3["data_version"] == current["data_version"]
    assert pub3["provenance"]["data_version"] == current["data_version"]
    assert pub3["provenance"]["plots"] == current["plots"]
    block3b = next(
        p for p in pub3["provenance"]["plots"]
        if p["block_id"] == 3 and p["treatment"] == "B"
    )
    assert block3b["moisture"] == 0.70
    digest = hashlib.sha256(
        json.dumps(pub3["provenance"]["plots"], sort_keys=True).encode()
    ).hexdigest()
    assert pub3["provenance"]["source_hash"] == digest

    # exactly one non-stale publication remains
    summaries = client.get("/publications").json()
    assert [s["id"] for s in summaries] == [pub3["id"], pub2["id"], pub1["id"]]
    assert [s["stale"] for s in summaries] == [False, True, True]


def test_failed_publish_keeps_old_conclusion(client):
    client.post("/imports", json=IMPORT_6_BLOCKS)
    client.post("/moisture", json=MOISTURE_BLOCKS_1_4)
    pub1 = client.post("/publications").json()
    assert pub1["stale"] is False

    # clear every moisture value -> all blocks incomplete -> publish fails
    clear = {
        "corrections": [
            {"block_id": b, "treatment": t, "moisture": None}
            for b in (1, 2, 3, 4)
            for t in ("A", "B")
        ]
    }
    resp = client.post("/corrections", json=clear)
    assert resp.status_code == 200
    assert client.get("/blocks").json()["complete"] == []

    resp = client.post("/publications")
    assert resp.status_code == 422
    assert len(resp.json()["detail"]["pending"]) == 6

    # the old conclusion is still queryable, flagged stale, source retained
    old = client.get(f"/publications/{pub1['id']}").json()
    assert old["stale"] is True
    assert old["n_blocks"] == 4
    assert old["provenance"]["data_version"] == pub1["data_version"]
    assert len(old["provenance"]["plots"]) == 12
    assert client.get("/publications/latest").json()["id"] == pub1["id"]


def test_known_p_value_ground_truth(client):
    # dry matter: block1 A=1, B=2 (diff +1); block2 A=1, B=2 (diff +1)
    # observed mean = 1; sign assignments give means [+1, 0, 0, -1]
    # -> |stat| >= 1 in 2 of 4 permutations -> p = 0.5
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 1.0, "fresh_weight": 1.0},
                {"block_id": 1, "treatment": "B", "area": 1.0, "fresh_weight": 2.0},
                {"block_id": 2, "treatment": "A", "area": 1.0, "fresh_weight": 1.0},
                {"block_id": 2, "treatment": "B", "area": 1.0, "fresh_weight": 2.0},
            ]
        },
    )
    client.post(
        "/moisture",
        json={
            "entries": [
                {"block_id": b, "treatment": t, "moisture": 0.0}
                for b in (1, 2)
                for t in ("A", "B")
            ]
        },
    )
    pub = client.post("/publications").json()
    assert pub["mean_diff"] == pytest.approx(1.0, abs=1e-12)
    assert pub["p_value"] == pytest.approx(0.5, abs=1e-12)
    assert pub["n_permutations"] == 4


def test_pending_reasons(client):
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 30.0, "fresh_weight": 40.0},
                {"block_id": 1, "treatment": "B", "area": 30.0, "fresh_weight": 45.0},
                {"block_id": 2, "treatment": "A", "area": 30.0, "fresh_weight": 41.0},
            ]
        },
    )
    status = client.get("/blocks").json()
    assert status["complete"] == []
    pending = {p["block_id"]: p["missing"] for p in status["pending"]}
    assert pending[1] == ["A.moisture missing", "B.moisture missing"]
    assert pending[2] == ["A.moisture missing", "B: plot not imported"]


def test_noop_mutation_does_not_invalidate(client):
    client.post("/imports", json=IMPORT_6_BLOCKS)
    client.post("/moisture", json=MOISTURE_BLOCKS_1_4)
    pub = client.post("/publications").json()

    # identical re-import and re-moisture change nothing -> not stale
    resp = client.post("/imports", json=IMPORT_6_BLOCKS)
    assert resp.json()["changed"] == 0
    resp = client.post("/moisture", json=MOISTURE_BLOCKS_1_4)
    assert resp.json()["changed"] == 0
    assert client.get(f"/publications/{pub['id']}").json()["stale"] is False


def test_import_can_carry_moisture(client):
    client.post(
        "/imports",
        json={
            "plots": [
                {"block_id": 1, "treatment": "A", "area": 10.0, "fresh_weight": 20.0,
                 "moisture": 0.5},
                {"block_id": 1, "treatment": "B", "area": 10.0, "fresh_weight": 24.0,
                 "moisture": 0.5},
            ]
        },
    )
    pub = client.post("/publications")
    assert pub.status_code == 201
    assert pub.json()["blocks"][0]["diff"] == pytest.approx(0.2, abs=1e-12)


# ---------------------------------------------------------------- validation

def test_validation_errors(client):
    bad_block = {"plots": [{"block_id": 13, "treatment": "A", "area": 1.0,
                            "fresh_weight": 1.0}]}
    assert client.post("/imports", json=bad_block).status_code == 422

    bad_treatment = {"plots": [{"block_id": 1, "treatment": "C", "area": 1.0,
                                "fresh_weight": 1.0}]}
    assert client.post("/imports", json=bad_treatment).status_code == 422

    duplicate = {"plots": [
        {"block_id": 1, "treatment": "A", "area": 1.0, "fresh_weight": 1.0},
        {"block_id": 1, "treatment": "A", "area": 2.0, "fresh_weight": 2.0},
    ]}
    assert client.post("/imports", json=duplicate).status_code == 422

    bad_moisture = {"entries": [{"block_id": 1, "treatment": "A", "moisture": 1.0}]}
    assert client.post("/moisture", json=bad_moisture).status_code == 422

    empty_correction = {"corrections": [{"block_id": 1, "treatment": "A"}]}
    assert client.post("/corrections", json=empty_correction).status_code == 422


def test_moisture_and_correction_require_existing_plot(client):
    resp = client.post(
        "/moisture", json={"entries": [{"block_id": 1, "treatment": "A", "moisture": 0.5}]}
    )
    assert resp.status_code == 404
    resp = client.post(
        "/corrections",
        json={"corrections": [{"block_id": 1, "treatment": "A", "area": 5.0}]},
    )
    assert resp.status_code == 404


def test_unknown_publication_404(client):
    assert client.get("/publications/999").status_code == 404
    assert client.get("/publications/latest").status_code == 404
