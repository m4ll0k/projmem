"""Regression tests for read-time freshness probing.

Contract:
  - check_paths() compares on-disk SHA-1 vs stored hash; divergent files
    produce a stale record with reason="hash-mismatch".
  - Missing files produce reason="file-missing".
  - Not-indexed paths are SKIPPED (no stale record).
  - freshness_warning() returns None for empty input, a HIGH-severity block
    for non-empty.
  - cmd_symbol, cmd_reverse, cmd_contracts, cmd_pack include
    freshness_warning when a touched file's on-disk content diverges.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from projmem import freshness as _fresh
from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store


# ---------------------------------------------------------------------------
# check_paths / freshness_warning — pure
# ---------------------------------------------------------------------------

def test_check_paths_returns_empty_when_nothing_drifted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    stale = _fresh.check_paths(store, cfg.root, ["a.py"])
    store.close()
    assert stale == []


def test_check_paths_detects_hash_mismatch(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    fp = root / "a.py"
    fp.write_text("x = 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    # Modify the file — hash now diverges.
    fp.write_text("x = 999\n")
    stale = _fresh.check_paths(store, cfg.root, ["a.py"])
    store.close()
    assert len(stale) == 1
    r = stale[0]
    assert r["path"] == "a.py"
    assert r["reason"] == "hash-mismatch"
    assert r["indexed_hash"] != r["current_hash"]


def test_check_paths_detects_missing_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    fp = root / "a.py"
    fp.write_text("x = 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    fp.unlink()
    stale = _fresh.check_paths(store, cfg.root, ["a.py"])
    store.close()
    assert len(stale) == 1
    assert stale[0]["reason"] == "file-missing"
    assert stale[0]["current_hash"] is None


def test_check_paths_skips_unindexed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    stale = _fresh.check_paths(store, cfg.root, ["never-indexed.py"])
    store.close()
    assert stale == []


def test_freshness_warning_empty_returns_none():
    assert _fresh.freshness_warning([]) is None


def test_freshness_warning_populates_all_fields():
    stale = [{"path": "a.py", "indexed_hash": "abc",
              "current_hash": "def", "reason": "hash-mismatch"}]
    w = _fresh.freshness_warning(stale)
    assert w["severity"] == "high"
    assert w["stale_file_count"] == 1
    assert w["paths"] == ["a.py"]
    assert "hash-mismatch" in w["reasons"]
    assert "projmem index" in w["message"]


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


def test_cmd_symbol_surfaces_freshness_warning(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    fp = repo / "src" / "lib.py"
    fp.write_text("def known_fn():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # Mutate after index
    fp.write_text("def known_fn():\n    return 9999\n")
    rc, out = _run_cli(["--path", str(repo), "symbol", "known_fn",
                         "--allow-ambiguous", "--no-implicit-check"])
    assert rc == 0
    data = json.loads(out)
    assert "freshness_warning" in data, (
        f"Expected freshness_warning on stale query; keys: {list(data.keys())}")
    assert data["freshness_warning"]["stale_file_count"] == 1
    paths = data["freshness_warning"]["paths"]
    assert "src/lib.py" in paths


def test_cmd_symbol_no_warning_when_fresh(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("def a():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "symbol", "a",
                         "--allow-ambiguous", "--no-implicit-check"])
    data = json.loads(out)
    assert "freshness_warning" not in data


def test_cmd_reverse_surfaces_freshness_warning(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    core = repo / "src" / "core.py"
    core.write_text("x = 1\n")
    (repo / "src" / "uses.py").write_text("from core import x\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    core.write_text("x = 2\n")
    rc, out = _run_cli(["--path", str(repo), "reverse", "src/core.py"])
    data = json.loads(out)
    # P0#3: drift on the target file is now AUTO-REFRESHED in-place by
    # default. The output reports `auto_refreshed`; freshness_warning
    # only fires for files we couldn't refresh (over threshold or
    # missing).
    assert data.get("auto_refreshed") == ["src/core.py"]
    assert "freshness_warning" not in data or data["freshness_warning"] is None


def test_cmd_reverse_no_auto_refresh_falls_back_to_warning(tmp_path):
    """--no-auto-refresh disables the lazy refresh; freshness_warning
    becomes the only signal again."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    core = repo / "src" / "core.py"
    core.write_text("x = 1\n")
    (repo / "src" / "uses.py").write_text("from core import x\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    core.write_text("x = 2\n")
    rc, out = _run_cli(["--path", str(repo), "reverse", "src/core.py",
                         "--no-auto-refresh"])
    data = json.loads(out)
    assert "freshness_warning" in data
    assert "auto_refreshed" not in data


def test_cmd_contracts_surfaces_freshness_warning(tmp_path):
    """contracts doesn't auto-refresh (no canonical target file when
    target is a contract NAME instead of a file path), so it still
    surfaces freshness_warning when its touched files drift."""
    repo = tmp_path / "repo"
    repo.mkdir()
    fp = repo / "a.js"
    fp.write_text("const m = process.env.MY_VAR;\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    fp.write_text("const m = process.env.OTHER_VAR;\n")
    rc, out = _run_cli(["--path", str(repo), "contracts", "a.js",
                         "--kind", "env"])
    data = json.loads(out)
    assert "freshness_warning" in data


def test_cmd_pack_surfaces_auto_refresh_on_drift(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fp = repo / "a.py"
    fp.write_text("def go():\n    return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    fp.write_text("def go():\n    return 22222\n")
    rc, out = _run_cli(["--path", str(repo), "pack", "a.py"])
    data = json.loads(out)
    # Pack on a stale target file: auto-refreshes the target.
    assert data.get("auto_refreshed") == ["a.py"]
