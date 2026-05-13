"""Step 3 coverage — critical notes (cosigner gate, review cadence,
edit-blocking, blast-radius propagation, ⚠ CRITICAL CONTEXT prelude)."""
from __future__ import annotations

import json
import time

import pytest

from projmem import critical as crit
from projmem import mutation_verbs as mv
from projmem.store import Store


GOOD_REASON_CRITICAL = (
    "This module signs every outbound API token. Compliance requires "
    "RS256 (never HS256). Three prior changes caused production incidents "
    "(INC-204, INC-301, INC-417). Edits require security review."
)
GOOD_EDIT_REASON = "extracting helper into shared/jwt.py"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / ".projmem" / "index.db"))
    yield s
    s.close()


def _seed(store, path):
    store.upsert_file(path, "py", "h", time.time(), 1, "ast")
    store.conn.commit()


# ---------------------------------------------------------------------------
# Cosigner gate + reason floor + category validation
# ---------------------------------------------------------------------------

class TestAuthoringGate:
    def test_rejects_missing_cosigner(self, store):
        _seed(store, "src/auth.py")
        with pytest.raises(crit.CosignerRequiredError):
            crit.add_critical(
                store, "src/auth.py",
                reason=GOOD_REASON_CRITICAL, category="security",
            )

    def test_accepts_explicit_cosigner(self, store):
        _seed(store, "src/auth.py")
        result = crit.add_critical(
            store, "src/auth.py",
            reason=GOOD_REASON_CRITICAL, category="security",
            approved_by=["alice@example.com"],
        )
        assert result["approved_by"] == ["alice@example.com"]

    def test_accepts_self_cosign(self, store):
        _seed(store, "src/auth.py")
        result = crit.add_critical(
            store, "src/auth.py",
            reason=GOOD_REASON_CRITICAL, category="security",
            self_cosign=True,
        )
        assert result["approved_by"] == ["@self-cosign"]

    def test_rejects_short_reason(self, store):
        _seed(store, "src/auth.py")
        with pytest.raises(crit.ReasonTooShortError):
            crit.add_critical(
                store, "src/auth.py",
                reason="too short — only one short clause",
                category="security",
                self_cosign=True,
            )

    def test_rejects_invalid_category(self, store):
        _seed(store, "src/auth.py")
        with pytest.raises(crit.CategoryError):
            crit.add_critical(
                store, "src/auth.py",
                reason=GOOD_REASON_CRITICAL, category="urgent",
                self_cosign=True,
            )


# ---------------------------------------------------------------------------
# Review cadence
# ---------------------------------------------------------------------------

class TestReviewCadence:
    def test_pending_review_empty_when_fresh(self, store):
        _seed(store, "src/a.py")
        crit.add_critical(
            store, "src/a.py", reason=GOOD_REASON_CRITICAL,
            category="security", self_cosign=True,
        )
        assert crit.pending_review(store) == []

    def test_pending_review_flags_old_notes(self, store):
        _seed(store, "src/a.py")
        result = crit.add_critical(
            store, "src/a.py", reason=GOOD_REASON_CRITICAL,
            category="compliance", self_cosign=True,
            review_window_days=30,
        )
        # Force last_reviewed_at into the past by 60 days.
        long_ago = time.time() - 60 * 86400
        store.conn.execute(
            "UPDATE annotations SET last_reviewed_at=? WHERE id=?",
            (long_ago, result["id"]),
        )
        store.conn.commit()
        pending = crit.pending_review(store)
        assert any(p["id"] == result["id"] for p in pending)
        assert pending[0]["overdue_by_days"] > 25  # 60 elapsed - 30 window

    def test_mark_reviewed_clears_flag(self, store):
        _seed(store, "src/a.py")
        result = crit.add_critical(
            store, "src/a.py", reason=GOOD_REASON_CRITICAL,
            category="performance", self_cosign=True,
            review_window_days=30,
        )
        store.conn.execute(
            "UPDATE annotations SET last_reviewed_at=? WHERE id=?",
            (time.time() - 60 * 86400, result["id"]),
        )
        store.conn.commit()
        assert crit.pending_review(store)
        crit.mark_reviewed(store, result["id"])
        assert crit.pending_review(store) == []


# ---------------------------------------------------------------------------
# Edit-blocking — `editing` lease state on a critical path
# ---------------------------------------------------------------------------

class TestEditBlocking:
    def test_editing_on_critical_path_enters_pending_approval(self, store):
        _seed(store, "src/auth.py")
        crit.add_critical(
            store, "src/auth.py", reason=GOOD_REASON_CRITICAL,
            category="security", self_cosign=True, blocks_edits=True,
        )
        result = mv.open_editing_lease(
            store, "src/auth.py", reason=GOOD_EDIT_REASON,
        )
        assert result["lease_state"] == "pending_approval"
        # The lease row carries the same state.
        row = store.conn.execute(
            "SELECT state FROM edit_lease WHERE id=?", (result["lease_id"],),
        ).fetchone()
        assert row["state"] == "pending_approval"

    def test_editing_attaches_critical_prelude(self, store):
        _seed(store, "src/auth.py")
        crit.add_critical(
            store, "src/auth.py", reason=GOOD_REASON_CRITICAL,
            category="security", self_cosign=True,
            incident_refs=["INC-204", "INC-301"],
        )
        result = mv.open_editing_lease(
            store, "src/auth.py", reason=GOOD_EDIT_REASON,
        )
        assert "critical_prelude" in result
        prelude = result["critical_prelude"]
        assert "⚠ CRITICAL CONTEXT" in prelude
        assert "[security]" in prelude
        assert "src/auth.py" in prelude
        assert "state intended change" in prelude

    def test_no_block_when_blocks_edits_false(self, store):
        _seed(store, "src/auth.py")
        crit.add_critical(
            store, "src/auth.py", reason=GOOD_REASON_CRITICAL,
            category="other", self_cosign=True, blocks_edits=False,
        )
        result = mv.open_editing_lease(
            store, "src/auth.py", reason=GOOD_EDIT_REASON,
        )
        # Prelude still surfaced (the agent should still engage), but
        # the lease is OPEN, not pending_approval.
        assert result["lease_state"] == "open"
        assert "critical_prelude" in result


# ---------------------------------------------------------------------------
# Blast-radius propagation
# ---------------------------------------------------------------------------

class TestBlastRadius:
    def test_1hop_dependent_inherits_critical_warning(self, store):
        _seed(store, "src/auth.py")
        _seed(store, "src/handler.py")
        # handler imports auth.
        store.conn.execute(
            "INSERT INTO edges(src, dst, type) "
            "VALUES('src/handler.py', 'src/auth.py', 'imports')",
        )
        store.conn.commit()
        crit.add_critical(
            store, "src/auth.py", reason=GOOD_REASON_CRITICAL,
            category="security", self_cosign=True, blast_radius_hops=1,
        )
        # Editing the DEPENDENT (handler) should surface the critical
        # warning from auth.py with via='blast-radius'.
        result = mv.open_editing_lease(
            store, "src/handler.py", reason=GOOD_EDIT_REASON,
        )
        assert "critical_prelude" in result
        assert "blast-radius" in result["critical_prelude"]
        crit_notes = result.get("critical_notes") or []
        assert any(c.get("via") == "blast-radius" for c in crit_notes)

    def test_blast_radius_blocks_dependent_too(self, store):
        _seed(store, "src/auth.py")
        _seed(store, "src/handler.py")
        store.conn.execute(
            "INSERT INTO edges(src, dst, type) "
            "VALUES('src/handler.py', 'src/auth.py', 'imports')",
        )
        store.conn.commit()
        crit.add_critical(
            store, "src/auth.py", reason=GOOD_REASON_CRITICAL,
            category="security", self_cosign=True,
            blast_radius_hops=1, blocks_edits=True,
        )
        result = mv.open_editing_lease(
            store, "src/handler.py", reason=GOOD_EDIT_REASON,
        )
        assert result["lease_state"] == "pending_approval"


# ---------------------------------------------------------------------------
# CLI integration (subprocess)
# ---------------------------------------------------------------------------

import subprocess
import sys


def _run_cli(args, *, cwd):
    return subprocess.run(
        [sys.executable, "-m", "projmem.cli", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


def test_cli_critical_add_requires_cosigner(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f(): pass\n")
    _run_cli(["index"], cwd=repo)
    rc = _run_cli(
        ["critical", "add", "a.py",
         "--reason", GOOD_REASON_CRITICAL,
         "--category", "security", "--json"],
        cwd=repo,
    )
    assert rc.returncode == 2, rc.stderr
    err = json.loads(rc.stdout)
    assert err["error"] == "cosigner-required"


def test_cli_critical_full_round_trip(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f(): pass\n")
    _run_cli(["index"], cwd=repo)
    rc = _run_cli(
        ["critical", "add", "a.py",
         "--reason", GOOD_REASON_CRITICAL,
         "--category", "security",
         "--self-cosign",
         "--incident-ref", "INC-204",
         "--review-window-days", "30",
         "--json"],
        cwd=repo,
    )
    assert rc.returncode == 0, rc.stderr
    added = json.loads(rc.stdout)
    assert added["category"] == "security"

    rc = _run_cli(["critical", "list", "--json"], cwd=repo)
    listed = json.loads(rc.stdout)
    assert len(listed["critical_notes"]) == 1

    # Editing returns pending_approval + critical_prelude.
    rc = _run_cli(
        ["editing", "a.py",
         "--reason", "trying to simplify the token validation flow", "--json"],
        cwd=repo,
    )
    assert rc.returncode == 0
    e = json.loads(rc.stdout)
    assert e["lease_state"] == "pending_approval"
    assert "⚠ CRITICAL CONTEXT" in e.get("critical_prelude", "")
