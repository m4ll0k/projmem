from projmem import config as config_mod, indexer
from projmem.store import Store
from projmem import graph


def _index(root):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    counts = indexer.index_all(cfg, store)
    return cfg, store, counts


def test_indexes_python_and_js(fresh_project):
    cfg, store, counts = _index(fresh_project)
    files = {r["path"] for r in store.all_files()}
    assert "src/core.py" in files
    assert "schema/reducer.js" in files
    assert counts["indexed"] >= 3
    store.close()


def test_reverse_dependencies(fresh_project):
    _, store, _ = _index(fresh_project)
    rev = graph.reverse_deps(store, "src/core.py")
    assert any(r["file"] == "cli/main.py" for r in rev), rev
    assert any(r["type"] == "imports" for r in rev)
    store.close()


def test_symbol_lookup(fresh_project):
    _, store, _ = _index(fresh_project)
    defs = [dict(r) for r in store.symbols_by_name("run")]
    assert any(d["file"] == "src/core.py" and d["kind"] == "function" for d in defs)
    store.close()


def test_staleness(fresh_project):
    import os, time
    cfg, store, _ = _index(fresh_project)
    path = os.path.join(fresh_project, "src/core.py")
    time.sleep(0.01)
    with open(path, "a") as f:
        f.write("\n# extra\n")
    stale = indexer.check_staleness(cfg, store)
    assert "src/core.py" in stale
    store.close()
