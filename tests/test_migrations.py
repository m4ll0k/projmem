"""Migration framework + v2 lifelines (m001) coverage.

The brief requires schema migration up + down to be tested on a "copy of
a real v1 database." These tests build a v1-shaped store by hand from
the ``SCHEMA`` constant in :mod:`projmem.store` (without the new
migration runner), then exercise the v2 migration as it would land on
that database.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import time

import pytest

from projmem import migrations
from projmem.migrations import m001_v2_lifelines as m001
from projmem.store import SCHEMA, Store


# ---------- helpers ----------------------------------------------------------

def _open_v1_db(tmp_path) -> sqlite3.Connection:
    """Return a connection seeded with the v1 schema only — no migrations.

    This mimics what an unupgraded v1.0.0 database looks like on disk.
    The pre-v2 ad-hoc ALTERs from Store.__init__ are NOT applied either,
    so the schema is intentionally minimal: just the ``CREATE TABLE`` /
    ``CREATE INDEX`` declarations from the SCHEMA constant.
    """
    os.makedirs(tmp_path / ".projmem", exist_ok=True)
    conn = sqlite3.connect(str(tmp_path / ".projmem" / "index.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


# ---------- baseline ---------------------------------------------------------

def test_v1_schema_lacks_v2_artifacts(tmp_path):
    conn = _open_v1_db(tmp_path)
    assert not _table_exists(conn, "file_lifeline")
    assert not _table_exists(conn, "file_event")
    assert not _table_exists(conn, "edit_lease")
    assert not _column_exists(conn, "files", "lifeline_id")
    assert not _column_exists(conn, "annotations", "lifeline_id")
    assert migrations.current_version(conn) == 0


# ---------- up ---------------------------------------------------------------

def test_apply_pending_adds_v2_tables_and_columns(tmp_path):
    conn = _open_v1_db(tmp_path)
    applied = migrations.apply_pending(conn)
    assert applied == [1]
    assert _table_exists(conn, "file_lifeline")
    assert _table_exists(conn, "file_event")
    assert _table_exists(conn, "edit_lease")
    assert _column_exists(conn, "files", "lifeline_id")
    assert _column_exists(conn, "annotations", "lifeline_id")
    assert migrations.current_version(conn) == 1


def test_apply_pending_backfills_existing_files(tmp_path):
    conn = _open_v1_db(tmp_path)
    now = time.time()
    for path in ("src/a.py", "src/b.py", "tests/test_a.py"):
        conn.execute(
            "INSERT INTO files(path, lang, hash, mtime, size, parser, "
            "indexed_at, stale) VALUES(?,?,?,?,?,?,?,0)",
            (path, "py", "hash-" + path, now, 1, "ast", now),
        )
    conn.commit()

    migrations.apply_pending(conn)

    lifeline_count = conn.execute("SELECT COUNT(*) FROM file_lifeline").fetchone()[0]
    file_event_count = conn.execute("SELECT COUNT(*) FROM file_event").fetchone()[0]
    assert lifeline_count == 3
    assert file_event_count == 3

    rows = conn.execute(
        "SELECT path, lifeline_id FROM files ORDER BY path"
    ).fetchall()
    for row in rows:
        assert row["lifeline_id"], f"row {row['path']!r} missing lifeline_id"

    # Every file_lifeline row carries the backfill reason verbatim.
    reasons = {r[0] for r in conn.execute(
        "SELECT DISTINCT created_reason FROM file_lifeline")}
    assert reasons == {"backfilled — pre-v2 lifeline"}

    # The matching file_event row marks 'created'.
    kinds = {r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM file_event")}
    assert kinds == {"created"}


def test_apply_pending_is_idempotent(tmp_path):
    conn = _open_v1_db(tmp_path)
    migrations.apply_pending(conn)
    second = migrations.apply_pending(conn)
    assert second == []
    assert migrations.current_version(conn) == 1


# ---------- down -------------------------------------------------------------

def test_rollback_removes_v2_tables_and_columns(tmp_path):
    conn = _open_v1_db(tmp_path)
    migrations.apply_pending(conn)
    rolled = migrations.rollback_to(conn, 0)
    assert rolled == [1]
    assert not _table_exists(conn, "file_lifeline")
    assert not _table_exists(conn, "file_event")
    assert not _table_exists(conn, "edit_lease")
    assert not _column_exists(conn, "files", "lifeline_id")
    assert not _column_exists(conn, "annotations", "lifeline_id")
    assert migrations.current_version(conn) == 0


def test_rollback_then_reapply_round_trips(tmp_path):
    conn = _open_v1_db(tmp_path)
    migrations.apply_pending(conn)
    migrations.rollback_to(conn, 0)
    second = migrations.apply_pending(conn)
    assert second == [1]
    assert migrations.current_version(conn) == 1


# ---------- backfill timestamp ----------------------------------------------

def test_backfill_uses_now_when_not_a_git_repo(tmp_path):
    conn = _open_v1_db(tmp_path)
    conn.execute(
        "INSERT INTO files(path, lang, hash, mtime, size, parser, "
        "indexed_at, stale) VALUES(?,?,?,?,?,?,?,0)",
        ("src/lone.py", "py", "h", time.time(), 1, "ast", time.time()),
    )
    conn.commit()
    before = time.time()
    migrations.apply_pending(conn)
    after = time.time()
    row = conn.execute(
        "SELECT created_at FROM file_lifeline WHERE current_path=?",
        ("src/lone.py",),
    ).fetchone()
    assert before - 1 <= row["created_at"] <= after + 1


def test_backfill_uses_git_first_add_when_history_exists(tmp_path):
    """A real git repo with a committed file backfills to the commit timestamp."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t",
                    "commit", "--allow-empty", "-q", "-m", "init"],
                   cwd=str(repo), check=True)
    (repo / "src").mkdir()
    (repo / "src" / "old.py").write_text("# hello\n")
    expected_ts = int(time.time()) - 3600  # one hour ago
    env = os.environ | {
        "GIT_AUTHOR_DATE":    f"@{expected_ts} +0000",
        "GIT_COMMITTER_DATE": f"@{expected_ts} +0000",
    }
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t",
                    "add", "src/old.py"], cwd=str(repo), check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t",
                    "commit", "-q", "-m", "add old"],
                   cwd=str(repo), env=env, check=True)
    # Index has to know about the file before the migration backfills it.
    conn = _open_v1_db(repo)
    conn.execute(
        "INSERT INTO files(path, lang, hash, mtime, size, parser, "
        "indexed_at, stale) VALUES(?,?,?,?,?,?,?,0)",
        ("src/old.py", "py", "h", time.time(), 1, "ast", time.time()),
    )
    conn.commit()
    migrations.apply_pending(conn)
    got = conn.execute(
        "SELECT created_at FROM file_lifeline WHERE current_path=?",
        ("src/old.py",),
    ).fetchone()["created_at"]
    # Allow ±2s for filesystem/git timestamp drift.
    assert abs(got - expected_ts) < 2, (got, expected_ts)


# ---------- runner inspection -----------------------------------------------

def test_list_migrations_shows_m001():
    listed = migrations.list_migrations()
    assert (1, "v2-lifelines") in listed


# ---------- end-to-end through Store ----------------------------------------

def test_store_open_auto_applies_migrations(tmp_path):
    db_path = str(tmp_path / ".projmem" / "index.db")
    store = Store(db_path)
    try:
        # Fresh store should land at the latest version.
        assert migrations.current_version(store.conn) == 1
        assert _table_exists(store.conn, "file_lifeline")
    finally:
        store.close()


def test_store_reopen_does_not_reapply_migrations(tmp_path):
    db_path = str(tmp_path / ".projmem" / "index.db")
    s1 = Store(db_path); s1.close()
    s2 = Store(db_path)
    try:
        # We rely on version stamp being set; a fresh apply would also work,
        # but the contract is "skip when already current."
        assert migrations.current_version(s2.conn) == 1
        # And the lifeline tables should still be present (not re-created
        # destructively or doubled).
        assert _table_exists(s2.conn, "file_lifeline")
    finally:
        s2.close()


# ---------- upsert_file auto-creates lifelines for new files ----------------

def test_upsert_file_creates_lifeline_for_new_file(tmp_path):
    """A fresh `projmem index` adds files AFTER the migration runs, so the
    backfill phase sees zero rows. `Store.upsert_file` must close that gap
    by ensuring every newly-indexed path gets a lifeline + 'created' event.
    """
    db_path = str(tmp_path / ".projmem" / "index.db")
    store = Store(db_path)
    try:
        store.upsert_file("src/new.py", "py", "h", time.time(), 1, "ast")
        store.conn.commit()
        row = store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/new.py",),
        ).fetchone()
        assert row["lifeline_id"], "files row missing lifeline_id"
        ll = store.conn.execute(
            "SELECT current_path, created_reason FROM file_lifeline "
            "WHERE id=?", (row["lifeline_id"],),
        ).fetchone()
        assert ll["current_path"] == "src/new.py"
        assert ll["created_reason"] == "indexed — no explicit creation event"
        # Matching 'created' event.
        ev = store.conn.execute(
            "SELECT kind FROM file_event WHERE lifeline_id=?",
            (row["lifeline_id"],),
        ).fetchone()
        assert ev["kind"] == "created"
    finally:
        store.close()


def test_upsert_file_is_idempotent_for_lifeline(tmp_path):
    """Re-indexing the same path must NOT create a second lifeline."""
    db_path = str(tmp_path / ".projmem" / "index.db")
    store = Store(db_path)
    try:
        store.upsert_file("src/same.py", "py", "h1", time.time(), 1, "ast")
        store.upsert_file("src/same.py", "py", "h2", time.time(), 2, "ast")
        store.upsert_file("src/same.py", "py", "h3", time.time(), 3, "ast")
        store.conn.commit()
        n = store.conn.execute(
            "SELECT COUNT(*) FROM file_lifeline WHERE current_path=?",
            ("src/same.py",),
        ).fetchone()[0]
        assert n == 1
        events = store.conn.execute(
            "SELECT COUNT(*) FROM file_event WHERE kind='created'"
        ).fetchone()[0]
        assert events == 1
    finally:
        store.close()


def test_upsert_file_self_heals_legacy_row_without_lifeline(tmp_path):
    """A file that landed in `files` before the upsert_file fix (or via a
    direct INSERT skipping `upsert_file`) is missing a lifeline_id. Calling
    upsert_file on the same path must heal it on the next index.
    """
    db_path = str(tmp_path / ".projmem" / "index.db")
    store = Store(db_path)
    try:
        # Bypass upsert_file: insert directly without lifeline_id.
        store.conn.execute(
            "INSERT INTO files(path, lang, hash, mtime, size, parser, "
            "indexed_at, stale, lifeline_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0, NULL)",
            ("src/legacy.py", "py", "h", time.time(), 1, "ast", time.time()),
        )
        store.conn.commit()
        assert store.conn.execute(
            "SELECT COUNT(*) FROM file_lifeline WHERE current_path=?",
            ("src/legacy.py",),
        ).fetchone()[0] == 0
        # Re-index: triggers the self-heal path.
        store.upsert_file("src/legacy.py", "py", "h2", time.time(), 2, "ast")
        store.conn.commit()
        assert store.conn.execute(
            "SELECT COUNT(*) FROM file_lifeline WHERE current_path=?",
            ("src/legacy.py",),
        ).fetchone()[0] == 1
    finally:
        store.close()
