"""Coverage for Step 1 — the four mutation verbs + done/abandoned + sweep + forget.

Direct module-level tests against :mod:`projmem.mutation_verbs` cover
the state machine; a small set of subprocess tests at the end exercise
the CLI wiring so we know argparse + JSON-emission paths haven't
drifted from the function signatures.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from projmem import mutation_verbs as mv
from projmem.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / ".projmem" / "index.db")
    s = Store(db_path)
    yield s
    s.close()


GOOD_REASON = "extracting auth into shared/jwt.py"


def _seed_file(store, path: str) -> None:
    store.upsert_file(path, "py", "h", time.time(), 1, "ast")
    store.conn.commit()


def _add_note(store, target: str, body: str, *, kind: str = "note",
              staleness: str = "fresh") -> int:
    now = time.time()
    cur = store.conn.execute(
        "INSERT INTO annotations(target, kind, body, created_at, staleness) "
        "VALUES(?, ?, ?, ?, ?)",
        (target, kind, body, now, staleness),
    )
    store.conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Reason quality gate
# ---------------------------------------------------------------------------

class TestReasonGate:
    def test_rejects_empty(self, store):
        _seed_file(store, "src/a.py")
        with pytest.raises(mv.ReasonQualityError):
            mv.open_editing_lease(store, "src/a.py", reason="")

    def test_rejects_whitespace_only(self, store):
        _seed_file(store, "src/a.py")
        with pytest.raises(mv.ReasonQualityError):
            mv.open_editing_lease(store, "src/a.py", reason="     ")

    def test_rejects_too_short(self, store):
        _seed_file(store, "src/a.py")
        with pytest.raises(mv.ReasonQualityError, match=r"\d+ chars"):
            mv.open_editing_lease(store, "src/a.py", reason="cleanup")

    def test_rejects_single_word_even_if_long(self, store):
        _seed_file(store, "src/a.py")
        with pytest.raises(mv.ReasonQualityError):
            mv.open_editing_lease(
                store, "src/a.py",
                reason="cleanupcleanupcleanupcleanup",
            )

    def test_accepts_valid_reason(self, store):
        _seed_file(store, "src/a.py")
        result = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        assert result["lease_id"]


# ---------------------------------------------------------------------------
# `editing` — opens a lease + bundles guidance
# ---------------------------------------------------------------------------

class TestEditing:
    def test_returns_lease_with_ttl(self, store):
        _seed_file(store, "src/a.py")
        before = time.time()
        result = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        assert result["lease_id"]
        # TTL is 5 min — expires_at should be ~now + 300s.
        assert result["expires_at"] - before == pytest.approx(
            mv.LEASE_TTL_SECONDS, abs=2,
        )

    def test_surfaces_existing_annotations_as_guidance(self, store):
        _seed_file(store, "src/a.py")
        _add_note(store, "src/a.py", "this is fragile, do not touch")
        result = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        bodies = [g["body"] for g in result["guidance"]]
        assert "this is fragile, do not touch" in bodies

    def test_includes_directory_scoped_guidance(self, store):
        _seed_file(store, "src/auth/jwt.py")
        _add_note(store, "src/auth/", "this whole subsystem is critical")
        _add_note(store, "@project", "project-wide convention: snake_case")
        result = mv.open_editing_lease(
            store, "src/auth/jwt.py", reason=GOOD_REASON,
        )
        bodies = {g["body"] for g in result["guidance"]}
        assert "this whole subsystem is critical" in bodies
        assert "project-wide convention: snake_case" in bodies

    def test_warns_on_contradicted_annotations(self, store):
        _seed_file(store, "src/a.py")
        _add_note(store, "src/a.py", "stale belief",
                  staleness="contradicted")
        result = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        assert any("contradicted" in w for w in result["warnings"])

    def test_inherits_1hop_dep_annotations(self, store):
        _seed_file(store, "src/a.py")
        _seed_file(store, "src/b.py")
        # b.py imports a.py
        store.conn.execute(
            "INSERT INTO edges(src, dst, type) VALUES('src/b.py', 'src/a.py', 'imports')"
        )
        store.conn.commit()
        _add_note(store, "src/b.py", "b depends on a — break carefully")
        result = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        notes_with_via = [g for g in result["guidance"] if g.get("via")]
        assert any("b depends on a" in g["body"] for g in notes_with_via)

    def test_emits_leased_file_event(self, store):
        _seed_file(store, "src/a.py")
        mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        kinds = [r[0] for r in store.conn.execute(
            "SELECT kind FROM file_event")]
        assert "leased" in kinds


# ---------------------------------------------------------------------------
# `creating` — open a lease on a NEW file
# ---------------------------------------------------------------------------

class TestCreating:
    def test_creates_fresh_lifeline(self, store):
        result = mv.open_creating_lease(
            store, "src/new.py", reason="adding password reset endpoint",
        )
        assert result["lifeline_id"]
        row = store.conn.execute(
            "SELECT current_path FROM file_lifeline WHERE id=?",
            (result["lifeline_id"],),
        ).fetchone()
        assert row["current_path"] == "src/new.py"

    def test_rejects_active_lifeline_at_same_path(self, store):
        _seed_file(store, "src/a.py")  # auto-creates a lifeline
        with pytest.raises(mv.PathExistsError):
            mv.open_creating_lease(
                store, "src/a.py", reason="trying to recreate an existing path",
            )

    def test_warns_on_tombstoned_path_with_reason(self, store):
        # Tombstone a prior lifeline at the same path.
        _seed_file(store, "src/dead.py")
        mv.delete_path(
            store, "src/dead.py",
            reason="consolidated into shared/auth.py",
            replaced_by=["shared/auth.py"],
        )
        result = mv.open_creating_lease(
            store, "src/dead.py",
            reason="recreating the auth helper for a different shape",
        )
        assert result["warnings"], "should have warned"
        msg = " ".join(result["warnings"])
        assert "consolidated into shared/auth.py" in msg
        assert "shared/auth.py" in msg
        assert "deleted" in msg


# ---------------------------------------------------------------------------
# `moving` — rename, preserving lifeline + notes
# ---------------------------------------------------------------------------

class TestMoving:
    def test_preserves_lifeline_id(self, store):
        _seed_file(store, "src/old.py")
        before = store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/old.py",),
        ).fetchone()["lifeline_id"]
        mv.move_path(
            store, "src/old.py", "src/new.py",
            reason="renamed for module clarity",
        )
        after = store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/new.py",),
        ).fetchone()
        assert after["lifeline_id"] == before
        # Old path is gone.
        assert store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/old.py",),
        ).fetchone() is None

    def test_moves_annotations_to_new_path(self, store):
        _seed_file(store, "src/old.py")
        _add_note(store, "src/old.py", "important context")
        _add_note(store, "src/old.py#foo", "symbol-scoped context")
        mv.move_path(
            store, "src/old.py", "src/new.py",
            reason="renaming for naming consistency with sibling modules",
        )
        targets = [r[0] for r in store.conn.execute(
            "SELECT target FROM annotations")]
        assert "src/new.py" in targets
        assert "src/new.py#foo" in targets
        assert "src/old.py" not in targets
        assert "src/old.py#foo" not in targets

    def test_appends_moved_event(self, store):
        _seed_file(store, "src/old.py")
        mv.move_path(
            store, "src/old.py", "src/new.py",
            reason="moving to a more discoverable location",
        )
        kinds = [r[0] for r in store.conn.execute(
            "SELECT kind FROM file_event WHERE kind='moved'")]
        assert len(kinds) == 1

    def test_rejects_if_target_already_has_active_lifeline(self, store):
        _seed_file(store, "src/old.py")
        _seed_file(store, "src/already-there.py")
        with pytest.raises(mv.PathExistsError):
            mv.move_path(
                store, "src/old.py", "src/already-there.py",
                reason="trying to clobber an existing destination",
            )

    def test_rejects_if_source_has_no_lifeline(self, store):
        with pytest.raises(mv.PathMissingError):
            mv.move_path(
                store, "src/ghost.py", "src/anywhere.py",
                reason="moving something that doesn't exist",
            )


# ---------------------------------------------------------------------------
# `deleting` — tombstone, never delete
# ---------------------------------------------------------------------------

class TestDeleting:
    def test_tombstones_lifeline(self, store):
        _seed_file(store, "src/a.py")
        mv.delete_path(
            store, "src/a.py",
            reason="removing duplicate of shared/auth.py",
        )
        row = store.conn.execute(
            "SELECT tombstoned_at, tombstoned_reason FROM file_lifeline "
            "WHERE current_path=?", ("src/a.py",),
        ).fetchone()
        assert row["tombstoned_at"] is not None
        assert row["tombstoned_reason"] == "removing duplicate of shared/auth.py"

    def test_records_replaced_by(self, store):
        _seed_file(store, "src/old.py")
        mv.delete_path(
            store, "src/old.py",
            reason="folded into shared/util.py",
            replaced_by=["shared/util.py"],
        )
        row = store.conn.execute(
            "SELECT replaced_by FROM file_lifeline WHERE current_path=?",
            ("src/old.py",),
        ).fetchone()
        assert json.loads(row["replaced_by"]) == ["shared/util.py"]

    def test_closes_open_leases(self, store):
        _seed_file(store, "src/a.py")
        lease = mv.open_editing_lease(
            store, "src/a.py", reason=GOOD_REASON,
        )
        mv.delete_path(
            store, "src/a.py",
            reason="aborting and deleting, this was wrong",
        )
        row = store.conn.execute(
            "SELECT state FROM edit_lease WHERE id=?", (lease["lease_id"],),
        ).fetchone()
        assert row["state"] == "closed"

    def test_rejects_missing_path(self, store):
        with pytest.raises(mv.PathMissingError):
            mv.delete_path(
                store, "src/never.py",
                reason="deleting something that doesn't exist",
            )


# ---------------------------------------------------------------------------
# `done` / `abandoned` — close the lease
# ---------------------------------------------------------------------------

class TestDoneAbandoned:
    def test_done_closes_and_records_duration(self, store):
        _seed_file(store, "src/a.py")
        lease = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        result = mv.close_lease(store, lease["lease_id"], kind="done")
        assert result["closed_kind"] == "done"
        assert result["duration_s"] >= 0

    def test_done_is_idempotent(self, store):
        _seed_file(store, "src/a.py")
        lease = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        first = mv.close_lease(store, lease["lease_id"], kind="done")
        second = mv.close_lease(store, lease["lease_id"], kind="done")
        assert second.get("already_closed") is True
        # Closed exactly once — only one 'released' event recorded.
        n_released = store.conn.execute(
            "SELECT COUNT(*) FROM file_event WHERE kind='released' "
            "AND lifeline_id=?", (lease["lifeline_id"],),
        ).fetchone()[0]
        assert n_released == 1

    def test_done_rejects_unknown_lease(self, store):
        with pytest.raises(mv.LeaseNotFoundError):
            mv.close_lease(store, "00000000-0000-0000-0000-000000000000")

    def test_abandoned_records_abandoned_event(self, store):
        _seed_file(store, "src/a.py")
        lease = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        mv.close_lease(
            store, lease["lease_id"], kind="abandoned",
            reason="dropping this attempt",
        )
        kinds = [r[0] for r in store.conn.execute(
            "SELECT kind FROM file_event WHERE lifeline_id=?",
            (lease["lifeline_id"],),
        )]
        assert "abandoned" in kinds


# ---------------------------------------------------------------------------
# `sweep_expired_leases`
# ---------------------------------------------------------------------------

class TestSweep:
    def test_marks_expired_lease_as_closed(self, store):
        _seed_file(store, "src/a.py")
        lease = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        # Force expiry by rewriting expires_at into the past.
        store.conn.execute(
            "UPDATE edit_lease SET expires_at=? WHERE id=?",
            (time.time() - 10, lease["lease_id"]),
        )
        store.conn.commit()
        result = mv.sweep_expired_leases(store)
        assert lease["lease_id"] in result["expired"]
        row = store.conn.execute(
            "SELECT state, closed_kind FROM edit_lease WHERE id=?",
            (lease["lease_id"],),
        ).fetchone()
        assert row["state"] == "closed"
        assert row["closed_kind"] == "expired"

    def test_leaves_still_valid_leases_alone(self, store):
        _seed_file(store, "src/a.py")
        lease = mv.open_editing_lease(store, "src/a.py", reason=GOOD_REASON)
        result = mv.sweep_expired_leases(store)
        assert lease["lease_id"] not in result["expired"]


# ---------------------------------------------------------------------------
# `forget` — rare-purge
# ---------------------------------------------------------------------------

class TestForget:
    def test_refuses_without_flag(self, store):
        _seed_file(store, "src/a.py")
        lifeline_id = store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/a.py",),
        ).fetchone()["lifeline_id"]
        with pytest.raises(mv.PurgeRefusedError):
            mv.forget_lifeline(store, lifeline_id, yes_really_purge=False)

    def test_purges_lifeline_and_events(self, store):
        _seed_file(store, "src/a.py")
        lifeline_id = store.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", ("src/a.py",),
        ).fetchone()["lifeline_id"]
        mv.forget_lifeline(store, lifeline_id, yes_really_purge=True)
        assert store.conn.execute(
            "SELECT 1 FROM file_lifeline WHERE id=?", (lifeline_id,),
        ).fetchone() is None
        assert store.conn.execute(
            "SELECT 1 FROM file_event WHERE lifeline_id=?", (lifeline_id,),
        ).fetchone() is None

    def test_rejects_unknown_lifeline(self, store):
        with pytest.raises(mv.LifelineNotFoundError):
            mv.forget_lifeline(
                store, "00000000-0000-0000-0000-000000000000",
                yes_really_purge=True,
            )


# ---------------------------------------------------------------------------
# CLI integration (subprocess) — minimal coverage; happy path round-trip.
# ---------------------------------------------------------------------------

def _run_cli(args, *, cwd, env=None):
    env_full = os.environ.copy()
    if env:
        env_full.update(env)
    return subprocess.run(
        [sys.executable, "-m", "projmem.cli", *args],
        cwd=str(cwd), capture_output=True, text=True, env=env_full,
    )


def test_cli_editing_then_done_round_trip(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    rc = _run_cli(["index"], cwd=repo)
    assert rc.returncode == 0, rc.stderr

    rc = _run_cli(
        ["editing", "a.py", "--reason", GOOD_REASON, "--json"],
        cwd=repo,
    )
    assert rc.returncode == 0, rc.stderr
    out = json.loads(rc.stdout)
    assert out["lease_id"]
    lease_id = out["lease_id"]

    rc = _run_cli(["done", lease_id, "--json"], cwd=repo)
    assert rc.returncode == 0, rc.stderr
    closed = json.loads(rc.stdout)
    assert closed["closed_kind"] == "done"


def test_cli_reason_gate_returns_structured_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _run_cli(["index"], cwd=repo)
    rc = _run_cli(
        ["editing", "a.py", "--reason", "tiny", "--json"], cwd=repo,
    )
    assert rc.returncode == 2
    err = json.loads(rc.stdout)
    assert err["error"] == "reason-quality"
