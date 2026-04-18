"""Regression tests for the audit-driven fix batch.

Each test reproduces a real bug from the audit and pins the fix so it
cannot silently regress. Bugs:

  P0#1 — comment / string-literal contract false positives
  P0#2 — `index --include` purged files outside the include scope
  P0#3 — stale state was silently trusted; now auto-refresh + warn
  P1#4 — DSL contract extraction (Prisma / SQL / TOML) was disabled
  P1#6 — `trace` over-promised on same-name collisions; now flagged
  P2#8 — default-export aliased imports produced zero refs
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store


def _run_cli(args):
    from projmem.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            rc = main(args)
        except SystemExit as e:
            # Commands like fact-check call sys.exit(2) to signal
            # non-zero status for CI gating. Capture the code so tests
            # see it as an rc instead of an exception.
            rc = int(e.code) if e.code is not None else 0
    return rc, buf.getvalue()


def _indexed(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    cfg = Config(root=str(root))
    store = Store(cfg.db_path)
    index_all(cfg, store)
    return cfg, store, root


# ---------------------------------------------------------------------------
# P0#1 — comment / string-literal masking on contract extraction
# ---------------------------------------------------------------------------

def test_p0_1_env_in_comment_is_not_extracted(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {
        "a.js": "// mode was previously process.env.MODE but we removed it\n"
    })
    rows = list(store.conn.execute(
        "SELECT name FROM contracts WHERE kind='env'"))
    store.close()
    assert rows == [], (
        f"comment-only env mention must NOT be a contract; got {rows}")


def test_p0_1_env_in_string_literal_is_not_extracted(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {
        "a.js": "const label = 'process.env.DEBUG is how we debug';\n"
    })
    rows = list(store.conn.execute(
        "SELECT name FROM contracts WHERE kind='env'"))
    store.close()
    assert rows == [], (
        f"string-literal env mention must NOT be a contract; got {rows}")


def test_p0_1_real_env_plus_comment_only_keeps_real(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {
        "a.js": (
            "const x = process.env.REAL_VAR;"
            " // also process.env.FAKE_VAR in the comment\n"
        )
    })
    names = {r["name"] for r in store.conn.execute(
        "SELECT name FROM contracts WHERE kind='env'")}
    store.close()
    assert "REAL_VAR" in names
    assert "FAKE_VAR" not in names, (
        f"FAKE_VAR was inside a comment; should not be extracted. names={names}")


def test_p0_1_block_comment_env_not_extracted(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {
        "a.js": (
            "/* legacy doc:\n"
            "   process.env.OLD_DOC was removed in 2024\n"
            " */\n"
            "const real = process.env.REAL_DOC;\n"
        )
    })
    names = {r["name"] for r in store.conn.execute(
        "SELECT name FROM contracts WHERE kind='env'")}
    store.close()
    assert "REAL_DOC" in names
    assert "OLD_DOC" not in names


def test_p0_1_string_form_extractors_still_see_strings(tmp_path):
    """getenv("X")-style still works: the extractor's regex requires
    the function-call boundary so masking comments alone is enough."""
    cfg, store, _ = _indexed(tmp_path, {
        "a.c": (
            "char *x = getenv(\"REAL_C_ENV\");\n"
            "// getenv(\"COMMENT_C_ENV\") was here once\n"
        )
    })
    names = {r["name"] for r in store.conn.execute(
        "SELECT name FROM contracts WHERE kind='env'")}
    store.close()
    assert "REAL_C_ENV" in names
    assert "COMMENT_C_ENV" not in names


# ---------------------------------------------------------------------------
# P0#2 — `index --include` foot-gun
# ---------------------------------------------------------------------------

def test_p0_2_narrow_include_does_not_purge_other_files(tmp_path):
    """A targeted `--include` reindex must NOT delete files outside
    its scope from the index. This was the original foot-gun: a
    50k-file index collapsed to one file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    for n in ("alpha.py", "beta.py", "gamma.py", "delta.py"):
        (repo / "src" / n).write_text(f"x = '{n}'\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    assert rc == 0
    # Targeted reindex of just one file.
    rc, _ = _run_cli(["--path", str(repo), "index",
                       "--include", "src/alpha.py", "--force"])
    assert rc == 0
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    files = sorted(r["path"] for r in store.all_files())
    store.close()
    # All four files must still be present.
    assert files == ["src/alpha.py", "src/beta.py",
                     "src/delta.py", "src/gamma.py"]


def test_p0_2_genuinely_deleted_file_within_scope_is_removed(tmp_path):
    """Inside the include scope, genuinely-deleted files SHOULD still
    be removed. The fix must not regress legitimate deletion."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "src" / "b.py").write_text("y = 2\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    # Delete b.py and run targeted reindex covering just src/**
    (repo / "src" / "b.py").unlink()
    rc, _ = _run_cli(["--path", str(repo), "index",
                       "--include", "src/**", "--force"])
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    files = {r["path"] for r in store.all_files()}
    store.close()
    assert "src/a.py" in files
    assert "src/b.py" not in files


# ---------------------------------------------------------------------------
# P0#3 — auto-refresh on read commands
# ---------------------------------------------------------------------------

def test_p0_3_reverse_auto_refreshes_drifted_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    fp = repo / "src" / "core.py"
    fp.write_text("x = 1\n")
    (repo / "src" / "uses.py").write_text("from core import x\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    fp.write_text("x = 2\nz = 3\n")
    rc, out = _run_cli(["--path", str(repo), "--json", "reverse", "src/core.py"])
    data = json.loads(out)
    # The fix returns auto_refreshed instead of (or in addition to) freshness_warning.
    assert data.get("auto_refreshed") == ["src/core.py"]


def test_p0_3_no_auto_refresh_falls_back_to_warning(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fp = repo / "a.py"
    fp.write_text("def go(): return 1\n")
    rc, _ = _run_cli(["--path", str(repo), "index"])
    fp.write_text("def go(): return 99\n")
    rc, out = _run_cli(["--path", str(repo), "--json", "reverse", "a.py",
                         "--no-auto-refresh"])
    data = json.loads(out)
    assert "freshness_warning" in data
    assert "auto_refreshed" not in data


# ---------------------------------------------------------------------------
# P1#4 — DSL contracts extracted (the previous P0 fix was too broad)
# ---------------------------------------------------------------------------

def test_p1_4_uppercase_token_in_string_still_extracted(tmp_path):
    """The token extractor must still capture UPPER_SNAKE values from
    string literals. Audit reproduced this with Prisma enum literals;
    ours uses the cross-language behavior — string-embedded UPPER_SNAKE
    becomes a token contract."""
    cfg, store, _ = _indexed(tmp_path, {
        "handler.ts": (
            "const status = 'WEBHOOK_FAILED';\n"
            "if (status === 'WEBHOOK_FAILED') retry();\n"
        )
    })
    rows = list(store.conn.execute(
        "SELECT name FROM contracts WHERE name='WEBHOOK_FAILED'"))
    store.close()
    assert rows, "WEBHOOK_FAILED string-literal token must be extracted"


# ---------------------------------------------------------------------------
# P1#6 — trace flagged as experimental
# ---------------------------------------------------------------------------

def test_p1_6_trace_output_carries_experimental_flag(tmp_path):
    cfg, store, root = _indexed(tmp_path, {
        "a.js": "function f() { g(); }\nfunction g() {}\n"
    })
    store.close()
    # `trace` now requires --experimental opt-in (the output looked
    # authoritative but was name-level; consumers were misled).
    rc, out = _run_cli(["--path", str(root), "--json", "trace",
                         "f", "g", "--experimental"])
    data = json.loads(out)
    assert data.get("experimental") is True
    assert "caveat" in data
    assert "name-level" in data["caveat"] or "collision" in data["caveat"]


def test_p1_6_trace_finds_one_hop_intra_file_call(tmp_path):
    """Audit reproducer: `trace assertSharedEnumsMatchDatabase assertStringEnum`
    on a Next.js+Prisma codebase returned no path even though the caller
    directly invoked the callee in the same file. Root cause was BFS
    direction inverted (callee→caller instead of caller→callee).

    This test pins the fix: a direct 1-hop intra-file call must surface as
    a 2-element path with edge_type='call' on the second hop.
    """
    cfg, store, root = _indexed(tmp_path, {
        "lib/enums.ts": (
            "export function assertStringEnum(v: string) { return v; }\n"
            "export function assertSharedEnumsMatchDatabase() {\n"
            "  assertStringEnum('FOO');\n"
            "}\n"
        )
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "trace",
                        "assertSharedEnumsMatchDatabase",
                        "assertStringEnum", "--experimental"])
    data = json.loads(out)
    path = data.get("path") or []
    assert len(path) == 2, (
        f"Expected 2-hop path caller→callee, got {data}")
    assert path[0]["symbol"] == "assertSharedEnumsMatchDatabase"
    assert path[0]["edge_type"] is None
    assert path[1]["symbol"] == "assertStringEnum"
    assert path[1]["edge_type"] == "call"


# ---------------------------------------------------------------------------
# P0#8 — cross-layer enum mismatch caught by complete checklist
# ---------------------------------------------------------------------------

def _enum_mismatch_in_findings(findings):
    return next((f for f in findings
                 if f["code"] == "cross_layer_enum_mismatch"), None)


def test_p0_8_ts_prisma_enum_mismatch_flagged(tmp_path):
    """A TS enum and a Prisma enum sharing a name but with different
    member sets must surface as a HIGH cross_layer_enum_mismatch finding.
    Audit reproducer: NotificationType in TS had INFO|WARN|ERROR while
    Prisma had only INFO|WARN — `complete` returned ok."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/types.ts": (
            "export enum NotificationType {\n"
            "  INFO = 'INFO',\n"
            "  WARN = 'WARN',\n"
            "  ERROR = 'ERROR',\n"
            "}\n"
        ),
        "prisma/schema.prisma": (
            "enum NotificationType {\n"
            "  INFO\n"
            "  WARN\n"
            "}\n"
        ),
    })
    from projmem import checklist as _ck
    res = _ck.run(cfg, store)
    store.close()
    finding = _enum_mismatch_in_findings(res["findings"])
    assert finding is not None, (
        f"expected cross_layer_enum_mismatch finding; got {res['findings']}")
    assert finding["severity"] == "high"
    mm = finding["details"]["mismatches"]
    assert any(m["name"] == "NotificationType" for m in mm)
    target = next(m for m in mm if m["name"] == "NotificationType")
    layers = {l["layer"] for l in target["layers"]}
    assert {"ts", "prisma"} <= layers
    diff_pair = target["pair_diffs"][0]
    # 'ERROR' is the missing member on the Prisma side.
    assert "ERROR" in diff_pair.get("only_in_ts", []) \
        or "ERROR" in diff_pair.get("only_in_prisma", [])


def test_p0_8_matching_enums_no_finding(tmp_path):
    """When TS and Prisma agree on the member set, no mismatch is emitted."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/types.ts": (
            "export enum Status { OPEN = 'OPEN', CLOSED = 'CLOSED' }\n"
        ),
        "prisma/schema.prisma": (
            "enum Status {\n  OPEN\n  CLOSED\n}\n"
        ),
    })
    from projmem import checklist as _ck
    res = _ck.run(cfg, store)
    store.close()
    assert _enum_mismatch_in_findings(res["findings"]) is None


def test_p0_8_single_layer_no_finding(tmp_path):
    """An enum that only exists in one layer (no Prisma counterpart) is
    not a cross-layer mismatch — it's just a normal type definition."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/types.ts": (
            "export enum LocalOnly { A = 'A', B = 'B' }\n"
        ),
    })
    from projmem import checklist as _ck
    res = _ck.run(cfg, store)
    store.close()
    assert _enum_mismatch_in_findings(res["findings"]) is None


def test_p0_8_sql_create_type_enum_participates(tmp_path):
    """SQL `CREATE TYPE x AS ENUM (...)` should also feed the cross-layer
    check. Useful for projects whose source-of-truth is a hand-written
    schema.sql rather than Prisma."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/types.ts": (
            "export enum Color { RED = 'RED', GREEN = 'GREEN' }\n"
        ),
        "db/schema.sql": (
            "CREATE TYPE Color AS ENUM ('RED', 'BLUE');\n"
        ),
    })
    from projmem import checklist as _ck
    res = _ck.run(cfg, store)
    store.close()
    finding = _enum_mismatch_in_findings(res["findings"])
    assert finding is not None
    target = next(m for m in finding["details"]["mismatches"]
                  if m["name"] == "Color")
    layers = {l["layer"] for l in target["layers"]}
    assert {"ts", "sql"} <= layers


# ---------------------------------------------------------------------------
# P0#11 — ref binding rate: barrel re-exports now resolve
# ---------------------------------------------------------------------------

def test_changes_command_detects_on_disk_drift(tmp_path):
    """A file whose on-disk hash diverges from the indexed hash must be
    reported as drifted_on_disk with hash-mismatch reason. Closes the
    'fresh-session agent fell back to filesystem timestamps' gap."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() { return 1; }\n",
        "src/b.ts": "export function beta() { return 2; }\n",
    })
    store.close()
    # Edit b.ts on disk without re-indexing.
    (root / "src" / "b.ts").write_text(
        "export function beta() { return 999; }\n"
        "export function newOne() { return 0; }\n")
    rc, out = _run_cli(["--path", str(root), "--json", "changes"])
    assert rc == 0
    data = json.loads(out)
    assert data["summary"]["drifted_on_disk"] >= 1
    drifted = data["drifted_on_disk_paths"]
    assert "src/b.ts" in drifted
    assert "drifted on disk" in data["hint"].lower()


def test_bench_v4_bug1_refute_note_id_resolves_target(tmp_path):
    """Benchmark v4 Bug 1: `refute add <note_id>` previously stored
    the integer as `target` string. Must now resolve to disputed
    note's actual file path via either positional bare-int OR
    explicit --note-id flag."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "conclude",
        "The @defined-at(foo, src/a.ts:1) helper.",
    ])
    assert rc == 0
    note_id = json.loads(out)["id"]
    # --note-id form: explicit, preferred.
    rc, out = _run_cli([
        "--path", str(root), "--json", "refute", "add",
        "--note-id", str(note_id), "I disagree",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["target"] == "src/a.ts", (
        f"refute target should be resolved to file path; got {data}")
    assert data.get("resolved_from_note") == note_id
    # Bare-int positional auto-resolves too.
    rc, out = _run_cli([
        "--path", str(root), "--json", "refute", "add",
        str(note_id), "Also disagree (positional form)",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["target"] == "src/a.ts"
    assert data.get("resolved_from_note") == note_id


def test_bench_v4_bug2_defined_at_refutes_orphan_target(tmp_path):
    """Benchmark v4 Bug 2: when the cited file no longer exists on
    disk, defined-at must REFUTE (not VERIFY using stale symbols-table
    evidence). Orphan claims must surface as wrong."""
    cfg, store, root = _indexed(tmp_path, {
        "src/orphan.ts": "export const x = 1;\n",
    })
    store.close()
    # Save a claim while the file exists.
    rc, out = _run_cli([
        "--path", str(root), "--json", "conclude",
        "The @defined-at(x, src/orphan.ts:1) constant exists.",
    ])
    assert rc == 0
    # Delete the file (do NOT re-index — we want stale symbols).
    (root / "src" / "orphan.ts").unlink()
    rc, out = _run_cli([
        "--path", str(root), "--json", "note-verify", "src/orphan.ts",
    ])
    # Round-5 F008: contradicted_count > 0 → exit 1. Refuted FACT here
    # produces a contradicted note, so rc is 1, not 0.
    assert rc == 1, f"contradicted FACT must propagate to exit 1; got {rc}"
    data = json.loads(out)
    # Find the claim and assert REFUTED.
    found = False
    for r in data["results"]:
        for c in r.get("claims") or []:
            if c.get("subject") == "x":
                assert c["status"] == "REFUTED", (
                    f"orphan target must REFUTE not VERIFY; got {c}")
                assert "no longer exists" in (c.get("reason") or "").lower()
                found = True
    assert found, f"expected to find the x claim in results: {data}"


# ---------------------------------------------------------------------------
# Bench iter 4 — contradicted_count_mismatch: projmem notes lacked repo_memory
# ---------------------------------------------------------------------------

def test_bench4_notes_envelope_includes_repo_memory(tmp_path):
    """Bug bench iter 4: cmd_notes called _emit() not _emit_with_memory(),
    so projmem notes output had no repo_memory.contradicted_count while
    task resume / note-verify included it — envelope inconsistency."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "notes"])
    assert rc == 0
    data = json.loads(out)
    # repo_memory must be present in the notes envelope, just like all
    # other read commands.
    assert "repo_memory" in data, (
        "projmem notes must include repo_memory header; bench iter 4 fix")
    rm = data["repo_memory"]
    assert "contradicted_count" in rm, (
        "repo_memory must contain contradicted_count; bench iter 4 fix")
    assert "total_notes" in rm, (
        "repo_memory must contain total_notes; bench iter 4 fix")


def test_bench_v4_bug3_task_event_count_is_true_total(tmp_path):
    """Benchmark v4 Bug 3: task resume previously read LIMIT 25 from
    task_events and reported event_count from that bounded read.
    Must now report TRUE total via separate COUNT query, plus
    events_returned + events_truncated for transparency."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    _run_cli(["--path", str(root), "--json", "task", "start", "long task"])
    # Add 30 steps (more than the legacy LIMIT 25).
    for i in range(30):
        _run_cli([
            "--path", str(root), "--json", "task", "step", f"step {i}",
        ])
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    task = next((t for t in data["open_tasks"]
                  if t["goal"] == "long task"), None)
    assert task is not None
    # Open task: 30 steps + 1 status event = 31 events at minimum.
    assert task["event_count"] >= 30, (
        f"true event_count must reflect all events; got {task['event_count']}")


def test_bench_v4_bug4_root_mismatch_warning_in_header(tmp_path):
    """Benchmark v4 Bug 4: when CWD diverges from the indexed_root
    (operator running projmem from the wrong directory), the
    repo_memory header must surface a warning so the operator
    notices instead of silently querying the wrong project."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    # Run from a CWD outside the indexed root, point --path at the
    # real repo. The header should still flag the mismatch because
    # process CWD is what build_header inspects.
    import os as _os_test
    saved = _os_test.getcwd()
    try:
        _os_test.chdir(str(tmp_path.parent))  # outside the indexed tree
        rc, out = _run_cli([
            "--path", str(root), "--json", "task", "resume",
        ])
        assert rc == 0
        data = json.loads(out)
        rm = data.get("repo_memory") or {}
        assert "indexed_root" in rm
        assert "root_mismatch_warning" in rm, (
            f"expected root_mismatch_warning when CWD outside "
            f"indexed_root; got header={rm}")
    finally:
        _os_test.chdir(saved)


def test_bench_v3_bug1_notes_saved_scoped_to_task_window(tmp_path):
    """Benchmark v3 Bug 1: closed tasks showed every note EVER SAVED
    since their creation, including notes saved AFTER they closed.
    Must scope to [created_at, closed_at] only for closed tasks."""
    import time
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    # Start + close task A.
    _run_cli(["--path", str(root), "--json", "task", "start", "task A"])
    _run_cli(["--path", str(root), "--json", "task", "step", "did A step"])
    _run_cli(["--path", str(root), "--json", "task", "close",
               "--detail", "A done"])
    time.sleep(0.05)  # ensure note created_at > task A's closed_at
    # Save a note AFTER task A closed.
    _run_cli(["--path", str(root), "note", "add", "src/a.ts",
               "post-A note", "--kind", "note", "--author", "t"])
    # task resume should NOT show this note under task A's notes_saved.
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    task_a = next((t for t in data["recently_closed"]
                    if t["goal"] == "task A"), None)
    assert task_a is not None
    notes = task_a.get("notes_saved") or []
    post_notes = [n for n in notes
                  if "post-A" in (n.get("body") or "")]
    # The post-A note was created AFTER task A closed — it must NOT
    # appear in task A's notes_saved scope.
    assert not post_notes, (
        f"task A's notes_saved must not include post-close notes; "
        f"got {notes}")


def test_bench_iter2_notes_saved_excludes_pre_step_notes(tmp_path):
    """Benchmark iter-2 HIGH bug: notes created AFTER task creation but
    BEFORE the first task step were incorrectly included in notes_saved.
    The scoping window lower-bound must be the first step ts, not created_at."""
    import time
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
        "src/b.ts": "export function bar() {}\n",
    })
    store.close()
    # Start a task, then immediately save a note BEFORE any step.
    _run_cli(["--path", str(root), "--json", "task", "start", "iter2 task"])
    time.sleep(0.05)  # note created_at will be > task.created_at
    _run_cli(["--path", str(root), "note", "add", "src/a.ts",
               "pre-step note", "--kind", "note", "--author", "t"])
    time.sleep(0.05)  # ensure step ts > note created_at
    # Now record a step.
    _run_cli(["--path", str(root), "--json", "task", "step", "step one"])
    time.sleep(0.05)
    # Save a post-step note — this SHOULD appear.
    _run_cli(["--path", str(root), "note", "add", "src/b.ts",
               "post-step note", "--kind", "note", "--author", "t"])
    # Check open task resume.
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    task = next((t for t in data["open_tasks"]
                 if t["goal"] == "iter2 task"), None)
    assert task is not None, "iter2 task must be in open_tasks"
    notes = task.get("notes_saved") or []
    targets = [n.get("target", "") for n in notes]
    # pre-step note targets src/a.ts; post-step note targets src/b.ts
    assert "src/a.ts" not in targets, (
        f"pre-step note (src/a.ts) must NOT appear in notes_saved; "
        f"got targets={targets}")
    assert "src/b.ts" in targets, (
        f"post-step note (src/b.ts) MUST appear in notes_saved; "
        f"got targets={targets}")


def test_bench_iter2_notes_saved_closed_excludes_pre_step_notes(tmp_path):
    """Benchmark iter-2 HIGH bug (closed task variant): same pre-step
    scoping fix must also apply to recently_closed tasks."""
    import time
    cfg, store, root = _indexed(tmp_path, {
        "src/c.ts": "export function baz() {}\n",
    })
    store.close()
    _run_cli(["--path", str(root), "--json", "task", "start", "iter2 closed task"])
    time.sleep(0.05)
    # Note saved before first step.
    _run_cli(["--path", str(root), "note", "add", "src/c.ts",
               "pre-step-closed note", "--kind", "note", "--author", "t"])
    time.sleep(0.05)
    _run_cli(["--path", str(root), "--json", "task", "step", "only step"])
    time.sleep(0.05)
    _run_cli(["--path", str(root), "--json", "task", "close", "--detail", "done"])
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    task = next((t for t in data["recently_closed"]
                 if t["goal"] == "iter2 closed task"), None)
    assert task is not None, "iter2 closed task must be in recently_closed"
    notes = task.get("notes_saved") or []
    targets = [n.get("target", "") for n in notes]
    # pre-step note targets src/c.ts; if it appears the bug is not fixed
    assert "src/c.ts" not in targets, (
        f"pre-step note (src/c.ts) must NOT appear in closed task "
        f"notes_saved; got targets={targets}")


def test_bench_v3_bug2_complete_findings_is_list_when_empty(tmp_path):
    """Benchmark v3 Bug 2: `jq '.findings[]'` failed because complete
    returned null/missing top-level findings. Must always be a list
    (possibly empty) AND at the top level (not only inside checklist)."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "complete"])
    assert rc == 0
    data = json.loads(out)
    assert "findings" in data, "top-level `findings` key must exist"
    assert isinstance(data["findings"], list), (
        f"findings must be a list, got {type(data['findings']).__name__}")
    assert "overall" in data
    assert "severity_counts" in data


def test_bench_v3_bug3_factcheck_suffix_matches_bare_filename(tmp_path):
    """Benchmark v3 Bug 3: fact-check silently REFUTED bare filenames
    as 'file missing'. Must either VERIFY via suffix match (if unique)
    or return UNCHECKABLE with candidates."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
        "src/nested/unique.ts": "export const X = 1;\n",
    })
    store.close()
    # Bare `unique.ts:1` → only one indexed match → VERIFIED.
    rc, out = _run_cli([
        "--path", str(root), "--json", "fact-check",
        "See unique.ts:1 for context.",
    ])
    assert rc == 0, f"unique suffix-match should VERIFY; got exit {rc}"
    data = json.loads(out)
    checks = data.get("bare_file_line_checks") or []
    hit = next((c for c in checks if c["path"] == "unique.ts"), None)
    assert hit is not None
    assert hit["status"] == "VERIFIED"
    evidence = hit.get("evidence") or {}
    assert evidence.get("resolved_from_suffix") is True


def test_bench_v3_bug4_note_list_limit_and_fields(tmp_path):
    """Benchmark v3 Bug 4: note list had no --limit or --fields.
    Verify both now scope the output."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    for i in range(5):
        _run_cli(["--path", str(root), "note", "add", "src/a.ts",
                   f"note {i}", "--kind", "note", "--author", "t"])
    rc, out = _run_cli([
        "--path", str(root), "--json", "note", "list",
        "--limit", "2", "--fields", "id,body",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["count"] == 2
    assert data["total"] >= 5
    assert data.get("truncated") is True
    # Each row should have ONLY id + body.
    for n in data["annotations"]:
        assert set(n.keys()) <= {"id", "body"}, (
            f"--fields should restrict keys; got {n.keys()}")


def test_bench_v3_bug5_ask_detects_placeholder(tmp_path):
    """Benchmark v3 Bug 5: ask with literal `<name>` placeholder
    returned 'Couldn't identify a subject' with no hint. Must now
    detect the placeholder and suggest examples."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "ask",
        "safe to delete <name>?",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["shape"] == "placeholder_detected", (
        f"expected placeholder shape; got {data['shape']}")
    assert "placeholder" in data["summary"].lower()
    assert data.get("hints"), "must emit hints with examples"


def test_bench_v3_bug6_explain_emits_runtime_limits_hint(tmp_path):
    """Benchmark v3 Bug 6: 'what is the TCP timeout?' matched the
    explain shape, returned a weak 'No def for TCP' with no admission
    that projmem can't answer runtime questions. Must emit hints
    explaining the structural-vs-runtime limit."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "ask",
        "what is the TCP connect timeout for the HTTP client?",
    ])
    assert rc == 0
    data = json.loads(out)
    hints = data.get("hints") or []
    assert hints, "unresolved explain should emit hints"
    combined = " ".join(hints).lower()
    assert "runtime" in combined or "execute" in combined, (
        f"hints should mention runtime / execution limits; got {hints}")


def test_bench_v2_bug1_exported_from_enum_member_verifies(tmp_path):
    """Benchmark v2 Bug 1: `@exported-from(TASK_ASSIGNED, notification.ts)`
    should VERIFY — the name is a legitimate enum member, not a
    top-level symbol. Previously REFUTED silently."""
    cfg, store, root = _indexed(tmp_path, {
        "src/shared/enums/notification.ts": (
            "export const NotificationTypes = [\n"
            "  'TASK_ASSIGNED',\n"
            "  'INCIDENT_CREATED',\n"
            "] as const;\n"
        ),
    })
    store.close()
    claims_file = tmp_path / "c.json"
    claims_file.write_text(json.dumps([{
        "subject": "TASK_ASSIGNED",
        "predicate": "exported-from",
        "object": "src/shared/enums/notification.ts",
        "truth_class": "FACT",
    }]))
    rc, _ = _run_cli([
        "--path", str(root), "note", "add",
        "src/shared/enums/notification.ts", "enum-member claim",
        "--kind", "note", "--truth-class", "FACT", "--author", "t",
        "--claims", str(claims_file),
    ])
    assert rc == 0
    rc, out = _run_cli([
        "--path", str(root), "--json", "note-verify",
        "src/shared/enums/notification.ts",
    ])
    assert rc == 0
    data = json.loads(out)
    # Find the claim we just saved and assert it VERIFIED.
    all_claims = []
    for r in data["results"]:
        all_claims.extend(r.get("claims") or [])
    target = next((c for c in all_claims
                    if c["subject"] == "TASK_ASSIGNED"
                    and c["predicate"] == "exported-from"), None)
    assert target is not None, (
        f"expected TASK_ASSIGNED claim; got {all_claims}")
    assert target["status"] == "VERIFIED", (
        f"enum-member exported-from should VERIFY; got {target}")
    evidence = target.get("current_evidence") or []
    assert evidence and evidence[0].get("as_member") is True


def test_bench_v2_bug2_conclude_pre_verify_aborts_on_refuted(tmp_path):
    """Benchmark v2 Bug 2: conclude used to persist notes that were
    contradicted-on-creation. Must now pre-verify and abort with
    exit code 3 on any REFUTED FACT claim.

    Scope: a claim citing a symbol that doesn't exist anywhere → REFUTED.
    (A claim citing the right symbol at the wrong line produces MOVED,
    not REFUTED, since an above-line edit isn't a semantic refutation.)"""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    # Inline claim with a symbol that doesn't exist → true REFUTED.
    rc, out = _run_cli([
        "--path", str(root), "--json", "conclude",
        "The @defined-at(definitely_not_here, src/a.ts:1) planted.",
    ])
    assert rc == 3, f"expected exit 3 on refuted; got {rc}"
    data = json.loads(out)
    assert data.get("error") == "refuted-before-save"
    assert data.get("refuted_claims")
    # No note should have been persisted.
    rc, out2 = _run_cli([
        "--path", str(root), "--json", "note", "list",
    ])
    assert rc == 0
    d2 = json.loads(out2)
    # The planted claim's body should not appear.
    assert not any("planted" in (n.get("body") or "")
                    for n in d2["annotations"]), (
        "conclude must NOT persist when pre-verify fails")


def test_bench_v2_bug3_close_summary_preserves_file_line(tmp_path):
    """Benchmark v2 Bug 3: close_summary truncated `foo.ts:5` to
    `foo.ts:` at 80-char slice. Must now preserve the full file:line
    token by breaking on whitespace."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    _run_cli(["--path", str(root), "--json", "task", "start", "probe"])
    _run_cli([
        "--path", str(root), "--json", "task", "step",
        "verified FOO_CONST enum at src/shared/enums/foo.ts:3 fires as "
        "FooKey consumed at src/worker/handlers/fooHandler.ts:88",
    ])
    rc, out = _run_cli(["--path", str(root), "--json", "task", "close"])
    assert rc == 0
    data = json.loads(out)
    cs = data.get("close_summary") or ""
    # The file:line tokens must appear intact, not severed at the colon.
    assert "src/shared/enums/foo.ts:3" in cs, (
        f"file:line severed; got summary: {cs!r}")


def test_bench_v2_bug4_conclude_auto_populates_author(tmp_path):
    """Benchmark v2 Bug 4: when --author is omitted, conclude should
    auto-populate from $USER + timestamp, not leave it null."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "conclude",
        "The @defined-at(foo, src/a.ts:1) helper is "
        "@exported-from(foo, src/a.ts).",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data.get("author"), f"auto-author must be non-null; got {data}"
    assert data["author"].startswith("session-"), (
        f"auto-author should start with 'session-'; got {data['author']!r}")


def test_bench_v2_bug6_ask_commands_run_has_no_empty(tmp_path):
    """Benchmark v2 Bug 6: ask emitted '' in commands_run on edge
    cases. Never should appear."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    # Various question shapes that used to emit empty strings.
    for q in [
        "what is the timeout?",
        "explain foo",
        "what does bar do?",
    ]:
        rc, out = _run_cli([
            "--path", str(root), "--json", "ask", q,
        ])
        assert rc == 0
        data = json.loads(out)
        cmds = data.get("commands_run") or []
        assert all(c and c.strip() for c in cmds), (
            f"commands_run contained empty on q={q!r}: {cmds}")


def test_bench_v2_bug7_note_list_accepts_target_and_author_flags(tmp_path):
    """Benchmark v2 Bug 7: note list --target (flag) and --author
    should both work for API symmetry with note-verify."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    # Add two notes with different authors.
    _run_cli([
        "--path", str(root), "note", "add", "src/a.ts", "note one",
        "--kind", "note", "--author", "alice",
    ])
    _run_cli([
        "--path", str(root), "note", "add", "src/a.ts", "note two",
        "--kind", "note", "--author", "bob",
    ])
    # Filter by --author.
    rc, out = _run_cli([
        "--path", str(root), "--json", "note", "list",
        "--author", "alice",
    ])
    assert rc == 0
    data = json.loads(out)
    authors = {n["author"] for n in data["annotations"]}
    assert authors == {"alice"}, f"author filter failed: {authors}"
    # Filter by --target flag form.
    rc, out = _run_cli([
        "--path", str(root), "--json", "note", "list",
        "--target", "src/a.ts",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["count"] >= 2


def test_bench_task_resume_shows_event_arc_on_closed(tmp_path):
    """Benchmark R1: closed tasks were 'black box post-close'. Verify
    that resume now returns full event arc (steps + close_summary)."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    _run_cli(["--path", str(root), "--json", "task", "start", "audit X"])
    _run_cli(["--path", str(root), "--json", "task", "step",
               "confirmed step one"])
    _run_cli(["--path", str(root), "--json", "task", "step",
               "confirmed step two"])
    _run_cli(["--path", str(root), "--json", "task", "close",
               "--detail", "X audit passed"])
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    rc_list = data.get("recently_closed") or []
    assert rc_list, "closed task must surface in recently_closed"
    closed = rc_list[0]
    assert closed["close_summary"] == "X audit passed"
    steps = closed.get("steps") or []
    step_details = {s["detail"] for s in steps}
    assert "confirmed step one" in step_details
    assert "confirmed step two" in step_details


def test_bench_task_close_auto_synthesizes_when_no_detail(tmp_path):
    """Benchmark R1 root cause: agents forget --detail. When omitted,
    close must auto-synthesize a summary from last steps."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    _run_cli(["--path", str(root), "--json", "task", "start", "refactor foo"])
    _run_cli(["--path", str(root), "--json", "task", "step",
               "extracted helper"])
    rc, out = _run_cli(["--path", str(root), "--json", "task", "close"])
    assert rc == 0
    data = json.loads(out)
    # close_summary must be present even without --detail
    assert data.get("close_summary"), (
        f"close must auto-synthesize summary; got {data}")
    assert "extracted helper" in data["close_summary"] or \
           "auto-synthesized" in data["close_summary"]
    assert data.get("hint"), "hint nudging --detail should be emitted"


def test_bench_ask_fallback_on_string_literal(tmp_path):
    """Benchmark R5: ask on a string literal (kebab-case header name)
    returned empty. Must now fall back to contracts/notes search."""
    cfg, store, root = _indexed(tmp_path, {
        "src/h.ts": (
            "export const HEADER = 'x-my-signature';\n"
            "export function send() { return HEADER; }\n"
        ),
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "ask",
                        "where is the x-my-signature header set?"])
    assert rc == 0
    data = json.loads(out)
    hits = (data.get("detail") or {}).get("hits") or []
    # Either it found literal hits OR it emitted a hint pointing at the
    # right command. Both are acceptable; empty-and-silent is not.
    assert hits or data.get("hints"), (
        f"ask must fall back to literal search or emit a hint; got {data}")


def test_bench_contradicted_count_agrees_across_surfaces(tmp_path):
    """Benchmark R3: repo_memory.contradicted_count and notes summary's
    by_staleness['contradicted'] must agree after a `notes` call."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    # Save a note with a REFUTED claim — defined-at at a line that
    # doesn't exist.
    claims_file = tmp_path / "c.json"
    claims_file.write_text(json.dumps([{
        "subject": "foo", "predicate": "defined-at",
        "object": "src/a.ts:999",   # beyond EOF → REFUTED
        "truth_class": "FACT",
    }]))
    rc, _ = _run_cli([
        "--path", str(root), "note", "add", "src/a.ts",
        "planted note", "--kind", "note", "--truth-class", "FACT",
        "--claims", str(claims_file),
    ])
    assert rc == 0
    # Running `notes` triggers the revalidate+persist sweep.
    rc, out = _run_cli(["--path", str(root), "--json", "notes"])
    assert rc == 0
    notes_data = json.loads(out)
    by_stale_contradicted = (notes_data["totals"]["by_staleness"]
                              .get("contradicted", 0))
    # Now check a command whose output attaches repo_memory.
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    header = json.loads(out).get("repo_memory") or {}
    header_contradicted = header.get("contradicted_count", -1)
    assert by_stale_contradicted == header_contradicted, (
        f"contradicted_count must agree across surfaces: "
        f"by_staleness={by_stale_contradicted}, "
        f"header={header_contradicted}")


def test_tier0_factcheck_catches_refuted_nl_claim(tmp_path):
    """`projmem fact-check` must REFUTE a defined-at claim whose line is
    beyond EOF, and VERIFY one that resolves."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function realFn() { return 1; }\n",
    })
    store.close()
    # `realFn` on line 1 is truthful; `ghostFn` at :555 is a lie.
    text = ("`realFn` is defined at src/a.ts:1. "
            "`ghostFn` is defined at src/a.ts:555.")
    rc, out = _run_cli(["--path", str(root), "--json", "fact-check", text])
    # Exit code 2 expected when any claim is REFUTED.
    assert rc == 2, f"expected exit code 2 on refuted claim, got {rc}"
    data = json.loads(out)
    assert data["verdict"] == "has_refuted"
    # Verify the specific REFUTED entry.
    refuted = [c for c in data["claims"] if c.get("status") == "REFUTED"]
    assert any(c["subject"] == "ghostFn" for c in refuted), (
        f"ghostFn should be REFUTED; got {refuted}")


def test_tier0_factcheck_inline_predicate_text(tmp_path):
    """Text with `@predicate(subject, object)` inline syntax is also
    covered by fact-check."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function realFn() {}\n",
    })
    store.close()
    text = "Investigated: @defined-at(realFn, src/a.ts:1) is the target."
    rc, out = _run_cli(["--path", str(root), "--json", "fact-check", text])
    assert rc == 0
    data = json.loads(out)
    assert data["verdict"] == "all_verified"
    assert data["verified"] >= 1


def test_factcheck_summary_exposes_moved_count(tmp_path):
    """Round-4 #1: a MOVED claim must be counted under `moved`, not
    silently absent. The `verdict` becomes `has_moved` so a CI gate
    that wants strict line-number accuracy can detect the drift even
    when nothing is REFUTED."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.py": "\n\n\n\ndef foo():\n    return 1\n",
    })
    store.close()
    text = "@defined-at(foo, src/a.py:1)"  # foo actually at line 5
    rc, out = _run_cli(["--path", str(root), "--json", "fact-check", text])
    data = json.loads(out)
    assert data["extracted_count"] == 1
    assert data["moved"] == 1
    assert data["refuted"] == 0
    assert data["verdict"] == "has_moved"
    # MOVED is a soft warning per CLAUDE.md — exit 0, NOT 2.
    assert rc == 0


def test_factcheck_surfaces_parse_errors_for_malformed_claims(tmp_path):
    """Round-4 #2: a botched `@defined-at(...` (no closing paren or no
    comma) must NOT silently extract zero claims. parse_errors lists
    the attempted-but-rejected predicate openings; verdict becomes
    `parse_errors` so callers know there was an attempt."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.py": "def foo():\n    return 1\n",
    })
    store.close()
    text = "@defined-at(broken syntax no paren"
    rc, out = _run_cli(["--path", str(root), "--json", "fact-check", text])
    data = json.loads(out)
    assert data["extracted_count"] == 0
    assert data["verdict"] == "parse_errors"
    pe = data.get("parse_errors") or []
    assert pe and pe[0]["predicate"] == "defined-at"
    assert "closing" in pe[0]["reason"]
    # Comma-missing case.
    text2 = "@defined-at(no_comma_here)"
    rc2, out2 = _run_cli(
        ["--path", str(root), "--json", "fact-check", text2])
    data2 = json.loads(out2)
    pe2 = data2.get("parse_errors") or []
    assert pe2 and "comma" in pe2[0]["reason"]


def test_tier0_factcheck_bare_file_line_beyond_eof(tmp_path):
    """A bare `path:N` citation with N beyond EOF is REFUTED under the
    implicit 'location exists' check."""
    cfg, store, root = _indexed(tmp_path, {
        "src/short.py": "x = 1\n",
    })
    store.close()
    text = "The check at src/short.py:99 is the bug."
    rc, out = _run_cli(["--path", str(root), "--json", "fact-check", text])
    assert rc == 2
    data = json.loads(out)
    assert data["verdict"] == "has_refuted"
    assert any(c["path"] == "src/short.py" and c["status"] == "REFUTED"
                for c in data.get("bare_file_line_checks", []))


def test_tier1A_conclude_session_saves_verified_only(tmp_path):
    """conclude-session must persist VERIFIED claims as notes and
    SKIP anything REFUTED or UNCHECKABLE."""
    cfg, store, root = _indexed(tmp_path, {
        "src/auth.ts": "export function realAuth() {}\n",
    })
    store.close()
    transcript_path = tmp_path / "t.txt"
    transcript_path.write_text(
        "Agent: I investigated.\n"
        "`realAuth` is defined at src/auth.ts:1.\n"
        "`hallucinatedFn` is defined at src/auth.ts:999.\n")
    rc, out = _run_cli([
        "--path", str(root), "--json", "conclude-session",
        "--transcript", str(transcript_path),
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["created_count"] >= 1
    assert data["dropped_refuted"] >= 1, (
        f"hallucinated claim must be dropped; got {data}")


def test_tier1A_conclude_session_idempotent(tmp_path):
    """Running conclude-session twice on the same transcript must not
    duplicate notes."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function realFn() {}\n",
    })
    store.close()
    transcript = tmp_path / "t.txt"
    transcript.write_text("`realFn` is defined at src/a.ts:1.\n")
    rc, out1 = _run_cli([
        "--path", str(root), "--json", "conclude-session",
        "--transcript", str(transcript),
    ])
    assert rc == 0
    d1 = json.loads(out1)
    rc, out2 = _run_cli([
        "--path", str(root), "--json", "conclude-session",
        "--transcript", str(transcript),
    ])
    assert rc == 0
    d2 = json.loads(out2)
    assert d2["created_count"] == 0
    assert len(d2.get("skipped_duplicate") or []) >= 1


def test_tier1B_task_start_step_resume_close_roundtrip(tmp_path):
    """Full task lifecycle: start → step → resume shows it → close."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "task", "start",
                         "refactor foo()"])
    assert rc == 0
    t = json.loads(out)
    assert t["status"] in ("active", "reopened")
    task_id = t["id"]
    rc, _ = _run_cli(["--path", str(root), "--json", "task", "step",
                       "removed legacy branch"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    open_goals = {t["goal"] for t in data["open_tasks"]}
    assert "refactor foo()" in open_goals
    # The task should carry an event_count >= 2 (status + step).
    entry = next(t for t in data["open_tasks"]
                  if t["goal"] == "refactor foo()")
    assert entry["event_count"] >= 2
    rc, out = _run_cli(["--path", str(root), "--json", "task", "close"])
    assert rc == 0
    closed = json.loads(out)
    assert closed["status"] == "done"


def test_tier1B_task_blocked_survives_and_resumes(tmp_path):
    """Blocked tasks must appear first in `resume` output."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    _run_cli(["--path", str(root), "--json", "task", "start", "taskA"])
    _run_cli(["--path", str(root), "--json", "task", "start", "taskB"])
    _run_cli(["--path", str(root), "--json", "task", "blocked",
               "need input on taskB approach"])
    rc, out = _run_cli(["--path", str(root), "--json", "task", "resume"])
    assert rc == 0
    data = json.loads(out)
    goals = [t["goal"] for t in data["open_tasks"]]
    # Blocked task should come first.
    assert goals[0] == "taskB"
    assert data["open_tasks"][0]["status"] == "blocked"
    assert data["open_tasks"][0]["blockers"]


def test_tier1B_task_start_is_idempotent(tmp_path):
    """Starting the same goal twice should return the existing task id."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    store.close()
    rc, out1 = _run_cli(["--path", str(root), "--json", "task", "start",
                          "same goal"])
    id1 = json.loads(out1)["id"]
    rc, out2 = _run_cli(["--path", str(root), "--json", "task", "start",
                          "same goal"])
    id2 = json.loads(out2)["id"]
    assert id1 == id2, "identical goal should not create a duplicate task"


def test_phaseA_inline_claim_parser(tmp_path):
    """@<predicate>(<subject>, <object>) patterns in a note body must
    be parsed into structured claims. Lowers capture friction from
    'write JSON + pass --claims' to one line of prose."""
    from projmem.claims import parse_inline_claims
    body = ("The @defined-at(withContext, src/server/http/withContext.ts:10) "
            "gateway is @exported-from(withContext, src/server/http/withContext.ts). "
            "It reads @env-read-at(AUTH_SECRET, src/config/env.ts:4).")
    claims = parse_inline_claims(body, default_truth_class="FACT")
    triples = {(c.subject, c.predicate, c.object) for c in claims}
    assert ("withContext", "defined-at",
            "src/server/http/withContext.ts:10") in triples
    assert ("withContext", "exported-from",
            "src/server/http/withContext.ts") in triples
    assert ("AUTH_SECRET", "env-read-at",
            "src/config/env.ts:4") in triples
    assert all(c.truth_class == "FACT" for c in claims)


def test_phaseA_inline_claims_merge_with_cli_claims(tmp_path):
    """Inline body claims merge with --claims (don't replace). Both
    paths end up in the evidence list."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    claims_json = tmp_path / "c.json"
    claims_json.write_text(json.dumps([
        {"subject": "foo", "predicate": "defined-at",
         "object": "src/a.ts:1", "truth_class": "FACT"}
    ]))
    rc, out = _run_cli([
        "--path", str(root), "--json", "note", "add", "src/a.ts",
        "Body with inline @exported-from(foo, src/a.ts) claim.",
        "--kind", "note", "--truth-class", "FACT",
        "--claims", str(claims_json),
    ])
    assert rc == 0
    data = json.loads(out)
    # 1 claim from --claims + 1 claim from inline = 2 total claims.
    assert data["claim_count"] >= 2, (
        f"inline + --claims must merge; got {data}")


def test_phaseA2_conclude_infers_target_from_body(tmp_path):
    """`projmem conclude` with a cited file path uses that path as the
    target so the agent doesn't have to supply --target manually."""
    cfg, store, root = _indexed(tmp_path, {
        "src/gateway.ts": "export function gate() {}\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "conclude",
        "The @defined-at(gate, src/gateway.ts:1) function is the gateway.",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["target"] == "src/gateway.ts", (
        f"target should be inferred from cited path; got {data}")
    assert data["claim_count"] >= 1
    assert data["truth_class"] == "FACT"


def test_phaseB_ask_dispatches_who_uses_to_reverse(tmp_path):
    """`projmem ask 'who uses <file>'` routes to reverse-dep logic and
    synthesizes a one-line answer."""
    cfg, store, root = _indexed(tmp_path, {
        "src/leaf.ts": "export function leaf() {}\n",
        "src/a.ts": "import { leaf } from './leaf';\nexport function a() { return leaf(); }\n",
        "src/b.ts": "import { leaf } from './leaf';\nexport function b() { return leaf(); }\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "ask", "who uses src/leaf.ts?",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["shape"] == "who_uses"
    assert data["subject"] == "src/leaf.ts"
    # reverse_deps should include at least one of the 2 importing files
    summary = data["summary"].lower()
    assert "reverse dep" in summary or "reverse_dep" in summary


def test_phaseB_ask_dispatches_what_changed_to_changes(tmp_path):
    """`projmem ask 'what changed'` routes to the edit log."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() {}\n",
    })
    store.close()
    (root / "src" / "a.ts").write_text(
        "export function alpha() {}\nexport function beta() {}\n")
    rc, _ = _run_cli(["--path", str(root), "index"])
    rc, out = _run_cli(["--path", str(root), "--json", "ask",
                        "what changed since last session"])
    assert rc == 0
    data = json.loads(out)
    assert data["shape"] == "changes"
    # summary should carry the change count
    assert "change" in data["summary"].lower() or "edit" in data["summary"].lower()


def test_phaseB_ask_unclassified_falls_back(tmp_path):
    """Unrecognized question → shape='unclassified' but still returns
    a useful response (pack . or explain on inferred subject)."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() {}\n",
    })
    store.close()
    rc, out = _run_cli([
        "--path", str(root), "--json", "ask", "tell me gibberish",
    ])
    assert rc == 0
    data = json.loads(out)
    # Shape must be present; summary must not be empty.
    assert data.get("shape") is not None
    assert data.get("summary")


def test_phaseC_index_attaches_nl_delta_summary(tmp_path):
    """After editing a file and re-indexing, the edit-log row must
    carry a natural-language `summary` describing the delta (e.g.
    'added beta')."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() {}\n",
    })
    store.close()
    (root / "src" / "a.ts").write_text(
        "export function alpha() {}\n"
        "export function beta() {}\n")
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(root), "--json", "changes"])
    assert rc == 0
    data = json.loads(out)
    files = {f["file"]: f for f in data["changed_files"]}
    assert "src/a.ts" in files
    summary = files["src/a.ts"].get("summary") or ""
    assert "beta" in summary, (
        f"summary should mention the added symbol; got {summary!r}")


def test_phaseD_seed_populates_hot_and_enum_notes(tmp_path):
    """`projmem seed` on a fresh repo must create INFERENCE notes on
    hot files and cross-layer enums without needing the agent to save
    anything first."""
    cfg, store, root = _indexed(tmp_path, {
        # A barrel + leaf → makes leaf a hot file.
        "src/barrel.ts": "export * from './leaf';\n",
        "src/a.ts":     "import { leaf } from './barrel'; export function a() { return leaf(); }\n",
        "src/b.ts":     "import { leaf } from './barrel'; export function b() { return leaf(); }\n",
        "src/leaf.ts":  "export function leaf() {}\n",
        # Cross-layer enum: TS as-const + Prisma enum with same name.
        "src/shared/status.ts": (
            "export const StatusTypes = ['OPEN','CLOSED'] as const;\n"
        ),
        "prisma/schema.prisma": "enum StatusType {\n OPEN\n CLOSED\n}\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "seed"])
    assert rc == 0
    data = json.loads(out)
    created = data.get("created") or []
    assert created, f"seed must create at least one note; got {data}"
    # A cross-layer-enum note should appear for StatusType.
    assert any(c["kind"] == "cross_layer_enum"
                and "StatusType" in c["target"]
                for c in created), (
        f"StatusType should be seeded; got {[c['target'] for c in created]}")
    # Every seed note must be author=projmem-seed, truth_class=INFERENCE.
    from projmem.store import Store as _Store
    s2 = _Store(cfg.db_path)
    seed_rows = list(s2.conn.execute(
        "SELECT author, truth_class FROM annotations "
        "WHERE author='projmem-seed'"))
    s2.close()
    assert seed_rows, "seed notes must be persisted"
    for r in seed_rows:
        assert r["truth_class"] == "INFERENCE"


def test_phaseD_seed_is_idempotent(tmp_path):
    """Running seed twice on the same repo must not duplicate notes."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": (
            "export function alpha() {}\n"
            "export function beta() { alpha(); alpha(); alpha(); }\n"
        ),
    })
    store.close()
    rc, out1 = _run_cli(["--path", str(root), "--json", "seed"])
    assert rc == 0
    d1 = json.loads(out1)
    rc, out2 = _run_cli(["--path", str(root), "--json", "seed"])
    assert rc == 0
    d2 = json.loads(out2)
    # Second run should skip every target created by the first.
    assert d2["created_count"] == 0, (
        f"second seed run must skip existing; got created={d2['created']}")
    assert d2["skipped_count"] >= d1["created_count"]


def test_gap1_scoped_npm_package_not_repo_relative(tmp_path):
    """@prisma/client, next/server etc. must be `external_module`, not
    `repo_relative`. Before the fix any spec containing `/` was called
    repo_relative which flooded the actionable list with npm packages."""
    from projmem.cli import _classify_import_spec
    assert _classify_import_spec("src/a.ts", "@prisma/client") == "external_module"
    assert _classify_import_spec("src/a.ts", "next/server") == "external_module"
    assert _classify_import_spec("src/a.ts", "@tanstack/react-query") == "external_module"
    assert _classify_import_spec("src/a.ts", "react/jsx-runtime") == "external_module"
    assert _classify_import_spec("src/a.ts", "node:fs/promises") == "external_module"
    # Sanity: real repo-relative specs still classify correctly.
    assert _classify_import_spec("src/a.ts", "./utils") == "repo_relative"
    assert _classify_import_spec("src/a.ts", "../lib/helper") == "repo_relative"
    assert _classify_import_spec("src/a.ts", "/abs/path") == "repo_relative"


def test_gap2_parity_excludes_doc_files_by_default(tmp_path):
    """parity default must skip refs from lang='other' (markdown,
    AGENTS.md, .cursorrules) — the regex fallback over prose emits
    call-kind refs for English words that flood referenced_but_undefined."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function realFn() { realFn(); }\n",
        "AGENTS.md": (
            "Use the `handler` pattern and check the `route` output.\n"
            "The `processor` runs `validator` with custom `adapter` logic.\n"
        ),
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "parity"])
    assert rc == 0
    data = json.loads(out)
    # Words from the markdown file should NOT surface as undefined refs.
    undefined_files = {r["file"] for r in data.get("referenced_but_undefined", [])}
    assert "AGENTS.md" not in undefined_files, (
        f"prose refs should be excluded by default; got files={undefined_files}")
    # Opt-in: --include-non-code keeps legacy behavior available.
    rc, out = _run_cli(["--path", str(root), "--json", "parity",
                        "--include-non-code"])
    assert rc == 0
    data = json.loads(out)
    undefined_files = {r["file"] for r in data.get("referenced_but_undefined", [])}
    # With the flag on, markdown refs do appear (that's the raw view).
    # Note: don't assert AGENTS.md is PRESENT because the regex extractor
    # may not tag every markdown file as having refs; just assert the
    # flag doesn't error out.


def test_gap3_as_const_array_detected_as_enum_shape(tmp_path):
    """Shared TS enums written as `export const Xs = [...] as const;`
    must be captured as enum_shape contracts so the cross-layer check
    compares them against Prisma enums. OpsCanvas pattern."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/shared/status.ts": (
            "export const StatusTypes = [\n"
            "  'OPEN',\n  'CLOSED',\n  'PENDING',\n"
            "] as const;\n"
        ),
    })
    rows = list(store.conn.execute(
        "SELECT name, context FROM contracts WHERE kind='enum_shape' "
        "AND file='src/shared/status.ts'"))
    store.close()
    names = {r["name"] for r in rows}
    assert "StatusTypes" in names, f"as-const array must be captured; got {names}"
    # Singular alias is also emitted so Prisma `enum Status { ... }`
    # reconciles against the TS `StatusTypes` const.
    assert "StatusType" in names, (
        f"singular canonical name should also be emitted; got {names}")
    ts_row = next(r for r in rows if r["name"] == "StatusTypes")
    assert "OPEN" in ts_row["context"]
    assert "CLOSED" in ts_row["context"]
    assert "PENDING" in ts_row["context"]


def test_gap3_cross_layer_enum_check_fires_on_as_const_mismatch(tmp_path):
    """End-to-end: TS `as const` array + Prisma enum of same name with
    different member sets must trigger cross_layer_enum_mismatch
    finding in the checklist. Previously failed silently because the
    extractor didn't capture the TS side."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/shared/status.ts": (
            "export const StatusTypes = ['OPEN', 'CLOSED', 'PENDING'] as const;\n"
        ),
        "prisma/schema.prisma": (
            "enum StatusType {\n  OPEN\n  CLOSED\n}\n"
        ),
    })
    from projmem import checklist as _ck
    res = _ck.run(cfg, store)
    store.close()
    finding = next((f for f in res["findings"]
                    if f["code"] == "cross_layer_enum_mismatch"), None)
    assert finding is not None, (
        f"as-const vs Prisma mismatch must fire the gate; got {res['findings']}")
    names = {m["name"] for m in finding["details"]["mismatches"]}
    assert "StatusType" in names


def test_gap4_flow_empty_result_suggests_other_kinds(tmp_path):
    """When flow <name> --kind X returns empty but the name exists under
    another kind, the output must include a `hints` array pointing the
    user at the right kind."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": (
            "export const FEATURE_TASKS_V2 = 'FEATURE_TASKS_V2';\n"
        ),
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "flow",
                        "FEATURE_TASKS_V2", "--kind", "env"])
    assert rc == 0
    data = json.loads(out)
    assert data.get("read_sites") == []
    hints = data.get("hints") or []
    assert hints, (
        f"empty flow with --kind env should emit hints when name exists "
        f"under another kind; got {data}")
    # At least one hint should name another kind.
    combined = " ".join(hints).lower()
    assert "token" in combined or "kind" in combined


def test_gap5_callgraph_cross_file_flag_follows_external_edges(tmp_path):
    """`projmem callgraph <file> --cross-file` must emit edges to
    symbols defined in OTHER files. Default (no flag) is still
    intra-file only."""
    cfg, store, root = _indexed(tmp_path, {
        "src/gateway.ts": (
            "import { checkPerm } from './rbac';\n"
            "export function route() { return checkPerm('read'); }\n"
        ),
        "src/rbac.ts": "export function checkPerm(p: string) { return true; }\n",
    })
    store.close()
    # Default: no cross-file edges.
    rc, out = _run_cli(["--path", str(root), "--json", "callgraph",
                        "src/gateway.ts"])
    assert rc == 0
    data = json.loads(out)
    edges = data.get("edges") or []
    assert not any(e.get("cross_file") for e in edges), (
        f"default callgraph must be intra-file only; got {edges}")
    # With --cross-file: the route → checkPerm edge should appear.
    rc, out = _run_cli(["--path", str(root), "--json", "callgraph",
                        "src/gateway.ts", "--cross-file"])
    assert rc == 0
    data = json.loads(out)
    edges = data.get("edges") or []
    xf_edges = [e for e in edges if e.get("cross_file")]
    assert xf_edges, (
        f"--cross-file must emit at least one external edge; got {edges}")
    assert any(e["from"] == "route" and e["to"] == "checkPerm"
               and e.get("to_file") == "src/rbac.ts"
               for e in xf_edges), (
        f"route→checkPerm cross-file edge missing; got {xf_edges}")


def test_changes_survives_pre_index_snapshot_rotation(tmp_path):
    """Reported bug: session 1 edits file + runs `projmem complete`.
    Session 2 runs `projmem changes` and gets "no changes" because
    `projmem complete` rotated the pre-index snapshot PAST the edits.

    The edit log is append-only and must preserve the history. After
    two re-indexes (each rotating the snapshot), the original edit
    still shows up in `projmem changes`.
    """
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() { return 1; }\n",
        "src/b.ts": "export function beta() {}\n",
    })
    store.close()
    # Session 1: edit a.ts and re-index (rotates pre-index).
    (root / "src" / "a.ts").write_text(
        "export function alpha() { return 99; }\n")
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0
    # Second re-index with no further edits (rotates pre-index AGAIN).
    # This is the exact sequence a `projmem complete` run + session 2
    # bootstrap would trigger.
    rc, _ = _run_cli(["--path", str(root), "index"])
    assert rc == 0
    # Session 2 asks: what did we edit?
    rc, out = _run_cli(["--path", str(root), "--json", "changes"])
    assert rc == 0
    data = json.loads(out)
    files = {f["file"]: f for f in data["changed_files"]}
    assert "src/a.ts" in files, (
        "edit log must survive pre-index rotation; src/a.ts disappeared: "
        f"{list(files)}")


def test_changes_command_edit_log_records_hash_transition(tmp_path):
    """Edit a file and re-index. The edit log must show the path with
    new edit_count and the drift should be cleared (it was absorbed
    into the index run).

    This is the core regression for the reported bug: "after `projmem
    complete`, session 2's `projmem changes` said no changes". The
    edit log survives pre-index rotation, so the re-index is visible."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() { return 1; }\n",
    })
    store.close()
    (root / "src" / "a.ts").write_text(
        "export function alpha() { return 1; }\n"
        "export function newlyAdded() { return 7; }\n")
    rc, _ = _run_cli(["--path", str(root), "index"])  # incremental
    assert rc == 0
    rc, out = _run_cli(["--path", str(root), "--json", "changes"])
    assert rc == 0
    data = json.loads(out)
    assert data["source"] == "edit_log", (
        f"default mode should read edit log; got source={data.get('source')}")
    files = {f["file"]: f for f in data["changed_files"]}
    assert "src/a.ts" in files, (
        f"edit log must show src/a.ts after re-index; got {list(files)}")
    assert files["src/a.ts"]["edit_count"] >= 1
    assert data["summary"]["drifted_on_disk"] == 0


def test_changes_command_via_snapshot_diff_shows_added_symbols(tmp_path):
    """Explicit `--since pre-index` keeps the old symbol/contract-level
    diff shape for callers who want that breakdown."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() { return 1; }\n",
    })
    store.close()
    (root / "src" / "a.ts").write_text(
        "export function alpha() { return 1; }\n"
        "export function newlyAdded() { return 7; }\n")
    rc, _ = _run_cli(["--path", str(root), "index", "--force"])
    assert rc == 0
    rc, out = _run_cli(["--path", str(root), "--json", "changes",
                        "--since", "pre-index"])
    assert rc == 0
    data = json.loads(out)
    files = {f["file"]: f for f in data["changed_files"]}
    assert "src/a.ts" in files
    added_names = {s["name"] for s in files["src/a.ts"]["added_symbols"]}
    assert "newlyAdded" in added_names


def test_changes_command_freshly_indexed_has_no_drift(tmp_path):
    """A freshly-indexed repo that was NOT edited afterward should
    report zero on-disk drift. (The edit log will have entries from the
    initial index run — that's expected and correct.)"""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() {}\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "changes"])
    assert rc == 0
    data = json.loads(out)
    assert data["summary"]["drifted_on_disk"] == 0
    # Every edit log row from the initial index has prev_hash=None
    # (was_added=True) because the files are new to the index.
    for f in data["changed_files"]:
        assert f.get("was_added") is True, (
            f"initial-index rows should be was_added=True; got {f}")


def test_session_includes_truth_class_per_note(tmp_path):
    """`projmem session <target>` must surface each note's truth_class so
    callers don't have to fall back to `note list` to see FACT vs
    INFERENCE. Codex audit on /tmp/projectX flagged the omission."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() { return 1; }\n",
    })
    rc, _ = _run_cli([
        "--path", str(root), "note", "add", "src/a.ts", "fact-note body",
        "--kind", "verified-safe",
        "--truth-class", "FACT", "--confidence", "0.9",
        "--author", "tester",
    ])
    assert rc == 0
    rc, out = _run_cli(["--path", str(root), "--json", "session", "src/a.ts"])
    assert rc == 0
    data = json.loads(out)
    notes = data.get("notes_on_target") or []
    assert notes, f"expected a note in session output; got {data}"
    assert notes[0].get("truth_class") == "FACT", (
        f"session must echo truth_class; got {notes[0]}")


def test_contracts_kind_accepts_enum_shape(tmp_path):
    """`projmem contracts --kind enum_shape` was rejected by argparse
    because the choices list was hardcoded to the pre-enum_shape kinds.
    Codex audit on /tmp/projectX flagged this."""
    cfg, store, root = _indexed(tmp_path, {
        "src/types.ts": (
            "export enum Status { OPEN = 'OPEN', CLOSED = 'CLOSED' }\n"
        ),
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "contracts",
                        "src/types.ts", "--kind", "enum_shape"])
    assert rc == 0, f"contracts --kind enum_shape exited non-zero: {out}"


def test_notes_summary_uses_internal_bound_pct_for_hint(tmp_path):
    """The notes-summary low-binding hint should reference internal
    binding (real signal) rather than headline bound_pct (which is
    dragged down by external/framework refs).

    Strategy: build a tiny repo where bound_pct ≈ 100%; assert NO
    low-binding hint appears."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": (
            "export function leaf() { return 1; }\n"
            "export function caller() { return leaf(); }\n"
        ),
    })
    from projmem.notes_summary import build_summary
    summ = build_summary(store, str(root))
    store.close()
    hints = summ.get("next_steps_hint") or []
    assert not any("ref binding is low" in h.lower() for h in hints), (
        f"healthy small repo should not emit low-binding hint; got hints={hints}")


def test_p0_11_reexport_barrel_refs_now_bound(tmp_path):
    """A name imported through a `export * from ...` barrel must be bound
    to the leaf symbol's id, not left unbound. Audit reproducer:
    TypeScript namespace barrels (very common in TS projects) caused
    most of the 68% unbound refs."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/leaf.ts": (
            "export function uniqueLeafFn() { return 1; }\n"
        ),
        "src/_ns.ts": (
            "export * from './leaf';\n"
        ),
        "src/consumer.ts": (
            "import { uniqueLeafFn } from './_ns';\n"
            "export function caller() { return uniqueLeafFn(); }\n"
        ),
    })
    refs = list(store.conn.execute(
        "SELECT file, name, target_symbol_id FROM refs "
        "WHERE name='uniqueLeafFn' AND file='src/consumer.ts'"))
    store.close()
    assert refs, "expected refs to uniqueLeafFn from consumer.ts"
    bound = [r for r in refs if r["target_symbol_id"]]
    assert bound, (
        "consumer.ts should have a bound ref to uniqueLeafFn via the "
        f"_ns barrel; got refs={[(r['file'], r['name'], r['target_symbol_id']) for r in refs]}")
    assert bound[0]["target_symbol_id"].startswith("src/leaf.ts#"), (
        f"binding should point at leaf.ts def; got {bound[0]['target_symbol_id']}")


def test_p0_11_two_level_reexport_chain_binds(tmp_path):
    """Two-hop barrel chain (consumer → barrel2 → barrel1 → leaf)
    must still bind."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/leaf.ts": (
            "export function deepLeafSym() { return 1; }\n"
        ),
        "src/barrel1.ts": "export * from './leaf';\n",
        "src/barrel2.ts": "export * from './barrel1';\n",
        "src/consumer.ts": (
            "import { deepLeafSym } from './barrel2';\n"
            "export function go() { return deepLeafSym(); }\n"
        ),
    })
    refs = list(store.conn.execute(
        "SELECT target_symbol_id FROM refs "
        "WHERE name='deepLeafSym' AND file='src/consumer.ts'"))
    store.close()
    assert refs, "expected refs to deepLeafSym from consumer.ts"
    assert any(r["target_symbol_id"]
               and r["target_symbol_id"].startswith("src/leaf.ts#")
               for r in refs), (
        f"two-level barrel chain should bind to leaf.ts; got {refs}")


def test_p0_11_binding_summary_exposes_reexport_tier(tmp_path):
    """The doctor-visible binding stats must include a non-zero
    bound_reexport count when barrel resolution succeeds. Reset
    target_symbol_id first to attribute the binding to the new tier
    rather than `already_bound` (the index_all pass binds eagerly)."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/leaf.ts": "export function rxOnlyLeaf() {}\n",
        "src/_ns.ts": "export * from './leaf';\n",
        "src/consumer.ts": (
            "import { rxOnlyLeaf } from './_ns';\n"
            "export function c() { return rxOnlyLeaf(); }\n"
        ),
    })
    # Clear bindings so the pass attributes resolution to the right tier.
    store.conn.execute("UPDATE refs SET target_symbol_id=NULL")
    store.conn.commit()
    from projmem.binding import resolve_refs
    counts = resolve_refs(store)
    store.close()
    assert counts["bound_reexport"] >= 1, (
        f"expected a reexport-bound ref; got counts={counts}")


# ---------------------------------------------------------------------------
# P0#10 — note body verification (body-text staleness, not just file hash)
# ---------------------------------------------------------------------------

def _make_note_row(target: str, body: str) -> dict:
    """Synthesize an annotation row sufficient for revalidate_annotation."""
    return {
        "id": 1, "target": target, "kind": "note", "body": body,
        "author": "test", "created_at": 0,
        "staleness": "fresh", "confidence": 0.5, "confidence_base": 0.5,
        "fingerprint": None, "evidence": None, "expires_at": None,
    }


def test_p0_10_body_with_missing_backtick_identifier_is_stale(tmp_path):
    """A note whose prose mentions `vanished_function` (backticked) is
    marked body_stale when no symbol of that name exists, even if the
    target file's fingerprint hasn't drifted."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/a.py": "def real_function():\n    return 1\n",
    })
    from projmem.integrity import verify_note_body
    body = ("This module exposes `real_function` and used to expose "
            "`vanished_function` until last quarter.")
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert res["is_stale"] is True
    assert "vanished_function" in res["missing_identifiers"]
    assert "real_function" not in res["missing_identifiers"]


def test_p0_10_body_with_missing_path_is_stale(tmp_path):
    """A note prose citing `src/old_module.ts` should be flagged when
    that file no longer exists in the index nor on disk."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/keeper.ts": "export const x = 1;\n",
    })
    from projmem.integrity import verify_note_body
    body = "See src/keeper.ts and the legacy src/old_module.ts for context."
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert res["is_stale"] is True
    assert "src/old_module.ts" in res["missing_paths"]
    assert "src/keeper.ts" not in res["missing_paths"]


def test_p0_10_body_with_drifted_line_citation_is_stale(tmp_path):
    """A `file:line` citation past EOF should be flagged as line drift."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/short.py": "x = 1\n",
    })
    from projmem.integrity import verify_note_body
    body = "The check at src/short.py:99 used to handle this."
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert res["is_stale"] is True
    drifts = res["line_drift"]
    assert any(d["file"] == "src/short.py" and d["cited_line"] == 99
               for d in drifts)


def test_p0_10_kebab_header_in_backticks_not_flagged(tmp_path):
    """Backticked kebab tokens like `x-opscanvas-signature` are HTTP
    headers, not symbols. The body verifier must not treat them as
    missing-identifier citations. Codex audit on /tmp/projectX flagged
    this as a false-positive class."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/h.ts": "export function realSig() {}\n",
    })
    from projmem.integrity import verify_note_body
    body = "Sets `x-opscanvas-signature` header via `realSig` helper."
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert res["is_stale"] is False
    assert "x-opscanvas-signature" not in res["missing_identifiers"]


def test_p0_10_path_suffix_match_not_flagged(tmp_path):
    """A body that drops the `src/` prefix in prose — `worker/handlers/
    foo.ts` when the file is at `src/worker/handlers/foo.ts` — must not
    be flagged as missing. Codex audit on /tmp/projectX caught this."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/worker/handlers/foo.ts": "export function deliver() {}\n",
    })
    from projmem.integrity import verify_note_body
    body = "Used by worker/handlers/foo.ts to sign before delivery."
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert "worker/handlers/foo.ts" not in res["missing_paths"], (
        f"prefix-dropped path should suffix-match indexed file; got {res}")


def test_p0_10_all_upper_env_token_not_flagged(tmp_path):
    """Backticked ALL_UPPER tokens like `OPS_CANVAS_FF_KEY` are env-var
    templates / contract names, not symbols. Must not be treated as
    missing-identifier citations."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/f.ts": "export function isFeatureEnabled() {}\n",
    })
    from projmem.integrity import verify_note_body
    body = "Reads env override `OPS_CANVAS_FF_KEY`, falls back via `isFeatureEnabled`."
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert res["is_stale"] is False
    assert "OPS_CANVAS_FF_KEY" not in res["missing_identifiers"]


def test_p0_10_body_with_only_valid_refs_is_fresh(tmp_path):
    """No false positives when every cited identifier and path resolves."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/a.py": "def real():\n    return 1\n",
    })
    from projmem.integrity import verify_note_body
    body = "The src/a.py module exports `real`. See src/a.py:1."
    res = verify_note_body(store, body, str(cfg.root))
    store.close()
    assert res["is_stale"] is False
    assert res["missing_identifiers"] == []
    assert res["missing_paths"] == []
    assert res["line_drift"] == []


def test_p0_10_revalidation_downgrades_fresh_when_body_stale(tmp_path):
    """End-to-end: a note whose target fingerprint is unchanged but whose
    body cites a vanished symbol must NOT come back as 'fresh'.

    Audit reproducer: notes can be 'fresh' while body text is stale."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/a.py": "def keeper():\n    return 1\n",
    })
    from projmem.integrity import revalidate_annotation
    note = _make_note_row(
        "src/a.py",
        "Note: `keeper` works fine. Caveat: `gone_function` was removed.")
    res = revalidate_annotation(store, str(cfg.root), note, persist=False)
    store.close()
    assert res.body_consistency["is_stale"] is True
    assert "gone_function" in res.body_consistency["missing_identifiers"]
    assert res.now != "fresh", (
        f"body-stale note should not be reported fresh; got {res.now}")


# ---------------------------------------------------------------------------
# P0#9 — pack . returns a useful repo overview
# ---------------------------------------------------------------------------

def test_p0_9_pack_dot_returns_repo_overview(tmp_path):
    """`pack .` previously returned a hollow symbol-undefined pack. Now it
    returns kind='repo_overview' with summary, top files, contracts, and
    cross-layer enum mismatches when present."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": (
            "import { b } from './b';\n"
            "import { b as b2 } from './b';\n"
            "export function go() { return b(); }\n"
        ),
        "src/b.ts": "export function b() { return 1; }\n",
        "src/types.ts": "export enum Status { OPEN='OPEN', CLOSED='CLOSED' }\n",
        "prisma/schema.prisma": "enum Status {\n  OPEN\n  CLOSED\n  PENDING\n}\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "pack", "."])
    assert rc == 0, f"pack . exited non-zero: {out}"
    data = json.loads(out)
    assert data.get("kind") == "repo_overview", (
        f"expected repo_overview kind; got {data.get('kind')}: {list(data)}")
    assert data["summary"]["files_indexed"] >= 3
    # Languages summary should include the two we indexed.
    assert "typescript" in data["summary"]["languages"]
    # Cross-layer enum mismatch should surface via the overview too.
    mm = data.get("cross_layer_enum_mismatches") or []
    assert any(m["name"] == "Status" for m in mm), (
        f"Status mismatch should appear in overview; got {mm}")
    # The tip helps the agent escalate to a deeper pack.
    assert "tip" in data


def test_p0_9_pack_subdirectory_scopes_overview(tmp_path):
    """`pack src/` should restrict counts to files under src/."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function a() {}\n",
        "src/b.ts": "export function b() {}\n",
        "scripts/build.js": "console.log('build');\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "pack", "src/"])
    assert rc == 0
    data = json.loads(out)
    assert data["kind"] == "repo_overview"
    assert data["target"]["scope_prefix"] == "src/"
    # Only the two src/ files should be counted in the overview.
    assert data["summary"]["files_indexed"] == 2


def test_p0_9_pack_file_target_unaffected(tmp_path):
    """A normal `pack <file>` call must still produce a file-level pack,
    not the new overview."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function alpha() { return 1; }\n",
    })
    store.close()
    rc, out = _run_cli(["--path", str(root), "--json", "pack", "src/a.ts"])
    assert rc == 0
    data = json.loads(out)
    assert data.get("kind") != "repo_overview", (
        f"file target should not return repo_overview; got {data.get('kind')}")
    assert data["target"]["kind"] == "file"


# ---------------------------------------------------------------------------
# P2#8 — default-export aliased imports produce a ref
# ---------------------------------------------------------------------------

def test_p2_8_default_import_alias_produces_ref_to_source_default(tmp_path):
    cfg, store, _ = _indexed(tmp_path, {
        "src/lib.js": "export default function helpfulFn() { return 42; }\n",
        "src/caller.js": (
            "import renamed from './lib';\n"
            "export function g() { return renamed(); }\n"
        ),
    })
    refs = list(store.conn.execute(
        "SELECT file, kind FROM refs WHERE name='helpfulFn'"))
    store.close()
    files = {r["file"] for r in refs}
    assert "src/caller.js" in files, (
        f"caller.js must appear as a ref to helpfulFn; got {files}")
    kinds = {r["kind"] for r in refs}
    assert "default_import" in kinds


def test_p2_8_default_import_skipped_when_ambiguous_default(tmp_path):
    """When the source file has multiple exported symbols, the heuristic
    SHOULDN'T guess. No ref is emitted — better to be silent than to
    bind the wrong symbol."""
    cfg, store, _ = _indexed(tmp_path, {
        "src/lib.js": (
            "export function alpha() {}\n"
            "export function beta() {}\n"
        ),
        "src/caller.js": "import renamed from './lib';\n",
    })
    refs = list(store.conn.execute(
        "SELECT file FROM refs WHERE name IN ('alpha','beta') "
        "AND kind='default_import'"))
    store.close()
    assert refs == [], (
        f"ambiguous default-import should NOT bind; got {refs}")


# ---------------------------------------------------------------------------
# Bench iter 3 regressions — pin correct behaviors found by the diagnostic
# (all three HIGH findings were false positives: the regex fired on text that
#  describes *passing* test output, not actual bugs).
# ---------------------------------------------------------------------------

def test_bench_iter3_refute_note_id_resolves_to_file_path(tmp_path):
    """Bench iter 3 / v4 Bug 1 pin: `refute add --note-id N` must resolve
    the target from note N's target field (a file path), NOT store the raw
    integer as the target. The diagnostic fired on passing benchmark text;
    this test pins the fixed behavior so a regression would be caught."""
    cfg, store, root = _indexed(tmp_path, {
        "src/a.ts": "export function foo() {}\n",
    })
    # Add a note targeting src/a.ts.
    from projmem.store import Store as _Store
    note_id = store.add_annotation(
        target="src/a.ts", kind="note",
        body="@defined-at(foo, src/a.ts:1)", author="test-author")
    store.close()

    # refute add --note-id <N> should resolve to src/a.ts, not "N".
    rc, out = _run_cli([
        "--path", str(root), "--json",
        "refute", "add", "--note-id", str(note_id), "counter-evidence",
    ])
    assert rc == 0, f"refute add should succeed; got rc={rc}, out={out}"
    data = json.loads(out)
    # Bug: if target is the integer string, this assertion fails.
    assert data["target"] == "src/a.ts", (
        f"refute target must be file path, not note-id integer; got {data['target']!r}")
    assert data.get("resolved_from_note") == note_id


def test_bench_iter3_orphan_note_returns_refuted_not_verified(tmp_path):
    """Bench iter 3 / v4 Bug 2 pin: note-verify on a deleted file must
    return REFUTED (contradicted), NOT VERIFIED. The diagnostic fired on
    passing benchmark text; this test pins the fixed behavior."""
    cfg, store, root = _indexed(tmp_path, {
        "src/orphan.ts": "export function gone() {}\n",
    })
    # Add a note with a structured claim (subject/predicate/object) so
    # claims.verify_note() has something to check.
    note_id = store.add_annotation(
        target="src/orphan.ts", kind="note",
        body="gone is defined at line 1",
        author="test-orphan",
        evidence=[{
            "subject": "gone",
            "predicate": "defined-at",
            "object": "src/orphan.ts:1",
        }])
    store.close()

    # Now delete the file so it becomes an orphan on disk.
    (root / "src" / "orphan.ts").unlink()

    rc, out = _run_cli([
        "--path", str(root), "--json", "note-verify", "src/orphan.ts",
    ])
    assert rc == 0
    data = json.loads(out)
    results = data.get("results") or []
    assert results, "note-verify must return at least one result for the orphan note"
    # The claim should be REFUTED (file missing on disk).
    entry = next((r for r in results if r.get("id") == note_id), None)
    assert entry is not None, f"orphan note id={note_id} not found in results"
    claims = entry.get("claims") or []
    assert claims, "orphan note must produce at least one claim verdict"
    statuses = {c.get("status") for c in claims}
    assert "REFUTED" in statuses, (
        f"orphan claim must be REFUTED (file missing on disk); got statuses={statuses}")
    assert "VERIFIED" not in statuses, (
        f"orphan claim must NOT be VERIFIED when file is missing; got statuses={statuses}")


def test_bench_iter5_complete_expires_notes_for_deleted_file(tmp_path):
    """Bench iter 5 / Bug 2 pin: when `projmem complete` removes a deleted
    file from the index, notes whose target is that file must be soft-expired
    so they no longer inflate contradicted_count and block agents.

    Bug: store.remove_file() was called but annotations targeting the deleted
    file were left intact, keeping contradicted_count elevated permanently.
    Fix: expire_annotations_for_deleted_file() is now called in cmd_complete.
    """
    import time as _time
    cfg, store, root = _indexed(tmp_path, {
        "src/alive.ts": "export function alive() {}\n",
        "src/doomed.ts": "export function gone() {}\n",
    })

    # Save a note targeting the file that will be deleted.
    ann_id = store.add_annotation(
        target="src/doomed.ts",
        kind="fact",
        body="@defined-at(gone, src/doomed.ts:1) gone is here.",
        author="test-iter5",
        staleness="contradicted",  # simulate it already being contradicted
    )
    store.commit()

    # Confirm the note is returned by list_annotations (not expired yet).
    before = store.list_annotations(target="src/doomed.ts", include_expired=False)
    assert any(r["id"] == ann_id for r in before), \
        "note should be live before file deletion"

    # Now delete the file from disk and call expire helper directly
    # (mirrors what cmd_complete does after store.remove_file).
    expired = store.expire_annotations_for_deleted_file("src/doomed.ts")
    store.commit()

    assert expired >= 1, (
        f"expire_annotations_for_deleted_file must expire >=1 note; got {expired}")

    # The note must no longer appear in non-expired queries.
    after = store.list_annotations(target="src/doomed.ts", include_expired=False)
    assert not any(r["id"] == ann_id for r in after), (
        "expired note must not appear in list_annotations(include_expired=False)")

    # But it must still be retrievable with include_expired=True.
    with_expired = store.list_annotations(target="src/doomed.ts", include_expired=True)
    assert any(r["id"] == ann_id for r in with_expired), (
        "soft-expired note must still be present with include_expired=True")

    store.close()


def test_bench_iter5_complete_notes_expired_count_in_refresh(tmp_path):
    """Bench iter 5 regression pin: cmd_complete output must include
    notes_expired_for_deleted in the refresh block when files are deleted,
    so agents can see that orphan notes were cleaned up."""
    cfg, store, root = _indexed(tmp_path, {
        "src/alive.ts": "export function alive() {}\n",
        "src/doomed.ts": "export function gone() {}\n",
    })

    # Add a note targeting doomed.ts
    store.add_annotation(
        target="src/doomed.ts",
        kind="fact",
        body="@defined-at(gone, src/doomed.ts:1) gone is here.",
        author="test-iter5",
    )
    store.commit()
    store.close()

    # Delete the file from disk so complete() sees it as deleted.
    (root / "src" / "doomed.ts").unlink()

    rc, out = _run_cli(["--path", str(root), "--json", "complete"])
    data = json.loads(out)

    refresh = data.get("refresh", {})
    applied = refresh.get("applied", {})
    assert "notes_expired_for_deleted" in applied, (
        f"refresh.applied must contain notes_expired_for_deleted; got {applied}")
    assert applied["notes_expired_for_deleted"] >= 1, (
        f"at least 1 note should be expired; got {applied['notes_expired_for_deleted']}")


def test_bench_iter3_conclude_rejects_refuted_claim_exit3(tmp_path):
    """Bench iter 3 / v2 Bug 2 pin: `conclude` must reject prose that
    contains a claim REFUTED by the index (symbol genuinely absent),
    returning exit code 3 and error 'refuted-before-save'.

    Note on scope: wrong LINE numbers in the claim now produce MOVED,
    not REFUTED (non-semantic drift shouldn't gate a save). The gate
    still fires on true refutations — symbol doesn't exist anywhere."""
    cfg, store, root = _indexed(tmp_path, {
        "src/b.ts": "export function bar() {}\n",
    })
    store.close()

    # nonexistent_fn is not in the index at all → true REFUTED.
    rc, out = _run_cli([
        "--path", str(root), "--json",
        "conclude",
        "@defined-at(nonexistent_fn, src/b.ts:9999) is the main helper.",
    ])
    # Exit code 3 = refuted-before-save gate fired.
    assert rc == 3, (
        f"conclude with a refuted claim must exit 3; got rc={rc}, out={out}")
    data = json.loads(out)
    assert data.get("error") == "refuted-before-save", (
        f"expected error=refuted-before-save; got {data}")
    assert data.get("verdict") == "has_refuted"
