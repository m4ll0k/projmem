"""projmem/flow.py — contract flow tracing.

"What does this env var actually do?" is a one-call question that grep
answers badly. grep gets you the read site; you then manually chase the
local variable, the switch-case, and the consumer function. This module
builds that chain.

A **flow** for a contract `N` is:

    env-read  →  local-assign  →  local-read  →  switch-case  →  consumer

Not every contract has every hop. For e.g. a plain `if (process.env.X) {...}`
there's no local-assign; we just report the read site's enclosing symbol
as the direct consumer. For a flag routed through a dispatch table there
may be multiple switch-case hops.

## Scope of v1

Single-file flow. We follow the contract's read site to the local it
assigns into (if any), then to every in-file ref of that local, tagging
switch/case patterns along the way. Cross-file flow (parameter passing,
re-export) is deferred — that belongs in a v2 once the single-file layer
is known to be correct.

## Structure

    trace_flow(store, name, kind=None) -> {
      "subject":        str,
      "kind":           "env" | "flag" | ...,
      "read_sites":     [ContractSite, ...],
      "local_aliases":  [{file, line, local_name, contract_line}, ...],
      "consumers":      [{file, line, symbol, via, snippet}, ...],
      "flow_graph":     [{hop: int, kind: str, at: "file:line", detail: {...}}],
      "coverage":       {counts + notes}
    }
"""
from __future__ import annotations
import os
import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Local-alias detectors
# ---------------------------------------------------------------------------
#
# These regexes find "the local that was assigned FROM the contract read" on
# a single source line. They're deliberately conservative — false positives
# are worse than false negatives in a flow trace (the user relies on it).

# JS/TS: `const foo = process.env.NAME` / `let foo = process.env.NAME`
#        `var foo = process.env.NAME`    / `this.foo = process.env.NAME`
_JS_LOCAL_ASSIGN_FROM_ENV = re.compile(
    r"""
    (?:(?:const|let|var)\s+)?            # optional declarator
    (?:this\.)?                           # optional `this.` prefix
    ([A-Za-z_$][\w$]*)                    # capture: local name
    \s*=\s*
    process\.env\.([A-Z_][A-Z0-9_]*)      # capture: env name
    """,
    re.VERBOSE,
)

# JS/TS object property: `foo: process.env.NAME` (inside an object literal).
# Commonly used for config objects — e.g. TypeScript's sys.ts defines
# `tscWatchFile: process.env.TSC_WATCHFILE,` inside a literal returned by
# `getNodeSystem()`. The "local name" in this case is the property key;
# downstream consumers don't read a variable but do read `obj.foo`.
_JS_OBJECT_PROP_FROM_ENV = re.compile(
    r"""
    ^\s*                                  # indent
    ([A-Za-z_$][\w$]*)                    # property name
    \s*:\s*
    (?:!{0,2}|Boolean\(\s*)?              # optional coercion
    process\.env\.([A-Z_][A-Z0-9_]*)      # env name
    """,
    re.VERBOSE,
)

# Python: `foo = os.environ.get("NAME")` / `foo = os.environ["NAME"]`
_PY_LOCAL_ASSIGN_FROM_ENV = re.compile(
    r"""
    (?:self\.)?
    ([A-Za-z_][\w]*)
    \s*=\s*
    os\.environ(?:\.get\(\s*['"]([A-Z_][A-Z0-9_]*)['"]|\s*\[\s*['"]([A-Z_][A-Z0-9_]*)['"]\])
    """,
    re.VERBOSE,
)

# Flag read (JS/TS): `const foo = args["--flag"]` or `const foo = argv.flag`
_JS_LOCAL_ASSIGN_FROM_FLAG = re.compile(
    r"""
    (?:(?:const|let|var)\s+)?
    ([A-Za-z_$][\w$]*)
    \s*=\s*
    (?:args|argv|opts|options|parsed)
    \.?([A-Za-z_$][\w$]*)?
    """,
    re.VERBOSE,
)


def _detect_local_alias(src_line: str, contract_name: str,
                        language_hint: str = "auto"
                        ) -> Optional[Tuple[str, str, str]]:
    """Return (local_name, detected_contract_name, shape) if `src_line`
    contains a local assignment sourced from a known contract read.

    `shape` is one of:
      - "local_var"      — `const x = process.env.NAME` / `this.x = ...`
      - "object_property" — `x: process.env.NAME` inside an object literal
      - "python_env"     — `x = os.environ.get('NAME')` / `os.environ['NAME']`

    Shape matters because it determines the scope the consumer scan should
    use: local_var / python_env stays inside the enclosing symbol (tight),
    while object_property must search the whole file (the property leaks
    out of the function through the returned object literal).
    """
    shape_by_rx = [
        (_JS_LOCAL_ASSIGN_FROM_ENV,  "local_var"),
        (_JS_OBJECT_PROP_FROM_ENV,   "object_property"),
        (_PY_LOCAL_ASSIGN_FROM_ENV,  "python_env"),
    ]
    for rx, shape in shape_by_rx:
        m = rx.search(src_line)
        if not m:
            continue
        groups = m.groups()
        local = groups[0]
        env_name = next((g for g in groups[1:] if g), None)
        if not env_name:
            continue
        if env_name == contract_name:
            return local, env_name, shape
    return None


# ---------------------------------------------------------------------------
# Switch-case detector
# ---------------------------------------------------------------------------

_SWITCH_LINE_RX = re.compile(
    r"\bswitch\s*\(\s*(?:this\.)?([A-Za-z_$][\w$]*)\s*\)",
)
_CASE_LINE_RX = re.compile(
    r"^\s*case\s+(['\"])(?P<val>.+?)\1\s*:",
)


def _classify_consumer_line(src_line: str,
                             local_name: Optional[str]) -> str:
    """Tag a single consumer-site line.

    Returns one of:
      - "switch-case"  — line is `case "X":` inside a switch on our local
      - "switch-head"  — line is `switch(local)`
      - "conditional"  — line mentions the local in an `if` / ternary
      - "read"         — plain read, no obvious classification
    """
    s = src_line.strip()
    if _CASE_LINE_RX.match(src_line):
        return "switch-case"
    if local_name and _SWITCH_LINE_RX.search(src_line):
        # Only tag switch-head when the switch is on OUR local.
        m = _SWITCH_LINE_RX.search(src_line)
        if m and m.group(1) == local_name:
            return "switch-head"
    if s.startswith(("if ", "if(", "else if")) or "? " in s:
        return "conditional"
    return "read"


# ---------------------------------------------------------------------------
# Enclosing symbol lookup (shared helper)
# ---------------------------------------------------------------------------

def _enclosing_symbol(store, file: str, line: int) -> Optional[Dict[str, Any]]:
    """Nearest preceding symbol def in the same file. Same heuristic the
    trace engine uses — an approximation, but cheap."""
    row = store.conn.execute(
        "SELECT name, kind, line FROM symbols "
        "WHERE file=? AND line<=? ORDER BY line DESC LIMIT 1",
        (file, int(line))).fetchone()
    if not row:
        return None
    return {"name": row["name"], "kind": row["kind"],
            "line": int(row["line"])}


# ---------------------------------------------------------------------------
# Source line reader
# ---------------------------------------------------------------------------

def _read_line(repo_root: str, rel_path: str, line: int) -> Optional[str]:
    """Read a single 1-indexed line from disk. None on error."""
    full = os.path.join(repo_root, rel_path) if repo_root else rel_path
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            for i, text in enumerate(f, 1):
                if i == line:
                    return text.rstrip("\n")
                if i > line:
                    break
    except OSError:
        return None
    return None


def _read_lines(repo_root: str, rel_path: str) -> List[str]:
    """Read the whole file as a list of lines (1-indexed semantics when
    callers do `lines[i - 1]`). Returns empty list on error."""
    full = os.path.join(repo_root, rel_path) if repo_root else rel_path
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


_COMMENT_ONLY_LINE_RX = re.compile(
    r"""
    ^\s*
    (?:
        //.*           # JS/TS line comment
      | \#.*           # Python / shell comment
      | /\*.*\*/\s*$   # JS/C single-line block comment
      | \*\s.*         # mid-block-comment continuation (` * foo`)
      | \*/\s*         # block-comment closer
      | /\*.*$         # block-comment opener (skip; whole block ambiguous)
    )
    \s*$
    """,
    re.VERBOSE,
)


def _is_comment_only_line(text: str) -> bool:
    """Cheap line-level heuristic: is this line ENTIRELY a comment?

    Used to filter false-positive flow consumers — when the local name
    appears only inside a comment, that's not an actual consumer. We
    don't try to parse comments inside expressions (e.g. `foo; // local`
    still counts as a real use of `foo`)."""
    return bool(_COMMENT_ONLY_LINE_RX.match(text))


def _scan_local_consumers(repo_root: str, rel_path: str, local_name: str,
                           start_line: int,
                           end_line: Optional[int] = None,
                           max_hits: int = 50
                           ) -> List[Tuple[int, str]]:
    """Scan source lines [start_line+1, end_line] for word-boundary matches
    of `local_name`. Returns list of (line_no, line_text) tuples.

    This exists because the refs table doesn't capture local-variable reads
    in JS/TS — tree-sitter queries focus on calls/new/import/binding, not
    every identifier occurrence. For flow tracing we need every consumer
    of a local, which means a targeted source-level text scan bounded to
    the enclosing symbol's body.

    Tightening (limit #5 fix): comment-only lines are filtered to reduce
    false positives in object_property whole-file scope. A name appearing
    only in a comment isn't a real consumer.
    """
    lines = _read_lines(repo_root, rel_path)
    if not lines:
        return []
    if end_line is None or end_line <= 0 or end_line > len(lines):
        # Heuristic: stop at dedent-to-column-0 or file end.
        end_line = len(lines)

    pattern = re.compile(r"\b" + re.escape(local_name) + r"\b")
    out: List[Tuple[int, str]] = []
    for i in range(start_line, min(end_line, len(lines))):
        # `i` is 0-indexed; the line number we report is i+1.
        if i + 1 <= start_line:
            # Skip the declaring line itself — caller already has it.
            continue
        text = lines[i].rstrip("\n")
        if not pattern.search(text):
            continue
        if _is_comment_only_line(text):
            continue
        out.append((i + 1, text))
        if len(out) >= max_hits:
            break
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _scan_cross_file_consumers(store, repo_root: str, leaf_file: str,
                                local_name: str,
                                max_consumers_per_file: int = 20,
                                max_files: int = 30
                                ) -> List[Dict[str, Any]]:
    """Find consumers of `local_name` in OTHER files that import `leaf_file`.

    Used by trace_flow when the alias shape is `object_property` — the
    captured value is attached to a returned object, so consumers live in
    files that import the leaf module and then read the property
    (`obj.local_name` or destructured `{ local_name }`).

    Bounded:
      - max_files: only the first N importer files are scanned
      - max_consumers_per_file: per-file hit cap

    Returns a flat list of consumer dicts. Each carries
    ``via="cross-file-read"`` (or "cross-file-destructure" when the line
    looks like `{ local_name }` syntax) and an ``importer`` field naming
    the file we found the hit in.
    """
    importers = list(store.conn.execute(
        "SELECT DISTINCT src FROM edges "
        "WHERE dst=? AND type='imports' LIMIT ?",
        (leaf_file, max_files)))
    out: List[Dict[str, Any]] = []
    if not importers:
        return out

    pattern = re.compile(r"\b" + re.escape(local_name) + r"\b")
    destructure_rx = re.compile(
        r"\{\s*[^}]*\b" + re.escape(local_name) + r"\b[^}]*\}")
    for row in importers:
        importer_file = row["src"]
        if importer_file == leaf_file:
            continue
        full = os.path.join(repo_root, importer_file) if repo_root else importer_file
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        hits = 0
        for i, text in enumerate(lines, start=1):
            if not pattern.search(text):
                continue
            via = ("cross-file-destructure" if destructure_rx.search(text)
                   else "cross-file-read")
            cons_symbol = _enclosing_symbol(store, importer_file, i)
            out.append({
                "file":     importer_file,
                "line":     i,
                "via":      via,
                "ref_kind": "text-scan",
                "symbol":   (cons_symbol or {}).get("name"),
                "snippet":  text.rstrip("\n").strip()[:200],
                "importer": importer_file,
                "leaf":     leaf_file,
                "local_name": local_name,
            })
            hits += 1
            if hits >= max_consumers_per_file:
                break
    return out


def trace_flow(store, repo_root: str, name: str,
               kind: Optional[str] = None,
               max_consumers_per_read: int = 50,
               cross_file: bool = True
               ) -> Dict[str, Any]:
    """Trace the usage chain for a contract named `name`.

    If `kind` is provided (``env`` / ``flag`` / ``schema_field`` / ``token``),
    we restrict the read sites to that kind. Otherwise every matching
    contract row is considered.

    `cross_file=True` (default): for `object_property` shape aliases, also
    follow consumers in files that import the leaf file. Disable to keep
    the trace strictly intra-file.

    Returns a structured flow report — see module docstring.
    """
    # 1. Gather read sites from the contracts table.
    rows = list(store.contracts_by_name(name, kind))
    read_sites = [
        {"file": r["file"], "line": int(r["line"]),
         "role": r["role"], "kind": r["kind"], "confidence": r["confidence"]}
        for r in rows
        if (r["role"] or "") in ("read", "use", "occurrence", "declare")
    ]
    # Java Bean setter write sites are also "interesting" — they mutate
    # the flag and the enclosing method is the natural consumer. Pulled
    # separately from read_sites so the flow_graph can tag them
    # distinctly (`setter-write` hop) and the consumer list surfaces
    # the argument value (true/false/expr).
    setter_writes = [
        {"file": r["file"], "line": int(r["line"]),
         "role": r["role"], "kind": r["kind"],
         "confidence": r["confidence"],
         "context": r["context"] or ""}
        for r in rows
        if (r["role"] == "write"
            and (r["context"] or "").startswith("java-setter"))
    ]

    # Audit fix: when `--kind` narrowed the result to empty, don't just
    # return {}. Check whether the name exists under OTHER kinds and
    # surface a hint. Reason: agents would run
    # `projmem flow FEATURE_TASKS_V2 --kind env` on a name that's
    # actually a SettingKey token, see empty output, and conclude the
    # name doesn't exist — when in reality the kind filter was wrong.
    hints: List[str] = []
    if kind and not read_sites:
        other_rows = store.contracts_by_name(name)
        other_kinds: Dict[str, List[Dict[str, Any]]] = {}
        for r in other_rows:
            k = r["kind"]
            if k == kind:
                continue
            if r["file"] == "<config>":
                continue
            other_kinds.setdefault(k, []).append({
                "file": r["file"], "line": int(r["line"] or 0),
                "role": r["role"],
            })
        if other_kinds:
            kind_list = ", ".join(f"{k} ({len(v)} site{'s' if len(v) != 1 else ''})"
                                    for k, v in sorted(other_kinds.items()))
            sample_cmd = (f"projmem flow {name} --kind "
                          f"{sorted(other_kinds)[0]}")
            hints.append(
                f"No `{kind}` sites for {name!r}, but it exists under: "
                f"{kind_list}. Try `{sample_cmd}`.")

    flow_graph: List[Dict[str, Any]] = []
    local_aliases: List[Dict[str, Any]] = []
    consumers: List[Dict[str, Any]] = []
    seen_consumers: set = set()

    # 2. For each read site: find the local assignment on that line (if any),
    #    then walk the local's in-file refs.
    for site_idx, site in enumerate(read_sites):
        hop_count = len(flow_graph)
        flow_graph.append({
            "hop":    hop_count,
            "kind":   f"{site['kind']}-read",
            "at":     f"{site['file']}:{site['line']}",
            "detail": {"confidence": site["confidence"],
                       "role": site["role"]},
            "site_idx": site_idx,
        })
        enclosing = _enclosing_symbol(store, site["file"], site["line"])
        if enclosing:
            flow_graph[-1]["detail"]["enclosing_symbol"] = enclosing["name"]

        src_line = _read_line(repo_root, site["file"], site["line"]) or ""
        alias = _detect_local_alias(src_line, name)
        if not alias:
            continue
        local_name, _, shape = alias
        local_aliases.append({
            "file":          site["file"],
            "line":          site["line"],
            "local_name":    local_name,
            "contract_name": name,
            "shape":         shape,
        })
        flow_graph.append({
            "hop":    len(flow_graph),
            "kind":   "local-assign",
            "at":     f"{site['file']}:{site['line']}",
            "detail": {"local_name": local_name,
                       "shape": shape,
                       "enclosing_symbol": (enclosing or {}).get("name")},
            "site_idx": site_idx,
        })

        # 3. Consumers of the captured local. Scope depends on shape:
        #    - local_var / python_env: scan within the enclosing symbol
        #      (tight scope; the var doesn't escape).
        #    - object_property: scan the WHOLE FILE — the property is
        #      attached to a returned object and consumers read it via
        #      destructuring or member access in OTHER functions.
        #
        # We can't rely on the refs table for either case: JS/TS indexers
        # don't record every local-variable read, and the property-read
        # form `obj.tscWatchFile` is captured under the property name only.
        end_line: Optional[int] = None
        scan_start = site["line"]
        if shape == "object_property":
            # Whole-file scope. Start from line 1 so we catch consumers
            # that appear BEFORE the assignment (the pattern is common
            # when the file declares an interface/type at the top, then
            # the assignment lives in a return-block lower down).
            scan_start = 0
            end_line = None  # whole file
        else:
            if enclosing:
                enc_row = store.conn.execute(
                    "SELECT end_line FROM symbols WHERE file=? AND line=? "
                    "AND name=?",
                    (site["file"], enclosing["line"], enclosing["name"])
                ).fetchone()
                if enc_row and enc_row["end_line"]:
                    end_line = int(enc_row["end_line"])
        hits = _scan_local_consumers(
            repo_root, site["file"], local_name,
            start_line=scan_start, end_line=end_line,
            max_hits=max_consumers_per_read)
        # Filter the assignment line itself out of consumers (the scan
        # starts at line 0 for object_property which would include it).
        hits = [(ln, txt) for ln, txt in hits if ln != site["line"]]
        # Cross-file consumers for object_property aliases. The captured
        # value is exposed via the file's exported object — consumers
        # live in importer files. Bounded scan; tagged as
        # `cross-file-read` / `cross-file-destructure`.
        cross_hits: List[Dict[str, Any]] = []
        if cross_file and shape == "object_property":
            cross_hits = _scan_cross_file_consumers(
                store, repo_root, site["file"], local_name)
        # Pre-fetch refs at this name in this file so we can mark hits as
        # ast-confirmed when the structural index ALSO sees them. Tree-sitter
        # records calls/new/import/binding/property-access — not every local
        # read — so absence isn't a false-positive flag, just a confidence
        # downgrade. Use a (line) set for O(1) lookups per hit.
        ast_lines = {int(r["line"]) for r in store.conn.execute(
            "SELECT line FROM refs WHERE file=? AND name=?",
            (site["file"], local_name))}
        for hit_line, hit_text in hits:
            key = (site["file"], hit_line)
            if key in seen_consumers:
                continue
            seen_consumers.add(key)
            tag = _classify_consumer_line(hit_text, local_name)
            cons_symbol = _enclosing_symbol(store, site["file"], hit_line)
            ast_confirmed = hit_line in ast_lines
            rec = {
                "file":     site["file"],
                "line":     hit_line,
                "via":      tag,
                "ref_kind": "text-scan",  # local reads aren't in the ref table
                "symbol":   (cons_symbol or {}).get("name"),
                "snippet":  hit_text.strip()[:200],
                "source_read_at": f"{site['file']}:{site['line']}",
                "local_name": local_name,
                "ast_confirmed": ast_confirmed,
                "confidence": "high" if ast_confirmed else "medium",
            }
            consumers.append(rec)
            flow_graph.append({
                "hop":    len(flow_graph),
                "kind":   tag,
                "at":     f"{site['file']}:{hit_line}",
                "detail": {"local_name": local_name,
                           "enclosing_symbol": (cons_symbol or {}).get("name"),
                           "snippet": rec["snippet"]},
                "site_idx": site_idx,
            })

        # Append cross-file consumer hits (object_property only).
        for ch in cross_hits:
            key = (ch["file"], ch["line"])
            if key in seen_consumers:
                continue
            seen_consumers.add(key)
            consumers.append(ch)
            flow_graph.append({
                "hop":    len(flow_graph),
                "kind":   ch["via"],
                "at":     f"{ch['file']}:{ch['line']}",
                "detail": {"local_name": local_name,
                           "enclosing_symbol": ch.get("symbol"),
                           "importer": ch["importer"],
                           "leaf": ch["leaf"],
                           "snippet": ch["snippet"]},
                "site_idx": site_idx,
            })

    # 3b. Java Bean-setter writes. Each site is a direct mutation of the
    # flag; the enclosing method is the consumer we want to surface. The
    # argument value (true / false / expression) lives in the site's
    # `context` field as `java-setter` or `java-setter:<value>`.
    for w in setter_writes:
        key = (w["file"], w["line"])
        if key in seen_consumers:
            continue
        seen_consumers.add(key)
        enclosing = _enclosing_symbol(store, w["file"], w["line"])
        src_line = _read_line(repo_root, w["file"], w["line"]) or ""
        # Parse the value off the context tag. "java-setter" alone means
        # the arg wasn't a boolean literal — still useful, just no value.
        ctx = w.get("context", "")
        value = None
        if ":" in ctx:
            _, _, value = ctx.partition(":")
        rec = {
            "file":     w["file"],
            "line":     w["line"],
            "via":      "setter-call",
            "ref_kind": "java-bean-setter",
            "symbol":   (enclosing or {}).get("name"),
            "snippet":  src_line.strip()[:200],
            "setter_value": value,
            "confidence": "high",
        }
        consumers.append(rec)
        flow_graph.append({
            "hop":    len(flow_graph),
            "kind":   "setter-call",
            "at":     f"{w['file']}:{w['line']}",
            "detail": {"enclosing_symbol": (enclosing or {}).get("name"),
                       "setter_value": value,
                       "snippet":    rec["snippet"]},
        })

    # 4. Coverage summary.
    cross_file_count = sum(1 for c in consumers
                            if str(c.get("via", "")).startswith("cross-file"))
    coverage = {
        "read_site_count":       len(read_sites),
        "local_alias_count":     len(local_aliases),
        "consumer_count":        len(consumers),
        "switch_case_count":     sum(1 for c in consumers
                                      if c["via"] == "switch-case"),
        "conditional_count":     sum(1 for c in consumers
                                      if c["via"] == "conditional"),
        "plain_read_count":      sum(1 for c in consumers
                                      if c["via"] == "read"),
        "setter_call_count":     sum(1 for c in consumers
                                      if c["via"] == "setter-call"),
        "cross_file_count":      cross_file_count,
        "scope": ("single+cross-file (object_property aliases)"
                   if cross_file_count > 0
                   else "single-file"),
    }
    notes: List[str] = []
    if read_sites and not local_aliases:
        notes.append(
            "No local aliases detected. The contract is read but its value "
            "is consumed inline (no captured variable) — consumer chain "
            "limited to the immediate enclosing symbol.")
    # Round-5 P3: only emit the truncation note when there ARE
    # consumers AND the cap actually engaged. Previously fired on
    # every empty result because `0 >= max * 0` is `True`.
    if (consumers and read_sites
            and len(consumers) >= max_consumers_per_read * len(read_sites)):
        notes.append(
            f"consumer list truncated at {max_consumers_per_read} per "
            "read site; pass a larger --max-consumers to see more.")

    return {
        "subject":       name,
        "kind_filter":   kind,
        "read_sites":    read_sites,
        "local_aliases": local_aliases,
        "consumers":     consumers,
        "flow_graph":    flow_graph,
        "coverage":      coverage,
        "notes":         notes,
        "hints":         hints,
    }
