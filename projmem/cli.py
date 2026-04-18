"""projmem CLI — drift-aware code memory for AI agents."""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional

from . import config as config_mod
from . import graph, indexer, packs, runtime_evidence, git_support
from .store import Store
from . import symbols as symbols_mod


def _open_store(root: str, *, allow_unindexed: bool = False,
                 as_json: bool = False):
    """Open the projmem store at `root`.

    By default, gates on the presence of a prior index at this root —
    pointing at an unindexed dir produces a structured `no-index`
    error instead of silent zeros (round-4 finding #3). `index` /
    `init` pass `allow_unindexed=True` so they can build a fresh
    store from scratch.
    """
    cfg = config_mod.load(root)
    os.makedirs(cfg.store_dir, exist_ok=True)
    store = Store(cfg.db_path)
    if not allow_unindexed:
        _require_indexed(cfg, store, as_json=as_json)
    return cfg, store


def _emit_error(payload: Dict[str, Any], as_json: bool,
                  *, exit_code: int = 2,
                  store=None) -> None:
    """Single chokepoint for structured error returns.

    Round-4-followup meta-fix: dozens of paths emit a correctly shaped
    `{error, message, ...}` envelope but then return 0. CI gates that
    rely on shell exit codes pass on those failures. Any code path
    that builds a structured error MUST funnel through here so the
    process exits non-zero (default 2) by construction.

    Closes the entire "envelope right, exit wrong" class — F002, F004,
    plus every future error site that uses `_emit_error`.
    """
    if store is not None:
        try:
            store.close()
        except Exception:
            pass
    _emit(payload, as_json)
    sys.exit(exit_code)


def _resolve_target_or_hint(store, cfg, target: str, *,
                              command: str,
                              as_json: bool) -> Dict[str, Any]:
    """Round-5 round-2 meta-fix #2: shared "does this target actually
    exist?" gate for any command that takes a `<target>` positional.

    Three classes of inputs are accepted:
      - file path indexed in the store
      - file existing on disk under cfg.root (even if unindexed)
      - bare symbol name with at least one def in the store

    On success returns a `loc` dict from `symbols.locate`. On miss
    routes through `_emit_error` with a kebab `target-not-found` tag,
    a substring-match `suggestions` list (cheap fuzzy assist), and
    an actionable hint. Closes the silent-success-on-unknown-target
    class (round-5-r2 F008).
    """
    from . import symbols as _symbols
    loc = _symbols.locate(store, target, project_root=cfg.root)
    kind = (loc or {}).get("kind")
    # File / directory / symbol with defs → ok.
    if kind in ("file", "directory"):
        return loc
    if kind == "symbol" and loc.get("defs"):
        return loc
    # Miss path. Build cheap suggestions: substring-LIKE on files +
    # symbols. Capped to 5 each to keep the payload small.
    like = f"%{target}%"
    file_hits: List[str] = []
    symbol_hits: List[Dict[str, Any]] = []
    try:
        for r in store.conn.execute(
                "SELECT path FROM files WHERE path LIKE ? "
                "ORDER BY length(path) LIMIT 5", (like,)):
            file_hits.append(r["path"])
        for r in store.conn.execute(
                "SELECT DISTINCT name, file FROM symbols "
                "WHERE name LIKE ? LIMIT 5", (like,)):
            symbol_hits.append({"name": r["name"], "file": r["file"]})
    except Exception:
        pass
    payload: Dict[str, Any] = {
        "error":   "target-not-found",
        "message": (f"{command}: target {target!r} did not resolve to a "
                     "file or symbol in the index"),
        "target":  target,
        "command": command,
        "suggestions": {
            "files":   file_hits,
            "symbols": symbol_hits,
        },
        "hint": ("Pass an indexed file path (`projmem files --glob "
                  "'*'` to list), a `file#symbol` qualified id, OR a "
                  "bare symbol name that has at least one def. Run "
                  "`projmem search " + target + "` for a cross-bucket "
                  "fuzzy match."),
    }
    _emit_error(payload, as_json, store=store)
    return loc  # never reached


def _emit_with_memory(out: Dict, as_json: bool, store) -> None:
    """Attach the `repo_memory` header then emit.

    Every user-facing READ command funnels through this so agents see
    a memory-presence signal on every interaction — the key UX fix for
    "second agent behaves like first agent because memory is hidden".
    Never raises; on store-query failure the header reports
    has_memory=None and the caller still sees the primary payload.

    F008: when `contradicted_count > 0`, the BLOCKER signal must
    propagate into the process exit code (1, distinct from the `2`
    that structured errors use). Without this, CI gates that read
    only `$?` couldn't tell that a saved FACT had been REFUTED. The
    behavior matches CLAUDE.md's "STOP — a saved FACT was REFUTED"
    semantics: caller can override with PROJMEM_NO_BLOCKER=1.
    """
    from . import memory_header as _mh
    _mh.attach(out, store)
    _emit(out, as_json)
    contradicted = 0
    try:
        contradicted = int((out.get("repo_memory") or {})
                            .get("contradicted_count") or 0)
    except (TypeError, ValueError):
        contradicted = 0
    if contradicted > 0 and os.environ.get("PROJMEM_NO_BLOCKER") != "1":
        # Distinct from the "2" used by `_emit_error` — callers that
        # treat any non-zero as failure still detect it; callers that
        # discriminate know exit-1 means "data is good but a saved
        # FACT was refuted; investigate before trusting downstream
        # decisions". Set PROJMEM_NO_BLOCKER=1 in CI when blockers are
        # tracked separately.
        sys.exit(1)


def _benchmark_line(cfg, store, refuted_in_run: Optional[int] = None,
                    as_json: bool = False) -> None:
    """Emit the trust ribbon to stderr unless suppressed.

    Suppressed when:
      - PROJMEM_NO_BENCHMARK=1 is set (CI / scripted callers)
      - `as_json=True` — the caller asked for machine output and
        ribbon-on-stderr, while parseable, was an explicit user
        complaint ("--json should suppress stderr banners"). When
        you ask for JSON you want clean stderr too.
      - all four counters are zero (the ribbon would be noise)
    """
    if os.environ.get("PROJMEM_NO_BENCHMARK") == "1":
        return
    if as_json:
        return
    try:
        from . import benchmark as _bm
        _bm.emit(store, cfg.root, refuted_in_run=refuted_in_run)
    except Exception:
        pass  # ribbon is decorative — never block the command


def _foreign_index_warning(cfg, store) -> Optional[dict]:
    """Return a dict describing a foreign-index condition, or None.

    An index DB is "foreign" when the root it was built at no longer
    matches the current project root. That's the exact failure mode the
    user reports (fresh unzip of a repo has a stale .projmem/index.db
    pointing at another machine's path). Silently serving packs from
    that DB is exactly the "false certainty" failure class projmem is
    supposed to expose.
    """
    indexed_root = store.get_meta("root")
    if not indexed_root:
        return None
    # Compare after normalization so `./foo` vs `/abs/foo` doesn't trigger.
    # Use `realpath` to resolve symlinks — on macOS `/tmp` → `/private/tmp`,
    # so abspath alone would false-positive every test run that indexes
    # under /tmp. realpath follows the link, making the comparison
    # semantic-equality rather than string-equality of the path.
    cur = os.path.realpath(cfg.root)
    idx = os.path.realpath(indexed_root)
    if cur == idx:
        return None
    return {
        "indexed_root": indexed_root,
        "current_root": cfg.root,
        "note": "FOREIGN INDEX: the store was built at a different root. "
                "Results may reflect another machine's files or an older "
                "layout. Run `projmem index` to rebuild at this root, or "
                "set PROJMEM_ALLOW_FOREIGN=1 to acknowledge.",
    }


def _require_indexed(cfg, store, *, as_json: bool = False) -> None:
    """Hard-error when no index exists at this root.

    Round-4 finding #3: pointing at an unindexed dir returned silent
    zeros (`{files: 0, symbols: 0, ...}`) instead of a structured
    error. Any read command that calls this gets a
    `{error: "no-index", ...}` payload + exit 2 when the store has
    never been built. The marker we trust is the `root` meta key,
    set inside `index_all`. Files-table emptiness alone isn't enough
    (a freshly indexed empty repo also has zero files but a real
    `root` row).
    """
    indexed_root = None
    try:
        indexed_root = store.get_meta("root")
    except Exception:
        indexed_root = None
    if indexed_root:
        return
    payload = {
        "error":   "no-index",
        "message": (f"no projmem index at {cfg.root!r}; nothing to "
                    "read."),
        "path":    cfg.root,
        "hint": ("Run `projmem index --path " + cfg.root + "` first. "
                  "Read commands refuse to silently return empty "
                  "results when the database hasn't been built."),
    }
    try:
        store.close()
    except Exception:
        pass
    _emit(payload, as_json)
    sys.exit(2)


def _require_fresh_index(cfg, store, writer=sys.stderr,
                          as_json: bool = False) -> Optional[dict]:
    """Refuse to serve from a foreign index unless the env override is set.
    Returns the warning dict (for callers that want to include it in JSON
    output) or None.

    Also gates on the existence of any index at all — if the root has
    never been indexed, returns a structured error and exits. This is
    the fix for round-4 #3 (silent zeros on `--path /tmp`)."""
    _require_indexed(cfg, store, as_json=as_json)
    warn = _foreign_index_warning(cfg, store)
    if not warn:
        return None
    if os.environ.get("PROJMEM_ALLOW_FOREIGN") == "1":
        writer.write(
            f"projmem: warning: foreign index (indexed_root="
            f"{warn['indexed_root']}, current_root={warn['current_root']}). "
            "Continuing because PROJMEM_ALLOW_FOREIGN=1.\n")
        return warn
    raise SystemExit(
        f"projmem: refusing to use foreign index.\n"
        f"  indexed_root: {warn['indexed_root']}\n"
        f"  current_root: {warn['current_root']}\n"
        f"Run `projmem index` to rebuild, or set PROJMEM_ALLOW_FOREIGN=1 "
        f"to override."
    )


def cmd_index(args):
    # Round-5-r2 F005: refuse `projmem index /` (filesystem root) and
    # other catastrophic targets BEFORE the walker plows into
    # `/proc`, `/dev`, mounts, etc., where it's guaranteed to OSError.
    # Also catch OSError from the walker itself so the caller sees a
    # structured error instead of a Python traceback.
    real_root = os.path.realpath(args.path)
    if real_root in ("/", os.path.realpath(os.path.expanduser("~"))):
        _emit_error(
            {"error":   "refusing-catastrophic-root",
             "message": (f"refusing to index {real_root!r} — too broad. "
                          "Pick a project-scoped subdirectory."),
             "path":    real_root,
             "hint": ("Pass `--path <project_root>` pointing at a "
                       "specific repository. Indexing the filesystem "
                       "root or your home directory is almost never "
                       "intended and would scan millions of irrelevant "
                       "paths.")},
            getattr(args, "json", False))
    cfg, store = _open_store(args.path, allow_unindexed=True)
    try:
        counts = indexer.index_all(
            cfg, store, force=args.force,
            extra_includes=args.include or None,
            extra_excludes=args.exclude or None,
            exclude_wins=getattr(args, "exclude_wins", False),
        )
    except OSError as e:
        _emit_error(
            {"error":   "filesystem-error",
             "message": f"index failed: {e}",
             "path":    cfg.root,
             "hint": ("Check that the path exists and is readable; "
                       "narrow scope with `--include`/`--exclude` or "
                       "`.projmemignore`. Re-run with PROJMEM_DEBUG=1 "
                       "for the full traceback.")},
            getattr(args, "json", False), store=store)
    _benchmark_line(cfg, store,
                    as_json=getattr(args, "json", False))
    store.close()
    out = {"root": cfg.root, **counts}
    if args.exclude or args.include:
        out["patterns"] = {"include": args.include or [],
                           "exclude": args.exclude or []}
    # Round-5 P3: an `--include` pattern that matches zero new files
    # AND zero unchanged files used to look like "indexed: 0,
    # unchanged: 0" — silently empty. Now we surface a structured
    # warning so the caller knows the glob produced no work.
    if args.include and counts.get("indexed", 0) == 0 \
            and counts.get("unchanged", 0) == 0:
        out["include_no_match_warning"] = {
            "severity": "warning",
            "message": ("--include pattern(s) matched zero files. "
                        "Either the glob is wrong, the matched files "
                        "were excluded by another rule, or they live "
                        "in directories the discovery walker skips."),
            "patterns": args.include,
            "hint": ("Drop the include filter to test, or pass "
                      "`projmem scope --json` to see what the walker "
                      "considered. Common gotcha: globs are matched "
                      "against repo-relative paths (no leading `/`).")
        }
    # HIGH-severity warning for silently-dropped oversize files. This is
    # surfaced AT INDEX TIME (not buried in stats) because the user is
    # right there and can act: raise max_file_bytes in .projmem/config.json
    # or narrow scope. Without this warning, critical files like
    # TypeScript's 3.15 MB checker.ts disappeared from the index and
    # every downstream symbol lookup returned empty defs.
    oversize = counts.get("oversize_skipped") or []
    if oversize:
        out["oversize_warning"] = {
            "severity": "high",
            "count": len(oversize),
            "message": (
                f"{len(oversize)} file(s) skipped because they exceed "
                f"max_file_bytes={cfg.max_file_bytes}. Raise the limit in "
                "`.projmem/config.json` (e.g. `\"max_file_bytes\": 10000000`) "
                "or accept the blind spot. Files:"
            ),
            "files": [
                {"path": o["path"], "size": o["size"], "limit": o["limit"]}
                for o in oversize
            ],
        }
    print(json.dumps(out, indent=2))


def _parser_coverage(store) -> Dict:
    """Compute AST vs regex vs none breakdown for parser_coverage output.

    Returns a dict with:
      total_files      — total indexed file count
      ast_grounded     — {count, pct} for treesitter:* + ast parsers
      regex_fallback   — {count, pct} for regex parser
      no_parser        — {count, pct} for none / null parsers
      per_lang         — per language-key dict with ast/regex/total/ast_pct

    "AST-grounded" = any parser whose name starts with "treesitter:" or is
    exactly "ast". Regex means the heuristic fallback. none/null means the
    file was discovered but not indexed by any symbol extractor.
    """
    rows = list(store.conn.execute(
        "SELECT lang, parser, COUNT(*) AS n FROM files GROUP BY lang, parser"))

    total = 0
    ast_count = 0
    regex_count = 0
    none_count = 0
    per_lang: Dict[str, Dict] = {}

    for r in rows:
        lang = r["lang"] or "unknown"
        parser = r["parser"] or "none"
        n = r["n"]
        total += n

        is_ast = parser.startswith("treesitter:") or parser == "ast"
        is_regex = parser == "regex"
        is_none = not is_ast and not is_regex

        if is_ast:
            ast_count += n
        elif is_regex:
            regex_count += n
        else:
            none_count += n

        entry = per_lang.setdefault(lang, {"ast": 0, "regex": 0, "none": 0})
        if is_ast:
            entry["ast"] += n
        elif is_regex:
            entry["regex"] += n
        else:
            entry["none"] += n

    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    per_lang_out = {}
    for lang, e in sorted(per_lang.items()):
        lang_total = e["ast"] + e["regex"] + e["none"]
        per_lang_out[lang] = {
            "ast": e["ast"],
            "regex": e["regex"],
            "none": e["none"],
            "total": lang_total,
            "ast_pct": round(100.0 * e["ast"] / lang_total, 1)
                       if lang_total else 0.0,
        }

    return {
        "total_files": total,
        "ast_grounded":   {"count": ast_count,   "pct": pct(ast_count)},
        "regex_fallback": {"count": regex_count,  "pct": pct(regex_count)},
        "no_parser":      {"count": none_count,   "pct": pct(none_count)},
        "per_lang":       per_lang_out,
    }


def cmd_stats(args):
    from . import ts_backend
    cfg, store = _open_store(args.path)
    parser_dist = {r[0]: r[1] for r in store.conn.execute(
        "SELECT parser, COUNT(*) FROM files GROUP BY parser")}
    regex_js = list(store.conn.execute(
        "SELECT COUNT(*) FROM files WHERE parser='regex' "
        "AND lang IN ('javascript','typescript')"))[0][0]
    from . import binding as _binding
    out = {
        "root": cfg.root,
        **store.stats(),
        "indexed_root": store.get_meta("root"),
        "parser_distribution": parser_dist,
        "parser_coverage": _parser_coverage(store),
        "ref_binding": _binding.binding_summary(store),
        "tree_sitter_available": ts_backend.available(),
    }
    if regex_js > 0 and not ts_backend.available():
        out["warning"] = (
            f"{regex_js} JS/TS file(s) indexed via REGEX (AST unavailable). "
            "Install the tree-sitter extra for same-file ref tracking: "
            "`pip install -e '.[treesitter]'`."
        )
    elif regex_js > 0:
        out["warning"] = (
            f"{regex_js} JS/TS file(s) indexed via REGEX despite tree-sitter "
            "being available — likely a parse fallback. Re-run with "
            "PROJMEM_DEBUG=1 projmem index --force to see the cause."
        )
    # Surface foreign-index condition on stats too — stats is the first
    # command a user runs after picking up a fresh checkout.
    foreign = _foreign_index_warning(cfg, store)
    if foreign:
        out["foreign_index_warning"] = foreign
    store.close()
    print(json.dumps(out, indent=2))


def cmd_symbol(args):
    from .store import (roles_describe, ROLE_READ, ROLE_WRITE, ROLE_CALL,
                        ROLE_NEW, ROLE_IMPORT, ROLE_IMPORT_BINDING,
                        ROLE_CALLBACK, ROLE_SHORTHAND, ROLE_TEST)
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    # Same-name disambiguation: bare name lookups are a frequent source of
    # false attachment in large repos. Require explicit disambiguation when
    # multiple defs exist, unless the caller opts into ambiguity.
    from . import symbol_id as _sid
    sym_name = args.name
    file_filter = getattr(args, "file", None)
    ambiguity_warning: Optional[Dict[str, Any]] = None
    if not _sid.is_symbol_id(sym_name):
        # Support `file#name` shorthand without forcing the user to pass --file.
        if "#" in sym_name:
            fp, sym = sym_name.rsplit("#", 1)
            sym_name = re.split(r"[.#/!]", sym, maxsplit=1)[0]
            if not file_filter and fp:
                file_filter = fp
        if not getattr(args, "allow_ambiguous", False) and not file_filter:
            # Ambiguous if defined in >1 location. Compute counts cheaply.
            try:
                total_defs = store.conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE name=?", (sym_name,)).fetchone()[0]
                distinct_files = store.conn.execute(
                    "SELECT COUNT(DISTINCT file) FROM symbols WHERE name=?", (sym_name,)).fetchone()[0]
            except Exception:
                total_defs = 0
                distinct_files = 0
            if total_defs > 1 or distinct_files > 1:
                rows = list(store.conn.execute(
                    "SELECT file, kind, line, symbol_id FROM symbols "
                    "WHERE name=? ORDER BY file, line LIMIT 25",
                    (sym_name,)))
                candidates = [
                    {"file": r["file"], "kind": r["kind"], "line": r["line"],
                     "symbol_id": r["symbol_id"]}
                    for r in rows
                ]
                # Strict by default (matches `callees-of`): refuse to
                # silently pick when ambiguous. The agent must either
                # disambiguate (`projmem symbol <file>#<name>` or
                # `--file PATH`) or opt into soft-pick mode with
                # `--allow-ambiguous`. The previous default soft-picked
                # one def's refs while leaving the other 5 invisible
                # in the ref count — agents read the 25-ref number
                # as ground truth and missed broader call sites.
                # `--strict-ambiguity` still works as an explicit opt-in
                # to the same hard-error path (now it's the default).
                strict = (getattr(args, "strict_ambiguity", False)
                           or not getattr(args, "allow_ambiguous", False))
                if strict:
                    store.close()
                    _emit({
                        "error": "ambiguous-symbol",
                        "name": args.name,
                        "symbol": sym_name,
                        "candidate_count": int(total_defs),
                        "distinct_file_count": int(distinct_files),
                        "candidates": candidates,
                        "hint": (
                            f"AMBIGUITY: symbol {sym_name!r} has multiple "
                            "definitions. Re-run with a file-qualified query "
                            f"(`projmem symbol <file>#{sym_name}`) or pass "
                            "--file PATH, or use the canonical symbol_id "
                            "from `candidates`."
                        ),
                    }, args.json)
                    sys.exit(2)
                # Pick a primary guess: the candidate with the most incoming
                # refs (the "most-connected" def). Ties broken by shortest
                # file path, then lexicographic. Confidence reflects how
                # decisive that choice is.
                ref_counts = {}
                for c in candidates:
                    n = store.conn.execute(
                        "SELECT COUNT(*) AS n FROM refs WHERE name=? AND file=?",
                        (sym_name, c["file"])).fetchone()["n"]
                    ref_counts[(c["file"], c["line"])] = n
                # Deprioritize defs that live under test trees — tests
                # often define a same-named fixture (`class Flask(...)`
                # in `tests/test_config.py`) that shadows the real
                # canonical def. The agent almost always means the
                # source-tree def. We sort source defs FIRST, then by
                # ref-count, then path length / alphabetical.
                def _is_test(path: str) -> bool:
                    p = (path or "").lower().replace("\\", "/")
                    return any(seg in p.split("/")
                               for seg in ("tests", "test", "spec",
                                           "__tests__", "specs"))
                def _rank(c):
                    rc = ref_counts.get((c["file"], c["line"]), 0)
                    return (1 if _is_test(c["file"]) else 0,
                            -rc,
                            len(c["file"] or ""),
                            c["file"] or "")
                ranked = sorted(candidates, key=_rank)
                primary = ranked[0]
                total_refs = sum(ref_counts.values()) or 1
                primary_share = ref_counts[(primary["file"],
                                            primary["line"])] / total_refs
                ambiguity_warning = {
                    "severity": "warning",
                    "candidate_count": int(total_defs),
                    "distinct_file_count": int(distinct_files),
                    "candidates": candidates,
                    "primary_guess": primary,
                    "primary_guess_confidence": round(primary_share, 3),
                    "hint": (
                        f"Symbol {sym_name!r} has {total_defs} definitions "
                        f"across {distinct_files} file(s). Proceeding with "
                        f"the most-connected def ({primary['file']}:"
                        f"{primary['line']}). Re-run with "
                        f"`projmem symbol {primary['file']}#{sym_name}` to "
                        "pin to this def explicitly, or "
                        "`--strict-ambiguity` to treat ambiguity as an error."
                    ),
                }
                # Scope the primary query to the guessed file so the refs
                # we return belong to THAT def, not the union of all.
                file_filter = primary["file"]
    refs = graph.symbol_refs(store, args.name if _sid.is_symbol_id(args.name) else sym_name,
                             file=file_filter)
    # `file_filter` narrows BOTH defs and refs. Refs SHOULD be scoped
    # (we want refs to the chosen def's identity, not the union), but
    # the defs LIST should show every def of the symbol — otherwise
    # `symbol normalize` on a polymorphic name returns 1 def while
    # the index has 6, and the agent reads it as ground truth. Restore
    # the full def list when ambiguity was detected.
    if ambiguity_warning is not None and ambiguity_warning.get("candidates"):
        # Re-emit all candidate defs as `defs`, marking which one's
        # refs we returned so the consumer can re-scope if needed.
        all_defs = []
        primary_file = (ambiguity_warning.get("primary_guess") or {}).get("file")
        primary_line = (ambiguity_warning.get("primary_guess") or {}).get("line")
        for c in ambiguity_warning["candidates"]:
            entry = dict(c)
            entry["is_primary"] = (c.get("file") == primary_file
                                    and c.get("line") == primary_line)
            all_defs.append(entry)
        refs["defs"] = all_defs
    # M8: each def row already carries `symbol_id` from the column. Surface
    # it to the consumer so re-queries can use the canonical ID and
    # bypass the ambiguous-name path.
    for d in refs["defs"]:
        d.setdefault("symbol_id", d.get("symbol_id"))
    # Annotate roles on every ref row so the consumer can query by role bitset.
    for r in refs["refs"]:
        rv = r.get("roles") or 0
        r["roles"] = rv
        r["roles_describe"] = roles_describe(rv)
    # Optional role-based filter (M1): --role read|write|call|new|...
    if getattr(args, "role", None):
        name_to_bit = {
            "read": ROLE_READ, "write": ROLE_WRITE, "call": ROLE_CALL,
            "new": ROLE_NEW, "import": ROLE_IMPORT,
            "import_binding": ROLE_IMPORT_BINDING,
            "callback": ROLE_CALLBACK, "shorthand": ROLE_SHORTHAND,
            "test": ROLE_TEST,
        }
        mask = 0
        for tok in args.role.split(","):
            tok = tok.strip()
            if tok in name_to_bit:
                mask |= name_to_bit[tok]
        if mask:
            refs["refs"] = [r for r in refs["refs"] if (r.get("roles") or 0) & mask]
    # Annotate each ref with the parser used on its file — caller can see at
    # a glance which counts are AST-grounded vs regex lower-bounds.
    parser_by_file = {r[0]: r[1] for r in store.conn.execute(
        "SELECT path, parser FROM files")}
    for r in refs["refs"]:
        r["parser"] = parser_by_file.get(r["file"])
    for d in refs["defs"]:
        d["parser"] = parser_by_file.get(d["file"])
    # Resolve --context. Accepts an integer string OR the literal "auto".
    # `auto` picks 2 lines in --json mode (agent-friendly, low byte cost)
    # and 0 in human/terminal mode (avoid clobbering interactive views).
    raw_ctx = getattr(args, "context", 0)
    try:
        n_ctx = int(raw_ctx)
    except (TypeError, ValueError):
        if str(raw_ctx).lower() == "auto":
            n_ctx = 2 if getattr(args, "json", False) else 0
        else:
            n_ctx = 0
    # Optional --context N: attach a snippet of N lines around each ref
    # / def site, read from disk. Cuts agent round-trips by letting
    # triage happen from the projmem output alone.
    if n_ctx > 0:
        from .config import load as _load
        _cfg = _load(args.path)
        n = n_ctx
        for collection in (refs["refs"], refs["defs"]):
            for r in collection:
                fp = os.path.join(_cfg.root, r["file"])
                try:
                    with open(fp, "r", encoding="utf-8",
                              errors="replace") as f:
                        lines = f.readlines()
                except OSError:
                    continue
                ln = int(r.get("line") or 1)
                start = max(1, ln - n)
                end = min(len(lines), ln + n)
                r["snippet"] = "".join(lines[start - 1:end])
                r["snippet_range"] = [start, end]
    # Artifact-vs-source classification. By default, refs living in build
    # output / snapshots / changelogs / baselines are moved OUT of the
    # primary ref list so ref_count reflects real source consumers — the
    # blast-radius the agent actually cares about. `--include-artifacts`
    # keeps the legacy behaviour (everything in one bucket).
    from . import artifacts as _artifacts
    include_artifacts = bool(getattr(args, "include_artifacts", False))
    artifact_refs: list = []
    if not include_artifacts:
        src_refs, artifact_refs = _artifacts.partition_refs(
            refs["refs"], path_key="file")
        refs["refs"] = src_refs
        # Annotate each artifact ref with WHY we classified it so a user
        # passing --include-artifacts can audit the filter.
        for a in artifact_refs:
            cls, reason = _artifacts.classify_file(a.get("file") or "")
            a["artifact_class"] = cls
            a["artifact_reason"] = reason
    # Count only ref sources we'd consider "trusted" (AST/ast) as an exact
    # count; everything else is a lower bound.
    regex_ref_count = sum(1 for r in refs["refs"]
                          if (r.get("parser") or "").startswith("regex"))
    out = {
        "name": args.name,
        **refs,
        "ref_count": len(refs["refs"]),
        "ref_count_is_lower_bound": regex_ref_count > 0,
        "refs_from_regex_parsers": regex_ref_count,
        "note": "Per-row `parser` names the backend that captured the row. "
                "If `ref_count_is_lower_bound` is true, the real count is "
                ">= ref_count — the regex parser dedups at (name, line) and "
                "cannot see refs that don't syntactically look like calls.",
    }
    # Soft ambiguity: surface the warning block + primary guess so the
    # agent has structured disambiguation data without a second round-trip.
    if ambiguity_warning:
        out["ambiguity_warning"] = ambiguity_warning
    # Surface artifact refs as a separate bucket — they remain reachable
    # (agents can `--include-artifacts` to see them inline) but don't
    # inflate the primary count.
    if artifact_refs:
        out["artifact_refs"] = artifact_refs
        out["artifact_ref_count"] = len(artifact_refs)
        out["artifact_filter_note"] = (
            f"{len(artifact_refs)} ref(s) in build output / snapshot / "
            "changelog / baseline paths excluded from `ref_count`. "
            "Pass `--include-artifacts` to include them."
        )
    # Native binding edges: JS-facing string names bound to C/C++ functions
    # (Node/V8 SetMethod/NODE_SET_METHOD patterns). These edges are stored
    # separately from `edges` so they can be cleaned up on reindex.
    try:
        from . import symbol_id as _sid
        lookup = args.name
        if _sid.is_symbol_id(lookup):
            lookup = (_sid.parse(lookup).get("name") or lookup)
        if "#" in lookup:
            lookup = lookup.rsplit("#", 1)[-1]
        lookup = re.split(r"[.#/!]", lookup, maxsplit=1)[0]
        binds = [dict(r) for r in store.bindings_for_js_name(lookup)]
        if not binds:
            # Query by native side too when the user asked for the C++ name.
            for d in refs.get("defs") or []:
                sid = d.get("symbol_id")
                if sid:
                    binds += [dict(r) for r in store.bindings_for_cpp_symbol_id(sid)]
            if not binds and lookup:
                binds = [dict(r) for r in store.bindings_for_cpp_name(lookup)]
        if binds:
            for b in binds:
                b.setdefault("type", "binding")
            out["binding_edges"] = binds
    except Exception:
        pass
    # Exhaustive mode: full-text scan parity with `rg -w <name>` across the
    # indexed scope. This does NOT upgrade structured refs; it surfaces the
    # gap explicitly and returns concrete match sites as lower-trust evidence.
    if getattr(args, "exhaustive", False):
        from . import implicit as _implicit
        scan = _implicit.scan_text_matches(
            store,
            cfg.root,
            args.name,
            capture_limit=int(getattr(args, "exhaustive_max_matches", 5000)),
            include_line_text=not getattr(args, "exhaustive_no_line_text", False),
        )
        structured_sites = {
            (r.get("file"), int(r.get("line") or 0))
            for r in (refs.get("refs") or [])
        }
        # Treat defs as "accounted for" so a def-only symbol doesn't report
        # a fake implicit gap.
        structured_sites |= {
            (d.get("file"), int(d.get("line") or 0))
            for d in (refs.get("defs") or [])
        }
        implicit_refs = [
            m for m in (scan.get("matches") or [])
            if (m.get("file"), int(m.get("line") or 0)) not in structured_sites
        ]
        out.update({
            "structured_refs": refs.get("refs") or [],
            "structured_defs": refs.get("defs") or [],
            "text_match_count": int(scan.get("text_count") or 0),
            "text_matches": scan.get("matches") or [],
            "text_matches_truncated": bool(scan.get("matches_truncated")),
            "implicit_refs": implicit_refs,
            "coverage": {
                "structured": len(refs.get("refs") or []),
                "text": int(scan.get("text_count") or 0),
                "gap": max(0, int(scan.get("text_count") or 0)
                           - len(refs.get("refs") or [])),
            },
            "exhaustive_scan": {
                "files_scanned": int(scan.get("files_scanned") or 0),
                "files_skipped": int(scan.get("files_skipped") or 0),
                "capture_limit": int(scan.get("capture_limit") or 0),
                "skipped_reason": scan.get("skipped_reason"),
            },
            "note_exhaustive": (
                "Exhaustive mode returns a raw word-boundary text scan across "
                "the indexed scope. `text_matches` includes comments/strings "
                "and may include false positives; `implicit_refs` are the "
                "subset of `text_matches` not already covered by structured "
                "defs/refs. Do NOT treat `text_matches` as structured graph "
                "evidence."
            ),
        })
    # Implicit-usage gap check. Compares the structured ref count to a
    # word-boundary text scan of the indexed files. If the gap is large
    # we emit a warning — agents should NOT treat the structured count
    # as exhaustive when macro / string-based / generated usage is in
    # play (the Codex benchmark flagged 68 structured vs 88 text matches
    # on a macro-heavy symbol). Controlled by --no-implicit-check for
    # callers who want to skip the extra scan.
    if not getattr(args, "no_implicit_check", False):
        from . import implicit as _implicit
        if _implicit.is_identifier(args.name):
            scan = _implicit.count_text_occurrences(store, cfg.root, args.name)
            # Subtract def count from text_count — definitions would
            # otherwise double-count against refs.
            struct_total = len(refs["refs"]) + len(refs["defs"])
            verdict = _implicit.detect_implicit_usage(
                structured_count=struct_total,
                text_count=scan["text_count"],
                text_scan_truncated=scan["truncated"],
            )
            out["implicit_usage"] = {**verdict, "text_scan": scan}
    # Freshness probe: re-hash every file we're about to report on. A
    # divergent hash means the agent is reading from a stale index.
    from . import freshness as _fresh
    touched_files = {r.get("file") for r in refs.get("refs", [])
                     if r.get("file")}
    touched_files |= {d.get("file") for d in refs.get("defs", [])
                      if d.get("file")}
    stale = _fresh.check_paths(store, cfg.root, touched_files)
    warn = _fresh.freshness_warning(stale)
    if warn:
        out["freshness_warning"] = warn
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_reverse(args):
    cfg, store = _open_store(args.path)
    foreign = _require_fresh_index(cfg, store)
    # P0#3 lazy refresh — before trusting the index for this target,
    # re-hash the target file and reindex it if on-disk content drifted.
    # Keeps the product promise: "ask a question, get a CURRENT answer";
    # agents can't silently consume stale results.
    auto_refresh_out = None
    if not getattr(args, "no_auto_refresh", False):
        from . import freshness as _fresh
        auto_refresh_out = _fresh.auto_refresh_if_stale(
            cfg, store, [args.target], max_files=1)
    rev = graph.reverse_deps(store, args.target)
    fwd = graph.forward_deps(store, args.target)
    # Default: separate artifact deps from real source consumers.
    from . import artifacts as _artifacts
    include_artifacts = bool(getattr(args, "include_artifacts", False))
    artifact_rev: list = []
    artifact_fwd: list = []
    if not include_artifacts:
        rev, artifact_rev = _artifacts.partition_refs(rev, path_key="file")
        fwd, artifact_fwd = _artifacts.partition_refs(fwd, path_key="file")
        for a in artifact_rev + artifact_fwd:
            cls, reason = _artifacts.classify_file(a.get("file") or "")
            a["artifact_class"] = cls
            a["artifact_reason"] = reason
    out = {"target": args.target,
           "reverse_dependencies": rev,
           "direct_dependencies": fwd}
    if artifact_rev:
        out["artifact_reverse_dependencies"] = artifact_rev
    if artifact_fwd:
        out["artifact_direct_dependencies"] = artifact_fwd
    if (artifact_rev or artifact_fwd) and not include_artifacts:
        out["artifact_filter_note"] = (
            f"{len(artifact_rev)+len(artifact_fwd)} dep(s) in build output / "
            "snapshot / changelog / baseline paths excluded. Pass "
            "`--include-artifacts` to include them in the primary lists."
        )
    # Freshness probe for the target + everything we're surfacing.
    # (Auto-refresh above already handled the target; this catches any
    # other file in the reported list that's drifted.)
    from . import freshness as _fresh
    paths_to_check = {args.target}
    paths_to_check |= {r.get("file") for r in rev if r.get("file")}
    paths_to_check |= {r.get("file") for r in fwd if r.get("file")}
    stale = _fresh.check_paths(store, cfg.root, paths_to_check)
    warn = _fresh.freshness_warning(stale)
    if warn:
        out["freshness_warning"] = warn
    if auto_refresh_out and auto_refresh_out.get("refreshed"):
        out["auto_refreshed"] = auto_refresh_out["refreshed"]
    if foreign:
        out["foreign_index_warning"] = foreign
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_forward(args):
    """Outbound-only view: who does THIS file depend on?

    `projmem reverse` returns both inbound and outbound edges for
    historical reasons, which confuses readers who expect a literal
    "only inbound" view. `projmem forward` is the other half — just
    outbound edges (imports from this file's POV)."""
    cfg, store = _open_store(args.path)
    foreign = _require_fresh_index(cfg, store)
    out = {"target": args.target,
           "direct_dependencies": graph.forward_deps(store, args.target)}
    if foreign:
        out["foreign_index_warning"] = foreign
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_pack(args):
    cfg, store = _open_store(args.path)
    foreign = _require_fresh_index(cfg, store)
    # P0#3 lazy refresh on the target file before pack assembly.
    # Skip when target looks like a directory ('.', './', or 'src/') — the
    # repo-overview branch in build_pack handles those without needing to
    # refresh a single file. Audit fix #9.
    auto_refresh_out = None
    target_file = args.target.split("#", 1)[0]
    _is_dir_target = (target_file in (".", "./") or target_file.endswith("/")
                       or os.path.isdir(os.path.join(cfg.root, target_file)))
    if not getattr(args, "no_auto_refresh", False) and not _is_dir_target:
        from . import freshness as _fresh
        auto_refresh_out = _fresh.auto_refresh_if_stale(
            cfg, store, [target_file], max_files=1)
    force_kind = None
    if args.as_file:
        force_kind = "file"
    elif args.as_symbol:
        force_kind = "symbol"
    pack = packs.build_pack(cfg, store, args.target,
                            radius=args.radius,
                            include_tests=not args.no_tests,
                            force_kind=force_kind,
                            include_snippets=args.snippets,
                            snippet_bytes=args.snippet_bytes,
                            include_source=args.include_source,
                            timeout_secs=getattr(args, "timeout", None),
                            include_artifacts=getattr(args, "include_artifacts",
                                                       False))
    # Hard error when the target couldn't be resolved at all. Previous
    # behavior: emit the partially-built pack with `target.resolved =
    # False` and exit 0 — which let scripts proceed as if the target
    # existed and operate on empty fields. Stress test surfaced this
    # on `pack does/not/exist.java`. Now: structured error + exit 2.
    _tgt = pack.get("target") or {}
    if (_tgt.get("resolved") is False
            and _tgt.get("kind") == "file"):
        store.close()
        _emit({
            "error":      "target-not-found",
            "target":     args.target,
            "kind":       "file",
            "exists_on_disk_but_not_indexed":
                _tgt.get("exists_on_disk_but_not_indexed", False),
            "hint": ("File not found in the index. Run `projmem index` "
                      "if it was just added, or check `projmem files` for "
                      "the canonical path. For symbol-style targets use "
                      "`projmem symbol <name>` first to discover the "
                      "right file/path."),
        }, args.json)
        sys.exit(2)
    if foreign:
        pack.setdefault("unknowns", []).insert(0, {
            "kind": "foreign-index",
            **foreign,
        })
    # Read-time freshness probe: compare on-disk hash vs indexed hash for
    # every file the pack touches. If anything drifted, surface it so the
    # agent doesn't trust a stale pack.
    from . import freshness as _fresh
    touched: set = set()
    tgt = pack.get("target") or {}
    if isinstance(tgt, dict):
        if tgt.get("path"):
            touched.add(tgt["path"])
        if tgt.get("file"):
            touched.add(tgt["file"])
    for d in pack.get("direct_dependencies") or []:
        if d.get("file"):
            touched.add(d["file"])
    for d in pack.get("reverse_dependencies") or []:
        if d.get("file"):
            touched.add(d["file"])
    stale = _fresh.check_paths(store, cfg.root, touched)
    warn = _fresh.freshness_warning(stale)
    if warn:
        pack["freshness_warning"] = warn
    if auto_refresh_out and auto_refresh_out.get("refreshed"):
        pack["auto_refreshed"] = auto_refresh_out["refreshed"]
    # Attach repo_memory header so the agent sees the memory signal
    # on every pack call — not just when they happen to call `notes`.
    from . import memory_header as _mh
    _mh.attach(pack, store)
    # --budget enforcement: trim the payload to fit the requested token
    # ceiling before we render. Sections shrink in reverse priority so
    # `target`/`direct_dependencies`/notes survive when the budget bites.
    pack = _apply_budget(pack, getattr(args, "budget", None),
                         priority=["target", "direct_dependencies",
                                   "reverse_dependencies", "notes",
                                   "freshness_warning", "coverage",
                                   "repo_memory"])
    out_path = None
    if args.write:
        out_path = packs.write_pack(cfg, pack, name=args.name)
    if args.markdown:
        text = packs.render_markdown(pack)
        if out_path:
            md_path = out_path.replace(".json", ".md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(text)
            print(json.dumps({"pack_json": out_path, "pack_md": md_path,
                              "coverage": pack["coverage"],
                              "repo_memory": pack.get("repo_memory")},
                             indent=2))
        else:
            print(text)
    else:
        if out_path:
            print(json.dumps({"pack_json": out_path,
                              "coverage": pack["coverage"],
                              "repo_memory": pack.get("repo_memory")},
                             indent=2))
        else:
            print(json.dumps(pack, indent=2))
    store.close()


def _apply_budget(payload, budget, *, priority=None):
    """Trim `payload` to fit `budget` tokens. No-op when budget is None."""
    if not budget:
        return payload
    from . import budget as _bud
    return _bud.fit_to_budget(payload, int(budget), priority=priority)


def cmd_contracts(args):
    """List contracts in a file, or occurrences of a named contract.

    Correctness fix (round-3 report bug #1): `--kind` was ignored when the
    target was a file path — only the by-name branch honored it. Now applied
    at the query layer so every returned row respects the filter.
    """
    cfg, store = _open_store(args.path)
    target = args.target
    # Reuse the same file-detection used by `pack` so package.json /
    # config-file targets work consistently across commands.
    from .symbols import _looks_like_path, _normalize_target_path
    normalized = _normalize_target_path(target)
    looks_like_file = (_looks_like_path(target) or _looks_like_path(normalized)
                       or store.get_file(normalized) is not None)
    if looks_like_file:
        # Prefer normalized path for the lookup
        target = normalized
    if looks_like_file:
        rows = list(store.contracts_in_file(target))
        if args.kind:
            rows = [r for r in rows if r["kind"] == args.kind]
        out = {
            "file": target,
            "kind_filter": args.kind,
            "contracts": [dict(r) for r in rows],
            "count": len(rows),
        }
        # Coverage hint when 0 contracts on a Java/JVM file.
        # projmem's contract extractors target env-var reads,
        # CLI flags, Zod/envalid/Drizzle schemas, and enum-token
        # patterns — none of which match Java's Bean-setter +
        # XML-attribute config style. A silent zero misled the
        # Tomcat audit; surface the limitation here so the agent
        # knows this isn't a "no contracts in this file" answer
        # but a "no contracts of types projmem extracts."
        if len(rows) == 0 and target.lower().endswith(
                (".java", ".kt", ".scala", ".groovy")):
            out["coverage_note"] = (
                "projmem contract extractors target env-var reads, "
                "CLI flags, Zod/envalid/Drizzle schemas, enum tokens "
                "— Java Bean-setter / XML-attribute config is not "
                "currently extracted. Use `projmem search <flag_name>` "
                "or `projmem symbol <setterName>` to find Java config "
                "sites manually.")
        paths_touched = {target}
    else:
        rows = [dict(r) for r in store.contracts_by_name(target, args.kind)]
        out = {
            "name": target,
            "kind_filter": args.kind,
            "occurrences": rows,
            "count": len(rows),
        }
        paths_touched = {r.get("file") for r in rows if r.get("file")}
    # Freshness probe for the files we read from.
    from . import freshness as _fresh
    stale = _fresh.check_paths(store, cfg.root, paths_touched)
    warn = _fresh.freshness_warning(stale)
    if warn:
        out["freshness_warning"] = warn
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_entrypoints(args):
    """List detected/declared entrypoints. Round-3 fix: `indexed` column now
    exposes whether the entrypoint target is actually in the index DB. Pass
    `--hide-unindexed` to suppress entries pointing at files we don't have."""
    cfg, store = _open_store(args.path)
    rows = [dict(r) for r in store.entrypoints()]
    unindexed = [r for r in rows if not r.get("indexed", 1)]
    if args.hide_unindexed:
        rows = [r for r in rows if r.get("indexed", 1)]
    out = {
        "entrypoints": rows,
        "unindexed_count": len(unindexed),
    }
    if unindexed and not args.hide_unindexed:
        out["warning"] = (
            f"{len(unindexed)} entrypoint(s) reference files NOT in the index "
            "(e.g. package.json main pointing at an excluded path). These are "
            "listed with indexed=0. They cannot be used for reachability."
        )
    store.close()
    _emit(out, args.json)


def cmd_explain(args):
    """Show human-readable reasoning for a target: summary of deps, contracts, unknowns."""
    cfg, store = _open_store(args.path)
    # Strict ambiguity check (matches `symbol` and `callees-of`):
    # if `target` is a bare symbol name with multiple defs and the
    # caller hasn't disambiguated, hard-error with candidates rather
    # than silent-pick. Previous behavior buried the warning in an
    # "Unknowns" section of the markdown — agents read the rest of
    # the pack as ground truth and missed that 5 other defs exist.
    target = args.target
    looks_bare_symbol = ("/" not in target and "#" not in target
                         and ":" not in target and "." not in target)
    if looks_bare_symbol and not getattr(args, "allow_ambiguous", False):
        try:
            n = store.conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE name=?",
                (target,)).fetchone()[0]
        except Exception:
            n = 0
        if n > 1:
            rows = list(store.conn.execute(
                "SELECT file, kind, line, symbol_id FROM symbols "
                "WHERE name=? ORDER BY file, line LIMIT 25",
                (target,)))
            store.close()
            _emit({
                "error":           "ambiguous-symbol",
                "name":            target,
                "candidate_count": int(n),
                "candidates": [
                    {"file": r["file"], "kind": r["kind"],
                     "line": r["line"], "symbol_id": r["symbol_id"]}
                    for r in rows
                ],
                "hint": ("AMBIGUITY: multiple defs of "
                          f"{target!r}. Re-run with "
                          f"`projmem explain <file>#{target}` to pin "
                          "to a specific def, or `--allow-ambiguous` "
                          "to render a pack on the first match."),
            }, args.json)
            sys.exit(2)
    pack = packs.build_pack(cfg, store, target, radius=1)
    # Default JSON to match every other read command. Old behavior
    # (markdown) is one flag away. Mixing markdown-default into a
    # JSON-default tool was a UX trap — agents that piped explain
    # into jq broke silently.
    if getattr(args, "markdown", False):
        print(packs.render_markdown(pack))
        store.close()
    else:
        _emit_with_memory(pack, args.json, store)


def cmd_changes(args):
    """Show what's been edited. Designed for fresh-session agents asking
    "what changed since last session?" without git history.

    Default (no flags): reads the append-only file_edits log and shows
    edits from the most recent index session, PLUS any on-disk drift
    that hasn't been re-indexed yet. This answers "what did we edit
    last time?" even when `projmem complete` has rotated the
    `pre-index` snapshot past those edits.

    Alternatives:
      --since <snapshot>  — old snapshot-diff mode (contract+symbol)
      --hours N           — edits from the last N hours
      --last N            — last N log entries regardless of session
      --all-sessions      — every edit row in the log (capped)
    """
    from . import changes as _changes
    cfg, store = _open_store(args.path)
    if args.hours is not None:
        mode, hours, last = "hours", args.hours, None
    elif args.last is not None:
        mode, hours, last = "last", None, args.last
    elif args.all_sessions:
        mode, hours, last = "all", None, None
    else:
        mode, hours, last = "auto", None, None
    out = _changes.compute_changes(
        store, cfg.root,
        since=args.since,
        max_files=args.limit,
        max_per_file=args.per_file_limit,
        include_unchanged_files=args.include_unchanged,
        mode=mode, hours=hours, last=last)
    from . import memory_header as _mh
    _mh.attach(out, store)
    _emit(out, args.json)
    store.close()


def cmd_refresh(args):
    """Detect changes vs. the index AND apply them by default.

    "Changes" = modified + added + deleted files (full disk-vs-index
    delta). Modified/added files are re-parsed incrementally and
    deleted files are removed from the index — the index is brought
    back in sync with disk.

    Quantum-thinking-round bug fix: previously detect-only by default;
    `--reindex` was a separate flag agents/users forgot to pass. The
    auto-extract verifier loop depends on the index being current —
    a stale index produces stale verdicts. Now `refresh` actually
    refreshes; `--detect-only` opts back into the look-but-don't-touch
    behavior.

    Cheap: only the changed paths are re-parsed. Use this AFTER
    editing code; it's the everyday alternative to a full
    `projmem index` rebuild.
    """
    cfg, store = _open_store(args.path)
    changes = indexer.discover_changes(cfg, store)
    to_reindex = changes["modified"] + changes["added"]
    out: Dict[str, Any] = {
        "modified": changes["modified"],
        "added":    changes["added"],
        "deleted":  changes["deleted"],
        "modified_count": len(changes["modified"]),
        "added_count":    len(changes["added"]),
        "deleted_count":  len(changes["deleted"]),
    }
    # Default: apply. `--detect-only` (or legacy `--no-reindex`)
    # preserves the look-but-don't-touch path.
    detect_only = (getattr(args, "detect_only", False)
                    or getattr(args, "no_reindex", False))
    if not detect_only:
        applied: Dict[str, Any] = {"reindexed": 0, "removed": 0}
        if to_reindex:
            counts = indexer.index_all(cfg, store, paths=to_reindex,
                                        force=True)
            applied["reindexed"] = counts.get("indexed", 0)
        # Remove deleted files from the index. `index_all(paths=...)` does
        # NOT touch rows outside the supplied paths, so deletions need an
        # explicit removal step.
        for p in changes["deleted"]:
            store.remove_file(p)
            applied["removed"] += 1
        store.commit()
        out["applied"] = applied
    else:
        out["hint"] = ("Detect-only mode (`--detect-only`); the index "
                        "was NOT updated. Re-run without the flag to "
                        "apply changes.")
    store.close()
    _emit(out, args.json)


def cmd_status(args):
    cfg, store = _open_store(args.path)
    stats = store.stats()
    stale = indexer.check_staleness(cfg, store)
    out = {"root": cfg.root, "stats": stats,
           "stale_files": stale,
           "store_path": cfg.db_path}
    store.close()
    _emit(out, args.json)


def cmd_git(args):
    cfg, store = _open_store(args.path)
    if not git_support.is_git(cfg.root):
        out = {"git": False, "note": "Not a git repo."}
    else:
        out = {"git": True,
               "recent_commits": git_support.recent_commits(cfg.root, args.target, args.limit)}
    store.close()
    _emit(out, args.json)


def cmd_callees_of(args):
    """Transitive callees of a symbol — "what's the blast radius of changing X?"

    Walks intra-file call edges starting at `symbol`, bounded by `--depth` and
    `--limit` to keep runaway fan-out (e.g. a utility called by everything)
    from producing a useless forest. Same-file only by default since that's
    the decisive monolith question; cross-file expansion is left to
    per-layer tools because call-edges across files require proper name
    resolution to distinguish same-named symbols.
    """
    cfg, store = _open_store(args.path)
    from . import packs as _packs

    # Find the def file for the symbol if not specified.
    file_path = args.file
    if not file_path:
        defs = [dict(r) for r in store.symbols_by_name(args.symbol)]
        if not defs:
            _emit_error(
                {"symbol": args.symbol,
                 "error":  "symbol-undefined",
                 "message": (f"no definition for {args.symbol!r} in the "
                              "index"),
                 "hint": ("Run `projmem symbol " + args.symbol +
                           "` to confirm; check the file is indexed.")},
                args.json, store=store)
        if len(defs) > 1:
            files = [d["file"] for d in defs]
            _emit_error(
                {"symbol":  args.symbol,
                 "error":   "ambiguous-symbol",
                 "message": (f"{args.symbol!r} has {len(defs)} "
                              "definitions; pick one with `--file`."),
                 "candidates": files,
                 "hint": "Pass `--file PATH` to disambiguate."},
                args.json, store=store)
        file_path = defs[0]["file"]

    cg = _packs._intra_file_calls(store, file_path, cap=0)
    # Capture scope BEFORE closing — store needed for the meta lookup.
    from .scope import get_vendor_prefixes, is_in_scope
    vendor_prefixes = get_vendor_prefixes(store)
    store.close()

    if cg.get("partial"):
        _emit({"symbol": args.symbol, "file": file_path,
               "partial": True, "warnings": cg.get("warnings", []),
               "tree": [], "note": "Call graph unavailable for this file."},
              args.json)
        return

    # Build an adjacency map of from→[to] on the full edge set.
    adj: Dict[str, List[dict]] = {}
    for e in cg["edges"]:
        adj.setdefault(e["from"], []).append(e)

    # BFS bounded by depth and total-nodes budget.
    tree: List[dict] = []
    visited = {args.symbol}
    frontier = [(args.symbol, 0, None)]
    while frontier and len(visited) < args.limit:
        caller, depth, parent_line = frontier.pop(0)
        if depth >= args.depth:
            continue
        for e in adj.get(caller, []):
            callee = e["to"]
            record = {"from": caller, "to": callee, "line": e["line"],
                      "depth": depth + 1}
            tree.append(record)
            if callee not in visited:
                visited.add(callee)
                frontier.append((callee, depth + 1, e["line"]))

    # Round-X feedback: schema was inconsistent — `tree` / `unique_reachable`
    # were the only keys; consumers expecting `callees`/`unique_callees` got
    # 0. Add stable aliases + document the canonical schema.
    unique_reachable = sorted(visited - {args.symbol})
    file_in_scope = is_in_scope(file_path, vendor_prefixes)
    for row in tree:
        row["in_scope"] = file_in_scope
    out_payload = {
        "symbol": args.symbol,
        "file": file_path,
        "depth": args.depth,
        "in_scope": file_in_scope,
        "vendor_prefixes": vendor_prefixes,
        # Canonical fields
        "tree": tree,
        "unique_reachable": unique_reachable,
        "reachable_count": len(visited) - 1,
        "truncated": len(visited) >= args.limit,
        "parser": cg.get("parser"),
        # Ergonomic aliases (always present; same data; agent-friendly names)
        "callees": tree,
        "unique_callees": unique_reachable,
        "callee_count": len(visited) - 1,
        "schema_version": 1,
        "note": "Transitive intra-file callees from `symbol` at bounded depth. "
                "Same-file only; cross-file transitive calls require "
                "global name resolution to avoid merging same-named "
                "symbols. Caller attribution uses the same scope "
                "approximation as `callgraph`. SCHEMA: `tree` and "
                "`callees` are the same list [{from,to,line,depth}]; "
                "`unique_reachable` and `unique_callees` are the same "
                "set; `reachable_count` and `callee_count` are the same "
                "int. Aliases preserved for agent ergonomics.",
    }
    _emit(out_payload, args.json)


def cmd_callgraph(args):
    """Intra-file call graph (default). With --cross-file: also include
    edges whose callee is defined in a DIFFERENT file (e.g. a handler
    in withContext.ts calling hasPermission in auth/rbac.ts). The
    cross-file edges are tagged `cross_file: true` with `to_file` and
    confidence labels so consumers can separate them from intra-file.
    """
    cfg, store = _open_store(args.path)
    from . import packs as _packs
    # Validate `args.file` is actually an indexed file path. The arg
    # is named `file` internally but agents naturally pass a SYMBOL
    # name (`callgraph normalize`), which silently returns an empty
    # graph because no file matches. Be explicit: if the input
    # doesn't have a path shape AND isn't in the files table, hard-
    # error and suggest the right command.
    looks_like_path = ("/" in args.file or "." in args.file)
    file_row = store.conn.execute(
        "SELECT 1 FROM files WHERE path=? LIMIT 1",
        (args.file,)).fetchone()
    if not looks_like_path and file_row is None:
        # Smell-test: do we have a SYMBOL by this name? Then point
        # the agent at `symbol` instead.
        sym_count = 0
        try:
            sym_count = store.conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE name=?",
                (args.file,)).fetchone()[0]
        except Exception:
            pass
        store.close()
        _emit({
            "error":  "callgraph-needs-file-path",
            "got":    args.file,
            "looks_like_symbol": sym_count > 0,
            "hint": (
                f"`callgraph` takes a FILE path, not a symbol name. "
                + (f"`{args.file}` looks like a symbol — use "
                   f"`projmem symbol {args.file}` first to find the "
                   f"file(s) it's defined in, then re-run "
                   f"`projmem callgraph <that_file>`."
                   if sym_count > 0 else
                   f"`{args.file}` is neither a path nor a known "
                   "symbol. Try `projmem files` for the indexed list.")
            ),
        }, args.json)
        sys.exit(2)
    # --all disables edge truncation; --limit 0 is the same.
    cap = 0 if args.all else args.limit
    cg = _packs._intra_file_calls(store, args.file, cap=cap)
    if getattr(args, "cross_file", False):
        xf = _packs._cross_file_calls(store, args.file, cap=cap)
        # Merge edges; prepend an inter-file nodes-summary the consumer
        # can use to enumerate callee files touched.
        cg_edges = cg.get("edges") or []
        cg_edges = list(cg_edges) + list(xf.get("edges") or [])
        cg["edges"] = cg_edges
        cg["cross_file_edge_count"] = xf.get("total", 0)
        cg["cross_file_truncated"] = xf.get("truncated", False)
        if xf.get("warnings"):
            cg.setdefault("warnings", []).extend(xf["warnings"])
    store.close()
    # Round-3 fix bug #5: stable envelope regardless of filter mode. ALL
    # outputs now include `file`, `nodes`, `edges`, `total`, `truncated`,
    # `by_caller_count`, `partial`, `warnings`, `parser`. Filter modes add
    # `symbol`/`function` + `incoming`/`outgoing`/`calls` on top.
    out: Dict[str, object] = {
        "file": args.file,
        "nodes": cg.get("nodes", []),
        "edges": cg.get("edges", []),
        "total": cg.get("total", 0),
        "truncated": cg.get("truncated", False),
        "by_caller_count": cg.get("by_caller_count", {}),
        "partial": cg.get("partial", False),
        "warnings": cg.get("warnings", []),
        "parser": cg.get("parser"),
        # Self-test D3 fix: preserve caller_attribution so consumers can
        # tell exact-range (M2) from scope-approximation fallback. Was
        # being stripped by this envelope projection.
        "caller_attribution": cg.get("caller_attribution"),
        "note": cg.get("note", ""),
        "filter_symbol": None,
        "in_function": None,
    }
    if args.in_function:
        subset = [e for e in cg["edges"] if e["from"] == args.in_function]
        out["in_function"] = args.in_function
        out["edges"] = subset           # scoped view replaces full edges
        out["total"] = len(subset)
        out["calls"] = subset           # legacy/ergonomic alias
        out["unique_callees"] = sorted({e["to"] for e in subset})
        # Preserve full-file by_caller_count so consumers can still see the
        # whole distribution; filter only narrows `edges`.
    elif args.filter_to:
        sym = args.filter_to
        subset = [e for e in cg["edges"]
                  if e["from"] == sym or e["to"] == sym]
        out["filter_symbol"] = sym
        out["edges"] = subset
        out["total"] = len(subset)
        out["incoming"] = [e for e in subset if e["to"] == sym]
        out["outgoing"] = [e for e in subset if e["from"] == sym]
    _emit(out, args.json)


# Structural kinds = real named definitions, not stringy/contract artifacts.
# Default filter for orphans/parity so they stop mixing comment words with
# real symbols. Callers can override with --kind or --include-kind.
_STRUCTURAL_KINDS = (
    "function", "class", "method", "exported", "var",
    "interface", "type", "enum", "struct", "trait", "module", "object",
)

# Round-4 report P1: `parity` referenced-but-undefined returned 100% noise
# because every `obj.length`, `arr.map`, `console.log` captured `length`,
# `map`, `log` as refs — all "undefined" from projmem's perspective. These
# are language globals / common method names, not broken references.
# Filter is per-command (always applied to `referenced_but_undefined`) and
# can be disabled with `--include-builtins` for agents that want raw data.
# Round-5-r3 F004: short-name aliases for the values stored in the
# `files.lang` column. CLI consumers reach for `js`, `ts`, `py`, `cc`
# more often than the long names — without this map `--lang js`
# silently returned zero files. Alias source of truth lives here so
# we can extend in one place when new langs land.
_LANG_ALIASES = {
    "js":         "javascript",
    "jsx":        "javascript",
    "mjs":        "javascript",
    "cjs":        "javascript",
    "ts":         "typescript",
    "tsx":        "typescript",
    "py":         "python",
    "rb":         "ruby",
    "rs":         "rust",
    "kt":         "kotlin",
    "kts":        "kotlin",
    "cs":         "csharp",
    "cc":         "cpp",
    "cxx":        "cpp",
    "hpp":        "cpp",
    "hxx":        "cpp",
    "sh":         "bash",
    "yml":        "yaml",
    "md":         "markdown",
}


_JS_BUILTINS = frozenset({
    # Global constructors / values
    "String", "Number", "Boolean", "Array", "Object", "Symbol", "Function",
    "Math", "Date", "RegExp", "Error", "TypeError", "RangeError",
    "SyntaxError", "ReferenceError", "JSON", "Map", "Set", "WeakMap",
    "WeakSet", "Promise", "Proxy", "Reflect", "ArrayBuffer", "DataView",
    "Int8Array", "Uint8Array", "Uint8ClampedArray", "Int16Array",
    "Uint16Array", "Int32Array", "Uint32Array", "Float32Array",
    "Float64Array", "BigInt", "BigInt64Array", "BigUint64Array",
    "NaN", "Infinity", "undefined", "null", "true", "false", "globalThis",
    # Common globals (Node + browser)
    "console", "process", "Buffer", "global", "window", "document",
    "navigator", "location", "history", "fetch", "setTimeout",
    "setInterval", "clearTimeout", "clearInterval", "setImmediate",
    "clearImmediate", "queueMicrotask", "parseInt", "parseFloat", "isNaN",
    "isFinite", "encodeURIComponent", "decodeURIComponent",
    "encodeURI", "decodeURI", "require", "module", "exports", "__dirname",
    "__filename", "URL", "URLSearchParams", "AbortController",
    "AbortSignal", "TextEncoder", "TextDecoder", "EventTarget",
    "performance",
    # Common method / property names captured via `.method(` regex or tree-sitter
    "length", "name", "constructor", "prototype", "toString", "valueOf",
    "hasOwnProperty", "isPrototypeOf", "propertyIsEnumerable",
    "toLowerCase", "toUpperCase", "substring", "substr", "slice",
    "indexOf", "lastIndexOf", "split", "replace", "replaceAll", "match",
    "matchAll", "search", "test", "exec", "trim", "trimStart", "trimEnd",
    "padStart", "padEnd", "repeat", "concat", "charAt", "charCodeAt",
    "codePointAt", "normalize", "startsWith", "endsWith", "includes",
    "push", "pop", "shift", "unshift", "splice", "reverse", "sort", "fill",
    "flat", "flatMap", "map", "filter", "reduce", "reduceRight", "forEach",
    "find", "findIndex", "findLast", "findLastIndex", "some", "every",
    "keys", "values", "entries", "from", "of", "isArray", "join", "copyWithin",
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled",
    "any", "race",
    "on", "once", "off", "emit", "addListener", "prependListener",
    "removeListener", "removeAllListeners", "listeners", "eventNames",
    "addEventListener", "removeEventListener", "dispatchEvent",
    "get", "set", "has", "delete", "clear", "size",
    "error", "warn", "log", "info", "debug", "trace",
    # F007: common middleware / callback names that show up as bare
    # `name();` calls but aren't user-defined symbols. Express/Connect
    # idiom: `(req, res, next) => { ... next(); }`. Without these,
    # parity flagged every `next();` / `done();` / `cb(...)` as an
    # orphan ref because the param def isn't a top-level symbol.
    # `use` is the most-called Express method (`app.use(middleware)`)
    # — invocation surfaces as a bare ref by the regex fallback.
    "use", "next", "done", "cb", "callback",
    "req", "res", "ctx", "request", "response",
    "err",
    # Round-5-r3 F003: HTTP / Express / Koa / connect-style instance
    # methods that surface as bare member-calls (`app.listen(...)`,
    # `res.sendStatus(...)`, `req.session.regenerate(...)`). The
    # regex backend records them as call-kind refs by NAME with no
    # owning class, which then flooded `parity` as
    # "referenced-but-undefined". Listed here so they're filtered as
    # language/framework builtins by default.
    "listen", "close", "address",                       # net.Server
    "sendStatus", "sendFile", "send", "json", "jsonp",  # res.*
    "redirect", "render", "status", "type", "header",   # res.*
    "cookie", "clearCookie", "attachment", "download",  # res.*
    "format", "links", "vary", "append", "location",    # res.*
    "param", "query", "params",                          # req.*
    "regenerate", "reload", "save", "destroy", "touch", # session
    "pipe", "unpipe", "pause", "resume", "end",          # streams
    "param", "all", "route", "engine",                   # router
    "method", "headers", "body", "originalUrl", "url",   # req.*
    "ip", "ips", "hostname", "protocol", "secure",       # req.*
    "cookies", "signedCookies", "fresh", "stale",        # req.*
    "xhr", "subdomains", "accepts", "acceptsCharsets",   # req.*
    "acceptsEncodings", "acceptsLanguages",
    "is", "get",                                          # req.is, req.get
    "static", "Router",                                  # express.*
    # Promise + async patterns commonly chained via member call.
    "asCallback", "nodeify", "spread", "tap", "tapCatch",
})

_PY_BUILTINS = frozenset({
    # Callables
    "print", "len", "range", "list", "dict", "tuple", "set", "frozenset",
    "str", "int", "float", "bool", "bytes", "bytearray", "type", "id",
    "hash", "hex", "oct", "bin", "ord", "chr", "abs", "min", "max", "sum",
    "pow", "divmod", "round", "repr", "ascii", "format", "sorted",
    "reversed", "enumerate", "zip", "map", "filter", "any", "all",
    "iter", "next", "open", "input", "vars", "dir", "getattr", "setattr",
    "hasattr", "delattr", "isinstance", "issubclass", "callable",
    "staticmethod", "classmethod", "property", "super", "object", "slice",
    "memoryview", "complex", "globals", "locals", "compile", "exec", "eval",
    # Common attributes / method names
    "append", "extend", "pop", "remove", "insert", "clear", "copy",
    "count", "index", "sort", "reverse", "keys", "values", "items",
    "get", "setdefault", "update", "join", "split", "rsplit", "strip",
    "lstrip", "rstrip", "replace", "startswith", "endswith", "find",
    "rfind", "lower", "upper", "title", "encode", "decode", "format",
    "splitlines", "zfill", "add", "discard", "difference", "intersection",
    "union", "symmetric_difference", "issubset", "issuperset",
    "read", "write", "readline", "readlines", "close", "flush", "seek",
    "tell", "getvalue",
    # Exceptions
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "NameError", "ImportError",
    "ModuleNotFoundError", "RuntimeError", "StopIteration", "GeneratorExit",
    "FileNotFoundError", "OSError", "IOError", "NotImplementedError",
    "ArithmeticError", "ZeroDivisionError", "OverflowError",
    # Singletons / constants
    "None", "True", "False", "Ellipsis", "NotImplemented",
    "__name__", "__main__", "__file__", "__doc__", "__init__", "self", "cls",
})

_JAVA_BUILTINS = frozenset({
    # java.lang (auto-imported in every .java file)
    "Object", "String", "Integer", "Long", "Short", "Byte", "Float",
    "Double", "Boolean", "Character", "Number", "Class", "Enum",
    "Math", "System", "Thread", "Runnable", "Throwable", "Exception",
    "RuntimeException", "IllegalArgumentException", "IllegalStateException",
    "NullPointerException", "IndexOutOfBoundsException",
    "UnsupportedOperationException", "ClassCastException",
    "ArrayIndexOutOfBoundsException", "NumberFormatException",
    "InterruptedException", "Error", "AssertionError",
    "StringBuilder", "StringBuffer", "CharSequence", "Iterable",
    "Comparable", "Cloneable", "AutoCloseable",
    "Override", "Deprecated", "SuppressWarnings", "FunctionalInterface",
    "SafeVarargs",
    # java.util common
    "List", "ArrayList", "LinkedList", "Map", "HashMap", "LinkedHashMap",
    "TreeMap", "ConcurrentHashMap", "Set", "HashSet", "LinkedHashSet",
    "TreeSet", "Collection", "Collections", "Arrays", "Optional",
    "Objects", "Iterator", "Comparator", "Date", "Calendar",
    "TimeZone", "Locale", "UUID", "Properties", "Stack", "Queue",
    "Deque", "ArrayDeque", "PriorityQueue", "Vector",
    # java.util.function
    "Function", "BiFunction", "Consumer", "BiConsumer", "Supplier",
    "Predicate", "BiPredicate", "Runnable", "UnaryOperator",
    "BinaryOperator",
    # Objects.requireNonNull / Objects.equals etc — these are STATIC
    # method names that show up in refs as bare `requireNonNull`,
    # `equals`, `hashCode`. Common stdlib noise.
    "requireNonNull", "requireNonNullElse", "requireNonNullElseGet",
    "equals", "hashCode", "toString", "compare", "compareTo",
    "of", "ofNullable", "isPresent", "isEmpty", "orElse", "orElseGet",
    "orElseThrow", "ifPresent", "map", "flatMap", "filter",
    "stream", "parallelStream", "collect", "forEach", "reduce",
    "count", "findFirst", "findAny", "anyMatch", "allMatch",
    "noneMatch", "min", "max", "sorted", "distinct", "limit", "skip",
    # Common method names that leak via tree-sitter
    "size", "isEmpty", "contains", "containsKey", "containsValue",
    "add", "remove", "put", "get", "set", "clear", "addAll",
    "putAll", "removeAll", "retainAll", "iterator", "listIterator",
    "subList", "indexOf", "lastIndexOf", "toArray", "values", "keySet",
    "entrySet", "computeIfAbsent", "computeIfPresent", "putIfAbsent",
    "getOrDefault", "merge", "replace", "replaceAll",
    "length", "charAt", "indexOf", "substring", "concat", "split",
    "trim", "strip", "toLowerCase", "toUpperCase", "startsWith",
    "endsWith", "matches", "replaceFirst", "valueOf", "format",
    "printf", "println", "print", "append", "delete", "insert",
    "reverse", "setLength", "deleteCharAt",
    # Lifecycle / threading
    "run", "start", "stop", "join", "wait", "notify", "notifyAll",
    "sleep", "yield", "interrupt", "isInterrupted", "isAlive",
    "currentThread", "setDaemon", "setPriority", "getName", "getId",
    # Annotations commonly written without import (ambiguous lookup)
    "Test", "Before", "After", "BeforeAll", "AfterAll", "Nullable",
    "NonNull", "NotNull",
})

_BUILTINS_BY_LANG = {
    "javascript": _JS_BUILTINS, "typescript": _JS_BUILTINS,
    "tsx": _JS_BUILTINS, "jsx": _JS_BUILTINS,
    "python": _PY_BUILTINS,
    "java": _JAVA_BUILTINS, "kotlin": _JAVA_BUILTINS,
    "scala": _JAVA_BUILTINS,
}


def _is_language_builtin(name: str, file: str, store) -> bool:
    """Return True if `name` is a known language builtin in the context of
    `file`'s language. Conservative — we'd rather leak a few real typos than
    flood the output with `length`/`map`/`console`."""
    row = store.get_file(file)
    if not row:
        return False
    return name in _BUILTINS_BY_LANG.get(row["lang"], frozenset())


def _kind_filter_clause(args, col: str) -> str:
    if args.kind:
        kinds = [k.strip() for k in args.kind.split(",") if k.strip()]
    else:
        kinds = list(_STRUCTURAL_KINDS)
    # Parameterization is impossible with IN via sqlite3.execute positional,
    # so enforce a strict allowlist here instead of user strings.
    safe = [k for k in kinds if re.match(r"^[a-z_]+$", k)]
    if not safe:
        return ""
    joined = ",".join(f"'{k}'" for k in safe)
    return f" AND {col} IN ({joined})"


def cmd_reach(args):
    """List extracted guard conditions under which a symbol is called.

    Down-payment on H2 (reachability). Python-only for now. The condition is
    the *textual* form of the enclosing `if` — not normalized, not evaluated;
    it's the raw source. Useful for answering "under what gate does X fire?"
    when the gate is simple (`if flag:`, `if x.length == 0:`).
    Limitations: does not track nested elif branches as separate, does not
    extract early-return guards, does not resolve guards across function
    boundaries. Labelled confidence=medium accordingly.
    """
    cfg, store = _open_store(args.path)
    # Pre-flight language check: reach extracts Python-only guards.
    # On a non-Python repo it always returned 0 with a trailing
    # `note` field — agents read the empty list as ground truth.
    # Now: hard-error upfront with the indexed language mix so the
    # caller knows this command can't answer for their codebase.
    try:
        lang_rows = list(store.conn.execute(
            "SELECT lang, COUNT(*) AS n FROM files "
            "WHERE lang IS NOT NULL AND lang != '' "
            "GROUP BY lang ORDER BY n DESC"))
    except Exception:
        lang_rows = []
    py_files = next((r["n"] for r in lang_rows if r["lang"] == "python"),
                     0)
    if py_files == 0:
        store.close()
        _emit({
            "error": "unsupported-language",
            "command": "reach",
            "supported": ["python"],
            "indexed_languages": {r["lang"]: r["n"] for r in lang_rows},
            "hint": ("`reach` extracts Python-only guard conditions. "
                      "This repo has no indexed Python files. For Java/JS "
                      "use `projmem callees-of <fn>` (call sites) and "
                      "`projmem flow <contract>` (env/flag flow)."),
        }, args.json)
        sys.exit(2)
    rows = [dict(r) for r in store.conn.execute(
        "SELECT file, line, context FROM contracts "
        "WHERE kind='guard' AND name=? ORDER BY file, line",
        (args.symbol,))]
    store.close()
    _emit({
        "symbol": args.symbol,
        "reachable_from": rows,
        "count": len(rows),
        "note": "Call sites where `symbol` appears inside an `if` body. "
                "`context` is the raw source text of the guard expression. "
                "Python-only extractor.",
    }, args.json)


def _annotate_scope(rows: list, vendor_prefixes: list,
                    path_keys: tuple = ("file",)) -> None:
    """Mutate each row in-place adding `in_scope: bool` based on its
    path-shaped fields. Round-X feedback: every command result row should
    carry this so AI agents can filter vendor noise automatically.

    A row is `in_scope=True` only if EVERY listed path-key on it is in
    scope. Conservative: drift entries that span both own-code and vendor
    are tagged out-of-scope so vendor noise dominates filtering."""
    from .scope import is_in_scope
    for r in rows:
        if not isinstance(r, dict):
            continue
        all_in = True
        for key in path_keys:
            v = r.get(key)
            if isinstance(v, str) and not is_in_scope(v, vendor_prefixes):
                all_in = False
                break
            # Also probe nested rows when key is "sites" (drift output)
            if key == "sites" and isinstance(v, list):
                for s in v:
                    if isinstance(s, dict):
                        sf = s.get("file")
                        if isinstance(sf, str) and not is_in_scope(
                                sf, vendor_prefixes):
                            all_in = False
                            break
        r["in_scope"] = all_in


def cmd_contract_drift(args):
    """Find contract NAMES that appear with multiple distinct VALUES.

    Round-X report: 'canary token values / status enums drifting across
    files is a frequent real bug class in scanners'. We detect it by
    grouping `contracts` rows on (kind, name) and surfacing names where
    the `context` field carries different value-like strings, OR — more
    reliably — where the same flag/env name appears with conflicting
    declared values across config + parse sites.

    For tokens specifically: a `token` contract whose name appears in
    multiple files is interesting only if the file-set spans roles
    (one file declares, others use). Heuristic; documented in note.
    """
    cfg, store = _open_store(args.path)
    by_name: dict = {}
    for r in store.conn.execute(
            "SELECT kind, name, file, line, role, confidence, context "
            "FROM contracts"):
        if not r["name"] or r["file"] == "<config>":
            continue
        key = (r["kind"], r["name"])
        slot = by_name.setdefault(key, [])
        slot.append(dict(r))

    drift: list = []
    for (kind, name), rows in by_name.items():
        if len(rows) < 2:
            continue
        # For env / flag: drift = same name, different role-value combos.
        # Heuristic: extract the value-shaped suffix from `context` after `=`.
        values = set()
        for r in rows:
            ctx = r.get("context") or ""
            if "=" in ctx:
                values.add(ctx.rsplit("=", 1)[1].strip())
        if len(values) > 1:
            drift.append({
                "kind": kind, "name": name,
                "distinct_values": sorted(values)[: 8],
                "sites": [{"file": r["file"], "line": r["line"],
                           "role": r["role"], "context": r["context"]}
                          for r in rows][: 12],
                "site_count": len(rows),
                "confidence": "medium",
            })
    # Filter
    if args.kind:
        drift = [d for d in drift if d["kind"] == args.kind]
    drift.sort(key=lambda d: (-len(d["distinct_values"]), -d["site_count"]))
    # Round-X: scope-boundary labeling. Drift rows that include any vendor
    # site are tagged in_scope=False; --scope-only filters them out.
    from .scope import get_vendor_prefixes
    vendor_prefixes = get_vendor_prefixes(store)
    _annotate_scope(drift, vendor_prefixes, path_keys=("sites",))
    if args.scope_only:
        drift = [d for d in drift if d.get("in_scope")]
    out_count = len(drift)
    store.close()
    _emit({
        "drift": drift[: args.limit],
        "count": out_count,
        "truncated": out_count > args.limit,
        "vendor_prefixes": vendor_prefixes,
        "scope_only": args.scope_only,
        "note": "Heuristic. Detects contract names whose `context` field "
                "(text after the `=` sign) varies across sites — typically "
                "package.json scripts that reference different paths, or "
                "flags declared with different default values. Each row "
                "carries `in_scope` (False when ANY site is under a vendor "
                "prefix). `--scope-only` keeps only own-code drift; "
                "default keeps everything but tags it. False positives: "
                "contexts that legitimately vary (parse-vs-use sites).",
    }, args.json)


def cmd_events(args):
    """List every (event-name, emitters, listeners) triple in the repo.

    Captures EventEmitter (`emit` / `on` / `once`), DOM
    (`addEventListener` / `dispatchEvent(new Event(...))`), and Node/CDP-style
    string-keyed handlers. Useful for catching temporal-coupling bugs
    (e.g. listener registered AFTER the emitter fires).
    """
    cfg, store = _open_store(args.path)
    q = "SELECT name, file, line, role FROM contracts WHERE kind='event'"
    params: list = []
    if args.name:
        q += " AND name=?"; params.append(args.name)
    if args.file:
        q += " AND file LIKE ?"; params.append(f"%{args.file}%")
    q += " ORDER BY name, role, file, line"
    by_name: dict = {}
    for r in store.conn.execute(q, params):
        rec = by_name.setdefault(r["name"], {"emitters": [], "listeners": []})
        key = "listeners" if r["role"] == "listen" else "emitters"
        rec[key].append({"file": r["file"], "line": r["line"]})
    store.close()
    # Filter to only "interesting" pairs by default: names that have either
    # side on its own are candidate bugs (emit-without-listener or
    # listener-without-emitter). Names with both sides are for cross-check.
    events = []
    for name, v in sorted(by_name.items()):
        events.append({
            "name": name,
            "emitters": v["emitters"],
            "listeners": v["listeners"],
            "emit_only": len(v["emitters"]) > 0 and len(v["listeners"]) == 0,
            "listen_only": len(v["listeners"]) > 0 and len(v["emitters"]) == 0,
        })
    _emit({
        "events": events,
        "count": len(events),
        "note": "Heuristic string matching on .emit/.on/.addEventListener/"
                "dispatchEvent idioms. `listen_only` names often denote "
                "events fired by external libraries or the platform (e.g. "
                "CDP `Page.loadEventFired`). `emit_only` names are stronger "
                "signals of a missed listener.",
    }, args.json)


def cmd_orphans(args):
    """Symbols defined but referenced nowhere in the indexed repo.

    **Noise discipline**: defaults to STRUCTURAL kinds only (function/class/
    method/exported/var/interface/type/enum/struct/trait/module/object). This
    is the fix for the "orphans listed 500 comment words" failure mode.
    Override with `--kind function,method` or widen with `--all-kinds`.
    """
    cfg, store = _open_store(args.path)
    parser_dist = {r[0]: r[1] for r in store.conn.execute(
        "SELECT parser, COUNT(*) FROM files GROUP BY parser")}
    has_regex = parser_dist.get("regex", 0) > 0

    q = ("SELECT s.name, s.file, s.kind, s.line, s.confidence "
         "FROM symbols s "
         "LEFT JOIN refs r ON r.name = s.name "
         "WHERE r.name IS NULL")
    if args.exported_only:
        q += " AND s.exported = 1"
    if not args.all_kinds:
        q += _kind_filter_clause(args, "s.kind")
    q += " ORDER BY s.file, s.line"
    rows = [dict(r) for r in store.conn.execute(q)]
    store.close()
    out = {
        "orphans": rows[: args.limit],
        "count": len(rows),
        "truncated": len(rows) > args.limit,
        "parser_distribution": parser_dist,
        "note": "Defined symbols with zero `refs` rows. Filtered to structural "
                "kinds by default. Heuristic — may be public API, dynamically "
                "called (new X() across require boundaries may miss), or "
                "referenced only from strings. Cross-check before deleting.",
    }
    if has_regex:
        out["lower_bound_warning"] = (
            f"{parser_dist['regex']} file(s) indexed via regex. Refs are a "
            "LOWER BOUND — some 'orphans' may actually be referenced but the "
            "parser missed it. Install `.[treesitter]` for AST-grounded refs."
        )
    _emit(out, args.json)


def cmd_parity(args):
    """Symbol-parity check: referenced-but-undefined (potential typos or
    missing imports) and defined-but-unreferenced (orphans).

    Defaults to STRUCTURAL kinds on the `defined_but_unreferenced` side.
    `referenced_but_undefined` is unfiltered — builtins and stdlib names
    are expected noise there; the value is finding repo-local typos.
    """
    cfg, store = _open_store(args.path)
    parser_dist = {r[0]: r[1] for r in store.conn.execute(
        "SELECT parser, COUNT(*) FROM files GROUP BY parser")}
    has_regex = parser_dist.get("regex", 0) > 0

    # Optional --lang filter: drops noise from cross-language repos
    # (e.g. Makefile keywords or license-text false positives on Node).
    lang_filter: set | None = None
    if getattr(args, "lang", None):
        lang_filter = {x.strip() for x in args.lang.split(",") if x.strip()}

    # Audit fix: by default EXCLUDE refs from non-code files. The regex
    # fallback over markdown / AGENTS.md / .cursorrules emits call-kind
    # refs for English words like `use`, `handler`, `route` which then
    # flood the referenced_but_undefined list. Opt in with
    # --include-non-code when a caller genuinely wants the raw view.
    include_non_code = bool(getattr(args, "include_non_code", False))

    if lang_filter:
        placeholders = ",".join(["?"] * len(lang_filter))
        dangling_raw = [dict(r) for r in store.conn.execute(
            f"SELECT r.name, r.file, r.line, r.confidence, r.kind FROM refs r "
            f"JOIN files f ON f.path = r.file "
            f"LEFT JOIN symbols s ON s.name = r.name "
            f"WHERE s.name IS NULL AND f.lang IN ({placeholders}) "
            f"ORDER BY r.file, r.line",
            tuple(lang_filter))]
    elif not include_non_code:
        dangling_raw = [dict(r) for r in store.conn.execute(
            "SELECT r.name, r.file, r.line, r.confidence, r.kind FROM refs r "
            "JOIN files f ON f.path = r.file "
            "LEFT JOIN symbols s ON s.name = r.name "
            "WHERE s.name IS NULL AND f.lang != 'other' "
            "ORDER BY r.file, r.line")]
    else:
        dangling_raw = [dict(r) for r in store.conn.execute(
            "SELECT r.name, r.file, r.line, r.confidence, r.kind FROM refs r "
            "LEFT JOIN symbols s ON s.name = r.name "
            "WHERE s.name IS NULL ORDER BY r.file, r.line")]
    # P1 fix: filter language builtins by default. Agents were seeing
    # `String`, `substring`, `on`, `console` as "unresolved" — pure noise.
    if not args.include_builtins:
        dangling = [d for d in dangling_raw
                    if not _is_language_builtin(d["name"], d["file"], store)]
        builtins_filtered = len(dangling_raw) - len(dangling)
    else:
        dangling = dangling_raw
        builtins_filtered = 0
    dangling = dangling[: args.limit]
    orph_q = ("SELECT s.name, s.file, s.kind, s.line FROM symbols s "
              "LEFT JOIN refs r ON r.name = s.name "
              "WHERE r.name IS NULL")
    if not args.all_kinds:
        orph_q += _kind_filter_clause(args, "s.kind")
    orph_q += " ORDER BY s.file, s.line LIMIT ?"
    orphans = [dict(r) for r in store.conn.execute(orph_q, (args.limit,))]
    store.close()
    out = {
        "referenced_but_undefined": dangling,
        "defined_but_unreferenced": orphans,
        "builtins_filtered": builtins_filtered,
        "parser_distribution": parser_dist,
        "note": "Heuristic. Language builtins (String, console, length, "
                "print, etc.) are filtered by default — use "
                "`--include-builtins` to see the raw unresolved set. "
                "Remaining `referenced_but_undefined` entries are candidates "
                "for typo / missing-import. External package imports also "
                "appear here because they're not indexed as defs.",
    }
    if has_regex:
        out["lower_bound_warning"] = (
            f"{parser_dist['regex']} file(s) indexed via regex. Counts on "
            "both sides are imprecise; install `.[treesitter]` for AST."
        )
    _emit(out, args.json)


def cmd_scope(args):
    """Print the EXACT effective scope of the last `projmem index` run —
    CLI globs, config globs, builtin skips, and exclude_wins mode. Round-X
    feedback: `projmem files` only showed config-level scope, so users
    couldn't reproduce what their last index actually saw."""
    cfg, store = _open_store(args.path)
    raw = store.get_meta("last_index_session")
    if raw:
        import json as _json
        sess = _json.loads(raw)
    else:
        sess = {"warning": "No `last_index_session` recorded. Re-run "
                           "`projmem index` to populate."}
    from .discovery import SKIP_DIRS
    out = {
        "root": cfg.root,
        "indexed_root": store.get_meta("root"),
        "last_index_session": sess,
        "builtin_skip_dirs": sorted(SKIP_DIRS),
        "current_config_include_globs": cfg.include_globs,
        "current_config_exclude_globs": cfg.exclude_globs,
        "note": "`last_index_session.*_effective` is the merged set used at "
                "index time. CLI flags + config are reported separately so "
                "you can audit which side contributed each glob.",
    }
    store.close()
    _emit(out, args.json)


def cmd_missing_paths(args):
    """Repo-wide scan for referenced files that don't exist on disk.

    Sources scanned:
      - `package.json`: `main`, `module`, `bin.*`, `scripts.*` (best-effort
        token-by-token: any token that looks like a relative path is checked).
      - JS/TS relative imports/requires (already in `edges` as `module:./X`).
      - Shell scripts: `node ./path/to/file` invocations (regex).

    Output: `{file, kind, ref, missing}` rows. Round-X feedback: this is the
    cheap run-blocker check users do manually with `ls + cat package.json +
    grep`. Surfacing it as one command catches the bug class fast.
    """
    import os as _os
    import re as _re
    cfg, store = _open_store(args.path)
    missing: list = []

    # 1. package.json — main / module / bin / scripts
    for r in store.conn.execute("SELECT path FROM files WHERE path LIKE '%package.json'"):
        rel = r["path"]
        full = _os.path.join(cfg.root, rel)
        try:
            data = json.loads(open(full).read())
        except Exception:
            continue

        def _check(refstr: str, kind: str) -> None:
            if not isinstance(refstr, str):
                return
            base = _os.path.dirname(rel)
            cand = _os.path.normpath(_os.path.join(base, refstr))
            if not _os.path.isfile(_os.path.join(cfg.root, cand)):
                missing.append({"file": rel, "kind": kind, "ref": refstr,
                                "expected_path": cand, "missing": True})

        for k in ("main", "module"):
            v = data.get(k)
            if isinstance(v, str): _check(v, f"package.json:{k}")
        bins = data.get("bin")
        if isinstance(bins, str): _check(bins, "package.json:bin")
        if isinstance(bins, dict):
            for bn, bv in bins.items():
                if isinstance(bv, str): _check(bv, f"package.json:bin:{bn}")
        # scripts: best-effort — scan for tokens that look like relative paths
        for sn, sv in (data.get("scripts") or {}).items():
            if not isinstance(sv, str): continue
            for tok in _re.findall(r"[\w/.\-]+\.(?:js|mjs|cjs|ts|tsx|py|sh)", sv):
                if tok.startswith(("/",)) or "://" in tok:
                    continue
                _check(tok, f"package.json:scripts:{sn}")

    # 2. Unresolved RELATIVE imports. Self-test D4 surfaced two bugs here:
    #
    #   (a) Only JS/TS extensions were probed, so every Python `from .x
    #       import Y` composite (`.x.Y`) was flagged missing.
    #   (b) The `py_from_stmt` path in ts_backend emits composite specs
    #       `<mod>.<name>` alongside the bare `<mod>`. If `<mod>` alone
    #       has a RESOLVED edge (to a real file), the composite is not
    #       actually missing — it's just a symbol within that module.
    #
    # Fix (a): probe Python extensions too.
    # Fix (b): before flagging a composite as missing, check whether any
    #          strict prefix of the dotted path already resolves to a
    #          real file in the store.

    # Build a quick set of `<src>|<resolved-dst>` entries so we can check
    # if a sibling edge resolved. Indexed by source file.
    resolved_by_src: dict = {}
    for r in store.conn.execute(
            "SELECT src, dst FROM edges WHERE type='imports' "
            "AND dst NOT LIKE 'module:%' AND dst NOT LIKE 'builtin:%'"):
        resolved_by_src.setdefault(r["src"], set()).add(r["dst"])

    def _py_prefix_resolves(src_file: str, spec: str) -> bool:
        """If spec is a Python dotted path, check if any strict PREFIX
        already resolved for this source. E.g., spec='.store.Store' and
        an edge `src → projmem/store.py` exists → not missing."""
        if not spec.startswith("."):
            return False
        resolved_dsts = resolved_by_src.get(src_file, set())
        # Try each progressively-shorter dotted prefix.
        parts = spec.lstrip(".").split(".")
        prefix_dots = spec[: len(spec) - len(spec.lstrip("."))]
        for i in range(len(parts) - 1, 0, -1):
            prefix_spec = prefix_dots + ".".join(parts[:i])
            base = _os.path.dirname(src_file)
            stripped = prefix_spec.lstrip(".")
            ups = len(prefix_spec) - len(stripped)
            rel_guess_parts = [base] + [".."] * (ups - 1)
            if stripped:
                rel_guess_parts += stripped.split(".")
            rel_guess = _os.path.normpath(_os.path.join(*rel_guess_parts))
            for ext in (".py", "/__init__.py"):
                cand = rel_guess + ext
                if cand in resolved_dsts:
                    return True
                if _os.path.isfile(_os.path.join(cfg.root, cand)):
                    return True
        return False

    for r in store.conn.execute(
            "SELECT src, dst FROM edges WHERE type='imports' "
            "AND dst LIKE 'module:.%'"):
        spec = r["dst"][len("module:"):]
        src_file = r["src"]
        # Fix (b): if a shorter dotted prefix resolves in Python, skip.
        if src_file.endswith(".py") and _py_prefix_resolves(src_file, spec):
            continue
        # Fix (a): try Python extensions too.
        base = _os.path.dirname(src_file)
        python_mode = src_file.endswith(".py")
        exts = (".py", "/__init__.py") if python_mode else (
            "", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
        if python_mode:
            # Python dotted form to filesystem path:
            stripped = spec.lstrip(".")
            ups = len(spec) - len(stripped)
            parts = [base] + [".."] * (ups - 1)
            if stripped:
                parts += stripped.split(".")
            probe_base = _os.path.normpath(_os.path.join(*parts)) if parts else ""
        else:
            probe_base = _os.path.normpath(_os.path.join(base, spec))
        kind = "python-import" if python_mode else "js-import"
        for ext in exts:
            cand = probe_base + ext
            if _os.path.isfile(_os.path.join(cfg.root, cand)):
                break
        else:
            missing.append({"file": src_file, "kind": kind,
                            "ref": spec, "expected_path": None,
                            "missing": True})

    # 3. Shell scripts — `node path/to/x` invocations
    sh_node_rx = _re.compile(r"\bnode\s+([\w/.\-]+\.(?:js|mjs|cjs|ts))")
    for r in store.conn.execute(
            "SELECT path FROM files WHERE lang='bash' OR path LIKE '%.sh'"):
        rel = r["path"]
        try:
            src = open(_os.path.join(cfg.root, rel)).read()
        except Exception:
            continue
        for m in sh_node_rx.finditer(src):
            spec = m.group(1)
            if spec.startswith(("/",)) or "://" in spec:
                continue
            base = _os.path.dirname(rel)
            cand = _os.path.normpath(_os.path.join(base, spec))
            if not _os.path.isfile(_os.path.join(cfg.root, cand)):
                missing.append({"file": rel, "kind": "shell-node-invoke",
                                "ref": spec, "expected_path": cand,
                                "missing": True})

    # Round-X: scope-boundary labeling. A missing-path entry is in_scope
    # only when the SOURCE file (where the bad reference lives) is in
    # scope. Vendor packages with broken refs to themselves are noise.
    from .scope import get_vendor_prefixes
    vendor_prefixes = get_vendor_prefixes(store)
    _annotate_scope(missing, vendor_prefixes, path_keys=("file",))
    if args.scope_only:
        missing = [m for m in missing if m.get("in_scope")]
    store.close()
    _emit({
        "missing_paths": missing[: args.limit],
        "count": len(missing),
        "truncated": len(missing) > args.limit,
        "vendor_prefixes": vendor_prefixes,
        "scope_only": args.scope_only,
        "scanned": ["package.json (main/module/bin/scripts)",
                    "JS/TS relative imports", "shell `node X` invocations"],
        "note": "Heuristic. Each row carries `in_scope` (False when the "
                "SOURCE file lives under a vendor prefix). Pass "
                "`--scope-only` to suppress vendor-self-references "
                "(round-X feedback: chrome/ alone produced 148 vendor "
                "fixture rows). False positives possible for scripts "
                "where the argument is generated/templated.",
    }, args.json)


def cmd_files(args):
    """Emit the indexed file list + effective include/exclude patterns so
    users can reproduce/audit the scope. Round-3 report #12."""
    import fnmatch as _fn
    cfg, store = _open_store(args.path)
    files = [{"path": r["path"], "lang": r["lang"], "parser": r["parser"],
              "size": r["size"], "stale": bool(r["stale"])}
             for r in store.all_files()]
    # Discovery context — so bug reports can reconstruct what was excluded.
    from .discovery import SKIP_DIRS
    store.close()
    full_count = len(files)
    # Glob filter (fnmatch on the relative path). Same syntax callers
    # already know from `--include` / `--exclude`. Cheap and avoids
    # forcing them to pipe through `jq` to narrow the dump.
    glob_pat = getattr(args, "glob", None)
    if glob_pat:
        files = [f for f in files if _fn.fnmatch(f["path"], glob_pat)]
    # Lang filter (CSV, e.g. --lang java,python). Round-5-r3 F004:
    # short aliases (`js`, `ts`, `py`, etc.) are normalized to the
    # canonical names stored on file rows so `--lang js` no longer
    # silently returns count:0.
    lang_filter = getattr(args, "lang", None)
    if lang_filter:
        wanted = {x.strip() for x in lang_filter.split(",") if x.strip()}
        wanted = {_LANG_ALIASES.get(x, x) for x in wanted}
        files = [f for f in files if f.get("lang") in wanted]
    sorted_files = sorted(files, key=lambda x: x["path"])
    matched = len(sorted_files)
    # Limit AFTER glob/lang filters so caps reflect the visible subset,
    # not the raw 4639-file dump that triggered the original UX cost.
    limit = int(getattr(args, "limit", 0) or 0)
    truncated = False
    if limit > 0 and matched > limit:
        sorted_files = sorted_files[:limit]
        truncated = True
    out = {
        "root": cfg.root,
        "files": sorted_files,
        "count": len(sorted_files),
        "matched": matched,
        "total_indexed": full_count,
        "truncated": truncated,
        "filters": {
            "glob": glob_pat,
            "lang": lang_filter,
            "limit": limit or None,
        },
        "config_include_globs": cfg.include_globs,
        "config_exclude_globs": cfg.exclude_globs,
        "builtin_skip_dirs": sorted(SKIP_DIRS),
        "note": "Effective scope. CLI --include/--exclude passed at index "
                "time are merged on top of `config_*` (this command only "
                "shows the config-level state). Default returns ALL "
                "indexed files; use --glob / --lang / --limit to narrow.",
    }
    _emit(out, args.json)


def _classify_import_spec(src_file: str, spec: str) -> str:
    """Round-X report item #6: classify an unresolved import spec by language
    convention so consumers can distinguish "external system header"
    (expected) from "broken repo-relative import" (actionable bug).

    Audit fix: previously ANY spec containing `/` was `repo_relative`,
    which wrongly flagged scoped npm packages (`@prisma/client`),
    built-in subpath exports (`next/server`, `react/jsx-runtime`), and
    Deno/Node URI specifiers (`node:fs/promises`) as actionable broken
    paths. A bare `utils/auth`-style repo import without a leading `./`
    is invalid ES module syntax anyway — TS/JS compilers reject it —
    so requiring an explicit `./` / `../` / `/` prefix for
    `repo_relative` loses nothing real and kills the false-positive
    flood.
    """
    file_lang = (src_file.rsplit(".", 1)[-1] if "." in src_file else "").lower()
    s = spec.strip()
    bracketed = s.startswith("<") and s.endswith(">")
    bare = s.strip("<>\"'")
    if bracketed:
        return "external_include"
    if file_lang in ("c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx"):
        if not bare.startswith("."):
            return "external_include"
    # GN imports — `//foo/bar.gni`
    if bare.startswith("//"):
        return "external_root_import"
    # URI-scheme specifiers (`node:fs`, `npm:zod`, `jsr:@std/http`,
    # `https://deno.land/x/...`) are always external.
    if ":" in bare and not bare.startswith((".", "/")):
        first_colon = bare.index(":")
        scheme = bare[:first_colon]
        if scheme and scheme.replace("+", "").isalnum() and "/" not in scheme:
            return "external_module"
    # Repo-relative: ONLY explicit `./` `../` `/` prefixes.
    # Everything else (bare names, scoped packages, subpath exports) is
    # external. A file path like `utils/auth` without a leading `./` is
    # not legal ES module syntax and we never flag it as actionable.
    if bare.startswith(("./", "../", "/")):
        return "repo_relative"
    # Single-segment dot form (`.`), per-package index shorthand — also
    # repo-relative in practice.
    if bare in (".", ".."):
        return "repo_relative"
    return "external_module"


def cmd_unresolved_imports(args):
    """Repo-wide list of imports that couldn't be resolved to a file.
    Round-3 report #7. Round-X: each row carries an `import_kind` tag —
    `external_include` (C/C++ system header), `external_root_import` (GN
    `//path`), `external_module` (npm/pip-style bare name), or
    `repo_relative` (the only truly-actionable category by default).
    """
    cfg, store = _open_store(args.path)
    rows = [dict(r) for r in store.conn.execute(
        "SELECT src, dst, evidence, confidence FROM edges "
        "WHERE type='imports' AND dst LIKE 'module:%' "
        "ORDER BY dst, src")]
    import os as _os
    seen = set()
    out_rows = []
    for r in rows:
        key = (r["src"], r["dst"])
        if key in seen: continue
        seen.add(key)
        spec = r["dst"][len("module:"):]
        # Match `_classify_import_spec`: only explicit `./`, `../`, `/`,
        # or `.`/`..` count as repo-relative. `@scope/pkg` and
        # `next/server` are NOT path-shaped despite containing `/`.
        looks_like_path = (spec.startswith(("./", "../", "/"))
                           or spec in (".", ".."))
        on_disk = False
        if looks_like_path:
            for ext in ("", ".js", ".ts", ".py", ".tsx", ".jsx", ".mjs",
                        ".cjs", ".h", ".hpp", ".cc", ".cpp"):
                if _os.path.isfile(_os.path.join(cfg.root, spec + ext)):
                    on_disk = True; break
        kind = _classify_import_spec(r["src"], spec)
        out_rows.append({
            **r, "target_spec": spec,
            "import_kind": kind,
            "looks_like_relative_path": looks_like_path,
            "exists_on_disk_but_not_indexed": on_disk,
        })
    # Filtering
    if args.only_missing_on_disk:
        out_rows = [r for r in out_rows
                    if r["looks_like_relative_path"]
                    and not r["exists_on_disk_but_not_indexed"]]
    if args.kind:
        wanted = {k.strip() for k in args.kind.split(",")}
        out_rows = [r for r in out_rows if r["import_kind"] in wanted]
    if not args.show_external and not args.kind:
        # Default: hide external_include + external_root_import (vendor noise).
        # Keep external_module since unresolved npm packages can still be a
        # genuine "missing dep" signal in some workflows.
        out_rows = [r for r in out_rows
                    if r["import_kind"] != "external_include"
                    and r["import_kind"] != "external_root_import"]
    # Round-X: also tag in_scope per row (the `src` is the source file).
    from .scope import get_vendor_prefixes
    vendor_prefixes = get_vendor_prefixes(store)
    _annotate_scope(out_rows, vendor_prefixes, path_keys=("src",))
    if args.scope_only:
        out_rows = [r for r in out_rows if r.get("in_scope")]

    # Stats: counts per kind BEFORE filtering for the consumer's audit.
    kind_counts = {}
    for r in rows:
        spec = r["dst"][len("module:"):]
        k = _classify_import_spec(r["src"], spec)
        kind_counts[k] = kind_counts.get(k, 0) + 1

    store.close()
    _emit({
        "unresolved_imports": out_rows[: args.limit],
        "count": len(out_rows),
        "total_unresolved": sum(kind_counts.values()),
        "by_kind": kind_counts,
        "truncated": len(out_rows) > args.limit,
        "note": "`import_kind` classifies the spec by language convention. "
                "`external_include` (C/C++ system headers) and "
                "`external_root_import` (GN `//...`) are HIDDEN by default — "
                "they're vendor/system noise. Pass `--show-external` to "
                "include them, or `--kind repo_relative` to focus on "
                "actionable broken paths. `exists_on_disk_but_not_indexed` "
                "flags scope-misconfiguration cases.",
    }, args.json)


def cmd_snapshot(args):
    """Freeze current contracts (and optionally symbols) under a label.
    Drives `contract-diff` and `symbol-diff`."""
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    if args.delete:
        n = store.delete_snapshot(args.delete)
        store.close()
        _emit({"deleted_label": args.delete, "rows_removed": n}, args.json)
        return
    if args.list:
        out = {
            "contract_snapshots": store.list_snapshots(),
            "symbol_snapshots": store.list_symbol_snapshots(),
        }
        store.close()
        _emit(out, args.json)
        return
    # F005: empty / whitespace label used to silently get rebranded to
    # "manual" — a typo or `--label ""` survived the round-trip and the
    # caller had no idea what label the snapshot lived under. Reject
    # explicit empty values; only the unset case (None) implies the
    # default. A label that's a literal hyphen or other unsafe form
    # (used in CLI flag space) also gets rejected.
    label = args.label
    if label is not None and not str(label).strip():
        _emit_error(
            {"error":   "empty-snapshot-label",
             "message": "snapshot label was empty or whitespace-only",
             "hint": ("Pass a non-empty `--label NAME`, or omit "
                       "`--label` entirely to use the default 'manual'.")},
            args.json, store=store)
    if label is None:
        label = "manual"
    contract_count = store.snapshot_contracts(label)
    symbol_count = 0
    if args.symbols:
        symbol_count = store.snapshot_symbols(label)
    store.close()
    msg = {
        "label": label,
        "contract_rows": contract_count,
        "symbol_rows": symbol_count if args.symbols else None,
        "note": (f"Contracts frozen under '{label}'. Compare with "
                 f"`projmem contract-diff --base {label}`."
                 + (f" Symbols also frozen — `projmem symbol-diff "
                    f"--base {label}`." if args.symbols else
                    " Use `--symbols` to also freeze the symbol table.")),
    }
    _emit(msg, args.json)


def cmd_contract_diff_vs_base(args):
    """Diff a contract snapshot against current (or another snapshot).

    Default: `--base pre-index` (auto-taken on every `projmem index`) vs
    live contracts. Each added contract is annotated with consumer
    analysis; each removed contract with dangling-ref analysis.
    """
    from . import contract_diff as _cd
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    # Distinguish "snapshot has zero rows" (valid — the baseline repo had
    # no contracts) from "snapshot label does not exist" (user error).
    # The label-existence check goes through list_snapshots, which merges
    # the row-based and meta-tracked views.
    known_labels = {s["label"] for s in store.list_snapshots()}
    if args.base not in known_labels:
        _emit_error({
            "error":   "snapshot-not-found",
            "message": f"snapshot {args.base!r} not found",
            "available_options": sorted(known_labels),
            "hint": "Run `projmem snapshot <name>` first, or use "
                    "`projmem contract-diff --base pre-index` after "
                    "`projmem index`.",
        }, args.json, store=store)
    base_rows = store.snapshot_rows(args.base)
    if args.head == "current":
        head_rows = store.live_contract_rows()
    else:
        if args.head not in known_labels:
            _emit_error(
                {"error":   "snapshot-not-found",
                 "message": f"snapshot {args.head!r} not found",
                 "available_options": sorted(known_labels),
                 "hint": ("Pass a name from `available_options`, or "
                           "use `current` to compare live contracts.")},
                args.json, store=store)
        head_rows = store.snapshot_rows(args.head)
    kinds = None
    if args.kind:
        kinds = [k.strip() for k in args.kind.split(",") if k.strip()]
    diff = _cd.compute_diff(store, base_rows, head_rows, kinds=kinds)
    if args.as_obligations:
        out = _cd.project_obligations(diff)
    else:
        out = {"base": args.base, "head": args.head, **diff}
    store.close()
    _emit(out, args.json)


def cmd_trace(args):
    """EXPERIMENTAL — call-chain BFS over the name-level refs graph.

    Audit P1#6 (de-scope): same-name collisions are over-approximated.
    Two functions named `handle()` in different files appear as one
    logical node, so the path may "cross" between unrelated symbols.
    The output now carries `experimental: true` and a `caveat` string
    so agents don't treat results as ground truth.

    For trustworthy reverse-by-symbol use:
      - `projmem reverse <file>`        — file-level ground truth
      - `projmem analyze-change <target>` — pre-edit blast radius
      - `projmem pack <file#symbol>`     — bounded context with confidence

    Strict mode (default) traverses only `call` edges; relaxed adds
    `new` and `callback`. Neither crosses import / read / shorthand
    edges.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    # Refuse without an explicit opt-in. The implementation is name-
    # level (collisions merged) and the output buries that caveat at
    # the bottom of a 100-entry too_ambiguous_names dump — agents read
    # the path as authoritative and act on noise. Force the flag so
    # consumers acknowledge the constraint before the output reaches
    # them. `analyze-change` / `reverse` / `pack` are the trustworthy
    # alternatives surfaced here.
    if not getattr(args, "experimental", False):
        # F011: standardized envelope shape (kebab tag in `error`,
        # sentence in `message`, hint last). Was using `reason` +
        # `alternatives` — close but inconsistent with every other
        # error path. Now matches the contract.
        _emit_error({
            "error":   "trace-requires-experimental-opt-in",
            "message": ("`trace` BFS is name-level; same-name "
                          "collisions across files are merged into "
                          "one logical node, so the returned path "
                          "may pass through unrelated symbols. "
                          "Output looks authoritative but isn't."),
            "source":  args.source,
            "sink":    args.sink,
            "alternatives": [
                f"projmem analyze-change {args.sink}",
                f"projmem reverse <file_containing_{args.sink}>",
                f"projmem pack <file>#{args.sink}",
            ],
            "hint": ("Pass `--experimental` to acknowledge the "
                      "name-collision risk and run anyway. For "
                      "trustworthy reverse-by-symbol use "
                      "`analyze-change` / `reverse` / `pack`."),
        }, args.json, store=store)
    res = graph.trace_call_chain(store, args.source, args.sink,
                                  max_hops=args.max_hops, via=args.via,
                                  mode=args.mode)
    out = {
        "source": args.source, "sink": args.sink,
        "experimental": True,
        "caveat": (
            "trace BFS is name-level; same-name collisions across files "
            "are merged into one logical node. The path may pass through "
            "unrelated symbols sharing the name. For reliable reverse-by-"
            "symbol use `reverse` / `analyze-change` / `pack`."
        ),
        **res,
    }
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_audit_trail(args):
    """Show the recent CLI command history scoped to this index.

    Use case: "what has the prior agent already examined here?"
    Auto-populated by every projmem command (except index/stats/
    snapshot/audit-trail to avoid recursion). Filterable by target
    file (or `file#symbol` shorthand) and by command name.
    """
    cfg, store = _open_store(args.path)
    rows = store.audit_trail(target=args.target, command=args.command,
                              limit=args.limit)
    store.close()
    import datetime as _dt
    for r in rows:
        if r.get("ts"):
            r["ts_iso"] = _dt.datetime.fromtimestamp(r["ts"]).isoformat()
    _emit({"audit_trail": rows, "count": len(rows),
           "filter": {"target": args.target, "command": args.command,
                      "limit": args.limit}}, args.json)


_KNOWN_NOTE_KINDS = {
    "note", "refute", "verified-safe", "documented-footgun",
    "todo", "link", "risk",
}


def cmd_note(args):
    """Annotate a target (file or symbol) with a human/agent assertion.

    Annotations live alongside the index and survive reindex; they show
    up in `projmem pack <target>` output at the top so a future agent
    sees the prior verdict before re-investigating.

    Subactions:
      add     — create a new annotation
      list    — show annotations (optionally filtered)
      delete  — remove an annotation by id
      search  — full-text search across body/target/kind
    """
    import time as _time
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    sub = args.note_action
    if sub == "add":
        # Hard reject empty / whitespace-only body. A memory tool that
        # accepts empty notes is a pollution vector — every later
        # `notes` / `session` query has to scroll past it. Rejecting at
        # write time costs the caller one informative error and saves
        # all future queries.
        if not (args.body or "").strip():
            _emit_error(
                {"error":   "empty-body",
                 "message": "note body is empty after trimming",
                 "hint": ("`note add` requires a non-empty body. "
                           "Use `--evidence file:line` and inline "
                           "`@predicate(subj, obj)` claims when you "
                           "want structured signal without prose.")},
                args.json, store=store)
        if args.kind not in _KNOWN_NOTE_KINDS:
            sys.stderr.write(
                f"projmem: warning: unknown kind '{args.kind}'. "
                f"Recommended: {sorted(_KNOWN_NOTE_KINDS)}\n")
        expires_at = None
        if args.expires_days is not None:
            expires_at = _time.time() + args.expires_days * 86400.0
        # Structured claim fields (SPEC #3 / #14 / #15). When the
        # caller provides evidence or a fingerprint, we capture a
        # fingerprint from the current code state so the note starts
        # life as ``fresh`` and can be revalidated later.
        evidence_list: Optional[list] = None
        if getattr(args, "evidence", None):
            evidence_list = []
            # F006: validate every --evidence file path is INSIDE the
            # repo root. Previously a `../../../etc/passwd:1` survived
            # round-trip and was stored verbatim — turning a memory
            # tool into a data-collection vector for whatever consumer
            # later inspected the note. Enforcement: realpath join,
            # ensure result lives under realpath(cfg.root).
            real_root = os.path.realpath(cfg.root)
            for ev in args.evidence:
                # Format: "file:line" or "file:line:note"
                parts = ev.split(":", 2)
                file_part = parts[0]
                # Reject obvious traversal / absolute escapes upfront
                # — even when the path doesn't exist on disk yet, the
                # SHAPE is unsafe to record.
                rejected_for: Optional[str] = None
                if (file_part.startswith("/")
                        and not file_part.startswith(real_root + "/")
                        and file_part != real_root):
                    rejected_for = "absolute-outside-repo"
                else:
                    candidate = (file_part if os.path.isabs(file_part)
                                  else os.path.join(real_root, file_part))
                    real_cand = os.path.realpath(candidate)
                    if (real_cand != real_root
                            and not real_cand.startswith(real_root + os.sep)):
                        rejected_for = "path-traversal"
                if rejected_for is not None:
                    _emit_error(
                        {"error":   "evidence-out-of-repo",
                         "message": (f"evidence path {file_part!r} "
                                      "escapes the repo root"),
                         "given":   ev,
                         "repo_root": real_root,
                         "reason":   rejected_for,
                         "hint": ("Cite a path RELATIVE to the repo "
                                   "root (e.g. `src/foo.ts:10`). "
                                   "Absolute paths or `..` segments "
                                   "that escape the root are rejected "
                                   "to prevent path-traversal storage.")},
                        args.json, store=store)
                rec: Dict[str, Any] = {"file": file_part}
                if len(parts) >= 2:
                    try:
                        rec["line"] = int(parts[1])
                    except ValueError:
                        rec["note"] = parts[1]
                if len(parts) == 3:
                    rec["note"] = parts[2]
                evidence_list.append(rec)
        # --claims: structured claims loaded from a JSON file or inline
        # JSON string. Each claim is merged into the evidence list; claim
        # verification happens at note-verify time, not here.
        if getattr(args, "claims", None):
            raw = args.claims
            claims_payload: Optional[list] = None
            # Path first, string fallback.
            if os.path.isfile(raw):
                try:
                    with open(raw, "r", encoding="utf-8") as f:
                        claims_payload = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    _emit_error(
                        {"error":   "invalid-claims-file",
                         "message": str(e),
                         "path":    raw,
                         "hint": ("Pass a path to a JSON file or a "
                                   "JSON string literal. The file "
                                   "must contain a JSON array of "
                                   "claim dicts.")},
                        args.json, store=store)
            else:
                try:
                    claims_payload = json.loads(raw)
                except json.JSONDecodeError as e:
                    _emit_error(
                        {"error":   "invalid-claims-json",
                         "message": str(e),
                         "hint": ("--claims expects a JSON array. "
                                   "Example: '[{\"subject\":\"foo\","
                                   "\"predicate\":\"defined-at\","
                                   "\"object\":\"src/a.ts:10\"}]'.")},
                        args.json, store=store)
            if not isinstance(claims_payload, list):
                _emit_error(
                    {"error":   "claims-not-array",
                     "message": "claims payload was not a JSON array",
                     "hint": ("Wrap claims in `[...]`. Each claim "
                               "needs subject, predicate, object.")},
                    args.json, store=store)
            # Validate: every claim must have subject/predicate/object.
            from . import claims as _claims
            bad = [c for c in claims_payload
                   if not (isinstance(c, dict) and _claims.is_claim(c))]
            if bad:
                _emit_error(
                    {"error":   "claim-missing-keys",
                     "message": ("one or more claims missing required "
                                   "keys (subject, predicate, object)"),
                     "invalid_claims": bad,
                     "hint": ("Each claim is a dict with subject, "
                               "predicate, object. Use `projmem note "
                               "predicates` for the catalog.")},
                    args.json, store=store)
            unknown_preds = sorted({c["predicate"] for c in claims_payload
                                     if c["predicate"]
                                     not in _claims.PREDICATES})
            if unknown_preds and not getattr(args, "allow_unknown_predicates",
                                              False):
                # REJECT by default. Previously: warned + accepted →
                # silent memory rot, claims that can never be verified
                # or refuted accumulate forever. Now: refuse the write
                # and tell the caller exactly what's supported (the
                # PREDICATES catalog is also exposed via
                # `projmem note predicates`).
                store.close()
                _emit({
                    "error":             "unknown-predicate",
                    "unknown_predicates": unknown_preds,
                    "known_predicates":   sorted(_claims.PREDICATES),
                    "hint": ("Use one of the known predicates, OR pass "
                              "`--allow-unknown-predicates` to store the "
                              "claim anyway (it will be UNCHECKABLE on "
                              "every revalidate). Discover the catalog "
                              "with `projmem note predicates`."),
                }, args.json)
                sys.exit(2)
            elif unknown_preds:
                sys.stderr.write(
                    f"projmem: storing claim with unknown predicate(s) "
                    f"{unknown_preds} — they will revalidate as "
                    f"UNCHECKABLE forever (you opted in via "
                    f"`--allow-unknown-predicates`).\n")
            # Propagate the note-level --truth-class to claims that don't
            # set their own. Without this, a `--truth-class FACT` note
            # whose claims omit truth_class gets aggregated as
            # `strongly_stale` (not `contradicted`) on REFUTED, which in
            # turn means `contradicted_count` doesn't increment and the
            # CLAUDE.md blocker signal never fires. Surfaced by the
            # axios eval — Sonnet wrote `--truth-class FACT` but the
            # claims JSON had no truth_class field, so the wedge silently
            # broke. Now FACT propagates cleanly.
            note_tc = getattr(args, "truth_class", None)
            if note_tc:
                for _c in claims_payload:
                    if isinstance(_c, dict) and not _c.get("truth_class"):
                        _c["truth_class"] = note_tc
            # Round-6 PROJMEM_PARANOID gate: refuse FACT claims the
            # caller hasn't just-verified via `fact-check` / `check`.
            # No-op when the env var is unset (zero overhead on the
            # default path).
            from . import paranoid as _paranoid
            if _paranoid.is_paranoid():
                fact_claims = [c for c in claims_payload
                                if isinstance(c, dict)
                                and c.get("truth_class") == "FACT"]
                unverified = _paranoid.assert_verified_or_raise(
                    cfg.root, fact_claims)
                if unverified:
                    _emit_error(
                        {"error":   "claim-not-verified",
                         "message": ("PROJMEM_PARANOID=1 set; refusing "
                                      f"to store {len(unverified)} FACT "
                                      "claim(s) that haven't been "
                                      "fact-checked in this session"),
                         "unverified_claims": unverified[:5],
                         "hint": ("Run `projmem check '<text>'` (or "
                                   "`fact-check`) on the same triple "
                                   "first — VERIFIED / MOVED claims are "
                                   "remembered for 10 minutes per repo. "
                                   "Unset PROJMEM_PARANOID to disable.")},
                        args.json, store=store)
            if evidence_list is None:
                evidence_list = []
            evidence_list.extend(claims_payload)
        # Audit fix: inline claim parser. Any `@<predicate>(<subject>,
        # <object>)` pattern in the body becomes a structured claim.
        # Lowers capture friction — no JSON file needed for simple
        # cases. Runs ALONGSIDE --claims (merged), so explicit JSON
        # still works and nothing regresses.
        #
        # Round-7-bench: the inline parser only catches `@predicate(...)`
        # syntax. Real benchmark agents (Sonnet) write prose like
        # "setupmethod is defined at src/flask/sansio/scaffold.py:42",
        # never the `@` form. Auto-extract widens to also catch the NL
        # patterns the fact-check module recognises. Default-on so
        # naive `note add "<prose>"` calls produce structured FACT
        # claims for free; pass --no-auto-extract to keep the legacy
        # inline-only behavior.
        from . import claims as _claims_inline
        explicit_tc = getattr(args, "truth_class", None)
        if getattr(args, "no_auto_extract", False):
            body_claims = _claims_inline.parse_inline_claims(
                args.body or "",
                default_truth_class=explicit_tc or "INFERENCE")
        else:
            # Auto-extracted claims default to FACT — that's the whole
            # point. INFERENCE doesn't fire the contradicted_count
            # blocker on REFUTED; FACT does. The verifier needs FACT
            # claims to differentiate from a markdown scratchpad. The
            # user's explicit --truth-class still wins. Paranoid mode
            # interaction is intentional: PROJMEM_PARANOID will block
            # auto-extracted FACTs that weren't pre-verified, which
            # is exactly what paranoid mode is for.
            body_claims = _claims_inline.auto_extract_claims(
                args.body or "",
                default_truth_class=explicit_tc or "FACT")
        if body_claims:
            if evidence_list is None:
                evidence_list = []
            evidence_list.extend(c.to_dict() for c in body_claims)
        fp = None
        try:
            from . import integrity as _intg
            fp_dict = _intg.compute_fingerprint(
                store, cfg.root, args.target).to_dict()
            # Only treat a fingerprint as "captured" when at least one
            # component can be computed. Targets like `@project`, directory
            # prefixes (`src/auth/`), and opaque symbol_ids can produce an
            # all-None bundle — storing that as "fresh" would be false
            # certainty.
            if any(v is not None for v in fp_dict.values()):
                fp = fp_dict
        except Exception:
            fp = None
        # Off-repo / unindexed-target gate. Path-shaped targets
        # (`/abs/...`, `lib/foo.java`, `pkg/bar.py`) that don't resolve
        # to a known indexed file get a structured warning. We don't
        # hard-reject because legitimate targets exist (`@project`,
        # `pkg/` directory prefixes, claims about future files, opaque
        # symbol_ids), but the response includes `target_indexed: False`
        # so the caller can audit. `--allow-off-repo-target` skips the
        # check entirely.
        target_indexed: Optional[bool] = None
        target_warning: Optional[Dict[str, Any]] = None
        _t = (args.target or "").strip()
        _is_special = (_t.startswith("@") or _t.endswith("/")
                        or "#" in _t  # file#name form
                        or _t in (".", "./"))
        _looks_path = ("/" in _t and not _is_special)
        _is_abs = _t.startswith("/")
        if _looks_path or _is_abs:
            indexed_row = store.conn.execute(
                "SELECT 1 FROM files WHERE path=? LIMIT 1",
                (_t,)).fetchone()
            target_indexed = indexed_row is not None
            if not target_indexed and not getattr(
                    args, "allow_off_repo_target", False):
                store.close()
                _emit({
                    "error": "target-not-indexed",
                    "target": _t,
                    "is_absolute_path": _is_abs,
                    "hint": (
                        "Target doesn't match any indexed file. "
                        "Run `projmem files` to see the canonical paths, "
                        "or `projmem index` if the file was just added. "
                        "Use `--allow-off-repo-target` to store anyway "
                        "(the note will be unverifiable). For symbol-name "
                        "targets pass them bare (no `/`); for project-wide "
                        "notes use `@project`."),
                }, args.json)
                sys.exit(2)
        ann_id = store.add_annotation(
            target=args.target, kind=args.kind, body=args.body,
            author=args.author, expires_at=expires_at,
            confidence=getattr(args, "confidence", None),
            evidence=evidence_list,
            assumptions=getattr(args, "assumptions", None),
            scope=getattr(args, "scope", None),
            truth_class=getattr(args, "truth_class", None),
            fingerprint=fp)
        from . import claims as _claims
        claim_count = sum(1 for e in (evidence_list or [])
                          if _claims.is_claim(e))
        # Immediately revalidate the just-added note so its `staleness`
        # column reflects the truth of any FACT claims it carries. Without
        # this, an agent that added a REFUTED note and then queried
        # `session <other_target>` would see `repo_memory.contradicted_count
        # = 0` because the column wasn't refreshed yet — the wedge signal
        # would silently fail to fire until something else triggered a
        # revalidate sweep (notes / session-no-target). Cheap: one note,
        # one fingerprint compute.
        new_staleness = None
        if claim_count > 0:
            try:
                from . import integrity as _intg
                row = store.conn.execute(
                    "SELECT * FROM annotations WHERE id=?",
                    (ann_id,)).fetchone()
                if row is not None:
                    res = _intg.revalidate_annotation(
                        store, cfg.root, dict(row), persist=True)
                    new_staleness = res.now
            except Exception:
                new_staleness = None
        # Surface the auto-extracted claims (if any) so the agent can
        # AUDIT what projmem inferred from the prose. Without this the
        # extraction is silent and the agent has no signal it happened.
        # Quantum-thinking-round upgrade: also include each claim's
        # IMMEDIATE verification status so the agent learns at write
        # time if a claim is already wrong (caught a typo: prose said
        # `foo at a.py:1` but indexer says foo is at a.py:5 → MOVED
        # before the note even hits storage). Reads the per-claim
        # statuses we already computed during the post-add
        # revalidate sweep and surfaces them here too.
        auto_extracted: list = []
        if body_claims:
            from . import claims as _cl_verify
            for c in body_claims:
                row: dict = {
                    "subject":     c.subject,
                    "predicate":   c.predicate,
                    "object":      c.object,
                    "truth_class": c.truth_class,
                }
                try:
                    v = _cl_verify.verify_claim(store, c).to_dict()
                    row["status"] = v.get("status")
                    if v.get("reason"):
                        row["reason"] = v["reason"][:200]
                except Exception:
                    row["status"] = "UNCHECKABLE"
                auto_extracted.append(row)
        out = {"id": ann_id, "target": args.target, "kind": args.kind,
               "body": args.body, "author": args.author,
               "expires_at": expires_at,
               "confidence": getattr(args, "confidence", None) or 0.5,
               "truth_class": getattr(args, "truth_class", None)
                              or "INFERENCE",
               "evidence_count": len(evidence_list or []),
               "claim_count": claim_count,
               "auto_extracted_claims": auto_extracted,
               "auto_extracted_count":  len(auto_extracted),
               "fingerprint_captured": fp is not None,
               "staleness": new_staleness}
    elif sub == "list":
        # Benchmark v2 Bug 7: accept --target as flag (symmetric with
        # note-verify). Flag wins if both positional and flag given.
        effective_target = (getattr(args, "target_flag", None)
                              or getattr(args, "target", None))
        rows = store.list_annotations(target=effective_target,
                                       kind=args.kind,
                                       include_expired=args.include_expired)
        # Optional --author filter.
        author_filter = getattr(args, "author", None)
        if author_filter:
            rows = [r for r in rows if (r.get("author") or "")
                    == author_filter]
        # Benchmark v3 Bug 4: --limit caps output; --fields projects
        # a subset of columns per row. Both are cost-reduction tools
        # for large note lists (the v3 run had `note list --author
        # projmem-seed` return 14 KB for rating 3 notes).
        limit = getattr(args, "limit", None)
        full_count = len(rows)
        if limit is not None and limit > 0:
            rows = rows[:limit]
        field_filter = getattr(args, "fields", None)
        if field_filter:
            keep = {f.strip() for f in field_filter.split(",") if f.strip()}
            rows = [{k: v for k, v in r.items() if k in keep}
                    for r in rows]
        out: Dict[str, Any] = {"annotations": rows, "count": len(rows),
                                "total": full_count}
        if limit is not None and full_count > limit:
            out["truncated"] = True
            out["hint"] = (f"Showing {limit} of {full_count}. "
                            "Raise --limit or drop the filter for more.")
    elif sub == "delete":
        # F002: bogus id used to return `{deleted: false}` exit 0 — a
        # silent no-op that masked typos. Verify the row exists first
        # and emit a structured error when it doesn't.
        row = store.conn.execute(
            "SELECT id FROM annotations WHERE id=?",
            (args.id,)).fetchone()
        if row is None:
            recent = list(store.conn.execute(
                "SELECT id, target FROM annotations "
                "ORDER BY created_at DESC LIMIT 5"))
            _emit_error(
                {"error":   "note-not-found",
                 "message": f"no note with id {args.id}",
                 "id":      args.id,
                 "recent":  [{"id": r["id"], "target": r["target"]}
                              for r in recent],
                 "hint":    "Run `projmem notes` to list current ids."},
                args.json, store=store)
        ok = store.delete_annotation(args.id)
        out = {"deleted": ok, "id": args.id}
    elif sub == "search":
        rows = store.search_annotations(
            args.query, include_expired=args.include_expired)
        out = {"annotations": rows, "count": len(rows), "query": args.query}
    elif sub == "show":
        # Fetch one full note by id — `list` truncates body, so agents
        # couldn't retrieve the full text without going to note-export
        # and filtering manually. This is the expected "give me the
        # full record" retrieval path.
        row = store.conn.execute(
            "SELECT * FROM annotations WHERE id=?", (args.id,)).fetchone()
        if row is None:
            out = {"error":   "note-not-found",
                   "message": f"no note with id {args.id}",
                   "id":      args.id,
                   "hint":    "Run `projmem notes` to list current notes."}
        else:
            out = dict(row)
    elif sub == "predicates":
        from . import claims as _claims
        out = {
            "predicates": sorted(_claims.PREDICATES),
            "hint": ("Use one of these in your --claims JSON's "
                      "`predicate` field. Unknown predicates are "
                      "REJECTED on `note add` unless you pass "
                      "`--allow-unknown-predicates` (which makes the "
                      "claim UNCHECKABLE forever)."),
        }
    else:
        out = {"error":   "unknown-subcommand",
               "message": f"unknown note subaction: {sub!r}",
               "available_options": ["add", "list", "delete",
                                       "search", "show", "predicates"],
               "hint": "Run `projmem note --help` to see subcommands."}
    store.close()
    _emit(out, args.json)


_CONCLUDE_TARGET_PATH_RX = re.compile(
    r"\b([a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-./]+"
    r"\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|h|hpp|java|"
    r"rb|kt|swift|php|scala|prisma|sql))\b"
)


def cmd_task(args):
    """Task verb cluster — session-continuity state.

    Subactions:
      start <goal>             open a task
      step <detail>            append progress to the active task
      blocked <detail>         mark active task blocked on a question
      unblock [--detail ...]   move blocked task back to active
      close [--detail ...]     mark active task done
      resume                   show open tasks + recent events
      list [--status X]        dump tasks
    """
    from . import tasks as _tasks
    cfg, store = _open_store(args.path)
    sub = args.task_action
    if sub == "start":
        out = _tasks.start(store, args.goal, author=args.author)
    elif sub == "step":
        out = _tasks.step(store, args.detail,
                            task_id=args.task_id, ref=args.ref)
    elif sub == "blocked":
        out = _tasks.blocked(store, args.detail, task_id=args.task_id)
    elif sub == "unblock":
        out = _tasks.unblock(store, detail=args.detail,
                                task_id=args.task_id)
    elif sub == "close":
        # Resolve `task close 5` (positional bare integer) into
        # --task-id 5 so the natural shorthand works without forcing
        # the flag form. --task-id explicit wins if both are passed.
        task_id = args.task_id
        if task_id is None and getattr(args, "close_target", None):
            try:
                task_id = int(args.close_target)
            except (TypeError, ValueError):
                _emit_error(
                    {"error":   "invalid-task-id",
                     "message": ("expected a numeric task id; got "
                                  f"{args.close_target!r}"),
                     "got":     args.close_target,
                     "hint": ("Pass a numeric task id, OR omit to "
                               "close the LIFO active task.")},
                    args.json, store=store)
        # F001: validate the task actually exists (and is open) before
        # claiming it was closed. The previous behavior returned
        # `status: done, exit 0` on bogus ids — silent wrong, masking
        # typos and hand-rolled ids in scripts.
        if task_id is not None:
            row = store.conn.execute(
                "SELECT id, status FROM tasks WHERE id=?",
                (int(task_id),)).fetchone()
            if row is None:
                open_rows = list(store.conn.execute(
                    "SELECT id, status, goal FROM tasks "
                    "WHERE status IN ('open','blocked') "
                    "ORDER BY id DESC LIMIT 10"))
                _emit_error(
                    {"error":   "task-not-found",
                     "message": f"no task with id {task_id}",
                     "task_id": int(task_id),
                     "candidates": [{"id": r["id"],
                                       "status": r["status"],
                                       "goal":   (r["goal"] or "")[:60]}
                                      for r in open_rows],
                     "hint": ("Run `projmem task list --status open` to "
                               "see the open ids; omit `--task-id` to "
                               "close the LIFO active task.")},
                    args.json, store=store)
            if row["status"] == "closed":
                _emit_error(
                    {"error":   "task-already-closed",
                     "message": f"task {task_id} is already closed",
                     "task_id": int(task_id),
                     "hint": ("Run `projmem task list --status closed` "
                               "to see closed tasks; nothing to do.")},
                    args.json, store=store)
        out = _tasks.close(store, task_id=task_id,
                             detail=args.detail)
    elif sub == "resume":
        out = _tasks.resume(store, limit=args.limit)
    elif sub == "list":
        # F013: normalize the friendlier aliases to the underlying
        # status enum the storage layer knows.
        _STATUS_ALIASES = {"open": "active", "closed": "done"}
        status_eff = _STATUS_ALIASES.get(args.status, args.status)
        out = _tasks.list_all(store, status=status_eff,
                                limit=args.limit,
                                verbose=getattr(args, "verbose", False))
    else:
        out = {"error":   "unknown-subcommand",
               "message": f"unknown task action: {sub!r}",
               "available_options": ["start", "step", "blocked",
                                       "close", "resume", "list"],
               "hint": "Run `projmem task --help` to see subcommands."}
    from . import memory_header as _mh
    _mh.attach(out, store)
    # `task resume` defaults to terse text — agents/humans call it
    # reflexively at session start, so the 50–100 line JSON dump was
    # active friction. Pass `--json` to get the full machine payload.
    if sub == "resume" and not getattr(args, "json", False):
        _print_resume_terse(out)
    else:
        _emit(out, args.json)
    store.close()


def _print_resume_terse(out: Dict[str, Any]) -> None:
    """3–6 line summary of `task resume` for interactive use. Lossy
    on purpose — the full record is one `--json` away."""
    open_tasks = out.get("open_tasks") or []
    closed = out.get("recently_closed") or []
    rm = out.get("repo_memory") or {}
    contradicted = rm.get("contradicted_count") or 0
    if contradicted:
        print(f"⚠ contradicted_count={contradicted} — "
              f"`projmem notes` to inspect REFUTED claims.")
    if not open_tasks:
        print("no open tasks")
    else:
        n_open = len(open_tasks)
        print(f"{n_open} open task{'s' if n_open != 1 else ''}, "
              f"{len(closed)} recently closed:")
        for t in open_tasks:
            ls = (t.get("last_step") or {}).get("detail") or ""
            ls = ls.strip().splitlines()[0] if ls else ""
            if len(ls) > 80:
                ls = ls[:77] + "..."
            files_n = len(t.get("files_touched") or [])
            notes_n = len(t.get("notes_saved") or [])
            print(f"  #{t['id']} [{t['status']} · "
                  f"{t['last_update_hours']}h ago · "
                  f"{files_n} files · {notes_n} notes] "
                  f"{(t.get('goal') or '')[:60]}")
            if ls:
                print(f"     ↳ last: {ls}")
    if closed:
        last = closed[0]
        print(f"  last closed: #{last['id']} "
              f"\"{(last.get('goal') or '')[:60]}\"")
    print("(--json for full payload)")


def cmd_conclude_session(args):
    """Extract durable conclusions from a conversation transcript and
    save them as structured notes. Run at session end (Claude Code
    SessionEnd hook, or manually) to close the capture gap without
    requiring the agent to remember `note add`.

    Only VERIFIED claims are saved. REFUTED claims are dropped (they
    would be agent hallucinations). UNCHECKABLE claims are dropped
    unless `--include-uncheckable` is set.
    """
    from . import conclude_session as _cs
    cfg, store = _open_store(args.path)
    if args.transcript == "-":
        text = sys.stdin.read()
    elif args.transcript:
        try:
            with open(args.transcript, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            _emit_error(
                {"error":   "read-failed",
                 "message": str(e),
                 "path":    args.transcript,
                 "hint": ("Check the path and read permissions, or "
                           "pipe the transcript to stdin via `-`.")},
                args.json, store=store)
    else:
        _emit_error(
            {"error":   "no-transcript",
             "message": "no transcript was provided",
             "hint": "Pass --transcript <path> or '-' to read stdin."},
            args.json, store=store)
    out = _cs.conclude_session(store, cfg.root, text,
                                 author=args.author,
                                 dry_run=args.dry_run)
    _emit(out, args.json)
    store.close()


def cmd_fact_check(args):
    """Verify claims embedded in arbitrary text against the current
    index. Designed to run on a proposed agent answer BEFORE it's
    shipped — catches confidently-stated wrong claims (file:line
    references, symbol-definition sites, export assertions) without
    requiring the agent to save them as notes first.

    Input modes:
      - `projmem fact-check "<text>"`            — inline string
      - `projmem fact-check --file <path>`       — read text from file
      - `projmem fact-check -` (dash)            — read from stdin
      - `projmem fact-check --diff <patch>`      — unified diff (round-6)
      - `projmem fact-check --diff -`            — diff from stdin
    """
    from . import factcheck as _fc
    cfg, store = _open_store(args.path)
    # Round-6 user-feedback gap #1: --diff bypasses the prose-claim
    # extractor and checks saved notes against a unified diff. The
    # buckets are different (`at_risk / moved / unaffected`) and the
    # exit code maps `at_risk` → 2 so a pre-commit gate fails.
    diff_arg = getattr(args, "diff", None)
    if diff_arg is not None:
        from . import diff_check as _dc
        if diff_arg == "-":
            diff_text = sys.stdin.read()
        else:
            try:
                with open(diff_arg, "r", encoding="utf-8",
                          errors="replace") as fh:
                    diff_text = fh.read()
            except OSError as e:
                _emit_error(
                    {"error":   "read-failed",
                     "message": str(e),
                     "path":    diff_arg,
                     "hint": ("Pass a unified diff path or `-` for "
                               "stdin. `git diff HEAD` is the usual "
                               "input.")},
                    args.json, store=store)
        out = _dc.check_diff(store, diff_text)
        _emit_with_memory(out, args.json, store)
        store.close()
        if out.get("verdict") == "at_risk":
            sys.exit(2)
        return
    text = args.text
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            _emit_error(
                {"error":   "read-failed",
                 "message": str(e),
                 "path":    args.file,
                 "hint": ("Check the path and read permissions, or "
                           "pass `-` to read text from stdin.")},
                args.json, store=store)
    elif text == "-":
        text = sys.stdin.read()
    out = _fc.fact_check(store, text or "", repo_root=cfg.root)
    # Freshness probe: check on-disk hash for every file the claims
    # cite. If any of those files drifted since the last index, the
    # "VERIFIED" verdict is unreliable — surface a freshness_warning
    # and downgrade the verdict to "stale_index" so callers don't
    # auto-trust it. Without this, an agent edits a file then fact-
    # checks an old line:N reference and gets a false PASS.
    cited_files: set = set()
    for c in out.get("claims") or []:
        obj = c.get("object") or ""
        # @defined-at / @exported-from object is "<file>:<line>" or "<file>"
        if isinstance(obj, str) and obj:
            f = obj.split(":", 1)[0].strip()
            if f and ("/" in f or f.endswith(
                    (".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
                     ".java", ".go", ".rs", ".rb", ".cc", ".cpp", ".h",
                     ".hpp", ".cs", ".kt", ".swift", ".php", ".scala"))):
                cited_files.add(f)
    if cited_files:
        from . import freshness as _fresh
        stale = _fresh.check_paths(store, cfg.root, cited_files)
        warn = _fresh.freshness_warning(stale)
        if warn:
            out["freshness_warning"] = warn
            # Downgrade an "all_verified" verdict when the underlying
            # index is stale on a cited file — the verification used a
            # snapshot that no longer matches disk.
            if out.get("verdict") == "all_verified":
                out["verdict"] = "stale_index"
                out["hint"] = (
                    "Claims VERIFIED against the index, but at least "
                    "one cited file drifted on disk since the last "
                    "index. Re-run `projmem index` and re-check before "
                    "trusting the verdict.")
    from . import memory_header as _mh
    _mh.attach(out, store)
    _emit(out, args.json)
    store.close()
    # Exit code: non-zero if anything refuted OR the index is stale
    # under the claim. CI / wrapper scripts gate on either condition.
    if out.get("verdict") in ("has_refuted", "stale_index"):
        sys.exit(2)


def cmd_check(args):
    """Single-shot fact-check wrapper. Returns ONLY the verdict + counts —
    no claims array, no bare-file-line list, no parse_errors detail. The
    full envelope is one tool-call away (`projmem fact-check`); this
    command exists so an agent doing a one-off "is this true?" check
    pays the minimum context cost.

    Round-6 user feedback: the audit agent reported that for one-shot
    tasks the ceremony of `task start` / `fact-check` / `task close`
    cost more context than just writing findings to a file. `check`
    collapses all of that into one call: extract claims, verify, return
    the verdict tag. Nothing else.
    """
    from . import factcheck as _fc
    cfg, store = _open_store(args.path)
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    out = _fc.fact_check(store, text or "", repo_root=cfg.root)
    lean = {
        "verdict":         out.get("verdict"),
        "extracted_count": out.get("extracted_count"),
        "verified":        out.get("verified"),
        "moved":           out.get("moved"),
        "refuted":         out.get("refuted"),
        "uncheckable":     out.get("uncheckable"),
        "duplicate_count": out.get("duplicate_count"),
    }
    if out.get("parse_errors"):
        lean["parse_errors_count"] = len(out["parse_errors"])
    if out.get("hint"):
        lean["hint"] = out["hint"]
    _emit(lean, args.json)
    store.close()
    if out.get("verdict") in ("has_refuted", "stale_index"):
        sys.exit(2)


def cmd_at(args):
    """Cursor-position lookup: `projmem at <file>:<line>[:<col>]` finds
    the symbol whose def range contains that line and returns its pack
    (defs + refs + neighbors). The primitive every editor / LSP / MCP
    integration needs but the CLI didn't expose — round-6 user-feedback
    gap #2.

    Resolution order:
      1. exact `(file, line)` match in the symbols table
      2. nearest preceding def whose `end_line` covers `line`
      3. nearest preceding def in the file (fallback when end_line is
         missing — common on regex-fallback files)

    Returns `{file, line, col, symbol, pack}` on hit. On miss returns
    `target-not-found` with the file's symbol list as candidates so
    the caller can disambiguate.
    """
    spec = args.cursor
    parts = spec.split(":")
    if len(parts) < 2:
        _emit_error(
            {"error":   "missing-line",
             "message": "expected `<file>:<line>` (col optional)",
             "got":     spec,
             "hint": "Example: projmem at src/foo.ts:42"},
            getattr(args, "json", False))
    file_part = parts[0]
    try:
        line_no = int(parts[1])
    except ValueError:
        _emit_error(
            {"error":   "invalid-line",
             "message": f"line must be an integer; got {parts[1]!r}",
             "got":     spec},
            getattr(args, "json", False))
    col = None
    if len(parts) >= 3:
        try:
            col = int(parts[2])
        except ValueError:
            col = None
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    # Verify the file exists in the index.
    file_row = store.conn.execute(
        "SELECT path FROM files WHERE path=? LIMIT 1",
        (file_part,)).fetchone()
    if file_row is None:
        # Try a substring suggestion.
        sims = [r["path"] for r in store.conn.execute(
            "SELECT path FROM files WHERE path LIKE ? LIMIT 5",
            (f"%{file_part}%",))]
        _emit_error(
            {"error":   "file-not-indexed",
             "message": f"no indexed file at {file_part!r}",
             "file":    file_part,
             "suggestions": sims,
             "hint": ("Pass an indexed path. Run `projmem files "
                       "--glob '*'` to list, or `projmem index` to "
                       "refresh.")},
            getattr(args, "json", False), store=store)
    # Resolution: nearest def whose range contains the cursor line.
    syms = list(store.conn.execute(
        "SELECT name, kind, line, end_line, col FROM symbols "
        "WHERE file=? AND line<=? "
        "ORDER BY line DESC LIMIT 50", (file_part, line_no)))
    chosen = None
    for s in syms:
        end_line = s["end_line"] or s["line"]
        if s["line"] <= line_no <= end_line:
            chosen = s
            break
    if chosen is None and syms:
        # Fallback: nearest preceding def even if end_line is missing.
        chosen = syms[0]
    if chosen is None:
        _emit_error(
            {"error":   "no-symbol-at-cursor",
             "message": (f"no symbol contains line {line_no} in "
                          f"{file_part}"),
             "file":    file_part,
             "line":    line_no,
             "hint":    ("File is indexed but has no symbol def at or "
                          "before this line. Either it's a comment / "
                          "blank-line region, or the file was indexed "
                          "by the regex backend without symbol data.")},
            getattr(args, "json", False), store=store)
    # Reuse the pack builder for the consistent output shape.
    from . import packs as _packs
    target = f"{file_part}#{chosen['name']}"
    pack = _packs.build_pack(cfg, store, target)
    out = {
        "file":      file_part,
        "line":      line_no,
        "col":       col,
        "symbol": {
            "name":     chosen["name"],
            "kind":     chosen["kind"],
            "def_line": int(chosen["line"]),
            "end_line": (int(chosen["end_line"])
                          if chosen["end_line"] else None),
        },
        "pack":      pack,
    }
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_check_line(args):
    """Claim-authoring shortcut. Builds `@defined-at(<symbol>, <file>:
    <line>)` from positional args, runs fact-check, returns the lean
    verdict.

    Round-6 user feedback: `@defined-at(...)` syntax is annoying enough
    to write that agents skip the verification step. This collapses
    "construct claim → verify" into one call.
    """
    file_part = args.file_line
    if ":" not in file_part:
        _emit_error(
            {"error":   "missing-line",
             "message": "expected `<file>:<line>` for first arg",
             "got":     file_part,
             "hint": "Example: projmem check-line src/foo.ts:10 myFn"},
            args.json)
    file_, _, line_ = file_part.rpartition(":")
    text = f"@defined-at({args.symbol}, {file_}:{line_})"
    args.text = text
    cmd_check(args)


def cmd_seed(args):
    """Auto-populate INFERENCE-class notes on high-signal targets so a
    fresh-indexed repo has SOMETHING in memory for session 2 to work
    with. Idempotent — existing seed notes are skipped; human-authored
    notes are never touched.

    Seeds four classes:
      - cross-layer enum surfaces (TS/Prisma/SQL/Rust mismatches)
      - hot files (top reverse-dep counts)
      - gateway symbols (top call-ref counts)
      - re-export barrels (`index.*`, `_ns/`, `_namespaces/`)
    """
    from . import seed as _seed
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    dry_run = bool(getattr(args, "dry_run", False))
    out = _seed.seed(store, max_notes=args.max_notes, dry_run=dry_run)
    if not dry_run:
        store.conn.commit()
    store.close()
    if dry_run:
        out["dry_run"] = True
        out["hint"] = ("No notes were written. Re-run without "
                        "--dry-run to commit; OR use the `created` "
                        "list to decide which targets you want and "
                        "add them manually with `projmem note add`.")
    _emit(out, args.json)


def cmd_guide(args):
    """On-demand deep docs. Returns the full workflow / commands /
    capture / signals detail that used to live in AGENTS.md. Called
    only when the agent needs more than the quick reference.
    """
    from . import guide as _guide
    out = _guide.guide(args.topic or "")
    # F009: an unknown topic is a real error — exit 2 via _emit_error
    # so callers can detect the typo from the shell.
    if out.get("error"):
        _emit_error(out, args.json)
    if args.json:
        _emit(out, args.json)
    else:
        if "body" in out:
            print(out["body"])
        elif "topics" in out:
            print("Available topics: " + ", ".join(out["topics"]))
            if out.get("hint"):
                print(out["hint"])
        else:
            _emit(out, False)


def cmd_ask(args):
    """Natural-language query dispatcher. Maps common question shapes
    ("who uses X", "what changed", "where is X defined") to the right
    underlying projmem commands and returns a synthesized answer.

    Intended for agents that haven't memorized the full command
    catalog. The `summary` field is a one-liner; `detail` carries the
    raw output of the underlying command in case the caller wants it.
    """
    from . import ask as _ask
    cfg, store = _open_store(args.path)
    out = _ask.ask(cfg, store, args.question)
    from . import memory_header as _mh
    _mh.attach(out, store)
    _emit(out, args.json)
    store.close()


def _auto_author() -> str:
    """Auto-populate an author string when --author is omitted.
    Benchmark v2 Bug 4: session 1's conclude calls landed with
    author=null, making multi-session attribution impossible."""
    import getpass
    import time as _time_aa
    try:
        user = getpass.getuser() or "agent"
    except Exception:
        user = "agent"
    return f"session-{user}-{int(_time_aa.time())}"


def cmd_conclude(args):
    """Low-friction conclusion saver. Body may contain inline claims in
    `@<predicate>(<subject>, <object>)` form — they become structured
    claims automatically without a separate JSON file.

    Defaults: kind=note, truth_class=FACT, confidence=0.85. Target is
    inferred from the first file-path cited in the body (or in any
    inline claim's object), falling back to `@project` when the body
    mentions no path.

    Benchmark v2 Bug 2 fix: before persisting, we now run fact-check
    on the body. If ANY inline FACT claim is REFUTED, we abort with
    the refutation evidence — a note that's contradicted-on-creation
    is worse than no note (it poisons contradicted_count and misleads
    the next session). Opt out with `--no-verify` when you
    deliberately want to persist a currently-wrong claim (e.g. as
    bait for a future verifier fix).
    """
    import time as _time
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    target = getattr(args, "target", None)
    if not target:
        m = _CONCLUDE_TARGET_PATH_RX.search(args.body or "")
        if m:
            target = m.group(1)
    if not target:
        target = "@project"

    # Pre-persist verification gate (benchmark v2 Bug 2).
    if not getattr(args, "no_verify", False):
        from . import factcheck as _fc
        fc = _fc.fact_check(store, args.body or "", repo_root=cfg.root)
        if fc.get("verdict") == "has_refuted":
            refuted = [c for c in (fc.get("claims") or [])
                       if c.get("status") == "REFUTED"]
            store.close()
            _emit({
                "error":    "refuted-before-save",
                "target":   target,
                "verdict":  fc["verdict"],
                "refuted_claims": refuted,
                "hint": ("Fact-check found at least one REFUTED claim. "
                         "A note with a REFUTED FACT claim would be "
                         "marked `contradicted` on creation, poisoning "
                         "contradicted_count for the next session. "
                         "Revise the body using `current_evidence` in "
                         "each refuted claim, then re-run. Pass "
                         "`--no-verify` to bypass this gate."),
            }, args.json)
            sys.exit(3)

    # Auto-populate author when omitted so multi-session attribution
    # isn't ambiguous. Always preserves explicit --author.
    author = getattr(args, "author", None) or _auto_author()

    # Delegate to the note-add pipeline by constructing a Namespace.
    class _Ns:
        pass
    ns = _Ns()
    ns.note_action = "add"
    ns.target = target
    ns.body = args.body
    ns.kind = getattr(args, "kind", None) or "note"
    ns.author = author
    ns.expires_days = getattr(args, "expires_days", None)
    ns.confidence = getattr(args, "confidence", 0.85)
    ns.truth_class = getattr(args, "truth_class", None) or "FACT"
    ns.evidence = getattr(args, "evidence", None)
    ns.assumptions = None
    ns.scope = None
    ns.claims = getattr(args, "claims", None)
    ns.path = args.path
    ns.json = args.json
    store.close()
    return cmd_note(ns)


def cmd_refute(args):
    """Sugar over `note add --kind refute` with structured evidence
    citations. Use after a hypothesis dies — the next agent reading
    this target will see the refutation FIRST and skip re-investigation.

    Benchmark v4 Bug 1 fix: agents naturally call `refute add <note_id>
    "<body>"` thinking the positional resolves to the disputed note's
    target. It didn't — the integer was stored verbatim as `target`,
    severing the refute from any file-namespace query. Two changes:
      * If `target` parses as a bare integer, treat it as a note id
        and resolve to the disputed note's target file.
      * Add an explicit `--note-id N` flag for the same effect; this
        is the recommended form so the intent is unambiguous.
    """
    import time as _time
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)

    # Resolve target: explicit --note-id wins, then bare-integer
    # positional, else use the positional as a file path / symbol_id.
    target = args.target
    note_id = getattr(args, "note_id", None)
    resolved_from_note: Optional[int] = None
    if note_id is None and target and target.isdigit():
        # Bare integer positional → almost certainly a misuse meaning
        # "refute note <N>". Auto-resolve.
        try:
            note_id = int(target)
        except ValueError:
            note_id = None
    if note_id is not None:
        row = store.conn.execute(
            "SELECT target FROM annotations WHERE id=? LIMIT 1",
            (int(note_id),)).fetchone()
        if row is None:
            _emit_error(
                {"error":   "note-not-found",
                 "message": f"no note with id {note_id}",
                 "note_id": note_id,
                 "hint": ("--note-id requires an existing note. List "
                           "candidates with `projmem note list`.")},
                args.json, store=store)
        target = row["target"]
        resolved_from_note = int(note_id)

    if not target or (target.isdigit() if target else False):
        _emit_error(
            {"error":   "invalid-target",
             "message": f"invalid target {target!r}",
             "got":     target,
             "hint": ("`refute add` expects a file path or symbol_id "
                       "as the first positional, OR `--note-id N` to "
                       "auto-resolve from the disputed note. A bare "
                       "integer is not a valid target.")},
            args.json, store=store)

    # When called WITHOUT --note-id and FACT notes already exist on
    # this target, refuse — the user almost certainly means to refute
    # one of THOSE, not file a free-standing counter-claim that the
    # engine ignores. Previous silent-add behavior trapped agents:
    # they thought they were producing a contradiction, but
    # `contradicted_count` stayed 0 and the CLAUDE.md BLOCKER signal
    # never fired. Listing the candidates makes the resolution
    # actionable (paste back `--note-id N`).
    if (resolved_from_note is None
            and not getattr(args, "allow_free_standing", False)):
        fact_candidates = [
            row for row in store.list_annotations(target=target,
                                                    include_expired=False)
            if (row.get("truth_class") or "").upper() == "FACT"
        ]
        if fact_candidates:
            store.close()
            _emit({
                "error": "ambiguous-refute-target",
                "target": target,
                "hint": ("FACT notes exist on this target. `refute add "
                          "<target>` without --note-id used to silently "
                          "store a separate note that the verifier ignored, "
                          "leaving `contradicted_count` at 0. Re-run with "
                          "`--note-id N` to flip a specific note, OR add "
                          "`--allow-free-standing` to keep the legacy "
                          "no-flip behavior."),
                "fact_candidates": [
                    {"id": r.get("id"),
                     "kind": r.get("kind"),
                     "body_preview": (r.get("body") or "")[:120],
                     "staleness": r.get("staleness")}
                    for r in fact_candidates[:10]
                ],
            }, args.json)
            sys.exit(2)

    body_parts = [args.body]
    if resolved_from_note is not None:
        body_parts.append(
            f"\n[refute resolved from note id {resolved_from_note}]")
    if args.evidence:
        body_parts.append("\nEvidence:")
        for e in args.evidence:
            body_parts.append(f"  - {e}")
    body = "\n".join(body_parts)
    expires_at = None
    if args.expires_days is not None:
        expires_at = _time.time() + args.expires_days * 86400.0
    ann_id = store.add_annotation(
        target=target, kind="refute", body=body,
        author=args.author, expires_at=expires_at)

    # When resolved from a specific note, mark THAT note as manually
    # contradicted. Without this, refute add was a write-only log:
    # `contradicted_count` stayed at 0, the CLAUDE.md "STOP on
    # contradicted_count > 0" workflow never fired, and the disputed
    # FACT note kept reporting VERIFIED. We append a sticky
    # `_manual_refute` marker into the disputed note's evidence so
    # revalidation respects the manual override (otherwise the next
    # revalidate would re-VERIFY the underlying claim and erase the
    # contradiction). Removing the refutation requires deleting either
    # the refute note or the disputed note; this is intentional.
    flipped_note: Optional[Dict[str, Any]] = None
    if resolved_from_note is not None:
        import json as _json
        row = store.conn.execute(
            "SELECT evidence FROM annotations WHERE id=? LIMIT 1",
            (resolved_from_note,)).fetchone()
        try:
            ev = _json.loads(row["evidence"]) if row and row["evidence"] else []
            if not isinstance(ev, list):
                ev = []
        except (TypeError, ValueError):
            ev = []
        ev.append({
            "_manual_refute":     True,
            "refuted_by_note_id": ann_id,
            "ts":                 _time.time(),
        })
        store.update_annotation_integrity(
            resolved_from_note,
            staleness="contradicted",
            evidence=ev,
            last_verified_at=_time.time())
        flipped_note = {"id": resolved_from_note,
                         "now_staleness": "contradicted"}
    store.close()
    out: Dict[str, Any] = {
        "id": ann_id, "target": target, "kind": "refute",
        "evidence_count": len(args.evidence or []),
        "body_preview": body[:160],
    }
    if resolved_from_note is not None:
        out["resolved_from_note"] = resolved_from_note
        out["flipped_disputed_note"] = flipped_note
    _emit(out, args.json)


def cmd_note_export(args):
    """Export annotations as JSON Lines for portability across machines/forks.

    F001: previously only target/kind/body/author/created_at/expires_at
    survived the export; evidence, claims, confidence, truth_class,
    fingerprint, scope, assumptions, and integrity state were silently
    dropped. A FACT note round-tripped as a generic INFERENCE without
    its claim chain — the verifier could no longer answer the
    contradicted-vs-fresh question. Now every column on the
    annotations row is preserved; JSON-encoded fields (evidence,
    assumptions, fingerprint) are decoded once for export so importers
    don't see double-encoded JSON strings.
    """
    cfg, store = _open_store(args.path)
    rows = store.list_annotations(include_expired=args.include_expired)
    store.close()
    import hashlib as _hashlib
    out_lines = []

    def _decode_jsonish(v):
        if v is None or v == "":
            return None
        if isinstance(v, (dict, list)):
            return v
        try:
            return json.loads(v)
        except (TypeError, json.JSONDecodeError):
            return v

    for r in rows:
        body = {
            "target":       r["target"],
            "kind":         r["kind"],
            "body":         r["body"],
            "author":       r.get("author"),
            "created_at":   r["created_at"],
            "expires_at":   r.get("expires_at"),
            # Round-5 round-2 F001: extended fields. Skip when absent
            # to keep payloads compact.
            "confidence":       r.get("confidence"),
            "confidence_base":  r.get("confidence_base"),
            "scope":            r.get("scope"),
            "truth_class":      r.get("truth_class"),
            "staleness":        r.get("staleness"),
            "last_verified_at": r.get("last_verified_at"),
            "evidence":         _decode_jsonish(r.get("evidence")),
            "assumptions":      _decode_jsonish(r.get("assumptions")),
            "fingerprint":      _decode_jsonish(r.get("fingerprint")),
        }
        # Drop keys that are None to keep the export small AND avoid
        # the importer overwriting populated fields with nulls when a
        # caller mixes old- and new-shape exports.
        body = {k: v for k, v in body.items() if v is not None}
        body["sha1"] = _hashlib.sha1(
            f"{r['target']}|{r['kind']}|{r['body']}".encode("utf-8")
        ).hexdigest()
        out_lines.append(json.dumps(body, default=str))
    text = "\n".join(out_lines)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n" if text else "")
        _emit({"exported": len(rows), "output": args.output}, args.json)
    else:
        # Print raw JSONL to stdout (one per line) — bypass the JSON wrapper
        print(text)


def cmd_note_import(args):
    """Import notes from a JSONL file. Dedupes by SHA-1 of (target,kind,body).

    F003: separate dupe vs invalid counters so callers can tell whether
    "skipped" meant "you already have it" (safe) vs "I couldn't parse
    line N" (data loss).

    F004: missing input file used to throw FileNotFoundError as a
    Python traceback. Now it returns a structured `read-failed` error
    with exit 2 just like every other read-from-path command.

    F001 follow-through: preserve evidence/claims/confidence/
    truth_class/fingerprint/scope/assumptions/staleness on import so a
    full export → import round-trip is lossless.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    import hashlib as _hashlib
    existing_hashes: set = set()
    for r in store.list_annotations(include_expired=True):
        existing_hashes.add(_hashlib.sha1(
            f"{r['target']}|{r['kind']}|{r['body']}".encode("utf-8")
        ).hexdigest())
    imported = 0
    skipped_dupe = 0
    skipped_invalid: list = []
    # Accept `-` as stdin so `cat exports.jsonl | projmem note-import -`
    # works the same way `fact-check -` does. The previous form rejected
    # stdin even with `< file`, forcing a temp-path workaround on every
    # share-memory-across-machines flow.
    if args.input == "-":
        import contextlib as _ctx
        f_ctx = _ctx.nullcontext(sys.stdin)
    else:
        try:
            f_ctx = open(args.input, "r")
        except OSError as e:
            _emit_error(
                {"error":   "read-failed",
                 "message": str(e),
                 "path":    args.input,
                 "hint": ("Pass an existing JSONL path produced by "
                           "`projmem note-export`, or `-` to read from "
                           "stdin.")},
                args.json, store=store)
    with f_ctx as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                skipped_invalid.append({
                    "line": line_no, "reason": "invalid-json",
                    "detail": str(e),
                })
                continue
            if not (isinstance(obj, dict)
                     and obj.get("target") and obj.get("kind")
                     and obj.get("body") is not None):
                skipped_invalid.append({
                    "line": line_no, "reason": "missing-required-fields",
                    "missing": [k for k in ("target", "kind", "body")
                                 if not obj.get(k)],
                })
                continue
            sha = obj.get("sha1") or _hashlib.sha1(
                f"{obj['target']}|{obj['kind']}|{obj['body']}".encode("utf-8")
            ).hexdigest()
            if sha in existing_hashes:
                skipped_dupe += 1
                continue
            try:
                store.add_annotation(
                    target=obj["target"], kind=obj["kind"], body=obj["body"],
                    author=obj.get("author"),
                    expires_at=obj.get("expires_at"),
                    confidence=obj.get("confidence"),
                    evidence=obj.get("evidence"),
                    assumptions=obj.get("assumptions"),
                    scope=obj.get("scope"),
                    truth_class=obj.get("truth_class"),
                    fingerprint=obj.get("fingerprint"),
                    staleness=obj.get("staleness"))
            except Exception as e:
                skipped_invalid.append({
                    "line": line_no, "reason": "insert-failed",
                    "detail": str(e),
                })
                continue
            existing_hashes.add(sha)
            imported += 1
    store.close()
    _emit({"imported":         imported,
            "skipped_dupes":    skipped_dupe,
            "skipped_invalid":  len(skipped_invalid),
            "invalid_lines":    skipped_invalid[:20],
            "input":            args.input}, args.json)


def cmd_note_expire(args):
    """Mark notes older than --older-than-days as expired (soft delete
    via expires_at). Without --hard-delete, expired notes are still
    queryable with --include-expired."""
    import time as _time
    cfg, store = _open_store(args.path)
    cutoff = _time.time() - args.older_than_days * 86400.0
    rows = list(store.conn.execute(
        "SELECT id FROM annotations WHERE created_at < ? "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (cutoff, _time.time())))
    if args.hard_delete:
        store.conn.execute(
            "DELETE FROM annotations WHERE id IN (" +
            ",".join("?" for _ in rows) + ")",
            tuple(r["id"] for r in rows)) if rows else None
        store.conn.commit()
    else:
        now = _time.time()
        for r in rows:
            store.conn.execute(
                "UPDATE annotations SET expires_at=? WHERE id=?",
                (now, r["id"]))
        store.conn.commit()
    store.close()
    _emit({"affected": len(rows),
           "mode": "hard-delete" if args.hard_delete else "soft-expire",
           "older_than_days": args.older_than_days}, args.json)


def cmd_note_verify(args):
    """Revalidate every note matching the target.

    Recomputes each note's fingerprint against the current code
    state and updates its staleness label (fresh / weakly_stale /
    strongly_stale / contradicted). Confidence decays with
    staleness. Output per note includes the old → new transition
    and which fingerprint fields drifted.
    """
    cfg, store = _open_store(args.path)
    from . import integrity as _intg
    target = args.target
    # Round-5-r3 F006: gate on target resolution before "0 results"
    # silently passes as success. If the user typed a path that
    # doesn't resolve we want target-not-found + suggestions, not a
    # bare `{verified: 0, results: []}` envelope.
    if target:
        _resolve_target_or_hint(
            store, cfg, target,
            command="note-verify", as_json=getattr(args, "json", False))
    rows = store.list_annotations(target=target, include_expired=False)
    if not rows and target:
        # Also try symbol-keyed lookups (file#name, symbol_id).
        rows = store.annotations_for_pack(
            file=target if "/" in target else None,
            symbol_ids=[target] if "#" not in target and "/" not in target
                       else None,
            names_in_file=None)
    results = []
    for r in rows:
        try:
            res = _intg.revalidate_annotation(store, cfg.root, r)
            entry = {
                "id":             res.ann_id,
                "target":         r["target"],
                "previous":       res.previous,
                "now":            res.now,
                "drifted_fields": res.drifted_fields,
                "new_confidence": res.new_confidence,
            }
            # Claim-level results. Present only when the note carries
            # structured claims (subject/predicate/object) in its evidence
            # array — legacy notes get an empty list here and behave as
            # before.
            if res.claim_verdicts:
                entry["claims"] = res.claim_verdicts
                entry["claim_overall_status"] = res.claim_overall_status
                entry["verified_count"] = sum(
                    1 for c in res.claim_verdicts if c.get("status") == "VERIFIED")
                entry["refuted_count"] = sum(
                    1 for c in res.claim_verdicts if c.get("status") == "REFUTED")
                entry["uncheckable_count"] = sum(
                    1 for c in res.claim_verdicts
                    if c.get("status") == "UNCHECKABLE")
            results.append(entry)
        except Exception as e:
            results.append({"id":      r.get("id"),
                             "error":   "revalidation-failed",
                             "message": str(e)})
    refuted_in_run = sum(
        1 for entry in results
        if (entry.get("now") == "contradicted"
            or (entry.get("refuted_count") or 0) > 0))
    _benchmark_line(cfg, store, refuted_in_run=refuted_in_run,
                    as_json=getattr(args, "json", False))
    _emit_with_memory({"target": target, "verified": len(results),
                        "results": results}, args.json, store)
    store.close()


def cmd_graph(args):
    """Render a claim-and-drift-aware graph centered on `target`.

    Three deliverables in `projmem-out/`:
      - graph.dot : pure DOT source (reproducible; PR-friendly)
      - graph.mmd : Mermaid (embeddable in GitHub markdown)
      - graph.svg : rendered SVG, only when graphviz `dot` is on PATH

    Nodes are colored by claim status (green=PROVED, red=REFUTED,
    yellow=AMBIGUOUS, grey=NONE). A black ring marks files whose
    on-disk hash diverged from the index. Edge style encodes
    confidence (solid=high/EXTRACTED, dashed=medium/INFERRED,
    dotted=low/AMBIGUOUS).

    Default scope: 2-hop neighborhood around `target`. Pass `--full`
    for the whole repo (capped at `--max-nodes`). Pass no target to
    fall back to a `--full`-style summary.
    """
    cfg, store = _open_store(args.path)
    from . import graph_viz as _gv
    target = getattr(args, "target", None)
    full = bool(getattr(args, "full", False)) or not target
    hops = max(0, int(getattr(args, "hops", 2)))
    max_nodes = int(getattr(args, "max_nodes", _gv.MAX_NODES_DEFAULT))
    out_dir = getattr(args, "out", None) or os.path.join(
        cfg.root, "projmem-out")
    formats = _parse_formats(getattr(args, "format", None))

    data = _gv.build_graph_data(
        store, cfg.root,
        target=target, hops=hops, full=full,
        include_files=not getattr(args, "no_symbols", False),
        max_nodes=max_nodes,
    )
    write_result = _gv.write_outputs(out_dir, data, formats=formats)
    out = {
        "target":     target,
        "scope":      "full" if full else f"{hops}-hop neighborhood",
        "out_dir":    out_dir,
        "wrote":      write_result["wrote"],
        "skipped":    write_result["skipped"],
        "stats":      data["stats"],
        "truncated":  data["truncated"],
        "next_step": (
            "Open graph.svg, paste into a PR, or embed graph.mmd in a "
            "markdown doc. Re-run after edits to see drift surface as "
            "black-ringed nodes."),
    }
    if data["stats"]["refuted_nodes"]:
        out["headline"] = (
            f"{data['stats']['refuted_nodes']} REFUTED node(s) — "
            "claim contradicts current code. Inspect with "
            "`projmem note-verify <target>`.")
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_report(args):
    """Render a one-page markdown digest at `projmem-out/REPORT.md`.

    Sections (stable across runs so consumers can grep/diff):
      - Headline ribbon
      - Top REFUTED claims this week
      - God symbols (top refcount, artifact-filtered)
      - Stale notes
      - Knowledge gaps (isolated symbols, ambiguous defs, renamed targets)
      - Recent notes
    """
    cfg, store = _open_store(args.path)
    from . import report as _rep
    out_dir = getattr(args, "out", None) or os.path.join(
        cfg.root, "projmem-out")
    payload = _rep.build_report(
        store, cfg.root,
        max_refuted=getattr(args, "max_refuted", 10),
        max_god_symbols=getattr(args, "max_god_symbols", 10),
        max_stale=getattr(args, "max_stale", 10),
        max_isolated=getattr(args, "max_isolated", 10),
        max_ambiguous=getattr(args, "max_ambiguous", 10),
        max_renamed=getattr(args, "max_renamed", 10),
        max_recent_notes=getattr(args, "max_recent", 10),
    )
    write_result = _rep.write_report(out_dir, cfg.root, payload)
    out = {
        "out_dir":   out_dir,
        "wrote":     write_result["wrote"],
        "headline":  payload["headline"],
        "sections": {
            "refuted_this_week":     len(payload["refuted_this_week"]),
            "god_symbols":           len(payload["god_symbols"]),
            "stale_notes":           len(payload["stale_notes"]),
            "knowledge_gap_buckets": {
                k: len(v) for k, v in payload["knowledge_gaps"].items()
            },
        },
        "next_step": (
            "Embed REPORT.md from CLAUDE.md or paste headline into a "
            "PR. Re-run after edits — REFUTED count is the trust signal."),
    }
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_mcp_server(args):
    """Run projmem as an MCP server on stdio.

    Exposes ~8 key tools (session, search, symbol, reverse, forward,
    fact-check, notes, note_add) to any MCP-aware client (Claude Code,
    Claude Desktop, Cursor, Continue). Register in the client's MCP
    config as:

        {"projmem": {"command": "projmem",
                     "args": ["mcp-server", "--path", "/repo"]}}

    Logs nothing to stdout (reserved for MCP transport); everything
    human-facing goes to stderr.
    """
    from . import mcp_server as _mcp
    rc = _mcp.main_sync(default_path=getattr(args, "path", None) or ".")
    sys.exit(rc)


def cmd_search(args):
    """Full-text search across notes, symbols, contracts, and files.

    Returns a unified result blob with up to `--limit` hits per bucket.
    Useful for an agent that doesn't know the exact symbol name yet —
    cheaper than calling `projmem symbol`/`projmem contracts`/`notes`
    one at a time.

    Buckets:
      notes      — annotation body / target / kind contains the query
      symbols    — symbol name LIKE the query (case-insensitive)
      contracts  — contract name LIKE the query (env / flag / token)
      files      — file path contains the query as a substring

    Each hit includes file/line so the agent can navigate immediately.
    """
    cfg, store = _open_store(args.path)
    q = (args.query or "").strip()
    raw_limit = int(getattr(args, "limit", 25))
    # Round-5-r3 F008: a non-positive `--limit` used to be silently
    # clamped to 1, hiding the user's mistake (`-1` was likely a
    # typo for "unbounded"). Reject explicitly so the caller fixes
    # the input or learns the flag's contract.
    if raw_limit < 1:
        _emit_error(
            {"error":   "invalid-limit",
             "message": (f"--limit must be a positive integer; got "
                          f"{raw_limit}"),
             "hint": ("Pass `--limit N` with N >= 1. There's no "
                       "unbounded form — pick a cap that suits your "
                       "context window.")},
            args.json, store=store)
    limit = raw_limit
    if not q:
        _emit_error(
            {"error":   "empty-query",
             "message": "search query was empty after trimming",
             "hint": "projmem search '<term>' — substring-matched "
                     "across notes, symbols, contracts, files."},
            args.json, store=store)

    out: Dict[str, Any] = {"query": q, "limit": limit, "buckets": {}}
    like = f"%{q}%"

    # Notes (uses existing search_annotations).
    notes = store.search_annotations(q, include_expired=False)[:limit]
    out["buckets"]["notes"] = [{
        "id":       n.get("id"),
        "target":   n.get("target"),
        "kind":     n.get("kind"),
        "body":     (n.get("body") or "")[:200],
        "staleness": n.get("staleness"),
    } for n in notes]

    # Symbols (case-insensitive name LIKE; cap results).
    sym_rows = list(store.conn.execute(
        "SELECT name, file, kind, line FROM symbols "
        "WHERE name LIKE ? COLLATE NOCASE "
        "ORDER BY exported DESC, name LIMIT ?", (like, limit)))
    out["buckets"]["symbols"] = [
        {"name": r["name"], "file": r["file"], "kind": r["kind"],
         "line": r["line"]} for r in sym_rows]

    # Contracts.
    con_rows = list(store.conn.execute(
        "SELECT name, kind, file, line, role FROM contracts "
        "WHERE name LIKE ? COLLATE NOCASE "
        "ORDER BY name LIMIT ?", (like, limit)))
    out["buckets"]["contracts"] = [
        {"name": r["name"], "kind": r["kind"], "file": r["file"],
         "line": r["line"], "role": r["role"]} for r in con_rows]

    # Files.
    file_rows = list(store.conn.execute(
        "SELECT path, lang FROM files WHERE path LIKE ? "
        "ORDER BY path LIMIT ?", (like, limit)))
    out["buckets"]["files"] = [
        {"path": r["path"], "lang": r["lang"]} for r in file_rows]

    out["total_hits"] = sum(len(v) for v in out["buckets"].values())
    out["hint"] = (
        f"{out['total_hits']} hit(s). Use `projmem symbol <name>`, "
        f"`projmem contracts <name>`, `projmem session <target>`, or "
        f"`projmem note get <id>` for the full record on any hit.")
    _emit_with_memory(out, args.json, store)
    store.close()


def cmd_watch(args):
    """Background watcher that re-verifies notes on every file save.

    Quiet by default — emits one line ONLY when a note's status
    transitions (e.g., fresh → contradicted). Stop with Ctrl-C; the
    finally block prints a single-line session summary.

    The polling cadence (`--interval`) defaults to 1.0s — snappy
    enough for an editor save loop but cheap on CPU. Use `--paths`
    to scope the watch to a subset (still revalidates notes whose
    target points at any file in that subset)."""
    cfg, store = _open_store(args.path)
    from . import watch as _watch
    paths = getattr(args, "paths", None)
    interval = float(getattr(args, "interval", 1.0))
    once = bool(getattr(args, "once", False))
    summary = _watch.watch(
        cfg, store,
        paths=paths,
        interval=interval,
        once=once,
        writer=lambda msg: sys.stderr.write(msg + "\n"),
    )
    store.close()
    _emit(summary, args.json)


def cmd_hook(args):
    """Install / uninstall / inspect git hooks that run projmem on
    every commit and checkout, surfacing REFUTED notes the moment
    they're introduced.

    Subcommands:
      install   — drop the managed block into post-commit + post-checkout
      uninstall — remove the managed block (other hook content kept)
      status    — report what's installed without changing anything
    """
    cfg = config_mod.load(args.path)
    from . import hooks as _hooks
    action = getattr(args, "hook_action", "status")
    if action == "install":
        result = _hooks.install(cfg.root, force=getattr(args, "force", False))
    elif action == "uninstall":
        result = _hooks.uninstall(cfg.root)
    else:
        result = _hooks.status(cfg.root)
    result["repo_root"] = cfg.root
    # F010: hook errors must exit 2 like every other structured error.
    if result.get("error"):
        _emit_error(result, args.json)
    _emit(result, args.json)


def _parse_formats(spec) -> List[str]:
    """Comma-separated --format spec → list. Empty/None → all formats."""
    if not spec:
        return ["dot", "mmd", "svg"]
    out: List[str] = []
    for tok in str(spec).split(","):
        tok = tok.strip().lower()
        if tok in ("dot", "mmd", "mermaid", "svg") and tok not in out:
            out.append("mmd" if tok == "mermaid" else tok)
    return out or ["dot", "mmd", "svg"]


def cmd_integrity(args):
    """Compute the per-target integrity score (SPEC #9).

    Returns the 0-1 score plus its contributing factors, any
    contradictions detected between notes and current code, any
    ambiguity signals (same-name symbols, etc.), and agent guidance
    strings when risk is high.
    """
    cfg, store = _open_store(args.path)
    from . import integrity as _intg
    isc = _intg.integrity_score(store, cfg.root, args.target)
    conflicts = _intg.detect_contradictions(store, cfg.root, args.target)
    ambig = _intg.ambiguity_for_target(store, args.target)
    _emit_with_memory({
        "target":         args.target,
        "score":          isc.score,
        "factors":        isc.factors,
        "guidance":       isc.guidance,
        "contradictions": [c.to_dict() for c in conflicts],
        "ambiguity":      [a.to_dict() for a in ambig],
    }, args.json, store)
    store.close()


def cmd_map(args):
    """System map — auto-detected modules, cross-module edges, hubs.

    Returns the "big picture" an agent needs before diving into a single
    file: top-level modules (auto-detected from the source tree), their
    file/symbol/exported counts, top exported symbols, per-module
    contract surface (env/flag/schema counts), cross-module import edges
    (direction + count + examples), and hub files.

    No user setup required — modules are inferred from the source
    layout. Re-run with different include/exclude globs if the inferred
    layout is wrong.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    from . import system_map as _sm
    report = _sm.build_map(
        store, cfg,
        max_modules=int(getattr(args, "max_modules", 30)),
        max_hubs=int(getattr(args, "max_hubs", 10)),
        max_top_symbols=int(getattr(args, "max_top_symbols", 5)))
    _emit_with_memory(report, True, store)
    store.close()


def cmd_verify_completeness(args):
    """Target-focused post-edit gate — "did I update everything I
    should have when I changed this file/symbol?".

    Narrower than `projmem complete` (which scans the whole repo).
    Returns: dangling refs, stale paths, unupdated consumers (parsed
    by regex fallback, refs may be stale), inconsistent-strategy
    test files, and any notes on the target that became contradicted
    as a result of the change.

    Exit 1 if any HIGH severity finding — so wrapper scripts / CI can
    gate on it.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    # P0#3 lazy refresh of the target file before verification.
    if not getattr(args, "no_auto_refresh", False):
        from . import freshness as _fresh
        target_file = args.target.split("#", 1)[0]
        ar = _fresh.auto_refresh_if_stale(cfg, store, [target_file],
                                            max_files=1)
    else:
        ar = None
    from . import verify_completeness as _vc
    report = _vc.verify(
        store, cfg, args.target,
        limit=int(getattr(args, "limit", 50)))
    if ar and ar.get("refreshed"):
        report["auto_refreshed"] = ar["refreshed"]
    _emit_with_memory(report, True, store)
    store.close()
    return 1 if report["severity_counts"].get("high", 0) > 0 else 0


def cmd_analyze_change(args):
    """Change-impact analysis for a single target — the pre-edit gate.

    Given a file or symbol you're about to change, returns:
      direct_dependents:         importers / callers (source-only)
      indirect_dependents:       transitive reverse reach (bounded)
      tests_affected:            test files touching the target
      contracts_affected:        env / flag / schema-field contracts in
                                  the target file or its dependents
      likely_forgotten_updates:  dangling refs, low-confidence consumers,
                                  orphan obligations — the "you probably
                                  forgot to update X" list
      coverage + next_steps_hint

    Use BEFORE edits to size the change; pair with
    `projmem verify-completeness` AFTER edits to confirm nothing slipped.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    # P0#3 lazy refresh of the target file.
    if not getattr(args, "no_auto_refresh", False):
        from . import freshness as _fresh
        target_file = args.target.split("#", 1)[0]
        ar = _fresh.auto_refresh_if_stale(cfg, store, [target_file],
                                            max_files=1)
    else:
        ar = None
    from . import analyze_change as _ac
    report = _ac.analyze_change(
        store, cfg, args.target,
        radius=int(getattr(args, "radius", 2)),
        max_per_bucket=int(getattr(args, "max_per_bucket", 50)))
    if ar and ar.get("refreshed"):
        report["auto_refreshed"] = ar["refreshed"]
    _emit_with_memory(report, True, store)
    store.close()


def cmd_flow(args):
    """Trace the usage chain of a contract (env var / flag / schema field).

    Single-file flow in v1:

      env-read  →  local-assign  →  (switch-case | conditional | read)

    This is the kind of one-call answer grep can't give you. `projmem flow
    TSC_WATCHFILE` shows where the env var is read, what local it's
    assigned to, and every site that reads that local — with each site
    tagged as a switch case / conditional / plain read.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    from . import flow as _flow
    report = _flow.trace_flow(
        store, cfg.root, args.name,
        kind=getattr(args, "kind", None),
        max_consumers_per_read=int(getattr(args, "max_consumers", 50)),
        cross_file=not getattr(args, "no_cross_file", False))
    # Coverage hint when flow returns nothing AND the contract isn't
    # in the index. Java Bean-setter and XML config ARE now extracted
    # (#8 fix). This hint now fires only for truly absent names.
    if (not (report.get("read_sites") or [])
            and not (report.get("consumers") or [])):
        # Check if the name has any contract row at all.
        try:
            n_rows = store.conn.execute(
                "SELECT COUNT(*) AS n FROM contracts WHERE name=?",
                (args.name,)).fetchone()["n"]
        except Exception:
            n_rows = 0
        if n_rows == 0:
            report["coverage_note"] = (
                f"No contract named {args.name!r} in the index. "
                "`flow` covers env reads, CLI flags, schema libraries, "
                "Java Bean setters, and XML properties. "
                "Try `projmem search " + args.name + "` (cross-bucket "
                "substring) or `projmem symbol " + args.name +
                "` for a call-site view.")
    # Freshness probe over the files the flow touched.
    from . import freshness as _fresh
    touched = {s["file"] for s in report.get("read_sites") or []}
    touched |= {c["file"] for c in report.get("consumers") or []}
    stale = _fresh.check_paths(store, cfg.root, touched)
    warn = _fresh.freshness_warning(stale)
    if warn:
        report["freshness_warning"] = warn
    report = _apply_budget(report, getattr(args, "budget", None),
                           priority=["name", "kind", "summary",
                                     "read_sites", "consumers",
                                     "freshness_warning"])
    _emit_with_memory(report, args.json, store)
    store.close()


def cmd_predicates(args):
    """Top-level alias for `note predicates`. Lists the structured-claim
    catalog the verifier supports. Round-4 #20: predicates were buried
    under `note predicates`, easy to miss when an agent is authoring an
    `@predicate(...)` claim and wants to know what's accepted."""
    from . import claims as _claims
    out = {
        "predicates": sorted(_claims.PREDICATES),
        "hint": ("Use one of these in your `@predicate(subject, "
                  "object)` inline claims OR in `--claims` JSON. "
                  "Unknown predicates are REJECTED at note-add time "
                  "unless `--allow-unknown-predicates` is passed."),
    }
    _emit(out, getattr(args, "json", False))


def cmd_usage(args):
    """Self-document for any LLM agent: what projmem is, what commands
    exist, and the recommended workflow.

    Output is markdown by default — drop it directly into a system
    message or read it once at the start of a session. `--json` returns
    a structured catalog for harnesses that want to enumerate commands
    programmatically.

    Designed to work with ANY LLM that can shell out — Claude, GPT,
    local Llama via Ollama, Mistral, anything. No protocol required:
    the LLM just runs `projmem usage` and reads the result.
    """
    from . import agent_usage as _u
    if getattr(args, "json", False):
        _emit(_u.render_json(), True)
    else:
        sys.stdout.write(_u.render_markdown())


def _detect_agent_from_env() -> str:
    """Guess which agent is running from env variables. Returns a key
    in projmem.agent_init.TEMPLATES (or in AGENTS_ALIASES). Falls back
    to `agents` (AGENTS.md only) when no specific signal is present.

    Detection signals:
      - Claude Code:  CLAUDE_PROJECT_DIR / CLAUDECODE / ANTHROPIC_TOOL_HARNESS
      - Cursor:       CURSOR_SESSION_ID / CURSOR_CHAT / CURSOR_USER
      - Codex:        OPENAI_CLI_AGENT / CODEX_HOME / CODEX_TOOL_FILE
      - Gemini:       GEMINI_TOOL_FILE / GOOGLE_GENAI_AGENT
      - Kiro:         KIRO_PROJECT_DIR / KIRO_AGENT
      - OpenCode:     OPENCODE_SESSION
      - Antigravity:  AG_AGENT / ANTIGRAVITY_AGENT
      - VS Code Copilot: VSCODE_COPILOT_CHAT / GH_COPILOT_AGENT
    """
    env = os.environ
    if any(k in env for k in ("CLAUDE_PROJECT_DIR", "CLAUDECODE",
                                "ANTHROPIC_TOOL_HARNESS")):
        return "claude"
    if any(k in env for k in ("CURSOR_SESSION_ID", "CURSOR_CHAT",
                                "CURSOR_USER")):
        return "cursor"
    if any(k in env for k in ("CODEX_HOME", "CODEX_TOOL_FILE",
                                "OPENAI_CLI_AGENT")):
        return "codex"
    if any(k in env for k in ("GEMINI_TOOL_FILE", "GOOGLE_GENAI_AGENT")):
        return "gemini"
    if any(k in env for k in ("KIRO_PROJECT_DIR", "KIRO_AGENT")):
        return "kiro"
    if "OPENCODE_SESSION" in env:
        return "opencode"
    if any(k in env for k in ("AG_AGENT", "ANTIGRAVITY_AGENT")):
        return "antigravity"
    if any(k in env for k in ("VSCODE_COPILOT_CHAT", "GH_COPILOT_AGENT")):
        return "copilot"
    # Everyone else → AGENTS.md. Broadest-supported format.
    return "agents"


def cmd_init(args):
    """One-command setup: build the index AND drop agent-instruction
    templates so any LLM tool that reads CLAUDE.md / AGENTS.md /
    .cursorrules picks up the projmem workflow automatically.

    The intended end-user flow is just two steps:

        pip install projmem
        projmem init

    `projmem init` is idempotent — re-running it skips the index when
    one already exists and skips template files that are already on
    disk (use `--force` to overwrite, `--reindex` to rebuild the index).
    Pick a template with `--template {agents|claude|cursor|all}`.

    Pass `--no-index` to skip the indexing step (useful in CI or when
    you only want to refresh the templates).
    """
    from . import agent_init as _init
    from . import indexer as _indexer
    cfg = config_mod.load(args.path)
    os.makedirs(cfg.store_dir, exist_ok=True)
    # Resolve the agent choice. Priority: positional `agent` arg > legacy
    # --template flag > auto-detect from env > fall back to `agents`.
    # Default behavior shipped ONE file, not three — agents only read the
    # one that matches their convention, the others were dead weight and
    # cost startup latency.
    agent_arg = getattr(args, "agent", None)
    template_flag = getattr(args, "template", None)
    if agent_arg and agent_arg != "auto":
        template = agent_arg
    elif template_flag:
        template = template_flag
    else:
        template = _detect_agent_from_env()
    # Normalize aliases.
    if template == "codex":
        template = "agents"
    force = bool(getattr(args, "force", False))

    # Step 1: index. Skip if --no-index, OR if an index already exists
    # and --reindex wasn't passed (idempotent re-runs are cheap).
    index_action = "skipped"
    index_counts: Dict[str, Any] = {}
    if not getattr(args, "no_index", False):
        index_db_exists = os.path.isfile(cfg.db_path)
        wants_reindex = bool(getattr(args, "reindex", False))
        if not index_db_exists or wants_reindex:
            store = Store(cfg.db_path)
            index_counts = _indexer.index_all(
                cfg, store, force=wants_reindex)
            store.close()
            index_action = "rebuilt" if wants_reindex else "built"
        else:
            index_action = "exists (pass --reindex to rebuild)"

    # Step 2: drop the agent-instruction templates.
    template_result = _init.write_templates(cfg.root, template=template,
                                              force=force)

    out = {
        "index": {
            "action": index_action,
            **{k: index_counts.get(k) for k in
               ("indexed", "removed", "oversize_skipped")
               if k in index_counts},
        },
        "templates": template_result,
        "next_steps": [
            "Open this repo in your LLM tool (Claude Code / Cursor / "
            "Continue / Aider). The agent will read the dropped "
            "instruction file on its next session and start using projmem.",
            "Verify with: `projmem doctor` (health check) and "
            "`projmem usage` (full agent-facing command catalog).",
        ],
    }
    # Memory banner so the very first interaction surfaces what's
    # remembered — the primary UX fix for "second agent behaves like
    # first agent because memory is hidden".
    store = Store(cfg.db_path)
    try:
        from . import memory_header as _mh
        _mh.attach(out, store)
    finally:
        store.close()
    _emit(out, args.json)


def cmd_notes(args):
    """Project-wide memory summary — "what's remembered in this repo?".

    First-class entry point for a new agent. Returns, in one bounded blob:
      - totals:         counts by staleness / truth_class / kind
      - recent_notes:   last N notes (newest first), revalidated live
      - contradicted:   notes whose FACT claims are currently refuted
      - risk_targets:   targets ranked by contradicted / refuted / stale
      - authors:        who has been writing notes in this repo
      - index_summary:  files, symbols, ref-binding percent
      - next_steps_hint: concrete first actions

    Agents should call this BEFORE `projmem session <target>` when they
    don't know yet which target to investigate. Stable JSON schema.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    from . import notes_summary as _ns
    blob = _ns.build_summary(
        store, cfg.root,
        max_recent=int(getattr(args, "max_recent", 10)),
        max_contradicted=int(getattr(args, "max_contradicted", 10)),
        max_risk_targets=int(getattr(args, "max_risk_targets", 10)))
    # Bug fix bench iter 4 — contradicted_count_mismatch: cmd_notes used
    # _emit() which skips the repo_memory header, so projmem notes output
    # lacked repo_memory.contradicted_count while task resume / note-verify
    # included it. Fixed by using _emit_with_memory() like all other read
    # commands so the envelope is consistent across commands.
    if not getattr(args, "json", False):
        # Default: terse text. The full JSON blob is ~5 KB on a busy
        # repo and agents called `notes` reflexively at session start —
        # too expensive at default. Pass `--json` for the structured
        # payload (existing scripts already do).
        from . import memory_header as _mh
        _mh.attach(blob, store)
        _print_notes_terse(blob)
    else:
        _emit_with_memory(blob, True, store)
    store.close()


def _print_notes_terse(blob: Dict[str, Any]) -> None:
    """6–10 line summary of project memory for interactive use."""
    rm = blob.get("repo_memory") or {}
    totals = blob.get("totals") or {}
    by_st = totals.get("by_staleness") or {}
    contradicted = rm.get("contradicted_count") or 0
    if contradicted:
        print(f"⚠ contradicted_count={contradicted} — "
              f"FACT claim(s) refuted. STOP and `projmem notes --json` "
              f"to inspect.")
    n_total = totals.get("total_notes") or 0
    if n_total == 0:
        print("no notes saved yet")
        print("(--json for full payload)")
        return
    parts = [f"{n_total} note(s)"]
    for label in ("contradicted", "strongly_stale", "weakly_stale", "fresh"):
        if by_st.get(label):
            parts.append(f"{by_st[label]} {label}")
    print(" · ".join(parts))
    risk = blob.get("risk_targets") or []
    if risk:
        print("top risk:")
        for r in risk[:3]:
            print(f"  {r.get('target','?')[:60]} "
                  f"[contradicted={r.get('contradicted_count',0)} "
                  f"refuted={r.get('refuted_claim_count',0)} "
                  f"stale={r.get('stale_note_count',0)}]")
    recent = blob.get("recent_notes") or []
    if recent:
        last = recent[0]
        body = (last.get("body_preview") or "").strip().splitlines()
        body_first = body[0][:80] if body else ""
        print(f"latest: {last.get('target','?')[:50]} "
              f"({last.get('staleness','?')}) — {body_first}")
    print("(--json for full payload)")


def cmd_session(args):
    """One-call agent bootstrap — project-wide OR per-target.

    `projmem session`                → PROJECT bootstrap
    `projmem session <file_or_symbol>` → TARGET bootstrap

    PROJECT bootstrap returns: total notes, recent, contradicted, risk-
    ranked targets, known contracts, index health, next_steps_hint.
    This is the "what's in this repo's memory?" entrypoint — replaces
    needing to know about `projmem notes` / `projmem note list`.

    TARGET bootstrap returns the familiar per-target blob: actionable
    doctor findings, notes with per-claim verdicts, integrity score,
    recent activity, dependency neighbors, freshness_warning,
    next_steps_hint.

    Replaces 5+ chained calls. Always returns JSON.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    target = getattr(args, "target", None) or None
    if target:
        # Round-5-r2 F008: validate the target resolves to SOMETHING
        # in the index before spending cycles building a session blob
        # that says "0 notes, 0 neighbors, 0 everything." That shape
        # read as "target is safe / nothing to know" when in fact the
        # target was a typo or an uninindexed path.
        _resolve_target_or_hint(
            store, cfg, target,
            command="session", as_json=getattr(args, "json", False))
        # P0#3 lazy refresh of the target file when we have one.
        if not getattr(args, "no_auto_refresh", False) and "/" in target:
            from . import freshness as _fresh
            _fresh.auto_refresh_if_stale(
                cfg, store, [target.split("#", 1)[0]], max_files=1)
        from . import session as _session
        blob = _session.build_session(
            store, cfg.root, target,
            max_notes=int(getattr(args, "max_notes", 10)),
            max_audit_entries=int(getattr(args, "max_audit", 5)),
            max_neighbors_each=int(getattr(args, "max_neighbors", 5)))
    else:
        # Project-wide entrypoint. Wraps `notes_summary.build_summary`
        # and appends known_contracts so an agent sees env / flag /
        # schema-field coverage up front without a separate call.
        from . import notes_summary as _ns
        blob = _ns.build_summary(
            store, cfg.root,
            max_recent=int(getattr(args, "max_recent", 10)),
            max_contradicted=int(getattr(args, "max_contradicted", 10)),
            max_risk_targets=int(getattr(args, "max_risk_targets", 10)))
        blob["known_contracts"] = _known_contracts_summary(
            store, limit=int(getattr(args, "max_contracts", 15)))
        blob["mode"] = "project"
    _benchmark_line(cfg, store,
                    as_json=getattr(args, "json", False))
    blob = _apply_budget(blob, getattr(args, "budget", None),
                         priority=["mode", "totals", "next_steps_hint",
                                   "recent_notes", "contradicted",
                                   "risk_targets", "known_contracts",
                                   "audit_trail", "neighbors", "notes",
                                   "integrity", "freshness_warning"])
    _emit_with_memory(blob, True, store)   # always JSON: machine-readable bootstrap
    store.close()


def _known_contracts_summary(store, limit: int = 15) -> Dict:
    """Compact known-contract rollup for the project-wide session view.

    Returns counts + top-N sample per kind (env / flag / schema_field /
    token). Deliberately small — exists so the agent sees the shape of
    the project's external-facing surface without a separate
    `projmem contracts` sweep."""
    out: Dict = {}
    try:
        rows = list(store.conn.execute(
            "SELECT kind, COUNT(*) AS n, COUNT(DISTINCT name) AS distinct_n "
            "FROM contracts WHERE role != 'occurrence' GROUP BY kind"))
        out["counts_by_kind"] = [
            {"kind": r["kind"], "total": r["n"],
             "distinct_names": r["distinct_n"]}
            for r in rows]
        samples: Dict = {}
        for bucket in ("env", "flag", "schema_field"):
            top = list(store.conn.execute(
                "SELECT name, COUNT(*) AS n FROM contracts "
                "WHERE kind=? AND role != 'occurrence' "
                "GROUP BY name ORDER BY n DESC LIMIT ?",
                (bucket, limit)))
            if top:
                samples[bucket] = [
                    {"name": r["name"], "references": r["n"]}
                    for r in top]
        if samples:
            out["top_names"] = samples
    except Exception:
        pass
    return out


def cmd_doctor(args):
    """One-shot health check of the projmem install + index.

    Surfaces the issues that most commonly break first-run success:
      - tree-sitter not installed
      - foreign index (DB built at a different root)
      - oversize files silently skipped
      - regex fallback dominating JS/TS parse
      - stale files on disk (sampled)
      - artifact bleed (dist/, node_modules/, etc. in the index)

    Exit code reflects the worst severity: 0 (ok / info-only / warnings),
    1 (unhealthy — at least one HIGH finding).
    """
    cfg, store = _open_store(args.path)
    from . import doctor as _doctor
    report = _doctor.run(cfg, store,
                          skip_stale_check=getattr(args, "skip_stale_check",
                                                     False))
    store.close()
    _emit(report, args.json)
    return 1 if report["severity_counts"].get("high", 0) > 0 else 0


def cmd_complete(args):
    """End-of-task primitive — call this AFTER you finish editing.

    Two steps in one call:
      1. `refresh --reindex`: walk the disk, detect modified+added+deleted
         files since the last index, re-parse only what changed. Cheap
         and incremental — typically seconds, not minutes.
      2. `checklist`: post-edit completeness gate. Catches forgotten
         contract obligations, dangling symbol refs, broken repo-relative
         imports.

    The agent runs ONE command at task end instead of remembering to
    chain refresh + checklist (and forgetting the deletions case). This
    is the everyday alternative to a full `projmem index` rebuild.

    Exit code 1 when checklist finds at least one HIGH finding (matches
    doctor / checklist semantics) so CI can gate on it.
    """
    cfg, store = _open_store(args.path)
    # Step 1: incremental refresh.
    changes = indexer.discover_changes(cfg, store)
    to_reindex = changes["modified"] + changes["added"]
    refresh_applied: Dict[str, Any] = {"reindexed": 0, "removed": 0}
    if to_reindex:
        counts = indexer.index_all(cfg, store, paths=to_reindex, force=True)
        refresh_applied["reindexed"] = counts.get("indexed", 0)
    notes_expired_for_deleted = 0
    for p in changes["deleted"]:
        store.remove_file(p)
        # Bug fix bench iter 5 — orphan_verified_regression / Bug 2:
        # Soft-expire notes targeting the deleted file so they no longer
        # inflate contradicted_count and block agents in future sessions.
        notes_expired_for_deleted += store.expire_annotations_for_deleted_file(p)
        refresh_applied["removed"] += 1
    refresh_applied["notes_expired_for_deleted"] = notes_expired_for_deleted
    store.commit()

    # Step 2: post-edit checklist (always runs even when nothing changed,
    # because contract obligations from the prior session may still be open).
    from . import checklist as _checklist
    report = _checklist.run(
        cfg, store,
        base=getattr(args, "base", "pre-index"),
        head=getattr(args, "head", "current"),
        scope_only=not getattr(args, "include_vendor", False),
        include_artifacts=bool(getattr(args, "include_artifacts", False)),
        limit=int(getattr(args, "limit", 200)),
    )
    # F018 (round-7): if any saved FACT note is currently contradicted,
    # the gate must surface that AT THE TOP LEVEL. Previously the
    # checklist `overall` could read "ok" while `_emit_with_memory`
    # propagated exit 1 from the repo_memory blocker — gate and
    # status field disagreed. Now we synthesize a HIGH finding from
    # the contradicted_count and let it flow into `overall`.
    contradicted_now = int(store.conn.execute(
        "SELECT COUNT(*) FROM annotations "
        "WHERE staleness='contradicted' "
        "AND (expires_at IS NULL OR expires_at > strftime('%s','now'))"
    ).fetchone()[0])
    findings_list = list(report.get("findings") or [])
    sev_counts = dict(report.get("severity_counts") or {})
    overall = report.get("overall")
    if contradicted_now > 0:
        findings_list.insert(0, {
            "code":     "contradicted_notes",
            "severity": "high",
            "message": (f"{contradicted_now} note(s) contradicted — "
                         "saved FACT claim(s) refuted by current code. "
                         "Run `projmem notes` to inspect."),
            "suggestion": ("Resolve each via `projmem refute add "
                            "--note-id N --reason ...` or delete the "
                            "stale claim before shipping."),
        })
        sev_counts["high"] = sev_counts.get("high", 0) + 1
        overall = "unhealthy"
    out = {
        "refresh": {
            "modified":  changes["modified"],
            "added":     changes["added"],
            "deleted":   changes["deleted"],
            "modified_count": len(changes["modified"]),
            "added_count":    len(changes["added"]),
            "deleted_count":  len(changes["deleted"]),
            "applied":   refresh_applied,
        },
        # Benchmark v3 Bug 2 fix: expose `findings`, `overall`, and
        # `severity_counts` at the top level so `jq '.findings[]'`
        # works without needing to know it's nested inside `checklist`.
        # Always a LIST (possibly empty) — never null.
        "findings":       findings_list,
        "overall":        overall,
        "severity_counts": sev_counts,
        "contradicted_notes_count": contradicted_now,
        "checklist":      report,
        "next_steps_hint": [
            ("Address the HIGH findings above before claiming done"
             if report["severity_counts"].get("high", 0) > 0 else None),
            (f"Index updated: +{len(changes['added'])} added, "
             f"~{len(changes['modified'])} modified, "
             f"-{len(changes['deleted'])} deleted"
             if to_reindex or changes['deleted'] else None),
            ("All clear — refresh found no changes, checklist found no "
             "HIGH findings"
             if (not to_reindex and not changes['deleted']
                  and report['severity_counts'].get('high', 0) == 0)
             else None),
        ],
    }
    out["next_steps_hint"] = [s for s in out["next_steps_hint"] if s]
    # Memory banner on every task-end call — keeps memory presence
    # visible even when the agent is just running `projmem complete`
    # in a tight loop between edits.
    _benchmark_line(cfg, store,
                    as_json=getattr(args, "json", False))
    _emit_with_memory(out, args.json, store)
    store.close()
    # F018: factor in the synthesized contradicted-notes finding so
    # rc reflects the SAME truth `overall` reports.
    return 1 if sev_counts.get("high", 0) > 0 else 0


def cmd_checklist(args):
    """Post-edit completeness gate.

    Surfaces the most common "you forgot to update X" failures:
      - open contract obligations (added-orphan / removed-dangling)
      - dangling symbol refs (removed names still referenced)
      - unresolved repo-relative imports missing on disk

    Exit code mirrors doctor: 1 when at least one HIGH finding exists.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    from . import checklist as _checklist
    report = _checklist.run(
        cfg, store,
        base=getattr(args, "base", "pre-index"),
        head=getattr(args, "head", "current"),
        scope_only=not getattr(args, "include_vendor", False),
        include_artifacts=bool(getattr(args, "include_artifacts", False)),
        limit=int(getattr(args, "limit", 200)),
    )
    store.close()
    _emit(report, args.json)
    return 1 if report["severity_counts"].get("high", 0) > 0 else 0


def cmd_audit(args):
    """Aggregate claim-level verdicts across every note on a target.

    Produces a crisp summary of "what did we believe about this file/symbol,
    and what's still true?" — the load-bearing read path for the MemTrace
    differentiator. For each note on the target, we run claim verification
    and roll up:

      - total VERIFIED / REFUTED / UNCHECKABLE counts
      - refuted claims grouped by subject (so you see "TSC_WATCHFILE has
        1 refuted env-read claim across 2 notes" not just "2 stale notes")
      - contradicted notes (any FACT claim refuted)
      - per-note drill-down with claim statuses

    Notes with no structured claims are surfaced too (they contribute to
    `legacy_note_count`) but carry no claim statistics.
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    from . import integrity as _intg
    from . import claims as _claims
    target = args.target

    # Gather notes on the target. Same lookup strategy as cmd_note_verify.
    rows = store.list_annotations(target=target, include_expired=False)
    if not rows and target:
        rows = store.annotations_for_pack(
            file=target if "/" in target else None,
            symbol_ids=[target] if "#" not in target and "/" not in target
                       else None,
            names_in_file=None)

    per_note: List[Dict[str, Any]] = []
    total_claims = 0
    v_total = r_total = u_total = 0
    contradicted_notes: List[int] = []
    refuted_by_subject: Dict[str, Dict[str, Any]] = {}
    legacy_count = 0

    for ann in rows:
        try:
            rev = _intg.revalidate_annotation(store, cfg.root, ann,
                                               persist=False)
        except Exception as e:
            per_note.append({
                "id":      ann.get("id"),
                "target":  ann.get("target"),
                "error":   "revalidation-failed",
                "message": str(e),
            })
            continue
        verdicts = rev.claim_verdicts or []
        if not verdicts:
            legacy_count += 1
            per_note.append({
                "id": ann.get("id"), "target": ann.get("target"),
                "kind": ann.get("kind"),
                "staleness": rev.now,
                "has_claims": False,
                "legacy": True,
            })
            continue
        v_count = sum(1 for v in verdicts if v.get("status") == "VERIFIED")
        r_count = sum(1 for v in verdicts if v.get("status") == "REFUTED")
        u_count = sum(1 for v in verdicts if v.get("status") == "UNCHECKABLE")
        v_total += v_count; r_total += r_count; u_total += u_count
        total_claims += len(verdicts)
        if rev.claim_overall_status == _claims.CONTRADICTED:
            contradicted_notes.append(int(ann["id"]))
        for v in verdicts:
            if v.get("status") != "REFUTED":
                continue
            subj = str(v.get("subject") or "")
            slot = refuted_by_subject.setdefault(subj, {
                "subject":        subj,
                "refuted_count":  0,
                "note_ids":       [],
                "predicates":     set(),
                "first_reason":   v.get("reason"),
            })
            slot["refuted_count"] += 1
            if ann.get("id") not in slot["note_ids"]:
                slot["note_ids"].append(int(ann["id"]))
            slot["predicates"].add(v.get("predicate"))
        per_note.append({
            "id": ann.get("id"),
            "target": ann.get("target"),
            "kind": ann.get("kind"),
            "staleness": rev.now,
            "claim_overall_status": rev.claim_overall_status,
            "verified_count": v_count,
            "refuted_count": r_count,
            "uncheckable_count": u_count,
            "claims": verdicts,
        })

    # Finalize: convert sets to sorted lists for stable JSON.
    refuted_summary = []
    for s in sorted(refuted_by_subject.values(),
                     key=lambda x: -x["refuted_count"]):
        s["predicates"] = sorted(s["predicates"])
        refuted_summary.append(s)

    _emit_with_memory({
        "target":                target,
        "total_notes":           len(rows),
        "legacy_note_count":     legacy_count,
        "notes_with_claims":     len(rows) - legacy_count,
        "total_claims":          total_claims,
        "verified_count":        v_total,
        "refuted_count":         r_total,
        "uncheckable_count":     u_total,
        "contradicted_note_ids": contradicted_notes,
        "refuted_by_subject":    refuted_summary,
        "notes":                 per_note,
    }, args.json, store)
    store.close()


def cmd_conflicts(args):
    """List every contradiction across all annotated targets.

    Streams over the distinct targets in the annotations table and
    reports any contradiction found. Useful as a pre-commit / CI
    gate: "are there contradicted notes in the tree?".
    """
    cfg, store = _open_store(args.path)
    from . import integrity as _intg
    targets = [r["target"] for r in store.conn.execute(
        "SELECT DISTINCT target FROM annotations")]
    by_target = []
    for t in targets:
        conflicts = _intg.detect_contradictions(store, cfg.root, t)
        if conflicts:
            by_target.append({
                "target": t,
                "conflicts": [c.to_dict() for c in conflicts],
            })
    store.close()
    total = sum(len(b["conflicts"]) for b in by_target)
    _emit({"targets_with_conflicts": len(by_target),
           "total_conflicts": total,
           "items": by_target}, args.json)


def cmd_symbol_diff(args):
    """Diff a symbol snapshot against the current index (or another
    snapshot). Mirror of `contract-diff` for the `symbols` table.

    Output: added/removed/moved sets keyed on (file, name, kind).
    """
    cfg, store = _open_store(args.path)
    _require_fresh_index(cfg, store)
    known = {s["label"] for s in store.list_symbol_snapshots()}
    if args.base not in known:
        _emit_error(
            {"error":   "snapshot-not-found",
             "message": f"snapshot {args.base!r} not found",
             "available_options": sorted(known),
             "hint": ("`projmem snapshot --symbols <name>` to create. "
                       "`pre-index` is auto-taken on each `projmem index`.")},
            args.json, store=store)
    base_rows = store.symbol_snapshot_rows(args.base)
    if args.head == "current":
        head_rows = store.live_symbol_rows()
    else:
        if args.head not in known:
            _emit_error(
                {"error":   "snapshot-not-found",
                 "message": f"snapshot {args.head!r} not found",
                 "available_options": sorted(known),
                 "hint": ("Pass a name from `available_options`, or "
                           "use `current` to compare live symbols.")},
                args.json, store=store)
        head_rows = store.symbol_snapshot_rows(args.head)

    def _key(r):
        return (r["file"], r["name"], r["kind"])

    def _site(r):
        """Site-level shape: file, line, end_line (if present), kind, name,
        symbol_id. Consumers of symbol-diff want concrete jump-to-site
        coordinates, not just the triple identity."""
        return {
            "file":       r["file"],
            "line":       r.get("line"),
            "end_line":   r.get("end_line") if hasattr(r, "get")
                          else None,
            "kind":       r["kind"],
            "name":       r["name"],
            "symbol_id":  r.get("symbol_id"),
        }

    base = {_key(r): r for r in base_rows}
    head = {_key(r): r for r in head_rows}
    added = [_site(head[k]) for k in head if k not in base]
    removed = [_site(base[k]) for k in base if k not in head]
    # "moved" = same (name, kind) but different file. Enrich with before/
    # after site coordinates so the reader sees actual file+line motion.
    moved = []
    base_by_nk: dict = {}
    head_by_nk: dict = {}
    for r in base_rows:
        base_by_nk.setdefault((r["name"], r["kind"]), []).append(r)
    for r in head_rows:
        head_by_nk.setdefault((r["name"], r["kind"]), []).append(r)
    seen_moves: set = set()
    for k in head:
        if k in base:
            continue
        nk = (k[1], k[2])
        if nk in base_by_nk and nk not in seen_moves:
            seen_moves.add(nk)
            from_sites = [_site(x) for x in base_by_nk[nk]
                          if (x["file"], x["name"], x["kind"]) not in head]
            to_sites = [_site(x) for x in head_by_nk.get(nk, [])
                        if (x["file"], x["name"], x["kind"]) not in base]
            # Pair (before, after) so a reader doesn't have to correlate
            # two parallel lists. For 1→1 moves the pair is unambiguous;
            # for split/merge cases we still produce a flat pair list
            # (every before × every after), tagged with shape, and the
            # original from_sites/to_sites stay available for richer cases.
            pairs = []
            for b in from_sites:
                for a in to_sites:
                    pairs.append({
                        "before": {"file": b["file"], "line": b["line"]},
                        "after":  {"file": a["file"], "line": a["line"]},
                    })
            shape = ("1to1" if len(from_sites) == 1 and len(to_sites) == 1
                     else "split" if len(from_sites) == 1
                     else "merge" if len(to_sites) == 1
                     else "many-to-many")
            moved.append({
                "name": k[1], "kind": k[2],
                "shape":            shape,
                "from_sites":       from_sites,
                "to_sites":         to_sites,
                "before_after":     pairs,
                # Legacy fields (deprecated, kept for backward compat):
                "from": sorted({x["file"] for x in base_by_nk[nk]}),
                "to":   k[0],
            })

    out = {"base": args.base, "head": args.head,
           "added": added[: args.limit],
           "removed": removed[: args.limit],
           "moved": moved[: args.limit],
           "summary": {"added": len(added), "removed": len(removed),
                        "moved": len(moved)}}
    store.close()
    _emit(out, args.json)


def cmd_evidence(args):
    cfg, store = _open_store(args.path)
    counts = runtime_evidence.ingest_jsonl(store, args.file)
    store.close()
    _emit(counts, args.json)


def cmd_evidence_query(args):
    """Return runtime evidence linked to a symbol or file.

    This is the first half of the L3 static↔runtime loop: given a static
    target, list the runtime events that reference it. The other half is
    `projmem drift` (static symbols with zero runtime evidence).
    """
    cfg, store = _open_store(args.path)
    # Round-5-r3 F007: gate on target resolution. Previously a typoed
    # path returned a clean `{direct_evidence: [], file_scoped_evidence:
    # []}` envelope that read as "no runtime hits — safe" when the
    # truth was "you misspelled the target."
    _resolve_target_or_hint(
        store, cfg, args.target,
        command="evidence-query", as_json=getattr(args, "json", False))
    # Evidence target matches either a file path or a symbol name.
    rows = [dict(r) for r in store.conn.execute(
        "SELECT source, target, kind, note, ts FROM evidence "
        "WHERE target=? ORDER BY ts", (args.target,))]
    # If target is a symbol, also pull evidence rows whose `target` is a file
    # that DEFINES that symbol — this catches cases where the runtime logs
    # the file path but the static index has only the symbol name.
    sym_files = [r["file"] for r in store.symbols_by_name(args.target)]
    file_rows: list = []
    for f in sym_files:
        file_rows += [dict(r) for r in store.conn.execute(
            "SELECT source, target, kind, note, ts FROM evidence "
            "WHERE target=? ORDER BY ts", (f,))]
    store.close()
    _emit({
        "target": args.target,
        "direct_evidence": rows,
        "file_scoped_evidence": file_rows,
        "symbol_defined_in": sym_files,
        "note": "Evidence is literal. Rows are whatever your runtime ingester "
                "chose to emit. `file_scoped_evidence` is the join via "
                "static def-site; cross-check for drift (static def exists, "
                "no runtime hit → see `projmem drift`).",
    }, args.json)


# Path-shape heuristic for "this 'symbol' came out of regex-indexing
# a doc/data file, not real source." Conservative — only excludes
# files whose extension or directory clearly identifies them as
# non-executable artifacts. Used by drift to drop XML-tag / markdown-
# heading / plantuml-class false positives that dominate at high
# confidence on Java repos with rich docs trees.
_NON_CODE_EXTS = (
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc",
    ".xml", ".plantuml", ".puml", ".uml", ".dot", ".mmd",
    ".html", ".htm", ".css", ".scss", ".less",
    ".csv", ".tsv", ".log", ".lock",
)
_NON_CODE_DIR_FRAGMENTS = (
    "/docs/", "/doc/", "/webapps/docs/", "/webapps/examples/",
    "/examples/", "/example/", "/changelog", "/CHANGELOG",
    "/baseline", "/__snapshots__/", "/snapshots/",
)


def _is_non_code_path(path: str) -> bool:
    if not path:
        return False
    p = path.lower()
    if any(p.endswith(ext) for ext in _NON_CODE_EXTS):
        return True
    if any(frag in "/" + p for frag in _NON_CODE_DIR_FRAGMENTS):
        return True
    return False


def cmd_drift(args):
    """Static ↔ runtime drift: defined symbols/files that runtime evidence
    never touched. The unique L3 query that no other mainstream tool gives.

    Default: structural symbols only (function/class/method/exported). The
    value is "this static def says the code path exists; your runtime never
    exercised it" — the exact failure class the round-2 report identified
    as the most expensive LLM mistake to avoid.
    """
    cfg, store = _open_store(args.path)
    targets = {r["target"] for r in store.conn.execute(
        "SELECT DISTINCT target FROM evidence")}
    total_evidence = store.conn.execute(
        "SELECT COUNT(*) FROM evidence").fetchone()[0]

    if total_evidence == 0:
        store.close()
        _emit({
            "warning": "No evidence ingested. Run `projmem evidence <file.jsonl>` "
                       "first. Without evidence, drift is meaningless.",
            "static_symbols": 0,
            "defined_never_exercised": [],
        }, args.json)
        return

    kind_clause = _kind_filter_clause(args, "kind") if not args.all_kinds else ""
    q = f"SELECT name, file, kind, line FROM symbols WHERE 1=1{kind_clause}"
    syms = [dict(r) for r in store.conn.execute(q)]
    # Filter non-source-code "symbols" by default. The regex parser
    # treats XML tags / markdown headings / plantuml class blocks as
    # `class` defs (e.g. `<loader>` in `class-loader-howto.xml`),
    # which then dominate the drift list at high confidence —
    # actively misleading. `--include-non-code` opts back in.
    include_non_code = bool(getattr(args, "include_non_code", False))
    excluded_non_code = 0
    if not include_non_code:
        before = len(syms)
        syms = [s for s in syms if not _is_non_code_path(s.get("file") or "")]
        excluded_non_code = before - len(syms)
    # F017 (round-7): exclude import-binding `var` symbols. JS/TS
    # `const foo = require('./x')` and `await import('./x')` create a
    # `var` row in the symbols table, but the binding isn't an
    # exercisable code definition — runtime evidence will never name
    # it as a target. They dominated drift output and made the
    # signal unusable on real Node.js code (191/460 noise rows in
    # the user's audit). Two paths catch them:
    #   1. names already tagged `import_binding` in the refs table
    #      (destructured form: `const {bar, baz} = require(...)`).
    #   2. var symbols whose source line is itself an import / require
    #      assignment (non-destructured form: `const foo = require(...)`).
    excluded_import_bindings = 0
    if not getattr(args, "include_import_bindings", False):
        # Names already classified as import_bindings by the indexer.
        ib_names = {r["name"] for r in store.conn.execute(
            "SELECT DISTINCT name FROM refs WHERE kind='import_binding'")}
        # For the non-destructured shape, peek at the source line.
        _IMPORT_ASSIGN_RX = re.compile(
            r"^\s*(?:const|let|var)\s+\w+\s*=\s*"
            r"(?:require\s*\(|await\s+import\s*\(|import\s*\()")
        before = len(syms)
        kept: list = []
        for s in syms:
            if s["kind"] != "var":
                kept.append(s)
                continue
            if s["name"] in ib_names:
                continue
            # Read the source line to detect the assignment shape.
            try:
                full = os.path.join(cfg.root, s["file"])
                with open(full, "r", encoding="utf-8",
                          errors="replace") as f:
                    for i, txt in enumerate(f, 1):
                        if i == int(s["line"]):
                            if _IMPORT_ASSIGN_RX.match(txt):
                                break  # filter out — drop this s
                            kept.append(s)
                            break
                        if i > int(s["line"]):
                            kept.append(s)
                            break
                    else:
                        kept.append(s)  # EOF before line — be lenient
            except OSError:
                kept.append(s)  # source unreadable — be lenient
        excluded_import_bindings = before - len(kept)
        syms = kept
    # A symbol is "exercised" if either its name or its file is in evidence targets.
    unexercised = [s for s in syms
                   if s["name"] not in targets and s["file"] not in targets]
    store.close()
    _emit({
        "evidence_targets": sorted(t for t in targets if t),
        "total_evidence_rows": total_evidence,
        "static_symbols": len(syms),
        "non_code_filtered": excluded_non_code,
        "import_bindings_filtered": excluded_import_bindings,
        "defined_never_exercised": unexercised[: args.limit],
        "drift_count": len(unexercised),
        "note": "Defined statically but matched by no runtime evidence. "
                "Heuristic. False positives: symbols called only via dynamic "
                "dispatch, test-only code, rarely-hit branches. Real positives: "
                "dead code, unreachable guards, features the runtime never "
                "took. Works only as well as your runtime ingester's "
                "coverage — if the ingester only records top-level entrypoint "
                "names, only those will count as 'exercised'.",
    }, args.json)


def _emit(obj, as_json: bool):
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(json.dumps(obj, indent=2, default=str))  # default to JSON, still readable


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="projmem",
        description="Drift-aware code memory for AI agents. "
                    "Run `projmem` with no subcommand to see memory status "
                    "+ next-step guidance; `projmem usage` for the full "
                    "agent-facing command catalog.")
    p.add_argument("--path", default=".", help="Project root (default: cwd)")
    p.add_argument("--json", action="store_true",
                   help="Force JSON output (default output is also JSON).")
    # Subcommand is OPTIONAL so `projmem` alone can surface memory status —
    # the natural discovery path when an agent types the bare tool name.
    # Subcommands stay fully explicit below.
    sub = p.add_subparsers(dest="cmd", required=False)

    # `--json` and `--path` get injected into EVERY subparser AND every
    # NESTED subparser (note add, task start, refute submit, etc.) so
    # users can write `projmem note add target body --path X --json`
    # without argparse rejecting the late globals. We patch
    # `add_subparsers` so that any subparser created (top-level or
    # nested) returns objects whose `add_parser` injects the globals.
    def _wrap_subparsers_action(action):
        orig_add_parser = action.add_parser

        def _add_parser(name, **kwargs):
            sp = orig_add_parser(name, **kwargs)
            # default=SUPPRESS so the subparser doesn't overwrite the
            # global namespace value when the flag isn't repeated.
            sp.add_argument("--json", action="store_true", dest="json",
                            default=argparse.SUPPRESS,
                            help=argparse.SUPPRESS)
            sp.add_argument("--path", default=argparse.SUPPRESS,
                            help=argparse.SUPPRESS)
            # Recurse: any sub-subparsers this leaf creates inherit the
            # same wrapping. add_subparsers is itself a method that we
            # need to intercept once it's called on `sp`.
            orig_add_subparsers = sp.add_subparsers

            def _add_subparsers_wrapped(*args, **kwargs2):
                nested = orig_add_subparsers(*args, **kwargs2)
                _wrap_subparsers_action(nested)
                return nested
            sp.add_subparsers = _add_subparsers_wrapped
            return sp
        action.add_parser = _add_parser
        return action

    _wrap_subparsers_action(sub)

    s = sub.add_parser("index", help="Build/refresh the index.")
    s.add_argument("--force", action="store_true")
    s.add_argument("--exclude", action="append", metavar="GLOB",
                   help="Skip files/dirs matching this glob. Repeatable. "
                        "Matches `node_modules/**`, `chrome/src/*`, basenames like "
                        "`vendor`, etc. Also merges with "
                        "`.projmem/config.json::exclude_globs`.")
    s.add_argument("--include", action="append", metavar="GLOB",
                   help="Only index files matching this glob. Repeatable.")
    s.add_argument("--exclude-wins", action="store_true",
                   help="EXCLUDE always trumps INCLUDE. Lets you `--include "
                        "deploy/**` AND `--exclude deploy/patches/**` to "
                        "keep wrapper scripts but drop a vendor subtree. "
                        "Default behavior (include-wins) is unchanged when "
                        "this flag is absent.")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("stats", help="Show index stats.")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("symbol", help="Look up a symbol (defs + refs).")
    s.add_argument("name")
    s.add_argument("--file", metavar="PATH",
                   help="Disambiguate same-name symbols by restricting the "
                        "definition lookup to PATH. Equivalent to `PATH#name` "
                        "shorthand.")
    s.add_argument("--allow-ambiguous", action="store_true",
                   help="Allow ambiguous bare-name lookups (multiple defs). "
                        "Without this flag, projmem refuses to answer and "
                        "requires disambiguation to prevent false attachment.")
    s.add_argument("--role", metavar="ROLES",
                   help="Comma-separated role filter. Values: read, write, "
                        "call, new, import, import_binding, callback, "
                        "shorthand, test. E.g. `--role write,call`.")
    s.add_argument("--context", default=0, metavar="N",
                   help="Attach N lines of source-code context around each "
                        "def/ref site. Saves a round-trip when triaging. "
                        "Use `--context auto` to pick a sensible default "
                        "(2 lines in --json mode for agents, 0 otherwise). "
                        "Default: 0 (no snippets) for backward compat.")
    s.add_argument("--no-implicit-check", action="store_true",
                   help="Skip the word-boundary text scan that flags potential "
                        "macro / string-based / generated usage sites. Use "
                        "when the extra ~100ms-per-MB scan is unwanted.")
    s.add_argument("--exhaustive", action="store_true",
                   help="Full text scan across every indexed file. Returns "
                        "`text_matches` (rg -w parity), `implicit_refs`, and "
                        "structured-vs-text coverage. Can be expensive on "
                        "large repos; use only when you need exhaustiveness.")
    s.add_argument("--exhaustive-max-matches", type=int, default=5000, metavar="N",
                   help="Max match sites to include in `text_matches` output. "
                        "Counts remain exact even if truncated.")
    s.add_argument("--exhaustive-no-line-text", action="store_true",
                   help="Omit per-match `text` line payload from "
                        "`text_matches` rows (smaller JSON).")
    s.add_argument("--include-artifacts", action="store_true",
                   help="Include refs from build output / snapshots / "
                        "changelogs / baselines in the primary `refs` list. "
                        "Default: artifacts move to `artifact_refs` so the "
                        "blast-radius count reflects real source consumers.")
    s.add_argument("--strict-ambiguity", action="store_true",
                   help="Return error=ambiguous-symbol on multi-def names. "
                        "Default: soft ambiguity — pick a primary guess, "
                        "surface candidates under `ambiguity_warning`.")
    s.set_defaults(func=cmd_symbol)

    s = sub.add_parser("forward",
                       help="Outbound deps only — what does this file import?")
    s.add_argument("target")
    s.set_defaults(func=cmd_forward)

    s = sub.add_parser("reverse", help="Reverse + forward deps for a file.")
    s.add_argument("target")
    s.add_argument("--include-artifacts", action="store_true",
                   help="Include build output / snapshot / changelog / "
                        "baseline paths in the primary dep lists. Default: "
                        "those move to `artifact_reverse_dependencies`.")
    s.add_argument("--no-auto-refresh", action="store_true",
                   help="Skip the lazy refresh on the target file. Default "
                        "behavior re-indexes the target if its on-disk "
                        "hash drifted before answering — guarantees "
                        "current results.")
    s.set_defaults(func=cmd_reverse)

    s = sub.add_parser("pack", help="Build a context pack for a target (file or symbol).")
    s.add_argument("target")
    s.add_argument("--radius", type=int, default=1)
    s.add_argument("--no-tests", action="store_true")
    s.add_argument("--write", action="store_true",
                   help="Write pack to .projmem/packs/.")
    s.add_argument("--markdown", action="store_true",
                   help="Also render a Markdown view.")
    s.add_argument("--name", default=None)
    s.add_argument("--as-file", action="store_true",
                   help="Force file-target interpretation (overrides auto-detect).")
    s.add_argument("--as-symbol", action="store_true",
                   help="Force symbol-target interpretation.")
    s.add_argument("--snippets", action="store_true",
                   help="Include bounded source-code excerpts around the "
                        "target (skip a separate file-read round-trip).")
    s.add_argument("--snippet-bytes", type=int, default=8000,
                   help="Hard byte budget for `--snippets` payload.")
    s.add_argument("--budget", type=int, default=None, metavar="TOKENS",
                   help="Cap the total pack output to ~N tokens "
                        "(estimated at 3.5 chars/token). Sections drop "
                        "or shrink in reverse priority — target / direct "
                        "deps / notes survive when the budget bites.")
    s.add_argument("--include-source", action="store_true",
                   help="When target is a symbol with end_line, include "
                        "the full function body in `target_body` field. "
                        "Bypasses snippet byte budget.")
    s.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                   help="Abort pack construction after SECONDS wall-clock "
                        "seconds. Returns a partial pack with timed_out=true "
                        "and timeout_truncated_at indicating where it stopped. "
                        "Always returns usable output even when truncated.")
    s.add_argument("--include-artifacts", action="store_true",
                   help="Include build output / snapshot / changelog / "
                        "baseline paths in the primary direct/reverse-dep "
                        "lists. Default: those move to "
                        "`artifact_reverse_dependencies` so the blast-radius "
                        "view reflects real source consumers.")
    s.set_defaults(func=cmd_pack)

    s = sub.add_parser("contracts", help="Show contracts in a file or by name.")
    s.add_argument("target")
    s.add_argument("--kind",
                   choices=["flag", "env", "schema_field", "token", "event",
                            "guard", "entrypoint", "script", "dep",
                            "enum_shape"],
                   default=None)
    s.set_defaults(func=cmd_contracts)

    s = sub.add_parser("entrypoints", help="List detected/declared entrypoints.")
    s.add_argument("--hide-unindexed", action="store_true",
                   help="Suppress entries whose target file is not in the index.")
    s.set_defaults(func=cmd_entrypoints)

    s = sub.add_parser("explain",
                       help="Pack for a target. JSON by default (matches "
                            "every other read command); add --markdown "
                            "for the human-readable rendering.")
    s.add_argument("--markdown", action="store_true",
                   help="Emit the human-readable markdown rendering "
                        "instead of JSON. Default until this round was "
                        "markdown — flipped because pipelines expecting "
                        "JSON broke silently.")
    s.add_argument("target")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser(
        "refresh",
        help="Detect AND APPLY changes vs. the index. Reindexes "
             "modified/added files and removes deleted ones. Pass "
             "`--detect-only` to look without applying.")
    # `--reindex` kept as a no-op for back-compat (now the default).
    s.add_argument("--reindex", action="store_true",
                   help="(Deprecated; this is now the default. "
                        "Kept so existing scripts don't break.)")
    s.add_argument("--detect-only", action="store_true",
                   help="Detect changes but do NOT update the index. "
                        "Use when you want a dry-run preview.")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser(
        "changes",
        help="Show files edited since last session (reads append-only "
             "edit log; survives pre-index snapshot rotation).")
    s.add_argument("--since", default=None,
                   help="Baseline snapshot label for diff mode. Only "
                        "use this when you want symbol-/contract-level "
                        "diffs against a specific snapshot. Default mode "
                        "(no flag) reads the durable edit log.")
    s.add_argument("--hours", type=float, default=None,
                   help="Edits from the last N hours (alternative to "
                        "the default 'last index session' view).")
    s.add_argument("--last", type=int, default=None,
                   help="Most recent N edit-log entries regardless of "
                        "session/time.")
    s.add_argument("--all-sessions", action="store_true",
                   help="Dump every edit-log row (capped by --limit).")
    s.add_argument("--limit", type=int, default=50,
                   help="Max files to return. Default 50.")
    s.add_argument("--per-file-limit", type=int, default=25,
                   help="Max added/removed entries per file in diff mode.")
    s.add_argument("--include-unchanged", action="store_true",
                   help="Diff mode only: include files with no drift.")
    s.set_defaults(func=cmd_changes)

    s = sub.add_parser("status", help="Store + staleness status.")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("git", help="Constrained git info for a path.")
    s.add_argument("target")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_git)

    s = sub.add_parser("callees-of",
                       help="Transitive callees of a symbol (blast radius).")
    s.add_argument("symbol")
    s.add_argument("--file", help="Disambiguate when the symbol has multiple defs.")
    s.add_argument("--depth", type=int, default=3)
    s.add_argument("--limit", type=int, default=1000,
                   help="Max unique reachable nodes before truncating.")
    s.set_defaults(func=cmd_callees_of)

    s = sub.add_parser("callgraph",
                       help="Intra-file call graph (function → function) for one file. "
                            "Pass --cross-file to include edges to symbols defined in "
                            "other files.")
    s.add_argument("file")
    s.add_argument("--filter-to", metavar="SYMBOL",
                   help="Show only incoming + outgoing edges for this symbol.")
    s.add_argument("--in-function", metavar="FN",
                   help="Show only calls made FROM inside FN (in source-line order). "
                        "Use this to audit event-loop / listener-registration ordering.")
    s.add_argument("--cross-file", action="store_true",
                   help="Also include edges to symbols defined in OTHER files. "
                        "Cross-file edges are tagged `cross_file: true` with "
                        "`to_file` and a confidence label (high/medium/low "
                        "depending on ref binding and name ambiguity).")
    s.add_argument("--limit", type=int, default=300,
                   help="Max edges to return (default 300). by_caller_count "
                        "is ALWAYS computed from the full edge set.")
    s.add_argument("--all", action="store_true",
                   help="Disable edge truncation (equivalent to --limit 0).")
    s.set_defaults(func=cmd_callgraph)

    s = sub.add_parser("reach",
                       help="Under what guard conditions is a symbol called? (Python only).")
    s.add_argument("symbol")
    s.set_defaults(func=cmd_reach)

    s = sub.add_parser("contract-drift",
                       help="Find contract names whose values vary across sites.")
    s.add_argument("--kind", choices=["flag", "env", "schema_field", "token",
                                      "event", "guard", "entrypoint",
                                      "script", "dep", "enum_shape"])
    s.add_argument("--scope-only", action="store_true",
                   help="Filter out vendor/third-party rows (in_scope=False). "
                        "Defaults: chrome/, node_modules/, vendor/, "
                        "third_party/, _archive/, build/, dist/, out/, target/, "
                        "Pods/, plus any --exclude'd dirs from the last "
                        "index session.")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_contract_drift)

    s = sub.add_parser("events",
                       help="List (event-name → emitters, listeners) in the repo.")
    s.add_argument("--name", help="Filter to a single event name.")
    s.add_argument("--file", help="Filter to files whose path contains this substring.")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("orphans", help="Symbols defined but referenced nowhere.")
    s.add_argument("--exported-only", action="store_true",
                   help="Only list symbols marked exported.")
    s.add_argument("--kind", metavar="KINDS",
                   help="Comma-separated symbol kinds to include. Default: "
                        "structural kinds (function,class,method,exported,var,"
                        "interface,type,enum,struct,trait,module,object).")
    s.add_argument("--all-kinds", action="store_true",
                   help="Disable the structural-kind filter.")
    s.add_argument("--limit", type=int, default=500)
    s.set_defaults(func=cmd_orphans)

    s = sub.add_parser("parity", help="referenced-but-undefined + defined-but-unreferenced.")
    s.add_argument("--lang", default=None, metavar="LANGS",
                   help="Comma-separated language filter on the dangling-ref "
                        "side (c,cpp,js,ts,python,go,rust,...). Drops noise "
                        "from Makefile/license/Perl-script files in mixed "
                        "repos.")
    s.add_argument("--kind", metavar="KINDS",
                   help="Same as orphans --kind; filters the "
                        "defined_but_unreferenced side only.")
    s.add_argument("--all-kinds", action="store_true")
    s.add_argument("--include-builtins", action="store_true",
                   help="Include language builtins (String, console, length, "
                        "print, ...) in `referenced_but_undefined`. Default "
                        "filters them since they produce 100%% noise otherwise.")
    s.add_argument("--include-non-code", action="store_true",
                   help="Include refs from doc / config / other non-code files "
                        "(markdown, .cursorrules, AGENTS.md, TOML, YAML). "
                        "Default EXCLUDES them — the regex backend over prose "
                        "generates spurious `call` refs on English words. "
                        "Opt in only if you want the raw unfiltered view.")
    s.add_argument("--limit", type=int, default=500)
    s.set_defaults(func=cmd_parity)

    s = sub.add_parser("scope",
                       help="Print the EXACT effective scope of the last index run.")
    s.set_defaults(func=cmd_scope)

    s = sub.add_parser("missing-paths",
                       help="Repo-wide check for referenced files that don't exist.")
    s.add_argument("--scope-only", action="store_true",
                   help="Drop rows whose SOURCE file is under a vendor prefix "
                        "(chrome/, node_modules/, vendor/, third_party/, ...). "
                        "Round-X fix: chrome/-tree alone produced 148 vendor "
                        "fixture rows when this flag was missing.")
    s.add_argument("--limit", type=int, default=500)
    s.set_defaults(func=cmd_missing_paths)

    s = sub.add_parser("files",
                       help="Print the indexed file list + effective "
                            "scope. Use --glob / --lang / --limit to "
                            "narrow on big repos (default dumps all).")
    s.add_argument("--glob", default=None, metavar="PATTERN",
                   help="fnmatch-style filter on the relative path "
                        "(e.g. `lib/**/*.js`, `tests/*.py`).")
    s.add_argument("--lang", default=None, metavar="CSV",
                   help="Comma-separated language filter "
                        "(e.g. `java`, `python,go,rust`).")
    s.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Cap output to first N files (post-filter). "
                        "Default 0 = unlimited.")
    s.set_defaults(func=cmd_files)

    s = sub.add_parser("unresolved-imports",
                       help="Repo-wide list of imports that couldn't be resolved to a file.")
    s.add_argument("--only-missing-on-disk", action="store_true",
                   help="Only show specs that look like a relative path but "
                        "aren't on disk — likely typos or moved files.")
    s.add_argument("--kind", metavar="KINDS",
                   help="Comma-separated import kinds to include: "
                        "repo_relative, external_module, external_include, "
                        "external_root_import. Default: hides "
                        "external_include + external_root_import.")
    s.add_argument("--show-external", action="store_true",
                   help="Include vendor/system headers (`external_include`, "
                        "`external_root_import`) — hidden by default.")
    s.add_argument("--scope-only", action="store_true",
                   help="Drop rows whose source file is under a vendor prefix.")
    s.add_argument("--limit", type=int, default=500)
    s.set_defaults(func=cmd_unresolved_imports)

    s = sub.add_parser("snapshot",
                       help="Freeze contracts (and optionally symbols) under a label.")
    s.add_argument("label", nargs="?", default=None,
                   help="Snapshot label (default 'manual'). "
                        "`pre-index` is auto-taken on every `projmem index` "
                        "for both contracts and symbols.")
    s.add_argument("--symbols", action="store_true",
                   help="Also freeze the symbol table for `symbol-diff`.")
    s.add_argument("--list", action="store_true",
                   help="List saved snapshots and exit.")
    s.add_argument("--delete", metavar="LABEL",
                   help="Delete a snapshot by label.")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("contract-diff",
                       help="Diff a contract snapshot against the current index.")
    s.add_argument("--base", default="pre-index",
                   help="Base snapshot label (default: 'pre-index', "
                        "auto-taken on every `projmem index`).")
    s.add_argument("--head", default="current",
                   help="Head snapshot label or 'current' for live contracts "
                        "(default: 'current').")
    s.add_argument("--kind", metavar="KINDS",
                   help="Comma-separated contract kinds to diff. Default: "
                        "flag,env,schema_field,event,entrypoint. `token` "
                        "and `guard`/`dep`/`script` are excluded unless "
                        "named explicitly.")
    s.add_argument("--as-obligations", action="store_true",
                   help="Project the diff into the obligation schema — "
                        "one open_obligation per orphan-added or "
                        "dangling-removed contract.")
    s.set_defaults(func=cmd_contract_diff_vs_base)

    # symbol-diff: structural-symbol counterpart to contract-diff.
    s = sub.add_parser("symbol-diff",
                       help="Diff symbols (defs) between two snapshots — "
                            "shows added/removed/moved structural symbols.")
    s.add_argument("--base", default="pre-index",
                   help="Base snapshot label (default 'pre-index').")
    s.add_argument("--head", default="current",
                   help="Head snapshot label or 'current' (default).")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_symbol_diff)

    s = sub.add_parser("trace",
                       help="EXPERIMENTAL — call-chain BFS over name-level "
                            "refs. Same-name collisions are over-approximated; "
                            "for trustworthy reverse-by-symbol use `projmem "
                            "reverse <file>` or `projmem analyze-change "
                            "<target>` instead. Kept for power-user investigation.")
    s.add_argument("source",
                   help="Source symbol (bare name, file#name, or SCIP id).")
    s.add_argument("sink",
                   help="Sink symbol (bare name, file#name, or SCIP id).")
    s.add_argument("--max-hops", type=int, default=5,
                   help="BFS depth cap. Default 5.")
    s.add_argument("--via", default=None,
                   help="Restrict caller files to those whose path "
                        "contains this substring.")
    s.add_argument("--mode", choices=["strict", "relaxed"], default="strict",
                   help="Edge filter: strict (call-only, default) or "
                        "relaxed (call+new+callback). Never crosses "
                        "import/read/shorthand edges.")
    s.add_argument("--experimental", action="store_true",
                   help="Required opt-in. The output looks authoritative "
                        "but is name-level (same-name collisions merged "
                        "into one logical node). Use only when "
                        "`reverse`/`analyze-change`/`pack` cannot answer "
                        "your question and you accept the noise.")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("audit-trail",
                       help="Show recent projmem commands run against this index.")
    s.add_argument("--target", default=None,
                   help="Filter by target file or `file#symbol` shorthand.")
    s.add_argument("--command", default=None,
                   help="Filter by command name (pack, symbol, ...).")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_audit_trail)

    # Annotations: human/agent assertions that survive reindex and
    # surface in `pack` output. See cmd_note for full description.
    s = sub.add_parser("note",
                       help="Manage annotations on files / symbols "
                            "(add | list | delete | search).")
    nsub = s.add_subparsers(dest="note_action", required=True)

    sa = nsub.add_parser("add", help="Add a note on a target.")
    sa.add_argument("target",
                    help="File path, symbol_id, or `file#name` shorthand.")
    sa.add_argument("--kind", required=True,
                    help=("note | refute | verified-safe | "
                          "documented-footgun | todo | link | risk "
                          "(unknown values warned but accepted)."))
    sa.add_argument("body",
                    help="Free-text annotation body.")
    sa.add_argument("--author", default=None,
                    help="Optional author / session identifier.")
    sa.add_argument("--expires-days", type=int, default=None,
                    help="Optional expiry in days. Default: never expires.")
    sa.add_argument("--evidence", action="append", default=None,
                    help="file:line[:note] — cite the evidence that "
                         "justifies the claim. Repeatable. Notes with "
                         "evidence start at higher confidence.")
    sa.add_argument("--confidence", type=float, default=None,
                    help="0.0–1.0 confidence in the claim. "
                         "Default: 0.5 (neutral).")
    sa.add_argument("--truth-class", default=None,
                    choices=["FACT", "INFERENCE", "ASSUMPTION", "UNKNOWN"],
                    help="Taxonomic classification of the claim "
                         "(SPEC #15). Default: INFERENCE.")
    sa.add_argument("--scope", default=None,
                    choices=["symbol", "file", "subsystem"],
                    help="Scope at which the claim applies. "
                         "Higher scope survives lower-level changes.")
    sa.add_argument("--claims", default=None, metavar="FILE_OR_JSON",
                    help="Path to a JSON file (or a JSON string) containing "
                         "a list of structured claims. Each claim must have "
                         "subject, predicate, and object keys. See "
                         "projmem.claims.PREDICATES for supported predicates. "
                         "Claims are appended to --evidence; both can be used.")
    sa.add_argument("--allow-unknown-predicates", action="store_true",
                    help="Store a claim whose predicate isn't in the known "
                         "catalog. Default: reject (the claim would be "
                         "UNCHECKABLE forever, polluting memory). Use "
                         "`projmem note predicates` to see the catalog.")
    sa.add_argument("--no-auto-extract", action="store_true",
                    help="Disable auto-extraction of structured claims "
                         "from the note body. Default: ON — bodies "
                         "containing `@predicate(s, o)` OR NL forms "
                         "like \"`X` is defined at file:line\" become "
                         "structured claims for free. This was the "
                         "round-7-bench fix: agents wrote prose; "
                         "verifier never saw structured input. Now "
                         "they get FACT claims even from prose.")
    sa.add_argument("--allow-off-repo-target", action="store_true",
                    help="Store a note whose target is a path that "
                         "doesn't match any indexed file. Default: "
                         "reject (the note would be unverifiable and "
                         "would never re-validate). Use for claims about "
                         "future files or external context you "
                         "deliberately want pinned.")
    sa.set_defaults(func=cmd_note)

    sl = nsub.add_parser("list", help="List annotations.")
    sl.add_argument("target", nargs="?", default=None,
                    help="Optional exact-target filter.")
    # Accept --target as well for API symmetry with `note-verify`,
    # which is what the benchmark v2 agent reached for. Positional
    # still works; the flag form wins if both are supplied.
    sl.add_argument("--target", dest="target_flag", default=None,
                    help="Same as positional target; added for "
                         "symmetry with `note-verify`.")
    sl.add_argument("--author", default=None,
                    help="Filter to annotations authored by the given "
                         "name (exact match).")
    sl.add_argument("--kind", default=None)
    sl.add_argument("--limit", type=int, default=None,
                    help="Max notes to return. Default: unlimited. "
                         "Use this to cap context cost on seeded / "
                         "high-volume note sets.")
    sl.add_argument("--fields", default=None,
                    help="Comma-separated subset of fields to include "
                         "per note (e.g. `id,target,body`). Drops "
                         "fingerprint/evidence JSON by default when "
                         "set, reducing output size ~10x on busy "
                         "repos. When omitted, returns every field.")
    sl.add_argument("--include-expired", action="store_true")
    sl.set_defaults(func=cmd_note)

    sd = nsub.add_parser("delete", help="Delete an annotation by id.")
    sd.add_argument("id", type=int)
    sd.set_defaults(func=cmd_note)

    ss = nsub.add_parser("search",
                         help="Substring search across body / target / kind.")
    ss.add_argument("query")
    ss.add_argument("--include-expired", action="store_true")
    ss.set_defaults(func=cmd_note)

    sh = nsub.add_parser("show",
                         help="Fetch one note by id (full body + all fields; "
                              "complements `note list` which truncates body).")
    sh.add_argument("id", type=int)
    sh.set_defaults(func=cmd_note)

    sp = nsub.add_parser("predicates",
                         help="List the structured-claim predicates the "
                              "verifier knows about. Use these in your "
                              "--claims JSON; unknown predicates are "
                              "rejected unless --allow-unknown-predicates.")
    sp.set_defaults(func=cmd_note)

    # task — Tier 1B session-continuity. Session 2 runs `task resume`
    # first to see what session 1 was doing.
    s = sub.add_parser("task",
                        help="Session-continuity task state. Verbs: "
                             "start / step / blocked / unblock / close / "
                             "resume / list.")
    tsub = s.add_subparsers(dest="task_action", required=True)

    ts = tsub.add_parser("start", help="Open a new task with a goal.")
    ts.add_argument("goal", help="What you're trying to accomplish.")
    ts.add_argument("--author", default=None)
    ts.set_defaults(func=cmd_task)

    ts = tsub.add_parser("step",
                          help="Append a progress step. Default targets "
                               "the LIFO active task — pass --task-id "
                               "when 2+ tasks are open to avoid race.")
    ts.add_argument("detail")
    ts.add_argument("--task-id", type=int, default=None,
                     help="Explicit task id. ESSENTIAL with multiple "
                          "open tasks (otherwise step routes to "
                          "whichever was created last).")
    ts.add_argument("--ref", default=None,
                     help="Optional file path or note id cited.")
    ts.set_defaults(func=cmd_task)

    ts = tsub.add_parser("blocked",
                          help="Mark a task blocked on a question. Default "
                               "targets LIFO active — use --task-id when "
                               "2+ open.")
    ts.add_argument("detail", help="What you're blocked on.")
    ts.add_argument("--task-id", type=int, default=None,
                     help="Explicit task id. ESSENTIAL with multiple "
                          "open tasks.")
    ts.set_defaults(func=cmd_task)

    ts = tsub.add_parser("unblock",
                          help="Move a blocked task to active. Default "
                               "targets LIFO blocked — use --task-id "
                               "when 2+ blocked.")
    ts.add_argument("--detail", default=None)
    ts.add_argument("--task-id", type=int, default=None,
                     help="Explicit task id.")
    ts.set_defaults(func=cmd_task)

    ts = tsub.add_parser("close",
                          help="Close a task as done. With no argument: "
                               "closes 'the' active task (LIFO if more "
                               "than one open). Pass an integer or "
                               "--task-id to target a specific task — "
                               "essential when 2+ are open.")
    ts.add_argument("close_target", nargs="?", default=None,
                     help="Bare integer = task id; omit to close LIFO active.")
    ts.add_argument("--task-id", type=int, default=None,
                     help="Explicit task id (wins over the positional).")
    ts.add_argument("--detail", default=None,
                     help="Optional close summary.")
    ts.set_defaults(func=cmd_task)

    ts = tsub.add_parser("resume",
                          help="Show open tasks + their progress. Run first in "
                               "a fresh session to see where you left off.")
    ts.add_argument("--limit", type=int, default=5)
    ts.set_defaults(func=cmd_task)

    ts = tsub.add_parser("list", help="List tasks.")
    # Round-5-r3 F013: `open` is the natural English word for "not
    # closed yet" — accept it as an alias of `active`. Same for
    # `closed` ↔ `done`. The cmd_task layer normalizes before the DB
    # query so the underlying status enum stays unchanged.
    ts.add_argument("--status",
                     choices=["active", "open", "blocked", "done", "closed"],
                     default=None,
                     help="Filter by status. `open` aliases `active`, "
                          "`closed` aliases `done`.")
    ts.add_argument("--limit", type=int, default=50)
    ts.add_argument("--verbose", action="store_true",
                     help="Inline each task's full event arc (steps, "
                          "blockers, close summary). Default off so the "
                          "list stays compact; without it, `task step` "
                          "becomes a write-only log.")
    ts.set_defaults(func=cmd_task)

    # conclude-session — Tier 1A. Closes the capture gap: feed a
    # session transcript at session end, projmem auto-extracts durable
    # conclusions and saves them as notes.
    s = sub.add_parser(
        "conclude-session",
        help="Extract durable conclusions from a conversation "
             "transcript and save them as notes. Run at session end.")
    s.add_argument("--transcript", required=True,
                   help="Path to transcript file, or '-' for stdin.")
    s.add_argument("--author", default=None,
                   help="Override the default 'session-<timestamp>' "
                        "author.")
    s.add_argument("--dry-run", action="store_true",
                   help="Show what would be saved without writing.")
    s.set_defaults(func=cmd_conclude_session)

    # Fact-check — Tier 0 pre-output verification. Extracts claims from
    # arbitrary text and runs them through the same verifier that
    # note-verify uses. Exits with code 2 on any REFUTED claim so CI or
    # agent wrappers can gate on it.
    s = sub.add_parser(
        "fact-check",
        help="Verify claims in arbitrary text against the current index. "
             "Exit code 2 if any claim is REFUTED. Run before shipping "
             "a non-trivial answer. Pass --diff <patch> to check a "
             "unified diff against saved note claims instead.")
    s.add_argument("text", nargs="?", default="",
                   help="The text to check. Use '-' to read from stdin.")
    s.add_argument("--file", default=None,
                   help="Read text from a file instead of the CLI arg.")
    s.add_argument("--diff", default=None, metavar="PATCH",
                   help="Unified-diff mode: classify saved note claims "
                        "as `at_risk` / `moved` / `unaffected` against "
                        "the patch. Path or `-` for stdin. Exit 2 on "
                        "any at_risk claim — wrap pre-commit hooks "
                        "with `git diff HEAD | projmem fact-check "
                        "--diff - || exit 2`.")
    s.set_defaults(func=cmd_fact_check)

    # Round-6: `verify-text` is an agent-friendly alias for `fact-check`.
    # Same handler, same flags — added because audit feedback showed
    # agents reach for "verify" in NL but not "fact-check."
    s = sub.add_parser(
        "verify-text",
        help="Alias of `fact-check`. Same handler / flags / exit codes.")
    s.add_argument("text", nargs="?", default="",
                   help="The text to check. Use '-' to read from stdin.")
    s.add_argument("--file", default=None,
                   help="Read text from a file instead of the CLI arg.")
    s.add_argument("--diff", default=None, metavar="PATCH",
                   help="Same as `fact-check --diff`.")
    s.set_defaults(func=cmd_fact_check)

    # Round-6 single-shot wrapper: `projmem check`. Trivial-cost path
    # for agents who just want a verdict, not the full claims/parse-
    # errors/bare-line envelope.
    s = sub.add_parser(
        "check",
        help="Single-shot fact-check. Returns ONLY verdict + counts; "
             "no per-claim detail. Use for cheap one-off verification.")
    s.add_argument("text", nargs="?", default="",
                   help="The text to check. Use '-' to read from stdin.")
    s.set_defaults(func=cmd_check)

    # Round-6 claim-authoring shortcut: `projmem check-line file:line
    # symbol`. Builds the @defined-at claim for you and runs `check`.
    s = sub.add_parser(
        "check-line",
        help="Claim-authoring shortcut: `check-line file:line symbol` "
             "verifies that <symbol> is defined at <file>:<line>.")
    s.add_argument("file_line",
                   help="`<file>:<line>` — e.g. src/foo.ts:10")
    s.add_argument("symbol",
                   help="Symbol name claimed to live at that location.")
    s.set_defaults(func=cmd_check_line)

    # Round-6 cursor lookup: `projmem at file:line[:col]`. The MCP /
    # editor / LSP primitive — "what does projmem know about the thing
    # my cursor is on?" in one call.
    s = sub.add_parser(
        "at",
        help="Cursor-position lookup: `at file:line[:col]` returns the "
             "enclosing symbol's pack (defs + refs + neighbors). The "
             "primitive editor / LSP / MCP integrations need.")
    s.add_argument("cursor",
                   help="`<file>:<line>[:<col>]` — e.g. src/foo.ts:42:5")
    s.set_defaults(func=cmd_at)

    # Seed — cold-start auto-population of INFERENCE notes on hot files,
    # gateway symbols, cross-layer enum surfaces, and re-export barrels.
    # Runs on a fresh-indexed repo so session 2 isn't starting from zero.
    s = sub.add_parser(
        "seed",
        help="Auto-populate INFERENCE-class notes on graph-shape heuristics "
             "(hot files, gateway symbols, cross-layer enums, barrels) so "
             "a fresh repo has memory for the next session. Idempotent.")
    s.add_argument("--max-notes", type=int, default=15,
                   help="Upper bound on seed notes created. Default 15.")
    s.add_argument("--dry-run", action="store_true",
                   help="Preview what would be seeded — don't write. "
                        "Returns the same `created` list with `id: null` "
                        "and `dry_run: true` per row so you can review "
                        "before committing. Useful on low-binding repos "
                        "where heuristic-picked targets may be noise.")
    s.set_defaults(func=cmd_seed)

    # Guide — on-demand deep docs. Kept short so the templates stay
    # light; detail lives here and is pulled only when needed.
    s = sub.add_parser(
        "guide",
        help="Deep docs on demand: `projmem guide workflow | commands | "
             "capture | signals`. Called when the quick-ref in AGENTS.md "
             "isn't enough.")
    s.add_argument("topic", nargs="?", default="",
                   help="workflow | commands | capture | signals. "
                        "Empty → list available topics.")
    s.set_defaults(func=cmd_guide)

    # Ask — natural-language query dispatcher. Routes common question
    # shapes to the right underlying command and synthesizes a one-line
    # answer. Meant for agents that don't know the full command catalog.
    s = sub.add_parser(
        "ask",
        help="Natural-language query. `projmem ask \"who uses X?\"` or "
             "`projmem ask \"what changed?\"` picks the right command "
             "for you.")
    s.add_argument("question",
                   help="The question in plain English. Common shapes: "
                        "'who uses X', 'what does X do', 'where is X "
                        "defined', 'what changed', 'safe to delete X', "
                        "'verify notes on X', 'repo overview'.")
    s.set_defaults(func=cmd_ask)

    # Conclude is sugar over `note add` that parses @<predicate>(subject,
    # object) inline claims directly from the body. Lowers capture cost
    # from "write JSON, pass --claims" to one line of prose.
    s = sub.add_parser(
        "conclude",
        help="Save a durable conclusion. Inline `@<predicate>(<subject>, "
             "<object>)` patterns in the body become structured claims "
             "automatically — no JSON file needed.")
    s.add_argument("body",
                   help="Free-text conclusion. Embed claims inline as "
                        "@defined-at(name, file:line), "
                        "@exported-from(name, file), etc. First cited "
                        "file path is used as target unless --target "
                        "is passed.")
    s.add_argument("--target", default=None,
                   help="Explicit target (file / symbol_id / file#name). "
                        "Default: first file path cited in the body, "
                        "or `@project` if none.")
    s.add_argument("--kind", default="note",
                   help="Note kind (default: note).")
    s.add_argument("--truth-class", default="FACT",
                   choices=["FACT", "INFERENCE", "ASSUMPTION", "UNKNOWN"],
                   help="Default FACT for conclude (caller is asserting).")
    s.add_argument("--confidence", type=float, default=0.85)
    s.add_argument("--author", default=None)
    s.add_argument("--expires-days", type=int, default=None)
    s.add_argument("--evidence", action="append", default=None,
                   help="Extra evidence citation (file:line[:note]). "
                        "Repeatable. Merges with inline claims.")
    s.add_argument("--claims", default=None,
                   help="Optional path to a JSON claims file (same as "
                        "`note add --claims`); merges with inline claims.")
    s.add_argument("--no-verify", action="store_true",
                   help="Skip the pre-persist fact-check gate. Default: "
                        "conclude runs fact-check on the body first and "
                        "aborts with exit code 3 if any inline FACT "
                        "claim is REFUTED. Bypass only when you "
                        "deliberately want to save a currently-wrong "
                        "claim (rare).")
    s.set_defaults(func=cmd_conclude)

    # Refute is sugar over `note add --kind refute` with structured
    # evidence citations. Heavily used after a hypothesis dies.
    s = sub.add_parser("refute",
                       help="Add a refute annotation with structured "
                            "evidence citations (sugar over `note add "
                            "--kind refute`).")
    rsub = s.add_subparsers(dest="refute_action", required=True)
    ra = rsub.add_parser("add", help="Add a refute on a target.")
    ra.add_argument("target", nargs="?", default=None,
                    help="File path, symbol_id, or `file#name` shorthand. "
                         "Pass --note-id N instead to auto-resolve from "
                         "the disputed note's target.")
    ra.add_argument("body", help="Why the hypothesis fails.")
    ra.add_argument("--note-id", dest="note_id", type=int, default=None,
                    help="Refute a specific saved note by id. The "
                         "refute's `target` field is auto-resolved to "
                         "the disputed note's target so file-namespace "
                         "queries surface the dissent. Recommended "
                         "form for clarity. (v4 fix: previously `refute "
                         "add <id>` stored the integer as a string in "
                         "`target`, severing the refute.)")
    ra.add_argument("--evidence", action="append", default=[],
                    help="Evidence citation (file:line or URL). "
                         "Repeatable.")
    ra.add_argument("--author", default=None)
    ra.add_argument("--expires-days", type=int, default=None)
    ra.add_argument("--allow-free-standing", action="store_true",
                    help="Permit `refute add <target>` (no --note-id) "
                         "even when FACT notes exist on the target. "
                         "Stores a counter-claim that the verifier does "
                         "NOT evaluate (legacy behavior). Default: "
                         "refuse and list the FACT note ids so you can "
                         "pick one with --note-id.")
    ra.set_defaults(func=cmd_refute)

    # Note import/export — for sharing across machines / branches.
    s = sub.add_parser("note-export",
                       help="Export annotations as JSONL.")
    s.add_argument("--output", default=None,
                   help="File path (default: stdout).")
    s.add_argument("--include-expired", action="store_true")
    s.set_defaults(func=cmd_note_export)

    s = sub.add_parser("note-import",
                       help="Import annotations from a JSONL file. "
                            "Dedupes by SHA-1 of (target,kind,body). "
                            "Accepts `-` to read from stdin.")
    s.add_argument("input",
                   help="Path to the JSONL file, or `-` to read from stdin.")
    s.set_defaults(func=cmd_note_import)

    s = sub.add_parser("note-expire",
                       help="Soft-expire (or hard-delete) old annotations.")
    s.add_argument("--older-than-days", type=int, default=180)
    s.add_argument("--hard-delete", action="store_true",
                   help="Permanent removal instead of expires_at update.")
    s.set_defaults(func=cmd_note_expire)

    s = sub.add_parser("note-verify",
                       help="Revalidate notes on a target — recompute "
                            "fingerprint, update staleness and "
                            "decayed confidence.")
    s.add_argument("target", help="File / symbol / file#name shorthand.")
    s.set_defaults(func=cmd_note_verify)

    s = sub.add_parser("integrity",
                       help="Per-target integrity score (SPEC #9): "
                            "retrieval + freshness + contradictions + "
                            "ambiguity + structural coverage.")
    s.add_argument("target",
                   help="File / symbol / file#name shorthand.")
    s.set_defaults(func=cmd_integrity)

    s = sub.add_parser("audit",
                       help="Aggregate claim-level verdicts across every "
                            "note on a target. Tells you which specific "
                            "beliefs are still true, which are refuted, "
                            "and which are uncheckable — not just "
                            "'strongly_stale'.")
    s.add_argument("target",
                   help="File / symbol / file#name shorthand.")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("map",
                       help="System map: auto-detected modules, "
                            "cross-module edges, hub files, per-module "
                            "contract surface. The 'big picture' view "
                            "before diving into a single file.")
    s.add_argument("--max-modules", type=int, default=30)
    s.add_argument("--max-hubs", type=int, default=10)
    s.add_argument("--max-top-symbols", type=int, default=5)
    s.set_defaults(func=cmd_map)

    s = sub.add_parser("verify-completeness",
                       help="Target-focused post-edit gate. Narrow "
                            "companion to `projmem complete`: checks "
                            "dangling refs, stale paths, unupdated "
                            "consumers, inconsistent tests, and "
                            "contradicted notes — for ONE target.")
    s.add_argument("target", help="File / symbol / file#name shorthand.")
    s.add_argument("--limit", type=int, default=50,
                   help="Cap items per finding (default 50).")
    s.set_defaults(func=cmd_verify_completeness)

    s = sub.add_parser("analyze-change",
                       help="Pre-edit change-impact analysis for one "
                            "target. Returns direct + indirect dependents, "
                            "tests affected, contracts affected, and "
                            "likely-forgotten updates (dangling refs, "
                            "low-confidence consumers). Pair with "
                            "`projmem verify-completeness` after edits.")
    s.add_argument("target", help="File / symbol / file#name shorthand.")
    s.add_argument("--radius", type=int, default=2,
                   help="Max hops for indirect dependents (default 2).")
    s.add_argument("--max-per-bucket", type=int, default=50,
                   help="Cap per output list (default 50).")
    s.set_defaults(func=cmd_analyze_change)

    s = sub.add_parser("flow",
                       help="Trace a contract's usage chain: env-read → "
                            "local-assign → switch-case / conditional / "
                            "read. One-call answer to 'what does this "
                            "env var actually do?'.")
    s.add_argument("name",
                   help="Contract name (env var, flag, schema field, token).")
    s.add_argument("--kind", default=None,
                   choices=["env", "flag", "schema_field", "token"],
                   help="Restrict to this contract kind. Default: any.")
    s.add_argument("--max-consumers", type=int, default=50,
                   help="Max consumer sites to surface per read site.")
    s.add_argument("--no-cross-file", action="store_true",
                   help="Skip the cross-file consumer scan (object_property "
                        "aliases). Default: cross-file scan enabled — "
                        "consumers in importer files are surfaced as "
                        "`cross-file-read` / `cross-file-destructure`.")
    s.add_argument("--budget", type=int, default=None, metavar="TOKENS",
                   help="Cap output to ~N tokens; consumers shrink first "
                        "while name / read_sites / freshness_warning "
                        "stay intact.")
    s.set_defaults(func=cmd_flow)

    s = sub.add_parser("usage",
                       help="Self-document for any LLM agent. Markdown by "
                            "default; --json for structured catalog. Drop "
                            "into CLAUDE.md / AGENTS.md / .cursorrules.")
    s.set_defaults(func=cmd_usage)

    # Top-level alias for `note predicates`. Discoverability fix
    # (round-4 #20): the catalog hides under `note predicates`,
    # which agents don't think to look at when authoring an
    # `@predicate(...)` claim. This surfaces it from `projmem -h`.
    s = sub.add_parser(
        "predicates",
        help="List the structured-claim predicates the verifier "
             "knows about (alias of `projmem note predicates`).")
    s.set_defaults(func=cmd_predicates)

    s = sub.add_parser("search",
                       help="Full-text search across notes, symbols, "
                            "contracts, and file paths. Substring "
                            "match (case-insensitive), capped per "
                            "bucket. The 'where do I start?' command.")
    s.add_argument("query", help="Substring to search for.")
    s.add_argument("--limit", type=int, default=25,
                   help="Max hits per bucket (default 25).")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser(
        "mcp-server",
        help="Run projmem as an MCP (Model Context Protocol) server on "
             "stdio. Exposes ~8 read/write tools to any MCP-aware "
             "client (Claude Code, Claude Desktop, Cursor, Continue).",
        description=(
            "Run projmem as an MCP (Model Context Protocol) server on "
            "stdio. Speaks the JSON-RPC framed MCP protocol; reserve "
            "stdout for transport bytes only — diagnostics go to "
            "stderr.\n\n"
            "Exposed tools (read): session, search, symbol, reverse, "
            "forward, fact-check, notes, ask. Exposed tools (write): "
            "note_add, conclude.\n\n"
            "Register the server in your MCP-aware client. Example "
            "(Claude Code / Cursor): add the snippet below to the "
            "client's MCP config (typically `~/.config/claude/mcp.json` "
            "or the equivalent for your client):\n\n"
            "    {\n"
            "      \"projmem\": {\n"
            "        \"command\": \"projmem\",\n"
            "        \"args\":    [\"mcp-server\", \"--path\", \"/abs/repo\"]\n"
            "      }\n"
            "    }\n\n"
            "Once registered, restart the client and the tools appear "
            "in the agent's tool list. Each call hits the same SQLite "
            "store the CLI uses, so writes from the MCP surface are "
            "visible to subsequent `projmem` invocations and vice "
            "versa."),
        epilog=("To see the catalog without running the server, use "
                 "`projmem usage --json` (the same command list is "
                 "exposed over MCP)."))
    s.set_defaults(func=cmd_mcp_server)

    s = sub.add_parser("watch",
                       help="Background watcher: re-verify notes whose "
                            "target falls in any saved file. Emits one "
                            "line per status TRANSITION (quiet on "
                            "no-ops). Ctrl-C to stop.")
    s.add_argument("paths", nargs="*", default=None,
                   help="Optional path subset to watch (default: every "
                        "indexed file).")
    s.add_argument("--interval", type=float, default=1.0,
                   help="Poll cadence in seconds (default 1.0).")
    s.add_argument("--once", action="store_true",
                   help="Run a single sweep and exit (CI / tests).")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("hook",
                       help="Install / uninstall git hooks that run "
                            "projmem on every commit and checkout. "
                            "Surfaces REFUTED notes the moment they're "
                            "introduced.")
    s.add_argument("hook_action", nargs="?", default="status",
                   choices=["install", "uninstall", "status"],
                   help="Default: status.")
    s.add_argument("--force", action="store_true",
                   help="With install: overwrite the existing hook file "
                        "entirely (wipes other tools' hook content). "
                        "Default: append/update only our managed block.")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("graph",
                       help="Render a claim-and-drift-aware graph. "
                            "Writes graph.svg / graph.dot / graph.mmd "
                            "into projmem-out/. Nodes colored by claim "
                            "status (PROVED/REFUTED/AMBIGUOUS/NONE); "
                            "drift = black ring.")
    s.add_argument("target", nargs="?", default=None,
                   help="File path, file#name, or bare symbol. Omit "
                        "(or pass --full) to render the whole repo.")
    s.add_argument("--full", action="store_true",
                   help="Render every node up to --max-nodes (default "
                        "150). Caps prevent unreadable canvases.")
    s.add_argument("--hops", type=int, default=2,
                   help="Neighborhood radius around target (default 2).")
    s.add_argument("--max-nodes", type=int, default=150,
                   help="Hard cap on nodes drawn (default 150).")
    s.add_argument("--no-symbols", action="store_true",
                   help="Files only — skip the per-file symbol nodes.")
    s.add_argument("--out", metavar="DIR",
                   help="Output directory (default: ./projmem-out/).")
    s.add_argument("--format", metavar="LIST",
                   help="Comma-separated subset of {dot,mmd,svg}. "
                        "Default: all three.")
    s.set_defaults(func=cmd_graph)

    s = sub.add_parser("report",
                       help="One-page markdown digest at "
                            "projmem-out/REPORT.md: headline ribbon, "
                            "REFUTED claims, god symbols, stale notes, "
                            "knowledge gaps.")
    s.add_argument("--out", metavar="DIR",
                   help="Output directory (default: ./projmem-out/).")
    s.add_argument("--max-refuted", type=int, default=10)
    s.add_argument("--max-god-symbols", type=int, default=10)
    s.add_argument("--max-stale", type=int, default=10)
    s.add_argument("--max-isolated", type=int, default=10)
    s.add_argument("--max-ambiguous", type=int, default=10)
    s.add_argument("--max-renamed", type=int, default=10)
    s.add_argument("--max-recent", type=int, default=10)
    s.set_defaults(func=cmd_report)

    from . import agent_init as _ai_choices
    s = sub.add_parser("init",
                       help="ONE-COMMAND SETUP: build the index AND drop "
                            "the agent-instruction file(s) for the chosen "
                            "platform. Usage: `projmem init claude` / "
                            "`init codex` / `init cursor` / `init gemini` "
                            "/ `init kiro` / `init opencode` / "
                            "`init antigravity` / `init copilot` / "
                            "`init auto` (auto-detect, default) / "
                            "`init all`.")
    s.add_argument("agent", nargs="?", default=None,
                   choices=[None] + _ai_choices.CHOICES,
                   help="Which agent's instruction file(s) to drop. "
                        "Default `auto` picks based on env vars. "
                        "Aider/Droid/Trae/Hermes/OpenClaw all map to "
                        "the AGENTS.md template.")
    s.add_argument("--template", default=None,
                   choices=_ai_choices.CHOICES,
                   help="[DEPRECATED] Use the positional `agent` arg "
                        "instead. Kept for backward compatibility.")
    s.add_argument("--force", action="store_true",
                   help="Overwrite existing template files. Default: skip.")
    s.add_argument("--no-index", action="store_true",
                   help="Skip the indexing step (templates only).")
    s.add_argument("--reindex", action="store_true",
                   help="Rebuild the index even if one already exists.")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("notes",
                       help="Project-wide memory summary. Totals + recent "
                            "notes + contradicted + risk-ranked targets + "
                            "authors + index health. Call this BEFORE "
                            "`projmem session` when you don't know which "
                            "target to investigate yet.")
    s.add_argument("--max-recent", type=int, default=10)
    s.add_argument("--max-contradicted", type=int, default=10)
    s.add_argument("--max-risk-targets", type=int, default=10)
    s.set_defaults(func=cmd_notes)

    s = sub.add_parser("session",
                       help="One-call agent bootstrap. With no target: "
                            "project-wide memory summary + known contracts "
                            "(the 'what's in this repo?' entrypoint). With "
                            "a target: per-target bootstrap (notes, "
                            "integrity, neighbors, freshness).")
    s.add_argument("target", nargs="?", default=None,
                   help="Optional: file / symbol / file#name shorthand. "
                        "Omit for project-wide mode.")
    s.add_argument("--max-notes", type=int, default=10,
                   help="Cap notes surfaced (default 10).")
    s.add_argument("--max-audit", type=int, default=5,
                   help="Cap recent audit-trail entries (default 5).")
    s.add_argument("--max-neighbors", type=int, default=5,
                   help="Cap each neighbor list sample (default 5).")
    s.add_argument("--max-recent", type=int, default=10,
                   help="Project-mode: cap on recent_notes list.")
    s.add_argument("--max-contradicted", type=int, default=10,
                   help="Project-mode: cap on contradicted_notes list.")
    s.add_argument("--max-risk-targets", type=int, default=10,
                   help="Project-mode: cap on risk_targets list.")
    s.add_argument("--max-contracts", type=int, default=15,
                   help="Project-mode: cap on known_contracts top-N per kind.")
    s.add_argument("--budget", type=int, default=None, metavar="TOKENS",
                   help="Cap output to ~N tokens; lower-priority "
                        "sections (audit_trail, neighbors) shrink first "
                        "while totals / next_steps_hint / notes survive.")
    s.set_defaults(func=cmd_session)

    s = sub.add_parser("doctor",
                       help="Health check: tree-sitter, foreign index, "
                            "oversize skips, parser coverage, stale files, "
                            "artifact bleed. Emits severity-tagged findings "
                            "with actionable suggestions.")
    s.add_argument("--skip-stale-check", action="store_true",
                   help="Skip the on-disk hash sampling (200 files). Use "
                        "in CI where you don't want to re-hash on every run.")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("complete",
                       help="END-OF-TASK PRIMITIVE: incremental refresh "
                            "(modified+added+deleted) PLUS checklist gate. "
                            "Run this AFTER you finish editing — the "
                            "everyday alternative to a full reindex.")
    s.add_argument("--base", default="pre-index",
                   help="Base snapshot label for the checklist gate "
                        "(default 'pre-index').")
    s.add_argument("--head", default="current",
                   help="Head snapshot label or 'current' (default).")
    s.add_argument("--limit", type=int, default=200,
                   help="Cap items per checklist finding (default 200).")
    s.add_argument("--include-vendor", action="store_true",
                   help="Include vendor/out-of-scope paths in checklist.")
    s.add_argument("--include-artifacts", action="store_true",
                   help="Include artifact refs in dangling-ref checks.")
    s.set_defaults(func=cmd_complete)

    s = sub.add_parser("checklist",
                       help="Post-edit completeness gate: contract obligations, "
                            "dangling symbol refs, and broken repo-relative "
                            "imports. One-call 'did I forget anything?'.")
    s.add_argument("--base", default="pre-index",
                   help="Base snapshot label (default 'pre-index').")
    s.add_argument("--head", default="current",
                   help="Head snapshot label or 'current' (default).")
    s.add_argument("--limit", type=int, default=200,
                   help="Cap items per finding (default 200).")
    s.add_argument("--include-vendor", action="store_true",
                   help="Include vendor/out-of-scope paths (default: "
                        "scope-only).")
    s.add_argument("--include-artifacts", action="store_true",
                   help="Include artifact refs (changelogs, snapshots, build "
                        "output) in dangling-ref checks.")
    s.set_defaults(func=cmd_checklist)

    s = sub.add_parser("conflicts",
                       help="List every contradiction across all "
                            "annotated targets.")
    s.set_defaults(func=cmd_conflicts)

    s = sub.add_parser("evidence", help="Ingest JSONL runtime evidence file.")
    s.add_argument("file")
    s.set_defaults(func=cmd_evidence)

    s = sub.add_parser("evidence-query",
                       help="List runtime evidence touching a symbol or file.")
    s.add_argument("target", help="Symbol name or file path.")
    s.set_defaults(func=cmd_evidence_query)

    s = sub.add_parser("drift",
                       help="Static ↔ runtime drift: defs the runtime never touched.")
    s.add_argument("--kind", metavar="KINDS",
                   help="Same comma-separated kinds as `orphans`.")
    s.add_argument("--all-kinds", action="store_true")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--include-non-code", action="store_true",
                   help="Don't filter docs / data files. By default "
                        "drift skips XML / markdown / plantuml etc. "
                        "where regex-extracted 'symbols' are tag names "
                        "or doc headings, not real defs (e.g. <loader> "
                        "in webapps/docs/class-loader-howto.xml).")
    s.add_argument("--include-import-bindings", action="store_true",
                   help="Don't filter `const X = require(...)` style "
                        "import bindings. Default is to drop them — "
                        "the runtime ingester never names them as a "
                        "target so they pollute drift output (191/460 "
                        "rows on a real Node audit).")
    s.set_defaults(func=cmd_drift)
    return p


def _autolog_command(args) -> None:
    """Best-effort auto-log of the command we're about to run.
    Skips index/snapshot/audit-trail itself (avoid recursion / noise)."""
    cmd = getattr(args, "cmd", None)
    if cmd in (None, "index", "snapshot", "audit-trail", "stats"):
        return
    try:
        cfg = config_mod.load(args.path)
        if not os.path.isfile(cfg.db_path):
            return  # no index → nothing to log into
        store = Store(cfg.db_path)
        # Common arg names that might be the "target"
        target = (getattr(args, "target", None) or
                  getattr(args, "name", None) or
                  getattr(args, "file", None) or
                  getattr(args, "symbol", None))
        author = os.environ.get("PROJMEM_AUTHOR")
        # Serialize remaining args lightly for context
        arg_blob = " ".join(
            f"{k}={v!r}" for k, v in vars(args).items()
            if k not in ("func", "cmd", "path", "json")
            and v is not None and v is not False
        )[:500]
        store.log_command(cmd, target=str(target) if target else None,
                          args=arg_blob, author=author)
        store.close()
    except Exception:
        pass  # never let logging block the actual command


def _cmd_default(args) -> int:
    """Handler for bare `projmem` (no subcommand).

    Produces a compact status blob so the agent — which might know
    nothing about this tool yet — immediately sees what's remembered
    and what to do next. Format parallels `projmem notes` but is
    intentionally shorter (fits in a single glance):

        {
          "repo_memory":    {has_memory, total_notes, ...},
          "index_present":  bool,
          "next_steps":     [concrete commands to run next]
        }
    """
    # If there's no .projmem/ at all, tell the user to run init.
    cfg = config_mod.load(args.path)
    db_exists = os.path.isfile(cfg.db_path)
    if not db_exists:
        _emit({
            "repo_memory":   {"has_memory": False,
                              "total_notes": 0,
                              "discover":    "projmem notes",
                              "hint":        "No projmem index in this repo. "
                                             "Run `projmem init` to set up."},
            "index_present": False,
            "next_steps": [
                "Run `projmem init` to build the index and drop "
                "AGENTS.md (the LLM agent-instruction file).",
                "After init, `projmem notes` surfaces what's "
                "remembered, `projmem session <target>` bootstraps "
                "a task, `projmem usage` lists every command.",
            ],
        }, args.json)
        return 0
    # Index exists — emit the memory header + a short next-step list.
    store = Store(cfg.db_path)
    try:
        from . import memory_header as _mh
        header = _mh.build_header(store)
    finally:
        store.close()
    next_steps: List[str] = []
    if header.get("contradicted_count", 0) > 0:
        next_steps.append(
            f"{header['contradicted_count']} note(s) contradicted — "
            "run `projmem notes` to see which beliefs are refuted.")
    if header.get("total_notes", 0) == 0:
        # Quantum-thinking-round: the auto-extract path (round 7+)
        # means agents save FACT claims by writing PROSE — no JSON,
        # no @predicate(...) syntax. Tell them about it explicitly
        # at the bare-entrypoint moment.
        next_steps.append(
            "No notes yet. `projmem note add <target> --kind note "
            "'`X` is defined at file.py:42'` saves a FACT claim from "
            "prose — auto-extracted, no JSON syntax.")
    else:
        next_steps.append(
            "`projmem notes` for project-wide memory summary.")
        next_steps.append(
            "`projmem session <file_or_symbol>` to bootstrap a task.")
    # Quantum-thinking-round: real benchmark data (audit_trail across
    # 36 sessions) showed 7 of 67 commands ever invoked by Sonnet.
    # Surface those 7 here as the agent-tier surface; `usage` keeps
    # the full catalog for power users.
    _emit({
        "repo_memory":   header,
        "index_present": True,
        "next_steps":    next_steps,
        "core_verbs": {
            "note add":    "save a finding (auto-extracts FACT claims from prose)",
            "notes":       "project-wide summary + contradicted_count blocker",
            "session":     "per-target bootstrap (notes + neighbors + freshness)",
            "conclude":    "save a one-line conclusion w/ inline @predicate(...) claims",
            "fact-check":  "verify claims in a draft BEFORE shipping (exit 2 on REFUTED)",
            "task":        "session-continuity (start / step / blocked / resume / close)",
            "refresh":     "incremental reindex after edits (auto-applies by default)",
        },
        "expert_verbs_count": 60,
        "expert_verbs_hint":  "`projmem usage` for the full 67-command catalog.",
    }, args.json)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # Round-5 P3 (refined in r3): when --json is requested anywhere in
    # the command line, route argparse's validation errors through a
    # JSON envelope. Previously the check only inspected `argv` when
    # explicitly passed — real CLI use passes None and argparse reads
    # sys.argv directly, so the override never kicked in. Also the
    # `--json` flag can appear on a nested subparser thanks to the
    # subparsers action wrapper, so check both surfaces.
    effective = list(argv) if argv is not None else list(sys.argv[1:])
    wants_json = "--json" in effective
    parser = build_parser()
    if wants_json:
        def _json_error(message: str) -> None:
            payload = {
                "error":   "argparse",
                "message": message,
                "hint":    ("Run `projmem -h` (or `projmem <verb> -h`) "
                             "for the accepted form."),
            }
            print(json.dumps(payload, indent=2))
            sys.exit(2)
        parser.error = _json_error  # type: ignore[assignment]
        # Subparsers carry their own .error — override each so a typo
        # in `projmem note add ... --bogus` also returns JSON.
        for action in parser._actions:
            if action.__class__.__name__ == "_SubParsersAction":
                for sub in (action.choices or {}).values():
                    sub.error = _json_error  # type: ignore[assignment]
                    # Walk nested subparsers (note add, task start, etc.).
                    for nested_action in sub._actions:
                        if nested_action.__class__.__name__ == "_SubParsersAction":
                            for nsub in (nested_action.choices or {}).values():
                                nsub.error = _json_error  # type: ignore[assignment]
    args = parser.parse_args(argv)
    # Bare `projmem` (no subcommand) → memory status + next-step guidance.
    # Replaces argparse's "argument required" error with the natural
    # discovery point: an agent typing the tool name alone sees what
    # the repo remembers and what to call next.
    if not getattr(args, "cmd", None):
        return _cmd_default(args)
    _autolog_command(args)
    try:
        # Command functions may opt-in to non-zero exit codes by returning
        # an int (e.g. `doctor` returns 1 on HIGH findings). Commands that
        # don't return anything stay at rc=0 as before.
        rc = args.func(args)
        return int(rc) if isinstance(rc, int) else 0
    except BrokenPipeError:
        # Common Unix pipeline case: `projmem ... | head` closes stdout early.
        # Treat as a clean exit with no traceback/noisy error.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except Exception as e:
        sys.stderr.write(f"projmem: error: {type(e).__name__}: {e}\n")
        if os.environ.get("PROJMEM_DEBUG"):
            import traceback; traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
