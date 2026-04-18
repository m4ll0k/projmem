import io
import json
import os
import sys
from contextlib import redirect_stdout

from projmem.cli import main


def run_cli(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            rc = main(args)
        except SystemExit as e:
            # Several commands now `sys.exit(2)` on structured errors
            # (ambiguous-symbol, target-not-found, unknown-predicate,
            # unsupported-language). Treat that as the rc the caller
            # would observe from the shell.
            rc = e.code if isinstance(e.code, int) else 1
    return rc, buf.getvalue()


def test_cli_index_and_pack(fresh_project):
    rc, out = run_cli(["--path", fresh_project, "index"])
    assert rc == 0
    data = json.loads(out)
    assert data["indexed"] >= 3

    rc, out = run_cli(["--path", fresh_project, "reverse", "src/core.py"])
    assert rc == 0
    rev = json.loads(out)
    assert any(r["file"] == "cli/main.py" for r in rev["reverse_dependencies"])

    rc, out = run_cli(["--path", fresh_project, "pack", "src/core.py"])
    assert rc == 0
    pack = json.loads(out)
    assert pack["target"]["path"] == "src/core.py"
    assert "coverage" in pack

    rc, out = run_cli(["--path", fresh_project, "entrypoints"])
    assert rc == 0
    eps = json.loads(out)["entrypoints"]
    assert any(e["file"] == "cli/main.py" for e in eps)


def test_cli_contracts(fresh_project):
    run_cli(["--path", fresh_project, "index"])
    rc, out = run_cli(["--path", fresh_project, "contracts", "DATABASE_URL", "--kind", "env"])
    assert rc == 0
    data = json.loads(out)
    assert data["occurrences"]


def test_cli_symbol_exhaustive_reports_text_matches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text(
        "FOO();\n"
        "console.log('FOO');\n"
        "// FOO in comment\n"
    )
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = run_cli(["--path", str(repo), "symbol", "FOO", "--exhaustive"])
    assert rc == 0
    data = json.loads(out)

    assert data["coverage"]["text"] == 3
    assert data["coverage"]["structured"] >= 1
    assert data["coverage"]["gap"] >= 1
    assert data["text_match_count"] == 3
    assert len(data["text_matches"]) == 3
    # At least one of the non-call occurrences should be surfaced as implicit.
    assert any("comment" in (m.get("text") or "") for m in data["implicit_refs"])


def test_cli_symbol_surfaces_binding_edges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "binding.cc").write_text(
        "void InternalModuleStat() {}\n"
        "void Init() {\n"
        "  SetMethod(ctx, target, \"internalModuleStat\", InternalModuleStat);\n"
        "}\n"
    )
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = run_cli(["--path", str(repo), "symbol", "internalModuleStat"])
    assert rc == 0
    data = json.loads(out)
    assert data.get("binding_edges"), "symbol lookup should surface binding edges"
    assert any(e.get("cpp_name") == "InternalModuleStat" for e in data["binding_edges"])


def test_cli_symbol_soft_ambiguity_surfaces_candidates(tmp_path):
    """Default is now STRICT (matches `callees-of`): multi-def names
    hard-error with `error: ambiguous-symbol` + candidates list. Soft
    behavior moved behind `--allow-ambiguous`. The previous default
    silently scoped refs to one def, which agents read as ground
    truth — now they're forced to disambiguate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def Has():\n    return True\n")
    (repo / "b.py").write_text("def Has():\n    return False\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0

    # Default — strict — must hard-error.
    rc, out = run_cli(["--path", str(repo), "symbol", "Has"])
    assert rc == 2, f"expected exit 2 for ambiguous default; got rc={rc}"
    data = json.loads(out)
    assert data.get("error") == "ambiguous-symbol"
    assert data.get("candidate_count") >= 2
    assert data.get("candidates")

    # --allow-ambiguous opts back into the soft path.
    rc, out = run_cli(["--path", str(repo), "symbol", "Has",
                        "--allow-ambiguous"])
    assert rc == 0, f"--allow-ambiguous should succeed; rc={rc}"
    data = json.loads(out)
    assert data.get("error") is None

    # File-qualified query is unambiguous either way.
    rc, out = run_cli(["--path", str(repo), "symbol", "a.py#Has"])
    assert rc == 0
    data = json.loads(out)
    assert data.get("error") is None
    assert "ambiguity_warning" not in data


def test_cli_symbol_strict_ambiguity_returns_error(tmp_path):
    """--strict-ambiguity is now an alias for the default; both produce
    the hard-error behavior with exit code 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def Has():\n    return True\n")
    (repo / "b.py").write_text("def Has():\n    return False\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = run_cli(["--path", str(repo), "symbol", "Has",
                         "--strict-ambiguity"])
    assert rc == 2
    data = json.loads(out)
    assert data.get("error") == "ambiguous-symbol"
    assert data.get("candidate_count", 0) >= 2


def test_cli_stats_parser_coverage(tmp_path):
    """stats output must include parser_coverage with AST/regex/none breakdown
    and per-language percentages. Totals must sum to 100% and be internally
    consistent."""
    from projmem.ts_backend import available as ts_available
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    (repo / "b.py").write_text("def bar():\n    return 2\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0

    rc, out = run_cli(["--path", str(repo), "stats"])
    assert rc == 0
    data = json.loads(out)

    assert "parser_coverage" in data, (
        f"Expected parser_coverage key in stats; got {list(data.keys())}")
    cov = data["parser_coverage"]

    # Top-level keys must be present.
    for key in ("total_files", "ast_grounded", "regex_fallback", "no_parser", "per_lang"):
        assert key in cov, f"Missing key {key!r} in parser_coverage"

    # ast + regex + none counts must sum to total.
    total = cov["total_files"]
    assert total >= 2
    parts = (cov["ast_grounded"]["count"]
             + cov["regex_fallback"]["count"]
             + cov["no_parser"]["count"])
    assert parts == total, f"Counts don't sum to total: {parts} != {total}"

    # Percentages must sum to 100 (within rounding tolerance).
    pct_sum = (cov["ast_grounded"]["pct"]
               + cov["regex_fallback"]["pct"]
               + cov["no_parser"]["pct"])
    assert abs(pct_sum - 100.0) <= 0.5, f"pct sum {pct_sum} not ~100"

    # per_lang must include python.
    assert "python" in cov["per_lang"], (
        f"Expected python in per_lang; got {list(cov['per_lang'].keys())}")
    py = cov["per_lang"]["python"]
    for k in ("ast", "regex", "none", "total", "ast_pct"):
        assert k in py, f"Missing key {k!r} in per_lang.python"

    # With tree-sitter available, python files should be AST-grounded.
    if ts_available():
        assert py["ast"] >= 2, (
            f"Expected python files AST-grounded with tree-sitter; got {py}")
        assert py["ast_pct"] > 0.0


def test_cli_symbol_context_auto_in_json_mode(tmp_path):
    """`--context auto` with --json must attach snippets (n=2) so agents
    can triage in one round-trip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    alpha()\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "--json", "symbol", "alpha",
                        "--allow-ambiguous", "--no-implicit-check",
                        "--context", "auto"])
    assert rc == 0
    data = json.loads(out)
    # At least one def/ref must carry a snippet field when context=auto in
    # JSON mode.
    items = (data.get("defs") or []) + (data.get("refs") or [])
    assert any(r.get("snippet") for r in items), (
        f"--context auto in --json mode should attach snippets; got items: "
        f"{[(r.get('file'), r.get('line'), 'snippet' in r) for r in items]}")


def test_cli_symbol_context_auto_no_json_does_not_attach(tmp_path):
    """`--context auto` WITHOUT --json must keep snippets off (default 0)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def thing():\n    return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    rc, out = run_cli(["--path", str(repo), "symbol", "thing",
                        "--allow-ambiguous", "--no-implicit-check",
                        "--context", "auto"])
    data = json.loads(out)
    items = (data.get("defs") or []) + (data.get("refs") or [])
    # No snippet attached because we're not in --json mode.
    assert not any(r.get("snippet") for r in items)


def test_cli_ask_lowercase_subject_resolves(tmp_path):
    """F015 (round-7): pure-lowercase identifiers like `normalize`,
    `callapplication` must be picked up by the subject extractor as
    a fallback when no verb / path / backtick / quoted form matches."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def normalize(x): return x\n"
        "def callapplication(h): return h()\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "ask", "callapplication"])
    data = json.loads(out)
    assert data["subject"] == "callapplication"


def test_cli_evidence_rejects_unknown_target(tmp_path):
    """F016 (round-7): `evidence` must validate target against the
    indexed file/symbol set. Garbage targets get counted under
    `invalid_target` and dropped, NOT stored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    ev = tmp_path / "ev.jsonl"
    ev.write_text(
        json.dumps({"target": "not-a-real-target",
                     "kind": "covered"}) + "\n"
        + json.dumps({"file": "a.py", "kind": "covered"}) + "\n"
        + json.dumps({"missing_keys": True}) + "\n"
    )
    rc, out = run_cli(["--path", str(repo), "evidence", str(ev)])
    assert rc == 0
    data = json.loads(out)
    assert data["ok"] == 1
    assert data["invalid_target"] == 1
    assert data["missing_target"] == 1
    assert data["invalid_target_examples"][0]["target"] == \
        "not-a-real-target"


def test_cli_drift_excludes_require_bindings(tmp_path):
    """F017 (round-7): `drift.defined_never_exercised` must skip
    `const X = require(...)` import-binding `var` symbols by default."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text(
        "const foo = require('./helper');\n"
        "function realFn() { return 42; }\n")
    (repo / "helper.js").write_text("module.exports = {};\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # Need at least one evidence row so drift runs the comparison
    # (otherwise it short-circuits with "No evidence ingested").
    ev = tmp_path / "ev.jsonl"
    ev.write_text(
        json.dumps({"file": "a.js", "kind": "covered"}) + "\n")
    run_cli(["--path", str(repo), "evidence", str(ev)])
    rc, out = run_cli(["--path", str(repo), "drift"])
    assert rc == 0
    data = json.loads(out)
    names = {s["name"] for s in
             data.get("defined_never_exercised") or []}
    assert "foo" not in names, (
        f"`foo` (require binding) leaked into drift: {names}")
    assert data.get("import_bindings_filtered", 0) >= 1


def test_cli_complete_propagates_contradicted_to_overall(tmp_path):
    """F018 (round-7): `complete` must surface `contradicted_count > 0`
    in BOTH the exit code AND the top-level `overall` field. Previously
    the gate (rc=1) and the status (`overall: ok`) disagreed.

    To force a REFUTED (→ contradicted) status we cite the symbol in
    a file that doesn't contain it. MOVED status (same file, wrong
    line) is only weakly_stale and wouldn't trigger the gate.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def realfn(): return 1\n")
    (repo / "b.py").write_text("def other(): return 2\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # FACT claim that places `realfn` in `b.py` — not where it lives.
    # Verifier refutes immediately → note becomes `contradicted`.
    rc, _ = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "realfn at b.py:1",
        "--truth-class", "FACT",
        "--claims",
        json.dumps([{"subject": "realfn", "predicate": "defined-at",
                      "object": "b.py:1",
                      "truth_class": "FACT"}])])
    # note add itself triggers the contradicted-blocker exit.
    assert rc in (0, 1)
    rc, out = run_cli(["--path", str(repo), "complete"])
    assert rc == 1
    data = json.loads(out)
    assert data["overall"] == "unhealthy"
    assert data["severity_counts"]["high"] >= 1
    assert any(f["code"] == "contradicted_notes"
                for f in data["findings"])
    assert data["contradicted_notes_count"] >= 1


def test_cli_task_start_returns_task_id_field(tmp_path):
    """F019 (round-7): `task start` returns `task_id` (matches
    step/blocked/close); legacy `id` kept as alias for back-compat."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "task", "start", "do thing"])
    data = json.loads(out)
    assert "task_id" in data
    # Back-compat alias retained this round.
    assert data.get("id") == data["task_id"]


def test_cli_ask_safe_to_delete_unknown_returns_cannot_assess(tmp_path):
    """F021 (round-7): `ask 'safe to delete X'` on an unknown symbol
    must default to CANNOT_ASSESS, NOT to LIKELY_SAFE / 'safe to
    remove'. The safer default avoids advising deletion based on
    'I don't see it' alone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli([
        "--path", str(repo), "ask", "safe to delete unknownThing"])
    data = json.loads(out)
    detail = data.get("detail") or {}
    assert detail.get("verdict") == "CANNOT_ASSESS"
    assert "do NOT delete" in (data.get("summary") or "")


def test_cli_fact_check_diff_at_risk(tmp_path):
    """Round-6 gap #1: `fact-check --diff` over a hunk that REMOVES
    the cited line surfaces it under `at_risk` and exits 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, _ = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "alpha at 1",
        "--truth-class", "FACT",
        "--claims",
        json.dumps([{"subject": "alpha", "predicate": "defined-at",
                      "object": "a.py:1"}])])
    assert rc == 0
    diff_path = tmp_path / "patch"
    diff_path.write_text(
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,0 @@\n"
        "-def alpha():\n"
        "-    return 1\n"
    )
    rc, out = run_cli(["--path", str(repo), "fact-check",
                        "--diff", str(diff_path)])
    assert rc == 2
    data = json.loads(out)
    assert data["verdict"] == "at_risk"
    assert len(data["at_risk"]) == 1
    assert data["at_risk"][0]["claim"]["subject"] == "alpha"


def test_cli_fact_check_diff_moved_only(tmp_path):
    """Round-6 gap #1: insertion above the cited line shifts it down;
    verdict is `moved_only`, exit 0, `new_line` carries the predicted
    new position."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    run_cli(["--path", str(repo), "index"])
    run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "alpha at 1",
        "--truth-class", "FACT",
        "--claims",
        json.dumps([{"subject": "alpha", "predicate": "defined-at",
                      "object": "a.py:1"}])])
    diff_path = tmp_path / "patch"
    diff_path.write_text(
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+# header\n"
        "+# blank\n"
    )
    rc, out = run_cli(["--path", str(repo), "fact-check",
                        "--diff", str(diff_path)])
    assert rc == 0
    data = json.loads(out)
    assert data["verdict"] == "moved_only"
    assert len(data["moved"]) == 1
    assert data["moved"][0]["old_line"] == 1
    assert data["moved"][0]["new_line"] == 3


def test_cli_at_returns_enclosing_symbol(tmp_path):
    """Round-6 gap #2: `projmem at file:line` returns the enclosing
    symbol's pack."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def beta():\n"
        "    alpha()\n"
        "    return 2\n"
    )
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "at", "a.py:6"])
    assert rc == 0
    data = json.loads(out)
    assert data["symbol"]["name"] == "beta"
    assert data["symbol"]["def_line"] == 5
    assert data["pack"]["target"]["name"] == "beta"


def test_cli_at_unknown_file_errors(tmp_path):
    """Round-6 gap #2: cursor in a non-indexed file returns
    `file-not-indexed`, exit 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "at", "no_such.py:1"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "file-not-indexed"


def test_cli_note_add_auto_extracts_nl_claim_from_body(tmp_path):
    """Round-7-bench follow-up: agents write prose like
    `\\`setupmethod\\` is defined at a.py:1` and never reach for
    `--claims`. `note add` now auto-extracts NL claims from the
    body so the verifier engages even from prose. Default truth_class
    is FACT — INFERENCE doesn't fire `contradicted_count` on REFUTED.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def setupmethod(f):\n    return f\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note",
        "`setupmethod` is defined at a.py:1"])
    assert rc == 0
    data = json.loads(out)
    assert data["claim_count"] == 1
    assert data["auto_extracted_count"] == 1
    extracted = data["auto_extracted_claims"][0]
    assert extracted["subject"]   == "setupmethod"
    assert extracted["predicate"] == "defined-at"
    assert extracted["object"]    == "a.py:1"
    assert extracted["truth_class"] == "FACT"


def test_cli_note_add_auto_extract_off_by_flag(tmp_path):
    """`--no-auto-extract` preserves the legacy inline-only path: bodies
    without `@predicate(...)` syntax produce zero claims."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def setupmethod(f):\n    return f\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note",
        "`setupmethod` is defined at a.py:1",
        "--no-auto-extract"])
    assert rc == 0
    data = json.loads(out)
    assert data["claim_count"] == 0
    assert data["auto_extracted_count"] == 0


def test_cli_check_returns_lean_envelope(tmp_path):
    """Round-6 single-shot: `projmem check` returns ONLY the verdict +
    counts, not the full claims array. Cheap one-call verification for
    agents on a context budget."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "check",
                        "@defined-at(foo, a.py:1)"])
    assert rc == 0
    data = json.loads(out)
    # Lean envelope: counts + verdict + maybe hint. NO claims array.
    assert data["verdict"] == "all_verified"
    assert data["verified"] == 1
    assert "claims" not in data
    assert "bare_file_line_checks" not in data


def test_cli_check_line_constructs_claim(tmp_path):
    """Round-6 claim-authoring shortcut: `check-line file:line symbol`
    builds the @defined-at claim and runs check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli(["--path", str(repo), "check-line", "a.py:1", "foo"])
    assert rc == 0
    data = json.loads(out)
    assert data["verdict"] == "all_verified"
    assert data["verified"] == 1
    # Wrong line → has_moved (NOT refuted, since same file).
    rc, out = run_cli(["--path", str(repo), "check-line", "a.py:99", "foo"])
    data = json.loads(out)
    assert data["verdict"] == "has_moved"
    assert data["moved"] == 1


def test_cli_repo_memory_lean_when_no_news(tmp_path, monkeypatch):
    """Round-6 verbose-banner fix: when nothing actionable, `repo_memory`
    collapses to `{has_memory, contradicted_count, _lean}`. Loud cases
    (contradicted, no memory, root mismatch) keep the full block.

    Must chdir into the indexed root so the `root_mismatch_warning`
    (which fires when CWD is outside the indexed tree) doesn't count
    as news and force the verbose form.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    monkeypatch.chdir(repo)
    run_cli(["--path", str(repo), "index"])
    run_cli(["--path", str(repo), "note", "add",
              "a.py", "--kind", "note", "x"])
    # First call may carry onboarding/news; second within TTL collapses.
    run_cli(["--path", str(repo), "--json", "notes"])
    rc, out = run_cli(["--path", str(repo), "--json", "notes"])
    assert rc == 0
    data = json.loads(out)
    rm = data["repo_memory"]
    assert rm.get("_lean") is True
    assert set(rm.keys()) == {"has_memory", "contradicted_count", "_lean"}


def test_cli_paranoid_blocks_unverified_fact_claim(tmp_path,
                                                     monkeypatch):
    """Round-6 PROJMEM_PARANOID: refuses note add for FACT claims that
    haven't been fact-checked in this session. Default mode (env unset)
    accepts them as before."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    monkeypatch.setenv("PROJMEM_PARANOID", "1")
    # Unverified — must reject.
    rc, out = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "x", "--truth-class", "FACT",
        "--claims",
        json.dumps([{"subject": "foo", "predicate": "defined-at",
                      "object": "a.py:1"}])])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "claim-not-verified"
    # Verify first → memo records it → accept.
    rc, _ = run_cli(["--path", str(repo), "check",
                      "@defined-at(foo, a.py:1)"])
    assert rc == 0
    rc, out = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "x", "--truth-class", "FACT",
        "--claims",
        json.dumps([{"subject": "foo", "predicate": "defined-at",
                      "object": "a.py:1"}])])
    assert rc == 0


def test_cli_files_lang_short_alias(tmp_path):
    """Round-5-r3 F004: `--lang js` must alias to `javascript`. Was
    silently returning count:0 because the row stores the long name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text("function x(){}\n")
    (repo / "b.py").write_text("def y(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "files", "--lang", "js"])
    data = json.loads(out)
    assert data["count"] == 1
    assert data["files"][0]["path"] == "a.js"
    rc, out = run_cli(["--path", str(repo), "files", "--lang", "py"])
    data = json.loads(out)
    assert data["count"] == 1


def test_cli_doc_only_files_emit_no_symbols(tmp_path):
    """Round-5-r3 F001 belt-and-braces: `.md` / `.txt` must produce
    NO symbols and NO import edges, even when their bodies contain
    code-block-shaped text. Reverse-deps must not surface them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text("export const x = 1;\n")
    (repo / "README.md").write_text(
        "# demo\n```js\nimport foo from './a.js';\n```\n")
    (repo / "doc.txt").write_text("function fake(){ return 1; }\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "reverse", "a.js"])
    data = json.loads(out)
    md_hits = [d for d in data.get("reverse_dependencies", [])
               if d.get("file") in ("README.md", "doc.txt")]
    assert not md_hits, (
        f"doc-only files leaked into reverse-deps: {md_hits}")


def test_cli_search_invalid_limit_rejected(tmp_path):
    """Round-5-r3 F008: `search --limit -1` must error rather than
    silently clamp to 1."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "search", "x", "--limit", "-1"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "invalid-limit"


def test_cli_factcheck_surfaces_dedup_count(tmp_path):
    """Round-5-r3 F009: when the same `@predicate(s,o)` appears
    multiple times in the input, `duplicate_count` must be > 0
    and `candidate_count > extracted_count`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    text = "@defined-at(x, a.py:1) " * 5
    rc, out = run_cli(["--path", str(repo), "fact-check", text])
    data = json.loads(out)
    assert data["candidate_count"] == 5
    assert data["duplicate_count"] == 4
    assert data["extracted_count"] == 1


def test_cli_argparse_json_with_flag_anywhere(tmp_path):
    """Round-5-r3 F012: --json must produce a JSON envelope on
    argparse errors regardless of where it appears in argv."""
    rc, out = run_cli(["no-such-verb", "--json"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "argparse"


def test_cli_task_list_status_open_alias(tmp_path):
    """Round-5-r3 F013: `--status open` must alias `active`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, _ = run_cli(["--path", str(repo), "task", "start", "do thing"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "task", "list",
                        "--status", "open"])
    data = json.loads(out)
    tasks = data.get("tasks", [])
    assert tasks
    assert all(t["status"] == "active" for t in tasks)


def test_cli_doctor_symlink_root_explained(tmp_path):
    """Round-5-r3 F14: when the raw root differs from indexed_root
    only by a /tmp ↔ /private/tmp symlink, doctor surfaces an
    explicit `roots_symlink_equivalent: true` flag and a sentence
    so callers don't chase a phantom divergence."""
    import os as _os
    # Index via the realpath form, then doctor via the alias.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    real = _os.path.realpath(str(repo))
    rc, _ = run_cli(["--path", real, "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "doctor"])
    data = json.loads(out)
    # Either the strings match (no symlink in play) OR the realpath
    # equality flag fires.
    assert data["roots_match"]
    if data["root"] != data["indexed_root"]:
        assert data["roots_symlink_equivalent"] is True
        assert data["roots_note"]


def test_cli_session_unknown_target_errors(tmp_path):
    """Round-5-r2 F008: session on a target that doesn't resolve to a
    file or symbol must return target-not-found, not a "0 notes /
    0 neighbors" success blob that reads as 'safe to ship'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "session", "no_such_thing"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "target-not-found"
    assert "suggestions" in data


def test_cli_index_refuses_filesystem_root(tmp_path):
    """Round-5-r2 F005: `projmem index /` used to OSError its way
    through /proc and /dev. Now hard-rejected upfront."""
    rc, out = run_cli(["--path", "/", "index"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "refusing-catastrophic-root"


def test_cli_note_export_import_roundtrip_preserves_claims(tmp_path):
    """Round-5-r2 F001: export → import must preserve evidence /
    claims / confidence / truth_class / fingerprint. The previous
    export dropped them, so a FACT note round-tripped as a generic
    INFERENCE without the claim chain the verifier needs."""
    src_repo = tmp_path / "src"
    src_repo.mkdir()
    (src_repo / "a.py").write_text("def foo(): return 1\n")
    rc, _ = run_cli(["--path", str(src_repo), "index"])
    assert rc == 0
    # Add a note carrying a structured FACT claim + evidence.
    rc, _ = run_cli([
        "--path", str(src_repo), "note", "add",
        "a.py", "--kind", "note",
        "Foo lives at line 1.",
        "--truth-class", "FACT",
        "--evidence", "a.py:1",
        "--claims",
        json.dumps([{"subject": "foo", "predicate": "defined-at",
                      "object": "a.py:1", "truth_class": "FACT"}])])
    assert rc == 0
    out_path = tmp_path / "exp.jsonl"
    rc, _ = run_cli([
        "--path", str(src_repo), "note-export",
        "--output", str(out_path)])
    assert rc == 0
    payload = json.loads(out_path.read_text().strip())
    # F001: export must carry the extended fields.
    assert payload.get("truth_class") == "FACT"
    assert payload.get("evidence"), "export dropped the evidence field"
    # The original claim must be preserved in the evidence list.
    ev_subjects = {e.get("subject") for e in payload["evidence"]
                   if isinstance(e, dict)}
    assert "foo" in ev_subjects, (
        f"FACT claim did not survive export: {payload.get('evidence')}")
    # Re-import into a fresh repo and confirm the data is still there.
    dst_repo = tmp_path / "dst"
    dst_repo.mkdir()
    (dst_repo / "a.py").write_text("def foo(): return 1\n")
    rc, _ = run_cli(["--path", str(dst_repo), "index"])
    assert rc == 0
    rc, out = run_cli([
        "--path", str(dst_repo), "note-import", str(out_path)])
    assert rc == 0
    rc, out = run_cli(["--path", str(dst_repo), "note", "list"])
    list_data = json.loads(out)
    notes = list_data.get("annotations", [])
    assert notes
    # Round-tripped note must still be FACT and carry evidence.
    n = notes[0]
    assert n["truth_class"] == "FACT"
    # Evidence field is JSON-encoded on the row; round-trip must keep
    # the structured payload, including the FACT claim subject.
    ev_raw = n.get("evidence")
    assert ev_raw, "imported note lost its evidence field"
    ev = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
    subjects = {e.get("subject") for e in ev if isinstance(e, dict)}
    assert "foo" in subjects, (
        f"FACT claim did not survive round-trip: {ev}")


def test_cli_note_import_missing_file_errors(tmp_path):
    """Round-5-r2 F004: `note-import /no/such/file` used to dump a
    Python traceback. Now it returns a structured `read-failed`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli([
        "--path", str(repo), "note-import", "/no/such/file.jsonl"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "read-failed"


def test_cli_note_import_separates_dupes_and_invalid(tmp_path):
    """Round-5-r2 F003: dupes vs invalid lines must be counted
    separately. Was a single `skipped_dupes_or_invalid` counter."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # Add one note so we have a dupe candidate.
    rc, _ = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "first"])
    assert rc == 0
    jsonl = tmp_path / "ndj.jsonl"
    jsonl.write_text(
        json.dumps({"target": "a.py", "kind": "note", "body": "first"}) + "\n"
        "{not valid json\n"
        + json.dumps({"missing": "required keys"}) + "\n"
        + json.dumps({"target": "a.py", "kind": "note", "body": "fresh"}) + "\n"
    )
    rc, out = run_cli(["--path", str(repo), "note-import", str(jsonl)])
    assert rc == 0
    data = json.loads(out)
    assert data["imported"] == 1
    assert data["skipped_dupes"] == 1
    assert data["skipped_invalid"] == 2
    assert any(r["reason"] == "invalid-json"
                for r in data["invalid_lines"])
    assert any(r["reason"] == "missing-required-fields"
                for r in data["invalid_lines"])


def test_cli_guide_unknown_topic_errors(tmp_path):
    """Round-5-r2 F009: `guide nonsense` used to return an envelope
    but exit 0. Must be a structured error with exit 2."""
    rc, out = run_cli(["--json", "guide", "nonsense"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "unknown-topic"
    assert "available_options" in data


def test_cli_hook_outside_git_errors(tmp_path):
    """Round-5-r2 F010: `hook install` outside a git repo used to
    return a half-shaped envelope with exit 0. Now standardized."""
    repo = tmp_path / "not-a-git-repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli(["--path", str(repo), "hook", "install"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "no-git-repo"


def test_cli_task_close_bogus_id_errors(tmp_path):
    """Round-5 F001: closing a bogus task id used to return
    `status: done` exit 0 — silent wrong. Must error with
    `task-not-found` and exit 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    rc, out = run_cli(["--path", str(repo), "task", "close", "99999"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "task-not-found"
    assert data["task_id"] == 99999
    assert "candidates" in data


def test_cli_note_delete_bogus_id_errors(tmp_path):
    """Round-5 F002: `note delete <bogus>` used to return
    `{deleted: false}` exit 0. Must structured-error + exit 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli(["--path", str(repo), "note", "delete", "9999"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "note-not-found"


def test_cli_search_empty_query_errors(tmp_path):
    """Round-5 F004 (subset): `search ""` had the right envelope
    but exit 0. Must exit 2 now via `_emit_error`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli(["--path", str(repo), "search", ""])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "empty-query"


def test_cli_snapshot_empty_label_rejected(tmp_path):
    """Round-5 F005: `snapshot ""` used to silently rebrand to
    `manual`. Must reject explicit empty label."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    run_cli(["--path", str(repo), "index"])
    # snapshot's label is positional, not --label.
    rc, out = run_cli(["--path", str(repo), "snapshot", ""])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "empty-snapshot-label"


def test_cli_note_evidence_path_traversal_rejected(tmp_path):
    """Round-5 F006: --evidence with `..` segments that escape
    the repo root must be rejected, not silently stored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli([
        "--path", str(repo), "note", "add",
        "a.py", "--kind", "note", "x",
        "--evidence", "../../../etc/passwd:1",
    ])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "evidence-out-of-repo"
    assert data["reason"] in ("path-traversal", "absolute-outside-repo")


def test_cli_index_include_no_match_warning(tmp_path):
    """Round-5 P3: `index --include <no-match>` used to return
    `indexed: 0, unchanged: 0` silently. Must surface a warning."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x(): return 1\n")
    run_cli(["--path", str(repo), "index"])
    rc, out = run_cli(["--path", str(repo), "index",
                        "--include", "no/such/path/**"])
    assert rc == 0
    data = json.loads(out)
    assert "include_no_match_warning" in data


def test_cli_argparse_error_emits_json_when_json_flag(tmp_path):
    """Round-5 P3: argparse validation errors with --json should
    return a JSON-shaped envelope, not bare argparse stderr."""
    rc, out = run_cli(["--json", "no-such-verb"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "argparse"


def test_cli_index_reports_unchanged_not_skipped(tmp_path):
    """Round-4 #4: re-indexing an unmodified repo must report
    `unchanged: N`, not `skipped: N`. "skipped" was misread as
    "excluded by rules" when in fact the file was a no-op revisit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def x():\n    return 1\n")
    rc, out = run_cli(["--path", str(repo), "index"])
    assert rc == 0
    data = json.loads(out)
    assert data["indexed"] == 1
    assert data.get("unchanged") == 0
    # Re-run: file is unmodified.
    rc2, out2 = run_cli(["--path", str(repo), "index"])
    data2 = json.loads(out2)
    assert data2["indexed"] == 0
    assert data2["unchanged"] == 1
    # The misleading "skipped" key must not be present.
    assert "skipped" not in data2 or data2.get("skipped") is None


def test_cli_unindexed_root_returns_no_index_error(tmp_path):
    """Round-4 #3: a read command on a never-indexed root must return a
    structured `no-index` error (exit 2), not silent zeros."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    rc, out = run_cli(["--path", str(repo), "stats"])
    assert rc == 2
    data = json.loads(out)
    assert data["error"] == "no-index"
    assert "Run `projmem index" in data["hint"]


def test_cli_broken_pipe_exits_cleanly(tmp_path):
    """Regression: piping projmem output to `head` must not print a traceback
    or return a failure code under `set -o pipefail`."""
    import shlex
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    rc, _ = run_cli(["--path", str(repo), "index"])
    assert rc == 0

    cmd = (
        "set -o pipefail; "
        f"{shlex.quote(sys.executable)} -m projmem.cli --path "
        f"{shlex.quote(str(repo))} stats | head -n 1"
    )
    p = subprocess.run(["bash", "-lc", cmd],
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "BrokenPipe" not in (p.stderr or "")
