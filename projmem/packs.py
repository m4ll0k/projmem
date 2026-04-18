"""Context pack generation — bounded, ranked, confidence-aware.

A pack is a JSON object with structural, semantic, entrypoint, and test context,
inclusion reasons per item, and an explicit confidence/coverage summary.
"""
from __future__ import annotations
import json
import os
import time as _time_mod
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import Config
from .store import Store
from . import graph, symbols, confidence, integrity as _integrity


MAX_ITEMS_PER_BUCKET = 25


def _check_deadline(deadline: Optional[float], pack: Dict[str, Any],
                    section: str) -> bool:
    """Return True (and mark pack timed_out) when wall-clock exceeds deadline.

    Checking at a named section lets consumers see where the build stopped.
    Returns False (do not skip) when deadline is None (no timeout configured).
    """
    if deadline is None:
        return False
    if _time_mod.time() > deadline:
        pack["timed_out"] = True
        pack.setdefault("timeout_truncated_at", section)
        return True
    return False
MAX_TOKEN_CONTRACTS = 20
# A contract that occurs in more than AMBIENT_CAP files is treated as ambient:
# it stays in the target's own inventory, but does NOT pull peers into the pack
# via shared-contract co-occurrence. Prevents "every file contains 'DONE'" spam.
AMBIENT_CAP = 6


# Py 3.10+ ships `sys.stdlib_module_names`. Earlier runtimes get a small
# fallback of the most common offenders — the same set that was previously
# mis-classified as "unresolved". Better to under-report stdlib than to
# miss a real relative-import break.
import sys as _sys
_STDLIB_MODULE_NAMES = frozenset(
    getattr(_sys, "stdlib_module_names", None) or {
        "os", "sys", "io", "re", "json", "ast", "abc", "argparse", "asyncio",
        "base64", "collections", "contextlib", "copy", "csv", "dataclasses",
        "datetime", "decimal", "enum", "errno", "fnmatch", "functools",
        "glob", "hashlib", "http", "importlib", "inspect", "itertools",
        "logging", "math", "operator", "pathlib", "pickle", "random",
        "secrets", "shlex", "shutil", "signal", "socket", "sqlite3",
        "string", "struct", "subprocess", "tempfile", "textwrap", "time",
        "traceback", "typing", "unittest", "urllib", "uuid", "warnings",
        "weakref", "xml", "__future__",
    }
)


def _classify_import_for_pack(src_file: str, spec: str) -> str:
    """Return one of: 'stdlib', 'external', 'unresolved_repo_relative'.

    This is the PACK-LEVEL classification used to decide whether an
    unresolved import should lower structural trust. Distinct from
    `cli._classify_import_spec` which serves the `unresolved-imports`
    command with finer language-level buckets.
    """
    s = (spec or "").strip()
    if not s:
        return "external"
    # Repo-relative: dot-prefixed Python relative or path-shaped JS spec.
    if s.startswith(".") or s.startswith("/") or s.startswith("./") \
            or s.startswith("../") or s.startswith("//"):
        return "unresolved_repo_relative"
    # Python: root package segment in stdlib_module_names → stdlib.
    if src_file.endswith(".py"):
        root = s.split(".", 1)[0]
        if root in _STDLIB_MODULE_NAMES:
            return "stdlib"
    return "external"


def build_repo_overview(cfg: Config, store: Store, *,
                         loc: Optional[Dict[str, Any]] = None,
                         prefix: str = "",
                         limit: int = 10) -> Dict[str, Any]:
    """Repo (or sub-directory) orientation pack — used by `projmem pack .`
    and `projmem pack <dir>/`. Returns top hot files (by reverse-deps),
    top referenced symbols, contract inventory, language mix,
    entrypoints, and cross-layer enum mismatches.

    Counts/limits are bounded; this is meant as a starting view, not as
    a substitute for `pack <file>` or `pack <symbol>` for narrow work.
    """
    if loc is None:
        loc = {"kind": "directory", "path": prefix or ".",
               "scope_prefix": prefix, "is_root": not prefix}
    scope_prefix = loc.get("scope_prefix") or ""

    def _in_scope(file: Optional[str]) -> bool:
        if not scope_prefix:
            return True
        return bool(file) and file.startswith(scope_prefix)

    # ---- file inventory + language mix ----
    all_rows = list(store.conn.execute(
        "SELECT path, lang, parser FROM files"))
    in_scope = [r for r in all_rows if _in_scope(r["path"])]
    lang_counts: Dict[str, int] = {}
    parser_counts: Dict[str, int] = {}
    for r in in_scope:
        lang = r["lang"] or "other"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
        p = r["parser"] or "unknown"
        parser_counts[p] = parser_counts.get(p, 0) + 1

    # ---- top files by inbound import edges (most-depended-on) ----
    hot_rows = list(store.conn.execute(
        "SELECT dst, COUNT(*) AS cnt FROM edges WHERE type='imports' "
        "GROUP BY dst ORDER BY cnt DESC LIMIT ?", (limit * 4,)))
    top_files: List[Dict[str, Any]] = []
    for r in hot_rows:
        dst = r["dst"]
        if not dst or dst.startswith("module:") or dst.startswith("builtin:"):
            continue
        if not _in_scope(dst):
            continue
        top_files.append({"file": dst, "reverse_deps": int(r["cnt"])})
        if len(top_files) >= limit:
            break

    # ---- top referenced symbols (call refs only — most-called) ----
    top_sym_rows = list(store.conn.execute(
        "SELECT name, COUNT(*) AS cnt FROM refs WHERE kind='call' "
        "GROUP BY name ORDER BY cnt DESC LIMIT ?", (limit * 6,)))
    top_symbols: List[Dict[str, Any]] = []
    for r in top_sym_rows:
        nm = r["name"]
        if not nm or len(nm) < 3:
            continue
        # Resolve to a def site for context. Filter to in-scope defs.
        def_rows = [dict(d) for d in store.symbols_by_name(nm)
                    if _in_scope(d["file"])]
        if not def_rows:
            continue
        primary = def_rows[0]
        top_symbols.append({
            "name": nm, "file": primary["file"],
            "line": primary["line"], "kind": primary["kind"],
            "ref_count": int(r["cnt"]),
        })
        if len(top_symbols) >= limit:
            break

    # ---- contract inventory by kind, with samples ----
    kind_counts: Dict[str, int] = {}
    samples: Dict[str, List[str]] = {}
    contract_rows = list(store.conn.execute(
        "SELECT kind, name, file FROM contracts"))
    for r in contract_rows:
        if not _in_scope(r["file"]) and r["file"] != "<config>":
            continue
        k = r["kind"]
        kind_counts[k] = kind_counts.get(k, 0) + 1
        if r["name"] and len(samples.get(k, [])) < 8:
            samples.setdefault(k, [])
            if r["name"] not in samples[k]:
                samples[k].append(r["name"])

    # ---- entrypoints (package.json main/bin/scripts) ----
    entry_rows: List[Dict[str, Any]] = []
    for r in store.conn.execute(
            "SELECT name, file, context FROM contracts "
            "WHERE kind='entrypoint' OR kind='script'"):
        if not _in_scope(r["file"]):
            continue
        entry_rows.append({
            "name": r["name"], "file": r["file"],
            "context": r["context"],
        })
        if len(entry_rows) >= limit * 2:
            break

    # ---- cross-layer enum mismatches (reuse the checklist helper) ----
    from . import checklist as _ck
    enum_mismatches = _ck._cross_layer_enum_mismatches(store)

    overview: Dict[str, Any] = {
        "kind": "repo_overview",
        "target": loc,
        "meta": {
            "root": cfg.root,
            "indexed_root": store.get_meta("root"),
            "store": store.path,
        },
        "summary": {
            "files_indexed": len(in_scope),
            "languages": dict(sorted(lang_counts.items(),
                                     key=lambda kv: -kv[1])),
            "parsers": dict(sorted(parser_counts.items(),
                                   key=lambda kv: -kv[1])),
        },
        "top_files_by_reverse_deps": top_files,
        "top_symbols_by_call_refs": top_symbols,
        "contracts": {
            "counts_by_kind": dict(sorted(kind_counts.items(),
                                          key=lambda kv: -kv[1])),
            "samples": samples,
        },
        "entrypoints": entry_rows,
        "cross_layer_enum_mismatches": enum_mismatches,
        "tip": (
            "This is a repo orientation. For deep context on a single "
            "file or symbol use `projmem pack <file-or-symbol>`. For "
            "completeness checks use `projmem complete`."),
    }
    # Always attach repo_memory header so the agent sees recent claims/notes.
    from . import memory_header as _mh
    _mh.attach(overview, store)
    return overview


def build_pack(cfg: Config, store: Store, target: str,
               radius: int = 1, include_tests: bool = True,
               force_kind: Optional[str] = None,
               include_snippets: bool = False,
               snippet_bytes: int = 8000,
               include_source: bool = False,
               timeout_secs: Optional[float] = None,
               include_artifacts: bool = False) -> Dict[str, Any]:
    loc = symbols.locate(store, target,
                         force_kind=force_kind,
                         project_root=cfg.root)
    # Directory target → repo / sub-directory overview (Audit fix #9).
    # `pack .` was previously empty because `.` resolved to nothing
    # in the symbol/file lookup; the overview surfaces top hot files,
    # contract inventory, language mix, entrypoints, and any open
    # cross-layer enum mismatches so the agent has a real starting
    # point for orientation.
    if loc.get("kind") == "directory":
        return build_repo_overview(cfg, store, loc=loc)
    deadline: Optional[float] = (
        _time_mod.time() + timeout_secs if timeout_secs is not None else None)
    pack: Dict[str, Any] = {
        "target": loc,
        "meta": {
            "root": cfg.root,
            "radius": radius,
            "generated_by": "projmem",
            "store": store.path,
            "indexed_root": store.get_meta("root"),
            **({"timeout_secs": timeout_secs} if timeout_secs is not None else {}),
        },
        "why": _why(loc, target),
        "reasons": {},  # item -> reason
        "unknowns": [],
        "coverage": {},
    }

    files_included: Set[str] = set()
    file_path: Optional[str] = None

    if loc["kind"] == "file":
        file_path = loc["path"]
        files_included.add(file_path)
    elif loc["kind"] == "symbol":
        defs = loc.get("defs", []) or []
        for d in defs:
            files_included.add(d["file"])
        if not defs:
            if loc.get("disambiguated") and not loc.get("file_resolved"):
                pack["unknowns"].append({
                    "kind": "file-not-indexed",
                    "note": f"File '{loc.get('file')}' is not in the index. "
                            "Run `projmem index` or check --exclude patterns.",
                })
            elif loc.get("disambiguated"):
                pack["unknowns"].append({
                    "kind": "symbol-not-in-file",
                    "note": f"Symbol '{loc['name']}' not found in "
                            f"'{loc.get('file')}'. `projmem symbol {loc['name']}` "
                            "may show it in another file.",
                })
            else:
                pack["unknowns"].append({
                    "kind": "symbol-undefined",
                    "note": f"No definition found for symbol '{target}'. "
                            "It may be external, dynamically defined, or not indexed.",
                })
        elif len(defs) > 1 and not loc.get("disambiguated"):
            # CRITICAL: do not merge contexts from multiple defs silently.
            # Pick the first as primary; surface alternatives so caller can
            # re-query with `file#symbol`.
            pack["unknowns"].append({
                "kind": "ambiguous-symbol",
                "note": f"Symbol '{loc['name']}' has {len(defs)} definitions in "
                        "different files. This pack narrows to the first; "
                        "re-run with `file#symbol` to disambiguate.",
                "alternatives": [
                    {"file": d["file"], "line": d["line"], "kind": d["kind"]}
                    for d in defs],
            })
            primary = defs[0]
            files_included.clear()
            files_included.add(primary["file"])
            loc["primary_def"] = primary

    # Human / agent annotations — surface BEFORE structural context so
    # any prior verdict on this target (verified-safe, refute,
    # documented-footgun, ...) is the first thing the consumer sees.
    # Killer use case: short-circuit a re-investigation when a
    # previous session already established the answer.
    _ann_file = file_path  # populated only for file targets
    _ann_symbol_ids: List[str] = []
    _ann_names_in_file: List[str] = []
    if loc.get("kind") == "symbol":
        # When target is a symbol, use the resolved file (if any) for
        # file-shorthand matching — without this, a `note add
        # 'foo.py#bar'` against a `pack 'foo.py#bar'` target wouldn't
        # match because the symbol_id has a SCIP suffix (e.g. `bar.`)
        # while the user-written shorthand doesn't.
        if loc.get("file"):
            _ann_file = loc["file"]
        for d in (loc.get("defs") or []):
            sid = d.get("symbol_id")
            if sid:
                _ann_symbol_ids.append(sid)
            if d.get("name"):
                _ann_names_in_file.append(d["name"])
        if loc.get("symbol_id"):
            _ann_symbol_ids.append(loc["symbol_id"])
        if loc.get("name"):
            _ann_names_in_file.append(loc["name"])
    if _ann_file:
        # Also pull annotations on every defined symbol within the file
        for r in store.conn.execute(
                "SELECT name FROM symbols WHERE file=?", (_ann_file,)):
            _ann_names_in_file.append(r["name"])
    notes = store.annotations_for_pack(
        file=_ann_file,
        symbol_ids=_ann_symbol_ids or None,
        names_in_file=_ann_names_in_file or None,
        include_project=True,
        include_dir_prefixes=True,
    )
    if notes:
        # Revalidate EVERY note against the current code state before
        # surfacing it. This is the load-bearing integrity step —
        # without it the pack treats stored conclusions as eternal
        # truth, which is exactly the drift failure mode SPEC calls
        # out. ``revalidate_for_pack`` persists the new staleness so
        # subsequent packs get the cached label.
        repo_root = cfg.root
        notes = _integrity.revalidate_for_pack(store, repo_root, notes)

        # Sort by freshness buckets (SPEC #5): fresh → unknown →
        # weakly_stale → strongly_stale → contradicted.
        notes = _integrity.sort_annotations_for_pack(notes)

        # Surface every note with its integrity metadata. We keep the
        # legacy ``confidence: "human-asserted"`` string for
        # backward compatibility (tests, external consumers), but
        # add ``confidence_score`` (numeric) and staleness alongside.
        pack["human_notes"] = [
            {"id": n["id"], "kind": n["kind"], "target": n["target"],
             "body": n["body"], "author": n.get("author"),
             "created_at": n["created_at"],
             "expires_at": n.get("expires_at"),
             "confidence":       "human-asserted",
             "confidence_score": round(float(n.get("confidence")
                                             or 0.5), 4),
             "staleness":        n.get("staleness") or _integrity.UNKNOWN,
             "truth_class":      n.get("truth_class") or "INFERENCE",
             "evidence":         _integrity._safe_json(n.get("evidence"))
                                 or [],
             "assumptions":      _integrity._safe_json(n.get("assumptions"))
                                 or [],
             "scope":            n.get("scope"),
             "drifted_fields":   n.get("drifted_fields") or [],
             "last_verified_at": n.get("last_verified_at"),
             "fingerprint":      _integrity._safe_json(n.get("fingerprint"))
                                 or None,
             "revalidation_error": n.get("revalidation_error"),
             # Claim-level verdicts (present only when this note carries
             # structured claims). Agents reading the pack see exactly which
             # belief became false rather than a coarse "strongly_stale".
             **({"claim_verdicts":       n["claim_verdicts"],
                 "claim_overall_status": n.get("claim_overall_status"),
                 "verified_count":       n.get("verified_count", 0),
                 "refuted_count":        n.get("refuted_count", 0),
                 "uncheckable_count":    n.get("uncheckable_count", 0)}
                if n.get("claim_verdicts") else {}),
            }
            for n in notes
        ]

        # Legacy annotation_conflicts (kind-pair) kept as-is for
        # downstream consumers. The richer structured conflicts live
        # under ``target_integrity.guidance`` and ``contradictions``.
        from collections import defaultdict as _dd
        by_target: dict = _dd(list)
        for n in pack["human_notes"]:
            by_target[n["target"]].append(n)
        _conflict_pairs = {
            frozenset({"verified-safe", "refute"}),
            frozenset({"verified-safe", "documented-footgun"}),
            frozenset({"refute", "todo"}),
        }
        legacy_conflicts: List[dict] = []
        for tgt, group in by_target.items():
            kinds = {g["kind"] for g in group}
            for pair in _conflict_pairs:
                if pair.issubset(kinds):
                    legacy_conflicts.append({
                        "target": tgt,
                        "conflicting_kinds": sorted(pair),
                        "note_ids": [g["id"] for g in group
                                     if g["kind"] in pair],
                        "guidance": ("Read both notes and reconcile. "
                                     "Newer note may supersede older; "
                                     "check author + created_at."),
                    })
        if legacy_conflicts:
            pack["annotation_conflicts"] = legacy_conflicts

    # ----- Per-target integrity score (SPEC #9) -----------------------
    # Always emitted, even when there are no notes — the score still
    # reflects ambiguity + structural coverage.
    primary_target = None
    if _ann_file:
        if _ann_names_in_file:
            primary_target = f"{_ann_file}#{_ann_names_in_file[0]}"
        else:
            primary_target = _ann_file
    elif _ann_symbol_ids:
        primary_target = _ann_symbol_ids[0]

    if primary_target:
        isc = _integrity.integrity_score(
            store, cfg.root, primary_target,
            annotations=notes if notes else None)
        # Explicit contradictions + ambiguity as structured blocks.
        conflicts_list = _integrity.detect_contradictions(
            store, cfg.root, primary_target)
        ambig_list = _integrity.ambiguity_for_target(store, primary_target)
        pack["target_integrity"] = {
            "target":        primary_target,
            "score":         isc.score,
            "factors":       isc.factors,
            "guidance":      isc.guidance,
            "contradictions": [c.to_dict() for c in conflicts_list],
            "ambiguity":     [a.to_dict() for a in ambig_list],
        }

    # Structural context
    #
    # Round-4 report P0 fix: when target is a SYMBOL (or file#symbol), the old
    # code only returned FILE-LEVEL reverse deps (imports of the def file).
    # That silently under-reports by missing every same-file and cross-file
    # call-site that doesn't happen through a top-level import edge —
    # precisely the `setupCanaryInterceptor` case where 3 real call sites
    # produced 0 reverse_dependencies.
    #
    # New behaviour for symbol targets: reverse_dependencies = union of
    #   (a) file-level imports of any file that defines the symbol, AND
    #   (b) every ref row for this symbol name (call / new / import_binding)
    # De-duped on (file, line). Each row is tagged `via`:
    #   via="imports"          — file-level import (existing behaviour)
    #   via="call" / "new" / "import_binding" — symbol-level ref (new)
    fwd, rev = [], []
    if file_path:
        fwd = graph.forward_deps(store, file_path)
        rev = graph.reverse_deps(store, file_path)
    else:
        for f in files_included:
            fwd += graph.forward_deps(store, f)
            rev += graph.reverse_deps(store, f)
    # Tag file-level edges for schema uniformity.
    for d in rev:
        d.setdefault("via", d.get("type", "imports"))
    for d in fwd:
        d.setdefault("via", d.get("type", "imports"))

    # Symbol-level reverse deps: every ref row for the target name.
    symbol_name: Optional[str] = None
    if loc["kind"] == "symbol":
        symbol_name = loc.get("name")
    if symbol_name:
        for r in store.refs_by_name(symbol_name):
            r = dict(r)
            # Skip the def file's own rows ONLY if the ref is AT the def line
            # (already excluded from refs by the indexer, but belt-and-braces).
            # Skip the target file itself when target is file#symbol — in that
            # case the intra-file callers live in `intra_file_for_symbol` and
            # `reverse_dependencies` should surface cross-file callers +
            # the same-file callers (which are the entire signal when the
            # symbol is only used intra-file, per the pwnpilot case).
            rev.append({
                "file": r["file"],
                "line": r["line"],
                "type": "references",
                "via": r.get("kind") or "call",
                "confidence": r.get("confidence", "high"),
                "evidence": f"ref to symbol '{symbol_name}' (line {r['line']})",
            })

    fwd = _dedup(fwd, key="file")[:MAX_ITEMS_PER_BUCKET]
    # For reverse, dedupe on (file, line, via) — two call sites on different
    # lines in the same file are DISTINCT signals the consumer needs.
    seen_rev = set()
    rev_unique: List[dict] = []
    for d in rev:
        key = (d.get("file"), d.get("line"), d.get("via"))
        if key in seen_rev: continue
        seen_rev.add(key)
        rev_unique.append(d)
    rev = rev_unique[: MAX_ITEMS_PER_BUCKET * 2]

    # Bounded BFS expansion. Radius 1 is the original one-hop view (unchanged).
    # Radius >= 2 transitively collects file-level imports (both directions)
    # with per-hop reasons, capped at MAX_ITEMS_PER_BUCKET * radius so a
    # pathological hub file can't blow up the pack.
    transitive_rev: List[dict] = []
    transitive_fwd: List[dict] = []
    if radius >= 2 and files_included:
        seen_fwd_files = {d["file"] for d in fwd} | set(files_included)
        seen_rev_files = {d["file"] for d in rev} | set(files_included)
        frontier_rev: List[Tuple[str, int]] = [(d["file"], 1) for d in rev
                                                if d.get("via") == "imports"]
        frontier_fwd: List[Tuple[str, int]] = [(d["file"], 1) for d in fwd
                                                if d.get("via") == "imports"]
        cap = MAX_ITEMS_PER_BUCKET * radius
        while frontier_rev and len(transitive_rev) < cap:
            src_file, hop = frontier_rev.pop(0)
            if hop >= radius:
                continue
            for nxt in graph.reverse_deps(store, src_file):
                nf = nxt["file"]
                if nf in seen_rev_files:
                    continue
                seen_rev_files.add(nf)
                rec = {"file": nf, "type": "imports",
                       "via": "imports",
                       "confidence": nxt["confidence"],
                       "evidence": f"{nxt.get('evidence', '')} (hop {hop + 1} via {src_file})",
                       "hop": hop + 1,
                       "through": src_file}
                transitive_rev.append(rec)
                if len(transitive_rev) >= cap:
                    break
                frontier_rev.append((nf, hop + 1))
        while frontier_fwd and len(transitive_fwd) < cap:
            src_file, hop = frontier_fwd.pop(0)
            if hop >= radius:
                continue
            for nxt in graph.forward_deps(store, src_file):
                nf = nxt["file"]
                if nf in seen_fwd_files:
                    continue
                seen_fwd_files.add(nf)
                rec = {"file": nf, "type": "imports",
                       "via": "imports",
                       "confidence": nxt["confidence"],
                       "evidence": f"{nxt.get('evidence', '')} (hop {hop + 1} via {src_file})",
                       "hop": hop + 1,
                       "through": src_file}
                transitive_fwd.append(rec)
                if len(transitive_fwd) >= cap:
                    break
                frontier_fwd.append((nf, hop + 1))
        # Tag single-hop items with hop=1 for uniformity.
        for d in rev:
            d.setdefault("hop", 1)
        for d in fwd:
            d.setdefault("hop", 1)
        rev = rev + transitive_rev
        fwd = fwd + transitive_fwd
        for d in transitive_rev:
            pack["reasons"].setdefault(d["file"], []).append(
                f"transitive reverse dep at hop {d['hop']} through "
                f"{d['through']} ({d['confidence']})")
        for d in transitive_fwd:
            pack["reasons"].setdefault(d["file"], []).append(
                f"transitive forward dep at hop {d['hop']} through "
                f"{d['through']} ({d['confidence']})")

    # Timeout checkpoint 1: after dependency graph expansion.
    _check_deadline(deadline, pack, "dependency_expansion")

    # Artifact partition: by default, deps in build output / snapshots /
    # changelogs / baselines move to `artifact_*` buckets so the primary
    # blast-radius reflects real source consumers. Mirrors cmd_symbol /
    # cmd_reverse behaviour. Opt back in via include_artifacts=True.
    artifact_rev: List[Dict[str, Any]] = []
    artifact_fwd: List[Dict[str, Any]] = []
    if not include_artifacts:
        from . import artifacts as _artifacts
        rev, artifact_rev = _artifacts.partition_refs(rev, path_key="file")
        fwd, artifact_fwd = _artifacts.partition_refs(fwd, path_key="file")
        for a in artifact_rev + artifact_fwd:
            cls, reason = _artifacts.classify_file(a.get("file") or "")
            a["artifact_class"] = cls
            a["artifact_reason"] = reason

    pack["direct_dependencies"] = fwd
    pack["reverse_dependencies"] = rev
    if artifact_rev:
        pack["artifact_reverse_dependencies"] = artifact_rev
    if artifact_fwd:
        pack["artifact_direct_dependencies"] = artifact_fwd
    if (artifact_rev or artifact_fwd) and not include_artifacts:
        pack.setdefault("unknowns", []).append({
            "kind": "artifact-deps-filtered",
            "filtered_count": len(artifact_rev) + len(artifact_fwd),
            "note": (f"{len(artifact_rev)+len(artifact_fwd)} dep(s) in build "
                     "output / snapshot / changelog / baseline paths "
                     "excluded from the primary lists. Pass "
                     "`include_artifacts=True` to include them."),
        })
    for d in fwd:
        pack["reasons"].setdefault(d["file"], []).append(
            f"imported by target ({d['confidence']})")
    for d in rev:
        verb = {"imports": "imports target",
                "pair_inspect": "pair-inspect rule",
                "references": f"references symbol ({d.get('via')})",
                "call": "calls target", "new": "instantiates target (new)",
                "import_binding": "destructures import of target",
                }.get(d.get("via") or d.get("type"), "references target")
        pack["reasons"].setdefault(d["file"], []).append(
            f"{verb} ({d['confidence']})")

    # Symbol refs
    if loc["kind"] == "symbol":
        # Use the LOCATOR's resolved identity — not the raw CLI target.
        # For `projmem pack 'projmem/packs.py#_coverage'`, `target` is the
        # full file#symbol string and the old call `symbol_refs(store, target)`
        # searched for a symbol literally named "projmem/packs.py#_coverage".
        # The locator already parsed out `name` and `file` for us.
        sid = loc.get("symbol_id")
        if sid:
            refs = graph.symbol_refs(store, sid)
        else:
            refs = graph.symbol_refs(store, loc["name"],
                                     file=loc.get("file") if loc.get("disambiguated") else None)
        pack["symbol_refs"] = {
            "defs": refs["defs"][:MAX_ITEMS_PER_BUCKET],
            "refs": refs["refs"][:MAX_ITEMS_PER_BUCKET],
            "total_defs": len(refs["defs"]),
            "total_refs": len(refs["refs"]),
        }
        for r in refs["refs"][:MAX_ITEMS_PER_BUCKET]:
            pack["reasons"].setdefault(r["file"], []).append(
                f"references symbol {loc['name']} (line {r['line']}, {r['confidence']})")

    # Semantic contract context
    sem = _semantic_context(store, files_included, target, loc)
    pack["semantic_contracts"] = sem
    for item in sem["related_files"][:MAX_ITEMS_PER_BUCKET]:
        pack["reasons"].setdefault(item["file"], []).append(
            f"shares contract {item['kind']}:{item['name']} ({item['confidence']})")

    # Tests
    if include_tests:
        tests = _guess_tests(store, files_included, target)
        pack["tests"] = tests[:MAX_ITEMS_PER_BUCKET]
        for t in tests[:MAX_ITEMS_PER_BUCKET]:
            pack["reasons"].setdefault(t["file"], []).append(
                f"heuristic test match ({t['confidence']})")

    # Entrypoints
    pack["entrypoints"] = _relevant_entrypoints(store, files_included)
    if not pack["entrypoints"]:
        pack["unknowns"].append({
            "kind": "entrypoint-unknown",
            "note": "Entrypoint reachability is unknown; no declared or detected entrypoints linked.",
        })
    # Round-3: report non-indexed entrypoints referenced in the pack so the
    # consumer knows "this entrypoint points at a file that's not in the index".
    unindexed = [e for e in pack["entrypoints"] if not e.get("indexed", 1)]
    if unindexed:
        pack["unknowns"].append({
            "kind": "entrypoint-unindexed",
            "items": [{"file": e["file"], "kind": e["kind"]} for e in unindexed],
            "note": "These entrypoints reference files not in the index. "
                    "Reachability through them cannot be verified.",
        })

    # Intra-file call graph — decisive for monolithic files where most calls
    # live inside a single module. Cheap to compute: cross-join symbols/refs
    # for the target file, attribute each call to its nearest preceding
    # function/method definition (a scope approximation; see caveat below).
    if file_path:
        pack["intra_file_calls"] = _intra_file_calls(store, file_path)
    # Symbol-scoped pack on a file#symbol target: pre-slice incoming/outgoing
    # intra-file edges for that exact symbol so the caller doesn't have to
    # run a second `callgraph --filter-to` query.
    if (loc.get("kind") == "symbol" and loc.get("disambiguated")
            and loc.get("file")):
        cg = _intra_file_calls(store, loc["file"])
        sym = loc["name"]
        pack["intra_file_for_symbol"] = {
            "incoming": [e for e in cg["edges"] if e["to"] == sym],
            "outgoing": [e for e in cg["edges"] if e["from"] == sym],
            "partial": cg.get("partial", False),
            "warnings": cg.get("warnings", []),
        }

    # Stale / freshness
    stale_files = [f for f in files_included
                   if _is_stale(store, f)]
    if stale_files:
        pack["unknowns"].append({
            "kind": "stale-index",
            "files": stale_files,
            "note": "Index is stale for these files. Run `projmem index` or `projmem refresh`.",
        })

    # Import classification. "Not resolved to a file" is NOT the same as
    # "broken" — `import os`/`import json` are known-external by construction.
    # We split into three buckets so the confidence model doesn't conflate
    # "missing in repo" with "lives in stdlib".
    #   stdlib_imports        — Python stdlib or an obvious language builtin
    #   external_imports      — bare module name, not stdlib, not on disk
    #   unresolved_repo_rel   — relative-shaped spec (./, ../, "/") that
    #                           doesn't map to an indexed or on-disk file
    stdlib_imports: List[dict] = []
    external_imports: List[dict] = []
    unresolved_repo_rel: List[dict] = []
    if file_path:
        for row in store.edges_from(file_path, type_="imports"):
            dst = str(row["dst"])
            if not dst.startswith("module:") or dst.startswith("builtin:"):
                continue
            spec = dst[len("module:"):]
            bucket = _classify_import_for_pack(file_path, spec)
            rec = {"from": row["src"], "to": dst, "spec": spec,
                   "confidence": row["confidence"]}
            if bucket == "stdlib":
                stdlib_imports.append(rec)
            elif bucket == "external":
                external_imports.append(rec)
            else:
                unresolved_repo_rel.append(rec)
    if stdlib_imports:
        pack["unknowns"].append({
            "kind": "stdlib-imports",
            "items": stdlib_imports[:20],
            "count": len(stdlib_imports),
            "note": "Known stdlib / language builtins. Not a trust concern — "
                    "listed for completeness only.",
        })
    if external_imports:
        pack["unknowns"].append({
            "kind": "external-imports",
            "items": external_imports[:20],
            "count": len(external_imports),
            "note": "Bare module specifiers resolved outside the repo "
                    "(packages, vendored deps). Not a trust concern.",
        })
    if unresolved_repo_rel:
        pack["unknowns"].append({
            "kind": "unresolved-imports",
            "items": unresolved_repo_rel[:20],
            "count": len(unresolved_repo_rel),
            "note": "Relative-shaped imports that do NOT map to an indexed "
                    "file. These lower structural trust — likely typos, "
                    "moved files, or excluded-from-index paths.",
        })

    # Coverage / confidence summary
    pack["coverage"] = _coverage(pack)

    # Round-X feedback #8: optional inline snippets — bounded byte budget,
    # labelled non-authoritative. Off by default; opt-in via build_pack
    # caller. The pack consumer can use these to skip a separate file-read
    # round-trip when the symbol/file is small enough.
    # Timeout checkpoint 2: skip snippets if budget already exceeded.
    if include_snippets and not _check_deadline(deadline, pack, "snippets"):
        pack["snippets"] = _collect_snippets(
            cfg, pack, byte_budget=snippet_bytes)

    # --include-source: when target is a symbol with a known end_line,
    # include its full body verbatim. Bypasses the snippet byte budget
    # because it's scoped to one definition the user explicitly asked
    # for. Cuts agent round-trips.
    # Timeout checkpoint 3: skip source body if budget exceeded.
    if include_source and loc.get("kind") == "symbol" and not _check_deadline(deadline, pack, "source_body"):
        for d in (loc.get("defs") or []):
            sl = d.get("line")
            el = d.get("end_line")
            if not (sl and el):
                continue
            try:
                with open(os.path.join(cfg.root, d["file"]),
                          encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                body_text = "".join(lines[sl - 1:el])
            except OSError:
                continue
            pack.setdefault("target_body", []).append({
                "file": d["file"],
                "name": d.get("name"),
                "line_range": [sl, el],
                "byte_length": len(body_text),
                "text": body_text,
                "confidence": "ast-grounded" if d.get("end_line") else "approximate",
            })

    # Expansion hints
    pack["expansion_hints"] = _hints(pack, radius)

    return pack


# ---- helpers ----

def _why(loc: dict, target: str) -> str:
    if loc["kind"] == "file":
        return f"Context for file '{target}'."
    return f"Context for symbol '{target}'."


def _dedup(items: List[dict], key: str) -> List[dict]:
    seen: Set[str] = set()
    out = []
    for it in items:
        k = it.get(key)
        if k in seen:
            continue
        seen.add(k); out.append(it)
    return out


def _semantic_context(store: Store, files: Set[str], target: str, loc: dict) -> Dict[str, Any]:
    """Gather contracts touching the target files and files that share those contracts."""
    contracts_in = []
    for f in files:
        contracts_in.extend([dict(r) for r in store.contracts_in_file(f)])

    # If target is a symbol whose name itself is a flag/token/env, include contract matches by name
    if loc["kind"] == "symbol":
        for kind in ("flag", "env", "schema_field", "token"):
            by = store.contracts_by_name(target, kind)
            if by:
                contracts_in.extend([dict(r) for r in by])

    # Bucket by kind; cap token volume (often noisy)
    by_kind: Dict[str, List[dict]] = defaultdict(list)
    for c in contracts_in:
        by_kind[c["kind"]].append(c)
    # cap tokens
    if len(by_kind.get("token", [])) > MAX_TOKEN_CONTRACTS:
        by_kind["token"] = by_kind["token"][:MAX_TOKEN_CONTRACTS]

    # Related files: other files with same contract name+kind.
    # Skip ambient contracts (shared by many files) to prevent token-noise spam.
    related: List[dict] = []
    seen_rel: Set[Tuple[str, str, str]] = set()
    ambient: List[dict] = []
    for c in contracts_in:
        peers = [p for p in store.contracts_by_name(c["name"], c["kind"])
                 if p["file"] != "<config>"]
        distinct_files = {p["file"] for p in peers}
        if len(distinct_files) > AMBIENT_CAP:
            ambient.append({"kind": c["kind"], "name": c["name"],
                            "files_count": len(distinct_files),
                            "note": f"Ambient contract: appears in {len(distinct_files)} files. "
                                    "Not pulling peers into pack."})
            continue
        for peer in peers:
            if peer["file"] in files:
                continue
            key = (peer["file"], c["kind"], c["name"])
            if key in seen_rel:
                continue
            seen_rel.add(key)
            related.append({
                "file": peer["file"], "kind": c["kind"], "name": c["name"],
                "role": peer["role"], "line": peer["line"],
                "confidence": peer["confidence"],
            })
    # Dedup ambient list
    _ambient_seen: Set[Tuple[str, str]] = set()
    ambient = [a for a in ambient
               if (a["kind"], a["name"]) not in _ambient_seen
               and not _ambient_seen.add((a["kind"], a["name"]))]
    # Rank: high confidence first, then non-token
    order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    related.sort(key=lambda r: (r["kind"] == "token", order.get(r["confidence"], 3)))

    return {
        "in_target": by_kind,
        "related_files": related[:MAX_ITEMS_PER_BUCKET * 2],
        "ambient": ambient,
        "note": "Contract detection is heuristic. High-confidence items come from "
                "argparse/click/commander parse sites, os.environ/process.env reads, "
                "struct tags, or user-declared contracts. Medium/low items are "
                "token/occurrence-based. Contracts shared by more than "
                f"{AMBIENT_CAP} files are reported as 'ambient' and do not pull "
                "peers into the pack.",
    }


def _guess_tests(store: Store, files: Set[str], target: str) -> List[dict]:
    """Best-effort: any file under tests/ that either imports one of our files or
    whose name matches the target's basename."""
    out: List[dict] = []
    # files under tests/
    for row in store.all_files():
        p = row["path"].replace("\\", "/")
        if "/tests/" not in p and not p.startswith("tests/") and "/test_" not in p and not os.path.basename(p).startswith("test_"):
            continue
        # Does it import one of our files?
        for src in files:
            for e in store.edges_from(p, type_="imports"):
                if e["dst"] == src:
                    out.append({"file": p, "confidence": e["confidence"],
                                "evidence": f"imports {src}"})
        # Name heuristic
        base = os.path.basename(next(iter(files), target))
        stem = os.path.splitext(base)[0].lstrip("_")
        if stem and stem in os.path.basename(p):
            out.append({"file": p, "confidence": "low",
                        "evidence": f"filename match on '{stem}'"})
    return _dedup(out, key="file")


def _relevant_entrypoints(store: Store, files: Set[str]) -> List[dict]:
    """Return entrypoints that directly touch the target files or whose path is in files."""
    eps = [dict(r) for r in store.entrypoints()]
    rel = []
    for ep in eps:
        if ep["file"] in files:
            rel.append({**ep, "relation": "is-target"})
        else:
            # Does entrypoint file transitively import target file? one-hop check only (bounded).
            for e in store.edges_from(ep["file"], type_="imports"):
                if e["dst"] in files:
                    rel.append({**ep, "relation": "imports-target (1 hop)"})
                    break
    return rel


def _cross_file_calls(store: Store, file_path: str,
                       cap: int = 300) -> Dict[str, Any]:
    """Cross-file call graph rooted in `file_path`. For every symbol
    defined in this file, find call-kind refs inside its body range
    whose name resolves to a def in a DIFFERENT file. Emits edges
    tagged `cross_file: True` so the consumer can merge with intra-file
    output.

    Audit fix: `callgraph` was intra-file only and returned empty for
    files like `withContext.ts` whose interesting calls all go to
    `auth/context.ts` + `auth/rbac.ts`. This makes the cross-file mode
    explicit when the caller opts in.

    Bounded: truncates at `cap` edges. Call resolution uses the
    binding pass's target_symbol_id when present (exact); else falls
    back to name-match globally (marked confidence=medium).
    """
    syms = [dict(r) for r in store.conn.execute(
        "SELECT name, kind, line, end_line FROM symbols "
        "WHERE file=? AND end_line IS NOT NULL ORDER BY line",
        (file_path,))]
    if not syms:
        return {"edges": [], "truncated": False, "total": 0,
                "warnings": [f"'{file_path}' has no symbols with end_line "
                             "(likely regex-parsed); cross-file mode needs "
                             "tree-sitter for body ranges."]}

    edges: List[Dict[str, Any]] = []
    for s in syms:
        ref_rows = list(store.conn.execute(
            "SELECT name, line, target_symbol_id FROM refs "
            "WHERE file=? AND line >= ? AND line <= ? AND kind='call'",
            (file_path, s["line"], s["end_line"])))
        for r in ref_rows:
            callee_name = r["name"]
            if not callee_name or callee_name == s["name"]:
                continue
            tsid = r["target_symbol_id"]
            if tsid:
                callee_row = store.symbol_by_id(tsid)
                if not callee_row or callee_row["file"] == file_path:
                    continue
                edges.append({
                    "from":       s["name"],
                    "to":         callee_name,
                    "to_file":    callee_row["file"],
                    "to_line":    callee_row["line"],
                    "line":       r["line"],
                    "cross_file": True,
                    "confidence": "high",
                })
            else:
                # Unbound — fall back to global name resolution.
                cands = [dict(c) for c in store.symbols_by_name(callee_name)
                         if c["file"] != file_path]
                if not cands:
                    continue
                # Unique match → accept; ambiguous → emit with low
                # confidence so the agent can see the candidate set.
                if len(cands) == 1:
                    c = cands[0]
                    edges.append({
                        "from":       s["name"],
                        "to":         callee_name,
                        "to_file":    c["file"],
                        "to_line":    c["line"],
                        "line":       r["line"],
                        "cross_file": True,
                        "confidence": "medium",
                    })
                else:
                    edges.append({
                        "from":       s["name"],
                        "to":         callee_name,
                        "to_file":    None,
                        "line":       r["line"],
                        "cross_file": True,
                        "confidence": "low",
                        "ambiguous_candidates": [
                            {"file": c["file"], "line": c["line"]}
                            for c in cands[:10]],
                    })
            if cap > 0 and len(edges) >= cap:
                break
        if cap > 0 and len(edges) >= cap:
            break
    return {
        "edges":     edges,
        "truncated": cap > 0 and len(edges) >= cap,
        "total":     len(edges),
    }


def _intra_file_calls(store: Store, file_path: str, cap: int = 300) -> Dict[str, Any]:
    """Intra-file call graph for a single file, including caller-less refs.

    Fail-loud contract: if the caller can't find either nodes or refs, it sets
    `partial: true` and populates `warnings` with the reason. The tool NEVER
    returns `edges=N, nodes=0` — that was the scanner.js-shape failure in
    real-world usage.
    """
    file_row = store.conn.execute(
        "SELECT parser, lang FROM files WHERE path=?", (file_path,)).fetchone()
    syms = [dict(r) for r in store.conn.execute(
        "SELECT name, kind, line, end_line FROM symbols WHERE file=? "
        "ORDER BY line", (file_path,))]
    sym_names = {s["name"] for s in syms}
    refs = [dict(r) for r in store.conn.execute(
        "SELECT name, line FROM refs WHERE file=? ORDER BY line", (file_path,))]

    warnings: List[str] = []
    partial = False
    if file_row is None:
        warnings.append(f"'{file_path}' is not indexed; run `projmem index` or "
                        "check --exclude patterns.")
        partial = True
    elif not syms and refs:
        warnings.append(f"'{file_path}' has {len(refs)} refs but zero symbols "
                        f"in the index (parser={file_row['parser']}). "
                        "The callgraph is incoherent — refusing to emit edges.")
        partial = True
    elif not syms:
        warnings.append(f"'{file_path}' has no indexed symbols "
                        f"(parser={file_row['parser']}).")
        partial = True
    if file_row and file_row["parser"] == "regex":
        warnings.append("Ref counts are a LOWER BOUND under the regex parser. "
                        "Install `.[treesitter]` for AST-grounded intra-file refs.")

    # Refuse to emit edges without nodes.
    if partial and not syms:
        return {
            "nodes": [], "edges": [], "total": 0, "truncated": False,
            "by_caller_count": {}, "partial": True, "warnings": warnings,
            "parser": file_row["parser"] if file_row else None,
            "note": "Incomplete result — see `warnings`.",
        }

    scope_syms = [s for s in syms if s["kind"] in ("function", "method",
                                                    "exported", "var")]
    # M2: when `end_line` is available on a scope symbol, use EXACT enclosing
    # range to attribute a ref to its caller. Falls back to the nearest-
    # preceding-function approximation ONLY when end_line is missing
    # (regex backend or legacy rows). Fixes nested-anonymous-closure
    # mis-attribution on monoliths.
    has_ranges = any(s.get("end_line") for s in scope_syms)
    edges: List[dict] = []
    if has_ranges:
        # Index by line → first scope whose [line, end_line] contains it
        scope_intervals = [s for s in scope_syms if s.get("end_line")]
        for ref in refs:
            if ref["name"] not in sym_names:
                continue
            # Inner-most containing scope: the one with largest start_line
            # whose start_line <= ref.line <= end_line.
            caller = None
            for s in scope_intervals:
                if s["line"] <= ref["line"] <= s["end_line"]:
                    if caller is None or s["line"] > caller["line"]:
                        caller = s
            caller_name = caller["name"] if caller else "<file-scope>"
            if caller_name == ref["name"]:
                continue
            edges.append({"from": caller_name, "to": ref["name"],
                          "line": ref["line"]})
    else:
        caller_idx = 0
        for ref in refs:
            if ref["name"] not in sym_names:
                continue
            while (caller_idx + 1 < len(scope_syms)
                   and scope_syms[caller_idx + 1]["line"] <= ref["line"]):
                caller_idx += 1
            caller = (scope_syms[caller_idx]["name"]
                      if scope_syms and scope_syms[caller_idx]["line"] <= ref["line"]
                      else None)
            if caller == ref["name"]:
                continue
            edges.append({"from": caller or "<file-scope>",
                          "to": ref["name"], "line": ref["line"]})
    # IMPORTANT: compute stats from the FULL edge list before slicing to `cap`.
    # Round-2 report: `by_caller_count` was previously computed on the
    # already-truncated list, hiding dominant callers like scanUrl/main in
    # large files. Fixed here.
    by_caller: Dict[str, int] = {}
    for e in edges:
        by_caller[e["from"]] = by_caller.get(e["from"], 0) + 1
    return {
        "nodes": [{"name": s["name"], "kind": s["kind"], "line": s["line"]}
                  for s in syms],
        "edges": edges if cap <= 0 else edges[:cap],
        "truncated": cap > 0 and len(edges) > cap,
        "total": len(edges),
        "by_caller_count": dict(sorted(by_caller.items(),
                                        key=lambda kv: -kv[1])[:50]),
        "partial": partial,
        "warnings": warnings,
        "parser": file_row["parser"] if file_row else None,
        "note": ("Caller attribution uses EXACT enclosing-range containment "
                 "when symbol end-lines are available (M2); otherwise falls "
                 "back to the nearest-preceding-function scope approximation "
                 "labelled confidence=medium. "
                 "Refs tagged parser=regex are a LOWER BOUND, not exact. "
                 "`by_caller_count` is computed from ALL edges before "
                 "truncation; `edges` may be sliced to `cap`."),
        "caller_attribution": "exact_range" if has_ranges else "scope_approximation",
    }


def _collect_snippets(cfg: Config, pack: dict, byte_budget: int) -> dict:
    """Bounded code excerpts around target symbols and direct dep imports.
    Hard byte budget; rows tagged `confidence: low` so consumers know these
    are anchors-with-context, not ground truth (real source on disk wins)."""
    import os as _os
    spent = 0
    snips: list = []
    target = pack.get("target", {})

    def _grab(file: str, start: int, end: int, why: str) -> None:
        nonlocal spent
        if spent >= byte_budget: return
        try:
            with open(_os.path.join(cfg.root, file), encoding="utf-8",
                      errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        s = max(1, start) - 1
        e = min(len(lines), end)
        text = "".join(lines[s:e])
        max_take = byte_budget - spent
        if len(text) > max_take:
            text = text[:max_take] + "\n…[truncated]"
        spent += len(text)
        snips.append({"file": file, "start_line": start, "end_line": end,
                      "bytes": len(text), "why": why, "text": text})

    if target.get("kind") == "symbol":
        for d in target.get("defs", []) or []:
            sl = d.get("line") or 1
            el = d.get("end_line") or (sl + 30)
            _grab(d["file"], sl, min(el, sl + 80),
                  f"def of `{d.get('name')}`")
    elif target.get("kind") == "file":
        # Show top of file (header / imports / brief module summary)
        _grab(target.get("path"), 1, 40, "file head")

    return {
        "items": snips, "bytes_spent": spent, "byte_budget": byte_budget,
        "truncated": spent >= byte_budget,
        "note": "Bounded excerpts. Treat as navigation anchors; canonical "
                "source remains on disk. Confidence: low (this is a copy).",
    }


def _is_stale(store: Store, path: str) -> bool:
    row = store.get_file(path)
    return bool(row and row["stale"])


def _coverage(pack: dict) -> Dict[str, Any]:
    """Coverage now reports THREE confidence views so a JS-heavy or token-heavy
    pack doesn't poison the structural navigation signal:

      structural_confidence  — imports, reverse deps, symbol refs (nav-quality)
      contract_confidence    — semantic co-occurrence edges (inference-quality)
      overall_confidence     — min(structural, contract), kept for back-compat

    A consumer that only cares about "can I follow these edges?" should read
    structural_confidence and ignore contract_confidence.
    """
    structural_counts: Counter = Counter()
    contract_counts: Counter = Counter()

    for bucket in ("direct_dependencies", "reverse_dependencies"):
        for it in pack.get(bucket, []):
            # Known external / stdlib imports do NOT represent structural
            # edges we failed to resolve — they resolve, just off-repo.
            # Counting them as medium-confidence structural signal was the
            # root cause of `pack projmem/packs.py` landing at
            # structural_confidence=medium purely because of `import os`.
            dst = it.get("file") or ""
            if dst.startswith("module:") or dst.startswith("builtin:"):
                continue
            structural_counts[it.get("confidence", "unknown")] += 1
    for rel in pack.get("semantic_contracts", {}).get("related_files", []):
        contract_counts[rel.get("confidence", "unknown")] += 1

    struct_conf = (confidence.min_conf(structural_counts.keys())
                   if structural_counts else "unknown")
    cont_conf = (confidence.min_conf(contract_counts.keys())
                 if contract_counts else "unknown")

    # overall = lower bound across everything, same as before for back-compat.
    combined = Counter(structural_counts)
    combined.update(contract_counts)
    overall = confidence.min_conf(combined.keys()) if combined else "unknown"

    heuristic = sum(v for k, v in combined.items() if k in ("medium", "low"))
    total = sum(combined.values())
    parser_used = (pack["target"].get("parser")
                   if pack["target"].get("kind") == "file" else "mixed")
    # Count the actual unresolved ITEMS, not the number of unknowns ENTRIES.
    # Only `unresolved-imports` (repo-relative, missing-on-disk) feeds this
    # count — `stdlib-imports` and `external-imports` are informational.
    unresolved_items = 0
    stdlib_items = 0
    external_items = 0
    for u in pack.get("unknowns", []):
        kind = u.get("kind")
        items = u.get("items") or []
        if kind == "unresolved-imports":
            unresolved_items += u.get("count", len(items))
        elif kind == "stdlib-imports":
            stdlib_items += u.get("count", len(items))
        elif kind == "external-imports":
            external_items += u.get("count", len(items))

    return {
        "structural_confidence": struct_conf,
        "contract_confidence": cont_conf,
        "overall_confidence": overall,
        "by_level": {"structural": dict(structural_counts),
                     "contract": dict(contract_counts)},
        "heuristic_edges": heuristic,
        "total_edges": total,
        "parser_used": parser_used,
        "missing_ref_kinds": _missing_ref_kinds_for(parser_used),
        "unresolved_count": unresolved_items,
        "stdlib_import_count": stdlib_items,
        "external_import_count": external_items,
        "note": ("structural_confidence = navigation quality (imports/refs). "
                 "contract_confidence = semantic-inference quality "
                 "(token/schema co-occurrence). overall = lower bound of both. "
                 "`missing_ref_kinds` names ref kinds the parser for this "
                 "target does NOT capture — counts are a lower bound. "
                 "`unresolved_count` counts repo-relative imports that did "
                 "NOT resolve; stdlib/external imports are reported "
                 "separately and do NOT lower structural trust."),
    }


# Per-parser list of ref kinds NOT captured. Drives `coverage.missing_ref_kinds`
# and `coverage.backend_limitations` so a consumer can decide when to fall
# back to grep. Round-3 report #13.
_MISSING_REF_KINDS = {
    # JS regex fallback can't distinguish kinds and misses non-call uses.
    "regex": ["new", "import_binding", "identifier_read", "property_access"],
    # Python AST captures: Name loads, leftmost name of attribute chains,
    # and method-call names (e.g. `module.func()` → ref on `func`).
    # Still misses: bare property READS in non-call contexts (e.g.
    # `config.db_url` used as a value, not called).
    "ast": ["property_access_non_call"],
    "treesitter:javascript": ["identifier_read", "property_access"],
    "treesitter:typescript": ["identifier_read", "property_access"],
    "treesitter:tsx":        ["identifier_read", "property_access"],
    "treesitter:go":         ["identifier_read", "property_access"],
    "treesitter:rust":       ["identifier_read", "property_access"],
    "treesitter:c":          ["identifier_read"],
    "treesitter:cpp":        ["identifier_read"],
    "treesitter:java":       ["identifier_read", "property_access"],
    "treesitter:ruby":       ["identifier_read", "property_access"],
    "treesitter:python":     ["property_access"],
}


def _missing_ref_kinds_for(parser_used) -> List[str]:
    if not parser_used or parser_used == "mixed":
        return ["varies_by_file"]
    return _MISSING_REF_KINDS.get(parser_used, [])


def _hints(pack: dict, radius: int) -> List[str]:
    hints = []
    if pack.get("unknowns"):
        hints.append("Unknowns reported — inspect `unknowns` before trusting the pack as complete.")
    if pack["coverage"].get("heuristic_edges", 0) > 0:
        hints.append("Some edges are heuristic. Open the cited files at the anchor line to verify.")
    if radius < 2:
        hints.append("Increase `--radius 2` for transitive reverse-deps one hop further (bounded).")
    else:
        transitive = [d for d in pack.get("reverse_dependencies", [])
                      if d.get("hop", 1) > 1]
        if transitive:
            hints.append(
                f"Radius {radius} expansion added {len(transitive)} "
                "transitive reverse-dep(s). Each carries `hop` + `through` "
                "to trace the path.")
    if not pack.get("entrypoints"):
        hints.append("No entrypoints linked. Declare one in .projmem/config.json under 'entrypoints' for better reachability context.")
    return hints


def write_pack(cfg: Config, pack: dict, name: Optional[str] = None) -> str:
    """Persist a pack to disk under `cfg.packs_dir`. The on-disk filename
    is filesystem-safe (slashes replaced, control chars stripped) via
    `projmem.security.safe_filename`."""
    from . import security as _sec
    os.makedirs(cfg.packs_dir, exist_ok=True)
    base = name or _safe_target_name(pack)
    safe = _sec.safe_filename(base)
    path = os.path.join(cfg.packs_dir, f"{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pack, f, indent=2)
    return path


def _safe_target_name(pack: dict) -> str:
    t = pack["target"]
    return t.get("path") or t.get("name") or "pack"


def render_markdown(pack: dict) -> str:
    t = pack["target"]
    out = [f"# Context Pack: {t.get('path') or t.get('name')}",
           f"_{pack['why']}_", ""]
    cov = pack["coverage"]
    # Round-3 report #6: lead with structural_confidence. The "overall: low"
    # reading was causing consumers to discard packs that have AST-grounded
    # navigation but heuristic contract edges — exactly backwards.
    out.append(f"**Structural confidence** (navigation quality): "
               f"`{cov.get('structural_confidence', 'unknown')}`")
    out.append(f"**Contract confidence** (semantic inference): "
               f"`{cov.get('contract_confidence', 'unknown')}`")
    out.append(f"_Overall (lower bound of both): "
               f"`{cov['overall_confidence']}`  —  "
               f"heuristic edges {cov['heuristic_edges']}/{cov['total_edges']}_")
    missing = cov.get("missing_ref_kinds") or []
    if missing:
        out.append(f"**Missing ref kinds** for parser `{cov.get('parser_used')}`: "
                   f"{', '.join(missing)} — counts are a lower bound.")
    if pack.get("unknowns"):
        out.append("\n## Unknowns / Unresolved")
        for u in pack["unknowns"]:
            out.append(f"- **{u['kind']}**: {u.get('note','')}")
    out.append("\n## Reverse Dependencies (who depends on target)")
    for d in pack["reverse_dependencies"]:
        out.append(f"- `{d['file']}` — {d['type']} ({d['confidence']})")
    out.append("\n## Direct Dependencies")
    for d in pack["direct_dependencies"]:
        out.append(f"- `{d['file']}` — {d['type']} ({d['confidence']})")
    sem = pack["semantic_contracts"]
    out.append("\n## Semantic Contracts Touching Target")
    for k, items in sem["in_target"].items():
        out.append(f"### {k}")
        for c in items[:15]:
            out.append(f"- `{c['name']}` in `{c['file']}`:{c['line']} role={c['role']} ({c['confidence']})")
    out.append("\n### Files sharing contracts")
    for r in sem["related_files"][:20]:
        out.append(f"- `{r['file']}` — {r['kind']}:{r['name']} role={r['role']} ({r['confidence']})")
    if pack.get("tests"):
        out.append("\n## Related Tests")
        for t in pack["tests"]:
            out.append(f"- `{t['file']}` — {t['evidence']} ({t['confidence']})")
    if pack.get("entrypoints"):
        out.append("\n## Entrypoints")
        for e in pack["entrypoints"]:
            out.append(f"- `{e['file']}` — {e['kind']} ({e['confidence']}, {e.get('relation','')})")
    out.append("\n## Inclusion Reasons")
    for f, reasons in pack["reasons"].items():
        out.append(f"- `{f}`")
        for r in reasons:
            out.append(f"  - {r}")
    out.append("\n## Expansion Hints")
    for h in pack["expansion_hints"]:
        out.append(f"- {h}")
    return "\n".join(out)
