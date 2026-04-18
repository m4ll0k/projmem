"""Regression tests for the implicit-usage gap detector (projmem/implicit.py)
and its integration into cmd_symbol and integrity_score.

Design goals:
  1. is_identifier() rejects unsafe names (dotted, empty, metacharacters).
  2. count_text_occurrences() counts word-boundary hits and respects budgets.
  3. detect_implicit_usage() fires only when BOTH thresholds are exceeded;
     never injects new refs — only emits a warning.
  4. scan_text_matches() returns concrete match sites with file/line/col.
  5. cmd_symbol embeds the verdict under "implicit_usage" in JSON output.
  6. --no-implicit-check suppresses the scan entirely.
  7. integrity_score lowers macro_gap_penalty when the gap is large.

All fixture-building is purely in-memory / tempdir — no network, no real repo.
"""
from __future__ import annotations

import os
import json
import sys
import io
import textwrap

import pytest

from projmem import implicit as _impl
from projmem.store import Store
from projmem.ts_backend import index as ts_index, available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


# ---------------------------------------------------------------------------
# 1. is_identifier()
# ---------------------------------------------------------------------------

def test_is_identifier_plain():
    assert _impl.is_identifier("SafeGetenv")
    assert _impl.is_identifier("_private")
    assert _impl.is_identifier("camelCase")
    assert _impl.is_identifier("ALL_CAPS")


def test_is_identifier_rejects_dotted():
    assert not _impl.is_identifier("os.path")
    assert not _impl.is_identifier("a.b.c")


def test_is_identifier_rejects_hash_qualified():
    assert not _impl.is_identifier("file#sym")


def test_is_identifier_rejects_empty():
    assert not _impl.is_identifier("")


def test_is_identifier_rejects_regex_metacharacters():
    assert not _impl.is_identifier("foo(bar)")
    assert not _impl.is_identifier("foo*")
    assert not _impl.is_identifier("foo+bar")
    assert not _impl.is_identifier("foo.bar")


def test_is_identifier_rejects_leading_digit():
    assert not _impl.is_identifier("1bad")


# ---------------------------------------------------------------------------
# 2. count_text_occurrences()
# ---------------------------------------------------------------------------

def _make_store_and_files(tmp_path, files: dict) -> tuple:
    """Write files to tmp_path, index them, return (store, root)."""
    root = str(tmp_path)
    store = Store(str(tmp_path / "idx.db"))
    for rel, src in files.items():
        abs_p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "w") as f:
            f.write(src)
        # Use a minimal "file" row so store.all_files() finds the path.
        store.conn.execute(
            "INSERT OR IGNORE INTO files(path, lang, hash, indexed_at) "
            "VALUES (?, ?, ?, ?)",
            (rel, "js", "aaa", 0))
    store.conn.commit()
    return store, root


def test_count_text_occurrences_basic(tmp_path):
    """Word-boundary hits are counted; partial matches are excluded."""
    store, root = _make_store_and_files(tmp_path, {
        "a.js": "function SafeGetenv() {}\nSafeGetenv();\nSafeGetenv();\n",
        "b.js": "// SafeGetenv is used here\nconst x = SafeGetenv('KEY');\n",
        # Should NOT match "SafeGetenvExtra" as a hit for "SafeGetenv"
        "c.js": "function SafeGetenvExtra() {}\n",
    })
    r = _impl.count_text_occurrences(store, root, "SafeGetenv")
    # a.js: 3 hits, b.js: 2 hits. c.js: SafeGetenvExtra doesn't match \b
    assert r["text_count"] == 5, (
        f"Expected 5 word-boundary hits, got {r['text_count']}")
    assert r["files_with_hits"] == 2
    assert r["files_scanned"] == 3
    assert not r["truncated"]


def test_count_text_occurrences_file_budget(tmp_path):
    """file_budget=1 stops after scanning the first file."""
    store, root = _make_store_and_files(tmp_path, {
        "a.js": "MACRO_FOO;\n",
        "b.js": "MACRO_FOO; MACRO_FOO;\n",
    })
    r = _impl.count_text_occurrences(store, root, "MACRO_FOO", file_budget=1)
    assert r["truncated"] is True
    assert r["files_scanned"] == 1


def test_count_text_occurrences_non_identifier(tmp_path):
    """A dotted name must be rejected with skipped_reason, count=0."""
    store = Store(str(tmp_path / "idx.db"))
    r = _impl.count_text_occurrences(store, str(tmp_path), "os.path")
    assert r["text_count"] == 0
    assert "skipped_reason" in r


def test_count_text_occurrences_missing_file_skipped(tmp_path):
    """Files registered in the index but absent on disk count as skipped."""
    store = Store(str(tmp_path / "idx.db"))
    store.conn.execute(
        "INSERT OR IGNORE INTO files(path, lang, hash, indexed_at) "
        "VALUES (?, ?, ?, ?)",
        ("ghost.js", "js", "aaa", 0))
    store.conn.commit()
    r = _impl.count_text_occurrences(store, str(tmp_path), "something")
    assert r["files_skipped"] >= 1


# ---------------------------------------------------------------------------
# 3. detect_implicit_usage()
# ---------------------------------------------------------------------------

def test_detect_implicit_usage_fires_above_both_thresholds():
    """Both conditions (gap >= 5 AND ratio >= 1.25) must hold."""
    v = _impl.detect_implicit_usage(structured_count=4, text_count=20)
    assert v["implicit_refs_detected"] is True
    assert "warning" in v
    assert v["missing_estimate_upper_bound"] == 16


def test_detect_implicit_usage_no_fire_gap_too_small():
    """Small absolute gap (< 5) must NOT fire even if ratio is high."""
    v = _impl.detect_implicit_usage(structured_count=1, text_count=3)
    # gap=2 < 5 → no fire
    assert v["implicit_refs_detected"] is False
    assert "warning" not in v


def test_detect_implicit_usage_no_fire_ratio_too_small():
    """Large absolute gap but ratio < 1.25 must NOT fire."""
    # structured=100, text=104 → gap=4 < 5, ratio=1.04 < 1.25
    v = _impl.detect_implicit_usage(structured_count=100, text_count=104)
    assert v["implicit_refs_detected"] is False


def test_detect_implicit_usage_no_fire_when_text_zero():
    """Zero text matches means no implicit usage."""
    v = _impl.detect_implicit_usage(structured_count=10, text_count=0)
    assert v["implicit_refs_detected"] is False


def test_detect_implicit_usage_zero_structured_fires():
    """structured=0, text=10 → ratio=inf. Gap=10 >= 5 → fires."""
    v = _impl.detect_implicit_usage(structured_count=0, text_count=10)
    assert v["implicit_refs_detected"] is True
    assert v["text_to_structured_ratio"] is None  # inf encoded as None


def test_detect_implicit_usage_truncated_adds_note():
    """text_scan_truncated=True must add a cautionary note."""
    v = _impl.detect_implicit_usage(structured_count=2, text_count=20,
                                     text_scan_truncated=True)
    assert v["implicit_refs_detected"] is True
    notes = v.get("notes") or []
    assert any("lower bound" in n for n in notes)


def test_detect_implicit_usage_does_not_add_refs():
    """Firing implicit detection must NOT add new structured refs — it is
    purely an advisory verdict. This test is structural: verify the return
    value has no 'refs' or 'defs' key that could be mistaken for real refs."""
    v = _impl.detect_implicit_usage(structured_count=2, text_count=30)
    assert "refs" not in v
    assert "defs" not in v
    assert "structured_refs" not in v  # no aliased injection


# ---------------------------------------------------------------------------
# 4. scan_text_matches() — exhaustive scan with site list
# ---------------------------------------------------------------------------

def test_scan_text_matches_returns_sites(tmp_path):
    """Match sites include file, line, col, and text fields."""
    store, root = _make_store_and_files(tmp_path, {
        "a.js": "// MY_MACRO usage\nMY_MACRO(1);\nMY_MACRO(2);\n",
    })
    r = _impl.scan_text_matches(store, root, "MY_MACRO")
    assert r["text_count"] == 3
    sites = r["matches"]
    assert len(sites) == 3
    for site in sites:
        assert "file" in site
        assert "line" in site
        assert "col" in site
        assert "text" in site
    # First match is line 1 (comment), col 4
    assert sites[0]["line"] == 1
    assert "MY_MACRO" in sites[0]["text"]


def test_scan_text_matches_truncation(tmp_path):
    """capture_limit caps the returned sites list but count is still total."""
    lines = "\n".join(f"MY_SYM_{i}; MY_SYM;" for i in range(100))
    store, root = _make_store_and_files(tmp_path, {"a.js": lines})
    r = _impl.scan_text_matches(store, root, "MY_SYM", capture_limit=5)
    assert r["text_count"] >= 100
    assert len(r["matches"]) == 5
    assert r["matches_truncated"] is True


def test_scan_text_matches_non_identifier(tmp_path):
    """Non-identifier name returns zero count and skipped_reason."""
    store = Store(str(tmp_path / "idx.db"))
    r = _impl.scan_text_matches(store, str(tmp_path), "foo.bar")
    assert r["text_count"] == 0
    assert "skipped_reason" in r


# ---------------------------------------------------------------------------
# 5. Integration: cmd_symbol embeds implicit_usage in JSON output
# ---------------------------------------------------------------------------

@needs_ts
def test_cmd_symbol_implicit_usage_in_output(tmp_path):
    """When a symbol appears far more in raw text than in structured refs,
    cmd_symbol must include implicit_usage.implicit_refs_detected=True
    in its JSON output — without injecting new refs into the index."""
    from projmem.config import Config
    from projmem.indexer import index_all

    root = str(tmp_path)
    # A symbol defined once but used 15x via a macro-like pattern that
    # tree-sitter won't capture as call refs. We embed the name in string
    # literals to simulate macro expansion that bypasses AST.
    src = textwrap.dedent("""\
        export function DISPATCH_FN() { return 1; }
        // Simulate macro / table-driven dispatch — these are NOT call refs
        const handlers = {
          DISPATCH_FN: null, // DISPATCH_FN
          type1: "DISPATCH_FN",
          type2: "DISPATCH_FN",
          type3: "DISPATCH_FN",
          type4: "DISPATCH_FN",
          type5: "DISPATCH_FN",
          type6: "DISPATCH_FN",
          type7: "DISPATCH_FN",
          type8: "DISPATCH_FN",
          type9: "DISPATCH_FN",
          type10: "DISPATCH_FN",
          type11: "DISPATCH_FN",
          type12: "DISPATCH_FN",
        };
    """)
    with open(os.path.join(root, "a.js"), "w") as f:
        f.write(src)

    cfg = Config(root=root)
    store = Store(cfg.db_path)
    index_all(cfg, store)
    store.close()

    # Invoke cmd_symbol via CLI simulation
    import argparse
    from projmem.cli import cmd_symbol

    class FakeArgs:
        name = "DISPATCH_FN"
        path = root
        json = True
        file = None
        role = None
        allow_ambiguous = True
        no_implicit_check = False
        exhaustive = False

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        cmd_symbol(FakeArgs())
    finally:
        sys.stdout = old_stdout

    data = json.loads(buf.getvalue())
    assert "implicit_usage" in data, (
        f"Expected implicit_usage key in output; got keys: {list(data.keys())}")
    iu = data["implicit_usage"]
    # The definition + string occurrences should dwarf the call refs (0).
    # With 1 structured ref (def) and ~14+ text hits, gap >= 5 → fires.
    assert iu["implicit_refs_detected"] is True, (
        f"Expected implicit_refs_detected=True; verdict={iu}")
    assert "warning" in iu

    # CRITICAL: no new refs injected — structured count unchanged
    assert "refs" not in iu
    assert "defs" not in iu


@needs_ts
def test_cmd_symbol_no_implicit_check_suppresses_scan(tmp_path):
    """--no-implicit-check flag must suppress the implicit usage scan entirely."""
    from projmem.config import Config
    from projmem.indexer import index_all

    root = str(tmp_path)
    with open(os.path.join(root, "a.js"), "w") as f:
        f.write("export function FOO() {} // FOO FOO FOO FOO FOO FOO\n")

    cfg = Config(root=root)
    store = Store(cfg.db_path)
    index_all(cfg, store)
    store.close()

    import argparse
    from projmem.cli import cmd_symbol

    class FakeArgs:
        name = "FOO"
        path = root
        json = True
        file = None
        role = None
        allow_ambiguous = True
        no_implicit_check = True   # suppression flag
        exhaustive = False

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        cmd_symbol(FakeArgs())
    finally:
        sys.stdout = old_stdout

    data = json.loads(buf.getvalue())
    assert "implicit_usage" not in data, (
        "implicit_usage should be absent when --no-implicit-check is set; "
        f"got keys: {list(data.keys())}")


# ---------------------------------------------------------------------------
# 6. Integrity score is penalized when gap is detected
# ---------------------------------------------------------------------------

@needs_ts
def test_integrity_score_lowered_by_macro_gap(tmp_path):
    """When text occurrences greatly outnumber structured refs,
    integrity_score.factors['macro_gap_penalty'] must be < 1.0."""
    from projmem.config import Config
    from projmem.indexer import index_all
    from projmem.integrity import integrity_score

    root = str(tmp_path)
    # One real def; 20 string-based / comment references that bypass AST.
    lines = ["export function XMACRO_HANDLER() {}"]
    for i in range(20):
        lines.append(f'  // XMACRO_HANDLER case_{i}')
    with open(os.path.join(root, "xmacro.js"), "w") as f:
        f.write("\n".join(lines) + "\n")

    cfg = Config(root=root)
    store = Store(cfg.db_path)
    index_all(cfg, store)

    score = integrity_score(store, root, "xmacro.js#XMACRO_HANDLER")
    store.close()

    penalty = score.factors.get("macro_gap_penalty", 1.0)
    assert penalty < 1.0, (
        f"Expected macro_gap_penalty < 1.0 when text >> structured refs; "
        f"got {penalty}. Full score: {score.to_dict()}")


@needs_ts
def test_integrity_score_no_penalty_when_refs_match_text(tmp_path):
    """When structured refs closely match text occurrences,
    macro_gap_penalty stays at 1.0 (no penalty)."""
    from projmem.config import Config
    from projmem.indexer import index_all
    from projmem.integrity import integrity_score

    root = str(tmp_path)
    # One def, one real call — text occurrences == 2, structured == 2
    src = textwrap.dedent("""\
        export function normalFn() { return 1; }
        export function caller() { return normalFn(); }
    """)
    with open(os.path.join(root, "a.js"), "w") as f:
        f.write(src)

    cfg = Config(root=root)
    store = Store(cfg.db_path)
    index_all(cfg, store)

    score = integrity_score(store, root, "a.js#normalFn")
    store.close()

    penalty = score.factors.get("macro_gap_penalty", 1.0)
    # With 2 text hits and 2 structured (1 def + 1 call ref), gap < 5 → no penalty
    assert penalty == 1.0, (
        f"Expected macro_gap_penalty=1.0 when structured ≈ text; got {penalty}")
