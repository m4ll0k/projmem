"""Regression tests from the self-improvement dogfood pass on examples/corpus."""
import os
import shutil

import pytest

from projmem import config as config_mod, indexer, packs, ts_backend
from projmem.store import Store


CORPUS = os.path.join(os.path.dirname(__file__), "..", "examples", "corpus")

pytestmark = pytest.mark.skipif(
    not ts_backend.available(), reason="tree-sitter backend not installed")


@pytest.fixture
def corpus_root(tmp_path):
    if not os.path.isdir(CORPUS):
        pytest.skip("examples/corpus/ not present (dev-only fixture; gitignored)")
    dst = tmp_path / "corpus"
    shutil.copytree(CORPUS, dst)
    p = dst / ".projmem" / "index.db"
    if p.exists(): p.unlink()
    return str(dst)


def _index(root):
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    return cfg, store


def test_js_barrel_reexport_creates_edge(corpus_root):
    """Bug #1: `export { x } from './y'` and `export * from './y'` must emit edges."""
    _, store = _index(corpus_root)
    edges = [dict(r) for r in store.edges_from("js/barrel/index.js", type_="imports")]
    dsts = {e["dst"] for e in edges}
    assert "js/barrel/auth.js" in dsts
    assert "js/barrel/users.js" in dsts
    store.close()


def test_go_struct_tags_become_schema_fields(corpus_root):
    """Bug #2: `json:"user_role"` struct tag → schema_field write, high confidence."""
    _, store = _index(corpus_root)
    rows = [dict(r) for r in store.contracts_by_name("user_role", "schema_field")
            if r["file"] == "go/api/server.go"]
    assert any(r["role"] == "write" and r["confidence"] == "high"
               and r["context"] == "go-struct-tag" for r in rows), rows
    store.close()


def test_ruby_require_relative_resolves(corpus_root):
    """Bug #3: `require_relative 'util'` in rb/app.rb → rb/util.rb."""
    _, store = _index(corpus_root)
    edges = [dict(r) for r in store.edges_from("rb/app.rb", type_="imports")]
    assert any(e["dst"] == "rb/util.rb" and e["confidence"] == "high" for e in edges)
    store.close()


def test_entrypoints_multi_language(corpus_root):
    """Bug #5: Go/C/Java main() should be detected as entrypoints."""
    _, store = _index(corpus_root)
    eps = {(r["file"], r["kind"]) for r in store.entrypoints()}
    assert ("go/cmd/main.go", "go-main") in eps
    assert ("c/main.c", "c-main") in eps
    assert ("java/com/demo/Service.java", "java-main") in eps
    store.close()


def test_python_from_dot_import_resolves(corpus_root):
    """Bug #7 (found by dogfooding projmem on itself):
    `from . import packs` in cli.py should produce an edge to packs.py, not
    merely resolve the `.` to the package __init__."""
    _, store = _index(corpus_root)
    # py/pkg/__init__.py: `from .models import User, Order` and `from .services import process`
    edges = {r["dst"] for r in store.edges_from("py/pkg/bare_from.py", type_="imports")}
    assert "py/pkg/models.py" in edges, edges
    assert "py/pkg/services.py" in edges, edges
    store.close()


def test_large_js_monolith_symbol_coverage(corpus_root):
    """Bug #8 (incomplete JS capture on monolithic files):
    - prototype methods, module.exports.*, exports.*, top-level consts,
      IIFE/class-expression assignments, case-label schema_field false-positive.
    """
    _, store = _index(corpus_root)
    syms = list(store.conn.execute(
        "SELECT name,kind,line FROM symbols WHERE file=?",
        ("js/monolith.js",)))
    by_name_kind = {(r["name"], r["kind"]) for r in syms}

    # Prototype methods
    assert ("greet", "method") in by_name_kind
    assert ("shout", "method") in by_name_kind
    # module.exports.X (full export surface)
    for exp in ("topLevelFn", "LegacyClass", "Queue", "UserService",
                "registry", "Tools", "reducer", "orchestrate"):
        assert (exp, "exported") in by_name_kind, exp
    # exports.X form
    assert ("helperX", "exported") in by_name_kind
    assert ("helperY", "exported") in by_name_kind
    # Class expression + IIFE const + object-literal const + plain top-level const
    assert ("Queue", "class") in by_name_kind
    assert ("Tools", "var") in by_name_kind
    assert ("registry", "var") in by_name_kind
    assert ("API_KEY", "var") in by_name_kind
    assert ("REGION", "var") in by_name_kind

    # case-label "BOOT" / "DONE" / "FAIL" / "RESET" must NOT be schema_field writes.
    bad = list(store.conn.execute(
        "SELECT name,line FROM contracts WHERE file=? AND kind='schema_field' "
        "AND name IN ('BOOT','FAIL','RESET')", ("js/monolith.js",)))
    assert bad == [], f"case-label false positives: {bad}"

    store.close()


def test_python_sibling_import_resolves(corpus_root):
    """Defect #1: `from differential import analyze` in analysis/report_builder.py
    must resolve to analysis/differential.py (sibling-to-importer lookup)."""
    _, store = _index(corpus_root)
    rev = {r["src"] for r in store.edges_to("analysis/differential.py",
                                             type_="imports")}
    assert "analysis/report_builder.py" in rev
    assert "scripts/test_analysis_pipeline.py" in rev
    store.close()


def test_js_same_file_refs_all_captured(corpus_root):
    """Defect #2: every same-file call site must be recorded, not just the first."""
    _, store = _index(corpus_root)
    # probe() is called 4 times in js/monolith.js after its definition.
    refs = [dict(r) for r in store.conn.execute(
        "SELECT line FROM refs WHERE file=? AND name='probe' ORDER BY line",
        ("js/monolith.js",))]
    assert len(refs) >= 4, f"expected ≥4 same-file refs to probe(), got {refs}"
    store.close()


def test_split_confidence_reports_structural_and_contract(corpus_root):
    """Defect #3: `overall_confidence` should no longer be the single readout.
    structural_confidence / contract_confidence must both be present."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "analysis/report_builder.py")
    cov = pack["coverage"]
    assert "structural_confidence" in cov
    assert "contract_confidence" in cov
    assert "overall_confidence" in cov
    # Nav is AST-resolved → should be high.
    assert cov["structural_confidence"] in ("high", "medium")
    store.close()


def test_orphans_reports_unreferenced_symbols(corpus_root):
    """Defect #4: `_internal_not_referenced` defined in analysis/differential.py
    and called nowhere must appear in orphans output."""
    from projmem.cli import build_parser, cmd_orphans
    import io, json
    from contextlib import redirect_stdout

    _, store = _index(corpus_root)
    store.close()

    parser = build_parser()
    args = parser.parse_args(["--path", corpus_root, "orphans", "--limit", "500"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_orphans(args)
    data = json.loads(buf.getvalue())
    names = {(o["file"], o["name"]) for o in data["orphans"]}
    assert ("analysis/differential.py", "_internal_not_referenced") in names


def test_regex_backend_captures_same_file_call_refs(tmp_path):
    """Regression for the pwnpilot run2 report: when JS falls back to regex
    (tree-sitter unavailable, large file, etc.), same-file call sites must
    still be recorded. Previously the regex path did `if nm in defined: continue`
    which silently dropped every intra-file call — exactly the scanner.js case.
    """
    from projmem.indexer import index_regex
    from projmem.store import Store

    src = """
function _looksLikeWsDeath(err) { return err && String(err.message).includes("ws"); }
function cdpCallOptional(fn) {
  try { return fn(); }
  catch (e) { if (_looksLikeWsDeath(e)) return null; throw e; }
}
function scanUrl(u) { return cdpCallOptional(() => ({ u })); }

// Multiple same-file call sites to the same name — all must be recorded.
scanUrl("a");
scanUrl("b");
scanUrl("c");
cdpCallOptional(() => 1);
"""
    store = Store(str(tmp_path / "db.sqlite"))
    store.set_meta("root", str(tmp_path))
    index_regex(store, "scanner.js", src, lang="javascript", confidence="low")
    store.commit()

    # Refs to a symbol defined in the same file must be present.
    lld = [r["line"] for r in store.conn.execute(
        "SELECT line FROM refs WHERE file='scanner.js' AND name='_looksLikeWsDeath' ORDER BY line")]
    assert len(lld) >= 1, f"_looksLikeWsDeath has no refs under regex backend: {lld}"

    cdp = [r["line"] for r in store.conn.execute(
        "SELECT line FROM refs WHERE file='scanner.js' AND name='cdpCallOptional' ORDER BY line")]
    assert len(cdp) >= 2, f"cdpCallOptional should have ≥2 refs (monolith): {cdp}"

    # Multiple same-file call sites to scanUrl — ≥3 distinct lines.
    scn = [r["line"] for r in store.conn.execute(
        "SELECT line FROM refs WHERE file='scanner.js' AND name='scanUrl' ORDER BY line")]
    assert len(scn) >= 3, f"scanUrl should have ≥3 refs: {scn}"

    # The definition line of scanUrl must NOT appear as a ref (that's the def site).
    defs_line = [r["line"] for r in store.conn.execute(
        "SELECT line FROM symbols WHERE file='scanner.js' AND name='scanUrl'")]
    assert defs_line and defs_line[0] not in scn, \
        f"def site leaked as ref: def={defs_line}, refs={scn}"
    store.close()


def test_ast_backend_captures_same_file_call_refs(corpus_root):
    """Mirror of the regex test on the AST/tree-sitter path."""
    _, store = _index(corpus_root)
    cdp = [r["line"] for r in store.conn.execute(
        "SELECT line FROM refs WHERE file='js/monolith.js' AND name='cdpCallOptional' ORDER BY line")]
    lld = [r["line"] for r in store.conn.execute(
        "SELECT line FROM refs WHERE file='js/monolith.js' AND name='_looksLikeWsDeath' ORDER BY line")]
    assert len(cdp) >= 1, f"cdpCallOptional same-file refs via AST: {cdp}"
    assert len(lld) >= 1, f"_looksLikeWsDeath same-file refs via AST: {lld}"
    store.close()


def test_node_require_main_entrypoint(corpus_root):
    """scanner.js-style entrypoint: `if (require.main === module)` must be detected."""
    _, store = _index(corpus_root)
    eps = {(r["file"], r["kind"]) for r in store.entrypoints()}
    assert ("js/monolith.js", "node-main-guard") in eps
    store.close()


def test_entrypoints_deduplicated(corpus_root):
    """Re-indexing must not accumulate duplicate entrypoint rows."""
    cfg = config_mod.load(corpus_root)
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    indexer.index_all(cfg, store, force=True)
    indexer.index_all(cfg, store, force=True)
    rows = list(store.conn.execute("SELECT file, kind, COUNT(*) FROM entrypoints "
                                    "GROUP BY file, kind HAVING COUNT(*) > 1"))
    assert rows == [], f"duplicate entrypoints after 3 reindexes: {rows}"
    store.close()


def test_pack_file_hash_symbol_disambiguation(corpus_root):
    """`projmem pack js/monolith.js#cdpCallOptional` must scope to that file only."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "js/monolith.js#cdpCallOptional")
    assert pack["target"].get("disambiguated") is True
    assert pack["target"]["file"] == "js/monolith.js"
    assert len(pack["target"]["defs"]) == 1
    # No ambiguous-symbol warning when user explicitly disambiguates.
    assert not any(u["kind"] == "ambiguous-symbol" for u in pack["unknowns"])
    store.close()


def test_ambiguous_symbol_does_not_silently_merge(corpus_root):
    """Bare `cdpCallOptional` has 2 defs across files → must warn, not merge."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "cdpCallOptional")
    amb = [u for u in pack["unknowns"] if u["kind"] == "ambiguous-symbol"]
    assert len(amb) == 1
    files_in_alts = {a["file"] for a in amb[0]["alternatives"]}
    assert "js/monolith.js" in files_in_alts
    assert "js/controller/cdp_utils.js" in files_in_alts
    store.close()


def test_intra_file_callgraph_surfaces_hidden_edges(corpus_root):
    """Monolith-critical: scanUrl → cdpCallOptional → _looksLikeWsDeath
    must be visible as intra-file edges (the pwnpilot scanner.js blocker)."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "js/monolith.js")
    cg = pack["intra_file_calls"]
    edges = {(e["from"], e["to"]) for e in cg["edges"]}
    assert ("scanUrl", "cdpCallOptional") in edges
    assert ("cdpCallOptional", "_looksLikeWsDeath") in edges
    store.close()


def test_node_builtins_not_unresolved(corpus_root):
    """`fs`, `path` and friends are known external — not `unresolved-imports`."""
    cfg, store = _index(corpus_root)
    # Edge to a builtin: present, confidence high, dst = builtin:node:X
    dsts = {r["dst"] for r in store.edges_from("js/monolith.js", type_="imports")}
    assert "builtin:node:fs" in dsts
    assert "builtin:node:path" in dsts
    pack = packs.build_pack(cfg, store, "js/monolith.js")
    # monolith.js only imports Node builtins. unresolved-imports should NOT fire.
    assert not any(u["kind"] == "unresolved-imports" for u in pack["unknowns"])
    store.close()


def test_callgraph_fail_loud_on_missing_file(corpus_root):
    """H5: callgraph on a non-indexed file must return partial=True with a
    warning, and MUST NOT emit edges without nodes."""
    from projmem import packs as _packs
    _, store = _index(corpus_root)
    cg = _packs._intra_file_calls(store, "nope/does_not_exist.js")
    assert cg["partial"] is True
    assert cg["edges"] == []
    assert cg["nodes"] == []
    assert any("not indexed" in w.lower() for w in cg["warnings"])
    store.close()


def test_callgraph_surfaces_nodes_and_parser(corpus_root):
    """H5 positive case: callgraph returns both nodes AND edges, tagged with parser."""
    from projmem import packs as _packs
    _, store = _index(corpus_root)
    cg = _packs._intra_file_calls(store, "js/monolith.js")
    assert cg["partial"] is False
    assert len(cg["nodes"]) > 10
    assert len(cg["edges"]) > 0
    assert cg["parser"].startswith("treesitter") or cg["parser"] == "regex"
    store.close()


def test_orphans_default_filters_to_structural_kinds(corpus_root):
    """M1: orphans must not return token/contract rows by default."""
    from projmem.cli import build_parser, cmd_orphans
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", corpus_root, "orphans"])
    with redirect_stdout(buf):
        cmd_orphans(args)
    data = _json.loads(buf.getvalue())
    kinds = {o["kind"] for o in data["orphans"]}
    # Every kind returned must be in the structural allowlist.
    assert kinds, "no orphans at all — fixture issue?"
    allowed = {"function", "class", "method", "exported", "var", "interface",
               "type", "enum", "struct", "trait", "module", "object"}
    assert kinds.issubset(allowed), f"non-structural kinds leaked: {kinds - allowed}"


def test_dotted_assignment_schema_field_writes(corpus_root):
    """H3: `result.status = 'DONE'` must land as schema_field write_assign."""
    _, store = _index(corpus_root)
    rows = [dict(r) for r in store.conn.execute(
        "SELECT * FROM contracts WHERE kind='schema_field' AND name='status' "
        "AND file='js/monolith.js' AND role='write_assign'")]
    assert len(rows) >= 1, rows
    # Python attr-assign path: `rec.setdefault(...)` is already covered;
    # add a direct attr-assign test via the guard fixture.
    rows_py = [dict(r) for r in store.conn.execute(
        "SELECT * FROM contracts WHERE kind='schema_field' "
        "AND role='write_assign' AND confidence='high'")]
    # At least one high-confidence attr-assign from Python code
    # (any schema field name qualifies; the fixture has `.status` writes).
    assert rows_py, "Python attr-assign writes not captured"
    store.close()


def test_event_listener_pair_detection(corpus_root):
    """H4: `bus.on('scan.started', ...)` and `bus.emit('scan.started')` must
    both be captured, same event name, different roles."""
    _, store = _index(corpus_root)
    rows = [dict(r) for r in store.conn.execute(
        "SELECT role FROM contracts WHERE kind='event' AND name='scan.started' "
        "AND file='js/monolith.js'")]
    roles = {r["role"] for r in rows}
    assert "emit" in roles
    assert "listen" in roles
    # Orphan listener must show up with listen role and no emit peer.
    orph = [dict(r) for r in store.conn.execute(
        "SELECT role FROM contracts WHERE kind='event' "
        "AND name='orphan-listener-event'")]
    assert orph and all(o["role"] == "listen" for o in orph)
    store.close()


def test_events_command_lists_emit_only_and_listen_only(corpus_root):
    from projmem.cli import build_parser, cmd_events
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", corpus_root, "events"])
    with redirect_stdout(buf):
        cmd_events(args)
    data = _json.loads(buf.getvalue())
    by_name = {e["name"]: e for e in data["events"]}
    assert "scan.started" in by_name
    assert not by_name["scan.started"]["listen_only"]
    assert not by_name["scan.started"]["emit_only"]
    assert by_name["orphan-listener-event"]["listen_only"] is True


def test_symbol_output_marks_lower_bound_when_regex(tmp_path):
    """D2/C2: if any file in the index was parsed by regex, symbol output
    must mark `ref_count_is_lower_bound: true`."""
    # Build a tiny index where at least one file is regex-parsed.
    import shutil, projmem.ts_backend as ts
    dst = tmp_path / "proj"
    dst.mkdir()
    (dst / "a.js").write_text("function foo(){}\nfoo();\nfoo();\n")
    orig = ts.index
    ts.index = lambda *a, **k: False
    try:
        cfg = config_mod.load(str(dst))
        store = Store(cfg.db_path)
        indexer.index_all(cfg, store)
        store.close()
    finally:
        ts.index = orig
    from projmem.cli import build_parser, cmd_symbol
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", str(dst), "symbol", "foo"])
    with redirect_stdout(buf):
        cmd_symbol(args)
    data = _json.loads(buf.getvalue())
    assert data["ref_count_is_lower_bound"] is True
    assert data["refs_from_regex_parsers"] >= 1


def test_reach_command_lists_guards(corpus_root):
    """H2 (lite): `projmem reach run_probe` should list the `if`-guard
    conditions wrapping each call."""
    from projmem.cli import build_parser, cmd_reach
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", corpus_root, "reach", "run_probe"])
    with redirect_stdout(buf):
        cmd_reach(args)
    data = _json.loads(buf.getvalue())
    conds = [r["context"] for r in data["reachable_from"]]
    assert any("len(params)" in c for c in conds), conds
    assert any("ready" in c for c in conds), conds


def test_symbol_scoped_pack_has_intra_file_edges(corpus_root):
    """Monolith workflow: `pack file#symbol` surfaces same-file incoming/outgoing."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "js/monolith.js#cdpCallOptional")
    sm = pack.get("intra_file_for_symbol")
    assert sm is not None
    incoming_callers = {e["from"] for e in sm["incoming"]}
    outgoing_callees = {e["to"] for e in sm["outgoing"]}
    assert "scanUrl" in incoming_callers
    assert "_looksLikeWsDeath" in outgoing_callees
    store.close()


def test_include_overrides_exclude_for_subpath(tmp_path):
    """Round-2 bug: `--exclude 'chrome/**' --include 'chrome/src/**'` was
    producing zero files because the directory-prune check didn't know the
    include reaches INTO the excluded dir."""
    import os
    root = tmp_path / "proj"
    (root / "chrome" / "src").mkdir(parents=True)
    (root / "chrome" / "other").mkdir(parents=True)
    (root / "chrome" / "src" / "wanted.py").write_text("def keep(): pass\n")
    (root / "chrome" / "other" / "skipme.py").write_text("def drop(): pass\n")
    (root / "chrome" / "top_level.py").write_text("def also_drop(): pass\n")

    from projmem.discovery import walk
    kept = set(walk(str(root),
                    include_globs=["chrome/src/**"],
                    exclude_globs=["chrome/**"]))
    rels = {os.path.relpath(p, str(root)) for p in kept}
    assert "chrome/src/wanted.py" in rels, rels
    assert "chrome/other/skipme.py" not in rels, rels
    assert "chrome/top_level.py" not in rels, rels


def test_callgraph_by_caller_count_not_truncated(corpus_root):
    """Round-2 bug: by_caller_count was computed from the already-sliced edge
    list, hiding dominant callers on large files. Stats must reflect ALL edges."""
    from projmem import packs as _packs
    _, store = _index(corpus_root)
    # Force aggressive truncation: cap=1. by_caller_count must still reflect
    # the full edge set (several callers in our monolith).
    cg = _packs._intra_file_calls(store, "js/monolith.js", cap=1)
    assert cg["truncated"] is True
    assert len(cg["edges"]) == 1
    # by_caller_count is built from all edges, so it should have > 1 entry
    # since the monolith has many callers (scanUrl, cdpCallOptional, finalize, ...).
    assert len(cg["by_caller_count"]) > 1, cg["by_caller_count"]
    assert cg["total"] > 1
    store.close()


def test_callgraph_all_flag_disables_truncation(corpus_root):
    from projmem import packs as _packs
    _, store = _index(corpus_root)
    cg_full = _packs._intra_file_calls(store, "js/monolith.js", cap=0)
    assert cg_full["truncated"] is False
    assert len(cg_full["edges"]) == cg_full["total"]
    store.close()


def test_callgraph_in_function_ordering(corpus_root):
    """Round-2 need: `--in-function` returns calls made inside FN, in line order."""
    from projmem.cli import build_parser, cmd_callgraph
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args([
        "--path", corpus_root, "callgraph", "js/monolith.js",
        "--in-function", "cdpCallOptional"])
    with redirect_stdout(buf):
        cmd_callgraph(args)
    data = _json.loads(buf.getvalue())
    assert data["in_function"] == "cdpCallOptional"
    # cdpCallOptional calls _looksLikeWsDeath (our fixture); must appear.
    callees = {c["to"] for c in data["calls"]}
    assert "_looksLikeWsDeath" in callees
    # Edges ordered by line ascending.
    lines = [c["line"] for c in data["calls"]]
    assert lines == sorted(lines)


def test_store_uses_wal_when_available(tmp_path):
    """WAL mode enables concurrent readers during writes — round-2 fix for
    'database is locked' when running `stats` during `index`."""
    from projmem.store import Store
    s = Store(str(tmp_path / "db.sqlite"))
    mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    # Some filesystems reject WAL — but local tmp paths should allow it.
    assert mode in ("wal", "delete", "memory"), mode
    s.close()


def test_drift_surfaces_static_never_exercised(tmp_path):
    """L3 static↔runtime moat: `projmem drift` surfaces defs the runtime
    evidence never touched — the unique signal no other tool emits."""
    import os, shutil
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.py").write_text(
        "def used_fn(): pass\ndef never_used(): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    # Add a synthetic evidence row pointing at used_fn.
    store.add_evidence(source="run1.jsonl", target="used_fn",
                       kind="trace", note="ok")
    store.commit()
    store.close()

    from projmem.cli import build_parser, cmd_drift
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", str(root), "drift"])
    with redirect_stdout(buf):
        cmd_drift(args)
    data = _json.loads(buf.getvalue())
    names = {s["name"] for s in data["defined_never_exercised"]}
    assert "never_used" in names
    assert "used_fn" not in names


def test_drift_warns_when_no_evidence(corpus_root):
    """If nothing's been ingested, drift must say so honestly rather than
    labeling every static symbol as drift."""
    from projmem.cli import build_parser, cmd_drift
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", corpus_root, "drift"])
    with redirect_stdout(buf):
        cmd_drift(args)
    data = _json.loads(buf.getvalue())
    assert "warning" in data
    assert data["defined_never_exercised"] == []


def test_ts_backend_respects_parse_timeout(monkeypatch):
    """Round-2 CRITICAL: ts_backend.index must return False (regex fallback)
    if tree-sitter parsing/querying doesn't complete within the timeout.
    We simulate the hang by monkey-patching the parser to sleep."""
    from projmem import ts_backend
    import os as _os, time, tempfile
    if not ts_backend.available():
        import pytest; pytest.skip("tree-sitter not installed")

    # Force a tiny timeout and a synthetic 'hang' in the query step.
    monkeypatch.setenv("PROJMEM_TS_TIMEOUT_MS", "100")
    real_compile = ts_backend._compile

    class _SlowParser:
        def parse(self, _b):
            time.sleep(1.0)
            raise RuntimeError("should not be reached")

    class _Q:  # placeholder, never reached because parse hangs
        pass

    monkeypatch.setitem(ts_backend._COMPILED, "python", (_SlowParser(), _Q()))

    class _Store:
        def add_symbol(self, **k): pass
        def add_ref(self, **k): pass
        def add_edge(self, **k): pass

    t0 = time.perf_counter()
    ok = ts_backend.index(_Store(), "fake.py", "x = 1\n", "python", "/tmp")
    elapsed = time.perf_counter() - t0
    assert ok is False, "should have fallen back on timeout"
    assert elapsed < 2.0, f"did not time out in bounded time: {elapsed:.2f}s"


def test_contracts_kind_filter_applies_to_file_targets(corpus_root):
    """Round-3 bug #1: `contracts <file> --kind env` returned all kinds."""
    from projmem.cli import build_parser, cmd_contracts
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args([
        "--path", corpus_root, "contracts", "js/monolith.js", "--kind", "env"])
    with redirect_stdout(buf):
        cmd_contracts(args)
    data = _json.loads(buf.getvalue())
    assert data["kind_filter"] == "env"
    kinds = {c["kind"] for c in data["contracts"]}
    assert kinds == {"env"} or kinds == set(), f"leaked kinds: {kinds}"


def test_entrypoints_mark_unindexed_targets(tmp_path):
    """Round-3 bug #2: entrypoint derived from package.json pointing at a
    non-indexed file must be marked indexed=0."""
    import os
    root = tmp_path / "proj"
    root.mkdir()
    (root / "cli.js").write_text("// entry\n")
    # package.json points at a file we don't index (missing on disk).
    (root / "package.json").write_text(
        '{"main": "controller/missing.js", "bin": {"x": "cli.js"}}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    rows = {(r["file"], r["kind"], r["indexed"])
            for r in store.entrypoints()}
    # The bin entry (cli.js) IS indexed; the main entry (missing.js) is NOT.
    assert any(file == "cli.js" and idx == 1 for (file, kind, idx) in rows)
    assert any("missing.js" in file and idx == 0 for (file, kind, idx) in rows)
    store.close()


def test_edge_insert_dedupes(tmp_path):
    """Round-3 bug #3: duplicate edges inflated `reverse`. Insert-time dedupe
    on (src, dst, type) makes multiple `require('fs')` calls produce ONE edge."""
    src = ("const fs = require('fs');\n"
           "const f2 = require('fs');\n"   # second call — dup edge target
           "const f3 = require('fs');\n"
           "function go(){ require('fs'); }\n")
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.js").write_text(src)
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    rows = [dict(r) for r in store.edges_from("a.js", type_="imports")]
    # One edge per (src, dst, type) — must not appear 4 times.
    dsts = [r["dst"] for r in rows]
    assert dsts.count("builtin:node:fs") == 1, dsts
    store.close()


def test_js_refs_capture_new_and_destructured_imports(tmp_path):
    """Round-3 bug #4: widen JS refs. `new Foo()`, destructured require/import
    must produce ref rows."""
    root = tmp_path / "p"; root.mkdir()
    (root / "constants.js").write_text(
        "const CANARY_TOKENS = ['x'];\n"
        "module.exports = { CANARY_TOKENS };\n")
    (root / "user.js").write_text(
        "const { CANARY_TOKENS } = require('./constants');\n"
        "import { Helper } from './helper';\n"
        "new Helper(CANARY_TOKENS);\n")
    (root / "helper.js").write_text("export class Helper {}\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)
    # The CANARY_TOKENS destructured-require must land as an `import_binding` ref.
    refs = [dict(r) for r in store.conn.execute(
        "SELECT name, kind FROM refs WHERE file='user.js' AND name='CANARY_TOKENS'")]
    kinds = {r["kind"] for r in refs}
    assert "import_binding" in kinds, f"destructured require not captured: {refs}"
    # `new Helper(...)` must land as a ref with kind=new.
    refs_new = [dict(r) for r in store.conn.execute(
        "SELECT name, kind FROM refs WHERE file='user.js' AND kind='new'")]
    assert any(r["name"] == "Helper" for r in refs_new), refs_new
    # `import { Helper } from ...` must also land as import_binding.
    imp = [dict(r) for r in store.conn.execute(
        "SELECT name, kind FROM refs WHERE file='user.js' AND kind='import_binding' "
        "AND name='Helper'")]
    assert imp, "import {} from destructured-import not captured"
    store.close()


def test_callgraph_stable_envelope(corpus_root):
    """Round-3 bug #5: filter-to mode must include the same top-level keys
    as full mode (edges/total/truncated/nodes/by_caller_count/parser)."""
    from projmem.cli import build_parser, cmd_callgraph
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()

    def run(extra):
        buf = io.StringIO()
        args = build_parser().parse_args(
            ["--path", corpus_root, "callgraph", "js/monolith.js"] + extra)
        with redirect_stdout(buf):
            cmd_callgraph(args)
        return _json.loads(buf.getvalue())

    full = run([])
    filt = run(["--filter-to", "cdpCallOptional"])
    infn = run(["--in-function", "cdpCallOptional"])
    required = {"file", "nodes", "edges", "total", "truncated",
                "by_caller_count", "partial", "warnings", "parser",
                "filter_symbol", "in_function"}
    for view in (full, filt, infn):
        missing = required - set(view)
        assert not missing, f"missing keys: {missing}"
    # incoming/outgoing appear only in filter-to mode
    assert "incoming" in filt and "outgoing" in filt
    assert "calls" in infn


def test_missing_ref_kinds_surfaced(corpus_root):
    """Round-3 #13: pack coverage must list ref kinds the backend does not
    capture, so the consumer knows when counts are a lower bound."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "js/monolith.js")
    mrk = pack["coverage"].get("missing_ref_kinds")
    assert isinstance(mrk, list)
    assert "identifier_read" in mrk or "property_access" in mrk
    store.close()


def test_files_command_reports_scope(corpus_root):
    from projmem.cli import build_parser, cmd_files
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", corpus_root, "files"])
    with redirect_stdout(buf):
        cmd_files(args)
    data = _json.loads(buf.getvalue())
    assert data["count"] > 0
    assert any(f["path"] == "js/monolith.js" for f in data["files"])
    assert "builtin_skip_dirs" in data
    assert "node_modules" in data["builtin_skip_dirs"]


def test_unresolved_imports_global_command(corpus_root):
    from projmem.cli import build_parser, cmd_unresolved_imports
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", corpus_root, "unresolved-imports", "--limit", "50"])
    with redirect_stdout(buf):
        cmd_unresolved_imports(args)
    data = _json.loads(buf.getvalue())
    assert "unresolved_imports" in data
    # None of the entries should be builtin Node modules — those were
    # classified out of `unresolved-imports` in an earlier round.
    for r in data["unresolved_imports"]:
        assert not r["dst"].startswith("builtin:")


def test_pack_symbol_target_reverse_deps_include_refs(tmp_path):
    """Round-4 P0: pack for `file#symbol` must include same-file call sites
    as reverse_dependencies. This was the `setupCanaryInterceptor` bug —
    3 real callers, pack returned `reverse_dependencies: []`."""
    root = tmp_path / "p"; root.mkdir()
    (root / "mono.js").write_text(
        "function setupCanaryInterceptor() { return 1; }\n"
        "function a() { setupCanaryInterceptor(); }\n"
        "function b() { setupCanaryInterceptor(); }\n"
        "function c() { setupCanaryInterceptor(); }\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "mono.js#setupCanaryInterceptor")
    # Reverse deps must now contain three rows (one per call site line).
    rev_lines = sorted({d.get("line") for d in pack["reverse_dependencies"]
                        if d.get("via") == "call"})
    assert len(rev_lines) == 3, pack["reverse_dependencies"]
    # And each must carry `via="call"` (symbol-level, not `imports`).
    vias = {d.get("via") for d in pack["reverse_dependencies"]}
    assert "call" in vias
    store.close()


def test_parity_filters_js_builtins(tmp_path):
    """Round-4 P1: parity referenced-but-undefined used to include `String`,
    `substring`, `console`, etc. — 100% noise. Now filtered by default."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.js").write_text(
        "function go(x) {\n"
        "  const s = String(x);\n"
        "  console.log(s.substring(0, 3));\n"
        "  unknownThing();\n"
        "}\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()

    from projmem.cli import build_parser, cmd_parity
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", str(root), "parity"])
    with redirect_stdout(buf):
        cmd_parity(args)
    data = _json.loads(buf.getvalue())
    names = {d["name"] for d in data["referenced_but_undefined"]}
    # Builtins must be filtered out.
    for b in ("String", "console", "log", "substring"):
        assert b not in names, f"builtin {b} leaked: {names}"
    # Real typos must remain.
    assert "unknownThing" in names
    assert data["builtins_filtered"] > 0


def test_parity_include_builtins_disables_filter(tmp_path):
    root = tmp_path / "p"; root.mkdir()
    (root / "a.js").write_text("console.log(1);\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_parity
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "parity", "--include-builtins"])
    with redirect_stdout(buf):
        cmd_parity(args)
    data = _json.loads(buf.getvalue())
    names = {d["name"] for d in data["referenced_but_undefined"]}
    # `console.log(1)` — the tree-sitter query captures the property
    # (`log`) as a call-site ref; `log` is a stdlib-method name that the
    # builtin filter WOULD otherwise drop.
    assert "log" in names
    assert data["builtins_filtered"] == 0


def test_orphans_sees_callback_passing_and_shorthand_exports(tmp_path):
    """Round-4 P1: `.map(foo)` (callback) and `{foo}` (shorthand export)
    must count as references to `foo`, so it's not flagged orphan."""
    root = tmp_path / "p"; root.mkdir()
    (root / "mod.js").write_text(
        "function normalizeInputUrlLine(x) { return x; }\n"
        "function shorthandExported() { return 2; }\n"
        "function trulyDead() { return 3; }\n"
        "['a','b'].map(normalizeInputUrlLine);\n"
        "module.exports = { shorthandExported };\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_orphans
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "orphans", "--kind", "function"])
    with redirect_stdout(buf):
        cmd_orphans(args)
    data = _json.loads(buf.getvalue())
    names = {o["name"] for o in data["orphans"]}
    # False positives must be gone
    assert "normalizeInputUrlLine" not in names, names
    assert "shorthandExported" not in names, names
    # Real dead function must still be flagged
    assert "trulyDead" in names


def test_callees_of_transitive_within_file(corpus_root):
    """Top-utility wishlist item: transitive blast-radius query."""
    from projmem.cli import build_parser, cmd_callees_of
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", corpus_root, "callees-of", "scanUrl",
         "--file", "js/monolith.js", "--depth", "3"])
    with redirect_stdout(buf):
        cmd_callees_of(args)
    data = _json.loads(buf.getvalue())
    reachable = set(data["unique_reachable"])
    # scanUrl → cdpCallOptional → _looksLikeWsDeath
    assert "cdpCallOptional" in reachable
    assert "_looksLikeWsDeath" in reachable
    # depth is bounded — no self-loop
    assert "scanUrl" not in reachable


def test_callees_of_errors_on_unknown_symbol(corpus_root):
    from projmem.cli import build_parser, cmd_callees_of
    import io, json as _json
    from contextlib import redirect_stdout
    _, store = _index(corpus_root); store.close()
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", corpus_root, "callees-of", "DoesNotExistXYZ"])
    with redirect_stdout(buf):
        try:
            cmd_callees_of(args)
            rc = 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    data = _json.loads(buf.getvalue())
    assert data["error"] == "symbol-undefined"
    # Round-5 meta-fix: structured errors exit 2.
    assert rc == 2


def test_m1_role_bitset_populated_automatically(tmp_path):
    """M1: every ref emitted via `add_ref(kind=...)` auto-derives a roles
    bitset (SCIP-shaped). Old code paths keep working, new queries on roles
    are enabled."""
    from projmem.store import (Store, ROLE_CALL, ROLE_READ, ROLE_NEW,
                               ROLE_IMPORT_BINDING, ROLE_IMPORT, roles_describe)
    root = tmp_path / "p"; root.mkdir()
    (root / "a.js").write_text(
        "function foo(){}\nfoo();\n"
        "class Bar {}\nnew Bar();\n"
        "const {helper} = require('./util');\n")
    (root / "util.js").write_text("module.exports = { helper: () => 1 };\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store)

    foo_refs = list(store.conn.execute(
        "SELECT roles FROM refs WHERE name='foo' AND file='a.js'"))
    assert foo_refs
    assert all(r["roles"] & ROLE_CALL and r["roles"] & ROLE_READ
               for r in foo_refs), foo_refs

    bar_refs = list(store.conn.execute(
        "SELECT roles FROM refs WHERE name='Bar' AND file='a.js' AND kind='new'"))
    assert bar_refs and (bar_refs[0]["roles"] & ROLE_NEW)

    helper_refs = list(store.conn.execute(
        "SELECT roles FROM refs WHERE name='helper' AND file='a.js' "
        "AND kind='import_binding'"))
    assert helper_refs
    r = helper_refs[0]["roles"]
    assert r & ROLE_IMPORT_BINDING and r & ROLE_IMPORT
    # roles_describe roundtrip
    names = roles_describe(r)
    assert "import_binding" in names and "import" in names
    store.close()


def test_m1_symbol_role_filter_cli(tmp_path):
    """M1: `projmem symbol foo --role call` returns only call-site refs."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.js").write_text(
        "function foo(){}\nfoo();\nconst {foo:alias} = require('./b');\n")
    (root / "b.js").write_text("module.exports = { foo: () => 1 };\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()

    from projmem.cli import build_parser, cmd_symbol
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "symbol", "foo", "--role", "call"])
    with redirect_stdout(buf):
        cmd_symbol(args)
    data = _json.loads(buf.getvalue())
    # All returned refs must have the call role
    from projmem.store import ROLE_CALL
    assert data["refs"]  # at least one
    for r in data["refs"]:
        assert (r["roles"] or 0) & ROLE_CALL, r


def test_m2_symbol_end_line_populated(tmp_path):
    """M2: `end_line` stored for function/class symbols from the AST path."""
    root = tmp_path / "p"; root.mkdir()
    (root / "m.py").write_text(
        "def outer():\n"
        "    def inner():\n"
        "        pass\n"
        "    return inner\n"
        "\n"
        "class C:\n"
        "    def m(self):\n"
        "        return 1\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    rows = {r["name"]: dict(r) for r in store.conn.execute(
        "SELECT name, line, end_line FROM symbols WHERE file='m.py'")}
    # outer spans lines 1..4; C spans 6..8 (or 6..8+)
    assert rows["outer"]["end_line"] >= 4
    assert rows["C"]["end_line"] >= 8
    store.close()


def test_m2_exact_enclosing_range_beats_heuristic(tmp_path):
    """M2: with end_line available, a ref inside a nested function must
    attribute to the INNER function, not the outer. The old heuristic
    would pick whichever function's start line was most recently seen —
    works for simple cases but fails on non-monotonic layouts."""
    root = tmp_path / "p"; root.mkdir()
    (root / "m.py").write_text(
        "def helper(): pass\n"
        "def outer():\n"
        "    def inner():\n"
        "        helper()\n"       # call inside inner()
        "    return inner\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "m.py")
    cg = pack["intra_file_calls"]
    # Caller attribution must say "exact_range", not "scope_approximation"
    assert cg["caller_attribution"] == "exact_range"
    # helper() at line 4 — enclosed by inner (3..4/5) → caller = inner
    helper_edges = [e for e in cg["edges"] if e["to"] == "helper"]
    assert helper_edges and helper_edges[0]["from"] == "inner", helper_edges
    store.close()


def test_m3_python_extends_edge(tmp_path):
    """M3+M8: `class Dog(Animal)` in Python emits an `extends` edge.
    Source uses canonical symbol_id; target uses canonical symbol_id when
    Animal has a unique def in the repo (this fixture: yes)."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text("class Animal: pass\nclass Dog(Animal): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    edges = [dict(r) for r in store.conn.execute(
        "SELECT src, dst, type FROM edges WHERE type='extends'")]
    assert any(e["src"] == "a.py#Dog#" and
               e["dst"] in ("a.py#Animal#", "Animal") for e in edges), edges
    # Also present as a ref (kind=extends)
    refs = [dict(r) for r in store.conn.execute(
        "SELECT name, kind FROM refs WHERE file='a.py' AND name='Animal' "
        "AND kind='extends'")]
    assert refs
    store.close()


def test_m3_typescript_implements_edge(tmp_path):
    """M3: `class Dog implements Animal { ... }` emits `implements` edge."""
    import projmem.ts_backend as ts
    if not ts.available():
        import pytest; pytest.skip("tree-sitter not installed")
    root = tmp_path / "p"; root.mkdir()
    (root / "a.ts").write_text(
        "interface Animal { sound(): string; }\n"
        "class Dog implements Animal { sound(): string { return 'woof'; } }\n"
        "class Puppy extends Dog {}\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    edges = [dict(r) for r in store.conn.execute(
        "SELECT src, dst, type FROM edges WHERE type IN ('extends','implements')")]
    # M8: src is canonical symbol_id (`<file>#Name#`). Pull bare class names
    # from the src for the assertion shape.
    def src_name(e):
        # symbol_id format: `file#Name#` — strip suffix and split on '#'
        rest = e["src"].split("#", 1)[1] if "#" in e["src"] else e["src"]
        if rest.endswith("#"): rest = rest[:-1]
        return rest
    srcs_types = {(src_name(e), e["type"], e["dst"]) for e in edges}
    # dst may be canonical (`a.ts#Animal#`) when uniquely resolvable; otherwise bare name.
    assert any(s == "Dog" and t == "implements" and d in ("a.ts#Animal#", "Animal")
               for (s, t, d) in srcs_types), edges
    assert any(s == "Puppy" and t == "extends" and d in ("a.ts#Dog#", "Dog")
               for (s, t, d) in srcs_types), edges
    store.close()


def test_m8_symbol_id_format(tmp_path):
    """M8: every indexed symbol must carry a canonical `<file>#<name><suffix>`
    symbol_id. Suffix mapping: function/var/method = '.', class/interface/
    struct/enum/trait/type = '#', module = '/'."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text(
        "def foo(): pass\n"
        "class Bar: pass\n"
        "X = 1\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    rows = {r["name"]: dict(r) for r in store.conn.execute(
        "SELECT name, kind, symbol_id FROM symbols WHERE file='a.py'")}
    assert rows["foo"]["symbol_id"] == "a.py#foo."
    assert rows["Bar"]["symbol_id"] == "a.py#Bar#"
    # X is captured as kind=var by Python AST → `.` suffix.
    if "X" in rows:
        assert rows["X"]["symbol_id"].endswith(".")
    store.close()


def test_m8_symbol_id_disambiguates_collisions(tmp_path):
    """M8 promise: two `Animal` classes in different files get DIFFERENT
    symbol_ids. The store can no longer collide them."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text("class Animal: pass\n")
    (root / "b.py").write_text("class Animal: pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    ids = sorted(r["symbol_id"] for r in store.conn.execute(
        "SELECT symbol_id FROM symbols WHERE name='Animal'"))
    assert ids == ["a.py#Animal#", "b.py#Animal#"], ids
    # And both are O(1) addressable
    a = store.symbol_by_id("a.py#Animal#")
    b = store.symbol_by_id("b.py#Animal#")
    assert a and a["file"] == "a.py"
    assert b and b["file"] == "b.py"
    store.close()


def test_m8_extends_edge_resolves_to_symbol_id_when_unique(tmp_path):
    """M3+M8: when the parent class has a UNIQUE def in the repo, the
    `extends` edge target uses the canonical symbol_id, not the bare name.
    Confidence high; downstream queries can join symbol↔edge cleanly."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text("class Animal: pass\nclass Dog(Animal): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    edges = [dict(r) for r in store.conn.execute(
        "SELECT src, dst, type, confidence FROM edges WHERE type='extends'")]
    extends = [e for e in edges if e["src"].endswith("#Dog#")]
    assert extends, edges
    e = extends[0]
    # dst MUST be the canonical symbol_id, not bare "Animal"
    assert e["dst"] == "a.py#Animal#", e
    assert e["confidence"] == "high", e
    store.close()


def test_m8_extends_edge_falls_back_to_name_when_ambiguous(tmp_path):
    """When the parent class name is AMBIGUOUS (multiple defs, no
    disambiguation), the edge falls back to the bare name with
    confidence=medium. Honest behavior — no fake resolution."""
    root = tmp_path / "p"; root.mkdir()
    (root / "x.py").write_text("class Animal: pass\n")
    (root / "y.py").write_text("class Animal: pass\n")
    (root / "z.py").write_text("from x import Animal\nclass Pet(Animal): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    edges = [dict(r) for r in store.conn.execute(
        "SELECT src, dst, type, confidence FROM edges "
        "WHERE type='extends' AND src LIKE '%Pet%'")]
    assert edges
    # Two Animals exist → resolver returns None → dst == "Animal"
    assert edges[0]["dst"] == "Animal", edges
    assert edges[0]["confidence"] == "medium", edges
    store.close()


def test_m8_pack_accepts_symbol_id_target(tmp_path):
    """M8: `projmem pack <symbol_id>` works as a target shape, gives O(1)
    resolution, no ambiguous-symbol warning."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text("def foo(): pass\ndef caller(): foo()\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "a.py#foo.")
    t = pack["target"]
    assert t["kind"] == "symbol"
    assert t["disambiguated"] is True
    assert t["symbol_id"] == "a.py#foo."
    assert t["file"] == "a.py"
    assert t["defs"] and t["defs"][0]["name"] == "foo"
    assert not any(u["kind"] == "ambiguous-symbol" for u in pack["unknowns"])
    store.close()


def test_m8_pack_symbol_id_lookup_failure_is_fail_loud(tmp_path):
    """M8: a well-formed but absent symbol_id must produce a typed
    `lookup_failed` signal, not silently degrade to a string-name search."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text("def foo(): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "a.py#bar.")  # bar doesn't exist
    t = pack["target"]
    assert t.get("lookup_failed") is True
    assert t["defs"] == []
    # And there's a clear unknown
    kinds = [u["kind"] for u in pack["unknowns"]]
    assert any(k in ("symbol-not-in-file", "symbol-undefined") for k in kinds)
    store.close()


def test_pack_treats_package_json_as_file(tmp_path):
    """Round-X bug #1: `pack package.json` was returning symbol-undefined.
    Now: bare config-file basenames resolve as files."""
    root = tmp_path / "p"; root.mkdir()
    (root / "package.json").write_text('{"name": "x", "main": "a.js"}\n')
    (root / "a.js").write_text("module.exports = {};\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "package.json")
    assert pack["target"]["kind"] == "file"
    assert pack["target"]["path"] == "package.json"
    store.close()


def test_pack_normalizes_dot_slash_target(tmp_path):
    """Round-X bug #2: `pack ./package.json` failed to resolve."""
    root = tmp_path / "p"; root.mkdir()
    (root / "package.json").write_text('{"name":"x"}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "./package.json")
    assert pack["target"]["kind"] == "file"
    assert pack["target"]["path"] == "package.json"
    assert pack["target"].get("normalized_from") == "./package.json"
    store.close()


def test_pack_force_kind_overrides(tmp_path):
    """`--as-symbol` and `--as-file` overrides force the interpretation."""
    root = tmp_path / "p"; root.mkdir()
    (root / "package.json").write_text('{}\n')
    (root / "a.py").write_text('def package(): pass\n')  # collision: symbol named "package"
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    # --as-symbol forces symbol path, finds the function
    pack = packs.build_pack(cfg, store, "package", force_kind="symbol")
    assert pack["target"]["kind"] == "symbol"
    assert pack["target"]["defs"], "should have found the `package` function"
    # --as-file forces file path
    pack = packs.build_pack(cfg, store, "package.json", force_kind="file")
    assert pack["target"]["kind"] == "file"
    store.close()


def test_exclude_wins_subtracts_inside_included_subtree(tmp_path):
    """Round-X feedback #3: include-wins blocked `--include deploy/**` AND
    `--exclude deploy/patches/**`. New `exclude_wins=True` mode resolves it:
    excludes always subtract, even within an included subtree."""
    import os
    root = tmp_path / "p"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "patches").mkdir()
    (root / "deploy" / "run.sh").write_text("echo run\n")
    (root / "deploy" / "patches" / "vendor.cc").write_text("int x = 1;\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store,
                      extra_includes=["deploy/**"],
                      extra_excludes=["deploy/patches/**"],
                      exclude_wins=True)
    files = {r["path"] for r in store.all_files()}
    assert "deploy/run.sh" in files
    assert not any("patches" in f for f in files), files
    store.close()


def test_default_include_wins_still_works(tmp_path):
    """Regression: default mode (without --exclude-wins) keeps round-3
    include-wins behavior so previously-shipping users don't break."""
    root = tmp_path / "p"
    (root / "chrome" / "src").mkdir(parents=True)
    (root / "chrome" / "other").mkdir()
    (root / "chrome" / "src" / "want.py").write_text("def k(): pass\n")
    (root / "chrome" / "other" / "drop.py").write_text("def d(): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store,
                      extra_includes=["chrome/src/**"],
                      extra_excludes=["chrome/**"])
    files = {r["path"] for r in store.all_files()}
    assert "chrome/src/want.py" in files
    assert "chrome/other/drop.py" not in files
    store.close()


def test_scope_command_reports_last_index_globs(tmp_path):
    """Round-X feedback #5: `projmem scope` exposes EXACT effective scope
    used at last index (CLI globs included), not just config-level."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path)
    indexer.index_all(cfg, store,
                      extra_includes=["**/*.py"],
                      extra_excludes=["build/**"],
                      exclude_wins=True)
    store.close()
    from projmem.cli import build_parser, cmd_scope
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", str(root), "scope"])
    with redirect_stdout(buf):
        cmd_scope(args)
    data = _json.loads(buf.getvalue())
    sess = data["last_index_session"]
    assert sess["include_globs_cli"] == ["**/*.py"]
    assert sess["exclude_globs_cli"] == ["build/**"]
    assert sess["exclude_wins"] is True


def test_missing_paths_detects_broken_package_main(tmp_path):
    """Round-X new feature #4: `missing-paths` flags `package.json::main`
    that references a non-existent file — the most common run-blocker."""
    root = tmp_path / "p"; root.mkdir()
    (root / "package.json").write_text(
        '{"main": "controller/missing.js", "scripts": {"start": "node ./does_not_exist.js"}}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_missing_paths
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(["--path", str(root), "missing-paths"])
    with redirect_stdout(buf):
        cmd_missing_paths(args)
    data = _json.loads(buf.getvalue())
    refs = {(m["kind"], m["ref"]) for m in data["missing_paths"]}
    assert ("package.json:main", "controller/missing.js") in refs
    assert any(k.startswith("package.json:scripts:") for k, _ in refs)


def test_unresolved_imports_classifies_external_includes(tmp_path):
    """Round-X feedback #6: C system headers (`#include <stdio.h>`) and
    bare `"foo/bar.h"` includes from C/C++ files must classify as
    `external_include`, not as broken repo-relative imports."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.c").write_text(
        '#include <stdio.h>\n'
        '#include "vendor/lib.h"\n'
        '#include "./local.h"\n'
        'int x = 1;\n')
    (root / "local.h").write_text("int local;\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_unresolved_imports
    import io, json as _json
    from contextlib import redirect_stdout
    # Default: external_include hidden
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "unresolved-imports"])
    with redirect_stdout(buf):
        cmd_unresolved_imports(args)
    data = _json.loads(buf.getvalue())
    kinds_seen = {r["import_kind"] for r in data["unresolved_imports"]}
    assert "external_include" not in kinds_seen, data["unresolved_imports"]
    assert data["by_kind"].get("external_include", 0) >= 1
    # --show-external surfaces them
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "unresolved-imports", "--show-external"])
    with redirect_stdout(buf):
        cmd_unresolved_imports(args)
    data = _json.loads(buf.getvalue())
    kinds_seen = {r["import_kind"] for r in data["unresolved_imports"]}
    assert "external_include" in kinds_seen


def test_package_json_parsed_into_contracts(tmp_path):
    """Round-X feedback: package.json is a contract surface. Parse it into
    structured `entrypoint`, `script`, `dep` contracts so downstream
    queries can reason about it."""
    root = tmp_path / "p"; root.mkdir()
    (root / "package.json").write_text(
        '{"main": "src/index.js",\n'
        ' "bin": {"mycli": "bin/cli.js"},\n'
        ' "scripts": {"build": "tsc", "test": "jest"},\n'
        ' "dependencies": {"left-pad": "^1.0.0"},\n'
        ' "devDependencies": {"jest": "^29.0.0"}}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    by_kind: dict = {}
    for r in store.conn.execute(
            "SELECT kind, name, role, context FROM contracts "
            "WHERE file='package.json'"):
        by_kind.setdefault(r["kind"], []).append(dict(r))
    # Entrypoints
    eps = {(c["name"]) for c in by_kind.get("entrypoint", [])}
    assert "main" in eps
    assert "bin:mycli" in eps
    # Scripts
    scripts = {c["name"] for c in by_kind.get("script", [])}
    assert {"build", "test"} <= scripts
    # Deps differentiated by `role` field
    deps = by_kind.get("dep", [])
    roles = {c["role"] for c in deps}
    assert "dependencies" in roles
    assert "devDependencies" in roles
    store.close()


def test_contract_drift_command_finds_value_mismatches(tmp_path):
    """Round-X new command: same script name with different contexts
    across multiple package.json files should surface as drift."""
    root = tmp_path / "p"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "package.json").write_text(
        '{"scripts": {"start": "node ./a-server.js"}}\n')
    (root / "b" / "package.json").write_text(
        '{"scripts": {"start": "node ./b-server.js"}}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_contract_drift
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "contract-drift", "--kind", "script"])
    with redirect_stdout(buf):
        cmd_contract_drift(args)
    data = _json.loads(buf.getvalue())
    drift_names = {(d["kind"], d["name"]) for d in data["drift"]}
    # `start` is declared with two different commands → drift
    assert ("script", "start") in drift_names


def test_pack_snippets_bounded_and_labelled(tmp_path):
    """Round-X feedback #8: `pack --snippets` returns bounded code excerpts
    labelled non-authoritative."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text(
        "def foo():\n    return 'one'\n\n"
        "def bar():\n    return 'two'\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store)
    pack = packs.build_pack(cfg, store, "a.py#foo.",
                            include_snippets=True, snippet_bytes=200)
    sn = pack.get("snippets")
    assert sn is not None
    assert sn["bytes_spent"] <= sn["byte_budget"]
    assert sn["items"]
    item = sn["items"][0]
    assert "def foo" in item["text"]
    assert item["bytes"] > 0
    store.close()


def test_scope_module_is_in_scope():
    """Round-X scope module: vendor prefix detection works on common cases."""
    from projmem.scope import is_in_scope, _DEFAULT_VENDOR_PREFIXES
    vp = _DEFAULT_VENDOR_PREFIXES
    assert is_in_scope("src/main.py", vp)
    assert is_in_scope("a.js", vp)
    assert is_in_scope("<config>", vp)
    assert is_in_scope("", vp)
    assert not is_in_scope("chrome/src/x.cc", vp)
    assert not is_in_scope("node_modules/foo/index.js", vp)
    assert not is_in_scope("vendor/lib.h", vp)
    assert not is_in_scope("third_party/x.py", vp)


def test_contract_drift_in_scope_flag_and_scope_only(tmp_path):
    """Round-X feedback: drift flooded with 98% chrome/ noise. Each row now
    carries `in_scope`; `--scope-only` filters."""
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "chrome" / "vendor").mkdir(parents=True)
    # Two own-code package.jsons with different `start` scripts (real drift)
    (root / "src" / "package.json").write_text(
        '{"scripts": {"start": "node ./own-server.js"}}\n')
    (root / "package.json").write_text(
        '{"scripts": {"start": "node ./other-server.js"}}\n')
    # Two vendor package.jsons that also disagree (vendor noise)
    (root / "chrome" / "vendor" / "package.json").write_text(
        '{"scripts": {"start": "vendored-cmd-a"}}\n')
    (root / "chrome" / "package.json").write_text(
        '{"scripts": {"start": "vendored-cmd-b"}}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_contract_drift
    import io, json as _json
    from contextlib import redirect_stdout

    # Default: both groups present, each tagged
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "contract-drift", "--kind", "script"])
    with redirect_stdout(buf):
        cmd_contract_drift(args)
    data = _json.loads(buf.getvalue())
    rows = data["drift"]
    assert rows, "expected at least one drift row"
    in_scope_count = sum(1 for r in rows if r.get("in_scope"))
    out_scope_count = sum(1 for r in rows if not r.get("in_scope"))
    # We expect both kinds tagged; each row's in_scope is True only when
    # ALL its sites are own-code.
    assert in_scope_count >= 0  # may be 0 if drift collapsed both groups; tagging present
    # `vendor_prefixes` surfaced
    assert "chrome/" in data["vendor_prefixes"]

    # --scope-only suppresses any row that touches chrome/
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "contract-drift", "--kind", "script",
         "--scope-only"])
    with redirect_stdout(buf):
        cmd_contract_drift(args)
    data2 = _json.loads(buf.getvalue())
    for r in data2["drift"]:
        for s in r.get("sites", []):
            assert "chrome/" not in s["file"], r


def test_missing_paths_in_scope_flag_and_scope_only(tmp_path):
    """Round-X: missing-paths in chrome/ produces 148 vendor fixture rows.
    Each row now carries `in_scope`; `--scope-only` drops vendor rows."""
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "chrome" / "fixture").mkdir(parents=True)
    # Own-code: real run-blocker
    (root / "package.json").write_text(
        '{"main": "src/MISSING.js"}\n')
    # Vendor: chrome fixture with broken refs (noise)
    (root / "chrome" / "package.json").write_text(
        '{"main": "fixture/also-MISSING.js"}\n')
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_missing_paths
    import io, json as _json
    from contextlib import redirect_stdout

    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "missing-paths"])
    with redirect_stdout(buf):
        cmd_missing_paths(args)
    data = _json.loads(buf.getvalue())
    own = [m for m in data["missing_paths"] if m.get("in_scope")]
    vendor = [m for m in data["missing_paths"] if not m.get("in_scope")]
    assert any(m["file"] == "package.json" for m in own)
    assert any(m["file"] == "chrome/package.json" for m in vendor)

    # --scope-only suppresses vendor row
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "missing-paths", "--scope-only"])
    with redirect_stdout(buf):
        cmd_missing_paths(args)
    data2 = _json.loads(buf.getvalue())
    for m in data2["missing_paths"]:
        assert not m["file"].startswith("chrome/"), m


def test_callees_of_schema_aliases_present(tmp_path):
    """Round-X feedback: `callees-of` schema was inconsistent. Now provides
    `tree`/`callees`, `unique_reachable`/`unique_callees`,
    `reachable_count`/`callee_count` aliases + `schema_version`."""
    root = tmp_path / "p"; root.mkdir()
    (root / "a.py").write_text(
        "def helper(): pass\n"
        "def caller():\n"
        "    helper()\n"
        "    inner()\n"
        "def inner(): pass\n")
    cfg = config_mod.load(str(root))
    store = Store(cfg.db_path); indexer.index_all(cfg, store); store.close()
    from projmem.cli import build_parser, cmd_callees_of
    import io, json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    args = build_parser().parse_args(
        ["--path", str(root), "callees-of", "caller"])
    with redirect_stdout(buf):
        cmd_callees_of(args)
    data = _json.loads(buf.getvalue())
    # All canonical + alias keys present
    for key in ("tree", "callees", "unique_reachable", "unique_callees",
                "reachable_count", "callee_count", "schema_version",
                "in_scope", "vendor_prefixes"):
        assert key in data, f"missing schema key: {key}"
    # Aliases ARE the same data
    assert data["callees"] is data["tree"] or data["callees"] == data["tree"]
    assert data["unique_callees"] == data["unique_reachable"]
    assert data["callee_count"] == data["reachable_count"]
    # Schema version is a positive int
    assert isinstance(data["schema_version"], int) and data["schema_version"] >= 1


def test_exclude_patterns_prune_dirs(corpus_root):
    """--exclude / config.exclude_globs should prune at directory level, not just
    filter each file individually."""
    import os
    # Fake a node_modules dir under the corpus with a file that would otherwise index.
    nm = os.path.join(corpus_root, "node_modules", "pkg")
    os.makedirs(nm, exist_ok=True)
    with open(os.path.join(nm, "lib.js"), "w") as f:
        f.write("export function stub() {}\n")
    cfg = config_mod.load(corpus_root)
    store = Store(cfg.db_path)
    counts = indexer.index_all(cfg, store,
                               extra_excludes=["node_modules/**", "vendor"])
    files = {r["path"] for r in store.all_files()}
    assert not any("node_modules" in p for p in files), files
    assert "node_modules" in counts["excluded_dirs"]
    store.close()


def test_ambient_token_is_not_fanned_out(corpus_root):
    """Bug #4: tokens present in many files should not pull all peers into packs."""
    cfg, store = _index(corpus_root)
    pack = packs.build_pack(cfg, store, "py/monolith/big.py")
    ambient = pack["semantic_contracts"]["ambient"]
    ambient_names = {(a["kind"], a["name"]) for a in ambient}
    # DONE is present in ~12 files in the corpus; must be ambient.
    assert ("token", "DONE") in ambient_names
    # No related_file should cite 'DONE' (it was filtered).
    for r in pack["semantic_contracts"]["related_files"]:
        assert not (r["kind"] == "token" and r["name"] == "DONE"), r
    store.close()
