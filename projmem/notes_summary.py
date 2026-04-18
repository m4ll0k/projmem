"""projmem/notes_summary.py — project-wide "what's remembered?" rollup.

Answers the first question a second-session agent needs:

  - How many conclusions have been saved about this repo?
  - Which of them have been invalidated?
  - Which targets carry the most risk right now?
  - What did the last N sessions work on?

Without this, an agent has to guess a target and call `projmem session
<guess>` — that's slow and easy to miss the important areas.

The summary is BOUNDED: all lists capped (default 10 per section),
total payload stays under ~5 KB on typical repos. Safe to call as the
first command of every session.
"""
from __future__ import annotations
import time
from typing import Any, Dict, List, Optional


# Staleness labels that count toward `stale_note_count` in the per-target
# risk view. Includes `weakly_stale` (body-prose drift) so a target with
# only weakly-stale notes doesn't read as `stale_note_count: 0` while
# `totals.by_staleness` shows weakly_stale > 0 — that self-contradiction
# was actively misleading agents. The stronger concerns still have their
# own counters (`contradicted_count`, `refuted_claim_count`) so the
# distinction isn't lost.
ATTENTION_STALENESS = {"weakly_stale", "strongly_stale", "contradicted"}


def build_summary(store, repo_root: str,
                  max_recent: int = 10,
                  max_contradicted: int = 10,
                  max_risk_targets: int = 10,
                  max_distinct_authors: int = 10
                  ) -> Dict[str, Any]:
    """Project-wide memory summary. Reads only; no mutation.

    Returns:
      totals:            aggregate counts by staleness and truth_class
      recent_notes:      last N notes (newest first), with key fields
      contradicted:      notes whose current status is 'contradicted'
      risk_targets:      targets ranked by refuted-claim + contradicted counts
      authors:           distinct authors + count each (who's been writing)
      index_summary:     files / symbols / ref_binding bind_pct, for context
      next_steps_hint:   agent-facing guidance
    """
    from . import integrity as _intg
    from . import claims as _claims
    from . import binding as _binding

    # Lazy revalidation: we evaluate each note with the claims module so the
    # summary reflects CURRENT status, not last-persisted.
    #
    # Benchmark R3 fix: revalidate EVERY note first, persisting the
    # updated staleness column. Then the totals query below reads the
    # live truth, and `memory_header.build_header` (which reads the
    # same column) stays in sync. Without this sweep, `by_staleness`
    # and `repo_memory.contradicted_count` drift apart.
    try:
        all_note_rows = list(store.conn.execute(
            "SELECT * FROM annotations "
            "WHERE expires_at IS NULL OR expires_at > ?",
            (time.time(),)))
        for _row in all_note_rows:
            try:
                _intg.revalidate_annotation(store, repo_root, dict(_row),
                                              persist=True)
            except Exception:
                continue
    except Exception:
        pass

    # 1. Totals — NOW read the column; revalidation above ensured it's fresh.
    total = store.conn.execute(
        "SELECT COUNT(*) AS n FROM annotations").fetchone()["n"]
    by_staleness_rows = list(store.conn.execute(
        "SELECT staleness, COUNT(*) AS n FROM annotations "
        "GROUP BY staleness"))
    by_staleness = {r["staleness"] or "unknown": r["n"]
                     for r in by_staleness_rows}
    by_truth_class_rows = list(store.conn.execute(
        "SELECT truth_class, COUNT(*) AS n FROM annotations "
        "GROUP BY truth_class"))
    by_truth_class = {r["truth_class"] or "INFERENCE": r["n"]
                       for r in by_truth_class_rows}
    by_kind_rows = list(store.conn.execute(
        "SELECT kind, COUNT(*) AS n FROM annotations GROUP BY kind"))
    by_kind = {r["kind"] or "note": r["n"] for r in by_kind_rows}
    totals = {
        "total_notes":       int(total),
        "by_staleness":      by_staleness,
        "by_truth_class":    by_truth_class,
        "by_kind":           by_kind,
    }

    # 2. Recent notes — last N by created_at. We revalidate each to get
    # the CURRENT claim-level status without blindly trusting persisted
    # staleness.
    recent_rows = list(store.conn.execute(
        "SELECT * FROM annotations WHERE expires_at IS NULL "
        "OR expires_at > ? ORDER BY created_at DESC LIMIT ?",
        (time.time(), max_recent)))
    recent_notes: List[Dict[str, Any]] = []
    target_risk: Dict[str, Dict[str, Any]] = {}
    all_contradicted: List[Dict[str, Any]] = []
    seen_contradicted: int = 0

    for row in recent_rows:
        try:
            # Benchmark R3 fix: persist=True so the staleness column
            # (read cheaply by memory_header.build_header) stays in
            # sync with live claim verification. Without this, the
            # `repo_memory.contradicted_count` header reports 0 while
            # `notes` shows 1 — the two disagree because header reads
            # the stored column while notes revalidates live.
            res = _intg.revalidate_annotation(store, repo_root, dict(row),
                                               persist=True)
            status = res.now
            claim_overall = res.claim_overall_status
            claim_verdicts = res.claim_verdicts or []
        except Exception:
            status = row["staleness"] or "unknown"
            claim_overall = None
            claim_verdicts = []
        body_consistency = getattr(res, "body_consistency", {}) or {}
        recent_notes.append({
            "id":                 row["id"],
            "target":             row["target"],
            "kind":               row["kind"],
            "body_preview":       (row["body"] or "")[:200],
            "author":             row["author"],
            "created_at":         row["created_at"],
            "staleness":          status,
            "claim_overall":      claim_overall,
            "refuted_count":      sum(1 for v in claim_verdicts
                                       if v.get("status") == "REFUTED"),
            "verified_count":     sum(1 for v in claim_verdicts
                                       if v.get("status") == "VERIFIED"),
            # Audit fix #10 — surface body-text staleness alongside the
            # claim/fingerprint signals so a note whose prose cites
            # vanished code is visible at a glance.
            "body_stale":         bool(body_consistency.get("is_stale")),
            "body_missing_count": (len(body_consistency.get("missing_identifiers") or [])
                                    + len(body_consistency.get("missing_paths") or [])
                                    + len(body_consistency.get("line_drift") or [])),
        })
        # Accumulate target risk in-flight.
        _accumulate_target_risk(target_risk, row, status, claim_overall,
                                 claim_verdicts)

    # 3. Contradicted. Scan notes with AT LEAST one structured claim and
    # re-verify each — persisted staleness on disk may still read 'fresh'
    # on notes we haven't revalidated this session. Bounded by
    # max_contradicted * 3 scan budget so huge note tables don't dominate
    # the call.
    scan_budget = max(max_contradicted * 3, 50)
    candidate_rows = list(store.conn.execute(
        "SELECT * FROM annotations WHERE evidence IS NOT NULL "
        "AND evidence != '' ORDER BY created_at DESC LIMIT ?",
        (scan_budget,)))
    seen_ids: set = {n["id"] for n in recent_notes}
    for row in candidate_rows:
        if seen_contradicted >= max_contradicted:
            break
        try:
            # persist=True: same reasoning as above — the stored
            # staleness column needs to match live verification.
            res = _intg.revalidate_annotation(store, repo_root, dict(row),
                                               persist=True)
            if res.claim_overall_status != _claims.CONTRADICTED \
                    and res.now != "contradicted":
                continue
            first_refuted = next(
                (v for v in (res.claim_verdicts or [])
                 if v.get("status") == "REFUTED"),
                None)
            all_contradicted.append({
                "id":           row["id"],
                "target":       row["target"],
                "kind":         row["kind"],
                "body_preview": (row["body"] or "")[:200],
                "author":       row["author"],
                "created_at":   row["created_at"],
                "first_refuted_claim": ({
                    "subject":   first_refuted.get("subject"),
                    "predicate": first_refuted.get("predicate"),
                    "object":    first_refuted.get("object"),
                    "reason":    first_refuted.get("reason"),
                } if first_refuted else None),
            })
            seen_contradicted += 1
            # Risk aggregation: only add if this row wasn't already
            # aggregated via the recent_notes loop.
            if row["id"] not in seen_ids:
                _accumulate_target_risk(target_risk, row, res.now,
                                         res.claim_overall_status,
                                         res.claim_verdicts or [])
        except Exception:
            continue

    # 4. Risk-ranked targets: highest risk = contradicted notes +
    # refuted-claim count + stale-note count on that target.
    ranked = sorted(target_risk.values(),
                     key=lambda r: (-r["contradicted_count"],
                                     -r["refuted_claim_count"],
                                     -r["stale_note_count"],
                                     r["target"]))
    risk_targets = ranked[:max_risk_targets]

    # 5. Authors — who has been contributing notes?
    author_rows = list(store.conn.execute(
        "SELECT author, COUNT(*) AS n FROM annotations "
        "WHERE author IS NOT NULL GROUP BY author ORDER BY n DESC LIMIT ?",
        (max_distinct_authors,)))
    authors = [{"author": r["author"], "note_count": r["n"]}
                for r in author_rows]

    # 6. Index summary (surface for context — the agent sees memory
    # health alongside memory contents).
    try:
        binding_summary = _binding.binding_summary(store)
    except Exception:
        binding_summary = {}
    try:
        files_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        symbols_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    except Exception:
        files_count = 0
        symbols_count = 0
    index_summary = {
        "files": files_count,
        "symbols": symbols_count,
        "ref_binding": binding_summary,
    }

    # 7. Next-steps hint.
    hints: List[Optional[str]] = []
    if total == 0:
        hints.append(
            "No notes yet in this repo. As you investigate code and "
            "reach non-trivial conclusions, save them with "
            "`projmem note add <target> --kind note ... --claims file.json "
            "--truth-class FACT`. Future sessions will verify them "
            "automatically.")
    else:
        if all_contradicted:
            hints.append(
                f"{len(all_contradicted)} note(s) currently contradicted "
                "— prior FACT-class claims refuted. Inspect with "
                "`projmem audit <target>` per target.")
        if risk_targets:
            top = risk_targets[0]
            hints.append(
                f"Highest-risk target: {top['target']!r} "
                f"(contradicted={top['contradicted_count']}, "
                f"refuted={top['refuted_claim_count']}, "
                f"stale={top['stale_note_count']}). "
                "Start there.")
        # Use internal_bound_pct (bound / refs whose name has a def in the
        # index) rather than the headline bound_pct. The headline number
        # is dragged down by external/framework refs (React hooks, stdlib
        # calls) that can never bind, which the audit on /tmp/projectX
        # showed produced a misleading "low binding" hint when the
        # internal binding was actually 99.9%.
        internal_pct = binding_summary.get("internal_bound_pct")
        if internal_pct is not None and internal_pct < 80:
            hints.append(
                f"Internal ref binding is low ({internal_pct}%). "
                "Path aliases or unresolved imports may be reducing graph "
                "quality. Check `projmem stats` → `ref_binding` and config.")

    out: Dict[str, Any] = {
        "schema_version": 1,
        "totals":         totals,
        "recent_notes":   recent_notes,
        "contradicted":   all_contradicted,
        "risk_targets":   risk_targets,
        "authors":        authors,
        "index_summary":  index_summary,
        "next_steps_hint": [h for h in hints if h],
    }
    return out


def _accumulate_target_risk(target_risk: Dict[str, Dict[str, Any]],
                             row, status: str,
                             claim_overall: Optional[str],
                             claim_verdicts: List[Dict[str, Any]]) -> None:
    """Accumulate per-target risk signal for the risk_targets ranking."""
    t = row["target"]
    entry = target_risk.setdefault(t, {
        "target":                t,
        "note_count":            0,
        "stale_note_count":      0,
        "contradicted_count":    0,
        "refuted_claim_count":   0,
        "verified_claim_count":  0,
    })
    entry["note_count"] += 1
    if status in ATTENTION_STALENESS:
        entry["stale_note_count"] += 1
    if claim_overall == "contradicted" or status == "contradicted":
        entry["contradicted_count"] += 1
    for v in claim_verdicts:
        if v.get("status") == "REFUTED":
            entry["refuted_claim_count"] += 1
        elif v.get("status") == "VERIFIED":
            entry["verified_claim_count"] += 1
