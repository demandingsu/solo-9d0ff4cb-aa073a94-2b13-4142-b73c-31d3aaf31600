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
