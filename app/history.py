"""Persistenza storico masterizzazioni su SQLite."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("HISTORY_DB", "/data/history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS burns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT    NOT NULL,
    ended_at      TEXT,
    source_file   TEXT    NOT NULL,
    media_type    TEXT,
    speed         TEXT,
    verify        INTEGER NOT NULL DEFAULT 0,
    eject         INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL,
    duration_sec  INTEGER,
    log_path      TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_burns_started ON burns(started_at DESC);
"""

_lock = threading.Lock()
_initialized = False


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Crea lo schema se non esiste. Idempotente."""
    global _initialized
    if _initialized:
        return
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
    _initialized = True
    log.info("Database storico inizializzato: %s", DB_PATH)


def add_burn(
    *,
    started_at: str,
    source_file: str,
    media_type: Optional[str],
    speed: Optional[str],
    verify: bool,
    eject: bool,
    status: str,
    log_path: Optional[str] = None,
) -> int:
    """Inserisce un nuovo record. Restituisce l'id."""
    init_db()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO burns
                (started_at, source_file, media_type, speed, verify, eject, status, log_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (started_at, source_file, media_type, speed, int(verify), int(eject), status, log_path),
        )
        return int(cur.lastrowid)


def finish_burn(
    burn_id: int,
    *,
    ended_at: str,
    status: str,
    duration_sec: int,
    error: Optional[str] = None,
) -> None:
    """Aggiorna il record di una masterizzazione completata."""
    init_db()
    with _lock, _connect() as conn:
        conn.execute(
            """
            UPDATE burns
               SET ended_at = ?, status = ?, duration_sec = ?, error = ?
             WHERE id = ?
            """,
            (ended_at, status, duration_sec, error, burn_id),
        )


def list_burns(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM burns ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_burn(burn_id: int) -> Optional[dict]:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM burns WHERE id = ?", (burn_id,)).fetchone()
        return dict(row) if row else None
