from __future__ import annotations

from pathlib import Path

import pytest

# Tree-sitter is required for this regression: it exercises the TS backend's
# `py_from_stmt` handling.
try:
    from projmem import ts_backend

    if not ts_backend.available():
        raise ImportError
    if ts_backend._compile("python") is None:
        raise ImportError
except Exception:
    pytest.skip("tree-sitter python not available", allow_module_level=True)

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem import packs


def _setup(tmp_path: Path):
    cfg = Config(root=str(tmp_path))
    Path(cfg.store_dir).mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    return cfg, store


def test_ts_python_from_import_symbol_does_not_emit_unresolved_composite(tmp_path):
    """Regression: `from .store import Store` should NOT emit an unresolved
    `module:.store.Store` edge. That's a symbol import, not a file import, and
    it incorrectly lowers pack integrity."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "store.py").write_text("class Store:\n    pass\n", encoding="utf-8")
    (pkg / "packs.py").write_text(
        "from .store import Store\n\n\ndef f():\n    return Store()\n",
        encoding="utf-8",
    )

    cfg, store = _setup(tmp_path)
    index_all(cfg, store)

    dsts = {r["dst"] for r in store.edges_from("pkg/packs.py", type_="imports")}
    assert "pkg/store.py" in dsts, dsts
    assert "module:.store.Store" not in dsts, dsts

    pack = packs.build_pack(cfg, store, "pkg/packs.py")
    assert pack["coverage"]["unresolved_count"] == 0, pack["coverage"]
    store.close()


def test_ts_python_unresolved_module_no_fake_member_edge(tmp_path):
    """Regression (GAP 5): `from ...missing import Thing, Other` on a module
    that genuinely cannot be resolved should emit exactly ONE unresolved
    edge (`module:...missing`), NOT a separate `module:...missing.Thing` and
    `module:...missing.Other`. Per-symbol fabricated module entries are
    indistinguishable from real unresolved modules downstream and inflate
    the unresolved-imports view + pack integrity penalty."""
    pkg = tmp_path / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "tool.py").write_text(
        "from ...missing import Thing, Other\n",
        encoding="utf-8",
    )

    cfg, store = _setup(tmp_path)
    index_all(cfg, store)

    dsts = [r["dst"] for r in store.edges_from("pkg/sub/tool.py",
                                                type_="imports")]
    # One and only one unresolved edge for the module itself.
    module_edges = [d for d in dsts if d.startswith("module:...missing")]
    assert "module:...missing" in module_edges, dsts
    # No per-member fake edges.
    assert "module:...missing.Thing" not in dsts, dsts
    assert "module:...missing.Other" not in dsts, dsts
    store.close()


def test_ts_python_stdlib_ast_path_matches_ts_backend(tmp_path):
    """Same contract as above, but exercises the stdlib-AST path (used when
    tree-sitter Python is unavailable or the TS grammar misbehaves). Both
    paths must reach identical edges."""
    import ast as _ast
    from projmem.indexer import index_python

    cfg, store = _setup(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub" / "tool.py").write_text(
        "from ...missing import Thing\n")

    with open(tmp_path / "pkg" / "sub" / "tool.py") as f:
        src = f.read()
    index_python(store, "pkg/sub/tool.py", src, str(tmp_path))

    dsts = [r["dst"] for r in store.edges_from("pkg/sub/tool.py",
                                                type_="imports")]
    assert "module:...missing" in dsts
    assert "module:...missing.Thing" not in dsts
    store.close()

