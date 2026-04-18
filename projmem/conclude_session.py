"""projmem/conclude-session — extract durable conclusions from a
conversation transcript and save them as structured notes.

Tier-1A feature. Closes the capture gap: instead of hoping the agent
remembers to run `projmem conclude` between actions, feed the whole
session transcript through this command at session end. All
confidently-stated, verifiable claims get saved automatically.

Policy choices:
  - Only VERIFIED claims become notes. REFUTED claims are DROPPED —
    they're either the agent's mistakes (don't persist them) or
    documentation of a wrong belief (belongs in a `refute` kind, not
    auto-saved).
  - UNCHECKABLE claims are dropped by default. A `--include-uncheckable`
    flag can surface them as lower-confidence INFERENCE notes.
  - Idempotency: hash (subject, predicate, object) per note; skip if
    the exact claim already exists on that target from any prior
    session-extracted note.
  - Every saved note carries `author=session-<timestamp>` and
    `truth_class=INFERENCE`. Not FACT, because transcript prose is not
    the same as explicit `@predicate(...)` authoring intent.
"""
from __future__ import annotations
import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple

from . import factcheck as _fc


def _claims_by_target(fc_output: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Group VERIFIED claims by target. Target heuristic:
      * `defined-at` / `exported-from` / `env-read-at` / `flag-read-at`
        → target = the file path in `object`
      * `reexported-via` → target = the object-side file
      * `reverse-dependency-of` → target = the subject file
      * default → target = `@project`
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for c in fc_output.get("claims") or []:
        if c.get("status") != "VERIFIED":
            continue
        obj = c.get("object") or ""
        pred = c.get("predicate") or ""
        target: Optional[str] = None
        if pred in ("defined-at", "env-read-at", "flag-read-at"):
            # object = "file:line" → target = file
            target = obj.split(":", 1)[0]
        elif pred == "exported-from":
            target = obj
        elif pred == "reexported-via":
            target = obj  # barrel file
        elif pred == "reverse-dependency-of":
            target = c.get("subject")
        if not target:
            target = "@project"
        grouped.setdefault(target, []).append(c)
    return grouped


def _claim_hash(subject: str, predicate: str, obj: str) -> str:
    """Stable hash of a claim triple — used for idempotency keys."""
    return hashlib.sha1(
        f"{subject}|{predicate}|{obj}".encode("utf-8")).hexdigest()


def _already_captured(store, target: str, claim_hash: str) -> bool:
    """True iff a prior session-extracted note on this target already
    carries a claim with the same triple hash. Read from evidence
    column."""
    import json as _json
    rows = store.conn.execute(
        "SELECT evidence FROM annotations "
        "WHERE target=? AND author LIKE 'session-%'",
        (target,)).fetchall()
    for r in rows:
        ev = r["evidence"]
        if not ev:
            continue
        try:
            data = _json.loads(ev) if isinstance(ev, str) else ev
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            h = _claim_hash(
                str(item.get("subject") or ""),
                str(item.get("predicate") or ""),
                str(item.get("object") or ""))
            if h == claim_hash:
                return True
    return False


def conclude_session(store, repo_root: str, transcript: str, *,
                      author: Optional[str] = None,
                      max_claims: int = 100,
                      dry_run: bool = False) -> Dict[str, Any]:
    """Extract durable conclusions from a transcript and save them.

    Returns a report of what was saved, skipped, and dropped.
    """
    if author is None:
        author = f"session-{int(time.time())}"

    # 1. Run the full claim extractor on the transcript.
    fc = _fc.fact_check(store, transcript or "",
                         repo_root=repo_root,
                         max_claims=max_claims,
                         include_bare_file_lines=False)

    # 2. Group VERIFIED claims by target.
    grouped = _claims_by_target(fc)

    created: List[Dict[str, Any]] = []
    skipped_duplicate: List[Dict[str, Any]] = []
    dropped_refuted: int = fc.get("refuted", 0)
    dropped_uncheckable: int = fc.get("uncheckable", 0)

    for target, claims in grouped.items():
        novel: List[Dict[str, Any]] = []
        for c in claims:
            h = _claim_hash(c.get("subject", ""),
                             c.get("predicate", ""),
                             c.get("object", ""))
            if _already_captured(store, target, h):
                skipped_duplicate.append({
                    "target": target, "claim": c,
                    "reason": "already-captured-in-prior-session",
                })
                continue
            novel.append(c)
        if not novel:
            continue
        # Build body: one-line summary listing each claim.
        lines = [f"[session-extracted] {len(novel)} verified claim(s):"]
        for c in novel:
            lines.append(
                f"  - {c.get('predicate')}: "
                f"{c.get('subject')} → {c.get('object')}")
        body = "\n".join(lines)
        if dry_run:
            created.append({"target": target, "claim_count": len(novel),
                             "body_preview": body[:200],
                             "dry_run": True})
            continue
        try:
            ann_id = store.add_annotation(
                target=target, kind="note", body=body,
                author=author, expires_at=None,
                confidence=0.7,
                evidence=novel,
                assumptions=None,
                scope=None,
                truth_class="INFERENCE",
                fingerprint=None)
            created.append({"id": ann_id, "target": target,
                             "claim_count": len(novel)})
        except Exception as e:
            created.append({"target": target, "error": str(e)})

    if not dry_run:
        try:
            store.conn.commit()
        except Exception:
            pass

    return {
        "author":             author,
        "transcript_bytes":   len(transcript or ""),
        "extracted_count":    fc.get("extracted_count", 0),
        "verified_count":     fc.get("verified", 0),
        "created_count":      len([c for c in created if c.get("id")]
                                   if not dry_run
                                   else [c for c in created if c.get("dry_run")]),
        "created":            created,
        "skipped_duplicate":  skipped_duplicate,
        "dropped_refuted":    dropped_refuted,
        "dropped_uncheckable": dropped_uncheckable,
        "dry_run":            dry_run,
        "note": ("Only VERIFIED claims are saved. REFUTED claims are "
                 "dropped (they'd be agent hallucinations). UNCHECKABLE "
                 "claims are dropped unless --include-uncheckable set. "
                 "Use `projmem note-verify <target>` to re-verify later."),
    }
