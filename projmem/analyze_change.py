"""projmem/analyze_change.py — change-impact analysis for one target.

"I'm about to change X — what will break?" This is the agent's
highest-leverage pre-edit question. `projmem pack` returns context; this
command returns BLAST RADIUS with specific jump-to-site coordinates.

Returns:
  target:                 resolved target (file or file#symbol)
  direct_dependents:      files importing / referencing the target
  indirect_dependents:    transitive reverse reach (bounded radius)
  tests_affected:         test files that touch the target
  contracts_affected:     contracts declared / consumed by the target file
  likely_forgotten_updates: dangling refs + orphan obligations that usually
                            mean "you forgot to update X alongside Y"
  coverage:               counts and a confidence signal
  next_steps_hint:        concrete next actions

Bounded (~5-15 KB on a normal-sized change), fully machine-readable.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Set


def analyze_change(store, cfg, target: str,
                   radius: int = 2,
                   max_per_bucket: int = 50
                   ) -> Dict[str, Any]:
    """Compute change-impact for `target`. See module docstring."""
    from . import graph as _graph
    from . import symbols as _symbols
    from . import artifacts as _artifacts

    loc = _symbols.locate(store, target, project_root=cfg.root)
    out: Dict[str, Any] = {
        "target": {
            "raw":        target,
            "kind":       loc.get("kind"),
            "resolved":   loc.get("path") or loc.get("file"),
            "name":       loc.get("name"),
            "symbol_id":  loc.get("symbol_id"),
        },
        "radius": radius,
    }

    # Resolve the "file" form we'll use for most graph queries.
    file_target: Optional[str] = None
    if loc.get("kind") == "file":
        file_target = loc.get("path")
    elif loc.get("kind") == "symbol":
        file_target = loc.get("file")

    # 1. Direct reverse deps — files with a structural edge pointing IN.
    # Round-4 finding #6: `reverse` and `analyze-change` previously
    # disagreed on the dependents list because this path silently
    # truncated to `max_per_bucket` while `reverse` returned the full
    # set. Now we keep the entire source set under `direct_dependents`
    # and surface a `direct_dependents_truncated` field only when the
    # cap actually engaged. The two commands now produce IDENTICAL
    # source-deps sets for the same target.
    direct: List[Dict[str, Any]] = []
    direct_total = 0
    if file_target:
        direct_raw = _graph.reverse_deps(store, file_target)
        # Separate source from artifact so the blast radius reflects real
        # consumers, not baselines / changelogs.
        source, artifact_consumers = _artifacts.partition_refs(
            direct_raw, path_key="file")
        direct_total = len(source)
        direct = source  # full set; cap is informational, not destructive
    else:
        artifact_consumers = []

    # 2. Indirect (transitive) dependents. BFS up the import graph.
    # F003: surface BOTH the (capped) returned list AND a `_total`
    # counter so coverage doesn't lie. Previously the cap looked like
    # the true count, hiding inflated blast radius from the caller.
    indirect: List[Dict[str, Any]] = []
    indirect_total = 0
    indirect_truncated = False
    if file_target and radius >= 2:
        seen_files: Set[str] = {file_target} | {d["file"] for d in direct}
        frontier = [(d["file"], 1) for d in direct]
        # Walk the WHOLE frontier; cap only the returned list, not the
        # total. The cap is x2 to mirror the existing "soft headroom"
        # for the returned slice; total counts everything we visited.
        all_indirect: List[Dict[str, Any]] = []
        while frontier:
            src_file, hop = frontier.pop(0)
            if hop >= radius:
                continue
            for nxt in _graph.reverse_deps(store, src_file):
                nf = nxt["file"]
                if nf in seen_files:
                    continue
                if _artifacts.is_artifact_path(nf or ""):
                    continue
                seen_files.add(nf)
                all_indirect.append({
                    "file":       nf,
                    "hop":        hop + 1,
                    "through":    src_file,
                    "type":       nxt.get("type"),
                    "confidence": nxt.get("confidence"),
                })
                frontier.append((nf, hop + 1))
        indirect_total = len(all_indirect)
        indirect = all_indirect[:max_per_bucket]
        indirect_truncated = indirect_total > len(indirect)

    # 3. Tests affected — heuristic: any file whose path contains /test or
    # a `test_` / `_test` / `.test.` / `.spec.` pattern AND depends on the
    # target (directly or transitively). F003: track total separately.
    tests: List[Dict[str, Any]] = []
    tests_total = 0
    tests_truncated = False
    dependent_files = {d["file"] for d in direct} | {d["file"] for d in indirect}
    if file_target:
        dependent_files.add(file_target)  # tests might live next to source
    for f in sorted(dependent_files):
        if _is_test_file(f):
            tests_total += 1
            if len(tests) < max_per_bucket:
                tests.append({"file": f, "reason": "depends-on-target"})
            else:
                tests_truncated = True

    # 4. Contracts affected — env / flag / schema_field declared or read
    # in any of the dependent files OR in the target file itself. F003:
    # query without an artificial `LIMIT max_per_bucket * 2`; surface
    # `*_total` so callers know if they hit a real ceiling.
    contracts: List[Dict[str, Any]] = []
    contracts_total = 0
    contracts_truncated = False
    contract_files = list(dependent_files)[:100]   # cap
    if file_target and file_target not in contract_files:
        contract_files.insert(0, file_target)
    placeholders = ",".join("?" * len(contract_files)) or "''"
    if contract_files:
        rows = list(store.conn.execute(
            f"SELECT kind, name, file, line, role "
            f"FROM contracts WHERE file IN ({placeholders}) "
            f"AND role != 'occurrence'",
            tuple(contract_files)))
        contracts_total = len(rows)
        all_contracts = [{"kind": r["kind"], "name": r["name"],
                            "file": r["file"], "line": int(r["line"]),
                            "role": r["role"]}
                           for r in rows]
        contracts = all_contracts[:max_per_bucket]
        contracts_truncated = contracts_total > len(contracts)

    # 5. Likely forgotten updates. Three signals that commonly mean
    # "you should have updated X when you changed Y":
    #
    #   (a) Dangling refs: refs by name to a symbol whose def no longer
    #       exists in the target file (symbol was renamed/removed but
    #       consumers weren't updated).
    #
    #   (b) Orphan contract obligations: contracts declared but not
    #       consumed in the dependents (e.g. new flag with no consumer).
    #       Derived from the same engine that powers checklist.
    #
    #   (c) Callers whose file.parser == 'regex' and ref.confidence <
    #       'high' — those call sites may not have been re-checked after
    #       the rename.
    forgotten: List[Dict[str, Any]] = []
    if file_target and loc.get("kind") == "symbol" and loc.get("name"):
        name = loc["name"]
        sym_rows = list(store.conn.execute(
            "SELECT file, line FROM symbols WHERE name=? AND file=?",
            (name, file_target)))
        if not sym_rows:
            # Symbol name no longer defined in file → refs to it are
            # dangling now. Query refs pointing at the name in OTHER files.
            danglers = list(store.conn.execute(
                "SELECT file, line, kind FROM refs WHERE name=? "
                "AND file != ? LIMIT ?",
                (name, file_target, max_per_bucket)))
            for r in danglers:
                forgotten.append({
                    "kind":     "dangling-ref",
                    "file":     r["file"],
                    "line":     int(r["line"]),
                    "ref_kind": r["kind"],
                    "reason":   (f"{name!r} referenced here but no def in "
                                  f"{file_target} — symbol was removed?"),
                })
    # Regex-parser dependent files: their refs may be stale.
    for d in direct:
        if d.get("confidence") == "medium" or d.get("confidence") == "low":
            forgotten.append({
                "kind":       "low-confidence-consumer",
                "file":       d["file"],
                "confidence": d.get("confidence"),
                "reason":     "consumer file parsed via regex/medium "
                              "confidence — re-check this ref manually.",
            })
            if len(forgotten) >= max_per_bucket:
                break
    forgotten_total = len(forgotten)

    # F003: every bucket carries an explicit `*_total` so a CI gate or
    # downstream agent can see when the cap engaged. The `_count` keys
    # remain (length of the returned list) for backward compatibility,
    # but `_total` is the truth.
    coverage = {
        "direct_count":              len(direct),
        "direct_total":              direct_total,
        "indirect_count":            len(indirect),
        "indirect_total":            indirect_total,
        "indirect_truncated":        indirect_truncated,
        "tests_count":               len(tests),
        "tests_total":               tests_total,
        "tests_truncated":           tests_truncated,
        "contracts_count":           len(contracts),
        "contracts_total":           contracts_total,
        "contracts_truncated":       contracts_truncated,
        "likely_forgotten_count":    len(forgotten),
        "likely_forgotten_total":    forgotten_total,
        "artifact_consumers_count":  len(artifact_consumers),
        "artifact_consumers_total":  len(artifact_consumers),
        "max_per_bucket":            max_per_bucket,
        "note": ("Direct/indirect are SOURCE consumers; artifact paths "
                  "(build output, changelogs, baselines) filtered to a "
                  "separate bucket. Pass `--include-artifacts` to see "
                  "them. `direct_dependents` is the same set "
                  "`projmem reverse <target>` returns (source-only). "
                  "Each bucket carries `*_count` (returned slice) AND "
                  "`*_total` (true count); they differ when "
                  "`*_truncated` is true."),
    }
    # Next steps hint
    hints: List[str] = []
    if not file_target:
        hints.append(
            f"Target {target!r} didn't resolve to a file — use "
            "`projmem symbol <name>` or pass a file path.")
    if forgotten:
        hints.append(
            f"{len(forgotten)} likely-forgotten update(s) detected — "
            "inspect `likely_forgotten_updates` before shipping.")
    if indirect_truncated:
        hints.append(
            f"Indirect dependents truncated: showing {len(indirect)} "
            f"of {indirect_total}. Raise `--max-per-bucket` for the "
            "full list.")
    if contracts_truncated:
        hints.append(
            f"Contracts truncated: showing {len(contracts)} of "
            f"{contracts_total}. Raise `--max-per-bucket` for the "
            "full list.")
    if len(direct) >= max_per_bucket:
        hints.append(
            f"Direct dependents at the {max_per_bucket} mark — "
            f"true total is {direct_total}. Raise `--max-per-bucket` "
            "or narrow the target.")
    if len(indirect) == 0 and len(direct) == 0:
        hints.append(
            "No consumers found. Either the target is truly isolated OR "
            "the index scope is too narrow. Run `projmem session` to "
            "check ref_binding quality.")
    elif not forgotten:
        hints.append(
            "Pair with `projmem verify-completeness` before claiming the "
            "change is done.")

    out.update({
        "direct_dependents":        direct,
        "indirect_dependents":      indirect,
        "tests_affected":           tests,
        "contracts_affected":       contracts,
        "likely_forgotten_updates": forgotten,
        "artifact_consumers":       artifact_consumers[:max_per_bucket],
        "coverage":                 coverage,
        "next_steps_hint":          hints,
    })
    return out


def _is_test_file(path: str) -> bool:
    """Heuristic path-level test detection. Conservative."""
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
