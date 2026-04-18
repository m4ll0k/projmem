"""Regression tests for claim-level note verification (projmem/claims.py).

These tests cover the six cases required by the spec:

  1. VERIFIED when current code supports the claim
  2. REFUTED with current_evidence showing the new state
  3. UNCHECKABLE when the input is ambiguous or unknown
  4. Mixed note: one VERIFIED + one REFUTED → strongly_stale overall
  5. Backward compatibility: legacy free-text notes still work
  6. Realistic contract rename (TSC_WATCHFILE → TSC_WATCH_FILE) produces
     a REFUTED env-read-at claim with the new env name in current_evidence

Additional cases:
  - defined-at VERIFIED + REFUTED when symbol moves
  - exported-from verification
  - reexported-via / reverse-dependency-of edge-based verification
  - CLI `--claims FILE` loading
  - FACT claim refutation produces `contradicted` (not just strongly_stale)
  - Unknown predicate → UNCHECKABLE, not a crash
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

from projmem import claims as _claims
from projmem.config import Config
from projmem.indexer import index_all
from projmem.store import Store
from projmem.ts_backend import available as ts_available

needs_ts = pytest.mark.skipif(not ts_available(),
                              reason="tree-sitter not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_indexed_repo(tmp_path, files: dict):
    """Materialise `files` under tmp_path, index, return (cfg, store)."""
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
# 1. Parse / schema helpers
# ---------------------------------------------------------------------------

def test_is_claim_requires_subject_predicate_object():
    good = {"subject": "FOO", "predicate": "env-read-at", "object": "a.py:1"}
    assert _claims.is_claim(good)

    # Legacy evidence entry
    legacy = {"file": "a.py", "line": 1, "note": "see here"}
    assert not _claims.is_claim(legacy)

    # Missing object
    incomplete = {"subject": "FOO", "predicate": "env-read-at"}
    assert not _claims.is_claim(incomplete)


def test_parse_claims_skips_legacy_evidence():
    evidence = [
        {"file": "a.py", "line": 3},
        {"subject": "FOO", "predicate": "defined-at", "object": "a.py:1"},
    ]
    claims = _claims.parse_claims(evidence)
    assert len(claims) == 1
    assert claims[0].subject == "FOO"


def test_parse_claims_accepts_json_string():
    evidence_str = json.dumps([
        {"subject": "X", "predicate": "defined-at", "object": "a.py:5"}
    ])
    claims = _claims.parse_claims(evidence_str)
    assert len(claims) == 1
    assert claims[0].object == "a.py:5"


# ---------------------------------------------------------------------------
# 2. Aggregate status policy
# ---------------------------------------------------------------------------

def _mk_verdict(status, truth_class="INFERENCE"):
    c = _claims.Claim(subject="x", predicate="defined-at",
                      object="a.py:1", truth_class=truth_class)
    return _claims.ClaimVerdict(claim=c, status=status)


def test_aggregate_all_verified_is_fresh():
    v = [_mk_verdict(_claims.VERIFIED) for _ in range(3)]
    assert _claims.aggregate_status(v) == _claims.FRESH


def test_aggregate_refuted_fact_is_contradicted():
    v = [_mk_verdict(_claims.REFUTED, truth_class="FACT"),
         _mk_verdict(_claims.VERIFIED)]
    assert _claims.aggregate_status(v) == _claims.CONTRADICTED


def test_aggregate_refuted_nonfact_is_strongly_stale():
    v = [_mk_verdict(_claims.REFUTED, truth_class="INFERENCE"),
         _mk_verdict(_claims.VERIFIED)]
    assert _claims.aggregate_status(v) == _claims.STRONGLY_STALE


def test_aggregate_mix_verified_uncheckable_is_weakly_stale():
    v = [_mk_verdict(_claims.VERIFIED),
         _mk_verdict(_claims.UNCHECKABLE)]
    assert _claims.aggregate_status(v) == _claims.WEAKLY_STALE


def test_aggregate_all_uncheckable_is_unknown():
    v = [_mk_verdict(_claims.UNCHECKABLE) for _ in range(2)]
    assert _claims.aggregate_status(v) == _claims.UNKNOWN


# ---------------------------------------------------------------------------
# 3. Core verifiers
# ---------------------------------------------------------------------------

@needs_ts
def test_defined_at_verified(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/core.py": "def do_work():\n    return 1\n",
    })
    # Find what line the index assigned.
    row = store.conn.execute(
        "SELECT file, line FROM symbols WHERE name='do_work'").fetchone()
    assert row is not None
    c = _claims.Claim(
        subject="do_work", predicate="defined-at",
        object=f"{row['file']}:{row['line']}", truth_class="FACT")
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.VERIFIED, (
        f"Expected VERIFIED, got {v.status}: {v.reason}")
    assert v.current_evidence
    assert v.current_evidence[0]["file"] == row["file"]


@needs_ts
def test_defined_at_moved_when_symbol_shifted_same_file(tmp_path):
    """Non-semantic drift (symbol still in cited file, different line)
    is MOVED, not REFUTED. Previously flipped the note to contradicted
    on harmless edits like adding an import above; now surfaces the
    new line via `moved_to` for the agent to update the claim."""
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/core.py": "def helper():\n    return 1\n",
    })
    c = _claims.Claim(
        subject="helper", predicate="defined-at",
        object="src/core.py:99")   # wrong line, symbol lives at line 1
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.MOVED, (
        f"Expected MOVED, got {v.status}: {v.reason}")
    # current_evidence must carry the new location so the agent can
    # auto-update the claim.
    assert v.current_evidence
    first = v.current_evidence[0]
    assert first["file"] == "src/core.py"
    assert first["moved_from"] == 99
    assert first["moved_to"] == 1


@needs_ts
def test_defined_at_refuted_when_symbol_gone(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/core.py": "def only_one():\n    return 1\n",
    })
    c = _claims.Claim(
        subject="nonexistent_symbol", predicate="defined-at",
        object="src/core.py:1")
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.REFUTED
    assert "no symbol" in (v.reason or "").lower()


@needs_ts
def test_exported_from_verified(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/lib.js": "export function publicFn() { return 1; }\n",
    })
    c = _claims.Claim(
        subject="publicFn", predicate="exported-from",
        object="src/lib.js")
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.VERIFIED, (
        f"Expected VERIFIED, got {v.status}: {v.reason}")


# ---------------------------------------------------------------------------
# 4. Contract verifiers
# ---------------------------------------------------------------------------

@needs_ts
def test_env_read_at_verified(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/app.js": (
            "const mode = process.env.APP_MODE;\n"
            "console.log(mode);\n"
        )
    })
    # Find the env contract row.
    row = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='APP_MODE'").fetchone()
    assert row is not None, "indexer should have captured APP_MODE as env contract"
    c = _claims.Claim(
        subject="APP_MODE", predicate="env-read-at",
        object=f"{row['file']}:{row['line']}", truth_class="FACT")
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.VERIFIED


@needs_ts
def test_env_read_at_refuted_with_rename_evidence(tmp_path):
    """Realistic rename case: the site now reads a DIFFERENT env var.
    The verdict must be REFUTED and current_evidence must include the
    new env var name at that site (the agent needs to see what's there now)."""
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/watch.ts": (
            "const x = process.env.TSC_WATCH_FILE;\n"   # renamed
            "const y = process.env.UNRELATED;\n"
        )
    })
    # Find where the renamed env read landed.
    renamed = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='TSC_WATCH_FILE'").fetchone()
    assert renamed is not None
    # Claim against the OLD name at the SAME site.
    c = _claims.Claim(
        subject="TSC_WATCHFILE", predicate="env-read-at",
        object=f"{renamed['file']}:{renamed['line']}",
        truth_class="FACT", confidence=0.9)
    v = _claims.verify_claim(store, c)
    store.close()

    assert v.status == _claims.REFUTED
    # current_evidence must name the new env var so the agent sees it.
    names = {e.get("name") for e in v.current_evidence}
    assert "TSC_WATCH_FILE" in names, (
        f"refuted evidence must include the new env name; got {v.current_evidence}")


@needs_ts
def test_env_read_at_refuted_when_gone(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/app.js": "const x = process.env.STILL_HERE;\n",
    })
    c = _claims.Claim(
        subject="REMOVED_ENV", predicate="env-read-at",
        object="src/app.js:1")
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.REFUTED


# ---------------------------------------------------------------------------
# 5. Uncheckable cases
# ---------------------------------------------------------------------------

@needs_ts
def test_malformed_object_is_uncheckable(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "a.py": "x = 1\n"
    })
    c = _claims.Claim(subject="x", predicate="defined-at", object="")
    v = _claims.verify_claim(store, c)
    store.close()
    assert v.status == _claims.UNCHECKABLE


def test_unknown_predicate_is_uncheckable():
    store = None   # verifier fails fast before touching the store
    c = _claims.Claim(
        subject="x", predicate="made-up-predicate", object="a.py:1")
    v = _claims.verify_claim(store, c)
    assert v.status == _claims.UNCHECKABLE
    assert "unsupported predicate" in (v.reason or "").lower()


# ---------------------------------------------------------------------------
# 6. verify_note — mixed verdicts produce aggregate status
# ---------------------------------------------------------------------------

@needs_ts
def test_mixed_note_yields_strongly_stale(tmp_path):
    """Note has one VERIFIED symbol claim + one REFUTED env claim.
    Aggregate status should be strongly_stale."""
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "src/app.js": (
            "const cfg = process.env.CURRENT_NAME;\n"
            "export function doWork() { return cfg; }\n"
        )
    })
    # Use the actual indexed lines for the verified claim.
    sym = store.conn.execute(
        "SELECT file, line FROM symbols WHERE name='doWork'").fetchone()
    env = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='CURRENT_NAME'").fetchone()

    evidence = [
        {
            "subject": "doWork", "predicate": "defined-at",
            "object": f"{sym['file']}:{sym['line']}",
            "truth_class": "FACT"
        },
        {
            "subject": "OLD_NAME", "predicate": "env-read-at",
            "object": f"{env['file']}:{env['line']}",
            "truth_class": "INFERENCE"
        },
    ]
    ann_id = store.add_annotation(
        target=sym["file"], kind="note",
        body="doWork reads OLD_NAME", evidence=evidence)
    ann_row = store.get_annotation(ann_id) if hasattr(store, "get_annotation") \
              else dict(store.conn.execute(
                  "SELECT * FROM annotations WHERE id=?", (ann_id,)).fetchone())

    report = _claims.verify_note(store, ann_row)
    store.close()
    assert report["overall_status"] == _claims.STRONGLY_STALE
    assert report["verified_count"] == 1
    assert report["refuted_count"] == 1


@needs_ts
def test_all_verified_note_is_fresh(tmp_path):
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "a.py": "def foo():\n    return 1\ndef bar():\n    return 2\n"
    })
    rows = {r["name"]: r for r in store.conn.execute(
        "SELECT name, file, line FROM symbols WHERE name IN ('foo', 'bar')")}
    evidence = [
        {"subject": "foo", "predicate": "defined-at",
         "object": f"{rows['foo']['file']}:{rows['foo']['line']}"},
        {"subject": "bar", "predicate": "defined-at",
         "object": f"{rows['bar']['file']}:{rows['bar']['line']}"},
    ]
    ann_id = store.add_annotation(target="a.py", kind="note",
                                   body="foo and bar exist", evidence=evidence)
    ann_row = dict(store.conn.execute(
        "SELECT * FROM annotations WHERE id=?", (ann_id,)).fetchone())
    report = _claims.verify_note(store, ann_row)
    store.close()
    assert report["overall_status"] == _claims.FRESH
    assert report["verified_count"] == 2
    assert report["refuted_count"] == 0


# ---------------------------------------------------------------------------
# 7. Backward compatibility — legacy free-text notes still work
# ---------------------------------------------------------------------------

@needs_ts
def test_legacy_note_verify_does_not_crash(tmp_path):
    """A note with no evidence field and no claims must survive note-verify
    via the fingerprint path — no new exception, no silent corruption."""
    from projmem import integrity as _intg
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "a.py": "def legacy_fn():\n    return 1\n",
    })
    ann_id = store.add_annotation(
        target="a.py#legacy_fn", kind="note",
        body="Just a free-text observation")
    ann_row = dict(store.conn.execute(
        "SELECT * FROM annotations WHERE id=?", (ann_id,)).fetchone())
    res = _intg.revalidate_annotation(store, cfg.root, ann_row, persist=False)
    store.close()
    # No claims → claim_verdicts is empty, claim_overall_status is None, and
    # the fingerprint path decides the label.
    assert res.claim_verdicts == []
    assert res.claim_overall_status is None
    # Legal labels from the fingerprint path.
    assert res.now in ("fresh", "weakly_stale", "strongly_stale",
                       "contradicted", "unknown")


@needs_ts
def test_legacy_evidence_without_claims_still_works(tmp_path):
    """A note carrying only legacy {file, line, note} evidence entries
    (no subject/predicate/object) must NOT be mistaken for a claim-note."""
    cfg, store, _ = _build_indexed_repo(tmp_path, {
        "a.py": "x = 1\n",
    })
    ann_id = store.add_annotation(
        target="a.py", kind="note",
        body="see a.py:1",
        evidence=[{"file": "a.py", "line": 1, "note": "inline ref"}])
    ann_row = dict(store.conn.execute(
        "SELECT * FROM annotations WHERE id=?", (ann_id,)).fetchone())
    report = _claims.verify_note(store, ann_row)
    store.close()
    # No claims detected; fallback path should kick in.
    assert report["claims"] == []
    assert report["overall_status"] is None


# ---------------------------------------------------------------------------
# 8. CLI integration — `note-verify` emits claims + `--claims` loads JSON
# ---------------------------------------------------------------------------

@needs_ts
def test_cli_note_add_claims_from_json_file_then_verify(tmp_path):
    from projmem.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.js").write_text(
        "const cfg = process.env.API_KEY;\n"
        "export function handler() { return cfg; }\n"
    )

    # Index
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["--path", str(repo), "index"])
    assert rc == 0

    # Find real line numbers for the claims.
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    sym = store.conn.execute(
        "SELECT file, line FROM symbols WHERE name='handler'").fetchone()
    env = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='API_KEY'").fetchone()
    store.close()

    claims_payload = [
        {"subject": "handler", "predicate": "defined-at",
         "object": f"{sym['file']}:{sym['line']}", "truth_class": "FACT"},
        {"subject": "API_KEY", "predicate": "env-read-at",
         "object": f"{env['file']}:{env['line']}", "truth_class": "FACT"},
    ]
    claims_file = tmp_path / "claims.json"
    claims_file.write_text(json.dumps(claims_payload))

    # note add --claims
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["--path", str(repo), "note", "add",
                   "src/app.js#handler", "--kind", "note",
                   "handler uses API_KEY", "--claims", str(claims_file)])
    assert rc == 0
    add_out = json.loads(buf.getvalue())
    assert add_out["claim_count"] == 2

    # note-verify → expect fresh + 2 verified
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["--path", str(repo), "note-verify", "src/app.js#handler"])
    assert rc == 0
    verify_out = json.loads(buf.getvalue())
    assert verify_out["verified"] == 1
    entry = verify_out["results"][0]
    assert entry.get("claim_overall_status") == _claims.FRESH
    assert entry.get("verified_count") == 2
    assert entry.get("refuted_count") == 0


@needs_ts
def test_cli_note_verify_surfaces_refuted_claim_with_current_evidence(tmp_path):
    """End-to-end: add structured note, mutate file to simulate rename,
    reindex, note-verify → output must include the REFUTED claim with the
    new env name in current_evidence."""
    from projmem.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    src_path = repo / "src" / "watch.ts"
    src_path.write_text(
        "const foo = process.env.TSC_WATCHFILE;\n"
        "console.log(foo);\n"
    )

    # Index
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "index"])

    # Record the real line where the env read was captured
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    env = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='TSC_WATCHFILE'").fetchone()
    store.close()
    assert env, "indexer should have captured TSC_WATCHFILE"

    # Add a structured note claiming this env-read.
    claims_payload = [{
        "subject": "TSC_WATCHFILE", "predicate": "env-read-at",
        "object": f"{env['file']}:{env['line']}",
        "truth_class": "FACT", "confidence": 0.9
    }]
    claims_file = tmp_path / "claims.json"
    claims_file.write_text(json.dumps(claims_payload))

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "note", "add",
              "src/watch.ts", "--kind", "note",
              "TSC_WATCHFILE env read at known site",
              "--claims", str(claims_file)])

    # Mutate: rename to TSC_WATCH_FILE, reindex.
    src_path.write_text(
        "const foo = process.env.TSC_WATCH_FILE;\n"
        "console.log(foo);\n"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "index", "--force"])

    # note-verify. Round-5 F008: read commands now exit 1 when
    # `repo_memory.contradicted_count > 0` so CI gates can detect a
    # FACT refutation purely from the shell exit code. Catch the
    # SystemExit so we can still inspect the structured payload.
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["--path", str(repo), "note-verify", "src/watch.ts"])
        except SystemExit:
            pass
    verify_out = json.loads(buf.getvalue())
    entry = verify_out["results"][0]

    # A FACT refutation → contradicted.
    assert entry["claim_overall_status"] == _claims.CONTRADICTED, (
        f"Expected contradicted, got {entry.get('claim_overall_status')}")
    assert entry["refuted_count"] == 1
    # The refuted claim's current_evidence must surface the NEW env name.
    refuted = next(c for c in entry["claims"] if c["status"] == _claims.REFUTED)
    ev_names = {e.get("name") for e in refuted.get("current_evidence", [])}
    assert "TSC_WATCH_FILE" in ev_names, (
        f"Expected TSC_WATCH_FILE in refutation evidence; got {ev_names}")


@needs_ts
def test_cli_invalid_claims_json_file_returns_error(tmp_path):
    from projmem.cli import main
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "index"])
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not-valid-json")
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["--path", str(repo), "note", "add",
                  "a.py", "--kind", "note", "x",
                  "--claims", str(bad_file)])
            rc = 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    out = json.loads(buf.getvalue())
    assert out.get("error") == "invalid-claims-file"
    # Round-5 meta-fix: structured errors must exit 2.
    assert rc == 2


@needs_ts
def test_pack_surfaces_claim_verdicts(tmp_path):
    """When a note on the pack target carries structured claims, the pack's
    human_notes entry must include claim_verdicts + counts so the reader
    sees per-belief status, not just 'strongly_stale'."""
    from projmem.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "import os\n"
        "API_TOKEN = os.environ.get('API_TOKEN')\n"
        "def handler():\n"
        "    return API_TOKEN\n"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "index"])

    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    sym = store.conn.execute(
        "SELECT file, line FROM symbols WHERE name='handler'").fetchone()
    store.close()
    assert sym is not None

    claims = [{"subject": "handler", "predicate": "defined-at",
               "object": f"{sym['file']}:{sym['line']}",
               "truth_class": "FACT"}]
    cfp = tmp_path / "c.json"
    cfp.write_text(json.dumps(claims))

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "note", "add",
              "src/app.py", "--kind", "note", "claim-bearing note",
              "--claims", str(cfp)])

    # Pack the file and inspect human_notes.
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "pack", "src/app.py"])
    data = json.loads(buf.getvalue())
    notes = data.get("human_notes") or []
    assert notes, "pack must include the annotated note"
    n = notes[0]
    assert "claim_verdicts" in n
    assert n.get("claim_overall_status") == _claims.FRESH
    assert n.get("verified_count") == 1
    assert n.get("refuted_count") == 0


@needs_ts
def test_cli_audit_rolls_up_claim_statuses(tmp_path):
    """projmem audit <target> must produce a crisp summary with
    verified/refuted/uncheckable counts + refuted_by_subject grouping."""
    from projmem.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    src_path = repo / "src" / "cfg.py"
    src_path.write_text(
        "import os\n"
        "MODE = os.environ.get('APP_MODE')\n"
        "DB = os.environ.get('APP_DB')\n"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "index"])

    # Build claim-bearing notes with both a verified and a refuted claim.
    cfg = Config(root=str(repo))
    store = Store(cfg.db_path)
    e_mode = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='APP_MODE'").fetchone()
    e_db = store.conn.execute(
        "SELECT file, line FROM contracts "
        "WHERE kind='env' AND name='APP_DB'").fetchone()
    store.close()

    good = [{"subject": "APP_MODE", "predicate": "env-read-at",
             "object": f"{e_mode['file']}:{e_mode['line']}",
             "truth_class": "FACT"}]
    bad = [{"subject": "OLD_DB_NAME", "predicate": "env-read-at",
            "object": f"{e_db['file']}:{e_db['line']}",
            "truth_class": "FACT"}]
    good_f = tmp_path / "good.json"; good_f.write_text(json.dumps(good))
    bad_f = tmp_path / "bad.json"; bad_f.write_text(json.dumps(bad))

    # Two notes: one fully verified, one contradicted.
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "note", "add",
              "src/cfg.py", "--kind", "note", "good note",
              "--claims", str(good_f)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "note", "add",
              "src/cfg.py", "--kind", "note", "stale note",
              "--claims", str(bad_f)])

    # Audit. Round-5 F008: contradicted_count > 0 propagates to exit
    # 1 from the memory-attaching wrapper. The structured payload is
    # unchanged; only the shell rc shifted. Catch SystemExit and
    # assert the new code.
    buf = io.StringIO()
    rc = 0
    with redirect_stdout(buf):
        try:
            rc = main(["--path", str(repo), "audit", "src/cfg.py"]) or 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    assert rc == 1, f"contradicted notes must exit 1; got {rc}"
    data = json.loads(buf.getvalue())
    assert data["total_notes"] == 2
    assert data["total_claims"] == 2
    assert data["verified_count"] == 1
    assert data["refuted_count"] == 1
    assert data["uncheckable_count"] == 0
    # refuted_by_subject must group by subject identifier.
    by_subj = {x["subject"]: x for x in data["refuted_by_subject"]}
    assert "OLD_DB_NAME" in by_subj
    assert by_subj["OLD_DB_NAME"]["refuted_count"] == 1
    # Contradicted note IDs must be populated (FACT claim refuted).
    assert len(data["contradicted_note_ids"]) == 1


@needs_ts
def test_cli_claims_missing_required_keys_returns_error(tmp_path):
    from projmem.cli import main
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--path", str(repo), "index"])
    claims_file = tmp_path / "c.json"
    # Missing 'object' field.
    claims_file.write_text(json.dumps([
        {"subject": "x", "predicate": "defined-at"}
    ]))
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["--path", str(repo), "note", "add",
                  "a.py", "--kind", "note", "x",
                  "--claims", str(claims_file)])
            rc = 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    out = json.loads(buf.getvalue())
    assert "error" in out
    # Standardized shape (#11): tag in `error`, sentence in `message`.
    assert out["error"] == "claim-missing-keys"
    assert "missing required keys" in (out.get("message") or "").lower()
    # Round-5 meta-fix: structured errors exit 2.
    assert rc == 2
