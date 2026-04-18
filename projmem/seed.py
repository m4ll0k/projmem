"""projmem/seed.py — cold-start auto-population of INFERENCE notes.

Fresh session 1 bootstrap. Without this, a fresh-indexed repo has zero
notes; session 2 arrives and projmem has nothing to say. `projmem seed`
generates INFERENCE-class notes on high-signal targets derived from the
graph shape:

  - HOT-FILE notes on top files by reverse-dep count
  - GATEWAY notes on top symbols by call-ref count
  - CROSS-LAYER notes when an enum name appears in ≥2 layers
  - BARREL notes on likely re-export hubs (index.ts / _ns/ barrels)

All notes are:
  - author="projmem-seed"
  - truth_class=INFERENCE (low confidence ≤ 0.65 — they're GUESSES
    about what matters, not verified beliefs)
  - idempotent: if a `projmem-seed` note already exists on a target,
    skip re-creating it; human notes are never overwritten.

The seeding is bounded (default max 15 notes) so a huge repo doesn't
produce a note explosion.
"""
from __future__ import annotations
import time
from typing import Any, Dict, List, Optional


_SEED_AUTHOR = "projmem-seed"


def _seed_exists_for(store, target: str) -> bool:
    """True iff a seed note already exists on this target."""
    try:
        row = store.conn.execute(
            "SELECT 1 FROM annotations WHERE target=? AND author=? LIMIT 1",
            (target, _SEED_AUTHOR)).fetchone()
        return row is not None
    except Exception:
        return False


def _add_seed_note(store, *, target: str, kind: str, body: str,
                    claims: Optional[List[Dict[str, Any]]] = None,
                    confidence: float = 0.6,
                    dry_run: bool = False) -> Optional[int]:
    """Insert a seed note. Returns the new annotation id, or None when
    a seed note already exists for this target (idempotent skip).

    When `dry_run=True`, returns a sentinel value (-1) WITHOUT writing
    so callers can preview what would be created. Idempotency check
    still runs so previewed-as-skip stays consistent with the real run.
    """
    if _seed_exists_for(store, target):
        return None
    if dry_run:
        return -1
    evidence_list: List[Dict[str, Any]] = list(claims) if claims else []
    try:
        ann_id = store.add_annotation(
            target=target, kind=kind, body=body,
            author=_SEED_AUTHOR, expires_at=None,
            confidence=confidence,
            evidence=evidence_list if evidence_list else None,
            assumptions=None, scope="subsystem",
            truth_class="INFERENCE", fingerprint=None)
        return ann_id
    except Exception:
        return None


def _hot_files(store, limit: int = 5) -> List[Dict[str, Any]]:
    """Top files by inbound import edge count, excluding virtual
    `module:` / `builtin:` targets."""
    rows = list(store.conn.execute(
        "SELECT dst, COUNT(*) AS cnt FROM edges "
        "WHERE type='imports' AND dst NOT LIKE 'module:%' "
        "AND dst NOT LIKE 'builtin:%' "
        "GROUP BY dst ORDER BY cnt DESC LIMIT ?", (limit * 3,)))
    # Verify each exists in the files table (exclude paths we resolved
    # but never indexed).
    out: List[Dict[str, Any]] = []
    for r in rows:
        dst = r["dst"]
        if not dst:
            continue
        hit = store.conn.execute(
            "SELECT path FROM files WHERE path=? LIMIT 1", (dst,)).fetchone()
        if not hit:
            continue
        out.append({"file": dst, "reverse_deps": int(r["cnt"])})
        if len(out) >= limit:
            break
    return out


def _gateway_symbols(store, limit: int = 5) -> List[Dict[str, Any]]:
    """Top symbols by call-ref count. Narrows to `function` / `method`
    / `exported` kinds since class-level names with the same spelling
    are noise."""
    rows = list(store.conn.execute(
        "SELECT name, COUNT(*) AS cnt FROM refs WHERE kind='call' "
        "GROUP BY name ORDER BY cnt DESC LIMIT ?", (limit * 4,)))
    out: List[Dict[str, Any]] = []
    for r in rows:
        nm = r["name"]
        if not nm or len(nm) < 3:
            continue
        defs = list(store.conn.execute(
            "SELECT file, line, kind FROM symbols WHERE name=? "
            "AND kind IN ('function','method','exported') LIMIT 5", (nm,)))
        if not defs:
            continue
        # Pick the first def (tiebreak by file path). Seed notes are
        # INFERENCE so exact disambiguation isn't critical.
        primary = dict(defs[0])
        out.append({"name": nm, "ref_count": int(r["cnt"]),
                     "file": primary["file"], "line": primary["line"],
                     "kind": primary["kind"]})
        if len(out) >= limit:
            break
    return out


def _cross_layer_enums(store) -> List[Dict[str, Any]]:
    """Enums declared under ≥2 layers (ts, prisma, sql, rust).
    Returns a list of {name, layers, member_counts}."""
    try:
        rows = list(store.conn.execute(
            "SELECT name, file, context FROM contracts WHERE kind='enum_shape'"))
    except Exception:
        return []
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        ctx = r["context"] or ""
        layer, _, members = ctx.partition(":")
        by_name.setdefault(r["name"], []).append({
            "layer": layer or "?",
            "file":  r["file"],
            "members": [m for m in members.split(",") if m],
        })
    out: List[Dict[str, Any]] = []
    for name, sites in by_name.items():
        layers = {s["layer"] for s in sites}
        if len(layers) < 2:
            continue
        out.append({
            "name":   name,
            "layers": sorted(layers),
            "sites":  sites[:6],
        })
    return out


def _barrel_candidates(store, limit: int = 3) -> List[Dict[str, Any]]:
    """Files likely to be re-export barrels: high reverse-dep count AND
    basename in {index.*, barrel.*} OR under a `_namespaces`/`_ns` dir."""
    rows = list(store.conn.execute(
        "SELECT dst, COUNT(*) AS cnt FROM edges "
        "WHERE type='imports' AND dst NOT LIKE 'module:%' "
        "AND dst NOT LIKE 'builtin:%' "
        "GROUP BY dst ORDER BY cnt DESC"))
    import os as _os
    out: List[Dict[str, Any]] = []
    for r in rows:
        dst = r["dst"]
        if not dst:
            continue
        base = _os.path.basename(dst)
        stem, _, _ = base.partition(".")
        under_ns = ("/_namespaces/" in dst) or ("/_ns/" in dst)
        if not (stem in ("index", "barrel") or under_ns):
            continue
        hit = store.conn.execute(
            "SELECT path FROM files WHERE path=? LIMIT 1", (dst,)).fetchone()
        if not hit:
            continue
        out.append({"file": dst, "reverse_deps": int(r["cnt"])})
        if len(out) >= limit:
            break
    return out


def seed(store, *, max_notes: int = 15,
         dry_run: bool = False) -> Dict[str, Any]:
    """Seed INFERENCE-class notes based on graph-shape heuristics.
    Returns a report of what was created / skipped.

    `dry_run=True` performs the same selection but doesn't write — the
    `created` list shows what WOULD be added (with `id: null`) so the
    caller can review before committing. Useful because seed otherwise
    creates ~8 notes silently on first run, which can include false
    positives on low-binding repos (e.g. Java StringBuilder.append
    flagged as a "gateway" when ref binding is poor)."""
    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    def _record(ann_id: Optional[int], kind_tag: str,
                 target: str, reason: str) -> None:
        if ann_id is None:
            skipped.append({"kind": kind_tag, "target": target,
                             "reason": "already_seeded_or_failed"})
        else:
            entry = {"kind": kind_tag, "target": target,
                      "reason": reason}
            if dry_run:
                entry["id"] = None
                entry["dry_run"] = True
            else:
                entry["id"] = ann_id
            created.append(entry)

    budget = max_notes

    # 1. Cross-layer enums — highest-signal, put first.
    # Use compound `<file>#<EnumName>` targets so multiple enums in the
    # same prisma/schema.prisma file don't collide on the idempotency
    # check.
    for enum in _cross_layer_enums(store):
        if budget <= 0:
            break
        name = enum["name"]
        layers_s = ", ".join(enum["layers"])
        first = enum["sites"][0]
        target = f"{first['file']}#{name}"
        body = (f"[SEED] Enum {name!r} is declared in multiple layers "
                f"({layers_s}). Adding / removing a value on one side "
                "must be mirrored on the others or runtime validation "
                "fails. Check for an `assertSharedEnumsMatchDatabase`-"
                "style reconciliation point.")
        claims = [{"subject": name, "predicate": "defined-at",
                   "object": f"{first['file']}:1",
                   "truth_class": "INFERENCE", "confidence": 0.55}]
        ann_id = _add_seed_note(store, target=target, kind="risk",
                                  body=body, claims=claims,
                                  confidence=0.6, dry_run=dry_run)
        _record(ann_id, "cross_layer_enum", target,
                f"enum {name} in {layers_s}")
        budget -= 1

    # 2. Hot files — top reverse-deps.
    for hot in _hot_files(store, limit=5):
        if budget <= 0:
            break
        target = hot["file"]
        rd = hot["reverse_deps"]
        body = (f"[SEED] Hot file: {rd} reverse dep(s). Changes here "
                "propagate widely — run `projmem reverse` before "
                "non-trivial edits.")
        ann_id = _add_seed_note(store, target=target, kind="risk",
                                  body=body, confidence=0.55,
                                  dry_run=dry_run)
        _record(ann_id, "hot_file", target, f"{rd} reverse deps")
        budget -= 1

    # 3. Gateway symbols — top call-ref counts.
    for gw in _gateway_symbols(store, limit=5):
        if budget <= 0:
            break
        target = f"{gw['file']}#{gw['name']}"
        body = (f"[SEED] Gateway: {gw['name']} is called from "
                f"{gw['ref_count']} site(s). Breaking-change risk on "
                "signature changes; verify callers before edits.")
        claims = [{"subject": gw["name"], "predicate": "defined-at",
                   "object": f"{gw['file']}:{gw['line']}",
                   "truth_class": "INFERENCE", "confidence": 0.7}]
        ann_id = _add_seed_note(store, target=target, kind="note",
                                  body=body, claims=claims,
                                  confidence=0.65, dry_run=dry_run)
        _record(ann_id, "gateway_symbol", target,
                f"{gw['ref_count']} call refs")
        budget -= 1

    # 4. Barrel candidates.
    for b in _barrel_candidates(store, limit=3):
        if budget <= 0:
            break
        target = b["file"]
        body = (f"[SEED] Barrel (re-export hub): {b['reverse_deps']} "
                "reverse dep(s). Forgotten updates here silently break "
                "downstream imports. When adding a new leaf, confirm "
                "the barrel re-exports it.")
        ann_id = _add_seed_note(store, target=target, kind="risk",
                                  body=body, confidence=0.55,
                                  dry_run=dry_run)
        _record(ann_id, "barrel", target,
                f"{b['reverse_deps']} reverse deps")
        budget -= 1

    return {
        "created":     created,
        "skipped":     skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "max_notes":   max_notes,
        "author":      _SEED_AUTHOR,
        "note": ("Seed notes are INFERENCE-class — they're graph-shape "
                 "heuristics, not verified beliefs. Refine or delete "
                 "them as you discover the actual ground truth."),
    }
