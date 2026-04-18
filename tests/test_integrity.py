"""Tests for projmem/integrity.py and its packs.py integration.

The spec points:
  #1  Confidence decay        — staleness transitions decay confidence
  #3  Structured claim schema — evidence/assumptions round-trip
  #4  Contradiction detection — tier_contradiction when kind says
                                 'safe' but code has BUG marker
  #5  Freshness-aware ordering — fresh first in pack output
  #6  Revalidation on access  — fingerprint drift → staleness update
  #9  Per-target integrity    — composite + factors emitted
  #10 Ambiguity detection     — same-name symbols flagged
  #14 Change-impact hashing   — fingerprint detects file/symbol drift
  #15 Truth classification    — truth_class persisted and returned
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from projmem.store import Store
from projmem import integrity as I


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def store_with_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    s = Store(str(tmp_path / "db.sqlite"))
    yield s, str(repo)
    s.close()


# ---- #14 fingerprinting -------------------------------------------------


def test_fingerprint_captures_file_and_symbol(store_with_file):
    s, repo = store_with_file
    fp = I.compute_fingerprint(s, repo, "a.py#foo")
    assert fp.file_hash is not None
    assert fp.symbol_hash is not None
    assert fp.callers_hash is not None
    assert fp.contracts_hash is None


def test_fingerprint_stable_for_unchanged_file(store_with_file):
    s, repo = store_with_file
    fp1 = I.compute_fingerprint(s, repo, "a.py#foo")
    fp2 = I.compute_fingerprint(s, repo, "a.py#foo")
    assert fp1.to_dict() == fp2.to_dict()


def test_fingerprint_changes_when_file_changes(store_with_file):
    s, repo = store_with_file
    fp1 = I.compute_fingerprint(s, repo, "a.py#foo")
    Path(repo, "a.py").write_text("def foo():\n    return 2\n")
    fp2 = I.compute_fingerprint(s, repo, "a.py#foo")
    assert fp1.file_hash != fp2.file_hash
    assert fp1.symbol_hash != fp2.symbol_hash


# ---- #6 revalidation ----------------------------------------------------


def test_revalidate_fresh_when_nothing_changed(store_with_file):
    s, repo = store_with_file
    fp = I.compute_fingerprint(s, repo, "a.py#foo")
    aid = s.add_annotation("a.py#foo", "verified-safe", "ok",
                           fingerprint=fp.to_dict(), confidence=0.9)
    row = s.list_annotations()[0]
    r = I.revalidate_annotation(s, repo, row)
    assert r.now == I.FRESH
    assert r.drifted_fields == []


def test_revalidate_strongly_stale_when_file_and_symbol_changed(store_with_file):
    s, repo = store_with_file
    fp = I.compute_fingerprint(s, repo, "a.py#foo")
    s.add_annotation("a.py#foo", "verified-safe", "ok",
                     fingerprint=fp.to_dict(), confidence=0.9)
    Path(repo, "a.py").write_text(
        "def foo():\n    return 42  # different\n")
    row = s.list_annotations()[0]
    r = I.revalidate_annotation(s, repo, row)
    assert r.now == I.STRONGLY_STALE
    assert set(r.drifted_fields) == {"file_hash", "symbol_hash"}


# ---- #1 confidence decay ------------------------------------------------


def test_confidence_decays_with_staleness(store_with_file):
    s, repo = store_with_file
    fp = I.compute_fingerprint(s, repo, "a.py#foo")
    s.add_annotation("a.py#foo", "verified-safe", "ok",
                     fingerprint=fp.to_dict(), confidence=0.9)
    Path(repo, "a.py").write_text("def foo():\n    return 42\n")
    row = s.list_annotations()[0]
    r = I.revalidate_annotation(s, repo, row)
    # Strongly stale decays to 0.25·prev + tiny bump < 0.3
    assert r.new_confidence < 0.3
    assert r.new_confidence > 0.0


def test_note_recovers_after_restore(store_with_file):
    """Regression: projmem must support drift → restore → recovery.

    If the code returns to the same fingerprint the note was written
    against, the note must become recovered/fresh again and its confidence
    must restore to the asserted baseline (not stay permanently decayed).
    """
    import json
    s, repo = store_with_file
    fp0 = I.compute_fingerprint(s, repo, "a.py#foo")
    s.add_annotation("a.py#foo", "verified-safe", "ok",
                     fingerprint=fp0.to_dict(), confidence=0.9)

    # Drift.
    Path(repo, "a.py").write_text("def foo():\n    return 42\n")
    row = s.list_annotations()[0]
    r1 = I.revalidate_annotation(s, repo, row)
    assert r1.now in (I.WEAKLY_STALE, I.STRONGLY_STALE)
    assert r1.new_confidence < 0.9

    # Baseline fingerprint must remain the original (do not overwrite on verify).
    row_after = s.list_annotations()[0]
    assert json.loads(row_after["fingerprint"]) == fp0.to_dict()

    # Restore.
    Path(repo, "a.py").write_text("def foo():\n    return 1\n")
    row2 = s.list_annotations()[0]
    r2 = I.revalidate_annotation(s, repo, row2)
    assert r2.now in (I.RECOVERED, I.FRESH)
    assert abs(r2.new_confidence - 0.9) < 1e-6


def test_confidence_does_not_compound_on_repeated_verify(store_with_file):
    """Repeated verify on a stable stale target should not ratchet confidence
    toward 0. Confidence should be derived from baseline × staleness."""
    s, repo = store_with_file
    fp0 = I.compute_fingerprint(s, repo, "a.py#foo")
    s.add_annotation("a.py#foo", "verified-safe", "ok",
                     fingerprint=fp0.to_dict(), confidence=0.8)
    Path(repo, "a.py").write_text("def foo():\n    return 42\n")
    row = s.list_annotations()[0]
    r1 = I.revalidate_annotation(s, repo, row)
    row2 = s.list_annotations()[0]
    r2 = I.revalidate_annotation(s, repo, row2)
    assert r2.now == r1.now
    assert abs(r2.new_confidence - r1.new_confidence) < 1e-6


# ---- #4 contradiction detection -----------------------------------------


def test_tier_contradiction_on_bug_marker(store_with_file):
    s, repo = store_with_file
    s.add_annotation("a.py#foo", "verified-safe",
                     "function is safe", confidence=0.9)
    Path(repo, "a.py").write_text(
        "def foo():\n    # BUG: race condition\n    return 2\n")
    conflicts = I.detect_contradictions(s, repo, "a.py#foo")
    markers = [c.marker for c in conflicts]
    assert "tier_contradiction" in markers
    c = next(c for c in conflicts if c.marker == "tier_contradiction")
    assert c.severity == "high"


def test_kind_conflict_detected_on_pair(store_with_file):
    s, repo = store_with_file
    s.add_annotation("a.py", "verified-safe", "safe")
    s.add_annotation("a.py", "refute", "no, it isn't")
    conflicts = I.detect_contradictions(s, repo, "a.py")
    assert any(c.marker == "kind_conflict" for c in conflicts)


def test_file_deleted_conflict(store_with_file):
    s, repo = store_with_file
    s.add_annotation("ghost.py", "note", "this file won't exist")
    conflicts = I.detect_contradictions(s, repo, "ghost.py")
    assert any(c.marker == "file_deleted" for c in conflicts)


# ---- #3 structured claim schema round-trip ------------------------------


def test_evidence_and_truth_class_persist(store_with_file):
    s, _ = store_with_file
    aid = s.add_annotation(
        "foo.py#bar", "refute", "not vulnerable",
        evidence=[{"file": "foo.py", "line": 12, "note": "bounds check"}],
        assumptions=["amount > 0"],
        truth_class="FACT", scope="symbol", confidence=0.95)
    row = s.list_annotations()[0]
    assert row["truth_class"] == "FACT"
    assert row["scope"] == "symbol"
    assert row["confidence"] == 0.95
    import json
    assert json.loads(row["evidence"])[0]["line"] == 12
    assert json.loads(row["assumptions"]) == ["amount > 0"]


# ---- #9 integrity score -------------------------------------------------


def test_integrity_score_between_zero_and_one(store_with_file):
    s, repo = store_with_file
    isc = I.integrity_score(s, repo, "a.py#foo")
    assert 0.0 <= isc.score <= 1.0
    assert set(isc.factors) >= {
        "retrieval_confidence", "note_freshness",
        "contradiction_penalty", "ambiguity_penalty", "structural_coverage"}


def test_integrity_score_drops_on_contradiction(store_with_file):
    s, repo = store_with_file
    # Baseline — no notes.
    isc0 = I.integrity_score(s, repo, "a.py#foo")
    # Add contradicted note — verified-safe vs BUG marker in code.
    s.add_annotation("a.py#foo", "verified-safe", "safe", confidence=0.9)
    Path(repo, "a.py").write_text(
        "def foo():\n    # BUG: crash path\n    return None\n")
    isc1 = I.integrity_score(s, repo, "a.py#foo")
    assert isc1.score < isc0.score
    assert any("CONTRADICTION" in g for g in isc1.guidance)


# ---- #5 sort order ------------------------------------------------------


def test_sort_annotations_fresh_first():
    rows = [
        {"id": 1, "staleness": "contradicted", "confidence": 0.1,
         "created_at": 3},
        {"id": 2, "staleness": "fresh",        "confidence": 0.9,
         "created_at": 1},
        {"id": 3, "staleness": "weakly_stale", "confidence": 0.5,
         "created_at": 2},
    ]
    out = I.sort_annotations_for_pack(rows)
    assert [r["id"] for r in out] == [2, 3, 1]


# ---- #10 ambiguity ------------------------------------------------------


def test_same_name_symbols_trigger_ambiguity(store_with_file):
    s, _ = store_with_file
    # Simulate index: ``step`` defined in 3 files.
    for f in ("a.py", "b.py", "c.py"):
        s.add_symbol(file=f, name="step", kind="function",
                     line=1, col=0, exported=1, confidence="high")
    s.commit()
    ambig = I.ambiguity_for_target(s, "any.py#step")
    assert len(ambig) == 1
    assert ambig[0].kind == "same_name"
    assert ambig[0].severity == "high"
    assert set(ambig[0].files) == {"a.py", "b.py", "c.py"}


# ---- backward compatibility --------------------------------------------


def test_legacy_note_without_new_fields_loads(store_with_file):
    s, _ = store_with_file
    aid = s.add_annotation("a.py", "note", "plain old note")
    rows = s.list_annotations()
    assert rows[0]["kind"] == "note"
    # Defaults apply.
    assert rows[0]["confidence"] == 0.5
    assert rows[0]["truth_class"] == "INFERENCE"
    assert rows[0]["staleness"] == "unknown"


def test_legacy_note_still_appears_in_pack_style_lookup(store_with_file):
    s, _ = store_with_file
    s.add_annotation("a.py", "note", "plain")
    notes = s.annotations_for_pack(file="a.py")
    assert len(notes) == 1
    assert notes[0]["body"] == "plain"
