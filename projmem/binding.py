"""projmem/binding.py — post-index ref → symbol_id binding.

Motivation: the trace engine and reverse-dep analysis treat all refs with
the same bare name as referring to one "logical node". That's the cheap
approximation — correct when the name is unique, incorrect when it isn't.
The refs table carries a ``target_symbol_id`` column precisely for this,
but most refs are left NULL because resolving them requires global
information the indexer doesn't have per-file.

This pass runs AFTER all files are indexed and fills in ``target_symbol_id``
for refs we can uniquely resolve. Three tiers, in priority order:

  1. Same-file binding. A ref in file X with name N uniquely binds when X
     itself has a def of N. Trivially correct; handles the most common
     case (method calling another method in the same class).

  2. Imported binding. A ref in file X with name N uniquely binds when
     there's exactly one file Y where N is defined AND X has an import
     edge to Y. Covers most cross-file calls in import-disciplined code.

  3. Global-unique binding. A ref with name N uniquely binds when N has
     exactly one def across the entire index. Works for rare names; not
     safe for common names (``handler``, ``run``, ``process``).

Ambiguous refs stay NULL and are labeled "unbound" on read. Consumers
(cmd_symbol, trace_call_chain, reverse_deps) can decide whether to treat
an unbound ref as a candidate or as a separate bucket.

Never writes a WRONG binding — if resolution is ambiguous at any tier,
we move to the next. If no tier uniquely resolves, we leave it NULL.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple


def resolve_refs(store) -> Dict[str, int]:
    """Run the binding pass. Returns counts per tier.

    Idempotent: refs that already have a ``target_symbol_id`` are left
    alone. Safe to run multiple times after incremental reindexes.
    """
    counts = {
        "already_bound":      0,
        "bound_same_file":    0,
        "bound_imported":     0,
        "bound_reexport":     0,
        "bound_global_unique": 0,
        "unbound_ambiguous":  0,
        "unbound_no_match":   0,
    }

    # 1. Collect all (symbol_id, file, name) tuples for quick lookups.
    #    A symbol may have multiple entries if the indexer emitted
    #    alternative spellings — we dedupe on (file, name).
    defs_by_name: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    defs_by_file_name: Dict[Tuple[str, str], str] = {}
    for row in store.conn.execute(
            "SELECT file, name, symbol_id FROM symbols WHERE symbol_id IS NOT NULL"):
        key = (row["file"], row["name"])
        # Prefer the FIRST symbol_id per (file, name); duplicates are rare.
        defs_by_file_name.setdefault(key, row["symbol_id"])
        defs_by_name[row["name"]].append((row["file"], row["symbol_id"]))

    # 2. Build the import reachability map (src_file → set of dst_files).
    imports: Dict[str, Set[str]] = defaultdict(set)
    for row in store.conn.execute(
            "SELECT src, dst, type FROM edges WHERE type='imports'"):
        src = row["src"]
        dst = row["dst"]
        if src and dst:
            imports[src].add(dst)

    # 2b. Re-export map: barrel → set of files it `export * from`s.
    # Audit fix #11: extends the import-reachable set so that
    # `import { foo } from './_namespaces/ts'` (a barrel that re-exports
    # `foo` from `./scanner`) binds to scanner's symbol_id rather than
    # staying unbound. Bounded one hop — TypeScript namespace barrels
    # rarely chain deeper than 2-3 levels, so we precompute the
    # transitive closure with a small bfs.
    direct_reexports: Dict[str, Set[str]] = defaultdict(set)
    for row in store.conn.execute(
            "SELECT src, dst FROM edges WHERE type='reexport_star'"):
        if row["src"] and row["dst"]:
            direct_reexports[row["src"]].add(row["dst"])
    # Transitive closure (cap 4 hops to bound pathological self-referencing
    # namespace layouts; 4 covers all real-world cases I've seen).
    reexport_closure: Dict[str, Set[str]] = {}
    for barrel in direct_reexports:
        seen: Set[str] = set()
        frontier: Set[str] = set(direct_reexports[barrel])
        for _ in range(4):
            new_frontier: Set[str] = set()
            for f in frontier:
                if f in seen:
                    continue
                seen.add(f)
                new_frontier.update(direct_reexports.get(f, ()))
            frontier = new_frontier - seen
            if not frontier:
                break
        reexport_closure[barrel] = seen

    def _reexport_files_reachable_from(ref_file: str) -> Set[str]:
        """Return the set of files transitively reachable through
        re-export-star barrels from any direct import of ref_file."""
        out: Set[str] = set()
        for direct in imports.get(ref_file, ()):
            if direct in reexport_closure:
                out.update(reexport_closure[direct])
        return out

    # 3. Walk every ref and apply the binding tiers.
    ref_rows = list(store.conn.execute(
        "SELECT id, file, name, target_symbol_id FROM refs"))
    updates: List[Tuple[str, int]] = []

    for r in ref_rows:
        if r["target_symbol_id"]:
            counts["already_bound"] += 1
            continue
        name = r["name"]
        ref_file = r["file"]
        if not name:
            counts["unbound_no_match"] += 1
            continue

        # Tier 1: same-file
        sid = defs_by_file_name.get((ref_file, name))
        if sid:
            updates.append((sid, r["id"]))
            counts["bound_same_file"] += 1
            continue

        # Tier 2: imported file has a unique def of the name
        importable_defs = [
            (df, dsid) for (df, dsid) in defs_by_name.get(name, [])
            if df in imports.get(ref_file, set())
        ]
        if len(importable_defs) == 1:
            updates.append((importable_defs[0][1], r["id"]))
            counts["bound_imported"] += 1
            continue

        # Tier 2.5: re-export barrel chain. If a name is defined in a file
        # transitively reachable through an `export * from` chain rooted
        # in one of ref_file's direct imports, it's a valid binding.
        # Audit fix #11.
        reexport_reachable = _reexport_files_reachable_from(ref_file)
        if reexport_reachable:
            reexport_defs = [
                (df, dsid) for (df, dsid) in defs_by_name.get(name, [])
                if df in reexport_reachable
            ]
            if len(reexport_defs) == 1:
                updates.append((reexport_defs[0][1], r["id"]))
                counts["bound_reexport"] += 1
                continue

        # Tier 3: name is globally unique
        all_defs = defs_by_name.get(name, [])
        if len(all_defs) == 1:
            updates.append((all_defs[0][1], r["id"]))
            counts["bound_global_unique"] += 1
            continue

        # Unbound. Record whether we had candidates at all so stats reflect
        # the *reason* a ref stayed unbound.
        if all_defs:
            counts["unbound_ambiguous"] += 1
        else:
            counts["unbound_no_match"] += 1

    # 4. Batch-write the resolved bindings.
    if updates:
        store.conn.executemany(
            "UPDATE refs SET target_symbol_id=? WHERE id=?", updates)
        store.conn.commit()

    return counts


def binding_summary(store) -> Dict[str, Any]:
    """Read-only rollup used by `projmem stats` / `projmem doctor` to
    surface how well-bound the ref graph is.

    Two percentages are reported:
      * `bound_pct` — bound / total refs. Headline number, but conflates
        external refs (React hooks, stdlib calls, framework symbols) with
        actual binding failures, so it tends to look bad on framework-
        heavy projects.
      * `internal_bound_pct` — bound / refs whose name has at least one
        def in the index. This is the *real* binding-quality signal:
        100% means every ref to a symbol projmem CAN see is bound; less
        than that means trace / reverse-by-symbol are degraded.

    `internal_bindable` is the denominator for `internal_bound_pct` —
    refs to external/framework names are excluded, so the metric reflects
    only refs projmem could in principle resolve.
    """
    total = store.conn.execute(
        "SELECT COUNT(*) AS n FROM refs").fetchone()["n"]
    bound = store.conn.execute(
        "SELECT COUNT(*) AS n FROM refs "
        "WHERE target_symbol_id IS NOT NULL").fetchone()["n"]
    # Refs whose name appears in the symbols table — i.e., the ref COULD
    # in principle bind. The rest are external (framework / stdlib /
    # method calls on typed objects) and aren't fixable by the binding
    # pass; reporting them as "binding failures" is misleading.
    internal_bindable = store.conn.execute(
        "SELECT COUNT(*) AS n FROM refs r "
        "WHERE EXISTS (SELECT 1 FROM symbols s WHERE s.name = r.name)"
    ).fetchone()["n"]
    external_refs = int(total - internal_bindable)
    internal_pct = (round(100.0 * bound / internal_bindable, 1)
                    if internal_bindable else 0.0)
    return {
        "total_refs":         int(total),
        "bound_refs":         int(bound),
        "unbound_refs":       int(total - bound),
        "bound_pct":          round(100.0 * bound / total, 1) if total else 0.0,
        "internal_bindable":  int(internal_bindable),
        "external_refs":      external_refs,
        "internal_bound_pct": internal_pct,
    }
