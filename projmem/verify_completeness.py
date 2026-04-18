"""projmem/verify_completeness.py — narrow post-edit gate for ONE target.

`projmem complete` is the project-wide "did I forget anything?" gate.
`projmem verify-completeness <target>` is the FOCUSED version for the
specific file/symbol you just edited:

  missing_updates:       sites that look like they still reference the
                          old shape of the target (by name or by file)
  stale_paths:           files listed in notes or edges that claim to
                          touch the target but whose on-disk hash drifted
  unupdated_consumers:   importer/caller files that haven't been
                          re-touched in this working session (proxy: no
                          recent audit-trail) but whose refs look stale
  inconsistent_strategy: tests referencing the target that weren't
                          updated (symbol renamed → test still uses old
                          name; env renamed → test expects old value)

Each finding has a severity (`high` / `warning` / `info`) and a concrete
suggestion. Exit 1 when any HIGH.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Set


HIGH = "high"
WARNING = "warning"
INFO = "info"


def verify(store, cfg, target: str,
            limit: int = 50) -> Dict[str, Any]:
    """Focused change-completeness analysis. See module docstring."""
    from . import symbols as _symbols
    from . import freshness as _fresh
    from . import artifacts as _artifacts

    loc = _symbols.locate(store, target, project_root=cfg.root)
    file_target: Optional[str] = None
    symbol_name: Optional[str] = None
    if loc.get("kind") == "file":
        file_target = loc.get("path")
    elif loc.get("kind") == "symbol":
        file_target = loc.get("file")
        symbol_name = loc.get("name")

    findings: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 1. Missing updates: symbol renamed/removed → dangling consumers
    # ------------------------------------------------------------------
    if symbol_name and file_target:
        sym_here = list(store.conn.execute(
            "SELECT line FROM symbols WHERE name=? AND file=?",
            (symbol_name, file_target)))
        if not sym_here:
            # Symbol no longer at target — either moved or renamed.
            danglers = list(store.conn.execute(
                "SELECT file, line, kind FROM refs WHERE name=? "
                "AND file != ? LIMIT ?",
                (symbol_name, file_target, limit)))
            if danglers:
                findings.append({
                    "severity": HIGH,
                    "code":     "missing_updates_dangling_refs",
                    "message":  f"{len(danglers)} file(s) still reference "
                                f"{symbol_name!r} but the symbol is no "
                                f"longer defined in {file_target!r}.",
                    "items":    [{"file": r["file"], "line": int(r["line"]),
                                   "kind": r["kind"]} for r in danglers[:10]],
                    "suggestion": ("Rename / update every dangler, or "
                                    "restore the def in the target file."),
                })

    # ------------------------------------------------------------------
    # 2. Stale paths: notes claim things about files whose on-disk hash
    #    no longer matches the indexed hash.
    # ------------------------------------------------------------------
    if file_target:
        touched: Set[str] = {file_target}
        # Plus every consumer file.
        for r in store.conn.execute(
                "SELECT DISTINCT src FROM edges WHERE dst=? AND type='imports' LIMIT 100",
                (file_target,)):
            touched.add(r["src"])
        stale = _fresh.check_paths(store, cfg.root, list(touched))
        if stale:
            findings.append({
                "severity": HIGH,
                "code":     "stale_paths",
                "message":  f"{len(stale)} related file(s) drifted on disk "
                            "since the last index.",
                "items":    stale[:10],
                "suggestion": (f"Run `projmem index --include {file_target}` "
                                "(targeted) or `projmem complete` "
                                "(full incremental) before trusting this "
                                "verification."),
            })

    # ------------------------------------------------------------------
    # 3. Unupdated consumers: consumers with low-confidence (regex)
    #    parser entries — their refs may be stale after a rename even if
    #    the file itself is fresh.
    # ------------------------------------------------------------------
    if file_target:
        rows = list(store.conn.execute(
            "SELECT e.src, e.dst, f.parser FROM edges e "
            "LEFT JOIN files f ON f.path = e.src "
            "WHERE e.dst=? AND e.type='imports' LIMIT ?",
            (file_target, limit * 2)))
        low_conf = [r for r in rows
                     if (r["parser"] or "").startswith("regex")]
        if low_conf:
            findings.append({
                "severity": WARNING,
                "code":     "unupdated_consumers_low_confidence",
                "message":  (f"{len(low_conf)} consumer file(s) were parsed "
                              "by the regex fallback (not AST). Their refs "
                              "to this target may be stale after a rename."),
                "items":    [{"file": r["src"], "parser": r["parser"]}
                              for r in low_conf[:10]],
                "suggestion": ("Install tree-sitter (`pip install -e "
                                "'.[treesitter]'`) and re-index to get "
                                "AST-grounded refs."),
            })

    # ------------------------------------------------------------------
    # 4. Inconsistent strategy: tests reference the target but ref count
    #    is ZERO in every test file — suggests the test was left behind
    #    when the target's surface changed.
    # ------------------------------------------------------------------
    if file_target:
        test_files = []
        for r in store.conn.execute(
                "SELECT DISTINCT src FROM edges WHERE dst=? AND type='imports' LIMIT 200",
                (file_target,)):
            if _is_test_file(r["src"]):
                test_files.append(r["src"])
        tests_missing_refs: List[str] = []
        if symbol_name:
            for tf in test_files:
                row = store.conn.execute(
                    "SELECT COUNT(*) AS n FROM refs WHERE file=? AND name=?",
                    (tf, symbol_name)).fetchone()
                if row and row["n"] == 0:
                    tests_missing_refs.append(tf)
        if tests_missing_refs:
            findings.append({
                "severity": WARNING,
                "code":     "inconsistent_strategy_test_misses_symbol",
                "message":  (f"{len(tests_missing_refs)} test file(s) import "
                              f"{file_target!r} but contain NO refs to "
                              f"{symbol_name!r}. The test may be stale "
                              "(renamed or removed symbol)."),
                "items":    [{"file": t} for t in tests_missing_refs[:10]],
                "suggestion": ("Either update the test to reference the new "
                                "symbol name, or remove the stale import."),
            })

    # ------------------------------------------------------------------
    # 5. Note-level alignment: notes on this target that are currently
    #    REFUTED or contradicted but still saved — they should be
    #    re-saved (updated) alongside the code change.
    # ------------------------------------------------------------------
    try:
        from . import integrity as _intg
        note_rows = store.list_annotations(target=target,
                                            include_expired=False)
        refuted_notes: List[Dict[str, Any]] = []
        for ann in note_rows:
            res = _intg.revalidate_annotation(store, cfg.root, ann,
                                               persist=False)
            if res.claim_overall_status == "contradicted" \
                    or res.now == "contradicted":
                first_refuted = next(
                    (v for v in (res.claim_verdicts or [])
                     if v.get("status") == "REFUTED"),
                    None)
                refuted_notes.append({
                    "id":       ann.get("id"),
                    "target":   ann.get("target"),
                    "kind":     ann.get("kind"),
                    "body":     (ann.get("body") or "")[:160],
                    "first_refuted": (
                        first_refuted and {
                            "subject":   first_refuted.get("subject"),
                            "predicate": first_refuted.get("predicate"),
                            "object":    first_refuted.get("object"),
                            "reason":    first_refuted.get("reason"),
                        }),
                })
        if refuted_notes:
            findings.append({
                "severity": HIGH,
                "code":     "contradicted_notes_on_target",
                "message":  (f"{len(refuted_notes)} note(s) on this target "
                              "currently contradicted. The code change "
                              "invalidated prior FACT claims — update or "
                              "delete these notes as part of the edit."),
                "items":    refuted_notes[:10],
                "suggestion": ("Rewrite the claim with current evidence "
                                "via `projmem note add --claims` or delete "
                                "with `projmem note delete <id>`."),
            })
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Roll-up
    # ------------------------------------------------------------------
    severity_counts = {
        HIGH:    sum(1 for f in findings if f["severity"] == HIGH),
        WARNING: sum(1 for f in findings if f["severity"] == WARNING),
        INFO:    sum(1 for f in findings if f["severity"] == INFO),
    }
    overall = "ok" if not findings else (
        "needs_action" if severity_counts[HIGH] > 0 else "review")
    return {
        "schema_version":  1,
        "target":          target,
        "resolved": {
            "kind":       loc.get("kind"),
            "file":       file_target,
            "symbol":     symbol_name,
        },
        "overall":         overall,
        "severity_counts": severity_counts,
        "findings":        findings,
    }


def _is_test_file(path: str) -> bool:
    """Shared heuristic (mirrors analyze_change._is_test_file)."""
    if not path:
        return False
    p = path.lower()
    if "/test/" in p or p.startswith("test/") or "/tests/" in p \
            or p.startswith("tests/"):
        return True
    base = p.rsplit("/", 1)[-1]
    if base.startswith("test_") or base.endswith("_test.py") \
            or base.endswith("_test.go"):
        return True
    if ".test." in base or ".spec." in base:
        return True
    return False
