"""projmem/report.py — one-page markdown digest at projmem-out/REPORT.md.

Mirrors the pattern of `notes_summary.build_summary()` but rendered as
prose for human consumption (PR comments, dashboards, CLAUDE.md
embeds). Sections kept stable so consumers can grep/diff between runs:

  1. Headline ribbon (claims tracked / REFUTED / freshness / drift)
  2. Top REFUTED claims this week
  3. God symbols (top refcount after artifact filter)
  4. Stale notes (target unchanged but evidence shifted)
  5. Knowledge gaps
       - isolated symbols (defined, zero refs — deletion candidates)
       - high-AMBIGUOUS-ref symbols
       - notes whose target name no longer has any def (likely renamed)
  6. Per-note verdict summary (latest N)

The knowledge-gap section is what graphify can't compute — it relies
on projmem's claim layer. Highlighted as the wedge.
"""
from __future__ import annotations
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from . import render as _render


WEEK_SECONDS = 7 * 24 * 3600


def build_report(store, repo_root: str,
                 max_refuted: int = 10,
                 max_god_symbols: int = 10,
                 max_stale: int = 10,
                 max_isolated: int = 10,
                 max_ambiguous: int = 10,
                 max_renamed: int = 10,
                 max_recent_notes: int = 10) -> Dict[str, Any]:
    """Compute the full report payload (sections + headline ribbon).
    Pure data — caller chooses to render markdown or emit JSON.
    """
    from . import notes_summary as _ns
    from . import integrity as _intg
    from . import artifacts as _artifacts

    summary = _ns.build_summary(store, repo_root,
                                max_recent=max_recent_notes,
                                max_contradicted=max_refuted,
                                max_risk_targets=max_god_symbols)

    # Headline ribbon — counts that drive the PR/dashboard line.
    refuted_this_week = _refuted_this_week(store, repo_root, max_refuted)
    drift_count, drifted_files = _count_drift(store, repo_root)
    freshness_count = _count_stale_files(store)
    headline = {
        "claims_tracked":     _claims_total(store),
        "notes_total":        summary["totals"]["total_notes"],
        "refuted_this_run":   len(refuted_this_week),
        "freshness_warnings": freshness_count,
        "drifted_on_disk":    drift_count,
    }

    # God symbols — top refcount (after artifact filter).
    god_symbols = _god_symbols(store, max_god_symbols, _artifacts)

    # Stale notes — target unchanged but evidence shifted.
    stale_notes = _stale_notes(store, repo_root, max_stale)

    # Knowledge gaps.
    isolated = _isolated_symbols(store, max_isolated, _artifacts)
    ambiguous = _ambiguous_refs(store, max_ambiguous)
    renamed = _renamed_notes(store, max_renamed)

    return {
        "schema_version":     1,
        "generated_at":       time.time(),
        "headline":           headline,
        "refuted_this_week":  refuted_this_week,
        "god_symbols":        god_symbols,
        "stale_notes":        stale_notes,
        "knowledge_gaps": {
            "isolated_symbols":   isolated,
            "ambiguous_refs":     ambiguous,
            "notes_target_gone":  renamed,
        },
        "recent_notes":       summary["recent_notes"],
        "risk_targets":       summary["risk_targets"],
        "drifted_files":      drifted_files[:25],
    }


def render_markdown(report: Dict[str, Any], repo_root: str) -> str:
    """Render the markdown REPORT.md from the structured payload."""
    h = report["headline"]
    out: List[str] = []
    out.append("# projmem REPORT")
    out.append("")
    out.append(f"_Generated_: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report['generated_at']))}")
    out.append(f"_Repo_: `{repo_root}`")
    out.append("")
    out.append("## Headline")
    out.append("")
    out.append(f"- **Claims tracked**: {h['claims_tracked']}")
    out.append(f"- **Notes total**: {h['notes_total']}")
    out.append(f"- **REFUTED this week**: {h['refuted_this_run']}")
    out.append(f"- **Freshness warnings**: {h['freshness_warnings']}")
    out.append(f"- **Drifted on disk**: {h['drifted_on_disk']}")
    out.append("")
    out.append(_legend_block())
    out.append("")

    out.append("## Top REFUTED claims (last 7 days)")
    out.append("")
    if not report["refuted_this_week"]:
        out.append("_No REFUTED claims in the last week — index and notes agree._")
    else:
        out.append("| Target | Note kind | Refuted predicate | Reason |")
        out.append("| --- | --- | --- | --- |")
        for r in report["refuted_this_week"]:
            target = _md_escape(r.get("target", ""))
            kind = r.get("kind", "")
            pred = r.get("first_refuted_claim", {}) or {}
            predicate = (f"`{pred.get('predicate', '')}({pred.get('subject', '')}, "
                         f"{pred.get('object', '')})`") if pred else ""
            reason = _md_escape((pred.get("reason") if pred else "") or "")
            out.append(f"| `{target}` | {kind} | {predicate} | {reason} |")
    out.append("")

    out.append("## God symbols (top refcount)")
    out.append("")
    if not report["god_symbols"]:
        out.append("_No symbols indexed yet._")
    else:
        out.append("| Symbol | File | Refs | Status |")
        out.append("| --- | --- | --- | --- |")
        for g in report["god_symbols"]:
            sym = g["name"]
            file = _md_escape(g["file"])
            out.append(f"| `{sym}` | `{file}` | {g['refcount']} | {_status_badge(g.get('status', 'NONE'))} |")
    out.append("")

    out.append("## Stale notes")
    out.append("")
    if not report["stale_notes"]:
        out.append("_No stale notes — every note is fresh._")
    else:
        for n in report["stale_notes"]:
            target = n.get("target", "")
            body = (n.get("body") or "")[:160].replace("\n", " ")
            out.append(f"- `{target}` — {n.get('staleness', 'unknown')}, "
                       f"verified {_render.fmt_age(n.get('last_verified_at'))}")
            if body:
                out.append(f"  > {body}")
    out.append("")

    out.append("## Knowledge gaps")
    out.append("")
    gaps = report["knowledge_gaps"]
    out.append("### Isolated symbols (defined, zero refs — deletion candidates)")
    if not gaps["isolated_symbols"]:
        out.append("_None._")
    else:
        for s in gaps["isolated_symbols"]:
            out.append(f"- `{s['name']}` in `{s['file']}` ({s['kind']})")
    out.append("")
    out.append("### High-ambiguous-ref symbols (multi-def or unbound)")
    if not gaps["ambiguous_refs"]:
        out.append("_None._")
    else:
        for s in gaps["ambiguous_refs"]:
            out.append(f"- `{s['name']}` — {s['def_count']} defs, "
                       f"{s['ref_count']} refs")
    out.append("")
    out.append("### Notes whose target name no longer has a def (likely renamed)")
    if not gaps["notes_target_gone"]:
        out.append("_None._")
    else:
        for n in gaps["notes_target_gone"]:
            out.append(f"- `{n['target']}` (note id {n['id']}, "
                       f"created {_render.fmt_age(n.get('created_at'))})")
    out.append("")

    out.append("## Recent notes")
    out.append("")
    if not report["recent_notes"]:
        out.append("_No notes yet. Save with `projmem note add`._")
    else:
        for n in report["recent_notes"]:
            preview = (n.get("body_preview") or "").replace("\n", " ")
            out.append(f"- `{n.get('target')}` ({n.get('kind')}, "
                       f"{n.get('staleness')}) — {preview}")
    out.append("")

    if report["drifted_files"]:
        out.append("## Drifted files (on-disk hash ≠ index)")
        out.append("")
        for p in report["drifted_files"]:
            out.append(f"- `{p}`")
        out.append("")

    return "\n".join(out)


def write_report(out_dir: str, repo_root: str, report: Dict[str, Any]
                 ) -> Dict[str, Any]:
    """Persist REPORT.md to `out_dir`. Returns {wrote, skipped}."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "REPORT.md")
    md = render_markdown(report, repo_root)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return {"wrote": [path], "skipped": []}


# ---------------------------------------------------------------------------
# Section builders.
# ---------------------------------------------------------------------------

def _refuted_this_week(store, repo_root: str, limit: int) -> List[Dict[str, Any]]:
    """Notes with at least one REFUTED claim, scoped to the last 7 days
    of activity (last_verified_at OR created_at)."""
    from . import integrity as _intg
    cutoff = time.time() - WEEK_SECONDS
    rows = list(store.conn.execute(
        "SELECT * FROM annotations "
        "WHERE (last_verified_at IS NULL OR last_verified_at >= ?) "
        "  AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY last_verified_at DESC, created_at DESC "
        "LIMIT ?",
        (cutoff, time.time(), limit * 4)))
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            res = _intg.revalidate_annotation(store, repo_root, dict(row),
                                              persist=False)
        except Exception:
            continue
        first_refuted = next(
            (v for v in (res.claim_verdicts or [])
             if v.get("status") == "REFUTED"),
            None)
        if not first_refuted and res.now != "contradicted":
            continue
        out.append({
            "id":      row["id"],
            "target":  row["target"],
            "kind":    row["kind"],
            "body":    (row["body"] or "")[:200],
            "first_refuted_claim": first_refuted,
            "staleness": res.now,
        })
        if len(out) >= limit:
            break
    return out


def _claims_total(store) -> int:
    """Sum of claims across all annotations. Best-effort JSON parse."""
    import json as _json
    n = 0
    for row in store.conn.execute(
            "SELECT evidence FROM annotations WHERE evidence IS NOT NULL"):
        ev = row["evidence"]
        try:
            data = _json.loads(ev) if isinstance(ev, str) else ev
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            n += sum(1 for it in data if isinstance(it, dict)
                     and (it.get("predicate") or it.get("status")))
    return n


def _count_drift(store, repo_root: str) -> Tuple[int, List[str]]:
    """Hash-sample every indexed file and report the count + names of
    those whose on-disk content has drifted. Bounded by the file count
    in the store; cheap on small repos, OK on large ones (~10s for 10k
    files)."""
    paths = [r["path"] for r in store.conn.execute(
        "SELECT path FROM files")]
    drifted = _render.drifted_paths(store, repo_root, paths)
    return len(drifted), sorted(drifted)


def _count_stale_files(store) -> int:
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE stale=1").fetchone()
    return int(row["n"]) if row else 0


def _god_symbols(store, limit: int, _artifacts) -> List[Dict[str, Any]]:
    """Top-refcount defs whose file is NOT classified as artifact and
    whose name isn't a dunder/trivial constructor (those inflate the
    list because Python repeats `__init__` per class).
    """
    rows = list(store.conn.execute(
        "SELECT s.name, s.file, s.kind, "
        "       (SELECT COUNT(*) FROM refs r WHERE r.name=s.name) AS refcount "
        "FROM symbols s "
        "WHERE s.kind IS NOT NULL "
        "  AND s.name NOT LIKE '\\_\\_%\\_\\_' ESCAPE '\\' "
        "GROUP BY s.name, s.file "
        "ORDER BY refcount DESC "
        "LIMIT ?", (limit * 6,)))
    seen_names: set = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        if _artifacts.is_artifact_path(r["file"]):
            continue
        # Same name in many files inflates the list — keep only the first
        # def per name (highest refcount wins because of the ORDER BY).
        if r["name"] in seen_names:
            continue
        seen_names.add(r["name"])
        out.append({
            "name":     r["name"],
            "file":     r["file"],
            "kind":     r["kind"],
            "refcount": int(r["refcount"]),
            "status":   "NONE",
        })
        if len(out) >= limit:
            break
    return out


def _stale_notes(store, repo_root: str, limit: int) -> List[Dict[str, Any]]:
    """Notes whose persisted staleness ∈ {weakly_stale, strongly_stale}."""
    rows = list(store.conn.execute(
        "SELECT * FROM annotations "
        "WHERE staleness IN ('weakly_stale','strongly_stale') "
        "  AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY last_verified_at DESC NULLS LAST "
        "LIMIT ?", (time.time(), limit)))
    return [dict(r) for r in rows]


def _isolated_symbols(store, limit: int, _artifacts) -> List[Dict[str, Any]]:
    """Defs that have ZERO references AND aren't in artifact paths."""
    rows = list(store.conn.execute(
        "SELECT s.name, s.file, s.kind "
        "FROM symbols s "
        "LEFT JOIN refs r ON r.name = s.name "
        "WHERE s.exported=1 "
        "GROUP BY s.id "
        "HAVING COUNT(r.id) = 0 "
        "LIMIT ?", (limit * 4,)))
    out: List[Dict[str, Any]] = []
    for r in rows:
        if _artifacts.is_artifact_path(r["file"]):
            continue
        out.append({"name": r["name"], "file": r["file"], "kind": r["kind"]})
        if len(out) >= limit:
            break
    return out


def _ambiguous_refs(store, limit: int) -> List[Dict[str, Any]]:
    """Names with >1 def in the index — refs to them are inherently
    ambiguous unless callers disambiguate."""
    rows = list(store.conn.execute(
        "SELECT name, COUNT(*) AS def_count FROM symbols "
        "GROUP BY name HAVING def_count > 1 "
        "ORDER BY def_count DESC LIMIT ?", (limit,)))
    out: List[Dict[str, Any]] = []
    for r in rows:
        ref_row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM refs WHERE name=?",
            (r["name"],)).fetchone()
        out.append({
            "name":      r["name"],
            "def_count": int(r["def_count"]),
            "ref_count": int(ref_row["n"]) if ref_row else 0,
        })
    return out


def _renamed_notes(store, limit: int) -> List[Dict[str, Any]]:
    """Notes whose target looks like a symbol (no `/`, no `#`, no `:`)
    but that symbol has no def in the current index. Strong signal the
    symbol was renamed/deleted and the note is now floating."""
    out: List[Dict[str, Any]] = []
    rows = list(store.conn.execute(
        "SELECT id, target, created_at FROM annotations "
        "WHERE (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at DESC "
        "LIMIT 500", (time.time(),)))
    for r in rows:
        t = (r["target"] or "").strip()
        if not t or "/" in t or "#" in t or ":" in t or t.startswith("@"):
            continue
        # Looks like a bare symbol name. Check def count.
        def_row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols WHERE name=?", (t,)).fetchone()
        if def_row and int(def_row["n"]) == 0:
            out.append({
                "id":         r["id"],
                "target":     t,
                "created_at": r["created_at"],
            })
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Markdown helpers.
# ---------------------------------------------------------------------------

def _md_escape(s: str) -> str:
    return (s.replace("|", "\\|").replace("\n", " ").replace("\r", " "))


def _status_badge(label: str) -> str:
    """Markdown-friendly status indicator. ASCII to avoid emoji
    rendering issues in some viewers."""
    return {
        "PROVED":    "**PROVED**",
        "REFUTED":   "**REFUTED**",
        "AMBIGUOUS": "ambiguous",
        "NONE":      "—",
    }.get(label, "—")


def _legend_block() -> str:
    return (
        "Legend: **PROVED** = claim verifies against current index; "
        "**REFUTED** = claim contradicted; ambiguous = mixed/unprovable; "
        "drift = on-disk hash diverged from index."
    )
