"""Parser-hardening microbenchmarks.

These are the layer-1 ratchet for tree-sitter / regex parser edge
cases discovered in real-repo benchmarks. Every test pins ONE
specific shape so a future query change can't silently break it.

Distinct from tests/test_cpp_indexing.py (P1/P2/P3 fixes) — this
file covers the BROADER set of parser shapes including TS generics,
JS export forms, and the field-read limitation we explicitly chose
to defer (test marked xfail with a clear reason).
"""
from __future__ import annotations

import pytest
from pathlib import Path

try:
    from projmem import ts_backend
    if not ts_backend.available():
        raise ImportError
except Exception:
    pytest.skip("tree-sitter not available", allow_module_level=True)

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store


def _setup(tmp_path: Path):
    cfg = Config(root=str(tmp_path))
    Path(cfg.store_dir).mkdir(parents=True, exist_ok=True)
    return cfg, Store(cfg.db_path)


# =========================================================================
# C/C++ shapes — beyond the original P1/P2/P3 fixes
# =========================================================================


def test_cpp_namespace_three_levels(tmp_path):
    """`ns::sub::Class::method` should still produce a bare alias."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        "namespace a { namespace b {\n"
        "void Outer::Inner::deepMethod() { return; }\n"
        "}}\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("deepMethod"), \
        "3-level qualifier should alias to bare name"


def test_cpp_template_method_definition(tmp_path):
    """Templated out-of-line method definitions."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        "template <typename T>\n"
        "void Container::push(T value) { data_.push_back(value); }\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("push"), \
        "templated qualified method must be indexed"


def test_cpp_pointer_field(tmp_path):
    """Pointer fields like `char* listen_addr;`."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        "struct S { char* listen_addr; int* counters; };\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("listen_addr"), "char* field"
    assert store.symbols_by_name("counters"),    "int* field"


def test_cpp_static_member_function(tmp_path):
    """Static member functions defined inside the class body."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        "class Util {\n"
        " public:\n"
        "  static int helper(int x) { return x + 1; }\n"
        "};\n"
    )
    index_all(cfg, store)
    rows = store.symbols_by_name("helper")
    assert rows, "static member function inside class body"


@pytest.mark.xfail(
    reason="DEFERRED: tree-sitter cpp doesn't separate field-read from "
           "field-write in capture space — adding a generic field_expression "
           "ref capture would produce false positives. See "
           "docs/REAL_STRESS_REPLAY.md §'Honest remaining limits'.")
def test_cpp_field_read_emits_ref(tmp_path):
    """`options_->experimental_permission` (a non-call read) should
    eventually emit a ref. Currently 0 — captured here so future
    progress flips the test green automatically."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        "struct Opts { bool enabled = false; };\n"
        "void check(Opts* opts_) {\n"
        "  if (opts_->enabled) {}\n"
        "}\n"
    )
    index_all(cfg, store)
    name_rows = store.symbols_by_name("enabled")
    assert name_rows, "field def must exist (precondition)"
    rs = list(store.refs_by_name("enabled"))
    assert rs, "field-read at line 3 should be captured as a ref"


# =========================================================================
# C/C++ env-string detector
# =========================================================================


def test_env_safegetenv_namespace_qualified(tmp_path):
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        'void f() { credentials::SafeGetenv("MY_VAR", &out); }\n'
    )
    index_all(cfg, store)
    rows = list(store.contracts_by_name("MY_VAR", kind="env"))
    assert rows, "namespace-qualified SafeGetenv must be detected"


def test_env_win32_get_environment_variable(tmp_path):
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        'void f() { GetEnvironmentVariableW(L"PATH_EXTRA", buf, len); }\n'
    )
    index_all(cfg, store)
    assert list(store.contracts_by_name("PATH_EXTRA", kind="env"))


def test_env_lowercase_string_is_ignored(tmp_path):
    """The detector requires `[A-Z_][A-Z0-9_]+` — a lowercase string
    inside an env-API call MUST NOT produce a contract (precision)."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        'void f() { getenv("home"); }\n'  # lowercase — not env-shaped
    )
    index_all(cfg, store)
    assert not list(store.contracts_by_name("home", kind="env"))


# =========================================================================
# .h content-aware routing
# =========================================================================


def test_h_with_class_routes_to_cpp(tmp_path):
    """The fix: a `.h` containing `class` must parse as C++."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "obj.h").write_text(
        "class Object {\n"
        " public:\n"
        "  void method();\n"
        "};\n"
    )
    index_all(cfg, store)
    file_row = store.conn.execute(
        "SELECT parser FROM files WHERE path='obj.h'").fetchone()
    assert file_row["parser"] == "treesitter:cpp"


def test_h_pure_c_routes_to_c(tmp_path):
    """No C++-only tokens → keep as C."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.h").write_text(
        "#ifndef X_H\n#define X_H\n"
        "void some_function(int x);\n"
        "#endif\n"
    )
    index_all(cfg, store)
    file_row = store.conn.execute(
        "SELECT parser FROM files WHERE path='x.h'").fetchone()
    assert file_row["parser"] == "treesitter:c"


# =========================================================================
# TypeScript shapes
# =========================================================================


def test_ts_generic_function(tmp_path):
    """Generic function definitions must be indexed by base name."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.ts").write_text(
        "export function makePair<K, V>(k: K, v: V): [K, V] {\n"
        "  return [k, v];\n"
        "}\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("makePair"), \
        "generic function defs must be indexed"


def test_ts_class_with_generic_method(tmp_path):
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.ts").write_text(
        "export class Box<T> {\n"
        "  unwrap(): T { return null as any; }\n"
        "}\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("Box"),    "generic class def"
    assert store.symbols_by_name("unwrap"), "method on generic class"


def test_ts_arrow_function_const(tmp_path):
    """Arrow functions assigned to const should still be a callable
    symbol via the lexical_declaration path."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.ts").write_text(
        "export const handler = (req: any) => req.body;\n"
    )
    index_all(cfg, store)
    rows = store.symbols_by_name("handler")
    assert rows, "arrow-const handler must be a symbol"


# =========================================================================
# JS shapes
# =========================================================================


def test_js_named_export_function(tmp_path):
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.js").write_text(
        "export function compute(x) { return x + 1; }\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("compute")


def test_js_module_exports_function(tmp_path):
    """CommonJS — `module.exports.X = function() {}` — symbol indexed
    by RHS function name when the assignment uses a named function."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.js").write_text(
        "function helper() { return 1; }\n"
        "module.exports = { helper };\n"
    )
    index_all(cfg, store)
    assert store.symbols_by_name("helper"), \
        "function declared above module.exports must be indexed"


# =========================================================================
# Aliasing precision — the new bare-name aliases must not pollute
# =========================================================================


def test_qualified_alias_does_not_clobber_native(tmp_path):
    """A bare-name native function and a `Class::name` qualified alias
    should coexist without one shadowing the other."""
    cfg, store = _setup(tmp_path)
    (tmp_path / "x.cpp").write_text(
        "void apply() { return; }\n"                # native bare
        "void Worker::apply() { return; }\n"        # qualified
    )
    index_all(cfg, store)
    rows = store.symbols_by_name("apply")
    # Expect 2 rows: one native (high confidence), one alias (medium).
    assert len(rows) == 2, f"expected 2, got {len(rows)}"
    confs = sorted(r["confidence"] for r in rows)
    assert confs == ["high", "medium"], \
        "native row should be 'high', alias row should be 'medium'"
