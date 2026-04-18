from __future__ import annotations

from pathlib import Path

import pytest

# Tree-sitter is required for TS import + re-export captures.
try:
    from projmem import ts_backend
    if not ts_backend.available():
        raise ImportError
    if ts_backend._compile("typescript") is None:
        raise ImportError
except Exception:
    pytest.skip("tree-sitter typescript not available", allow_module_level=True)

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem import graph


def _setup(tmp_path: Path):
    cfg = Config(root=str(tmp_path))
    Path(cfg.store_dir).mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    return cfg, store


def test_ts_js_specifier_resolves_to_ts_for_reverse_deps(tmp_path):
    """Regression: TS NodeNext-style `.js` specifiers must resolve to source `.ts`.

    Mirrors the TypeScript repo pattern:
      _namespaces/ts.ts → export * from "../watchUtilities.js"
      checker.ts        → import "./_namespaces/ts.js"
    Reverse deps of watchUtilities.ts must include checker.ts via the barrel.
    """
    cfg, store = _setup(tmp_path)
    ns = tmp_path / "src" / "compiler" / "_namespaces"
    ns.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "compiler" / "watchUtilities.ts").write_text(
        "export const x = 1;\n"
    )
    (ns / "ts.ts").write_text(
        "export * from \"../watchUtilities.js\";\n"
    )
    (tmp_path / "src" / "compiler" / "checker.ts").write_text(
        "import \"./_namespaces/ts.js\";\n"
    )
    index_all(cfg, store)

    rev = graph.reverse_deps(store, "src/compiler/watchUtilities.ts")
    assert any(
        r["file"] == "src/compiler/checker.ts" and r["type"] == "reexport_via"
        for r in rev
    ), rev

