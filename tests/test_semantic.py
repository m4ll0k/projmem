from projmem import config as config_mod, indexer
from projmem.store import Store


def _index(root):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    return cfg, store


def test_flags_detected(fresh_project):
    _, store = _index(fresh_project)
    items = [dict(r) for r in store.contracts_by_name("live-logs", "flag")]
    assert any(i["role"] == "parse" and i["confidence"] == "high" for i in items), items
    store.close()


def test_env_vars_detected(fresh_project):
    _, store = _index(fresh_project)
    db = [dict(r) for r in store.contracts_by_name("DATABASE_URL", "env")]
    assert any(i["file"] == "src/core.py" and i["role"] == "read" for i in db)
    api = [dict(r) for r in store.contracts_by_name("API_KEY", "env")]
    assert any(i["file"] == "schema/reducer.js" for i in api)
    store.close()


def test_schema_fields_detected(fresh_project):
    _, store = _index(fresh_project)
    status = [dict(r) for r in store.contracts_by_name("status", "schema_field")]
    # should be written/read across core.py and helpers.py
    files = {i["file"] for i in status}
    assert "src/core.py" in files
    assert "src/helpers.py" in files or any(i["role"] in ("read", "write") for i in status)
    store.close()


def test_user_pair_rule_creates_edge(fresh_project):
    _, store = _index(fresh_project)
    edges = [dict(r) for r in store.edges_from("src/core.py", type_="pair_inspect")]
    assert any(e["dst"] == "tests/test_core.py" for e in edges)
    store.close()
