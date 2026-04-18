"""Regressions for the default-path (Python AST + regex) runtime.

These tests MUST NOT be gated on tree-sitter: they cover exactly the
failure modes that slipped through when the existing test matrix was
mostly behind a tree-sitter availability check. If any of these fail,
the default `pip install projmem` install is broken.

Covered issues (from the round-of-fixes scope):
  1. Recursion-safe indexing — one pathological file cannot kill the run.
  2. `from . import X` resolves to the sibling module, not to __init__.py.
  3. `pack 'file.py#symbol'` returns real symbol_refs for that symbol.
  4. `--radius 1/2/3` produces progressively larger packs.
  5. External imports do NOT show up as "unresolved".
  6. Foreign-index DB is detected and refused unless overridden.
"""
from __future__ import annotations
import os
import pathlib
import sys
import textwrap

import pytest

from projmem import config as config_mod, indexer, packs, graph
from projmem.store import Store


def _index(root: str):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    return cfg, store


def _mkpkg(tmp_path: pathlib.Path, files: dict) -> str:
    """Drop a small synthetic project under `tmp_path` and return the root."""
    root = tmp_path / "proj"
    root.mkdir()
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return str(root)


# ---------------------------------------------------------------------------
# 1. Recursion-safe indexing
# ---------------------------------------------------------------------------

def test_pathological_attribute_chain_does_not_abort_run(tmp_path):
    """A single pathological file (400+ attribute segments) must NOT take
    down the whole index pass. Fallback to regex is acceptable, aborting
    the entire run is not. This reproduces the scip-python
    `maxParseDepth2.py` crash."""
    deep_chain = "x" + ".x" * 500
    files = {
        "good.py": "def greet():\n    return 'hi'\n",
        "bad.py": f"from typing import Any\n\n\ndef f(x: Any):\n    {deep_chain}\n",
    }
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    paths = {r["path"] for r in store.all_files()}
    # Both files must be in the index, even if bad.py was downgraded.
    assert "good.py" in paths
    assert "bad.py" in paths
    # The good file must still carry AST-quality symbols.
    defs = [dict(r) for r in store.symbols_by_name("greet")]
    assert any(d["file"] == "good.py" for d in defs), defs
    store.close()


# ---------------------------------------------------------------------------
# 2. `from . import X` resolution
# ---------------------------------------------------------------------------

def test_from_dot_import_resolves_to_sibling_module(tmp_path):
    """`from . import mod` inside `pkg/__init__.py` must produce a reverse-dep
    edge from __init__.py → pkg/mod.py. Previously the edge landed on the
    package dir (the `.` module), so reverse pkg/mod.py came up empty."""
    files = {
        "pkg/__init__.py": "from . import mod\n",
        "pkg/mod.py": "def hello():\n    return 42\n",
    }
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    rev = graph.reverse_deps(store, "pkg/mod.py")
    assert any(r["file"] == "pkg/__init__.py" for r in rev), rev
    store.close()


def test_from_dot_import_with_use_creates_cross_file_ref(tmp_path):
    """`from . import mod; mod.hello()` must record a ref to `mod` — an
    imported alias is a LEGITIMATE reference to the source module's export."""
    files = {
        "pkg/__init__.py": "from . import mod\n\n\ndef bootstrap():\n    return mod.hello()\n",
        "pkg/mod.py": "def hello():\n    return 42\n",
    }
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    mod_refs = [dict(r) for r in store.refs_by_name("mod")]
    assert any(r["file"] == "pkg/__init__.py" for r in mod_refs), mod_refs
    store.close()


# ---------------------------------------------------------------------------
# 3. `file.py#symbol` returns refs
# ---------------------------------------------------------------------------

def test_file_symbol_pack_has_nonempty_symbol_refs(tmp_path):
    """`pack 'a.py#helper'` must return symbol_refs for `helper`, not for
    the literal string 'a.py#helper'. Was a headline false-empty bug."""
    files = {
        "a.py": "def helper():\n    return 1\n\n\ndef consumer():\n    return helper()\n",
        "b.py": "from a import helper\n\n\ndef use():\n    return helper() + 1\n",
    }
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    pack = packs.build_pack(cfg, store, "a.py#helper")
    sr = pack.get("symbol_refs") or {}
    assert sr.get("total_defs", 0) >= 1, sr
    assert sr.get("total_refs", 0) >= 1, sr
    # At least one cross-file ref from b.py.
    assert any(r["file"] == "b.py" for r in sr["refs"]), sr["refs"]
    store.close()


def test_imported_name_is_refed_not_skipped(tmp_path):
    """A cross-file call via `from m import f; f()` must register a ref on
    the caller side. Previously the importing file added `f` to its own
    `defined_names` set, which then suppressed the ref emission."""
    files = {
        "m.py": "def target():\n    return 'hi'\n",
        "caller.py": "from m import target\n\n\ndef run():\n    return target()\n",
    }
    root = _mkpkg(tmp_path, files)
    _cfg, store = _index(root)
    refs = [dict(r) for r in store.refs_by_name("target")]
    assert any(r["file"] == "caller.py" for r in refs), refs
    store.close()


# ---------------------------------------------------------------------------
# 4. `--radius` actually changes output
# ---------------------------------------------------------------------------

def test_radius_expansion_changes_reverse_deps(tmp_path):
    """A → B → C chain: pack on C at radius 1 sees B. At radius 2 it must
    ALSO see A, with `hop=2` annotation and a `through` field pointing at B.
    This is the whole point of --radius."""
    files = {
        "c.py": "VALUE = 42\n",
        "b.py": "from c import VALUE\n\n\ndef mid():\n    return VALUE\n",
        "a.py": "from b import mid\n\n\ndef top():\n    return mid()\n",
    }
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    p1 = packs.build_pack(cfg, store, "c.py", radius=1)
    p2 = packs.build_pack(cfg, store, "c.py", radius=2)
    r1_files = {d["file"] for d in p1["reverse_dependencies"]}
    r2_files = {d["file"] for d in p2["reverse_dependencies"]}
    assert "b.py" in r1_files
    assert "a.py" in r2_files and "a.py" not in r1_files
    # Radius-2 transitive rows carry `hop>1` and `through`.
    transit = [d for d in p2["reverse_dependencies"] if d.get("hop", 1) > 1]
    assert any(d["through"] == "b.py" and d["file"] == "a.py"
               for d in transit), transit
    store.close()


# ---------------------------------------------------------------------------
# 5. External/stdlib imports don't poison structural confidence
# ---------------------------------------------------------------------------

def test_stdlib_imports_do_not_lower_structural_confidence(tmp_path):
    """`import os`, `import json`, `from __future__ import ...` must NOT
    be counted as unresolved. These are stdlib — known external by
    construction. Was previously dragging structural_confidence to
    medium/low on every Python file."""
    files = {
        "clean.py": textwrap.dedent("""
            from __future__ import annotations
            import os
            import json
            from collections import defaultdict


            def compute(x):
                return defaultdict(list)
        """),
    }
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    pack = packs.build_pack(cfg, store, "clean.py")
    cov = pack["coverage"]
    # Unresolved (repo-relative missing) must be zero for this file.
    assert cov["unresolved_count"] == 0, cov
    # Stdlib count should be non-zero.
    assert cov["stdlib_import_count"] >= 2, cov
    # The unknowns list must have a `stdlib-imports` entry, not a
    # single collapsed `unresolved-imports` entry.
    kinds = {u.get("kind") for u in pack.get("unknowns") or []}
    assert "stdlib-imports" in kinds, kinds
    store.close()


def test_unresolved_count_reports_items_not_entries(tmp_path):
    """`coverage.unresolved_count` must count the number of actual
    unresolved items, not the number of `unknowns` entries (which is
    always <= 1). Prior behavior: 5 unresolved imports reported as 1."""
    files = {
        "badrel.py": textwrap.dedent("""
            from . import missing_one
            from .. import missing_two
            from .also_missing import thing
        """),
    }
    root = _mkpkg(tmp_path, files)
    cfg, store = _index(root)
    pack = packs.build_pack(cfg, store, "badrel.py")
    cov = pack["coverage"]
    # At least 2 distinct unresolved relative imports.
    assert cov["unresolved_count"] >= 2, cov
    store.close()


# ---------------------------------------------------------------------------
# 6. Foreign-index detection
# ---------------------------------------------------------------------------

def test_foreign_index_warning_surfaces(tmp_path, monkeypatch):
    """When `meta.root` disagrees with the current project root, the CLI
    must surface a `foreign_index_warning` (or refuse, without the env
    override). Simulated by indexing at one root then mutating the meta."""
    root = _mkpkg(tmp_path, {"a.py": "def f():\n    return 1\n"})
    cfg, store = _index(root)
    # Pretend the DB was built at a different machine's path.
    store.set_meta("root", "/somebody/else/Desktop/project")
    store.commit()
    store.close()

    # Without the override: pack must refuse (SystemExit).
    from projmem import cli as _cli
    cfg2 = config_mod.load(root)
    store2 = Store(cfg2.db_path)
    monkeypatch.delenv("PROJMEM_ALLOW_FOREIGN", raising=False)
    with pytest.raises(SystemExit):
        _cli._require_fresh_index(cfg2, store2)
    # With the override: returns a dict, does not raise.
    monkeypatch.setenv("PROJMEM_ALLOW_FOREIGN", "1")
    warn = _cli._require_fresh_index(cfg2, store2)
    assert warn is not None
    assert warn["indexed_root"].endswith("project")
    store2.close()
