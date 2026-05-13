"""v2.1 skill scaffold — schema is ready, verbs are NOT.

These tests assert two things:

  1. The ``skill`` and ``skill_attachment`` tables are created by
     m004 and present after a fresh ``Store()`` open.
  2. The ``skill`` kind is reserved in ``projmem.note_kinds.KNOWN_KINDS``
     so adding the v2.1 surface is a feature flip — no schema change.

We DELIBERATELY do NOT test any CLI verb (`projmem skill ...`); those
land in v2.1. If the test count grows here in a future commit, the
verbs are landing — re-read ``docs/v2-design.md`` Pillar 3.5 before
shipping.
"""
from __future__ import annotations

import time

from projmem import note_kinds
from projmem.store import Store


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _index_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone() is not None


def test_skill_tables_exist_on_fresh_store(tmp_path):
    store = Store(str(tmp_path / ".projmem" / "index.db"))
    try:
        assert _table_exists(store.conn, "skill")
        assert _table_exists(store.conn, "skill_attachment")
    finally:
        store.close()


def test_skill_indexes_exist(tmp_path):
    store = Store(str(tmp_path / ".projmem" / "index.db"))
    try:
        assert _index_exists(store.conn, "idx_skill_enabled")
        assert _index_exists(store.conn, "idx_skill_scope")
        assert _index_exists(store.conn, "idx_skill_attach_skill")
        assert _index_exists(store.conn, "idx_skill_attach_lifeline")
    finally:
        store.close()


def test_skill_kind_is_in_known_kinds():
    assert "skill" in note_kinds.KNOWN_KINDS
    assert note_kinds.is_skill_kind("skill") is True
    assert note_kinds.is_skill_kind("note") is False


def test_skill_table_columns_match_v21_spec(tmp_path):
    store = Store(str(tmp_path / ".projmem" / "index.db"))
    try:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(skill)")}
        expected = {
            "id", "name", "prompt", "scope_pattern", "trigger",
            "inject_as", "authored_by", "created_at", "description",
            "tags", "enabled", "conflicts_with", "composes_with",
        }
        assert expected <= cols, f"missing columns: {expected - cols}"
    finally:
        store.close()


def test_skill_attachment_table_columns_match_v21_spec(tmp_path):
    store = Store(str(tmp_path / ".projmem" / "index.db"))
    try:
        cols = {r[1] for r in store.conn.execute(
            "PRAGMA table_info(skill_attachment)")}
        expected = {"id", "skill_id", "lifeline_id", "added_at"}
        assert expected <= cols, f"missing columns: {expected - cols}"
    finally:
        store.close()


def test_skill_table_is_empty_in_v20(tmp_path):
    """No verbs ship skills in v2.0 — the table must stay empty until v2.1.

    If this test fails, someone added writes against `skill` before the
    v2.1 release. Re-read docs/v2-design.md Pillar 3.5 and confirm the
    decision is intentional.
    """
    store = Store(str(tmp_path / ".projmem" / "index.db"))
    try:
        n = store.conn.execute("SELECT COUNT(*) FROM skill").fetchone()[0]
        assert n == 0
        m = store.conn.execute(
            "SELECT COUNT(*) FROM skill_attachment").fetchone()[0]
        assert m == 0
    finally:
        store.close()
