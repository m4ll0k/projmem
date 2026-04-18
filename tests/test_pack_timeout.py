"""Regression tests for projmem pack --timeout / timeout_secs.

Contract:
  - A pack with timeout_secs=<very_small> must still return a valid pack dict.
  - When the deadline expires, pack["timed_out"] == True.
  - pack["timeout_truncated_at"] names the section where the build stopped.
  - Without a timeout (default), pack does NOT contain "timed_out".
  - The partial pack always contains at minimum "target", "coverage", "meta".
  - include_snippets and include_source sections are skipped when timed out.
"""
from __future__ import annotations

import os
import json
import io
import sys

import pytest

from projmem.store import Store
from projmem.config import Config
from projmem.ts_backend import index as ts_index, available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


def _make_project(tmp_path) -> tuple:
    """Create a minimal 2-file project, index it, return (cfg, store)."""
    root = str(tmp_path)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "a.py"), "w") as f:
        f.write("def foo():\n    return 1\n")
    with open(os.path.join(root, "b.py"), "w") as f:
        f.write("from a import foo\ndef bar():\n    return foo()\n")
    cfg = Config(root=root)
    store = Store(cfg.db_path)
    from projmem.indexer import index_all
    index_all(cfg, store)
    return cfg, store


# ---------------------------------------------------------------------------
# No timeout: result must NOT contain timed_out key
# ---------------------------------------------------------------------------

def test_no_timeout_no_timed_out_key(tmp_path):
    from projmem.packs import build_pack
    cfg, store = _make_project(tmp_path)
    pack = build_pack(cfg, store, "a.py")
    store.close()
    assert "timed_out" not in pack, (
        "pack must not have timed_out key when timeout is not set")


# ---------------------------------------------------------------------------
# Absurdly short timeout: must return a partial pack, not raise
# ---------------------------------------------------------------------------

def test_extremely_short_timeout_returns_valid_pack(tmp_path):
    from projmem.packs import build_pack
    cfg, store = _make_project(tmp_path)
    # 1 nanosecond — guaranteed to expire before anything meaningful runs
    pack = build_pack(cfg, store, "a.py", timeout_secs=1e-9)
    store.close()
    # Must still be a dict with the mandatory keys
    assert isinstance(pack, dict)
    assert "target" in pack
    assert "meta" in pack
    assert "coverage" in pack
    assert pack["timed_out"] is True
    assert "timeout_truncated_at" in pack


def test_timeout_key_in_meta(tmp_path):
    """timeout_secs must appear in pack.meta when a timeout was set."""
    from projmem.packs import build_pack
    cfg, store = _make_project(tmp_path)
    pack = build_pack(cfg, store, "a.py", timeout_secs=0.001)
    store.close()
    assert "timeout_secs" in pack.get("meta", {}), (
        "meta must contain timeout_secs when a timeout is configured")


def test_generous_timeout_does_not_truncate(tmp_path):
    """A 60s timeout on a tiny project should not trigger timed_out."""
    from projmem.packs import build_pack
    cfg, store = _make_project(tmp_path)
    pack = build_pack(cfg, store, "a.py", timeout_secs=60.0)
    store.close()
    assert pack.get("timed_out") is not True, (
        "A 60s timeout on a tiny project should complete without truncating")


# ---------------------------------------------------------------------------
# include_snippets is skipped when already timed out before that section
# ---------------------------------------------------------------------------

def test_snippets_skipped_when_timed_out(tmp_path):
    """When timeout fires before the snippets section, pack must NOT have
    a snippets key — the section is skipped entirely, not partially filled."""
    from projmem.packs import build_pack
    cfg, store = _make_project(tmp_path)
    # Force immediate timeout
    pack = build_pack(cfg, store, "a.py",
                      include_snippets=True, timeout_secs=1e-9)
    store.close()
    assert pack.get("timed_out") is True
    # snippets must be absent or None when timed out before that section
    assert "snippets" not in pack or pack.get("snippets") is None, (
        "snippets must not be populated when timed out before the snippets section")


# ---------------------------------------------------------------------------
# CLI: --timeout flag is accepted and surfaced in output
# ---------------------------------------------------------------------------

def test_cli_timeout_flag_accepted(tmp_path):
    """projmem pack --timeout 60 must complete without error on a tiny project."""
    from projmem.cli import main

    root = str(tmp_path)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "x.py"), "w") as f:
        f.write("def greet():\n    return 'hello'\n")

    buf = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(buf):
        rc = main(["--path", root, "index"])
    assert rc == 0

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc2 = main(["--path", root, "pack", "x.py", "--timeout", "60"])
    assert rc2 == 0
    data = json.loads(buf2.getvalue())
    assert "target" in data
    # A 60s timeout on a trivial project should complete without truncating
    assert data.get("timed_out") is not True


def test_cli_very_short_timeout_sets_timed_out(tmp_path):
    """projmem pack --timeout 0.000001 must return timed_out=true in output."""
    from projmem.cli import main

    root = str(tmp_path)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "y.py"), "w") as f:
        f.write("def work():\n    pass\n")

    buf = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(buf):
        rc = main(["--path", root, "index"])
    assert rc == 0

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc2 = main(["--path", root, "pack", "y.py", "--timeout", "0.000001"])
    assert rc2 == 0
    data = json.loads(buf2.getvalue())
    assert data.get("timed_out") is True
    assert "timeout_truncated_at" in data
