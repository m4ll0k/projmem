# `projmem` — MVP design note

## Problem

AI-assisted editing on real codebases fails in three repeatable ways:

1. The AI (or the developer) reads the wrong files — or too many files.
2. Dependency graphs miss **semantic contracts** (flags, env vars, schema fields, status tokens) whose drift causes silent breakage.
3. Tools present context as if it were complete, when in fact large portions are heuristic, unresolved, or stale.

`projmem` is narrowly scoped to this problem. It is a **context selection and contract-awareness tool**, not a proof engine.

## Why reverse dependencies are the primary structural primitive

Editing a symbol or file is safe only if you know who depends on it. Forward-only graphs tell you what your code *uses*; reverse dependencies tell you what *breaks* if you change. The store schema is built so reverse lookup (`edges WHERE dst=?`) is the first-class operation.

## Why semantic contracts are first-class

Pure import graphs miss contract drift: a CLI flag renamed in one place but read by name in another; a schema field written in module A, read in module B via a string key; an env var fetched in two places with different defaults. The indexer records a separate `contracts` table with `kind ∈ {flag, env, schema_field, token}` and a `role ∈ {parse, declare, read, write, use, occurrence}`. Users can also **declare** contracts in `.projmem/config.json` with high confidence.

## Confidence model

Every node/edge carries one of `high | medium | low | unknown`. Rules:
- `high` — Python AST (definitions, names, imports), argparse/click/commander parse calls, `os.environ`/`process.env` reads, user-declared contracts.
- `medium` — regex-based JS/TS imports/exports, `--flag` occurrences in Python comments/strings, dict-literal schema writes.
- `low` — regex token scans, JS/TS identifier refs, cross-file token heuristics.
- `unknown` — dynamic dispatch, computed imports, unresolved targets.

Pack-level `overall_confidence` is the **lower bound** across all included edges. This is intentional — if anything is heuristic, the pack says so.

## Fail-loud requirements

A pack is incomplete-but-honest before it is complete-looking-but-wrong:
- Unresolved imports (`module:<name>`) are listed in `unknowns`.
- Symbol lookups with no defs return `symbol-undefined`.
- Stale files (hash ≠ on-disk) surface a `stale-index` unknown.
- Dynamic dispatch is not pretended to be resolved; where the indexer cannot see past it (e.g., `globals().get(name)`), no edge is created and the behavior is documented.

## Why Git is constrained

Broad co-change graphs and hotspot scores are noisy, confound structural analysis, and give false confidence. The MVP exposes only `projmem git <path>` for recent-commits inspection, and does not feed Git data into pack ranking by default.

## Phase 0 baseline vs the index

A grep-on-name can answer many questions. The index earns its keep when:
- You need *reverse* imports, which raw grep does not give structurally.
- You need contract co-occurrence (files sharing a `status` key or a flag).
- You need confidence-aware ranked results and bounded packs.

For pure symbol lookups on small codebases, `git grep` remains competitive; `projmem symbol` adds definition-vs-reference disambiguation and confidence labels.

## Storage

SQLite at `.projmem/index.db`. Tables are inspectable with any SQLite client. Pack outputs live under `.projmem/packs/` as JSON (and optional Markdown). A single-file local store is intentional for the MVP.

## Parser backends

Three tiers, selected automatically per file:

1. **Tree-sitter** (when `tree-sitter` + `tree-sitter-language-pack` are installed) — AST-grounded indexing with `high` confidence for 15+ languages: Python, JS/JSX, TS/TSX, Go, Rust, C, C++, Java, Ruby, C#, Kotlin, Swift, PHP, Scala, Bash. Queries live in `projmem/ts_backend.py::QUERIES`; capture-name convention is `sym.<kind>`, `import.path`, `import.module`, `ref.call`. Import resolution is per-language-family (Python dotted/relative, JS/TS relative+extension hunt, C `#include`, Go local package with dir fan-out).
2. **Python stdlib `ast`** — fallback for `.py` if tree-sitter unavailable. Still `high` confidence.
3. **Regex heuristics** — last resort for JS-shaped syntax; `medium`/`low` confidence.

The semantic-contract layer is separate and always runs: language-agnostic flag-token regex plus per-language env-var patterns (`os.environ`/`os.Getenv`/`process.env`/`env::var`/`getenv`/`System.getenv`/`ENV[]`).

## Known limitations

- Tree-sitter queries capture structural definitions and call sites; they do **not** capture re-exports (`export * from`), `import type`, namespace imports, barrel files, decorators as control flow, monorepo path aliases, or `setattr`/dynamic attribute access.
- Schema-field detection treats any sufficiently identifier-like string literal used as a dict key as a candidate. This is noisy by design; use user-declared contracts for the critical fields.
- Token detection is bounded and heuristic; tokens appear with `low` confidence and are capped in packs.
- Test association is heuristic: files under `tests/` that either import the target or match its basename.
- Entrypoint detection is filename/guard-based; always cross-check with user-declared entrypoints.
- Staleness is hash-based only; there is no file watcher.
- No LLM calls. No dynamic runtime tracing; `runtime_evidence.py` is a pluggable JSONL ingester that attaches notes but does **not** infer causality.

## Out of scope for the MVP

Full points-to analysis; hotspot/co-change analytics; multi-language AST completeness; GUI; server; mandatory AI features. These are explicit omissions, not bugs.
