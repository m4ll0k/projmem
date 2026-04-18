"""Regression tests for the ref → symbol_id binding pass.

Contract:
  - Same-file binding: a ref whose name has a def in the same file binds
    to that def's symbol_id.
  - Imported binding: a ref whose file imports a file that uniquely
    defines the name binds across files.
  - Global-unique binding: a ref with a globally-unique name binds
    regardless of imports.
  - Ambiguous refs stay NULL (target_symbol_id) — never a wrong bind.
  - Binding summary reflects counts correctly.
  - `projmem stats` surfaces `ref_binding` block.
  - Doctor flags low_ref_binding when bind rate is < 60%.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from projmem import binding as _binding
from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem.ts_backend import available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


def _indexed(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    return cfg, store


def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Same-file binding
# ---------------------------------------------------------------------------

@needs_ts
def test_same_file_binding_resolved(tmp_path):
    cfg, store = _indexed(tmp_path, {
        "a.js": (
            "export function leaf() {}\n"
            "export function caller() { leaf(); }\n"
        )
    })
    # The call of leaf() inside caller should bind to leaf's symbol_id.
    row = store.conn.execute(
        "SELECT target_symbol_id FROM refs "
        "WHERE name='leaf' AND kind='call' AND file='a.js'").fetchone()
    store.close()
    assert row is not None
    assert row["target_symbol_id"] is not None


# ---------------------------------------------------------------------------
# Imported binding
# ---------------------------------------------------------------------------

@needs_ts
def test_imported_binding_resolved(tmp_path):
    cfg, store = _indexed(tmp_path, {
        "src/leaf.js":   "export function target() { return 1; }\n",
        "src/caller.js": (
            "import { target } from './leaf';\n"
            "export function outer() { target(); }\n"
        )
    })
    # The call of target() in caller.js should bind to leaf.js#target.
    row = store.conn.execute(
        "SELECT target_symbol_id FROM refs "
        "WHERE name='target' AND kind='call' AND file='src/caller.js'"
    ).fetchone()
    store.close()
    assert row is not None
    assert row["target_symbol_id"] is not None
    assert "leaf" in row["target_symbol_id"]


# ---------------------------------------------------------------------------
# Global-unique binding
# ---------------------------------------------------------------------------

@needs_ts
def test_global_unique_binding(tmp_path):
    """A call to a globally-unique name (no import edge required) still
    binds via tier-3."""
    cfg, store = _indexed(tmp_path, {
        "src/lib.js":  "export function rare_unique_name() { return 1; }\n",
        "src/user.js": "rare_unique_name();\n",
    })
    row = store.conn.execute(
        "SELECT target_symbol_id FROM refs "
        "WHERE name='rare_unique_name' AND file='src/user.js'"
    ).fetchone()
    store.close()
    # It's okay if imports bind it first; either way, it must be bound.
    assert row is not None
    assert row["target_symbol_id"] is not None


# ---------------------------------------------------------------------------
# Ambiguity: no wrong bind
# ---------------------------------------------------------------------------

@needs_ts
def test_ambiguous_name_stays_unbound(tmp_path):
    """When a name has multiple defs across unrelated files and no
    import edge disambiguates, the ref must NOT be bound."""
    cfg, store = _indexed(tmp_path, {
        # Two unrelated defs of `handler`.
        "src/a.js":    "export function handler() { return 1; }\n",
        "src/b.js":    "export function handler() { return 2; }\n",
        # A caller that imports NEITHER — purely name-based.
        "src/c.js":    "handler();\n",
    })
    row = store.conn.execute(
        "SELECT target_symbol_id FROM refs "
        "WHERE name='handler' AND file='src/c.js'").fetchone()
    store.close()
    # Must be NULL — we never want a wrong binding.
    assert row is not None
    assert row["target_symbol_id"] is None


# ---------------------------------------------------------------------------
# Summary + stats surface
# ---------------------------------------------------------------------------

@needs_ts
def test_binding_summary_counts(tmp_path):
    cfg, store = _indexed(tmp_path, {
        "a.js": (
            "export function targetFn() {}\n"
            "export function callerFn() { targetFn(); }\n"
        )
    })
    summary = _binding.binding_summary(store)
    store.close()
    assert summary["total_refs"] >= 1
    assert summary["bound_refs"] >= 1
    assert 0 <= summary["bound_pct"] <= 100


@needs_ts
def test_stats_includes_ref_binding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text(
        "export function leaf() {}\n"
        "export function caller() { leaf(); }\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(repo), "stats"])
    data = json.loads(out)
    assert "ref_binding" in data
    for k in ("total_refs", "bound_refs", "unbound_refs", "bound_pct"):
        assert k in data["ref_binding"]


# ---------------------------------------------------------------------------
# resolve_refs is idempotent
# ---------------------------------------------------------------------------

@needs_ts
def test_resolve_refs_idempotent(tmp_path):
    cfg, store = _indexed(tmp_path, {
        "a.js": (
            "export function leafFn() {}\n"
            "export function callerFn() { leafFn(); }\n"
        )
    })
    # First run was during index_all. Second run should find everything
    # already bound.
    counts = _binding.resolve_refs(store)
    store.close()
    # "already_bound" should be > 0; there should be zero NEW bindings.
    assert counts["already_bound"] > 0
    assert counts["bound_same_file"] == 0
    assert counts["bound_imported"] == 0
