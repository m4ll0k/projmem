"""Critical-note state machine — cosigner gate, review cadence, blast-radius.

Public surface used by CLI + mutation_verbs:

    add_critical(store, path, *, reason, category, approved_by, ...)
    list_critical(store)
    mark_reviewed(store, ann_id)
    pending_review(store)
    critical_notes_for_path(store, path)
    critical_notes_via_blast_radius(store, path, *, hops=1)
    build_critical_prelude(critical_rows) -> str

Errors inherit from :class:`CriticalError` so the CLI can map them to
structured envelopes the same way ``mutation_verbs.MutationError`` is.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence


CRITICAL_KIND = "critical"
DEFAULT_REVIEW_WINDOW_DAYS = 90
DEFAULT_BLAST_RADIUS_HOPS = 1
CATEGORIES = (
    "security", "compliance", "performance",
    "business_logic", "data_integrity", "other",
)


class CriticalError(Exception):
    code: str = "critical-error"

    def envelope(self) -> Dict[str, Any]:
        return {"error": self.code, "message": str(self)}


class CosignerRequiredError(CriticalError):
    code = "cosigner-required"


class CategoryError(CriticalError):
    code = "invalid-category"


class ReasonTooShortError(CriticalError):
    code = "reason-too-short"


class CriticalNotFoundError(CriticalError):
    code = "critical-not-found"


# Critical reasons get a stricter floor than guidance — they're the
# load-bearing context, so a 20-char min would be too lax.
MIN_REASON_CHARS = 40


def _validate_inputs(*, reason: str, category: str,
                     approved_by: Sequence[str], self_cosign: bool) -> None:
    cleaned = (reason or "").strip()
    if len(cleaned) < MIN_REASON_CHARS:
        raise ReasonTooShortError(
            f"critical reason must be ≥ {MIN_REASON_CHARS} chars; "
            f"got {len(cleaned)}. Critical notes are load-bearing — "
            f"describe the constraint, the incident, and the consequence."
        )
    if category not in CATEGORIES:
        raise CategoryError(
            f"category must be one of {list(CATEGORIES)}; got {category!r}"
        )
    if not approved_by and not self_cosign:
        raise CosignerRequiredError(
            "critical notes require ≥ 1 --approved-by <user> OR "
            "--self-cosign (with explicit confirmation). The cosigner gate "
            "prevents critical-note inflation; without it, the kind "
            "collapses to ordinary guidance."
        )


def add_critical(
    store, target: str, *, reason: str, category: str,
    approved_by: Sequence[str] = (),
    self_cosign: bool = False,
    incident_refs: Sequence[str] = (),
    review_window_days: int = DEFAULT_REVIEW_WINDOW_DAYS,
    blast_radius_hops: int = DEFAULT_BLAST_RADIUS_HOPS,
    blocks_edits: bool = True,
    severity: str = "critical",
) -> Dict[str, Any]:
    _validate_inputs(reason=reason, category=category,
                     approved_by=approved_by, self_cosign=self_cosign)
    approved_list = list(approved_by) or (["@self-cosign"] if self_cosign else [])
    ann_id = store.add_annotation(
        target=target, kind=CRITICAL_KIND, body=reason,
        truth_class="ASSUMPTION", severity=severity,
    )
    store.conn.execute(
        "UPDATE annotations SET category=?, incident_refs=?, approved_by=?, "
        "last_reviewed_at=?, review_window_days=?, blast_radius_hops=?, "
        "blocks_edits=? WHERE id=?",
        (category,
         json.dumps(list(incident_refs)) if incident_refs else None,
         json.dumps(approved_list),
         time.time(), int(review_window_days), int(blast_radius_hops),
         1 if blocks_edits else 0,
         ann_id),
    )
    store.conn.commit()
    return {
        "id":                  ann_id,
        "target":              target,
        "category":            category,
        "approved_by":         approved_list,
        "blocks_edits":        bool(blocks_edits),
        "blast_radius_hops":   int(blast_radius_hops),
        "review_window_days":  int(review_window_days),
    }


def _row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("incident_refs", "approved_by"):
        v = d.get(k)
        if v:
            try:
                d[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    d["blocks_edits"] = bool(d.get("blocks_edits", 0))
    return d


def list_critical(store) -> List[Dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM annotations WHERE kind=? ORDER BY created_at DESC",
        (CRITICAL_KIND,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_reviewed(store, ann_id: int) -> Dict[str, Any]:
    cur = store.conn.execute(
        "UPDATE annotations SET last_reviewed_at=? "
        "WHERE id=? AND kind=?",
        (time.time(), ann_id, CRITICAL_KIND),
    )
    store.conn.commit()
    if cur.rowcount == 0:
        raise CriticalNotFoundError(
            f"no critical note with id={ann_id}"
        )
    return {"id": ann_id, "reviewed_at": time.time()}


def pending_review(store) -> List[Dict[str, Any]]:
    now = time.time()
    rows = store.conn.execute(
        "SELECT * FROM annotations WHERE kind=? "
        "AND last_reviewed_at IS NOT NULL "
        "AND review_window_days IS NOT NULL "
        "AND (last_reviewed_at + review_window_days * 86400) < ?",
        (CRITICAL_KIND, now),
    ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["overdue_by_days"] = (
            (now - d["last_reviewed_at"]) / 86400
            - (d.get("review_window_days") or DEFAULT_REVIEW_WINDOW_DAYS)
        )
        out.append(d)
    return out


def critical_notes_for_path(store, path: str) -> List[Dict[str, Any]]:
    """Critical notes whose target is the path, a parent dir, or `@project`."""
    import os
    targets = [path, path + "#%"]
    cur = path
    while True:
        d = os.path.dirname(cur)
        if not d or d == cur:
            break
        targets.append(d + "/")
        cur = d
    targets.append("@project")
    rows = store.conn.execute(
        f"SELECT * FROM annotations WHERE kind='critical' "
        f"AND target IN ({','.join('?' * len(targets))}) "
        f"ORDER BY created_at DESC",
        tuple(targets),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def critical_notes_via_blast_radius(
    store, path: str, *, hops: int = 1,
) -> List[Dict[str, Any]]:
    """Critical notes on 1-hop reverse-deps of ``path``.

    For each neighbor we tag the result with ``via='blast-radius'`` so
    the caller can render `"1-hop dependent of critical file X"`.
    """
    if hops < 1:
        return []
    rows = store.conn.execute(
        "SELECT DISTINCT src FROM edges WHERE dst=? AND type='imports' "
        "UNION SELECT DISTINCT dst FROM edges WHERE src=? AND type='imports'",
        (path, path),
    ).fetchall()
    neighbors = [r[0] for r in rows if r[0]]
    if not neighbors:
        return []
    placeholders = ",".join("?" * len(neighbors))
    crit_rows = store.conn.execute(
        f"SELECT * FROM annotations WHERE kind='critical' "
        f"AND target IN ({placeholders})",
        tuple(neighbors),
    ).fetchall()
    out = []
    for r in crit_rows:
        d = _row_to_dict(r)
        d["via"] = "blast-radius"
        out.append(d)
    return out


def build_critical_prelude(rows: Sequence[Dict[str, Any]]) -> str:
    """Format a list of critical rows as the ⚠ CRITICAL CONTEXT block."""
    if not rows:
        return ""
    lines = ["⚠ CRITICAL CONTEXT — engage before editing:"]
    for r in rows:
        marker = (" (1-hop dependent — blast-radius)"
                   if r.get("via") == "blast-radius" else "")
        cat = r.get("category") or "other"
        approved = r.get("approved_by") or []
        approved_msg = (
            f"approved by {', '.join(approved)}" if approved else "no cosigners"
        )
        body = (r.get("body") or "").strip()
        if len(body) > 400:
            body = body[:397] + "..."
        lines.append(
            f"  • [{cat}]{marker} {r.get('target')}: {body}"
        )
        lines.append(f"      ({approved_msg}; blocks_edits="
                     f"{r.get('blocks_edits')})")
    lines.append("")
    lines.append("Required: (1) state intended change, "
                 "(2) confirm it does NOT touch these concerns, "
                 "(3) halt if it does.")
    return "\n".join(lines)
