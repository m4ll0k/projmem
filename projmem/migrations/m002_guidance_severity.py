"""v2 guidance notes — add a severity column to annotations.

Adds ``annotations.severity`` (nullable TEXT). Existing notes keep
``NULL`` and continue to behave as v1 notes. Guidance / constraint /
preference / critical kinds populate this column with ``info`` |
``warn`` | ``critical``; ``editing``'s guidance bundle uses it to
sort by attention.

The column is intentionally permissive (no CHECK constraint) — Step 2
plus Step 3 grow the vocabulary and we want SQLite to keep accepting
existing rows under rolling deploys.
"""
from __future__ import annotations

import sqlite3


VERSION = 2
NAME = "guidance-severity"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def up(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "annotations", "severity"):
        conn.execute("ALTER TABLE annotations ADD COLUMN severity TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ann_kind_severity "
        "ON annotations(kind, severity)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_ann_kind_severity")
    if _column_exists(conn, "annotations", "severity"):
        conn.execute("ALTER TABLE annotations DROP COLUMN severity")
