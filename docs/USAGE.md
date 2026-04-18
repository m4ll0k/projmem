# Usage — `projmem`

## Commands

| Command | Purpose |
|---|---|
| `projmem index [--force] [--exclude GLOB ...] [--include GLOB ...]` | Walk the repo, index files, rebuild edges and contracts. Skips unchanged files by hash unless `--force`. `--exclude`/`--include` are repeatable and merge with `.projmem/config.json::exclude_globs`/`include_globs`. |
| `projmem stats` | Counts per table. |
| `projmem status` | Counts + list of stale files. |
| `projmem refresh [--reindex]` | Detect stale files; optionally reindex them. |
| `projmem doctor [--skip-stale-check]` | One-shot health check: tree-sitter, foreign index, oversize skips, parser coverage, stale-file sampling, artifact bleed. Exit code 1 when any HIGH finding exists. |
| `projmem checklist [--base LABEL] [--head LABEL\|current]` | Post-edit completeness gate: open contract obligations, dangling symbol refs, and broken repo-relative imports. Exit code 1 when any HIGH finding exists. |
| `projmem symbol <name>` | Defs + refs for a symbol. AST-derived where available. |
| `projmem reverse <file>` | Reverse + forward deps for a file. |
| `projmem pack <target> [--radius N] [--no-tests] [--write] [--markdown] [--name N]` | Build a context pack (JSON, optionally Markdown). Target shapes: `path/to/file.ext`, `path/to/file.ext#symbol` (disambiguate), `SymbolName`. Ambiguous symbol names trigger an `ambiguous-symbol` unknown with alternatives. |
| `projmem note add\|list\|delete\|search ...` | Persistent annotations (notes) on files/symbols/subsystems; surfaced at the top of `pack` output. |
| `projmem note-verify <target>` | Revalidate notes: recompute fingerprint, verify structured claims, update staleness + decayed confidence. |
| `projmem audit <target>` | Aggregate claim-level verdicts across notes on a target — what beliefs are still true vs refuted. |
| `projmem integrity <target>` | Per-target integrity score (freshness, contradictions, ambiguity, structural coverage). |
| `projmem callgraph <file> [--filter-to SYMBOL] [--limit N]` | Intra-file call graph. **Fail-loud**: if the file has no indexed symbols, returns `partial: true` with concrete `warnings` and refuses to emit edges without nodes. Caller is the nearest preceding function/method (scope approximation). |
| `projmem contracts <target-or-name> [--kind flag\|env\|schema_field\|token\|event\|guard]` | List contracts in a file or by contract name. New kinds: `event` (emit/listen pairs) and `guard` (Python if-guarded calls). |
| `projmem entrypoints [--hide-unindexed]` | List detected/declared entrypoints. Each row carries `indexed: 0|1`; entries pointing at files outside the index are flagged and the command surfaces a top-level `warning`. |
| `projmem explain <target>` | Pack rendered as Markdown (fast human read). |
| `projmem orphans [--exported-only] [--kind K] [--all-kinds] [--limit N]` | Symbols defined but referenced nowhere. **Defaults to structural kinds only** (function/class/method/exported/var/interface/type/enum/struct/trait/module/object) so comment/token noise stays out. Use `--all-kinds` to disable the filter. Heuristic. |
| `projmem parity [--kind K] [--all-kinds] [--limit N]` | Referenced-but-undefined + defined-but-unreferenced. Same default kind filter as `orphans`. |
| `projmem events [--name N] [--file F]` | Every event-name in the repo with emitters/listeners. Flags `emit_only` and `listen_only` names so you can catch the "listener registered after emit" bug class. JS/TS idioms: `.on/.once/.addListener/.addEventListener/.emit/.dispatchEvent/.trigger/.fireEvent`. |
| `projmem reach <symbol>` | Under what `if`-guard conditions is this symbol called? Python only in this round; surfaces the raw condition text so reachability failures like "gate is always false" become visible. |
| `projmem git <file> [--limit N]` | Recent commits touching a file (optional, constrained). |
| `projmem files` | Emit the indexed file list + config-level include/exclude + builtin skip dirs. |
| `projmem scope` | Print the **EXACT effective scope of the last `index` run** — CLI globs, config globs, `exclude_wins` mode, builtin skips. Use this to reproduce a session. |
| `projmem missing-paths [--scope-only] [--limit N]` | Repo-wide check for referenced files that don't exist on disk. Each row carries `in_scope: bool` based on the source file's path; pass `--scope-only` to drop vendor-self-references. |
| `projmem unresolved-imports [--only-missing-on-disk] [--kind K] [--show-external] [--scope-only] [--limit N]` | Repo-wide unresolved-import edges. Each row tagged with `import_kind ∈ {repo_relative, external_module, external_include, external_root_import}` and `in_scope: bool`. Default hides `external_include`/`external_root_import` (vendor headers). |
| `projmem contract-drift [--kind K] [--scope-only] [--limit N]` | Contract names whose values vary across sites. Each row tagged `in_scope: bool` (False if ANY site is under a vendor prefix); `--scope-only` filters to own-code-only drift. |
| `projmem evidence <file.jsonl>` | Ingest runtime evidence entries. Each JSONL row: `{"file": "...", "symbol": "...", "kind": "trace|log|event", "note": "..."}`. |
| `projmem evidence-query <target>` | List runtime evidence rows touching a symbol or file (direct matches + def-file-scoped matches). |
| `projmem drift [--kind K] [--all-kinds] [--limit N]` | **Static ↔ runtime drift** — structural symbols with zero runtime evidence. The unique L3 query: "my runtime never took this code path". Warns explicitly when no evidence has been ingested. |

All commands accept `--path <project_root>` (default: cwd) and emit JSON.

## Command flag reference

Full per-command flag listing for every command in the table above.

### `projmem index`

| Flag | Default | Meaning |
|---|---|---|
| `--force` | off | Re-parse every file, skipping the hash-fresh shortcut. |
| `--exclude GLOB` (repeatable) | — | Skip files/dirs. Merges with `.projmem/config.json::exclude_globs`. |
| `--include GLOB` (repeatable) | — | Only index matching paths. By default `--include` wins over `--exclude` when both match (an include reaching into an excluded dir forces descent). |
| `--exclude-wins` | off | Reverses precedence: EXCLUDE always trumps INCLUDE. Lets you `--include 'deploy/**' --exclude 'deploy/patches/**'` to keep wrappers but drop a vendor subtree. |

Env: `PROJMEM_TS_TIMEOUT_MS` (default 20000) — per-file tree-sitter wall-clock timeout. `PROJMEM_DEBUG=1` — prints per-file fallback reasons.

### `projmem pack <target>`

| Flag | Default | Meaning |
|---|---|---|
| `--radius N` | 1 | Structural-expansion radius. |
| `--no-tests` | off | Skip test-file association. |
| `--write` | off | Write the pack to `.projmem/packs/<name>.json`. |
| `--markdown` | off | Also render the human-readable Markdown pack. |
| `--name NAME` | auto | Override the written filename. |
| `--as-file` | off | Force file-target interpretation. Use when a file basename collides with a symbol name. |
| `--as-symbol` | off | Force symbol-target interpretation. Use when a recognized file basename happens to also be a symbol you want to query. |
| `--snippets` | off | Include bounded source-code excerpts around the target — skips a separate file-read round-trip. Snippets are tagged `confidence: low` because they're a copy of disk content. |
| `--snippet-bytes N` | 8000 | Hard byte budget for `--snippets` payload. |

Target shapes: `path/to/file.ext`, `path/to/file.ext#symbol` (disambiguate), `SymbolName`. Ambiguous bare symbol names narrow to the first def and emit an `ambiguous-symbol` unknown.

**Reverse-dep semantics (round-4):** each entry in `reverse_dependencies`
carries a `via` field naming the relationship:

- `via: "imports"` — file-level import edge (top-of-file `require`/`import`).
- `via: "call"` / `"new"` / `"import_binding"` / `"callback"` / `"shorthand"` — symbol-level reference. Surfaced when target is a symbol so the pack reflects callers that don't go through a top-level import edge (e.g. same-file call sites in a monolith).
- `via: "pair_inspect"` — user-declared `.projmem/config.json` pair rule.

If you need only the same-file callers of a symbol, `intra_file_for_symbol` still carries the pre-sliced `incoming`/`outgoing` shape.

### `projmem callgraph <file>`

| Flag | Default | Meaning |
|---|---|---|
| `--filter-to SYMBOL` | — | Show only edges involving this symbol (incoming + outgoing). |
| `--in-function FN` | — | Show only calls made FROM inside `FN`, in source-line order. |
| `--limit N` | 300 | Max edges returned. `by_caller_count` is ALWAYS computed from the full edge set before truncation. |
| `--all` | off | Disable edge truncation (equivalent to `--limit 0`). |

Every mode returns the same stable envelope: `file`, `nodes`, `edges`, `total`, `truncated`, `by_caller_count`, `filter_symbol`, `in_function`, `partial`, `warnings`, `parser`, `note`. Filter modes add mode-specific keys (`incoming`/`outgoing`, `calls`/`unique_callees`) on top.

### `projmem contracts <target>`

| Flag | Default | Meaning |
|---|---|---|
| `--kind KIND` | — | One of `flag`, `env`, `schema_field`, `token`, `event`, `guard`. **Applies to BOTH file-targets and name-targets.** Output includes `kind_filter` so the consumer can verify. |

### `projmem orphans`

| Flag | Default | Meaning |
|---|---|---|
| `--exported-only` | off | Only list symbols marked exported. |
| `--kind KINDS` | structural | Comma-separated kinds to include. Default keeps out `token` and other non-structural noise; structural allowlist: `function,class,method,exported,var,interface,type,enum,struct,trait,module,object`. |
| `--all-kinds` | off | Disable the structural-kind filter entirely. |
| `--limit N` | 500 | Max rows returned. |

Emits `lower_bound_warning` when any file was parsed by the regex backend — counts are a lower bound there.

### `projmem parity`

| Flag | Default | Meaning |
|---|---|---|
| `--kind KINDS` | structural | Same meaning as `orphans --kind`; applies to the `defined_but_unreferenced` side only. |
| `--all-kinds` | off | Disable the structural filter. |
| `--limit N` | 500 | Cap both output sides. |

`referenced_but_undefined` is filtered for language builtins (JS: `String/Number/Array/Object/console/…` and common method names like `length/map/push/toString/…`; Python: `print/len/range/list/dict/…`). Use `--include-builtins` to disable the filter.

### `projmem events`

| Flag | Default | Meaning |
|---|---|---|
| `--name NAME` | — | Filter to a single event name. |
| `--file SUBSTR` | — | Filter to files whose path contains this substring. |

Each entry carries `emit_only` and `listen_only` booleans — the two most interesting cases.

### `projmem reach <symbol>`

No flags. Python only (JS deferred). Lists guard conditions under which the symbol is called — raw source text of the enclosing `if`.

### `projmem drift`

| Flag | Default | Meaning |
|---|---|---|
| `--kind KINDS` | structural | Same semantics as `orphans --kind`. |
| `--all-kinds` | off | Disable the structural filter. |
| `--limit N` | 500 | Cap results. |

Returns `warning: "no evidence ingested"` when the evidence table is empty rather than labelling every static symbol as drift.

### `projmem evidence-query <target>`, `projmem evidence <file.jsonl>`, `projmem git <target>`, `projmem files`

No sub-flags worth surfacing beyond the command-table description.

### `projmem unresolved-imports`

| Flag | Default | Meaning |
|---|---|---|
| `--only-missing-on-disk` | off | Only show specs that look like a relative path but aren't found on disk (likely moved/renamed files). |
| `--limit N` | 500 | Cap results. |

### `projmem entrypoints`

| Flag | Default | Meaning |
|---|---|---|
| `--hide-unindexed` | off | Suppress entries whose target file is not in the index. |

Output always includes `unindexed_count` and a top-level `warning` when non-indexed entries are present.

### `projmem refresh`

| Flag | Default | Meaning |
|---|---|---|
| `--reindex` | off | Reindex stale files as part of the refresh. |

### `projmem doctor`

| Flag | Default | Meaning |
|---|---|---|
| `--skip-stale-check` | off | Skip the on-disk hash sampling (useful in CI). |

Exit code: 1 when any HIGH finding exists.

### `projmem checklist`

| Flag | Default | Meaning |
|---|---|---|
| `--base LABEL` | `pre-index` | Base snapshot label (auto-taken at the start of every `projmem index`). |
| `--head LABEL\|current` | `current` | Head snapshot label or live tables (`current`). |
| `--limit N` | 200 | Cap items per finding. |
| `--include-vendor` | off | Include vendor/out-of-scope paths (default: scope-only). |
| `--include-artifacts` | off | Include artifact refs (snapshots/changelogs/build output) in dangling-ref checks. |

Exit code: 1 when any HIGH finding exists. Intended as a post-edit "did I forget anything?" gate.

### Notes (`projmem note`, `note-verify`, `audit`)

Notes are persistent annotations on a target string. Targets can be a file path,
`file#symbol` shorthand, `@project` for project-wide context, or a directory
prefix ending in `/` (e.g. `src/auth/`) for subsystem notes.

`projmem pack <file|symbol>` includes `@project` and directory-prefix notes for
file-backed targets so agents see the big picture before acting.

### `projmem callees-of <symbol>` (NEW this round)

| Flag | Default | Meaning |
|---|---|---|
| `--depth N` | 3 | Max transitive depth. Depth 1 = direct callees only. |
| `--file FILE` | — | Narrow to a single file (useful for monoliths). |
| `--limit N` | 1000 | Max nodes visited. Prevents runaway fan-out. |

Answers "blast-radius of changing this function": transitively follow intra-file call edges from the given symbol.

## Target syntax (`pack`, `explain`)

| Shape | Meaning |
|---|---|
| `path/to/file.ext` | File target. Pack is scoped to that file. |
| `path/to/file.ext#symbol` | **Canonical disambiguation.** Pack is scoped to that symbol **in that file only.** No cross-file merging, no ambiguity warning. |
| `path/to/file.ext:symbol` | Same as `#` form; tolerated for ergonomics. Only applies when the left side actually exists as an indexed file. |
| `SymbolName` | Symbol lookup across the whole repo. If the name resolves to more than one definition, the pack emits an `ambiguous-symbol` unknown listing every alternative and narrows the context to the first definition — **it does not silently merge contexts from sibling definitions.** Re-run with `file#symbol` to pick one. |

Example, on a repo with two `cdpCallOptional` definitions (`scanner.js` and
`controller/cdp_utils.js`):

```bash
projmem pack cdpCallOptional                          # warns: ambiguous-symbol
projmem pack 'scanner.js#cdpCallOptional'             # scoped, no warning
projmem pack 'controller/cdp_utils.js#cdpCallOptional'
```

## Same-file refs and lower-bound counts

`projmem symbol NAME` annotates every returned def/ref row with the `parser`
that captured it, and the top-level output includes:

```json
{
  "ref_count": 4,
  "ref_count_is_lower_bound": true,
  "refs_from_regex_parsers": 3
}
```

Rules to read:

- **`ref_count_is_lower_bound: false`** → the count is exact (all refs came from AST parsers).
- **`ref_count_is_lower_bound: true`** → real count ≥ `ref_count`. The regex backend dedups at `(name, line)` and can only see syntactic call sites (`foo(`, `.foo(`) — it will miss non-call references, template-string-embedded calls, and any call that doesn't match the regex.

The LLM consumer must treat lower-bound-marked counts as "at least this many,
possibly more", not as ground truth.

## Intra-file call graph (monolith files)

`projmem callgraph <file>` reports the caller → callee edges **inside one
file**. This is the one piece the `pack` view historically couldn't answer on
monolithic code (e.g. a 12k-line `scanner.js`):

```bash
projmem callgraph scanner.js                                 # full file
projmem callgraph scanner.js --filter-to cdpCallOptional     # incoming + outgoing for one symbol
```

Output shape:

```json
{
  "file": "scanner.js",
  "edges": [{"from": "scanUrl", "to": "cdpCallOptional", "line": 742}, ...],
  "total": 1834,
  "truncated": true,
  "by_caller_count": {"scanUrl": 41, "cdpCallOptional": 17, ...},
  "note": "Caller is the nearest preceding function/method in the file (scope approximation). Does not resolve nested anonymous closures. Confidence: medium."
}
```

`build_pack` also attaches `intra_file_calls` to every file-target pack, and
additionally attaches **`intra_file_for_symbol`** (pre-sliced `incoming` +
`outgoing` edges for the target symbol) when the target is `file#symbol` —
so a symbol-scoped pack tells you the same-file callers and callees without
a second `callgraph --filter-to` call. The caller attribution is a
**scope approximation** — a ref inside a nested anonymous closure is charged
to the nearest preceding named function. Clearly noted on the output; use it
as a navigation aid.

## Confidence and coverage

Every pack has:

```json
"coverage": {
  "structural_confidence": "high",
  "contract_confidence": "medium",
  "overall_confidence": "medium",
  "by_level": {
    "structural": {"high": 5},
    "contract":   {"medium": 8, "low": 3}
  },
  "heuristic_edges": 11,
  "total_edges": 16,
  "parser_used": "treesitter:python",
  "unresolved_count": 0,
  "note": "..."
}
```

- **`structural_confidence`** — navigation quality across imports, reverse deps, and symbol refs. Read this first. If it's `high`, the pack's "which files should I open?" question has an AST-grounded answer.
- **`contract_confidence`** — quality of the semantic co-occurrence edges (tokens, schema fields). Mostly `medium`/`low` by nature; these are heuristics.
- **`overall_confidence`** — lower bound across both. Kept for back-compat. **Do not use it to gate reads**: a token regex hit dragging it to `low` does not invalidate a `high` structural section.

**Rule of thumb:** trust `structural_confidence` for navigation; treat any `contract_*` edge as a hypothesis.

## Unknowns

`pack.unknowns` is a list of explicit caveats. Every kind:

| `kind` | Trigger | Action |
|---|---|---|
| `symbol-undefined` | Symbol target has no def in the index. | Check spelling, or index more paths. |
| `symbol-not-in-file` | Used `file#symbol` but the symbol is not defined in that file. | `projmem symbol NAME` may show it elsewhere. |
| `file-not-indexed` | Used `file#symbol` but the file itself isn't indexed. | Run `projmem index`; check `--exclude` patterns. |
| `ambiguous-symbol` | Bare symbol target has >1 definition across files. Pack narrows to the first and lists `alternatives`. | Re-run with `file#symbol`. |
| `unresolved-imports` | Imports that could not be mapped to a file **and** are not classified as known external (e.g. Node builtins). | Often dynamic specifiers or external packages you don't index. |
| `entrypoint-unknown` | No detected or declared entrypoint reaches the target. | Declare one in `.projmem/config.json::entrypoints`. |
| `stale-index` | On-disk hash differs from the stored hash for a file in the pack. | Run `projmem refresh --reindex`. |

## Entrypoints

Auto-detected patterns (high confidence unless noted):

| Language | Pattern |
|---|---|
| Python | `if __name__ == "__main__":` guard; filenames like `__main__.py`, `manage.py`, `wsgi.py`, `asgi.py`, `app.py`, `main.py`, `cli.py` (medium). |
| Go | `package main` + `func main()` in the same file. |
| Rust | `fn main()`. |
| C / C++ | `int main(...)`. |
| Java | `public static void main(String[] args)`. |
| JavaScript / TypeScript | `require.main === module` (Node CommonJS, high); `import.meta.url` + `process.argv` (ESM, medium); filenames like `index.js`, `server.js`, `main.js`, `cli.js` (medium). |
| Node `package.json` | `main`, `module`, and `bin.*` entries (high). |

User-declared entrypoints in `.projmem/config.json::entrypoints` always
override with `confidence: high`. Entrypoints are deduplicated on `(file, kind)`
and the table is rebuilt from scratch on every `index` run.

## Node built-in imports

`fs`, `path`, `http`, `crypto`, `stream`, etc. — including `node:`-prefixed
forms and submodules like `fs/promises` — are recorded as **known external**
edges with `dst = "builtin:node:<name>"` and `confidence: high`. They do **not**
show up under `unresolved-imports`. This removes a large chunk of noise on any
Node codebase.

## Static ↔ runtime loop (L3)

The unique layer `projmem` occupies vs. LSP / grep / long-context: ingested
runtime evidence joined back to the static symbol/file graph.

```bash
# 1. Your runtime writes a JSONL log. Each line references the file or symbol
#    the runtime touched. Minimal schema:
#      {"file": "src/scanner.py", "symbol": "run_probe",
#       "kind": "trace", "note": "iteration 1"}
projmem evidence path/to/run.jsonl

# 2. Given a target, show what the runtime said about it.
projmem evidence-query run_probe

# 3. The drift query — the part no other tool gives:
projmem drift --kind function
# -> structural functions that NO runtime evidence ever touched.
#    Real positives: dead code, always-false gates, unreachable paths.
#    False positives: rarely-hit branches, external callers, dynamic dispatch.
```

**Scope of the signal.** Drift is only as good as your runtime ingester's
coverage. If your ingester only logs top-level entrypoints, only those
count as "exercised" and most symbols will be drift. Write a richer tracer
to get a stronger drift signal.

**When drift is decisive (pwnpilot-shape):** scanners, fuzzers, compilers,
agents — anything that produces structured per-run JSONL. The LLM reviewer
can see "static says Tier C runs under condition X; runtime shows Tier C
fired 0 times across 200 runs" → immediate signal to audit the gate.

**When drift is weak:** pure libraries, greenfield code, UI apps without
instrumentation. The loop needs both ends.

## Performance on monolith files

Tree-sitter can wedge on adversarial AST shapes (huge template literals
with nested `${...}` chains — exactly what pwnpilot's `scanner.js` does
with V8/Blink hooks embedded as JS strings inside JS strings). `projmem`
enforces a per-file **wall-clock timeout** on parse+query (POSIX only,
20 000 ms by default):

```bash
PROJMEM_TS_TIMEOUT_MS=30000 projmem index --force
PROJMEM_DEBUG=1 projmem index --force   # prints per-file fallback reasons
```

On timeout, the file falls back to the regex backend and `projmem stats`
shows it in `parser_distribution` as `regex`. No file ever blocks the
index beyond the timeout.

## SQLite concurrency

The store opens in WAL mode with a 30-second `busy_timeout`, so reader
commands (`stats`, `symbol`, `pack`, `callgraph`) can run while a long
`index` is in progress. Round-2 report's "database is locked" on
concurrent `stats` is no longer a failure mode.

## Excluding directories

Common big directories (`node_modules`, `.venv`, `vendor`, generated builds)
should be skipped. Three ways, merged together:

1. **Built-in** skip list: `node_modules`, `__pycache__`, `.venv`, `dist`,
   `build`, `target`, `coverage`, `.git`, etc. — hard-coded in
   `projmem/utils.py::SKIP_DIRS`.
2. **CLI flags** (repeatable, good for one-off): `projmem index --exclude
   'node_modules/**' --exclude 'chrome/src/**' --exclude vendor`.
3. **Config**: `.projmem/config.json::exclude_globs` — persistent.

Patterns support `*`, `?`, and `[abc]`. A trailing `/**` or `/*` prunes at the
directory level (no descent) — so excluding `node_modules/**` is cheap even on
huge trees.

**Precedence:** `--include` wins over `--exclude` when both match. You can
do `--exclude 'chrome/**' --include 'chrome/src/**'` and get exactly
`chrome/src/` indexed; the directory walker recognizes the include reaches
under the excluded subtree and descends anyway.

The `index` output reports `excluded_dirs` at the repo root so you can see
"these exist but I'm not analyzing them":

```bash
$ projmem index --exclude '.venv/**'
{ "indexed": 63, "excluded_dirs": [".projmem", ".pytest_cache", ".venv"] }
```

Use `--include 'src/**'` to do the opposite — only index matching paths.

## Declaring contracts

Create `.projmem/config.json`:

```json
{
  "entrypoints": ["src/cli.py"],
  "contracts": {
    "flags": ["live-logs", "verbose"],
    "env": ["DATABASE_URL"],
    "schema_fields": ["status", "evidence"],
    "tokens": ["DONE", "IN_PROGRESS"],
    "pairs": [{"if_touch": "src/auth.py", "inspect": ["tests/test_auth.py"]}]
  }
}
```

Declared contracts get `confidence: high` and `role: declare`. `pairs` become `pair_inspect` edges that show up in `reverse`/`pack` output.

## Parser backends and when same-file refs degrade

`projmem stats` reports which backend parsed each file:

```json
"parser_distribution": {
  "treesitter:javascript": 14,
  "treesitter:python": 12,
  "regex": 3,
  "ast": 0
},
"tree_sitter_available": true
```

- **`treesitter:<lang>`** — AST-grounded indexing, `confidence: high` on symbols, imports, and call-site refs. Same-file intra-file call tracking works correctly.
- **`ast`** — Python stdlib `ast` fallback when tree-sitter isn't installed.
- **`regex`** — last-resort heuristic path. Symbols and imports still captured (medium/low); same-file call-site refs are captured but labelled `confidence: low`.

If any JS/TS file shows up as `regex` while `tree_sitter_available: true`, the
grammar fell back on that specific file — run with `PROJMEM_DEBUG=1 projmem
index --force` to see the reason on stderr (parse error, query error, etc).

If `tree_sitter_available: false`, install the extra:

```bash
pip install -e '.[treesitter]'
```

Without it, JS/TS same-file refs still land under the regex backend (real
call sites, de-duped to one per line), but `callgraph` output on big monolith
files will be noisier than with AST.

The default `max_file_bytes` is **3 MB** so a 12k-line monolith like
`scanner.js` is not silently skipped. Override in `.projmem/config.json` if
you have larger files.

## Monolith workflow (big single files)

For codebases where most logic sits in one 5k+ line file and the module's own
imports don't reveal the internal call structure (a scanner, a reducer, a
bundled worker):

```bash
# 1. Disambiguate your target if the symbol name is used elsewhere.
projmem pack 'scanner.js#cdpCallOptional' --markdown

# 2. Ask the in-file question the pack can't answer on its own.
projmem callgraph scanner.js --filter-to cdpCallOptional

# 3. If the callgraph flags a caller you don't recognize, pack it too.
projmem pack 'scanner.js#scanUrl' --markdown
```

Read `structural_confidence` first. `contract_confidence` on a monolith is
usually `medium` or `low` by nature (tokens/schema fields are heuristics) —
that should not stop you trusting the structural section.

## Finding dead code & typos

| Command | What it answers |
|---|---|
| `projmem orphans` | Which **structural** symbols are defined but never referenced? (default filter keeps out comment-word noise) |
| `projmem orphans --exported-only --kind function,method` | Narrow further — exported functions and methods only. |
| `projmem orphans --all-kinds` | Disable the structural filter (legacy noisy behavior). |
| `projmem parity` | Referenced-but-undefined (typos/missing imports) + defined-but-unreferenced. |

Both emit a `lower_bound_warning` when any file in the index was parsed by
the regex backend — refs under regex are a lower bound, so "orphan" may
actually be referenced somewhere the parser couldn't see. Install
`.[treesitter]` to eliminate. Output also includes a `parser_distribution`
count so you can tell at a glance.

Both remain heuristic — public API, dynamic dispatch (`new X()` across
`require` boundaries can miss), and string-keyed references will produce
false positives. Labelled as such.

## Event / listener pair detection

`projmem events` catalogs every (event-name, emitters, listeners) triple in
the repo. JS/TS idioms captured:

- `emitter.on("NAME", handler)` / `.once` / `.addListener` / `.prependListener`
- DOM `target.addEventListener("NAME", handler)`
- EventEmitter `emit("NAME", ...)`
- DOM `target.dispatchEvent(new Event("NAME"))` / `new CustomEvent(...)`

Output flags two interesting cases:

- **`emit_only: true`** — event emitted but nothing listens. Frequently a bug (dropped handler).
- **`listen_only: true`** — listener with no matching emitter in this repo. Usually a CDP event, a platform lifecycle event, or an external library — not always a bug, but worth checking.

Example — finding temporal-coupling race (the scanner.js `clickLink` shape):

```bash
# Step 1 — is there a listener for this CDP event?
projmem events --name Page.loadEventFired
# { "emitters": [], "listeners": [{"file": "controller/interaction.js", "line": 183}] }
# → listen_only:true, as expected for a CDP event.

# Step 2 — does the file that registers the listener also call navigate()
# BEFORE the registration line?
projmem callgraph controller/interaction.js --filter-to navigate
# → compare line numbers.
```

## Reachability (lightweight)

`projmem reach <symbol>` (Python only for now) lists every call site where
the symbol appears inside an `if`-guarded block, with the raw condition text.
This is the down-payment on H2 (full reachability/guard edges). Useful for
"under what condition does X fire?" questions — especially when the condition
is suspicious (default that evaluates to always-false, stale flag).

```bash
projmem reach _runHashSinkProbe
# {
#   "reachable_from": [
#     {"file": "scanner.js",  "line": 742,
#      "context": "params.length == 0 || (_effectiveHash and not _hasHashSourceParam)"},
#     ...
#   ]
# }
```

Limitations clearly stated in the output: not full reachability (no
cross-function, no early-return guards, no elif splitting), condition text is
raw source (not normalized), Python only. If it says "reachable_from: []",
the symbol may still be called — just not under an `if`.

## AI workflow

Before editing code in an AI-assisted session:

1. `projmem refresh --reindex` — make sure the index matches disk.
2. `projmem pack <file-or-symbol> --markdown --write` — produce a bounded pack.
3. Read the **reverse dependencies** and **semantic contracts** sections first.
4. Inspect `unknowns`. If an unknown is material, widen the pack (`--radius 2`) or declare contracts.
5. Edit; run tests.
6. Re-run `projmem refresh --reindex` and regenerate the pack if structure changed.

For the AI agent: pass `pack.json` (machine-readable) into the context. Treat any item with `confidence != "high"` as a hypothesis to verify, not a fact.

## Freshness

Indexing is hash-based. A file is "fresh" if its SHA-1 matches what's stored. `refresh` compares hashes and marks mismatches stale. There is no file watcher.

## Git (optional)

`projmem git <path>` surfaces the last N commits touching a path. Git-derived signals never rank context pack contents in the MVP.
