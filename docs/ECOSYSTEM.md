# `projmem` vs existing systems — why it exists

This note grounds `projmem` against the systems that already solve adjacent problems. The point of the MVP is **not** to re-implement any of them, but to fill the remaining gap: **bounded, contract-aware, confidence-labelled context selection for AI-assisted editing**.

## What already exists (and what we deliberately do not rebuild)

| System | Solves | Why we don't duplicate it |
|---|---|---|
| **SCIP** (Sourcegraph) | Language-agnostic symbol + reference indexing protocol. | Mature. An SCIP importer is a natural future adapter — `projmem`'s `symbols`/`refs` tables map cleanly onto SCIP occurrences. |
| **LSIF** (GitLab et al.) | Precomputed defs/refs/docs for code intelligence. | Same story as SCIP — consumer, not re-implementer. |
| **Tree-sitter** | Incremental, multi-language AST parsing. | The right long-term parser substrate. Python AST was chosen for the MVP to stay dependency-light; Tree-sitter is the obvious next step for JS/TS/Go/Rust. |
| **dependency-cruiser** | JS/TS module dependency graphs + rule validation. | File-level dep graphs alone are insufficient for contract drift. `projmem` can ingest a cruiser graph in the future — our `edges` table accepts foreign providers. |
| **Semgrep** | AST-aware pattern matching / lint rules. | Semgrep finds *patterns*; `projmem` tracks *relations* (reverse deps, contract co-occurrence) and packages them for AI consumption. Complementary, not competing. |
| **CodeQL** | Queryable AST + dataflow + control flow DBs. | Heavy to build and maintain; overkill for context selection. `projmem` is explicit: **no dataflow analysis**. |
| **srctx / CodePrism** | Graph-based code representations. | Demonstrate that graph-shaped storage is viable. `projmem`'s graph is narrower (file/symbol/contract/entrypoint) but tuned for **AI context delivery**, not exploration. |
| **Codebase-Memory (research)** | Persistent KG for LLM agents. | Aligned direction; `projmem` is the pragmatic, local-first, deterministic slice of that vision with no LLM in the loop.

## The gap `projmem` actually fills

None of the systems above provide, in one tool:

1. **Bounded context packs for AI editing** — ranked, anchored, size-capped JSON+Markdown packs with **inclusion reasons per item**.
2. **First-class semantic contracts** — CLI flags, env vars, schema fields, and repeated tokens tracked as their own graph entities with roles (`parse`/`declare`/`read`/`write`/`occurrence`), cross-file co-occurrence, and user-declarable rules.
3. **Fail-loud confidence model** — every edge carries `high|medium|low|unknown`; pack `overall_confidence` is the *lower bound*; unknowns (unresolved imports, undefined symbols, stale index, missing entrypoint) are surfaced explicitly.
4. **AI-oriented outputs** — JSON for agents, Markdown for humans, both from the same pack. No GUI, no server, no LLM dependency.
5. **User-declared contracts & pair rules** — `.projmem/config.json` lets humans encode knowledge that static analysis cannot infer (`if_touch: X, inspect: [Y,Z]`).

SCIP/LSIF deliver *navigation*. Semgrep/CodeQL deliver *queries*. Tree-sitter delivers *parsing*. **None of them deliver a bounded, contract-aware context pack with explicit uncertainty labels, ready to hand to an AI agent before it edits.** That is the specific hole the MVP fills.

## How `projmem` could be extended without re-scope

- **Parser adapter layer:** swap the Python AST + regex indexers for a Tree-sitter-based backend per language. Shape of the `symbols`, `refs`, `edges` tables does not change.
- **SCIP/LSIF importer:** ingest `index.scip` or `dump.lsif` into `symbols`/`refs` with `confidence=high`; keep `projmem`'s semantic contract + pack layer untouched.
- **dependency-cruiser / madge import:** treat their JSON output as an alternative `edges type='imports'` source for JS/TS heavy repos.
- **Semgrep rule bridge:** run user Semgrep rules to enrich the `contracts` table (e.g., custom flag-use patterns) — rule matches attach as `contract` rows with the rule ID as evidence.

These are adapters, not rewrites. The core MVP is the gap-filling layer.

## What `projmem` still will not do

- Full points-to / dataflow analysis (CodeQL territory).
- Dynamic JS call-graph resolution (Tree-sitter alone does not solve this either).
- Hotspot / co-change analytics as a primary signal.
- Any LLM-in-the-loop for the core tool. Hooks only.

Honesty about scope is a feature, not a limitation — it is why `overall_confidence` is a lower bound and `unknowns` is mandatory.
