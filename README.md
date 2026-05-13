<div align="center">

# `projmem`

### drift-aware code memory for AI agents

> grep tells you what's in the code.
> projmem tells you whether what you (or your agent) **believed** about the code is still true — and refuses tool calls that would violate it.

[![license: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20NC-blue.svg)](./LICENSE)
[![tests: 752 passing](https://img.shields.io/badge/tests-752%20passing-brightgreen.svg)](./tests)
[![python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](#install)
[![bench: 38% tokens saved](https://img.shields.io/badge/bench-%E2%88%9238%25%20tokens-orange.svg)](./bench/multisession/REAL_RESULTS.md)

</div>

---

## Start every prompt with `pj:`

That's the entire conversational protocol. Begin a message to Claude / Codex / Gemini / Cursor / Continue with `pj:` and the agent's first tool call **must** be `projmem context` or `projmem editing`, not Edit / Read / Bash. Warnings (`OUT OF SCOPE`, `CRITICAL`) halt the agent at the planning stage so the user sees them before any code moves.

```
You:    pj: delete tests/fixtures/sample_project/cli/main.py
Agent:  → runs projmem context first
        → sees CRITICAL: "never delete this file — load-bearing reducer"
        → halts and asks
```

The `pj:` rule is baked into every `projmem init <agent>` template — once you've run it, the agent picks it up on every new session, no reminder needed.

---

## Why this exists

Every AI coding tool today has the same failure mode:

1. The agent investigates code, builds beliefs, ships an answer.
2. The code drifts — a teammate renames a function, a refactor moves a class, you delete a file.
3. The agent's saved memory still says the **old** thing.
4. Next session, the agent reads the stale belief, treats it as ground truth, and ships a wrong answer on top of a refuted premise.

Worse: even when you write down `do not delete this file` or `this directory is out of scope`, the agent ignores your CLAUDE.md / AGENTS.md and `rm -rf`'s the file anyway. Prompts don't enforce.

**`projmem` is the missing layer.** It does three things:

| | What you get |
|---|---|
| **Verifier** | Every belief you (or the agent) save is a machine-checkable claim. On every read, projmem re-validates it against the live index. Stale claims flip to `contradicted`. |
| **Lifelines** | Files keep one stable id across renames/moves/deletes, so your notes don't fall off. Deleted files become ghost lifelines whose history you can still query. |
| **Enforcement** | A Claude Code `PreToolUse` hook actually **refuses** tool calls (Edit / Write / `rm` / `cat` / `projmem symbol --file …`) against paths the user marked critical or out-of-scope. Hallucination can't bypass it. |

---

## The loop, visualized

```mermaid
flowchart LR
    subgraph S1["Session 1"]
        A1[Agent investigates] --> A2["projmem note add a.py<br/>'setupmethod at a.py:42'"]
        A2 -->|auto-extract| C1["FACT claim"]
        C1 -->|verifier| ST1[("staleness: fresh")]
    end
    subgraph DRIFT["Between sessions"]
        D1[Teammate renames<br/>setupmethod → _setup]
    end
    subgraph S2["Session 2 — fresh process"]
        ST1 -.->|persisted| RV[projmem refresh]
        RV --> ST2[("staleness: contradicted")]
        ST2 --> SIG["repo_memory.contradicted_count: 1"]
        SIG --> AGENT[Agent reads STOP signal]
    end
    style ST1 fill:#9f9,stroke:#0a0
    style ST2 fill:#f99,stroke:#a00
    style SIG fill:#fa0,stroke:#a40,color:#000
```

---

## Install

```bash
git clone https://github.com/m4ll0k/projmem.git
cd projmem
pip install -e '.[treesitter,daemon]'
```

| Extra | What it adds | When you need it |
|---|---|---|
| `[treesitter]` | Multi-language AST indexing | Always — without it only Python is AST-grounded |
| `[daemon]`     | FastAPI daemon + live UI | If you want `projmem ui` |
| `[mcp]`        | MCP server (`projmem mcp-server`) | Cursor / Claude Desktop / Continue |

Verify:

```bash
python3 -m pytest -q          # 752 tests should pass
projmem --help
```

---

## Quick start — choose your agent

projmem ships instruction files for every major coding assistant. Pick the flag for yours:

```bash
cd path/to/your/repo

# Fresh project ─────────────────────────────────────────────
projmem init claude        # or: codex / gemini / cursor / copilot / aider / opencode / all / auto
projmem index
projmem ui --port 7777     # opens the live UI in your browser

# Existing project ──────────────────────────────────────────
projmem init claude --reindex
projmem ui --port 7777

# Switching / multi-agent ───────────────────────────────────
projmem init all --force   # drops CLAUDE.md + AGENTS.md + GEMINI.md + .cursorrules + …
```

| `init` flag | drops | for |
|---|---|---|
| `claude` | `CLAUDE.md` | Claude Code · Anthropic API · MCP |
| `codex` | `AGENTS.md` | OpenAI Codex CLI |
| `gemini` | `GEMINI.md` | Gemini CLI · Code Assist |
| `cursor` | `.cursorrules` | Cursor editor |
| `copilot` | `.github/copilot-instructions.md` | GitHub Copilot |
| `aider` | `AGENTS.md` | aider · droid · trae · hermes · openclaw |
| `opencode` | `opencode.md` | OpenCode |
| `antigravity` | `antigravity.md` | Antigravity |
| `kiro` | `.kiro/steering/` | Kiro |
| `all` | every file above | multi-agent setup |
| `auto` | picks from env vars | default if you omit the flag |

Then tell your agent something like: `pj: add a /healthz endpoint`. The `pj:` prefix is the conversational protocol baked into every instruction file — the agent's first tool call must be `projmem context` or `projmem editing`, not Edit/Read/Bash.

---

## Screenshots

<div align="center">

<img src="./assets/screenshots/ui-overview.png" width="900" alt="projmem UI — activity feed, graph, inspector" />

*The live UI: activity feed on the left, tree/graph/schema in the center, inspector on the right.*

<br/><br/>

<table>
<tr>
<td width="50%"><img src="./assets/screenshots/graph.png" alt="force-directed graph" /><br/><em>Force-directed graph with directory clustering</em></td>
<td width="50%"><img src="./assets/screenshots/inspector-code.png" alt="line-annotation menu" /><br/><em>Code tab — click a gutter line number to add note · guidance · critical</em></td>
</tr>
<tr>
<td><img src="./assets/screenshots/inspector-notes.png" alt="notes inspector with line badges" /><br/><em>Notes tab with line-scoped badges and staleness indicators</em></td>
<td><img src="./assets/screenshots/help-modal.png" alt="orientation popup" /><br/><em>First-run help popup explains every section</em></td>
</tr>
</table>

</div>

---

## How it works

```mermaid
flowchart TB
    subgraph CODE["Your codebase (any language)"]
        SRC[("src/**<br/>tests/**")]
    end
    subgraph PM["projmem · single-binary CLI"]
        IDX[Indexer<br/>tree-sitter + regex fallback]
        STORE[(SQLite<br/>.projmem/index.db)]
        VERIFIER[Verifier<br/>FACT claims · staleness]
        MUT[Mutation verbs<br/>editing · creating · moving · deleting]
        LIFE[Lifelines<br/>stable file id across renames]
        SWEEP[Filesystem sweeper<br/>auto-reindex every 5s]
        IDX --> STORE
        SWEEP --> IDX
        STORE <--> VERIFIER
        STORE <--> MUT
        STORE <--> LIFE
    end
    subgraph SURFACES["Surfaces"]
        UI[Live UI<br/>graph · tree · schema · code]
        HOOK[Claude Code<br/>PreToolUse hook<br/>BLOCKS denials]
        AGENT[Any AI agent<br/>CLAUDE.md · AGENTS.md · GEMINI.md]
        MCP[MCP server]
        CI[CI · pre-commit]
    end
    SRC --> IDX
    PM <--> UI
    PM <--> HOOK
    PM <--> AGENT
    PM <--> MCP
    PM <--> CI
    HOOK -->|deny on critical / exclude| AGENT
    style STORE fill:#fdf,stroke:#a0a
    style VERIFIER fill:#fa0,stroke:#a40,color:#000
    style HOOK fill:#f99,stroke:#a00,color:#000
```

- **Local-first**: SQLite + tree-sitter. No cloud, no LLM in the verifier hot path.
- **Polyglot**: Python (stdlib AST), JS/TS (tree-sitter), Go / Rust / Java / C / C++ / Ruby / Kotlin / Swift / PHP / Scala via tree-sitter; everything else via regex fallback.
- **Concurrent-safe**: SQLite WAL + busy_timeout; the daemon serves the UI while CLI commands write.

---

## Schema

The five tables that carry the v2 semantics:

| Table | Purpose |
|---|---|
| `files` | Every indexed path with its hash, language, mtime, and current `lifeline_id`. |
| `file_lifeline` | One row per logical file across its whole life — created at, current path (NULL when tombstoned). |
| `file_event` | Append-only history per lifeline: `created · leased · edited · released · abandoned · moved · deleted`, plus the reason from each mutation verb. |
| `edit_lease` | Open and historical leases. State machine: `open → pending_approval → open → done | abandoned`. |
| `annotations` | Every note, guidance, constraint, preference, critical, exclude, skill — including `cited_line`, `severity`, `category`, `blast_radius_hops`, and `staleness` from the verifier. |

Schema migrations live in `projmem/migrations/m00*.py` and run on first connect.

---

## The seven verbs that matter (v1)

projmem ships 60+ subcommands, but real benchmark data showed agents only ever use seven:

| Verb | Calls (36 runs) | What it does |
|---|---:|---|
| `projmem note add <target> "<prose>"` | 47× | Save a finding. Auto-extracts FACT claims from prose. |
| `projmem notes` | 43× | Project-wide summary; surfaces `contradicted_count`. |
| `projmem session <target>` | 12× | Per-target bootstrap: notes + neighbors + freshness. |
| `projmem conclude "<text>"` | 10× | One-line conclusion; parses inline `@predicate(...)`. |
| `projmem fact-check "<draft>"` | 7× | Verify claims in your draft before shipping. |
| `projmem task` | 2× | Session continuity (start / step / blocked / resume / close). |
| `projmem refresh` | 2× | Incremental reindex. |

---

## The five mutation verbs (v2 — what the hook enforces)

```bash
projmem editing  <path> --reason "..."         # before Edit / Write / Read
projmem creating <path> --reason "..."         # before creating a new file
projmem moving   <old> <new> --reason "..."    # before rename / move
projmem deleting <path> --reason "..."         # before delete
projmem done     <lease_id>                    # after successful change
```

Every call appends to `file_event`. Reasons must be ≥ 20 chars and contain a verb + object.

---

## Enforcement — three stacked layers

Prompts don't enforce, so projmem doesn't rely on them.

**Layer 1 — the `pj:` convention** (in every instruction template): when the user begins a message with `pj:`, the agent's **first tool call must be `projmem context` or `projmem editing`**, not Edit / Read / Bash. Warnings (OUT OF SCOPE, CRITICAL) halt the agent at the planning stage.

**Layer 2 — Claude Code PreToolUse hook** (install once):

```bash
projmem hook install --claude-code
```

The hook intercepts every Edit / Write / Read / Bash tool call. On `rm`/`mv` against a critical or excluded path, or `cat`/`grep`/`projmem symbol --file …` against an excluded path, it returns `permissionDecision: "deny"` with the user's reason surfaced verbatim. The agent can't bypass it.

**Layer 3 — CLI advisory** (for agents without hooks — Codex / Gemini / plain API): `projmem symbol`, `projmem at`, `projmem pack`, `projmem reverse` all emit `exclusion_warnings[]` in their JSON output when the target is under an exclusion.

---

## Live UI

```bash
projmem ui --port 7777
```

Three panes:

- **Activity feed** (left) — every file event in real time. Click any path to hop.
- **Tree · Graph · Schema** (center) — three lenses on the same index, cross-view selection sync.
- **Inspector** (right) — notes, guidance, critical, code with line-precise annotation menu (click a gutter line number to add note/guidance/critical scoped to that line).

The daemon auto-syncs disk every 5s — files written by `projmem init`, your agent's write_file tool, or anything else outside `projmem creating` are indexed and broadcast as live events. Disable with `--no-watch`.

UI is also where you approve pending-approval leases (a critical rule blocked an edit; click Approve to unblock).

---

## Real benchmark numbers

57 agent runs with `claude-sonnet-4-6`. Full methodology + raw transcripts in [`bench/multisession/REAL_RESULTS.md`](./bench/multisession/REAL_RESULTS.md).

| | Baseline | **`projmem`** | Free-form `notes.md` |
|---|---:|---:|---:|
| Correctness (session-2) | 0/7 (truthful NO_RECORD) | **7/7** | 7/7 |
| Mean tokens / session-2 | 1330 | **1051** | 1688 |
| **Token cost vs `notes.md`** | — | **−38%** | baseline |
| Variance | wide | **tight (1039–1065)** | wide (1548–1894) |

projmem ties scratchpad on correctness but uses **38% fewer tokens** — the agent reads pre-computed `staleness: contradicted` once, instead of re-investigating manually.

---

## Limits — known and intentional

- **Local only.** No cloud sync. Two operators on the same repo work from separate `.projmem/` dirs unless they commit them (and the indexer is deterministic enough that committing the DB is sometimes useful).
- **Indexer is best-effort on regex fallback languages.** AST-grounded for Python, JS/TS, Go, Rust, Java, C/C++; everything else is regex. Surfaces this honestly in the `coverage` field.
- **Hook enforcement is Claude-Code-specific.** Codex / Gemini / plain API agents only get the advisory CLI warnings + the `pj:` convention. The `permissionDecision: "deny"` mechanism only exists in Claude Code's hook protocol.
- **Daemon listens on `127.0.0.1` only.** Refuses non-loopback binds. No remote access; tunnel through SSH if you want it.
- **Filesystem sweeper runs the indexer on a 5s timer.** Big repos (>50k files) may want `--watch-interval 30` to dial it down.
- **No conflict resolution if two operators edit annotations at once.** Last-write-wins on the SQLite row.
- **No PyPI package yet.** Editable install from clone (`pip install -e .`) is the only path today; PyPI publish is planned.

---

## Comparison vs alternatives

| | scratchpad / `notes.md` | Cursor / Continue memory | LSP servers | **`projmem`** |
|---|:---:|:---:|:---:|:---:|
| Survives across sessions | ✓ | ✓ | — | ✓ |
| Verifies beliefs against code | ✗ | ✗ | partial (live only) | **✓** |
| Drift detection (REFUTED signal) | ✗ | ✗ | — | **✓** |
| Refuses tool calls on guarded paths | ✗ | ✗ | — | **✓** |
| Local, single-binary CLI | ✓ | ✗ | — | ✓ |
| Works with any LLM agent | ✓ | per-IDE | per-IDE | ✓ |
| MCP server included | — | — | — | ✓ |
| CI / pre-commit gate | — | — | — | ✓ |
| Token cost on multi-session work | baseline | n/a | n/a | **−38%** |

---

## Status

- ✓ **752 tests passing**
- ✓ Live UI (`projmem ui`) with graph · tree · schema · code · activity feed
- ✓ Claude Code PreToolUse hook with hard-deny on critical / exclude
- ✓ Filesystem auto-sweeper (default on)
- ✓ Lifelines + tombstones across renames/deletes
- ✓ MCP server for Cursor / Claude Desktop / Continue
- ✓ Two real-LLM benchmarks (controlled + in-the-wild)
- 🚧 PyPI publish — planned
- 🚧 LSP shim (editor diagnostics) — planned

---

## Documentation

| File | What's in it |
|---|---|
| [`USAGE.md`](./USAGE.md) | Full command reference, every flag, every workflow |
| [`docs/DESIGN.md`](./docs/DESIGN.md) | MVP design note — what we built and why |
| [`docs/v2-design.md`](./docs/v2-design.md) | v2 lifelines, leases, mutation verbs, UI, hooks |
| [`docs/claims.md`](./docs/claims.md) | Verifier deep-dive — predicates, truth classes, staleness |
| [`docs/agent-integration.md`](./docs/agent-integration.md) | Wiring projmem into specific agents |
| [`docs/ECOSYSTEM.md`](./docs/ECOSYSTEM.md) | How projmem differs from SCIP / LSIF / Semgrep / CodeQL |
| [`bench/multisession/REAL_RESULTS.md`](./bench/multisession/REAL_RESULTS.md) | Benchmark methodology + raw numbers |

---

## License

[**PolyForm Noncommercial 1.0.0**](./LICENSE) — free for personal, research, educational, and other noncommercial use. **Commercial use requires a separate license from the author.**

Plain-English: do whatever you want with it for personal projects, research, study, hobby work, or inside a charity / school / public agency. For a commercial product, SaaS, paid consulting, or paid developer tool — talk to me first ([`CITATION.cff`](./CITATION.cff)).

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Issues + PRs welcome.

## Security

Vulnerability reports: see [`SECURITY.md`](./SECURITY.md). Please do **not** open a public issue for security findings.

---

<div align="center">

**If projmem stops one wrong answer from shipping, it's earned its keep.**

⭐ Star the repo if this is the agent-memory layer you wish you had last week.

</div>
