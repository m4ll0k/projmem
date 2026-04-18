"""Regression tests for the P0 stabilization phase.

Each test exercises one of the four fixes identified from the real-repo
benchmark on nodejs/node and microsoft/TypeScript:

  Fix 1: `projmem trace` must only follow refs.kind='call' by default
  Fix 2: C++ namespaced / qualified calls resolve to the unqualified symbol
  Fix 3: TypeScript `.js` import specifiers resolve to `.ts`/`.tsx`/`.d.ts`
         sources; `export * from ...` barrels propagate reverse deps
  Fix 4: Silent file-skip on max_file_bytes becomes a HIGH warning

The tests are self-contained — they build a tiny fixture on disk, index
it, and assert the new behavior. Each was manually verified to FAIL on
the pre-fix code path and PASS after the patch.
"""
from __future__ import annotations
import os
import tempfile
import pytest

from projmem.config import Config
from projmem.store import Store
from projmem.ts_backend import index as ts_index, available as ts_available


needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


# --- Fix 1: trace call-only -------------------------------------------------

@needs_ts
def test_trace_strict_mode_does_not_cross_import_edges(tmp_path):
    """With only `import` and `read` edges between two files, strict-mode
    trace must return NO path — the sink is structurally reachable via
    import graph but not via any call chain.
    """
    from projmem.graph import trace_call_chain

    root = str(tmp_path)
    # File A defines `launch`. File B imports A and defines `logger`.
    # There is NO real caller of `launch` in B. A naive BFS over all refs
    # would reach `launch` from `logger` via the import edge, producing
    # a false path.
    os.makedirs(os.path.join(root, "pkg"))
    open(os.path.join(root, "pkg/a.js"), "w").write(
        "export function launch() {}\n"
    )
    open(os.path.join(root, "pkg/b.js"), "w").write(
        "import { launch } from './a.js';\n"
        "export function logger() { return 1; }\n"
    )

    store = Store(os.path.join(root, "idx.db"))
    for p in ("pkg/a.js", "pkg/b.js"):
        with open(os.path.join(root, p)) as f:
            src = f.read()
        ts_index(store, p, src, "javascript", root)

    res = trace_call_chain(store, "logger", "launch", max_hops=4,
                           mode="strict")
    assert res["path"] == [], (
        "Strict-mode trace must not cross import/read edges. Got path: "
        f"{res.get('path')}"
    )
    assert "call" in (res.get("allowed_edges") or [])


@needs_ts
def test_trace_follows_real_call_edge(tmp_path):
    """Sanity: a real call chain through `call` refs must still be found."""
    from projmem.graph import trace_call_chain

    root = str(tmp_path)
    open(os.path.join(root, "a.js"), "w").write(
        "export function leaf() { return 42; }\n"
        "export function middle() { return leaf(); }\n"
        "export function top() { return middle(); }\n"
    )
    store = Store(os.path.join(root, "idx.db"))
    with open(os.path.join(root, "a.js")) as f:
        src = f.read()
    ts_index(store, "a.js", src, "javascript", root)

    # caller→callee direction: top → middle → leaf.
    res = trace_call_chain(store, "top", "leaf", max_hops=3, mode="strict")
    path = res.get("path") or []
    assert len(path) >= 2, f"Expected a multi-hop call path, got {res}"
    # Hops beyond the source should be tagged edge_type='call'.
    for hop in path[1:]:
        assert hop.get("edge_type") == "call", (
            f"Strict-mode hops must be call edges, got {hop}")


@needs_ts
def test_trace_emits_edge_types(tmp_path):
    """Every reconstructed path must report the ref kind used at each hop."""
    from projmem.graph import trace_call_chain

    root = str(tmp_path)
    open(os.path.join(root, "a.js"), "w").write(
        "export function sink() {}\n"
        "export function middle() { sink(); }\n"
        "export function source() { middle(); }\n"
    )
    store = Store(os.path.join(root, "idx.db"))
    with open(os.path.join(root, "a.js")) as f:
        src = f.read()
    ts_index(store, "a.js", src, "javascript", root)

    res = trace_call_chain(store, "source", "sink", max_hops=3, mode="strict")
    for hop in res.get("path") or []:
        assert "edge_type" in hop


# --- Fix 2: C++ namespaced call resolution ----------------------------------

@needs_ts
def test_cpp_namespaced_call_attributes_to_function(tmp_path):
    """`ns::func(...)` must record a `call` ref under the bare name `func`.
    Previously only `func(...)` (unqualified) was captured, so
    `credentials::SafeGetenv(...)` callsites across Node.js's src/ tree
    (14+ sites) were invisible.
    """
    root = str(tmp_path)
    src = (
        "namespace ns {\n"
        "bool SafeGetenv(const char* k) { return false; }\n"
        "}\n"
        "void caller_a() { ns::SafeGetenv(\"A\"); }\n"
        "void caller_b() { ns::SafeGetenv(\"B\"); }\n"
        "void caller_c() { ns::SafeGetenv(\"C\"); }\n"
    )
    p = os.path.join(root, "f.cc")
    open(p, "w").write(src)
    store = Store(os.path.join(root, "idx.db"))
    ok = ts_index(store, "f.cc", src, "cpp", root)
    assert ok

    call_rows = list(store.conn.execute(
        "SELECT line FROM refs WHERE name='SafeGetenv' AND kind='call' "
        "ORDER BY line"))
    assert len(call_rows) == 3, (
        f"Expected 3 call refs for ns::SafeGetenv, got {len(call_rows)}: "
        f"{[dict(r) for r in call_rows]}")


@needs_ts
def test_cpp_nested_namespaced_call(tmp_path):
    """Deeply nested `a::b::c::func(...)` should still attribute to `func`."""
    root = str(tmp_path)
    src = (
        "namespace a { namespace b { namespace c {\n"
        "void target() {}\n"
        "}}}\n"
        "void caller() { a::b::c::target(); }\n"
    )
    p = os.path.join(root, "f.cc")
    open(p, "w").write(src)
    store = Store(os.path.join(root, "idx.db"))
    assert ts_index(store, "f.cc", src, "cpp", root)
    rows = list(store.conn.execute(
        "SELECT line FROM refs WHERE name='target' AND kind='call'"))
    assert len(rows) == 1, f"Expected 1 ref to target, got {rows}"


@needs_ts
def test_cpp_class_static_method_call(tmp_path):
    """`Class::static_method(...)` should attribute to `static_method`."""
    root = str(tmp_path)
    src = (
        "class Foo {\n"
        " public:\n"
        "  static void Bar() {}\n"
        "};\n"
        "void caller() {\n"
        "  Foo::Bar();\n"
        "  Foo::Bar();\n"
        "}\n"
    )
    p = os.path.join(root, "f.cc")
    open(p, "w").write(src)
    store = Store(os.path.join(root, "idx.db"))
    assert ts_index(store, "f.cc", src, "cpp", root)
    rows = list(store.conn.execute(
        "SELECT line FROM refs WHERE name='Bar' AND kind='call'"))
    assert len(rows) == 2, f"Expected 2 refs to Bar, got {rows}"


# --- Fix 3: TypeScript module resolution ------------------------------------

@needs_ts
def test_js_specifier_resolves_to_ts_source(tmp_path):
    """`import "./foo.js"` must resolve to `./foo.ts` when the `.ts`
    source exists on disk. Without this, TypeScript NodeNext/ESM
    projects lose all reverse deps."""
    from projmem.graph import reverse_deps
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "src"))
    open(os.path.join(root, "src/foo.ts"), "w").write(
        "export function bar() { return 1; }\n")
    open(os.path.join(root, "src/caller.ts"), "w").write(
        "import { bar } from './foo.js';\n"
        "export function run() { return bar(); }\n")
    store = Store(os.path.join(root, "idx.db"))
    for p in ("src/foo.ts", "src/caller.ts"):
        with open(os.path.join(root, p)) as f:
            src = f.read()
        ts_index(store, p, src, "typescript", root)

    rd = reverse_deps(store, "src/foo.ts")
    assert any(r["file"] == "src/caller.ts" for r in rd), (
        f"src/caller.ts should reverse-depend on src/foo.ts; got {rd}")


@needs_ts
def test_barrel_export_star_propagates_reverse_deps(tmp_path):
    """`export * from "./leaf"` in a barrel file must make that barrel's
    consumers appear in the LEAF's reverse deps (tagged `reexport_via`).
    Without this, TypeScript's scanner.ts (only consumed via the
    namespace barrel) showed 0 reverse deps."""
    from projmem.graph import reverse_deps
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "src"))
    os.makedirs(os.path.join(root, "src/_ns"))
    open(os.path.join(root, "src/scanner.ts"), "w").write(
        "export function createScanner() {}\n")
    open(os.path.join(root, "src/_ns/ts.ts"), "w").write(
        "export * from '../scanner.js';\n")
    open(os.path.join(root, "src/checker.ts"), "w").write(
        "import { createScanner } from './_ns/ts.js';\n"
        "export function check() { return createScanner(); }\n")
    store = Store(os.path.join(root, "idx.db"))
    for p in ("src/scanner.ts", "src/_ns/ts.ts", "src/checker.ts"):
        with open(os.path.join(root, p)) as f:
            src = f.read()
        ts_index(store, p, src, "typescript", root)

    rd = reverse_deps(store, "src/scanner.ts")
    via = [r for r in rd if r["type"] == "reexport_via"]
    assert any(r["file"] == "src/checker.ts" for r in via), (
        f"checker.ts should reach scanner.ts via the _ns barrel; got {rd}")


@needs_ts
def test_barrel_chain_two_levels(tmp_path):
    """Two-level barrel: A re-exports B re-exports C. Consumers of A
    must still appear in C's reverse deps."""
    from projmem.graph import reverse_deps
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "src"))
    open(os.path.join(root, "src/leaf.ts"), "w").write(
        "export const x = 1;\n")
    open(os.path.join(root, "src/barrel1.ts"), "w").write(
        "export * from './leaf.js';\n")
    open(os.path.join(root, "src/barrel2.ts"), "w").write(
        "export * from './barrel1.js';\n")
    open(os.path.join(root, "src/consumer.ts"), "w").write(
        "import { x } from './barrel2.js';\n"
        "export const y = x + 1;\n")
    store = Store(os.path.join(root, "idx.db"))
    for p in ("src/leaf.ts", "src/barrel1.ts", "src/barrel2.ts",
              "src/consumer.ts"):
        with open(os.path.join(root, p)) as f:
            src = f.read()
        ts_index(store, p, src, "typescript", root)

    rd = reverse_deps(store, "src/leaf.ts")
    files = {r["file"] for r in rd}
    assert "src/consumer.ts" in files, (
        f"consumer should reach leaf via 2-level barrel; got {files}")


# --- Fix 4: oversize-file warning -------------------------------------------

def test_oversize_file_is_recorded_not_silently_skipped(tmp_path):
    """A file larger than `max_file_bytes` must appear in the indexer
    stats as an oversize skip, so the caller knows there's a hole."""
    from projmem.indexer import index_all
    # Put the DB outside the walk root so its own size doesn't pollute
    # the oversize list.
    root = str(tmp_path / "src")
    os.makedirs(root)
    db_path = str(tmp_path / "store.db")
    big = "x" * 5000
    open(os.path.join(root, "big.js"), "w").write(
        f"// big file\nexport const x = '{big}';\n")
    open(os.path.join(root, "small.js"), "w").write(
        "export const y = 1;\n")

    cfg = Config(root=root, max_file_bytes=1000)
    store = Store(db_path)
    counts = index_all(cfg, store)
    oversize = counts.get("oversize_skipped") or []
    assert any(o["path"].endswith("big.js") for o in oversize), (
        f"Expected big.js in oversize list, got {oversize}")
    assert counts.get("indexed", 0) >= 1


def test_oversize_list_resets_between_runs(tmp_path):
    """Repeat indexing must not double-report oversize skips."""
    from projmem.indexer import index_all
    root = str(tmp_path / "src")
    os.makedirs(root)
    db_path = str(tmp_path / "store.db")
    open(os.path.join(root, "big.js"), "w").write(
        "// " + ("x" * 5000) + "\n")
    cfg = Config(root=root, max_file_bytes=1000)
    store = Store(db_path)
    counts_a = index_all(cfg, store, force=True)
    counts_b = index_all(cfg, store, force=True)
    assert len(counts_a["oversize_skipped"]) == 1
    assert len(counts_b["oversize_skipped"]) == 1
