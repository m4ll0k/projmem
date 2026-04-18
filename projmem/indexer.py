"""Indexer. Parses Python via ast (high confidence), JS/TS/other via regex (medium/low).

Populates: files, symbols, refs, edges(imports/defines/exports/references),
          contracts (flag/env/schema_field/token), entrypoints.
"""
from __future__ import annotations
import ast
import os
import re
import sys
import warnings
from typing import Dict, List, Optional, Set, Tuple

from .config import Config
from .store import Store
from .utils import hash_file, lang_of, read_text, rel
from .discovery import walk
from . import semantic, entrypoints, ts_backend


# ---- Python via ast ----

def index_python(store: Store, rel_path: str, src: str, root: str) -> str:
    """Index a Python file via the stdlib ast. Returns the parser tag used:
      - "ast"            — AST indexing succeeded
      - "regex"          — fell back (SyntaxError or pathological recursion)

    Returning the tag lets the caller record it on the file row so a single
    pathological file degrades gracefully without killing the whole run.
    """
    try:
        with warnings.catch_warnings():
            # Third-party Python files commonly contain non-raw regex strings
            # (e.g. "\w+" instead of r"\w+"). Python 3.12 upgraded those from
            # DeprecationWarning to SyntaxWarning. Suppress — we don't own
            # the code we're indexing.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(src, filename=rel_path)
    except SyntaxError:
        # Fall back to regex path with low confidence
        index_regex(store, rel_path, src, lang="python", confidence="low")
        return "regex"

    imports: List[Tuple[str, str, List[Tuple[str, Optional[str]]]]] = []
    defined_names: Set[str] = set()
    imported_aliases: Set[str] = set()
    referenced_names: Set[Tuple[str, int]] = set()

    class V(ast.NodeVisitor):
        def __init__(self):
            self.scope_depth = 0

        def visit_FunctionDef(self, node):
            store.add_symbol(file=rel_path, name=node.name, kind="function",
                             line=node.lineno, col=node.col_offset,
                             end_line=getattr(node, "end_lineno", None),
                             end_col=getattr(node, "end_col_offset", None),
                             exported=int(self.scope_depth == 0 and not node.name.startswith("_")),
                             confidence="high")
            defined_names.add(node.name)
            self.scope_depth += 1; self.generic_visit(node); self.scope_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            store.add_symbol(file=rel_path, name=node.name, kind="class",
                             line=node.lineno, col=node.col_offset,
                             end_line=getattr(node, "end_lineno", None),
                             end_col=getattr(node, "end_col_offset", None),
                             exported=int(self.scope_depth == 0 and not node.name.startswith("_")),
                             confidence="high")
            defined_names.add(node.name)
            # M3: base classes → `extends` edges. Python doesn't distinguish
            # interface implementation from inheritance syntactically.
            # M8: edge SOURCE uses canonical symbol_id; edge TARGET uses
            # symbol_id when resolvable to a single in-repo def, else falls
            # back to the bare name (preserves prior behavior, marks edge
            # as `confidence=medium` because ambiguity is structural).
            from . import symbol_id as _sid
            src_id = _sid.build(rel_path, node.name, "class")
            for base in node.bases:
                parent_name = None
                if isinstance(base, ast.Name):
                    parent_name = base.id
                elif isinstance(base, ast.Attribute):
                    parent_name = base.attr
                if parent_name:
                    resolved = store.resolve_name_to_symbol_id(
                        parent_name, prefer_kind="class")
                    edge_dst = resolved or parent_name
                    edge_conf = "high" if resolved else "medium"
                    store.add_edge(
                        src=src_id,
                        dst=edge_dst,
                        type="extends",
                        confidence=edge_conf,
                        evidence=f"extends: class {node.name}({parent_name})")
                    store.add_ref(file=rel_path, name=parent_name,
                                  kind="extends", line=base.lineno,
                                  target_symbol_id=resolved,
                                  confidence="high")
            self.scope_depth += 1; self.generic_visit(node); self.scope_depth -= 1

        def visit_Assign(self, node):
            if self.scope_depth == 0:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        store.add_symbol(file=rel_path, name=t.id, kind="var",
                                         line=node.lineno, col=node.col_offset,
                                         exported=int(not t.id.startswith("_")),
                                         confidence="high")
                        defined_names.add(t.id)
            self.generic_visit(node)

        def visit_Import(self, node):
            for n in node.names:
                # `import X` or `import X as Y` — bind local name Y/X, no
                # per-member resolution needed.
                imports.append((n.name, "import",
                                [(n.name, n.asname)]))
                local = n.asname or n.name.split(".", 1)[0]
                imported_aliases.add(local)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            mod = node.module or ""
            if node.level:
                mod = "." * node.level + mod
            members: List[Tuple[str, Optional[str]]] = []
            for n in node.names:
                if n.name == "*":
                    continue
                members.append((n.name, n.asname))
                local = n.asname or n.name
                # Track the local binding separately so call-site refs can
                # still fire — an imported name is BOTH a local name AND a
                # reference to the source module's export.
                imported_aliases.add(local)
            imports.append((mod, "importfrom", members))
            self.generic_visit(node)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                referenced_names.add((node.id, node.lineno))
            self.generic_visit(node)

        def visit_Attribute(self, node):
            # Capture leftmost name of attr access. Walk the chain ITERATIVELY
            # and visit only the non-Attribute leaf — skipping the default
            # `generic_visit` would still recurse `visit_Attribute` once per
            # segment, defeating the point. scip-python/pyright ships test
            # fixtures like `x[0][0]...[0]` and `y.x.x.x.x...x` with 400+
            # segments that blow past Python's default 1000-frame limit.
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                referenced_names.add((cur.id, node.lineno))
            else:
                # Non-attribute leaf (Call, Subscript, etc.) — visit once
                # so its own children still get indexed.
                self.visit(cur)

        def visit_Call(self, node):
            # Method-call sites: `module.func(...)` or `obj.method(...)`.
            # `visit_Attribute` only captures the leftmost Name in the
            # chain (`module` / `obj`), which means a cross-module call
            # like `_cd.compute_diff(...)` in cli.py would produce ZERO
            # refs to `compute_diff` — a real usability gap when running
            # `projmem symbol compute_diff` on any codebase that uses
            # dotted imports. This visitor emits the callee name
            # explicitly so method calls show up as refs on the target
            # symbol. `generic_visit` below still descends into `func`
            # (triggering `visit_Attribute` on the chain) and `args`, so
            # we don't lose the existing leftmost-name capture.
            if isinstance(node.func, ast.Attribute):
                referenced_names.add((node.func.attr, node.lineno))
            self.generic_visit(node)

    # Protect the whole visitor run against runaway recursion on pathological
    # files (scip-python's maxParseDepth2.py, auto-generated parsers, etc.).
    # We raise the limit once for the duration of the visit, and if it still
    # overflows, fall back to regex for THIS file only — the run continues.
    _prev_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(_prev_limit, 10000))
        try:
            V().visit(tree)
        except RecursionError:
            sys.setrecursionlimit(_prev_limit)
            # Drop any partial state captured on this file before falling back.
            store.delete_file_data(rel_path)
            index_regex(store, rel_path, src, lang="python", confidence="low")
            return "regex"
    finally:
        sys.setrecursionlimit(_prev_limit)

    # Imports -> resolve to files where possible, per member for ImportFrom so
    # `from . import packs` links to packs.py, not to the package __init__.
    for mod, kind, members in imports:
        mod_target = resolve_python_import(mod, rel_path, root)
        # Primary edge: the module itself. For `from . import X` with an
        # empty/pure-relative module, this often resolves to __init__.py —
        # which is CORRECT for import semantics but not enough alone.
        if mod:
            store.add_edge(src=rel_path,
                           dst=mod_target or f"module:{mod}",
                           type="imports",
                           confidence="high" if mod_target else "medium",
                           evidence=f"{'from ' + mod + ' import ...' if kind == 'importfrom' else 'import ' + mod}")
        # Per-member edges for `from PKG import X, Y` and `from . import X`.
        # Each imported NAME may itself resolve to a sibling submodule.
        if kind == "importfrom":
            for member_name, _alias in members:
                # Build the dotted spec that would match a submodule. For
                # pure-relative `from . import mod` (mod=".") the result is
                # ".mod" — no dot separator, since `.` already carries its
                # trailing "." semantics. For `from .pkg import mod`
                # (mod=".pkg") the result is ".pkg.mod". For absolute
                # `from foo.bar import mod` (mod="foo.bar") → "foo.bar.mod".
                if not mod:
                    composite = member_name
                elif mod.endswith("."):
                    composite = mod + member_name
                else:
                    composite = mod + "." + member_name
                member_target = resolve_python_import(composite, rel_path, root)
                if member_target and member_target != mod_target:
                    # Member uniquely resolves to a sibling submodule.
                    # Emit the FILE edge (no fake module:... prefix).
                    store.add_edge(src=rel_path, dst=member_target,
                                   type="imports", confidence="high",
                                   evidence=f"from {mod or '.'} import {member_name}")
                # Correctness fix (GAP 5): previously we emitted a second
                # `module:<mod>.<member>` edge whenever both the module and
                # the composite failed to resolve. That produced fake
                # "unresolved module" entries like `module:...missing.Thing`
                # for every `from ...missing import Thing`, polluting the
                # unresolved-imports view, integrity scores, and pack trust.
                # The primary module edge above ALREADY carries the
                # unresolved signal (`module:...missing`); emitting the
                # composite form on top was duplicate noise.
                #
                # We also explicitly do NOT emit a separate edge for
                # `from MOD import symbol` when MOD resolves — in that case
                # `symbol` is a name re-exported from MOD, not a file, and
                # fabricating an edge would confuse reverse-deps.

    # Emit every referenced name, including names that are ALSO defined
    # in this file. Intra-file callgraph (`_intra_file_calls` in packs.py)
    # reads from the same `refs` table and joins on `name IN sym_names`,
    # so stripping same-file refs here used to hide ALL intra-file calls
    # — the exact reason `pack 'file.py#symbol'` returned 0 symbol_refs
    # for a function called many times within its own file. We still skip
    # the DEF line itself so the definition doesn't count as a self-ref.
    def_lines: set = set()
    for row in store.conn.execute(
            "SELECT name, line FROM symbols WHERE file=?", (rel_path,)):
        def_lines.add((row["name"], row["line"]))
    for name, line in referenced_names:
        if (name, line) in def_lines:
            continue
        store.add_ref(file=rel_path, name=name, kind="name", line=line,
                      confidence="high")

    # Semantic contracts — skip on artifact files (changelogs, snapshots,
    # generated code) so doc mentions of `--flag` aren't harvested as
    # contract declarations.
    from . import artifacts as _artifacts
    if not _artifacts.is_artifact_path(rel_path):
        semantic.scan_python_contracts(store, rel_path, tree, src)
    return "ast"


def resolve_python_import(mod: str, from_file: str, root: str) -> Optional[str]:
    """Best-effort resolution of a Python import to a file path within the repo.

    Resolution order (first hit wins):
      1. If `mod` starts with '.', resolve relative to `from_file`'s dir.
      2. Treat `mod` as a dotted path anchored at the repo root.
      3. Fallback — sibling-to-importer and each parent up to root. Many Python
         projects rely on this sys.path-style lookup (e.g. scripts that add
         their own directory to sys.path, or packages whose directory is
         itself on sys.path at runtime). Without this, `from differential
         import analyze` inside `analysis/report_builder.py` will not resolve
         to the sibling `analysis/differential.py`.
    """
    if not mod:
        return None

    def _try(cand: str) -> Optional[str]:
        for p in (cand + ".py", os.path.join(cand, "__init__.py")):
            if os.path.isfile(os.path.join(root, p)):
                return p.replace("\\", "/")
        return None

    # 1. Relative import
    if mod.startswith("."):
        base_dir = os.path.dirname(from_file)
        stripped = mod.lstrip(".")
        ups = len(mod) - len(stripped)
        parts = [base_dir] + [".."] * (ups - 1)
        if stripped:
            parts += stripped.split(".")
        cand = os.path.normpath(os.path.join(*parts))
        return _try(cand)

    # 2. Absolute, rooted at repo root.
    rel_cand = mod.replace(".", os.sep)
    hit = _try(rel_cand)
    if hit:
        return hit

    # 3. Sibling-to-importer lookup: walk from dirname(from_file) up to root.
    base = os.path.dirname(from_file)
    while True:
        cand = os.path.normpath(os.path.join(base, rel_cand)) if base else rel_cand
        if not cand.startswith(".."):
            hit = _try(cand)
            if hit:
                return hit
        if not base:
            break
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent

    return None


# ---- Regex path for JS/TS/other ----

JS_IMPORT = re.compile(
    r"""^\s*(?:import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]"""
    r"""|(?:const|let|var)\s+[^=]+=\s*require\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)
JS_EXPORT_FN = re.compile(r"""^\s*export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)""", re.MULTILINE)
JS_FN = re.compile(r"""^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)""", re.MULTILINE)
JS_CLASS = re.compile(r"""^\s*class\s+([A-Za-z_$][\w$]*)""", re.MULTILINE)
JS_CONST = re.compile(r"""^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=""", re.MULTILINE)
JS_MODULE_EXPORTS = re.compile(r"""module\.exports(?:\.([A-Za-z_$][\w$]*))?\s*=""")

IDENT = re.compile(r"[A-Za-z_$][\w$]*")

# Call-site regexes for the JS regex backend. `(?<![.\w$])` avoids matching
# the method-call identifier inside a larger member expression — that's
# handled by the member-call regex below.
_JS_CALL_BARE_RX = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]{2,63})\s*\(")
_JS_CALL_MEMBER_RX = re.compile(r"\.([A-Za-z_$][\w$]{2,63})\s*\(")

# Reserved words that syntactically look like calls but aren't symbol refs.
_JS_NONCALL_KEYWORDS = {
    "if", "while", "for", "switch", "catch", "return", "throw", "typeof",
    "instanceof", "new", "delete", "void", "await", "yield", "function",
    "super", "this",
}


def index_regex(store: Store, rel_path: str, src: str, lang: str, confidence: str = "medium") -> None:
    # Line map
    lines = src.splitlines()

    def lineno(pos: int) -> int:
        return src.count("\n", 0, pos) + 1

    defined: Set[str] = set()
    for rx, kind in ((JS_FN, "function"), (JS_CLASS, "class"), (JS_CONST, "var"),
                     (JS_EXPORT_FN, "exported")):
        for m in rx.finditer(src):
            name = m.group(1)
            defined.add(name)
            store.add_symbol(file=rel_path, name=name, kind=kind,
                             line=lineno(m.start()), col=0,
                             exported=int(kind == "exported" or "export" in m.group(0)),
                             confidence=confidence)

    for m in JS_MODULE_EXPORTS.finditer(src):
        name = m.group(1) or "default"
        store.add_symbol(file=rel_path, name=name, kind="exported",
                         line=lineno(m.start()), col=0, exported=1,
                         confidence=confidence)
        defined.add(name)

    # imports / requires
    # F013: only scan JS imports when the file is JS/TS. The regex
    # was matching `import` statements inside markdown code fences and
    # README snippets and emitting them as confidence=high `imports`
    # edges — `projmem reverse` then double-counted those as real
    # consumers. Same fix shape protects against `# Python comment
    # mentioning import x` in non-JS sources.
    is_js_lang = lang in ("javascript", "typescript", "tsx", "jsx",
                            "mjs", "cjs")
    if is_js_lang:
        for m in JS_IMPORT.finditer(src):
            target = m.group(1) or m.group(2) or m.group(3)
            if not target:
                continue
            resolved = resolve_js_import(target, rel_path,
                                          store_root_hint(store))
            store.add_edge(src=rel_path,
                           dst=resolved or f"module:{target}",
                           type="imports",
                           confidence="high" if resolved else "medium",
                           evidence=f"import '{target}'")

    # References: capture CALL SITES specifically (not every identifier).
    # Two patterns: bare `foo(` and `obj.foo(`. This turns the regex path from
    # "noise firehose" into usable intra-file call data — critical for monolith
    # JS files where tree-sitter may not be available. We emit EVERY call site
    # (including calls to same-file defs), so the monolith callgraph works
    # under the regex backend too.
    #
    # We must exclude the definition-site occurrences themselves (e.g. the
    # `foo` in `function foo(`) — track def-site (name, line) pairs and skip.
    def_sites: Set[Tuple[str, int]] = set()
    for rx in (JS_FN, JS_CLASS, JS_CONST, JS_EXPORT_FN):
        for m in rx.finditer(src):
            def_sites.add((m.group(1), lineno(m.start())))
    for m in JS_MODULE_EXPORTS.finditer(src):
        name = m.group(1) or "default"
        def_sites.add((name, lineno(m.start())))

    ref_sites: Set[Tuple[str, int]] = set()
    for rx in (_JS_CALL_BARE_RX, _JS_CALL_MEMBER_RX):
        for m in rx.finditer(src):
            nm = m.group(1)
            if len(nm) < 3 or nm in JS_KEYWORDS or nm in _JS_NONCALL_KEYWORDS:
                continue
            ln = lineno(m.start())
            if (nm, ln) in def_sites:
                continue
            ref_sites.add((nm, ln))
    for nm, ln in ref_sites:
        store.add_ref(file=rel_path, name=nm, kind="call", line=ln,
                      confidence="low")

    # Semantic contracts via regex — skip on artifact paths AND on
    # `other`-language files (markdown, text, etc.). Contract patterns
    # like `--flag` regex-match documentation chatter (e.g. README
    # "use --json to ..." mentions) and create false-positive flag
    # declarations that then surface as "open obligations" on every
    # `projmem complete`.
    # Audit P1#4: contract extraction runs on every file EXCEPT
    # documentation (`.md`/`.txt`/`.rst`) and artifacts. The previous
    # rule (`lang != "other"`) was too broad — it disabled extraction
    # for legitimate DSL surfaces like Prisma, SQL, TOML, YAML where
    # contracts (env vars, schema fields, flag tokens) genuinely live.
    # The audit reproduced this: a Prisma enum left inconsistent with
    # TS code went undetected by `projmem complete`.
    from . import artifacts as _artifacts
    from .utils import is_doc_only
    if not is_doc_only(rel_path) and not _artifacts.is_artifact_path(rel_path):
        semantic.scan_regex_contracts(store, rel_path, src, lang)


JS_KEYWORDS = {
    "function", "return", "const", "let", "var", "if", "else", "for", "while",
    "class", "extends", "new", "this", "super", "import", "from", "export",
    "default", "async", "await", "try", "catch", "finally", "throw", "switch",
    "case", "break", "continue", "true", "false", "null", "undefined", "void",
    "typeof", "instanceof", "in", "of", "do", "yield", "static", "get", "set",
    "interface", "type", "enum", "public", "private", "protected", "readonly",
    "implements", "module", "namespace", "declare", "as",
}


def store_root_hint(store: Store) -> str:
    return store.get_meta("root") or ""


def resolve_js_import(spec: str, from_file: str, root: str) -> Optional[str]:
    # Node.js internal-module convention: `require('internal/url')` →
    # `lib/internal/url.js`. The Node runtime's custom loader routes
    # these specifiers through a private resolver, not through
    # node_modules. Bare specifiers starting with `internal/` OR any
    # Node built-in name followed by `/` are treated this way. Without
    # this, projmem under-reports reverse deps by ~5-50x on Node's lib
    # tree (47 real require sites for `internal/url` showed as 0 in
    # the reverse-deps query on a fresh scan).
    if spec.startswith("internal/") or spec.startswith("node:internal/"):
        stripped = spec.replace("node:", "", 1)
        candidates = [os.path.join("lib", stripped) + ext
                      for ext in (".js", ".mjs", ".cjs")]
        candidates += [os.path.join("lib", stripped, "index.js")]
        for c in candidates:
            full = os.path.join(root, c) if root else c
            if os.path.isfile(full):
                return c.replace("\\", "/")
        return None
    if not spec.startswith(".") and not spec.startswith("/"):
        # TypeScript / JavaScript path alias resolution FIRST — before
        # Node built-ins — because alias patterns (`@/*`, `~/*`,
        # `@components/*`) start with characters that would otherwise
        # fall through to "external package". Real-world feedback: a
        # project with heavy alias use had 386 unresolved imports and
        # 32.1% bind rate; resolving aliases is the highest-leverage
        # correctness fix. See projmem/tsconfig.py.
        if root:
            from . import tsconfig as _tsc
            for cand_rel in _tsc.resolve_alias(spec, root):
                # Try the candidate both raw and with common source
                # extensions. Candidates from paths may already include
                # an extension (e.g. `@utils` → `src/utils/index`).
                candidates = [cand_rel]
                for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                            ".mts", ".cts", ".d.ts"):
                    candidates.append(cand_rel + ext)
                for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
                    candidates.append(os.path.join(cand_rel, idx))
                for c in candidates:
                    full = os.path.join(root, c)
                    if os.path.isfile(full):
                        return c.replace("\\", "/")

        # Node built-in convention: bare specifier like `require('buffer')`
        # or `require('node:buffer')` resolves to `lib/buffer.js`. Try
        # this as a fallback — if no matching file exists under lib/,
        # return None (still treat as external). Safe on non-Node repos
        # because most bare specs don't have a matching lib/X.js.
        stripped = spec.replace("node:", "", 1)
        if root and "/" not in stripped and stripped:
            for ext in (".js", ".mjs", ".cjs"):
                cand = os.path.join("lib", stripped) + ext
                if os.path.isfile(os.path.join(root, cand)):
                    return cand.replace("\\", "/")
        return None  # external package
    base = os.path.dirname(from_file)
    cand = os.path.normpath(os.path.join(base, spec))
    candidates = [cand]
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        candidates.append(cand + ext)
    for idx in ("index.js", "index.ts", "index.jsx", "index.tsx"):
        candidates.append(os.path.join(cand, idx))

    # TypeScript NodeNext / ESM convention: source uses `.js` extension in
    # the import specifier (`import "./foo.js"`) but the on-disk source is
    # `.ts` / `.tsx` / `.d.ts`. Without this rewrite, projmem can't resolve
    # any modern ESM TS project — every barrel reference looks unresolved
    # and reverse-deps collapse to empty.
    for js_ext, ts_exts in (
        (".js", (".ts", ".tsx", ".d.ts")),
        (".jsx", (".tsx",)),
        (".mjs", (".mts", ".ts")),
        (".cjs", (".cts", ".ts")),
    ):
        if cand.endswith(js_ext):
            stem = cand[: -len(js_ext)]
            for ext in ts_exts:
                candidates.append(stem + ext)

    for c in candidates:
        full = os.path.join(root, c) if root else c
        if os.path.isfile(full):
            return c.replace("\\", "/")
    return None


# ---- Orchestration ----

def index_all(cfg: Config, store: Store, paths: Optional[List[str]] = None,
              force: bool = False,
              extra_includes: Optional[List[str]] = None,
              extra_excludes: Optional[List[str]] = None,
              exclude_wins: bool = False) -> Dict[str, object]:
    store.set_meta("root", cfg.root)
    # Auto-snapshot contracts BEFORE any delete_file_data/wipe runs. This
    # gives `projmem contract-diff` a ref to compare against without the
    # user having to remember to snapshot first. The 'pre-index' label is
    # overwritten every time — users who want a pinned baseline run
    # `projmem snapshot <name>` explicitly.
    #
    # First-run gotcha: on a fresh repo the live `contracts` table is
    # empty before indexing, so this snapshot captures NOTHING. After
    # indexing populates contracts, the diff `pre-index → current` then
    # shows every contract as "added in this session" — which makes
    # `projmem complete`'s checklist flag 50+ false-positive open
    # obligations on the very first run. We detect that case below
    # (after indexing) and re-snapshot pre-index so the baseline reflects
    # the just-indexed state, not the pre-anything empty DB.
    pre_contract_count = 0
    pre_symbol_count = 0
    try:
        pre_contract_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
        pre_symbol_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    except Exception:
        pass
    try:
        store.snapshot_contracts("pre-index")
    except Exception:
        # Never let snapshot failure (e.g. corrupt prior DB) block indexing.
        pass
    try:
        store.snapshot_symbols("pre-index")
    except Exception:
        pass
    # `unchanged` counts files whose content hash matches the previous
    # index — a no-op revisit, not "excluded by rules". The earlier
    # `skipped` label confused agents (they read "skipped" as "filtered
    # out by glob") and asked why a `--include` narrowed run reported
    # `indexed: 0, skipped: N` (round-4 finding #4). The semantically
    # correct label is `unchanged`.
    counts: Dict[str, object] = {"indexed": 0, "unchanged": 0,
                                  "removed": 0, "excluded_dirs": [],
                                  "oversize_skipped": []}

    existing = {r["path"]: r for r in store.all_files()}
    seen: Set[str] = set()

    # file_edits session id. Each index run ties its hash transitions
    # together so `projmem changes` can group "all edits from the last
    # index run" cleanly. Token form: `<pid>-<epoch-ms>` — human-
    # readable, unique enough for this purpose.
    import time as _time_ss
    _edit_session_id = f"{os.getpid()}-{int(_time_ss.time() * 1000)}"

    includes = list(cfg.include_globs) + list(extra_includes or [])
    excludes = list(cfg.exclude_globs) + list(extra_excludes or [])

    if paths:
        iter_paths = [os.path.join(cfg.root, p) for p in paths]
    else:
        from .discovery import walk_with_excluded, consume_oversize_skips
        from . import ignore as _ignore_mod
        ignore_spec = _ignore_mod.load(cfg.root)
        iter_paths, excluded_top = walk_with_excluded(
            cfg.root, includes, excludes, cfg.max_file_bytes,
            exclude_wins=exclude_wins,
            ignore_spec=ignore_spec if ignore_spec else None)
        counts["excluded_dirs"] = excluded_top
        # Surface that .projmemignore was loaded so users know it took
        # effect (otherwise a typo in the file silently does nothing).
        if ignore_spec:
            counts["projmemignore_rules"] = len(ignore_spec.rules)
        # Capture files skipped for exceeding max_file_bytes so we can
        # emit a HIGH-severity warning in the stats output. Without this,
        # a 3.15 MB `checker.ts` (or any file crossing the default 3 MB
        # limit) vanishes from the index with no indication.
        counts["oversize_skipped"] = consume_oversize_skips()

    # M-feedback: persist last-index session metadata so `projmem scope`
    # can show the EXACT effective scope, not just config-level globs.
    import json as _json, time as _time
    store.set_meta("last_index_session", _json.dumps({
        "started_at": _time.time(),
        "include_globs_cli": list(extra_includes or []),
        "exclude_globs_cli": list(extra_excludes or []),
        "include_globs_config": list(cfg.include_globs),
        "exclude_globs_config": list(cfg.exclude_globs),
        "include_globs_effective": includes,
        "exclude_globs_effective": excludes,
        "exclude_wins": exclude_wins,
        "max_file_bytes": cfg.max_file_bytes,
        "force": force,
    }))

    for full in iter_paths:
        r = rel(full, cfg.root)
        seen.add(r)
        try:
            size = os.path.getsize(full)
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        lang = lang_of(full)
        prev = existing.get(r)
        h = hash_file(full)
        if not force and prev and prev["hash"] == h:
            counts["unchanged"] += 1
            continue

        # Append-only edit log. Record the hash transition so
        # `projmem changes` can reconstruct edit history even after the
        # `pre-index` snapshot has rotated past it. Emit ONLY when the
        # hash actually changed (skip force-reindex of unchanged files).
        prev_hash = prev["hash"] if prev else None
        edit_row_id: Optional[int] = None
        pre_syms: List[Tuple[str, str]] = []
        pre_contracts: List[Tuple[str, str]] = []
        if prev_hash != h:
            # Snapshot the OLD symbol / contract shape before delete so
            # we can compute a natural-language delta after re-indexing.
            # Phase C — makes `projmem changes` readable by describing
            # what actually changed, not just "file touched".
            try:
                pre_syms = [(row["name"], row["kind"]) for row in
                             store.conn.execute(
                                 "SELECT name, kind FROM symbols WHERE file=?",
                                 (r,))]
            except Exception:
                pre_syms = []
            try:
                pre_contracts = [(row["kind"], row["name"]) for row in
                                  store.conn.execute(
                                      "SELECT kind, name FROM contracts WHERE file=?",
                                      (r,))]
            except Exception:
                pre_contracts = []
            try:
                cur = store.conn.execute(
                    "INSERT INTO file_edits (path, prev_hash, new_hash, "
                    "ts, session_id) VALUES (?, ?, ?, ?, ?)",
                    (r, prev_hash, h, _time_ss.time(), _edit_session_id))
                edit_row_id = cur.lastrowid
            except Exception:
                pass

        store.delete_file_data(r)
        src = read_text(full)

        # Dispatch: tree-sitter first (multi-language, AST-grounded, high conf),
        # fallback to stdlib ast for Python, then regex heuristics as last resort.
        # Content-aware variant disambiguates `.h` headers (default C, but
        # most Node.js / V8 / LLVM headers are actually C++ — a misroute
        # silently drops every class / namespace symbol).
        ts_lang = (ts_backend.ts_lang_for_with_content(full, src)
                   if ts_backend.available() else None)
        parser_used = "none"
        # Skip contract extraction on artifact files (changelogs,
        # snapshots, generated code) AND on documentation-only files
        # (`.md`/`.txt`/`.rst`). DSL files like `.prisma`/`.sql`/
        # `.toml`/`.yaml` ARE legitimate contract surfaces and continue
        # to get extracted (audit P1#4 restored).
        from . import artifacts as _artifacts
        from .utils import is_doc_only
        skip_contracts = (is_doc_only(r)
                           or _artifacts.is_artifact_path(r))
        # Round-5-r3 F001: also skip the SYMBOL/EDGE extractor on
        # doc-only files. The earlier round gated JS_IMPORT to JS
        # langs, which closed the case for `.md` (lang=other) but
        # the user reported markdown code-block imports STILL
        # surfacing on real repos. Belt-and-braces: doc-only files
        # produce no symbols and no edges.
        skip_symbols = is_doc_only(r)
        if ts_lang and ts_backend.index(store, r, src, ts_lang, cfg.root):
            parser_used = f"treesitter:{ts_lang}"
            # Semantic contract layer still runs regex-based (language-agnostic patterns)
            if not skip_contracts:
                if lang == "python":
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", SyntaxWarning)
                            tree = ast.parse(src, filename=r)
                        semantic.scan_python_contracts(store, r, tree, src)
                    except SyntaxError:
                        semantic.scan_regex_contracts(store, r, src, lang)
                else:
                    semantic.scan_regex_contracts(store, r, src, lang)
        elif lang == "python":
            # `index_python` may degrade to regex for a single pathological
            # file (e.g. scip-python's maxParseDepth2.py). Record whichever
            # parser actually captured this file.
            parser_used = index_python(store, r, src, cfg.root)
        elif skip_symbols:
            # Round-5-r3 F001: don't run the regex backend on `.md` /
            # `.txt` / `.rst` / `.adoc` / `.org`. The fallback would
            # capture in-prose `function foo()` / `import x` lines and
            # surface them as confidence:medium symbols / edges,
            # poisoning reverse-dep counts.
            parser_used = "skipped:doc-only"
        else:
            index_regex(store, r, src, lang=lang,
                        confidence="medium" if lang in ("javascript", "typescript") else "low")
            parser_used = "regex"

        store.upsert_file(path=r, lang=lang, hash_=h, mtime=mtime, size=size,
                          parser=parser_used)
        # Round-X: parse package.json into structured contract entities.
        # Detected by basename, not extension, since `package-lock.json` etc.
        # should NOT be parsed as a contract surface.
        if os.path.basename(r) == "package.json":
            semantic.scan_package_json(store, r, src)
        counts["indexed"] += 1

        # Phase C: compute natural-language summary of what changed in
        # this file and attach it to the file_edits row. Makes
        # `projmem changes` output directly readable.
        if edit_row_id is not None:
            try:
                new_syms = [(row["name"], row["kind"]) for row in
                             store.conn.execute(
                                 "SELECT name, kind FROM symbols WHERE file=?",
                                 (r,))]
                new_contracts = [(row["kind"], row["name"]) for row in
                                  store.conn.execute(
                                      "SELECT kind, name FROM contracts WHERE file=?",
                                      (r,))]
                summary = _describe_delta(pre_syms, new_syms,
                                            pre_contracts, new_contracts,
                                            is_new=(prev_hash is None))
                if summary:
                    store.conn.execute(
                        "UPDATE file_edits SET summary=? WHERE id=?",
                        (summary, edit_row_id))
            except Exception:
                pass

    if paths is None:
        # Remove deleted files from index.
        #
        # Foot-gun fix (audit P0#2): when the user passed a narrow
        # `--include GLOB`, `seen` contains only the include-matching
        # files. Without the guard below we'd then purge EVERY file
        # outside the include — collapsing a 50k-file index to whatever
        # the user's glob matched. That's destructive and has no
        # recovery; it looked to users like the index was corrupted.
        #
        # Rule: only delete indexed files that the CURRENT walk was
        # AUTHORIZED to see. When `extra_includes` is non-empty the
        # authority scope is exactly files matching at least one of
        # those globs. Files outside that scope are left as-is — the
        # user can rebuild the full index with a follow-up `projmem
        # index` (no --include) when they want to clean up.
        from .discovery import _match_any as _dm_match
        narrowed = bool(extra_includes)
        for p in list(existing):
            if p in seen:
                continue
            if narrowed and not _dm_match(p, extra_includes):
                # File was outside the authorized scope of THIS walk —
                # we have no basis for deleting it.
                continue
            # Record deletion in the edit log before removing the row,
            # so the session can see a `new_hash=NULL` entry.
            try:
                store.conn.execute(
                    "INSERT INTO file_edits (path, prev_hash, new_hash, "
                    "ts, session_id) VALUES (?, ?, NULL, ?, ?)",
                    (p, existing[p]["hash"], _time_ss.time(),
                     _edit_session_id))
            except Exception:
                pass
            store.remove_file(p); counts["removed"] += 1

    # user-declared contracts
    semantic.apply_user_contracts(store, cfg)
    # entrypoints
    entrypoints.detect(store, cfg)
    # M8.5: post-index pass — re-resolve `extends`/`implements` edges that
    # may have been written before all symbols were known. If a target name
    # now resolves to MULTIPLE symbols, downgrade the edge from canonical-id
    # back to bare name + confidence=medium so consumers know it's ambiguous.
    _reresolve_inheritance_edges(store)
    # Audit P2#8 — bind aliased default imports to the source file's
    # default export. Without this pass, `import renamed from './lib'`
    # produces a module edge but ZERO refs to `lib`'s default-exported
    # symbol, so `reverse src/lib.js#helpfulFn` finds no consumers.
    _resolve_default_import_aliases(cfg, store)
    # Post-index binding pass: resolve ref.target_symbol_id for every ref
    # whose name is uniquely resolvable (same-file, imported, or globally
    # unique). Leaves ambiguous refs unbound so consumers see the split.
    from . import binding as _binding
    counts["binding"] = _binding.resolve_refs(store)

    # First-run baseline fix: when the index was empty BEFORE this run,
    # the pre-index snapshot we took at the top is also empty. After
    # indexing populates contracts/symbols, that empty baseline would
    # make `projmem complete`'s checklist flag every contract as
    # "added in this session" — a false positive on the first ever
    # `projmem index` call. Re-snapshot pre-index AT THE INDEXED STATE
    # so the next checklist run starts from a clean baseline.
    if pre_contract_count == 0 and pre_symbol_count == 0:
        try:
            store.snapshot_contracts("pre-index")
        except Exception:
            pass
        try:
            store.snapshot_symbols("pre-index")
        except Exception:
            pass
        counts["first_run_baseline_set"] = True

    store.commit()
    return counts


def _describe_delta(pre_syms: List[Tuple[str, str]],
                     new_syms: List[Tuple[str, str]],
                     pre_contracts: List[Tuple[str, str]],
                     new_contracts: List[Tuple[str, str]],
                     *, is_new: bool = False,
                     max_names: int = 4) -> str:
    """Produce a one-line natural-language summary of per-file deltas.

    Examples:
      "newly indexed: 4 symbol(s), 2 contract(s)"
      "added handleWebhookDeliverJob, PayloadSchema; removed oldFn"
      "added 3 contract(s) (env:DATABASE_URL, flag:debug, ...)"
      "touched (no symbol/contract changes)"
    """
    if is_new:
        parts = []
        if new_syms:
            parts.append(f"{len(new_syms)} symbol(s)")
        if new_contracts:
            parts.append(f"{len(new_contracts)} contract(s)")
        if not parts:
            return "newly indexed"
        return "newly indexed: " + ", ".join(parts)

    pre_s = set(pre_syms)
    new_s = set(new_syms)
    added_syms = sorted(new_s - pre_s)
    removed_syms = sorted(pre_s - new_s)

    pre_c = set(pre_contracts)
    new_c = set(new_contracts)
    added_c = sorted(new_c - pre_c)
    removed_c = sorted(pre_c - new_c)

    clauses: List[str] = []
    if added_syms:
        names = [n for (n, _) in added_syms[:max_names]]
        tail = f" (+{len(added_syms) - max_names} more)" \
                if len(added_syms) > max_names else ""
        clauses.append("added " + ", ".join(names) + tail)
    if removed_syms:
        names = [n for (n, _) in removed_syms[:max_names]]
        tail = f" (+{len(removed_syms) - max_names} more)" \
                if len(removed_syms) > max_names else ""
        clauses.append("removed " + ", ".join(names) + tail)
    if added_c and not added_syms and not removed_syms:
        # Only mention contracts when they're the main change — otherwise
        # the symbol summary is already enough.
        ctx = [f"{k}:{n}" for (k, n) in added_c[:max_names]]
        tail = f" (+{len(added_c) - max_names} more)" \
                if len(added_c) > max_names else ""
        clauses.append(f"added {len(added_c)} contract(s) ("
                        + ", ".join(ctx) + tail + ")")
    if not clauses:
        return "touched (no symbol/contract changes)"
    return "; ".join(clauses)


_DEFAULT_IMPORT_RX = re.compile(
    r"""^[ \t]*import\s+([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _resolve_default_import_aliases(cfg: Config, store: Store) -> None:
    """Audit P2#8: emit a synthetic ref from each default-import site
    to the source file's default-exported symbol.

    Why: `import renamed from './lib'` produces a module edge but
    NO ref linking the caller to `lib`'s default export name. So
    `projmem reverse src/lib.js#helpfulFn` returns empty even though
    `caller.js` clearly uses `helpfulFn` (under the alias `renamed`).
    This pass walks every JS/TS file, finds default-import lines via
    regex, resolves the source file, and emits a `default_import`
    ref for the inferred default-export symbol.

    Conservative: only emits when the source file has EXACTLY ONE
    obvious default-export candidate. Ambiguous cases stay unbound
    rather than guessing wrong.
    """
    files = list(store.conn.execute(
        "SELECT path FROM files WHERE lang IN ('javascript','typescript')"))
    for row in files:
        rel_path = row["path"]
        full = os.path.join(cfg.root, rel_path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for m in _DEFAULT_IMPORT_RX.finditer(src):
            alias = m.group(1)
            spec = m.group(2)
            # Skip namespace/named-import patterns the regex may catch
            # (it specifically requires `import IDENT from`, not `{...}`).
            target = resolve_js_import(spec, rel_path, cfg.root)
            if not target:
                continue
            # Find the file's default-exported symbol. Heuristics, in
            # order: (a) symbol whose `kind` is 'default_export' or
            # whose name is literally 'default'; (b) sole exported
            # symbol; (c) skip — too ambiguous.
            cands = list(store.conn.execute(
                "SELECT name, kind, line FROM symbols "
                "WHERE file=? AND exported=1", (target,)))
            chosen = None
            for c in cands:
                if c["kind"] in ("default_export", "default") \
                        or c["name"] == "default":
                    chosen = c
                    break
            if chosen is None and len(cands) == 1:
                chosen = cands[0]
            if chosen is None:
                continue
            line = src.count("\n", 0, m.start()) + 1
            # Avoid duplicates if pass is re-run (idempotent).
            existing = store.conn.execute(
                "SELECT 1 FROM refs WHERE file=? AND name=? AND line=? "
                "AND kind='default_import' LIMIT 1",
                (rel_path, chosen["name"], line)).fetchone()
            if existing:
                continue
            store.add_ref(file=rel_path, name=chosen["name"],
                           kind="default_import", line=line,
                           confidence="medium")
    store.commit()


def _reresolve_inheritance_edges(store: Store) -> None:
    """Walk inheritance edges and verify each canonical-id target is still
    uniquely resolvable. If not, downgrade dst → bare name, confidence →
    medium. Closes the indexing-order race."""
    rows = list(store.conn.execute(
        "SELECT id, src, dst, type FROM edges "
        "WHERE type IN ('extends','implements')"))
    for row in rows:
        dst = row["dst"]
        if "#" not in dst:
            continue  # already a bare name
        # Parse to bare name
        bare = dst.split("#", 1)[1].rstrip("#./!")
        same_name_count = store.conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE name=?", (bare,)).fetchone()[0]
        if same_name_count > 1:
            store.conn.execute(
                "UPDATE edges SET dst=?, confidence='medium' WHERE id=?",
                (bare, row["id"]))


def check_staleness(cfg: Config, store: Store) -> List[str]:
    """Mark files whose on-disk hash no longer matches as stale; return list of stale paths.

    NOTE: this only flags files already in the index (modified or deleted).
    For a complete view including newly-added files, use ``discover_changes``."""
    stale = []
    for row in store.all_files():
        full = os.path.join(cfg.root, row["path"])
        if not os.path.isfile(full):
            store.mark_stale(row["path"]); stale.append(row["path"]); continue
        if hash_file(full) != row["hash"]:
            store.mark_stale(row["path"]); stale.append(row["path"])
    store.commit()
    return stale


def discover_changes(cfg: Config, store: Store) -> Dict[str, List[str]]:
    """Walk the disk, compare against the index, return all changes.

    Returns:
      {
        "modified": [paths whose on-disk hash diverges from indexed hash],
        "added":    [paths on disk that aren't in the index yet],
        "deleted":  [paths in the index that are no longer on disk],
      }

    This is the complete view of "what does the index need to do to match
    disk reality?" — the load-bearing primitive for `projmem complete`
    and the agent's end-of-task workflow.

    Cheap: a single disk walk + per-file hash on candidates only. Honors
    the same exclude/include globs as `index_all`.
    """
    indexed: Dict[str, str] = {row["path"]: row["hash"]
                                for row in store.all_files()}
    on_disk: set = set()
    # walk() yields absolute paths; convert to repo-relative for the
    # comparison set so it lines up with `files.path` rows.
    for absolute in walk(cfg.root, cfg.exclude_globs, cfg.include_globs,
                          max_bytes=cfg.max_file_bytes,
                          exclude_wins=getattr(cfg, "exclude_wins", False)):
        on_disk.add(rel(absolute, cfg.root))

    added = sorted(p for p in on_disk if p not in indexed)
    deleted = sorted(p for p in indexed if p not in on_disk)
    # For files present in BOTH, hash-compare to detect modifications.
    modified: List[str] = []
    for p in on_disk:
        if p not in indexed:
            continue
        full = os.path.join(cfg.root, p)
        try:
            current = hash_file(full)
        except OSError:
            continue
        if current != indexed[p]:
            modified.append(p)
    modified.sort()

    return {"modified": modified, "added": added, "deleted": deleted}
