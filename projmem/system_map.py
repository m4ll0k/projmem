"""projmem/system_map.py — auto-detected module / layer overview.

`projmem map` returns the "big picture" an agent needs to reason about
a codebase before diving into one file:

  - top-level modules (auto-detected from the source tree structure)
  - per-module: file count, symbol count, exported surface, top symbols
  - cross-module edges — imports that cross module boundaries
  - hub files (high fan-in or fan-out)
  - contract surface per module (env / flag / schema_field counts)

Heuristic, bounded, JSON-safe. Works without the user declaring layers;
offers a best-guess structure that's good enough to orient an agent.
"""
from __future__ import annotations
import os
from collections import defaultdict
from typing import Any, Dict, List, Set


def build_map(store, cfg,
              max_modules: int = 30,
              max_hubs: int = 10,
              max_top_symbols: int = 5) -> Dict[str, Any]:
    """Produce the system-map blob. See module docstring.

    Module detection heuristic: first-level directory under the repo's
    source root. When the repo has a clear "src/" layout, modules are
    the immediate children of src/. Otherwise they're the first path
    segment of each indexed file.
    """
    files = [r["path"] for r in store.all_files()]
    if not files:
        return {
            "schema_version": 1,
            "modules":        [],
            "hint":           "empty index; run `projmem init` first",
        }

    module_of = _module_detector(files)

    # Group files by module.
    files_by_mod: Dict[str, List[str]] = defaultdict(list)
    for f in files:
        m = module_of(f)
        files_by_mod[m].append(f)

    # Per-module stats.
    modules: List[Dict[str, Any]] = []
    for mod, mfiles in files_by_mod.items():
        stats = _module_stats(store, mod, mfiles, max_top_symbols)
        modules.append(stats)
    modules.sort(key=lambda m: (-m["symbol_count"], m["name"]))
    modules = modules[:max_modules]

    # Cross-module edges. An edge is "cross-module" when src and dst
    # modules differ AND neither is an external / virtual module like
    # `module:...` or `builtin:node:*`.
    cross_edges: Dict[tuple, Dict[str, Any]] = {}
    for row in store.conn.execute(
            "SELECT src, dst, type FROM edges WHERE type='imports'"):
        src, dst = row["src"], row["dst"]
        if not src or not dst:
            continue
        if dst.startswith(("module:", "builtin:", "binding:")):
            continue
        ms = module_of(src)
        md = module_of(dst)
        if ms == md:
            continue
        key = (ms, md)
        if key not in cross_edges:
            cross_edges[key] = {
                "from_module": ms, "to_module": md,
                "edge_count": 0, "examples": []}
        cross_edges[key]["edge_count"] += 1
        if len(cross_edges[key]["examples"]) < 3:
            cross_edges[key]["examples"].append(
                {"src": src, "dst": dst, "type": row["type"]})
    cross_edges_list = sorted(
        cross_edges.values(),
        key=lambda x: (-x["edge_count"], x["from_module"], x["to_module"]))

    # Hub files: highest fan-in (most consumers). Fan-out is less
    # interesting — every file imports something. Agents typically
    # want "which files are central load-bearers?".
    try:
        hub_rows = list(store.conn.execute(
            "SELECT dst, COUNT(*) AS n FROM edges WHERE type='imports' "
            "AND dst NOT LIKE 'module:%' AND dst NOT LIKE 'builtin:%' "
            "AND dst NOT LIKE 'binding:%' "
            "GROUP BY dst ORDER BY n DESC LIMIT ?",
            (max_hubs,)))
        hubs = [{"file": r["dst"], "incoming_edges": int(r["n"]),
                  "module": module_of(r["dst"])}
                 for r in hub_rows]
    except Exception:
        hubs = []

    # Bind rate — agent needs this context to know whether trace /
    # reverse answers are high-confidence or name-level approximate.
    try:
        from . import binding as _binding
        bind_summary = _binding.binding_summary(store)
    except Exception:
        bind_summary = {}

    total_files = sum(len(v) for v in files_by_mod.values())
    return {
        "schema_version":    1,
        "root":              cfg.root,
        "total_files":       total_files,
        "module_count":      len(files_by_mod),
        "modules":           modules,
        "cross_module_edges": cross_edges_list[:50],
        "hubs":              hubs,
        "ref_binding":       bind_summary,
        "detection_note": (
            "Modules auto-detected from the source-tree structure "
            "(first-level children of `src/` when present, else "
            "first path segment). Re-run with explicit include/exclude "
            "globs if the inferred layout is wrong."
        ),
    }


# ---------------------------------------------------------------------------
# Module detection
# ---------------------------------------------------------------------------

_SRC_ROOTS = ("src", "app", "lib", "packages", "internal", "pkg")


def _module_detector(files: List[str]):
    """Return a callable `file -> module_name`.

    Uses the most-common "source root" pattern present in the index.
    If `src/` is present for most files, modules = children of `src/`.
    Otherwise, modules = first path segment."""
    roots_hit: Dict[str, int] = defaultdict(int)
    for f in files:
        parts = f.split("/")
        if len(parts) >= 2 and parts[0] in _SRC_ROOTS:
            roots_hit[parts[0]] += 1
    # Pick the most-hit src root if at least half the files live under it.
    chosen_root = None
    total = len(files)
    if roots_hit:
        best = max(roots_hit.items(), key=lambda kv: kv[1])
        if best[1] >= max(1, total // 4):
            chosen_root = best[0]

    def _of(path: str) -> str:
        if not path:
            return "(root)"
        parts = path.split("/")
        if chosen_root and parts[0] == chosen_root and len(parts) >= 2:
            return f"{chosen_root}/{parts[1]}"
        return parts[0] if parts else "(root)"
    return _of


# ---------------------------------------------------------------------------
# Per-module stats
# ---------------------------------------------------------------------------

def _module_stats(store, module_name: str, files: List[str],
                   max_top_symbols: int) -> Dict[str, Any]:
    placeholders = ",".join("?" * len(files)) if files else "''"
    symbol_count = 0
    exported_count = 0
    top_symbols: List[Dict[str, Any]] = []
    env_count = flag_count = schema_count = 0
    try:
        if files:
            # Total symbols + exported.
            row = store.conn.execute(
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN exported=1 THEN 1 ELSE 0 END) AS exp "
                f"FROM symbols WHERE file IN ({placeholders})",
                files).fetchone()
            symbol_count = int(row["total"] or 0)
            exported_count = int(row["exp"] or 0)
            # Top exported symbols by incoming-ref count.
            top_rows = list(store.conn.execute(
                f"SELECT s.name, s.file, s.line, s.kind, "
                f"       (SELECT COUNT(*) FROM refs r "
                f"        WHERE r.name=s.name) AS refs "
                f"FROM symbols s "
                f"WHERE s.file IN ({placeholders}) AND s.exported=1 "
                f"ORDER BY refs DESC LIMIT ?",
                files + [max_top_symbols]))
            top_symbols = [{
                "name": r["name"], "file": r["file"],
                "line": int(r["line"]), "kind": r["kind"],
                "incoming_refs": int(r["refs"]),
            } for r in top_rows]
            # Contracts.
            contract_rows = list(store.conn.execute(
                f"SELECT kind, COUNT(*) AS n FROM contracts "
                f"WHERE file IN ({placeholders}) AND role != 'occurrence' "
                f"GROUP BY kind", files))
            for r in contract_rows:
                if r["kind"] == "env":
                    env_count = int(r["n"])
                elif r["kind"] == "flag":
                    flag_count = int(r["n"])
                elif r["kind"] == "schema_field":
                    schema_count = int(r["n"])
    except Exception:
        pass

    return {
        "name":           module_name,
        "file_count":     len(files),
        "symbol_count":   symbol_count,
        "exported_count": exported_count,
        "top_symbols":    top_symbols,
        "contracts": {
            "env":          env_count,
            "flag":         flag_count,
            "schema_field": schema_count,
        },
    }
