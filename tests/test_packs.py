from projmem import config as config_mod, indexer, packs
from projmem.store import Store


def _index(root):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    return cfg, store


def test_pack_for_file_has_structure(fresh_project):
    cfg, store = _index(fresh_project)
    pack = packs.build_pack(cfg, store, "src/core.py")
    assert pack["target"]["kind"] == "file"
    # reverse deps should include cli/main.py
    assert any(r["file"] == "cli/main.py" for r in pack["reverse_dependencies"])
    # direct deps should include src/helpers.py
    assert any(r["file"] == "src/helpers.py" for r in pack["direct_dependencies"])
    # semantic contracts include env DATABASE_URL or schema status
    kinds = set(pack["semantic_contracts"]["in_target"].keys())
    assert "env" in kinds or "schema_field" in kinds
    # coverage summary present
    assert "overall_confidence" in pack["coverage"]
    # reasons mapping non-empty
    assert pack["reasons"]
    store.close()


def test_pack_for_symbol(fresh_project):
    cfg, store = _index(fresh_project)
    pack = packs.build_pack(cfg, store, "run")
    assert pack["target"]["kind"] == "symbol"
    assert pack["target"]["defs"]
    # tests list may include test_core.py
    tests = {t["file"] for t in pack.get("tests", [])}
    assert "tests/test_core.py" in tests
    store.close()


def test_pack_reports_unknowns_when_missing(fresh_project):
    cfg, store = _index(fresh_project)
    pack = packs.build_pack(cfg, store, "DoesNotExistSymbolXYZ")
    # Must fail loud: unknowns should note the missing definition
    assert any(u["kind"] == "symbol-undefined" for u in pack["unknowns"])
    store.close()


def test_pack_markdown_render(fresh_project):
    cfg, store = _index(fresh_project)
    pack = packs.build_pack(cfg, store, "src/core.py")
    md = packs.render_markdown(pack)
    assert "Context Pack" in md
    assert "Reverse Dependencies" in md
    assert "confidence" in md.lower()
    store.close()


def test_pack_is_bounded(fresh_project):
    cfg, store = _index(fresh_project)
    pack = packs.build_pack(cfg, store, "src/core.py")
    # sanity: no bucket runs away
    assert len(pack["direct_dependencies"]) <= 25
    assert len(pack["reverse_dependencies"]) <= 25
    assert len(pack["semantic_contracts"]["related_files"]) <= 50
    store.close()
