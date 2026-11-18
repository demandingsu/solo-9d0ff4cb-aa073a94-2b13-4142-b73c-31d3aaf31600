"""SQLite persistence layer.

A monotonically increasing ``data_version`` (stored in ``meta``) tags every
state of the trial data.  Each publication records the version it was
computed from; a publication is stale iff its version differs from the
current one.  All mutations run inside ``BEGIN IMMEDIATE`` transactions
serialised by a process-wide write lock, so a publication always reflects
one consistent snapshot (blocks, differences, p-value and provenance are
same-version by construction).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plots (
    block_id     INTEGER NOT NULL CHECK (block_id BETWEEN 1 AND 12),
    treatment    TEXT    NOT NULL CHECK (treatment IN ('A', 'B')),
    area         REAL    NOT NULL CHECK (area > 0),
    fresh_weight REAL    NOT NULL CHECK (fresh_weight >= 0),
    moisture     REAL    CHECK (moisture IS NULL OR (moisture >= 0 AND moisture < 1)),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (block_id, treatment)
);
CREATE TABLE IF NOT EXISTS publications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    data_version   INTEGER NOT NULL,
    n_blocks       INTEGER NOT NULL,
    mean_diff      REAL    NOT NULL,
    p_value        REAL    NOT NULL,
    n_permutations INTEGER NOT NULL,
    payload        TEXT    NOT NULL
);
-- Versioned measurement-error settings.  Every actual revision inserts a new
-- row (the highest id is the current version); an identical replacement is a
-- no-op and reuses the current id, so reports do not go stale for nothing.
CREATE TABLE IF NOT EXISTS error_settings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    gain_error  REAL    NOT NULL CHECK (gain_error >= 0 AND gain_error < 1)
);
CREATE TABLE IF NOT EXISTS error_plot_settings (
    settings_id    INTEGER NOT NULL,
    block_id       INTEGER NOT NULL CHECK (block_id BETWEEN 1 AND 12),
    treatment      TEXT    NOT NULL CHECK (treatment IN ('A', 'B')),
    weight_error   REAL    NOT NULL CHECK (weight_error >= 0),
    moisture_error REAL    NOT NULL CHECK (moisture_error >= 0),
    PRIMARY KEY (settings_id, block_id, treatment)
);
-- Error-range reports are immutable once written; staleness is derived at
-- read time by comparing the pinned settings/data versions with the current
-- ones.  A failed computation rolls back and never overwrites an old report.
CREATE TABLE IF NOT EXISTS error_reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    publication_id    INTEGER NOT NULL,
    data_version      INTEGER NOT NULL,
    error_settings_id INTEGER NOT NULL,
    n_blocks          INTEGER NOT NULL,
    recorded_mean_diff REAL   NOT NULL,
    lower_bound       REAL    NOT NULL,
    upper_bound       REAL    NOT NULL,
    sign              TEXT    NOT NULL CHECK (sign IN ('positive', 'negative', 'indeterminate')),
    sign_certain      INTEGER NOT NULL,
    payload           TEXT    NOT NULL
);
"""

_write_lock = threading.Lock()


def db_path() -> str:
    return os.environ.get("DB_PATH", "./trial.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with session(write=True) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0')"
        )


@contextmanager
def session(write: bool = False) -> Iterator[sqlite3.Connection]:
    """A connection wrapped in a transaction.

    ``write=True`` takes the process-wide write lock and begins an IMMEDIATE
    transaction, guaranteeing a consistent snapshot that no other writer can
    disturb until commit/rollback.
    """
    if write:
        _write_lock.acquire()
    conn = connect()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if write:
            _write_lock.release()


def get_data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row["value"])


def bump_data_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        " WHERE key = 'data_version'"
    )
    return get_data_version(conn)


def fetch_plots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT block_id, treatment, area, fresh_weight, moisture, updated_at"
        " FROM plots ORDER BY block_id, treatment"
    ).fetchall()


def fetch_plot(
    conn: sqlite3.Connection, block_id: int, treatment: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT block_id, treatment, area, fresh_weight, moisture, updated_at"
        " FROM plots WHERE block_id = ? AND treatment = ?",
        (block_id, treatment),
    ).fetchone()


def latest_error_settings_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM error_settings").fetchone()
    return row["id"] if row and row["id"] is not None else None


def fetch_error_settings(
    conn: sqlite3.Connection, settings_id: int
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    """Return (settings header row, per-plot rows) for one settings version."""
    header = conn.execute(
        "SELECT * FROM error_settings WHERE id = ?", (settings_id,)
    ).fetchone()
    if header is None:
        return None, []
    plots = conn.execute(
        "SELECT block_id, treatment, weight_error, moisture_error"
        " FROM error_plot_settings WHERE settings_id = ?"
        " ORDER BY block_id, treatment",
        (settings_id,),
    ).fetchall()
    return header, plots
