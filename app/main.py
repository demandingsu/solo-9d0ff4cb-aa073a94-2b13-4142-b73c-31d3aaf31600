"""FastAPI service for the randomized block fertilizer trial.

Workflow: import plots (area + fresh weight) -> record moisture -> publish.
A publication reports per-block B-A dry matter differences, the mean
difference and a two-sided exact randomization p-value, computed from one
consistent data snapshot.  Later imports/corrections mark older publications
stale; a failed publish never touches stored conclusions.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from . import db
from .analysis import evaluate_blocks, exact_randomization_p
from .schemas import (
    BlockStatus,
    CorrectionRequest,
    ImportRequest,
    MoistureRequest,
    MutationResponse,
    ProvenancePlot,
    Publication,
    PublicationSummary,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Fertilizer RBD Trial API",
    version="1.0.0",
    description=(
        "Randomized complete block trial (up to 12 blocks, treatments A/B). "
        "Dry matter = fresh_weight * (1 - moisture) / area. Publications "
        "report per-block B-A differences, the mean difference and a "
        "two-sided exact randomization p-value over all 2^n within-block "
        "A/B swaps. Blocks lacking any required measurement stay pending; "
        "if no block is complete, nothing is published."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------- helpers

def _fetch_plot(conn, block_id: int, treatment: str):
    return conn.execute(
        "SELECT block_id, treatment, area, fresh_weight, moisture, updated_at"
        " FROM plots WHERE block_id = ? AND treatment = ?",
        (block_id, treatment),
    ).fetchone()


def _publication_from_row(row, current_version: int) -> Publication:
    payload = json.loads(row["payload"])
    return Publication(
        id=row["id"],
        created_at=row["created_at"],
        data_version=row["data_version"],
        stale=row["data_version"] != current_version,
        n_blocks=row["n_blocks"],
        mean_diff=row["mean_diff"],
        p_value=row["p_value"],
        n_permutations=row["n_permutations"],
        blocks=payload["blocks"],
        pending=payload["pending"],
        provenance=payload["provenance"],
    )


def _get_publication(conn, publication_id: int) -> Publication:
    row = conn.execute(
        "SELECT * FROM publications WHERE id = ?", (publication_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"publication {publication_id} not found")
    return _publication_from_row(row, db.get_data_version(conn))


# ---------------------------------------------------------------- meta

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- mutations

@app.post("/imports", response_model=MutationResponse)
def import_plots(req: ImportRequest) -> MutationResponse:
    """Import (or re-import) plot area and fresh weight for up to 12 blocks.

    Existing plots are updated; a moisture value already recorded is kept
    unless a new one is supplied here.
    """
    changed = unchanged = 0
    with db.session(write=True) as conn:
        now = _now()
        for plot in req.plots:
            existing = _fetch_plot(conn, plot.block_id, plot.treatment)
            moisture = (
                plot.moisture
                if plot.moisture is not None
                else (existing["moisture"] if existing else None)
            )
            if existing and (
                existing["area"],
                existing["fresh_weight"],
                existing["moisture"],
            ) == (plot.area, plot.fresh_weight, moisture):
                unchanged += 1
                continue
            conn.execute(
                "INSERT INTO plots (block_id, treatment, area, fresh_weight, moisture, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (block_id, treatment) DO UPDATE SET"
                "   area = excluded.area,"
                "   fresh_weight = excluded.fresh_weight,"
                "   moisture = excluded.moisture,"
                "   updated_at = excluded.updated_at",
                (plot.block_id, plot.treatment, plot.area, plot.fresh_weight, moisture, now),
            )
            changed += 1
        version = db.bump_data_version(conn) if changed else db.get_data_version(conn)
    return MutationResponse(data_version=version, changed=changed, unchanged=unchanged)


@app.post("/moisture", response_model=MutationResponse)
def record_moisture(req: MoistureRequest) -> MutationResponse:
    """Record moisture assay results for already imported plots."""
    changed = unchanged = 0
    with db.session(write=True) as conn:
        missing = [
            f"{e.block_id}{e.treatment}"
            for e in req.entries
            if _fetch_plot(conn, e.block_id, e.treatment) is None
        ]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"plots not imported yet: {', '.join(missing)}",
            )
        now = _now()
        for entry in req.entries:
            existing = _fetch_plot(conn, entry.block_id, entry.treatment)
            if existing["moisture"] == entry.moisture:
                unchanged += 1
                continue
            conn.execute(
                "UPDATE plots SET moisture = ?, updated_at = ?"
                " WHERE block_id = ? AND treatment = ?",
                (entry.moisture, now, entry.block_id, entry.treatment),
            )
            changed += 1
        version = db.bump_data_version(conn) if changed else db.get_data_version(conn)
    return MutationResponse(data_version=version, changed=changed, unchanged=unchanged)


@app.post("/corrections", response_model=MutationResponse)
def correct_plots(req: CorrectionRequest) -> MutationResponse:
    """Correct area, fresh weight and/or moisture of existing plots.

    Only provided fields are changed; ``"moisture": null`` clears a wrong
    moisture value so the block goes back to pending.
    """
    changed = unchanged = 0
    with db.session(write=True) as conn:
        missing = [
            f"{c.block_id}{c.treatment}"
            for c in req.corrections
            if _fetch_plot(conn, c.block_id, c.treatment) is None
        ]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"plots not imported yet: {', '.join(missing)}",
            )
        now = _now()
        for correction in req.corrections:
            existing = _fetch_plot(conn, correction.block_id, correction.treatment)
            updates: dict[str, Any] = {}
            for field in ("area", "fresh_weight", "moisture"):
                if field in correction.model_fields_set:
                    updates[field] = getattr(correction, field)
            if all(existing[field] == value for field, value in updates.items()):
                unchanged += 1
                continue
            assignments = ", ".join(f"{field} = ?" for field in updates)
            conn.execute(
                f"UPDATE plots SET {assignments}, updated_at = ?"
                " WHERE block_id = ? AND treatment = ?",
                (*updates.values(), now, correction.block_id, correction.treatment),
            )
            changed += 1
        version = db.bump_data_version(conn) if changed else db.get_data_version(conn)
    return MutationResponse(data_version=version, changed=changed, unchanged=unchanged)


# ---------------------------------------------------------------- status

@app.get("/plots", response_model=dict)
def list_plots() -> dict[str, Any]:
    """Current raw plot data and data version."""
    with db.session() as conn:
        rows = db.fetch_plots(conn)
        return {
            "data_version": db.get_data_version(conn),
            "plots": [dict(row) for row in rows],
        }


@app.get("/blocks", response_model=BlockStatus)
def block_status() -> BlockStatus:
    """Which blocks are analysable now and which are pending (and why)."""
    with db.session() as conn:
        complete, pending = evaluate_blocks(db.fetch_plots(conn))
        return BlockStatus(
            data_version=db.get_data_version(conn),
            complete=complete,
            pending=pending,
        )


# ---------------------------------------------------------------- publications

@app.post("/publications", status_code=201, response_model=Publication)
def publish() -> Publication:
    """Publish the conclusion from the current data snapshot.

    Fails with 422 when no block has both treatments fully measured; stored
    publications are never modified by a failed attempt.
    """
    with db.session(write=True) as conn:
        version = db.get_data_version(conn)
        rows = db.fetch_plots(conn)
        complete, pending = evaluate_blocks(rows)
        if not complete:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "no complete block: nothing published",
                    "pending": pending,
                },
            )
        diffs = [block["diff"] for block in complete]
        mean_diff = sum(diffs) / len(diffs)
        p_value, n_permutations = exact_randomization_p(diffs)
        provenance_plots = [ProvenancePlot(**dict(row)).model_dump() for row in rows]
        source_hash = hashlib.sha256(
            json.dumps(provenance_plots, sort_keys=True).encode()
        ).hexdigest()
        payload = {
            "blocks": complete,
            "pending": pending,
            "provenance": {
                "data_version": version,
                "source_hash": source_hash,
                "plots": provenance_plots,
            },
        }
        cursor = conn.execute(
            "INSERT INTO publications"
            " (created_at, data_version, n_blocks, mean_diff, p_value, n_permutations, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), version, len(complete), mean_diff, p_value, n_permutations,
             json.dumps(payload)),
        )
        row = conn.execute(
            "SELECT * FROM publications WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _publication_from_row(row, version)


@app.get("/publications", response_model=list[PublicationSummary])
def list_publications() -> list[PublicationSummary]:
    """All publications, newest first, with staleness flags."""
    with db.session() as conn:
        version = db.get_data_version(conn)
        rows = conn.execute(
            "SELECT * FROM publications ORDER BY id DESC"
        ).fetchall()
        return [
            PublicationSummary(
                id=row["id"],
                created_at=row["created_at"],
                data_version=row["data_version"],
                stale=row["data_version"] != version,
                n_blocks=row["n_blocks"],
                mean_diff=row["mean_diff"],
                p_value=row["p_value"],
                n_permutations=row["n_permutations"],
            )
            for row in rows
        ]


@app.get("/publications/latest", response_model=Publication)
def latest_publication() -> Publication:
    """The most recent publication (may be stale if data changed since)."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM publications ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no publications yet")
        return _publication_from_row(row, db.get_data_version(conn))


@app.get("/publications/{publication_id}", response_model=Publication)
def get_publication(publication_id: int) -> Publication:
    """A stored publication with its full provenance, stale flag included."""
    with db.session() as conn:
        return _get_publication(conn, publication_id)
