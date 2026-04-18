"""projmem/render.py — shared rendering primitives for `graph` and `report`.

Both visual artifacts (graph SVG/DOT/MMD and the REPORT.md digest) need
the same building blocks: how to color a symbol by claim status, how to
classify an edge by confidence, how to discover which annotations
attach to a target. Centralized here so both surfaces stay consistent
and a status convention change ripples to one place.

No graphviz / pydot dependency at import time — those are lazy-loaded
in graph_viz.py only when SVG rendering is requested.
"""
from __future__ import annotations
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# Claim-aware node fill colors. Hex-coded so DOT, Mermaid and Markdown
# (which only takes emoji) can all derive a consistent legend.
COLOR_PROVED      = "#2ecc71"   # green
COLOR_REFUTED     = "#e74c3c"   # red
COLOR_AMBIGUOUS   = "#f1c40f"   # yellow
COLOR_NONE        = "#bdc3c7"   # grey
COLOR_DRIFT_RING  = "#000000"   # black ring around node when on-disk drift

EMOJI = {
    "PROVED":     "[GREEN]",
    "REFUTED":    "[RED]",
    "AMBIGUOUS":  "[YELLOW]",
    "NONE":       "[GREY]",
    "DRIFT":      "[DRIFT]",
}

# Edge style by confidence.
EDGE_STYLE = {
    "EXTRACTED":  "solid",
    "INFERRED":   "dashed",
    "AMBIGUOUS":  "dotted",
}


def status_for_target(store, repo_root: str, target: str,
                      *, persist: bool = False) -> Dict[str, Any]:
    """Compute the aggregate claim status for one target string.

    Walks every non-expired annotation whose `target` exactly matches
    `target`, revalidates each against the current index (so we report
    LIVE status, not last-persisted), and reduces to a single label:

      - "REFUTED"   if any claim is REFUTED  (drift detected)
      - "PROVED"    if at least one claim is VERIFIED and none REFUTED
      - "AMBIGUOUS" if note staleness is contradicted/strongly_stale
                    or claims are all UNCHECKABLE/mixed without VERIFIED
      - "NONE"      no annotations on this target

    Returns:
      {label, note_count, refuted_claims, verified_claims,
       contradicted_notes, drifted_on_disk, last_verified_at}
    """
    from . import integrity as _intg
    rows = store.list_annotations(target=target, include_expired=False)
    out: Dict[str, Any] = {
        "label":              "NONE",
        "note_count":         len(rows),
        "refuted_claims":     0,
        "verified_claims":    0,
        "contradicted_notes": 0,
        "drifted_on_disk":    False,
        "last_verified_at":   None,
    }
    if not rows:
        return out
    any_refuted = False
    any_verified = False
    any_contradicted = False
    last_verified: Optional[float] = None
    for row in rows:
        try:
            res = _intg.revalidate_annotation(store, repo_root, dict(row),
                                              persist=persist)
        except Exception:
            continue
        for v in res.claim_verdicts or []:
            if v.get("status") == "REFUTED":
                out["refuted_claims"] += 1
                any_refuted = True
            elif v.get("status") == "VERIFIED":
                out["verified_claims"] += 1
                any_verified = True
        if res.claim_overall_status == "contradicted" or res.now == "contradicted":
            any_contradicted = True
            out["contradicted_notes"] += 1
        lv = row.get("last_verified_at")
        if lv and (last_verified is None or lv > last_verified):
            last_verified = lv
    out["last_verified_at"] = last_verified
    if any_refuted or any_contradicted:
        out["label"] = "REFUTED"
    elif any_verified:
        out["label"] = "PROVED"
    else:
        out["label"] = "AMBIGUOUS"
    return out


def status_for_targets(store, repo_root: str, targets: Iterable[str],
                       *, persist: bool = False
                       ) -> Dict[str, Dict[str, Any]]:
    """Batch wrapper around `status_for_target`. Skips empty targets."""
    out: Dict[str, Dict[str, Any]] = {}
    for t in targets:
        if not t:
            continue
        out[t] = status_for_target(store, repo_root, t, persist=persist)
    return out


def drifted_paths(store, repo_root: str,
                  paths: Iterable[str]) -> Set[str]:
    """Return the subset of `paths` whose on-disk hash no longer matches
    the indexed hash. Used to draw the black "drift" ring on graph nodes.
    Best-effort: paths we can't read are silently skipped."""
    from .utils import hash_file
    drifted: Set[str] = set()
    seen: Set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        full = os.path.join(repo_root, path)
        if not os.path.isfile(full):
            # Indexed but missing on disk — treat as drifted so the user
            # sees the discrepancy.
            row = store.conn.execute(
                "SELECT 1 FROM files WHERE path=? LIMIT 1", (path,)).fetchone()
            if row is not None:
                drifted.add(path)
            continue
        row = store.conn.execute(
            "SELECT hash FROM files WHERE path=? LIMIT 1", (path,)).fetchone()
        if row is None:
            continue
        try:
            cur = hash_file(full)
        except OSError:
            continue
        if cur != row["hash"]:
            drifted.add(path)
    return drifted


def refcount_for_symbol(store, name: str) -> int:
    """Best-effort reference count: rows in `refs` matching this name.
    Used to size graph nodes (1 → small, 2-9 → medium, 10+ → large)."""
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM refs WHERE name=?", (name,)).fetchone()
    return int(row["n"]) if row else 0


def refcount_bucket(n: int) -> str:
    """Discrete size bucket so we don't generate one node size per ref."""
    if n >= 10:
        return "large"
    if n >= 2:
        return "medium"
    return "small"


# Width/height for graphviz node sizing. Tuned so a `large` symbol is
# visually unmissable without bloating the SVG canvas on small graphs.
NODE_SIZE = {
    "small":  (0.6, 0.4),
    "medium": (1.0, 0.6),
    "large":  (1.6, 0.9),
}


def color_for_label(label: str) -> str:
    return {
        "PROVED":    COLOR_PROVED,
        "REFUTED":   COLOR_REFUTED,
        "AMBIGUOUS": COLOR_AMBIGUOUS,
    }.get(label, COLOR_NONE)


def edge_style_for_confidence(conf: Optional[str]) -> str:
    """Map projmem's confidence labels onto a graphviz line style.
    `None`/unknown → solid (no signal worse than guessing)."""
    if not conf:
        return "solid"
    c = str(conf).upper()
    if c in EDGE_STYLE:
        return EDGE_STYLE[c]
    # Common synonyms used elsewhere in the codebase.
    if c in ("HIGH", "STRUCTURAL", "STRONG"):
        return "solid"
    if c in ("LOW", "HEURISTIC", "WEAK"):
        return "dashed"
    if c in ("MULTI", "AMBIG"):
        return "dotted"
    return "solid"


def fmt_age(ts: Optional[float], now: Optional[float] = None) -> str:
    """Human-readable age for a unix timestamp. Used in REPORT.md."""
    if not ts:
        return "never"
    now = now or time.time()
    delta = max(0, now - float(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
