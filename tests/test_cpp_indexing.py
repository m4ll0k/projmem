"""Regression tests for the 3 P1/P2/P3 C/C++ indexing fixes.

Each test pins a specific defect surfaced by the nodejs/node v22.11.0
real-repo stress test (docs/REAL_STRESS_FINAL.md §5) and the matching
fix in projmem/ts_backend.py + projmem/semantic.py.

If any of these fail, the corresponding real-world scenario will
silently regress — these tests are the ratchet.
"""
from __future__ import annotations

import pytest
from pathlib import Path

# Tree-sitter is required for these tests; skip cleanly when missing.
try:
    from projmem import ts_backend
    if not ts_backend.available():
        raise ImportError
    if ts_backend._compile("cpp") is None or ts_backend._compile("c") is None:
        raise ImportError
except Exception:
    pytest.skip("tree-sitter c/cpp not available", allow_module_level=True)

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store


def _setup(tmp_path: Path):
    cfg = Config(root=str(tmp_path))
    Path(cfg.store_dir).mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    return cfg, store


# =========================================================================
# P1 — qualified C++ method names should be resolvable by their bare name.
# =========================================================================


def test_cpp_qualified_method_alias_is_searchable_by_bare_name(tmp_path):
    """`void Permission::EnablePermissions()` in a .cc file MUST become
    queryable as both `Permission::EnablePermissions` (qualified) AND
    `EnablePermissions` (bare). This was the projmem real-repo defect
    on nodejs/node v22.11.0 — bare-name lookups returned 0 defs."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "permission.cc").write_text(
        "#include \"permission.h\"\n"
        "namespace node { namespace permission {\n"
        "void Permission::EnablePermissions() {\n"
        "  enabled_ = true;\n"
        "}\n"
        "}}\n"
    )
    index_all(cfg, store)

    bare = store.symbols_by_name("EnablePermissions")
    qualified = store.symbols_by_name("Permission::EnablePermissions")

    assert bare, ("bare-name lookup MUST find the qualified def — "
                  "see ts_backend.py qualified_alias")
    assert qualified, "qualified name lookup must still work"
    # Same file, same line — the alias is additive, not a move.
    assert bare[0]["file"] == "permission.cc"
    assert qualified[0]["file"] == "permission.cc"


def test_cpp_namespace_qualified_method_alias(tmp_path):
    """Three-level `ns::Class::method` should also alias to bare."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cc").write_text(
        "namespace node { namespace fs {\n"
        "void Reader::doRead() { return; }\n"
        "}}\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("doRead"), \
        "bare 'doRead' must resolve to Reader::doRead"


# =========================================================================
# P2 — C++ class/struct fields should be indexed as symbols.
# =========================================================================


def test_cpp_struct_field_is_indexed(tmp_path):
    """The defect: `bool experimental_permission = false;` inside a
    struct was never emitted as a symbol, so flag-state propagation
    couldn't be traced. After fix: field is searchable."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "options.h").write_text(
        "struct EnvironmentOptions {\n"
        "  bool experimental_permission = false;\n"
        "  std::vector<std::string> allow_fs_read;\n"
        "  int max_workers = 0;\n"
        "};\n"
    )
    index_all(cfg, store)

    fields = store.symbols_by_name("experimental_permission")
    assert fields, "struct field MUST be indexed (P2 fix)"
    assert fields[0]["file"] == "options.h"
    assert fields[0]["kind"] == "field"

    # The other two field types should also work.
    assert store.symbols_by_name("allow_fs_read"), "vector field"
    assert store.symbols_by_name("max_workers"),   "int field"


def test_c_struct_field_is_indexed(tmp_path):
    """Same fix applied to plain C — struct members must be visible."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "config.c").write_text(
        "struct Config {\n"
        "  int max_connections;\n"
        "  char* listen_addr;\n"
        "};\n"
    )
    index_all(cfg, store)

    assert store.symbols_by_name("max_connections"), "int field"
    assert store.symbols_by_name("listen_addr"),     "pointer field"


# =========================================================================
# P3 — C/C++ env-string contracts must surface NODE_OPTIONS-style names.
# =========================================================================


def test_cpp_safe_getenv_emits_env_contract(tmp_path):
    """`SafeGetenv("NODE_OPTIONS", ...)` is the form Node uses; bare
    `getenv` regex missed it. After fix: contract appears."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "node.cc").write_text(
        '#include "node.h"\n'
        'void apply_env_options() {\n'
        '  std::string opts;\n'
        '  if (credentials::SafeGetenv("NODE_OPTIONS", &opts)) {}\n'
        '  if (SafeGetenv("NODE_DEBUG", &opts)) {}\n'
        '}\n'
    )
    index_all(cfg, store)

    rows = list(store.contracts_by_name("NODE_OPTIONS", kind="env"))
    assert rows, "NODE_OPTIONS env contract must be detected via SafeGetenv"
    assert rows[0]["file"] == "node.cc"
    assert rows[0]["context"] in ("SafeGetenv", "getenv")  # both acceptable

    assert list(store.contracts_by_name("NODE_DEBUG", kind="env")), \
        "second env name on the same file should also resolve"


def test_cpp_env_vars_get_emits_env_contract(tmp_path):
    """`env_vars->Get("X")` and `env_vars().Get("X")` are real Node
    patterns; pre-fix neither was detected."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "worker.cc").write_text(
        'void Worker::Init() {\n'
        '  auto x = env_vars->Get("WORKER_OPT_A");\n'
        '  auto y = env->env_vars()->Get("WORKER_OPT_B");\n'
        '}\n'
    )
    index_all(cfg, store)

    assert list(store.contracts_by_name("WORKER_OPT_A", kind="env")), \
        "env_vars->Get pattern must produce a contract"
    assert list(store.contracts_by_name("WORKER_OPT_B", kind="env")), \
        "env_vars().Get pattern must also produce a contract"


def test_no_false_positive_env_contracts_in_js(tmp_path):
    """The new C/C++ patterns must NOT fire on JS code, even when JS
    happens to contain a string like .Get("FOO"). Confines the
    extra patterns to lang in ('c','cpp')."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.js").write_text(
        "const x = obj.Get('NODE_OPTIONS');\n"
        "const y = service->Get('NODE_DEBUG');\n"  # not legal JS but text-wise matches
    )
    index_all(cfg, store)

    rows = list(store.contracts_by_name("NODE_OPTIONS", kind="env"))
    # Only acceptable rows are JS-context (process.env.*) — there are
    # none in this fixture, so any rows here would be false positives.
    assert not rows, ("NODE_OPTIONS must not be detected as an env "
                      "contract from a .js file via the C/C++ wrappers")


# =========================================================================
# P4 — JS ↔ C++ binding edges (SetMethod / NODE_SET_METHOD) should surface.
# =========================================================================


def test_cpp_setmethod_binding_edge_is_extracted(tmp_path):
    """Node/V8-style `SetMethod(..., "jsName", CppFn)` should produce a
    concrete binding edge row that maps the JS-facing name to the C++
    function symbol, with strong evidence and no speculation."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "binding.cc").write_text(
        "void InternalModuleStat() {}\n"
        "void Init() {\n"
        "  SetMethod(ctx, target, \"internalModuleStat\", InternalModuleStat);\n"
        "}\n"
    )
    index_all(cfg, store)

    rows = list(store.bindings_for_js_name("internalModuleStat"))
    assert rows, "binding edge should be present for js_name"
    assert rows[0]["file"] == "binding.cc"
    assert rows[0]["cpp_name"] == "InternalModuleStat"
    assert rows[0]["cpp_symbol_id"], "should resolve to same-file symbol_id"
