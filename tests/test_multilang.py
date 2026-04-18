"""Tree-sitter multi-language indexing tests.

Skipped if tree-sitter is not installed so the core MVP still ships without it.
"""
import os
import shutil

import pytest

from projmem import config as config_mod, indexer, ts_backend
from projmem.store import Store


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "multilang")

pytestmark = pytest.mark.skipif(
    not ts_backend.available(), reason="tree-sitter backend not installed")


@pytest.fixture
def multilang_root(tmp_path):
    dst = tmp_path / "multilang"
    shutil.copytree(FIXTURE, dst)
    p = dst / ".projmem" / "index.db"
    if p.exists(): p.unlink()
    return str(dst)


def _index(root):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    return cfg, store


def test_treesitter_used_for_all_languages(multilang_root):
    _, store = _index(multilang_root)
    files = {r["path"]: r["parser"] for r in store.all_files()}
    # Every non-json fixture file should be indexed via tree-sitter.
    for p, parser in files.items():
        assert parser.startswith("treesitter:"), (p, parser)
    store.close()


def test_go_symbols_and_import(multilang_root):
    _, store = _index(multilang_root)
    syms = {(r["name"], r["kind"]) for r in store.conn.execute(
        "SELECT name,kind FROM symbols WHERE file=?", ("go/server.go",))}
    assert ("NewServer", "function") in syms
    assert ("Server", "type") in syms
    assert ("Start", "method") in syms
    # Local package resolution: ./util → directory fanned out to .go files inside.
    edges = {r["dst"] for r in store.edges_from("go/server.go", type_="imports")}
    assert "go/util/util.go" in edges
    # Env contract: os.Getenv("API_KEY")
    rows = [dict(r) for r in store.contracts_by_name("API_KEY", "env")]
    assert any(r["file"] == "go/server.go" and r["role"] == "read"
               and r["confidence"] == "high" for r in rows)
    store.close()


def test_rust_symbols_and_env(multilang_root):
    _, store = _index(multilang_root)
    syms = {(r["name"], r["kind"]) for r in store.conn.execute(
        "SELECT name,kind FROM symbols WHERE file=?", ("rust/lib.rs",))}
    assert ("Config", "struct") in syms
    assert ("Status", "enum") in syms
    assert ("Runner", "trait") in syms
    assert ("load", "function") in syms
    rows = [dict(r) for r in store.contracts_by_name("APP_NAME", "env")]
    assert any(r["file"] == "rust/lib.rs" and r["confidence"] == "high" for r in rows)
    store.close()


def test_c_include_resolution(multilang_root):
    _, store = _index(multilang_root)
    edges = [dict(r) for r in store.edges_from("c/main.c", type_="imports")]
    dsts = {e["dst"] for e in edges}
    assert "c/util.h" in dsts           # "util.h" resolves relative
    assert any(d.startswith("module:stdio") for d in dsts)  # <stdio.h> stays external
    syms = {r["name"] for r in store.conn.execute(
        "SELECT name FROM symbols WHERE file=?", ("c/main.c",))}
    assert "compute" in syms and "main" in syms
    store.close()


def test_typescript_interfaces_and_reverse_dep(multilang_root):
    _, store = _index(multilang_root)
    rows = list(store.conn.execute(
        "SELECT name,kind FROM symbols WHERE file=?", ("ts/app.ts",)))
    kinds = {r["kind"] for r in rows}
    assert "interface" in kinds and "type" in kinds and "class" in kinds
    # ts/app.ts imports ./util → resolved to ts/util.ts
    rev = [dict(r) for r in store.edges_to("ts/util.ts", type_="imports")]
    assert any(r["src"] == "ts/app.ts" for r in rev)
    # process.env contract
    rows = [dict(r) for r in store.contracts_by_name("API_KEY", "env")]
    assert any(r["file"] == "ts/app.ts" for r in rows)
    store.close()


def test_java_symbols(multilang_root):
    _, store = _index(multilang_root)
    syms = {(r["name"], r["kind"]) for r in store.conn.execute(
        "SELECT name,kind FROM symbols WHERE file=?", ("java/App.java",))}
    assert ("App", "class") in syms
    assert ("main", "method") in syms
    store.close()


def test_pack_on_go_file_produces_reverse_deps(multilang_root):
    from projmem import packs
    cfg, store = _index(multilang_root)
    pack = packs.build_pack(cfg, store, "go/util/util.go")
    rev_files = {r["file"] for r in pack["reverse_dependencies"]}
    assert "go/server.go" in rev_files
    assert pack["coverage"]["parser_used"] == "treesitter:go"
    store.close()
