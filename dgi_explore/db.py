"""Database and mirror locations, shared by every step."""

import logging
import sqlite3
from pathlib import Path

import psycopg

DSN = "dbname=gis"
MIRROR = Path("/mnt/storage/datagovie_mirror")
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
STATE_DIR = Path.home() / ".local/state/dgi-explore"

log = logging.getLogger("dgi")


def connect(**kw) -> psycopg.Connection:
    return psycopg.connect(DSN, **kw)


def mirror_state() -> sqlite3.Connection:
    """The mirror's progress database, read-only: the mirror is still running and owns it."""
    db = sqlite3.connect(f"file:{MIRROR}/state.sqlite?mode=ro", uri=True, timeout=60)
    db.row_factory = sqlite3.Row
    return db


def run_sql_files(conn, *names):
    """Apply sql/<name>.sql files (all written to be safe to re-run)."""
    for name in names:
        path = SQL_DIR / f"{name}.sql"
        log.info("applying %s", path.name)
        conn.execute(path.read_text())
