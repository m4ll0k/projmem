"""projmem/checklist.py — post-edit completeness gate.

Motivation: LLM-driven edits frequently fail by *incompleteness* rather than
syntax: change A but forget B; rename a thing but miss one call site; add a
flag/env/schema field but never update the consumer side.

`projmem checklist` is a single command that surfaces the most common,
actionable "you forgot something" classes with explicit severity and
suggestions. It is intentionally conservative: it prefers a few high-signal
checks over a noisy lint pass.

Checks (current version):
  - Contract obligations from contract-diff (added-orphan / removed-dangling)
  - Dangling symbol refs for removed names that are now undefined
  - Unresolved repo-relative imports that are missing on disk

This is not a proof engine. It is a low-friction completeness gate that an
agent can run after edits and before claiming "done".
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import artifacts as _artifacts
from . import contract_diff as _contract_diff
from .scope import get_vendor_prefixes, is_in_scope


# Severity enum + ordering for sorting the finding list.
INFO = "info"
WARNING = "warning"
HIGH = "high"

_SEVERITY_ORDER = {HIGH: 0, WARNING: 1, INFO: 2}


def _finding(severity: str, code: str, message: str,
             suggestion: Optional[str] = None,
             **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if suggestion:
        out["suggestion"] = suggestion
    if extra:
        out["details"] = extra
    return out


def _labels(store) -> Tuple[Set[str], Set[str]]:
    """Return (contract_snapshot_labels, symbol_snapshot_labels)."""
    contract = {s["label"] for s in store.list_snapshots()}
    symbols = {s["label"] for s in store.list_symbol_snapshots()}
    return contract, symbols


def _symbol_diff_rows(store, base: str, head: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (base_rows, head_rows) for symbol snapshots."""
    base_rows = store.symbol_snapshot_rows(base)
    if head == "current":
        head_rows = store.live_symbol_rows()
    else:
        head_rows = store.symbol_snapshot_rows(head)
    return base_rows, head_rows


def _symbol_removed_names(base_rows: Sequence[Dict[str, Any]],
                          head_rows: Sequence[Dict[str, Any]]) -> Set[str]:
    """Names that had at least one def in base and have ZERO defs in head."""
    base_names = {r.get("name") for r in base_rows if r.get("name")}
    head_names = {r.get("name") for r in head_rows if r.get("name")}
    return {n for n in base_names if n and n not in head_names}


def _iter_ref_sites(store, name: str, *,
                    vendor_prefixes: Iterable[str],
                    scope_only: bool,
                    include_artifacts: bool,
                    limit: int = 50) -> Tuple[int, List[Dict[str, Any]]]:
    """Return (total_count, example_sites) for refs to `name`.

    Filters:
      - scope_only: drop rows whose source file is under a vendor prefix
      - include_artifacts: when False, drop artifact paths
    """
    try:
        total = int(store.conn.execute(
            "SELECT COUNT(*) FROM refs WHERE name=?", (name,)).fetchone()[0])
    except Exception:
        total = 0

    sites: List[Dict[str, Any]] = []
    for r in store.conn.execute(
            "SELECT file, line, kind, confidence, roles FROM refs "
            "WHERE name=? ORDER BY file, line LIMIT ?",
            (name, limit)):
        file = r["file"]
        if scope_only and not is_in_scope(file, vendor_prefixes):
            continue
        if (not include_artifacts) and _artifacts.is_artifact_path(file or ""):
            continue
        sites.append({
            "file": file,
            "line": int(r["line"]) if r["line"] is not None else None,
            "kind": r["kind"],
            "confidence": r["confidence"],
            "roles": int(r["roles"] or 0),
        })
    return total, sites


_PATH_EXTS = (
    "", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".rs",
    ".h", ".hpp", ".hh", ".hxx",
    ".c", ".cc", ".cpp", ".cxx",
    ".java", ".rb", ".scala", ".kt", ".swift", ".php",
)


def _probe_repo_relative_exists(repo_root: str, src_file: str, spec: str) -> bool:
    """Best-effort: does a repo-relative-looking spec exist on disk?"""
    s = (spec or "").strip().strip("\"'").strip()
    if not s:
        return False
    # Root import in GN style (`//foo/bar`) — treat as external root import.
    if s.startswith("//"):
        # Remove leading //, probe at root.
        s = s[2:]
        base = ""
    elif s.startswith("/"):
        s = s.lstrip("/")
        base = ""
    elif s.startswith("."):
        base = os.path.dirname(src_file or "")
    else:
        # Not a relative spec — caller should not have asked us.
        base = os.path.dirname(src_file or "")
    probe_base = os.path.normpath(os.path.join(base, s)) if base else os.path.normpath(s)
    for ext in _PATH_EXTS:
        cand = probe_base + ext
        if os.path.isfile(os.path.join(repo_root, cand)):
            return True
    return False


def _looks_repo_relative(spec: str) -> bool:
    """Heuristic: is this import spec intended to resolve to a repo file?"""
    s = (spec or "").strip().strip("\"'").strip()
    if not s:
        return False
    if s.startswith("@"):
        return False  # scoped npm package, almost certainly external
    if s.startswith((".", "/", "//")):
        return True
    # Path with a real extension is likely file-backed even without dot-prefix.
    _, ext = os.path.splitext(s)
    if ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
               ".py", ".go", ".rs", ".c", ".cc", ".cpp", ".cxx",
               ".h", ".hpp", ".java", ".rb", ".kt", ".swift", ".php", ".scala"):
        return True
    return False


def _cross_layer_enum_mismatches(store) -> List[Dict[str, Any]]:
    """Detect enums with the same name declared in multiple layers but
    with divergent member sets. Each row in `contracts` with kind=
    'enum_shape' carries `context` of the form `<layer>:m1,m2,m3` where
    `<layer>` is one of {ts, prisma, sql, rust}. Group by name, compare
    member sets across layers; emit a finding when ≥2 layers exist for
    the same name and any pair has a non-empty symmetric difference.
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    try:
        rows = list(store.conn.execute(
            "SELECT name, file, line, context FROM contracts "
            "WHERE kind='enum_shape'"))
    except Exception:
        return []
    for r in rows:
        name = r["name"]
        ctx = r["context"] or ""
        if ":" in ctx:
            layer, mlist = ctx.split(":", 1)
        else:
            layer, mlist = "?", ctx
        members = sorted({m for m in mlist.split(",") if m})
        by_name.setdefault(name, []).append({
            "layer": layer,
            "file": r["file"],
            "line": int(r["line"]) if r["line"] is not None else None,
            "members": members,
        })
    out: List[Dict[str, Any]] = []
    for name, sites in by_name.items():
        # Need at least two DIFFERENT layers (a TS enum re-declared in a
        # second TS file is a separate concern handled by symbol parity).
        layers = {s["layer"] for s in sites}
        if len(layers) < 2:
            continue
        # Reduce to one (layer → member-set) per layer (use the first
        # site if a layer appears multiple times — repeats within a layer
        # would indicate a different bug).
        per_layer: Dict[str, set] = {}
        layer_files: Dict[str, str] = {}
        for s in sites:
            if s["layer"] not in per_layer:
                per_layer[s["layer"]] = set(s["members"])
                layer_files[s["layer"]] = f"{s['file']}:{s['line']}"
        # Any pair with a symmetric difference?
        layer_keys = sorted(per_layer.keys())
        diverged = False
        diffs: List[Dict[str, Any]] = []
        for i, a in enumerate(layer_keys):
            for b in layer_keys[i+1:]:
                only_a = sorted(per_layer[a] - per_layer[b])
                only_b = sorted(per_layer[b] - per_layer[a])
                if only_a or only_b:
                    diverged = True
                    diffs.append({
                        "layer_a": a, "layer_b": b,
                        f"only_in_{a}": only_a,
                        f"only_in_{b}": only_b,
                    })
        if diverged:
            out.append({
                "name": name,
                "layers": [
                    {"layer": k, "site": layer_files[k],
                     "members": sorted(per_layer[k])}
                    for k in layer_keys
                ],
                "pair_diffs": diffs,
            })
    out.sort(key=lambda d: d["name"])
    return out


def _unresolved_repo_relative_imports(store, repo_root: str, *,
                                      vendor_prefixes: Sequence[str],
                                      scope_only: bool,
                                      limit: int = 200) -> List[Dict[str, Any]]:
    """Return unresolved imports that look repo-relative AND are missing on disk."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for r in store.conn.execute(
            "SELECT src, dst, evidence, confidence FROM edges "
            "WHERE type='imports' AND dst LIKE 'module:%' "
            "ORDER BY src, dst"):
        src = r["src"]
        if scope_only and not is_in_scope(src, vendor_prefixes):
            continue
        dst = r["dst"] or ""
        spec = dst[len("module:"):] if dst.startswith("module:") else dst
        if not _looks_repo_relative(spec):
            continue
        key = (src, spec)
        if key in seen:
            continue
        seen.add(key)
        exists = _probe_repo_relative_exists(repo_root, src, spec)
        if exists:
            # Exists on disk (likely scope misconfiguration). Not the
            # "missing file" class; leave to `missing-paths` / scope debug.
            continue
        out.append({
            "src": src,
            "spec": spec,
            "confidence": r["confidence"],
            "evidence": r["evidence"],
        })
        if len(out) >= limit:
            break
    return out


def run(cfg, store, *,
        base: str = "pre-index",
        head: str = "current",
        scope_only: bool = True,
        include_artifacts: bool = False,
        limit: int = 200) -> Dict[str, Any]:
    """Execute the checklist and return a consolidated report.

    `base` and `head` refer to snapshot labels; `head='current'` means the
    live index tables.
    """
    findings: List[Dict[str, Any]] = []

    contract_labels, symbol_labels = _labels(store)
    if base not in contract_labels:
        findings.append(_finding(
            HIGH, "missing_contract_snapshot",
            f"Contract snapshot '{base}' not found.",
            suggestion="Run `projmem index` (auto-creates 'pre-index') or "
                       "`projmem snapshot <label>` to create a baseline.",
            available=sorted(contract_labels)[:50],
        ))
    if base not in symbol_labels:
        findings.append(_finding(
            HIGH, "missing_symbol_snapshot",
            f"Symbol snapshot '{base}' not found.",
            suggestion="Run `projmem index` (auto-creates 'pre-index') or "
                       "`projmem snapshot <label> --symbols` to create one.",
            available=sorted(symbol_labels)[:50],
        ))
    if head != "current":
        if head not in contract_labels:
            findings.append(_finding(
                HIGH, "missing_contract_snapshot_head",
                f"Contract snapshot head '{head}' not found.",
                suggestion="Use `--head current` or run `projmem snapshot <label>`.",
                available=sorted(contract_labels)[:50],
            ))
        if head not in symbol_labels:
            findings.append(_finding(
                HIGH, "missing_symbol_snapshot_head",
                f"Symbol snapshot head '{head}' not found.",
                suggestion="Use `--head current` or run `projmem snapshot <label> --symbols`.",
                available=sorted(symbol_labels)[:50],
            ))

    # Abort early if we can't diff.
    can_contract = base in contract_labels and (head == "current" or head in contract_labels)
    can_symbol = base in symbol_labels and (head == "current" or head in symbol_labels)

    vendor_prefixes = get_vendor_prefixes(store)

    # ------------------------------------------------------------------
    # 1) Contract obligations (flag/env/schema/event/entrypoint drift).
    # ------------------------------------------------------------------
    if can_contract:
        base_rows = store.snapshot_rows(base)
        head_rows = store.live_contract_rows() if head == "current" else store.snapshot_rows(head)
        diff = _contract_diff.compute_diff(store, base_rows, head_rows, kinds=None)
        obligations = _contract_diff.project_obligations(diff)
        open_obs = obligations.get("open_obligations") or []
        if open_obs:
            findings.append(_finding(
                HIGH, "open_contract_obligations",
                f"{len(open_obs)} open contract obligation(s) detected "
                f"between snapshots {base!r} → {head!r}.",
                suggestion=("Inspect `projmem contract-diff --base "
                            f"{base} --head {head} --as-obligations` and "
                            "update the missing consumer/dangling sites."),
                open_obligations=open_obs[: min(50, len(open_obs))],
                coverage_debt=obligations.get("coverage_debt"),
                kinds=diff.get("kinds"),
                summary=diff.get("summary"),
            ))
        else:
            # Still useful to surface as INFO when there were changes but
            # no obligations — helps agents confirm "nothing contract-shaped
            # drifted".
            summ = diff.get("summary") or {}
            if any((summ.get("added"), summ.get("removed"), summ.get("moved"))):
                findings.append(_finding(
                    INFO, "contract_diff_no_obligations",
                    f"Contracts changed (added={summ.get('added')}, "
                    f"removed={summ.get('removed')}, moved={summ.get('moved')}) "
                    "but no open obligations were detected.",
                    summary=summ,
                ))

    # ------------------------------------------------------------------
    # 2) Dangling refs for removed symbol names that are now undefined.
    # ------------------------------------------------------------------
    if can_symbol:
        base_rows, head_rows = _symbol_diff_rows(store, base, head)
        removed_names = sorted(_symbol_removed_names(base_rows, head_rows))
        # Bound work: in pathological cases (huge refactors) this can be large.
        max_names = 2000
        truncated_names = len(removed_names) > max_names
        if truncated_names:
            removed_names = removed_names[:max_names]
        dangling: List[Dict[str, Any]] = []
        for n in removed_names:
            total, sites = _iter_ref_sites(
                store, n,
                vendor_prefixes=vendor_prefixes,
                scope_only=scope_only,
                include_artifacts=include_artifacts,
                limit=20,
            )
            if sites:
                dangling.append({
                    "name": n,
                    "ref_count": total,
                    "example_sites": sites,
                })
            if len(dangling) >= 50:
                break
        if dangling:
            findings.append(_finding(
                HIGH, "dangling_symbol_refs",
                f"{len(dangling)} undefined symbol name(s) still referenced "
                f"after changes ({base!r} → {head!r}).",
                suggestion=("Run `projmem parity` for a broader view, or "
                            "update/remove the remaining call sites."),
                dangling=dangling,
                scope_only=scope_only,
                include_artifacts=include_artifacts,
                truncated_removed_name_scan=truncated_names,
            ))

    # ------------------------------------------------------------------
    # 3) Cross-layer enum shape mismatch.
    # An enum named X declared in two layers (TS code, Prisma DSL, SQL
    # CREATE TYPE) must have the SAME member set, otherwise serialized
    # values from one side won't deserialize on the other. Audit case:
    # `NotificationType` had `INFO|WARN|ERROR` in TS but only `INFO|WARN`
    # in Prisma, and `complete` returned "ok" because the per-layer diff
    # buckets by (kind, name) within a single snapshot — never compares
    # the same name across layers.
    enum_mismatches = _cross_layer_enum_mismatches(store)
    if enum_mismatches:
        findings.append(_finding(
            HIGH, "cross_layer_enum_mismatch",
            f"{len(enum_mismatches)} enum(s) declared in multiple layers "
            "with divergent member sets.",
            suggestion=(
                "Reconcile the enum members across layers (e.g. update "
                "the Prisma schema enum to match the TS enum, then "
                "regenerate the client). Each entry lists per-layer "
                "members and the symmetric difference."),
            mismatches=enum_mismatches,
        ))

    # ------------------------------------------------------------------
    # 4) Unresolved repo-relative imports missing on disk.
    # ------------------------------------------------------------------
    unresolved_missing = _unresolved_repo_relative_imports(
        store, cfg.root,
        vendor_prefixes=vendor_prefixes,
        scope_only=scope_only,
        limit=limit,
    )
    if unresolved_missing:
        findings.append(_finding(
            HIGH, "unresolved_repo_relative_imports_missing_on_disk",
            f"{len(unresolved_missing)} repo-relative import(s) could not be "
            "resolved and do not exist on disk.",
            suggestion=("Run `projmem unresolved-imports --kind repo_relative "
                        "--only-missing-on-disk --scope-only` for the full "
                        "list and fix broken paths or scope configuration."),
            items=unresolved_missing,
            scope_only=scope_only,
        ))

    # Sort by severity (high first), stable by code.
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9),
                                 f["code"]))
    counts = {
        HIGH: sum(1 for f in findings if f["severity"] == HIGH),
        WARNING: sum(1 for f in findings if f["severity"] == WARNING),
        INFO: sum(1 for f in findings if f["severity"] == INFO),
    }
    overall = ("ok" if counts[HIGH] == 0 and counts[WARNING] == 0
               else "attention_needed" if counts[HIGH] == 0
               else "unhealthy")
    return {
        "schema_version": 1,
        "root": cfg.root,
        "indexed_root": store.get_meta("root"),
        "base": base,
        "head": head,
        "scope_only": scope_only,
        "vendor_prefixes": vendor_prefixes,
        "overall": overall,
        "severity_counts": counts,
        "findings": findings,
        "note": (
            "Checklist is conservative: it surfaces high-signal completeness "
            "failures (contract obligations, dangling refs, broken relative "
            "imports). It is not exhaustive program analysis."
        ),
    }
