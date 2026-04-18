"""Graph queries built on the store. Reverse deps + neighbor expansion."""
from __future__ import annotations
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .store import Store


# Defaults for bounded-cost graph queries. A previous benchmark against
# Node.js core hung indefinitely on common symbols (`execve`) because BFS
# traversal over same-name refs exploded the frontier. These caps
# transform a hang into a structured partial result the caller can act
# on (narrow with --file, raise the cap, etc.).
DEFAULT_TRACE_DEADLINE_S:     float = 30.0
DEFAULT_TRACE_PER_NAME_CAP:   int   = 25
DEFAULT_TRACE_EXPLORED_CAP:   int   = 20_000
DEFAULT_SYMBOL_REFS_CAP:      int   = 10_000


def reverse_deps(store: Store, target: str) -> List[dict]:
    """Who depends on `target`? Follows direct `imports` edges and walks
    `reexport_star` barrel chains transitively.

    Transitive barrel example (TypeScript): if `src/compiler/_namespaces/
    ts.ts` does `export * from "../scanner.js"` and `src/compiler/checker.ts`
    imports from `./_namespaces/ts.js`, then asking for reverse deps of
    scanner.ts should list checker.ts with `type=reexport_via` and the
    barrel file in the evidence. Without this, a delete-safety query
    returns "safe" on files that half the compiler transitively consumes.

    Java twist: imports go to a fully-qualified module form
    (`module:org.apache.catalina.connector.Connector`) rather than the
    file path. When `target` ends in `.java`, derive the FQ name from
    the path and ALSO query edges with that module: prefix so reverse-
    deps work on Java codebases (Tomcat audit surfaced this).
    """
    out: List[dict] = []
    seen: set = set()

    def _emit(src_file: str, type_: str, confidence: str, evidence: str):
        key = (src_file, type_)
        if key in seen:
            return
        seen.add(key)
        out.append({"file": src_file, "type": type_,
                    "confidence": confidence, "evidence": evidence})

    # Direct imports.
    for row in store.edges_to(target, type_="imports"):
        _emit(row["src"], "imports", row["confidence"], row["evidence"])
    # Java module form: `java/org/apache/.../Connector.java` →
    # `module:org.apache....Connector`. Try both common roots.
    if target.endswith(".java"):
        rel = target.replace("\\", "/")
        # Strip leading source-root segments (java/, src/main/java/, src/).
        for prefix in ("java/", "src/main/java/", "src/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        java_class = rel[:-5].replace("/", ".")  # drop .java + dot-form
        if java_class:
            fq_dst = f"module:{java_class}"
            for row in store.edges_to(fq_dst, type_="imports"):
                _emit(row["src"], "imports",
                      row["confidence"],
                      row["evidence"] or f"java import → {java_class}")
        # Same-package usage: Java classes in the same package don't
        # need imports, so the import-edge query above misses them.
        # Bridge by walking ref rows for every class defined in the
        # target file (`ref.new`/`ref.name` captures from the Java
        # query, including constructor sites and type-position refs).
        # Tomcat audit: `reverse CoyoteAdapter.java` returned 0
        # because Connector instantiates it via `new CoyoteAdapter(...)`
        # in the same package — no import, no edge. Now reported as
        # `same_package_ref` so the agent sees "X uses Y by class
        # name, not import."
        try:
            for sym in store.conn.execute(
                    "SELECT name, kind FROM symbols WHERE file=? AND "
                    "kind IN ('class','interface','enum')",
                    (target,)):
                cname = sym["name"]
                for r in store.conn.execute(
                        "SELECT file, line, kind FROM refs "
                        "WHERE name=? AND file != ? "
                        "  AND kind IN ('new','name','call') LIMIT 200",
                        (cname, target)):
                    _emit(r["file"], "same_package_ref", "medium",
                          f"{r['kind']}-ref to {cname} at line {r['line']}")
        except Exception:
            pass
    # Pair-inspect reverse.
    for row in store.edges_to(target, type_="pair_inspect"):
        _emit(row["src"], "pair_inspect", row["confidence"], row["evidence"])

    # Transitive barrel re-exports. If B does `export * from A`, then every
    # consumer of B transitively depends on A. Walk up to 8 hops to bound
    # pathological cycles in self-referential namespace layouts.
    barrel_frontier: List[Tuple[str, str]] = []
    for row in store.edges_to(target, type_="reexport_star"):
        barrel_frontier.append((row["src"], row["src"]))
    visited_barrels: set = set()
    hops = 0
    while barrel_frontier and hops < 8:
        next_frontier: List[Tuple[str, str]] = []
        for (barrel, origin) in barrel_frontier:
            if barrel in visited_barrels:
                continue
            visited_barrels.add(barrel)
            # Consumers of this barrel — anyone who imports it.
            for imp in store.edges_to(barrel, type_="imports"):
                _emit(imp["src"], "reexport_via",
                      imp["confidence"],
                      f"imports {barrel} which re-exports from {target}")
            # Barrels that re-export THIS barrel — follow the chain.
            for up in store.edges_to(barrel, type_="reexport_star"):
                next_frontier.append((up["src"], origin))
        barrel_frontier = next_frontier
        hops += 1
    return out


def forward_deps(store: Store, target: str) -> List[dict]:
    out = []
    for row in store.edges_from(target, type_="imports"):
        out.append({"file": row["dst"], "type": "imports",
                    "confidence": row["confidence"], "evidence": row["evidence"]})
    for row in store.edges_from(target, type_="pair_inspect"):
        out.append({"file": row["dst"], "type": "pair_inspect",
                    "confidence": row["confidence"], "evidence": row["evidence"]})
    return out


def symbol_refs(store: Store, name: str,
                file: str | None = None,
                max_refs: int = DEFAULT_SYMBOL_REFS_CAP,
                ) -> Dict[str, object]:
    """Return {'defs': [...], 'refs': [...], ...} for a symbol name.

    `name` can be a raw symbol name, a `file#symbol` disambiguation, or a
    full SCIP-shaped symbol_id. If `file` is supplied (or extractable from
    `name`), the def list is narrowed to that file — BUT the ref list is
    deliberately NOT narrowed, because cross-file references to the symbol
    are exactly what the caller is asking for.

    Ref collection is capped at `max_refs` to keep CLI latency bounded on
    hot identifiers (e.g. `execve`, `exit`, `printf` in Node core). When
    truncated, the result carries ``refs_truncated=True`` and
    ``ref_count_total`` so the agent can narrow with --file or raise the
    cap instead of timing out silently.
    """
    from . import symbol_id as _sid
    # Full SCIP-shaped symbol_id → O(1) def lookup.
    if _sid.is_symbol_id(name):
        row = store.symbol_by_id(name)
        parsed = _sid.parse(name)
        bare = parsed.get("name") or name
        defs = [dict(row)] if row else []
        total = store.conn.execute(
            "SELECT COUNT(*) AS n FROM refs WHERE target_symbol_id=?",
            (name,)).fetchone()["n"]
        if total > 0:
            tsid_rows = list(store.conn.execute(
                "SELECT * FROM refs WHERE target_symbol_id=? LIMIT ?",
                (name, max_refs)))
            refs = [dict(r) for r in tsid_rows]
        else:
            total = store.conn.execute(
                "SELECT COUNT(*) AS n FROM refs WHERE name=?",
                (bare,)).fetchone()["n"]
            rows = list(store.conn.execute(
                "SELECT * FROM refs WHERE name=? LIMIT ?",
                (bare, max_refs)))
            refs = [dict(r) for r in rows]
        out: Dict[str, object] = {"defs": defs, "refs": refs}
        if total > max_refs:
            out["refs_truncated"]  = True
            out["ref_count_total"] = int(total)
            out["max_refs"]        = int(max_refs)
        return out
    # `file#symbol` disambiguation.
    if "#" in name:
        fp, sym = name.rsplit("#", 1)
        file = file or fp
        name = sym
    defs = [dict(r) for r in store.symbols_by_name(name)]
    if file:
        defs = [d for d in defs if d["file"] == file]
    total = store.conn.execute(
        "SELECT COUNT(*) AS n FROM refs WHERE name=?", (name,)).fetchone()["n"]
    rows = list(store.conn.execute(
        "SELECT * FROM refs WHERE name=? LIMIT ?", (name, max_refs)))
    refs = [dict(r) for r in rows]
    out = {"defs": defs, "refs": refs}
    if total > max_refs:
        out["refs_truncated"]  = True
        out["ref_count_total"] = int(total)
        out["max_refs"]        = int(max_refs)
        out["hint"] = ("Ref set truncated. Narrow the query with "
                       "`--file PATH` or the canonical symbol_id, or "
                       "raise the cap.")
    return out


def trace_call_chain(store: Store, source: str, sink: str,
                     max_hops: int = 5,
                     via: str | None = None,
                     mode: str = "strict",
                     deadline_seconds: float = DEFAULT_TRACE_DEADLINE_S,
                     per_name_cap: int = DEFAULT_TRACE_PER_NAME_CAP,
                     explored_cap: int = DEFAULT_TRACE_EXPLORED_CAP,
                     ) -> Dict[str, object]:
    """BFS over symbol refs to find a call chain from `source` symbol to
    `sink` symbol.

    Modes:
      strict   — only traverse refs with kind='call' (default). Import,
                 read, callback, and new edges are NOT crossed. This
                 prevents the class of hallucination where BFS reaches
                 the sink through a logger import or a type reference
                 that no runtime call could actually take.
      relaxed  — traverse call + new + callback edges (still no imports
                 or plain reads). Useful when the call graph is sparse
                 (e.g. heavy use of function references stored in a
                 registry) and you want to surface plausible paths even
                 if not runtime-guaranteed.

    Both endpoints accept the same forms as `pack`:
        bare name           — `parserOnIncoming`
        file#name           — `lib/_http_server.js#parserOnIncoming`
        full SCIP id        — `lib/_http_server.js#parserOnIncoming.`

    Returns a dict with keys:
        path        — list of `{symbol, file, line, kind, edge_type}` hops.
                      `edge_type` is the ref kind that carried each
                      hop (always 'call' in strict mode; 'call'/'new'/
                      'callback' in relaxed). The first hop has edge_type=None.
        hops        — len(path) - 1, or None if no path
        max_hops    — the cap that was applied
        mode        — 'strict' | 'relaxed'
        explored    — number of unique symbols expanded
        dead_end_at — when no path: the deepest hop reached
        via_filter  — the file filter applied (if any)
        confidence  — 'high' (refs are tree-sitter-grounded) or
                      'medium' (regex parser involvement)

    Algorithm: BFS from source's def site. At each frontier symbol,
    expand to every distinct symbol whose def file contains a *call-kind*
    ref to this symbol's name. This is an OVERAPPROXIMATION (same-name
    collisions inflate edges) — for tighter results, callers should
    pre-disambiguate with `file#name` form so the def lookup narrows.
    """
    # Strict mode: only 'call' refs. Relaxed: allow 'new' and 'callback'
    # because those *can* be invoked at runtime, but never imports/reads.
    if mode == "relaxed":
        allowed_kinds: tuple = ("call", "new", "callback")
    else:
        allowed_kinds = ("call",)
    def _resolve(t: str) -> dict | None:
        defs = symbol_refs(store, t).get("defs", [])
        return defs[0] if defs else None
    def _heuristic_body_end(_store, _file: str, _line: int) -> int:
        """When `end_line` is missing on a regex-parsed symbol, fall back
        to the line just before the next-defined symbol in the same file
        — gives BFS a body range to scan even without tree-sitter."""
        row = _store.conn.execute(
            "SELECT line FROM symbols WHERE file=? AND line > ? "
            "ORDER BY line ASC LIMIT 1",
            (_file, _line)).fetchone()
        if row:
            return int(row["line"]) - 1
        # Last symbol in the file — no clean upper bound; return a
        # conservative window of 200 lines below.
        return _line + 200

    src_def = _resolve(source)
    sink_def = _resolve(sink)
    if src_def is None or sink_def is None:
        return {"path": [], "hops": None, "max_hops": max_hops,
                "explored": 0, "dead_end_at": 0,
                "via_filter": via, "confidence": "unknown",
                "error": (f"unresolvable: source={src_def is not None}, "
                          f"sink={sink_def is not None}")}

    # Audit fix: BFS direction is `source → sink` in the user's mental
    # model — i.e. SOURCE calls (transitively) SINK. The previous
    # implementation walked callee→caller, so `trace A B` actually asked
    # "is there a chain of CALLERS from A that reaches B?" — exactly
    # backward. That made obvious 1-hop intra-file calls look like
    # "no path found".
    #
    # New algorithm: at each frontier node, look at refs INSIDE the
    # symbol's BODY (sym.line..sym.end_line). Each ref is a call (or
    # new / callback in relaxed mode) FROM this symbol to a callee.
    # Resolve the callee name to its def(s); each becomes a next-
    # frontier node. Continue until SINK is reached.
    sink_key = (sink_def["file"], sink_def["name"])
    parent: Dict[Tuple[str, str], Tuple[Tuple[str, str], str] | None] = {}
    parent[(src_def["file"], src_def["name"])] = None
    frontier: List[Tuple[str, str, int, int, int]] = [(
        src_def["file"], src_def["name"],
        int(src_def.get("line") or 0),
        int(src_def.get("end_line") or 0),
        0,
    )]
    explored = 0
    dead_end_at = 0
    confidence = "high"
    # Preload parser-by-file once instead of one SQL per callee expansion.
    # Common-name expansions can visit thousands of callees; per-row SQL
    # is where the hang actually lives.
    parser_by_file: Dict[str, str] = {
        r["path"]: (r["parser"] or "")
        for r in store.conn.execute("SELECT path, parser FROM files")}
    too_ambiguous: List[str] = []
    deadline = time.monotonic() + max(1.0, float(deadline_seconds))

    def _timeout_return(reason: str) -> Dict[str, object]:
        return {"path": [], "hops": None, "max_hops": max_hops,
                "mode": mode, "explored": explored,
                "dead_end_at": dead_end_at, "via_filter": via,
                "confidence": confidence,
                "allowed_edges": list(allowed_kinds),
                "timeout": True, "timeout_reason": reason,
                "too_ambiguous_names": too_ambiguous[:25],
                "hint": ("Trace aborted before completion. Disambiguate "
                         "with `file#symbol` form on endpoints, narrow "
                         "via `--via PATH`, or raise caps with "
                         "`--deadline` / `--per-name-cap`.")}

    while frontier:
        if time.monotonic() > deadline:
            return _timeout_return("wall_clock_deadline")
        if explored > explored_cap:
            return _timeout_return("explored_cap")
        next_frontier: List[Tuple[str, str, int, int, int]] = []
        for (file, name, line, end_line, depth) in frontier:
            explored += 1
            dead_end_at = max(dead_end_at, depth)
            if (file, name) == sink_key:
                # Reconstruct path
                hops: List[dict] = []
                cur: Tuple[str, str] | None = (file, name)
                while cur is not None:
                    f, n = cur
                    rec = next((dict(r) for r in store.symbols_by_name(n)
                                if r["file"] == f), {"file": f, "name": n})
                    parent_entry = parent[cur]
                    edge_type = parent_entry[1] if parent_entry else None
                    hops.append({
                        "symbol": rec.get("name"),
                        "file": rec.get("file"),
                        "line": rec.get("line"),
                        "kind": rec.get("kind"),
                        "edge_type": edge_type,
                    })
                    cur = parent_entry[0] if parent_entry else None
                hops.reverse()
                return {"path": hops, "hops": len(hops) - 1,
                        "max_hops": max_hops, "mode": mode,
                        "explored": explored,
                        "dead_end_at": dead_end_at,
                        "via_filter": via, "confidence": confidence,
                        "allowed_edges": list(allowed_kinds),
                        "direction": "caller_to_callee",
                        "too_ambiguous_names": too_ambiguous[:25]}
            if depth >= max_hops:
                continue
            # Expand: refs WITHIN this symbol's body. Each ref is a
            # call site FROM this symbol TO a callee.
            #
            # Body range = [line, end_line]. When end_line is missing
            # (regex-parsed file), fall back to "next def in same file"
            # as a body boundary heuristic. When even that fails we
            # can't traverse, but other frontier entries may still find
            # the sink.
            # body_end may equal `line` for single-line functions like
            # `function f() { g(); }`; that's still a valid 1-line body
            # we want to scan. The previous `<= line` check skipped
            # those entirely.
            body_end = end_line if end_line >= line else 0
            if body_end < line:
                body_end = _heuristic_body_end(store, file, line)
            if body_end < line:
                continue
            ph = ",".join("?" * len(allowed_kinds))
            ref_rows = list(store.conn.execute(
                f"SELECT name, line, kind FROM refs "
                f"WHERE file=? AND line >= ? AND line <= ? "
                f"AND kind IN ({ph})",
                (file, line, body_end, *allowed_kinds)))
            for r in ref_rows:
                callee_name = r["name"]
                ref_kind = r["kind"]
                if not callee_name or callee_name == name:
                    continue
                # Resolve callee name → def(s). Same-name overapproximation
                # inflates the frontier for common identifiers (e.g.
                # `execve`, `close`, `printf`); cap expansion per name
                # so one hot identifier doesn't starve the whole trace.
                # Caller sees `too_ambiguous_names` listing which names
                # were skipped.
                syms = store.symbols_by_name(callee_name)
                if len(syms) > per_name_cap:
                    if callee_name not in too_ambiguous:
                        too_ambiguous.append(callee_name)
                    continue
                for sym in syms:
                    callee_file = sym["file"]
                    if via and via not in callee_file:
                        continue
                    # parser-confidence downgrade if the callee's def
                    # file was regex-parsed (cached lookup — per-call SQL
                    # here was the trace hang on large repos).
                    if parser_by_file.get(callee_file, "").startswith("regex"):
                        confidence = "medium"
                    callee_key = (callee_file, callee_name)
                    if callee_key in parent:
                        continue
                    parent[callee_key] = ((file, name), ref_kind)
                    next_frontier.append((
                        callee_file, callee_name,
                        int(sym["line"] or 0),
                        int((sym["end_line"] if "end_line" in sym.keys()
                             else 0) or 0),
                        depth + 1,
                    ))
        frontier = next_frontier

    return {"path": [], "hops": None, "max_hops": max_hops, "mode": mode,
            "explored": explored, "dead_end_at": dead_end_at,
            "via_filter": via, "confidence": confidence,
            "allowed_edges": list(allowed_kinds),
            "too_ambiguous_names": too_ambiguous[:25],
            "note": (f"No path from {source!r} to {sink!r} within "
                     f"{max_hops} hops via edges {list(allowed_kinds)}. "
                     f"Explored {explored} unique callers; try --mode relaxed "
                     "for call+new+callback edges, or raise --max-hops.")}


def contracts_touching_file(store: Store, file: str) -> List[dict]:
    return [dict(r) for r in store.contracts_in_file(file)]


def files_sharing_contract(store: Store, name: str, kind: str | None = None) -> List[dict]:
    return [dict(r) for r in store.contracts_by_name(name, kind)]
