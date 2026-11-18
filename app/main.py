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
from .analysis import (
    classify_sign,
    evaluate_blocks,
    exact_randomization_p,
    mean_difference_bounds,
)
from .schemas import (
    BlockStatus,
    CorrectionRequest,
    ErrorReport,
    ErrorReportBlock,
    ErrorReportSummary,
    ErrorSettingsRequest,
    ErrorSettingsResponse,
    ImportRequest,
    MoistureRequest,
    MutationResponse,
    PlotErrorEntry,
    Provenance,
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
    version="1.1.0",
    description=(
        "Randomized complete block trial (up to 12 blocks, treatments A/B). "
        "Dry matter = fresh_weight * (1 - moisture) / area. Publications "
        "report per-block B-A differences, the mean difference and a "
        "two-sided exact randomization p-value over all 2^n within-block "
        "A/B swaps. Blocks lacking any required measurement stay pending; "
        "if no block is complete, nothing is published. "
        "Error-range reports (/error-settings and /error-reports) compute "
        "exact reachable bounds of the published mean difference under an "
        "experiment-wide shared scale gain in [1-e, 1+e] plus independent "
        "per-plot weighing and moisture errors, and say whether the sign "
        "of B-A is determined."
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


def _settings_response(conn, settings_id: int, changed: bool) -> ErrorSettingsResponse:
    header, plot_rows = db.fetch_error_settings(conn, settings_id)
    if header is None:
        raise HTTPException(status_code=404, detail=f"error settings {settings_id} not found")
    return ErrorSettingsResponse(
        id=header["id"],
        created_at=header["created_at"],
        gain_error=header["gain_error"],
        entries=[PlotErrorEntry(**dict(row)) for row in plot_rows],
        changed=changed,
    )


def _report_stale_reasons(row, current_version: int, current_settings_id: int | None):
    reasons: list[str] = []
    if row["data_version"] != current_version:
        reasons.append("source_data_changed")
    if current_settings_id != row["error_settings_id"]:
        reasons.append("error_settings_revised")
    return reasons


def _error_report_from_row(row, current_version: int, current_settings_id: int | None) -> ErrorReport:
    payload = json.loads(row["payload"])
    reasons = _report_stale_reasons(row, current_version, current_settings_id)
    return ErrorReport(
        id=row["id"],
        created_at=row["created_at"],
        publication_id=row["publication_id"],
        data_version=row["data_version"],
        error_settings_id=row["error_settings_id"],
        n_blocks=row["n_blocks"],
        recorded_mean_diff=row["recorded_mean_diff"],
        lower_bound=row["lower_bound"],
        upper_bound=row["upper_bound"],
        sign=row["sign"],
        sign_certain=bool(row["sign_certain"]),
        stale=bool(reasons),
        stale_reasons=reasons,
        gain_bounds=tuple(payload["gain_bounds"]),
        blocks=payload["blocks"],
        error_settings=payload["error_settings"],
        publication_provenance=payload["publication_provenance"],
    )


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


# ------------------------------------------------------- measurement errors

@app.put("/error-settings", response_model=ErrorSettingsResponse)
def put_error_settings(req: ErrorSettingsRequest) -> ErrorSettingsResponse:
    """Replace the measurement-error settings, creating a new version.

    The gain error e is experiment-wide and shared; weighing/moisture error
    magnitudes are per plot and independent.  Submitting exactly the current
    settings is a no-op (the same id is returned), so existing reports are
    not invalidated.  Every referenced plot must already be imported.
    """
    with db.session(write=True) as conn:
        missing = [
            f"{e.block_id}{e.treatment}"
            for e in req.entries
            if db.fetch_plot(conn, e.block_id, e.treatment) is None
        ]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"plots not imported yet: {', '.join(missing)}",
            )

        current_id = db.latest_error_settings_id(conn)
        if current_id is not None:
            header, plot_rows = db.fetch_error_settings(conn, current_id)
            same_gain = header["gain_error"] == req.gain_error
            same_plots = [dict(r) for r in plot_rows] == [
                e.model_dump() for e in req.entries
            ]
            if same_gain and same_plots:
                return _settings_response(conn, current_id, changed=False)

        cursor = conn.execute(
            "INSERT INTO error_settings (created_at, gain_error) VALUES (?, ?)",
            (_now(), req.gain_error),
        )
        settings_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO error_plot_settings"
            " (settings_id, block_id, treatment, weight_error, moisture_error)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (settings_id, e.block_id, e.treatment, e.weight_error, e.moisture_error)
                for e in req.entries
            ],
        )
        return _settings_response(conn, settings_id, changed=True)


@app.get("/error-settings", response_model=ErrorSettingsResponse)
def get_error_settings() -> ErrorSettingsResponse:
    """The current (latest) measurement-error settings version."""
    with db.session() as conn:
        settings_id = db.latest_error_settings_id(conn)
        if settings_id is None:
            raise HTTPException(status_code=404, detail="no error settings defined yet")
        return _settings_response(conn, settings_id, changed=False)


@app.post(
    "/publications/{publication_id}/error-reports",
    status_code=201,
    response_model=ErrorReport,
)
def create_error_report(publication_id: int) -> ErrorReport:
    """Compute the reachable error range of a publication's mean difference.

    The report is built entirely inside one IMMEDIATE transaction from one
    committed state: the stored publication (its raw provenance rows and
    participating blocks) and the current error settings.  If the settings
    do not cover every participating plot, or the publication is unknown,
    the request fails with 4xx and no report row is written, so older
    reports are never overwritten.
    """
    with db.session(write=True) as conn:
        pub_row = conn.execute(
            "SELECT * FROM publications WHERE id = ?", (publication_id,)
        ).fetchone()
        if pub_row is None:
            raise HTTPException(
                status_code=404, detail=f"publication {publication_id} not found"
            )
        settings_id = db.latest_error_settings_id(conn)
        if settings_id is None:
            raise HTTPException(
                status_code=422,
                detail="no error settings defined yet: PUT /error-settings first",
            )

        header, setting_rows = db.fetch_error_settings(conn, settings_id)
        errors = {
            (r["block_id"], r["treatment"]): {
                "weight_error": r["weight_error"],
                "moisture_error": r["moisture_error"],
            }
            for r in setting_rows
        }

        pub_payload = json.loads(pub_row["payload"])
        provenance_plots = pub_payload["provenance"]["plots"]
        source = {
            (p["block_id"], p["treatment"]): p for p in provenance_plots
        }
        blocks_for_analysis = []
        report_blocks = []
        missing_settings: list[str] = []
        for block in pub_payload["blocks"]:
            block_id = block["block_id"]
            report_blocks.append(
                ErrorReportBlock(block_id=block_id, recorded_diff=block["diff"])
            )
            pair = {}
            for treatment in ("A", "B"):
                if (block_id, treatment) not in errors:
                    missing_settings.append(f"{block_id}{treatment}")
                pair[treatment] = source[(block_id, treatment)]
            blocks_for_analysis.append(
                {
                    "block_id": block_id,
                    "plot_a": pair["A"],
                    "plot_b": pair["B"],
                }
            )
        if missing_settings:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "error settings miss plots of participating blocks",
                    "missing": missing_settings,
                },
            )

        gain_error = header["gain_error"]
        lower, upper = mean_difference_bounds(
            blocks_for_analysis, errors, gain_error
        )
        sign, sign_certain = classify_sign(lower, upper)

        settings_snapshot = _settings_response(conn, settings_id, changed=False)
        payload = {
            "gain_bounds": [1.0 - gain_error, 1.0 + gain_error],
            "blocks": [b.model_dump() for b in report_blocks],
            "error_settings": settings_snapshot.model_dump(),
            "publication_provenance": pub_payload["provenance"],
        }
        cursor = conn.execute(
            "INSERT INTO error_reports"
            " (created_at, publication_id, data_version, error_settings_id,"
            "  n_blocks, recorded_mean_diff, lower_bound, upper_bound,"
            "  sign, sign_certain, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                publication_id,
                pub_row["data_version"],
                settings_id,
                len(report_blocks),
                pub_row["mean_diff"],
                lower,
                upper,
                sign,
                int(sign_certain),
                json.dumps(payload),
            ),
        )
        row = conn.execute(
            "SELECT * FROM error_reports WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _error_report_from_row(
            row, db.get_data_version(conn), settings_id
        )


@app.get("/error-reports", response_model=list[ErrorReportSummary])
def list_error_reports(publication_id: int | None = None) -> list[ErrorReportSummary]:
    """All error-range reports, newest first; optionally filter by publication."""
    with db.session() as conn:
        current_version = db.get_data_version(conn)
        current_settings_id = db.latest_error_settings_id(conn)
        if publication_id is None:
            rows = conn.execute(
                "SELECT * FROM error_reports ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM error_reports WHERE publication_id = ? ORDER BY id DESC",
                (publication_id,),
            ).fetchall()
        return [
            ErrorReportSummary(
                id=r["id"],
                created_at=r["created_at"],
                publication_id=r["publication_id"],
                data_version=r["data_version"],
                error_settings_id=r["error_settings_id"],
                n_blocks=r["n_blocks"],
                recorded_mean_diff=r["recorded_mean_diff"],
                lower_bound=r["lower_bound"],
                upper_bound=r["upper_bound"],
                sign=r["sign"],
                sign_certain=bool(r["sign_certain"]),
                stale=bool(_report_stale_reasons(r, current_version, current_settings_id)),
                stale_reasons=_report_stale_reasons(
                    r, current_version, current_settings_id
                ),
            )
            for r in rows
        ]


@app.get("/error-reports/latest", response_model=ErrorReport)
def latest_error_report() -> ErrorReport:
    """The most recent error-range report (may already be stale)."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM error_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no error reports yet")
        return _error_report_from_row(
            row, db.get_data_version(conn), db.latest_error_settings_id(conn)
        )


@app.get("/error-reports/{report_id}", response_model=ErrorReport)
def get_error_report(report_id: int) -> ErrorReport:
    """A stored error-range report with its full settings and provenance."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM error_reports WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"error report {report_id} not found")
        return _error_report_from_row(
            row, db.get_data_version(conn), db.latest_error_settings_id(conn)
        )
