"""projmem/session.py — one-call agent bootstrap.

Goal: an LLM starting work on a target should make EXACTLY ONE projmem
call to know everything relevant. Chaining 5 calls (`doctor`, `audit`,
`integrity`, `audit-trail`, `pack`) wastes context and tempts the agent
to skip steps.

The session blob is bounded:

  - doctor: HIGH/WARNING findings only, capped at top 5
  - notes_on_target: every note's per-claim verdicts, capped at 10 notes
  - integrity_score: number + factors + guidance
  - recent_agent_activity: last 5 audit-trail entries on this target
  - direct_neighbors: forward + reverse deps (counts + first 5 each)

Total payload is typically 2-5 KB on a healthy target — small enough to
include in any system message AND in every tool-call response.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional


def build_session(store, repo_root: str, target: str,
                  max_notes: int = 10,
                  max_audit_entries: int = 5,
                  max_neighbors_each: int = 5
                  ) -> Dict[str, Any]:
    """Bundle everything an agent needs to start work on `target`.

    Bounded; safe to call as the first projmem invocation on every task.
    """
    from . import doctor as _doctor
    from . import integrity as _intg
    from . import claims as _claims
    from . import graph as _graph
    from . import freshness as _fresh

    # Doctor — only HIGH + WARNING findings (skip INFO chatter for the agent).
    # Signature is `run(cfg, store, *, skip_stale_check=...)`.
    doctor_report = _doctor.run(
        _make_cfg(repo_root), store, skip_stale_check=True)
    # Split actionable findings by relevance: TASK-RELEVANT goes inline
    # in the session blob (the agent must see it before editing), INFRA
    # goes under a separate `infra_health` key so an agent reading
    # `actionable_findings` doesn't get distracted by 49% ref binding
    # when triaging a security review. Codes that gate actual edits
    # (stale files on the target, drifted_on_disk, foreign_index) stay
    # in actionable; codes about the indexer itself (ref binding %,
    # parser coverage %, oversize files, artifact bleed) go to infra.
    _INFRA_CODES = {
        "low_internal_ref_binding",
        "low_ref_binding",
        "low_parser_coverage",
        "high_regex_fallback",
        "oversize_skipped_files",
        "artifact_bleed",
        "treesitter_unavailable",
    }
    all_findings = [f for f in doctor_report.get("findings", [])
                     if f.get("severity") in ("high", "warning")]
    actionable = [f for f in all_findings
                   if f.get("code") not in _INFRA_CODES][:5]
    infra_health = [f for f in all_findings
                     if f.get("code") in _INFRA_CODES][:5]

    # Notes on target — verify each + roll up.
    note_rows = store.list_annotations(target=target,
                                        include_expired=False)
    if not note_rows and target:
        note_rows = store.annotations_for_pack(
            file=target if "/" in target else None,
            symbol_ids=[target] if "#" not in target and "/" not in target
                       else None,
            names_in_file=None)
    notes_summary: List[Dict[str, Any]] = []
    refuted_subjects: List[str] = []
    contradicted_ids: List[int] = []
    for ann in note_rows[:max_notes]:
        try:
            res = _intg.revalidate_annotation(store, repo_root, ann,
                                               persist=False)
        except Exception as e:
            notes_summary.append({"id": ann.get("id"), "error": str(e)})
            continue
        entry: Dict[str, Any] = {
            "id":          ann.get("id"),
            "kind":        ann.get("kind"),
            "body":        (ann.get("body") or "")[:200],
            "author":      ann.get("author"),
            "staleness":   res.now,
            "confidence":  res.new_confidence,
            # Surface truth_class so the agent can immediately tell FACT
            # claims (must hold) apart from INFERENCE / ASSUMPTION /
            # UNKNOWN. Codex audit on /tmp/projectX flagged the absence of
            # this field as forcing a fallback to `note list` for state
            # recovery.
            "truth_class": ann.get("truth_class") or "INFERENCE",
        }
        if res.claim_verdicts:
            entry["claim_overall_status"] = res.claim_overall_status
            entry["verified_count"] = sum(
                1 for v in res.claim_verdicts if v.get("status") == "VERIFIED")
            entry["refuted_count"] = sum(
                1 for v in res.claim_verdicts if v.get("status") == "REFUTED")
            entry["uncheckable_count"] = sum(
                1 for v in res.claim_verdicts
                if v.get("status") == "UNCHECKABLE")
            # First refuted claim per note for at-a-glance signal.
            first_refuted = next(
                (v for v in res.claim_verdicts if v.get("status") == "REFUTED"),
                None)
            if first_refuted:
                entry["first_refuted_claim"] = {
                    "subject":   first_refuted.get("subject"),
                    "predicate": first_refuted.get("predicate"),
                    "object":    first_refuted.get("object"),
                    "reason":    first_refuted.get("reason"),
                }
                refuted_subjects.append(first_refuted.get("subject"))
            if res.claim_overall_status == _claims.CONTRADICTED:
                contradicted_ids.append(int(ann["id"]))
        notes_summary.append(entry)

    # Integrity score (lightweight wrapper around per-target compute).
    try:
        isc = _intg.integrity_score(store, repo_root, target)
        integrity_block = {
            "score":    isc.score,
            "factors":  isc.factors,
            "guidance": isc.guidance,
        }
    except Exception as e:
        integrity_block = {"error": str(e)}

    # Recent agent activity on this target.
    recent: List[Dict[str, Any]] = []
    try:
        rows = list(store.conn.execute(
            "SELECT command, args, target, ts FROM audit_trail "
            "WHERE target=? OR (target IS NULL AND args LIKE ?) "
            "ORDER BY ts DESC LIMIT ?",
            (target, f"%{target}%", max_audit_entries)))
        for r in rows:
            recent.append({
                "command": r["command"],
                "ts":      r["ts"],
                "target":  r["target"],
            })
    except Exception:
        pass

    # Direct neighbors (forward + reverse). Bounded.
    neighbors: Dict[str, Any] = {}
    try:
        if "/" in target:
            file_target = target.split("#", 1)[0]
            rev = _graph.reverse_deps(store, file_target)[:max_neighbors_each]
            fwd = _graph.forward_deps(store, file_target)[:max_neighbors_each]
            neighbors = {
                "reverse_dep_count": len(_graph.reverse_deps(store, file_target)),
                "direct_dep_count":  len(_graph.forward_deps(store, file_target)),
                "reverse_sample":    [{"file": d.get("file"),
                                        "type":  d.get("type")} for d in rev],
                "direct_sample":     [{"file": d.get("file"),
                                        "type":  d.get("type")} for d in fwd],
            }
    except Exception:
        pass

    # Read-time freshness probe on the target itself.
    freshness = None
    try:
        if "/" in target:
            file_target = target.split("#", 1)[0]
            stale = _fresh.check_paths(store, repo_root, [file_target])
            if stale:
                freshness = _fresh.freshness_warning(stale)
    except Exception:
        pass

    # Narrow-index detection: when a target file is in the index but has
    # ZERO direct deps AND ZERO reverse deps, the most likely cause is
    # that `projmem index --include <glob>` was used with too narrow a
    # scope and we're missing the neighbor dirs. Real-world feedback from
    # external testing flagged this as a non-obvious failure mode
    # ("reverse deps disappeared after a narrow re-index"). We surface
    # the hint only when (a) target is an actual indexed file and (b)
    # the index contains other files too — to avoid false alarms on
    # genuinely isolated single-file repos.
    narrow_index_hint = False
    try:
        is_file_target = "/" in target and "#" not in target
        if (is_file_target
                and neighbors
                and neighbors.get("direct_dep_count", 0) == 0
                and neighbors.get("reverse_dep_count", 0) == 0):
            file_target = target.split("#", 1)[0]
            in_index = store.conn.execute(
                "SELECT 1 FROM files WHERE path=? LIMIT 1",
                (file_target,)).fetchone()
            total_files = store.conn.execute(
                "SELECT COUNT(*) AS n FROM files").fetchone()["n"]
            if in_index and total_files > 1:
                narrow_index_hint = True
    except Exception:
        pass

    out: Dict[str, Any] = {
        "schema_version":  1,
        "target":          target,
        "doctor_summary": {
            "overall":            doctor_report.get("overall"),
            "actionable_findings": actionable,
            "infra_health":        infra_health,
            "severity_counts":    doctor_report.get("severity_counts"),
        },
        "notes_on_target":          notes_summary,
        "notes_total":              len(note_rows),
        "refuted_subjects":         refuted_subjects,
        "contradicted_note_ids":    contradicted_ids,
        "integrity":                integrity_block,
        "recent_agent_activity":    recent,
        "neighbors":                neighbors,
        "next_steps_hint": [
            ("Address freshness_warning before trusting other reads"
             if freshness else None),
            (f"Investigate {len(refuted_subjects)} refuted claim(s) — they "
             "indicate prior beliefs no longer hold"
             if refuted_subjects else None),
            ("Index scope appears narrow: this file has zero indexed "
             "neighbors. Re-run `projmem init --reindex` (or `projmem "
             "index`) WITHOUT a narrow `--include` filter so cross-file "
             "deps become visible."
             if narrow_index_hint else None),
            ("Run `projmem doctor` for full health detail"
             if doctor_report.get("severity_counts", {}).get("high", 0) > 0
             else None),
            ("No notes yet on this target — consider saving conclusions "
             "via `projmem note add --claims` after investigating"
             if not note_rows else None),
        ],
    }
    out["next_steps_hint"] = [s for s in out["next_steps_hint"] if s]
    if freshness:
        out["freshness_warning"] = freshness
    if narrow_index_hint:
        out["index_scope_warning"] = {
            "severity": "warning",
            "code":     "narrow_index_scope",
            "message":  "Target is in the index but has zero direct+reverse "
                        "deps. The index scope was likely narrowed (e.g. "
                        "via `projmem index --include`) and is missing "
                        "neighbor files.",
            "suggestion": "Re-run `projmem init --reindex` or `projmem "
                          "index` without the include filter.",
        }
    return out


def _make_cfg(repo_root: str):
    """Tiny helper: doctor.run() expects a cfg-shaped object with .root.
    We avoid pulling the full Config to keep this module light."""
    class _C:
        pass
    c = _C()
    c.root = repo_root
    return c
