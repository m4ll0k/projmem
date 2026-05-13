"""v2 lifelines / events / leases — initial schema additions.

Adds:
  * ``files.lifeline_id``        (nullable TEXT, UUID)
  * ``annotations.lifeline_id``  (nullable TEXT, UUID; notes ride the file
                                  through renames)
  * table ``file_lifeline``      — file identity that survives rename/delete
  * table ``file_event``         — append-only event log per lifeline
  * table ``edit_lease``         — open intents to edit a file

Backfill: every existing row in ``files`` gets a fresh ``lifeline_id`` and a
matching ``file_lifeline`` row. ``created_at`` is derived from the first-add
commit timestamp via ``git log --diff-filter=A --follow --reverse``; if git
is unavailable or the file is untracked, falls back to ``time.time()``.
``created_reason`` is the verbatim string requested by the v2 brief.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
import uuid


VERSION = 1
NAME = "v2-lifelines"


_UP_SQL = """
CREATE TABLE IF NOT EXISTS file_lifeline (
    id                TEXT PRIMARY KEY,
    current_path      TEXT NOT NULL,
    created_at        REAL NOT NULL,
    created_reason    TEXT NOT NULL,
    created_by        TEXT,
    tombstoned_at     REAL,
    tombstoned_reason TEXT,
    replaced_by       TEXT
);
CREATE INDEX IF NOT EXISTS idx_lifeline_path
    ON file_lifeline(current_path);
CREATE INDEX IF NOT EXISTS idx_lifeline_tombstone
    ON file_lifeline(tombstoned_at);

CREATE TABLE IF NOT EXISTS file_event (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lifeline_id      TEXT NOT NULL,
    kind             TEXT NOT NULL,
    at               REAL NOT NULL,
    reason           TEXT,
    session_id       TEXT,
    diff_summary     TEXT,
    symbols_affected TEXT,
    FOREIGN KEY(lifeline_id) REFERENCES file_lifeline(id)
);
CREATE INDEX IF NOT EXISTS idx_file_event_lifeline
    ON file_event(lifeline_id, at);
CREATE INDEX IF NOT EXISTS idx_file_event_kind
    ON file_event(kind);

CREATE TABLE IF NOT EXISTS edit_lease (
    id          TEXT PRIMARY KEY,
    lifeline_id TEXT NOT NULL,
    opened_at   REAL NOT NULL,
    expires_at  REAL NOT NULL,
    closed_at   REAL,
    closed_kind TEXT,
    agent_id    TEXT,
    intent      TEXT,
    state       TEXT NOT NULL DEFAULT 'open',
    FOREIGN KEY(lifeline_id) REFERENCES file_lifeline(id)
);
CREATE INDEX IF NOT EXISTS idx_edit_lease_open
    ON edit_lease(state, expires_at);
CREATE INDEX IF NOT EXISTS idx_edit_lease_lifeline
    ON edit_lease(lifeline_id, opened_at);
"""

_DOWN_SQL = """
DROP INDEX IF EXISTS idx_edit_lease_lifeline;
DROP INDEX IF EXISTS idx_edit_lease_open;
DROP TABLE IF EXISTS edit_lease;

DROP INDEX IF EXISTS idx_file_event_kind;
DROP INDEX IF EXISTS idx_file_event_lifeline;
DROP TABLE IF EXISTS file_event;

DROP INDEX IF EXISTS idx_lifeline_tombstone;
DROP INDEX IF EXISTS idx_lifeline_path;
DROP TABLE IF EXISTS file_lifeline;
"""


def _project_root(conn: sqlite3.Connection) -> str | None:
    """Derive project root from the connection's main DB path.

    The store always lives at ``<root>/.projmem/index.db``; if the DB
    is in-memory or under an unexpected layout, return None and the
    caller will skip the git-based backfill.
    """
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            store_dir = os.path.dirname(path)
            if os.path.basename(store_dir) == ".projmem":
                return os.path.dirname(store_dir)
            return None
    return None


def _git_first_add_ts(root: str, path: str) -> float | None:
    """Epoch seconds of the first commit that added ``path``, or None."""
    if not shutil.which("git"):
        return None
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", root, "log", "--diff-filter=A", "--follow",
             "--reverse", "--format=%at", "--", path],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    first = (out.stdout.splitlines() or [None])[0]
    if not first:
        return None
    try:
        return float(first)
    except ValueError:
        return None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _exec_script(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a multi-statement SQL string one statement at a time.

    Unlike :meth:`sqlite3.Connection.executescript`, this does NOT
    implicitly commit the pending transaction — that lets the migration
    runner wrap us in a savepoint without losing it. Statements are
    split on ``;``, blank lines and comment-only lines are dropped.
    """
    for stmt in sql.split(";"):
        s = "\n".join(line for line in stmt.splitlines()
                      if line.strip() and not line.strip().startswith("--"))
        s = s.strip()
        if s:
            conn.execute(s)


def up(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "files", "lifeline_id"):
        conn.execute("ALTER TABLE files ADD COLUMN lifeline_id TEXT")
    if not _column_exists(conn, "annotations", "lifeline_id"):
        conn.execute("ALTER TABLE annotations ADD COLUMN lifeline_id TEXT")
    _exec_script(conn, _UP_SQL)

    root = _project_root(conn)
    now = time.time()
    reason = "backfilled — pre-v2 lifeline"
    rows = conn.execute(
        "SELECT path FROM files WHERE lifeline_id IS NULL OR lifeline_id=''"
    ).fetchall()
    for (path,) in rows:
        lid = str(uuid.uuid4())
        created_at = (
            _git_first_add_ts(root, path) if root is not None else None
        ) or now
        conn.execute(
            "INSERT INTO file_lifeline(id, current_path, created_at, "
            "created_reason) VALUES(?, ?, ?, ?)",
            (lid, path, created_at, reason),
        )
        conn.execute(
            "INSERT INTO file_event(lifeline_id, kind, at, reason) "
            "VALUES(?, 'created', ?, ?)",
            (lid, created_at, reason),
        )
        conn.execute(
            "UPDATE files SET lifeline_id=? WHERE path=?",
            (lid, path),
        )


def down(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _DOWN_SQL)
    if _column_exists(conn, "files", "lifeline_id"):
        conn.execute("ALTER TABLE files DROP COLUMN lifeline_id")
    if _column_exists(conn, "annotations", "lifeline_id"):
        conn.execute("ALTER TABLE annotations DROP COLUMN lifeline_id")
