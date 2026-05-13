# Usage

Full command reference and workflows for `projmem`. For the elevator pitch, install instructions, and architecture overview, see [README.md](./README.md).

---

## Table of contents

1. [Install](#install)
2. [Initialize for your agent](#initialize-for-your-agent)
3. [Indexing](#indexing)
4. [The five v2 mutation verbs](#the-five-v2-mutation-verbs)
5. [Annotations — note · guidance · critical · exclude](#annotations)
6. [The verifier — claims and staleness](#the-verifier)
7. [The live UI](#the-live-ui)
8. [Claude Code hooks (enforcement)](#claude-code-hooks-enforcement)
9. [The `pj:` convention](#the-pj-convention)
10. [MCP server](#mcp-server)
11. [Git hook · CI gate](#git-hook--ci-gate)
12. [Configuration](#configuration)
13. [Diagnostic commands](#diagnostic-commands)

---

## Install

```bash
git clone https://github.com/m4ll0k/projmem.git
cd projmem
pip install -e '.[treesitter,daemon]'
```

Extras:

| Extra | Adds | When |
|---|---|---|
| `[treesitter]` | Multi-language AST indexing | Always |
| `[daemon]` | FastAPI daemon + live UI | If you want `projmem ui` |
| `[mcp]` | MCP server | For Cursor / Claude Desktop / Continue |
| `[yaml]` | YAML config support | If `.projmem/config.yaml` |
| `[test]` | pytest + dev deps | Contributors |

Verify:

```bash
projmem --help
python3 -m pytest -q     # 752 tests should pass
```

---

## Initialize for your agent

```bash
projmem init <agent>
```

`<agent>` controls which instruction file lands on disk:

| Flag | File dropped | Targets |
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
| `auto` | env-var detection | default if you omit |

Flags:

- `--force` — overwrite existing instruction files.
- `--reindex` — rebuild the index even if one exists.
- `--no-index` — drop templates only, skip indexing.

```bash
# Fresh project
projmem init claude

# Existing project, refresh index
projmem init claude --reindex

# Drop every instruction file for a multi-agent setup
projmem init all --force
```

---

## Indexing

```bash
projmem index                    # idempotent — skips unchanged files by hash
projmem index --force            # full reindex
projmem index --exclude 'vendor/**' --include 'src/**'
projmem refresh                  # detect drifted files and auto-reindex them
projmem refresh --reindex        # rebuild ONLY the drifted files
projmem stats                    # row counts per table
projmem status                   # counts + list of stale files
projmem doctor                   # health check; exit 1 on any HIGH finding
projmem files                    # emit indexed file list + effective scope
projmem scope                    # the exact scope of the last `index` run
```

`projmem index` walks the project root, hashes every file, and indexes anything new/changed. Skip dirs (`.git`, `node_modules`, `.venv`, `__pycache__`, …) are built-in; merge with `.projmem/config.json::exclude_globs`/`include_globs`.

---

## The five v2 mutation verbs

Every agent edit goes through one of these. They append to `file_event`, open/close leases, and trigger the verifier.

```bash
projmem editing  <path> --reason "..."         # before Edit / Write / Read
projmem creating <path> --reason "..."         # before creating a new file
projmem moving   <old> <new> --reason "..."    # before rename / move
projmem deleting <path> --reason "..."         # before delete
projmem done     <lease_id>                    # close on success
projmem abandoned <lease_id> --reason "..."    # close on abandon
projmem sweep-leases                            # close expired leases (cron-safe)
```

**Reasons** must be ≥ 20 chars and contain a verb + object. `"cleanup"` is rejected; `"consolidating with shared/validators.ts"` is accepted. The reason is recorded forever in `file_event`.

**Editing-lease response** carries (scope-ordered, most-relevant first):

```json
{
  "lease_id": "…",
  "expires_at": 123,
  "path": "src/foo.py",
  "lifeline_id": "…",
  "lease_state": "open",          // or "pending_approval"
  "warnings": [
    "📍 line-scoped notes on this file at L5, L17 — read them before touching those lines.",
    "🚫 OUT OF SCOPE — this path is under an exclusion at …"
  ],
  "guidance": [
    {"id": 45, "kind": "note", "scope": "line", "cited_line": 5, "body": "…", "staleness": "fresh"},
    {"id": 12, "kind": "guidance", "scope": "file", "severity": "warn", "body": "…"},
    …
  ],
  "notes_by_line": {"5": [...], "17": [...]},
  "critical_prelude": "⚠ CRITICAL CONTEXT — never log session tokens …",
  "exclusions": [...],
  "history": {"lifeline_id": "…", "events": [...]}
}
```

If `warnings[]` contains `CRITICAL`, the lease enters `pending_approval`. Approve via the UI or:

```bash
# In another terminal
curl -X POST http://127.0.0.1:7777/control/approve/<lease_id>
```

---

## Annotations

Five kinds, one storage table. Pick the right kind:

| Kind | When |
|---|---|
| `note` | Free-text observation. "This is fragile." "See ticket #123." Auto-extracts FACT claims from prose. |
| `guidance` | Rule the agent should follow. Has `severity ∈ {info, warn, critical}`. "Prefer functional style here." |
| `constraint` / `preference` | Sub-kinds of guidance with different defaults. |
| `critical` | Load-bearing rule that **blocks edits** until reviewer approves. ≥ 40-char reason, category, blast-radius hops. |
| `exclude` | Mark a path (file or dir) as out-of-scope for the agent. Hook denies tool calls on the path; CLI commands warn. |
| `skill` | Path-scoped cognitive instruction (v2.1) — "when editing tests/**, write the failing test first." |

### Adding annotations

```bash
# Free-text note
projmem note add src/auth.py "verify_token is defined at src/auth.py:42"

# Guidance
projmem note add src/auth/ --kind guidance --severity warn \
  "prefer functional style; no shared mutable state"

# Critical (≥40-char reason, blocks edits, requires cosigner)
projmem critical add src/core.py \
  --reason "session tokens must never be logged — legal flagged for compliance" \
  --category security \
  --self-cosign \
  --blast-radius-hops 1

# Exclude a subtree
projmem note add tests/fixtures/multilang/go/util/ --kind exclude \
  "go test fixture — no real-codebase value, do not read"
```

Or do all of this from the **live UI** (`projmem ui --port 7777`) by clicking a file/dir → inspector → Add note / Add guidance / Add critical / Mark out of scope. The code tab supports **line-precise annotations**: click any gutter line number for a small menu.

### Reading annotations

```bash
projmem notes                          # project summary, surfaces contradicted_count
projmem note list <target>             # annotations on a file/dir/@project
projmem note search "regex"            # full-text search
projmem context <path>                 # read-only briefing for a path
projmem at src/foo.py:42               # context for a specific line
projmem audit <target>                 # claim-level verdicts across notes
projmem integrity <target>             # per-target integrity score
projmem critical list                  # every critical note in the repo
projmem exclusions                     # every active exclusion
```

### Editing / deleting

In the UI: pencil icon on each annotation row to edit; trash icon (with two-stage confirm) to delete. From the CLI:

```bash
projmem note delete <annotation_id>
projmem critical remove <annotation_id>
```

---

## The verifier

Every note carries an auto-extracted **claim**. On every read, the verifier re-checks claims against the live index.

### Four verdicts

| Verdict | Meaning | Exit code |
|---|---|---:|
| **VERIFIED** | Claim matches indexed code at the cited location | 0 |
| **MOVED** | Symbol in cited file but at a different line | 0 |
| **REFUTED** | Symbol absent or in a different file. **Note flips to `staleness: contradicted`** | 2 |
| **UNCHECKABLE** | Predicate isn't in the catalog | 0 |

### Six supported predicates

```
defined-at(symbol, file:line)
exported-from(symbol, file)
env-read-at(VAR, file:line)
flag-read-at(--flag, file:line)
reexported-via(symbol, file)
reverse-dependency-of(file, of_file)
```

`projmem predicates` for the live list. Predicates are extensible — see `docs/claims.md`.

### Workflows

```bash
projmem fact-check "TSC_WATCHFILE is at src/compiler/sys.ts:1516"
projmem check "draft text"                    # one-shot wrapper, exit 2 on REFUTED
projmem check-line src/foo.ts:42 myFn         # construct + verify in one call
projmem note-verify <target>                  # revalidate a note's claims
projmem fact-check < draft.txt                # stdin
```

Five `staleness` values: `fresh · weakly_stale · strongly_stale · contradicted · unknown`. `contradicted_count > 0` halts work everywhere (CI, agent prompts, the UI's blocker pill).

---

## The live UI

```bash
projmem ui --port 7777
```

Three panes:

- **Activity feed (left)** — every file event in real time. Click any path to navigate. Implicit edits (agent edited without announcing) get a dashed warn border.
- **Tree · Graph · Schema (center)** — three lenses on the same index. Click any node and the other views sync. Toggle ghosts (tombstoned files) on the graph.
- **Inspector (right)** — notes / guidance / critical / history / code. Code tab has line-precise annotations: click any gutter line number for the add-menu. Dots in the gutter mark lines with existing notes (red=critical, blue=guidance, amber=note).

**Live disk sync** — the daemon runs `index` on a 5s timer by default. Files created by `projmem init`, your agent's write_file, or anything else show up automatically. Tune or disable:

```bash
projmem ui --port 7777 --watch-interval 1.0   # snappier
projmem ui --port 7777 --no-watch              # disable sweeper
```

**Live controls**:

- ⏸ Pause agent — blocks new edit leases until you resume.
- Pending-approval lease — agent hit a critical; click Approve or Deny.
- ↻ sync (inspector header) — manual refetch.
- Esc — clear current selection.

**Help popup**: a `? help` button in the top bar (auto-opens on first visit) explains every section.

---

## Claude Code hooks (enforcement)

Prompts don't enforce. Install the `PreToolUse` hook and the agent **can't** bypass critical/exclude rules — the daemon refuses the tool call at the API boundary.

```bash
projmem hook install --claude-code
projmem hook status
projmem hook uninstall
```

This writes `.claude/hooks/pre-tool-use.py` + `post-tool-use.py`. Add the matcher block from `projmem hook install --claude-code --json` into `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|Read|NotebookEdit|Bash",
        "hooks": [{"type": "command", "command": ".claude/hooks/pre-tool-use.py"}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [{"type": "command", "command": ".claude/hooks/post-tool-use.py"}]
      }
    ]
  }
}
```

### What it actually does

For every tool call the hook:

1. Extracts path arguments — `file_path` from Edit/Write/Read/NotebookEdit, and from Bash:
   - Risky bash commands (`rm`, `rmdir`, `unlink`, `mv`, `shred`, `dd`)
   - Reader bash commands (`cat`, `less`, `more`, `head`, `tail`, `bat`, `view`, `grep`, `rg`, `ag`, `ack`)
   - `projmem <subcommand>` invocations — pulls `--file`, `-f`, and positional path args. Introspection subcommands (`context`, `editing`, `notes`, `graph`, `info`) pass through so the agent can still learn about exclusions.
2. For writes → calls `projmem editing` (opens a lease, returns warnings).
3. For reads → calls `projmem context` (read-only, no lease).
4. If `warnings[]` contains `OUT OF SCOPE` (exclusion) or `CRITICAL` (write-only) → returns `permissionDecision: "deny"` with the user's reason verbatim. Claude Code refuses the tool call.

`shlex` parses bash commands (no shell evaluation) — `rm $(touch /tmp/canary)` survives as inert tokens.

---

## The `pj:` convention

**This is the simplest thing in the whole tool.** Start any prompt to your agent with `pj:` and it switches into projmem-first mode: the first tool call **must** be `projmem context` or `projmem editing`, not Edit / Read / Bash. The agent reads warnings (`OUT OF SCOPE`, `CRITICAL`) and halts at the planning stage if any fire.

### Examples

```
pj: refactor the auth module to use httpOnly cookies
pj: delete tests/fixtures/old_format.json
pj: explain how the reducer in store/cart.ts handles partial updates
pj: rename `verify_token` to `verify_jwt` across the codebase
```

For each one, the agent's first action is `projmem context <relevant_file>` (or `projmem editing` if it intends to modify), gets the notes/guidance/critical/exclusion bundle, then plans.

### What's special about `pj:`

- **It's a plain-text trigger.** No subcommand, no installation, no flag — just two characters and a colon. Works in every chat / CLI / IDE that takes prose.
- **It's baked into every `projmem init <agent>` template** (CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules / …). Once you've run init, the agent reads it at session start and follows it automatically.
- **It complements the hook, doesn't replace it.** The PreToolUse hook is the safety net that fires at tool-call time. `pj:` shifts the same check earlier — to **planning time** — so the user sees the warning before a tool call is even attempted.

### Why two letters

Because friction matters. `pj:` is the shortest unambiguous prefix; `projmem:` works too but takes seven keystrokes you'd skip half the time. We tested `m:` and `p:` against this codebase's existing prompts — too many false matches. `pj:` is short, unique, and survives autocomplete.

The hook is the safety net; `pj:` is the planning shortcut.

---

## MCP server

For Cursor / Claude Desktop / Continue / any MCP-aware client:

```json
{
  "projmem": {
    "command": "projmem",
    "args": ["mcp-server", "--path", "/abs/path/to/your/repo"]
  }
}
```

Tools exposed: `session`, `search`, `symbol`, `reverse`, `forward`, `fact-check`, `notes`, `note_add`.

---

## Git hook · CI gate

```bash
projmem hook install              # writes .git/hooks/pre-commit
```

Runs `projmem refresh && projmem complete` before every commit. Blocks the commit if any saved FACT claim is now REFUTED.

CI:

```yaml
- name: Verify saved beliefs against current code
  run: |
    pip install -e '.[treesitter]' projmem
    projmem index
    projmem complete || exit 2     # exit 2 on any HIGH finding
```

Strict mode: `refuted_count == 0 AND moved_count == 0`. Loose (default): `refuted_count == 0`.

---

## Configuration

`.projmem/config.json` (or `.yaml` with `[yaml]` extra):

```json
{
  "exclude_globs": ["vendor/**", "build/**"],
  "include_globs": ["src/**", "tests/**"],
  "exclude_wins": false,
  "max_file_bytes": 1048576,
  "languages": {
    "python": {"backend": "ast"},
    "typescript": {"backend": "treesitter"}
  }
}
```

CLI globs (`--exclude`, `--include`) merge with config; `--exclude-wins` reverses precedence.

---

## Diagnostic commands

```bash
projmem stats                              # row counts
projmem doctor                             # health check
projmem checklist                          # post-edit completeness gate
projmem coverage                           # how much of the repo is AST vs regex
projmem orphans                            # symbols defined but referenced nowhere
projmem parity                             # referenced-but-undefined + defined-but-unreferenced
projmem entrypoints                        # detected/declared entrypoints
projmem missing-paths                      # referenced files that don't exist
projmem unresolved-imports
projmem contract-drift                     # contract names whose values vary across sites
projmem evidence  <file.jsonl>             # ingest runtime evidence
projmem evidence-query <target>            # runtime evidence on a symbol/file
projmem drift                              # static ↔ runtime drift
```

Every command emits JSON when called with `--json`. The full catalog with flag details is reachable via `projmem usage --json`.

---

## Troubleshooting

**The UI shows an old snapshot.** Restart the daemon (`projmem ui` picks up new schema migrations and code changes). The filesystem sweeper auto-reindexes every 5s once running.

**`projmem editing` returns `pending_approval` on a file with no critical.** Check `projmem critical list` — a critical note with `blast_radius_hops: 1` propagates to importing files. Either approve via the UI or run `projmem critical remove <id>`.

**Hook fires on every `ls`.** It shouldn't — `ls`, `grep` (without an excluded target), `cd`, and anything not in `RISKY_BASH_CMDS` / `READER_BASH_CMDS` / `projmem <non-introspection>` is a no-op. If you see otherwise, dump the hook payload: add `print(json.dumps(payload), file=sys.stderr)` to `.claude/hooks/pre-tool-use.py`.

**Filesystem sweeper burning CPU on a huge repo.** `--watch-interval 30` dials it down. Or `--no-watch` if you'd rather drive everything through explicit `projmem creating/editing`.

**`projmem symbol --file <excluded>` still works for my Codex/Gemini agent.** That's expected — they don't run the Claude Code hook. Look for `exclusion_warnings[]` in the JSON output; the agent's instruction file (AGENTS.md / GEMINI.md) tells it to honor that.

---

## See also

- [README.md](./README.md) — pitch + install
- [docs/DESIGN.md](./docs/DESIGN.md) — MVP design note
- [docs/v2-design.md](./docs/v2-design.md) — v2 lifelines, leases, mutation verbs, UI, hooks
- [docs/claims.md](./docs/claims.md) — verifier deep-dive
- [docs/agent-integration.md](./docs/agent-integration.md) — wiring into specific agents
- [bench/multisession/REAL_RESULTS.md](./bench/multisession/REAL_RESULTS.md) — benchmarks
