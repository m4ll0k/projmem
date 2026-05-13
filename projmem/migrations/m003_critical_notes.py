"""v2 critical notes — extend annotations with cosigner + review fields.

Adds seven nullable columns to ``annotations``. Existing rows keep
NULL and behave as v1 notes. Notes with ``kind='critical'`` populate
these columns; the CLI gates ``critical add`` so they're never NULL
on a critical row.

  * category          — security | compliance | performance |
                         business_logic | data_integrity | other
  * incident_refs     — JSON array of issue / post-mortem refs
  * approved_by       — JSON array of cosigner user_ids (≥ 1 required)
  * last_reviewed_at  — epoch seconds
  * review_window_days — int, default 90
  * blast_radius_hops — int, default 1
  * blocks_edits      — int (0/1), default 1
"""
from __future__ import annotations

import sqlite3


VERSION = 3
NAME = "critical-notes"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


_NEW_COLUMNS = (
    ("category",            "TEXT"),
    ("incident_refs",       "TEXT"),   # JSON array
    ("approved_by",         "TEXT"),   # JSON array
    ("last_reviewed_at",    "REAL"),
    ("review_window_days",  "INTEGER"),
    ("blast_radius_hops",   "INTEGER"),
    ("blocks_edits",        "INTEGER"),
)


def up(conn: sqlite3.Connection) -> None:
    for name, typ in _NEW_COLUMNS:
        if not _column_exists(conn, "annotations", name):
            conn.execute(f"ALTER TABLE annotations ADD COLUMN {name} {typ}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ann_critical_pending "
        "ON annotations(kind, last_reviewed_at) "
        "WHERE kind = 'critical'"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_ann_critical_pending")
    for name, _ in _NEW_COLUMNS:
        if _column_exists(conn, "annotations", name):
            conn.execute(f"ALTER TABLE annotations DROP COLUMN {name}")
