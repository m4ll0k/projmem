# projmem — drift-aware code memory for AI agents

> **grep tells you what's in the code.
> projmem tells you whether what you believed about the code is still true.**

`projmem` is a local, SQLite-backed CLI that stores an AI agent's
conclusions about code as **structured claims**, re-checks each claim
against the current codebase, and surfaces exactly which beliefs
became false after a code change.

The differentiator vs every "agent memory" notes file: a built-in
verifier that compares saved beliefs to current code and flips notes
to `staleness: contradicted` when something the agent claimed was
true is no longer true.

[![license: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20NC-blue.svg)](./LICENSE)

---

## Why projmem exists

Every AI coding tool today has the same failure mode: **the agent
makes confident claims about code, the code drifts, the claims become
silently wrong, and the agent ships the wrong answer because nothing
in its memory layer can detect the lie.**

Concretely, three things break across sessions:

1. **The agent forgets.** Session 2 has no idea what session 1 found.
   It either re-investigates everything (tokens wasted) or fabricates
   a confident-sounding answer (wrong shipped).
2. **Notes go stale invisibly.** Free-form `notes.md` / Cursor /
   Continue / built-in IDE memory all store text. None of them
   re-validate that text against the current code. A note saying
   "`X` is at `foo.ts:42`" stays in memory forever, even after `X`
   moves to `bar.ts`.
3. **There's no blocker signal.** When prior beliefs become wrong,
   no tool surfaces a hard `STOP, this assumption is now false`.
   The agent reads stale text, treats it as ground truth, and
   builds the next decision on top of a refuted premise.

projmem fixes the third one — the load-bearing one. Every saved
claim has a structured shape (`@defined-at(symbol, file:line)`,
`@exported-from(symbol, file)`, `@env-read-at(name, file:line)`,
etc.). On every read, the verifier checks each claim against the
current index. If the symbol moved, the note is flagged `MOVED`. If
it's gone or in a different file, the note is `REFUTED` and the
project's `contradicted_count` increments. CI scripts gate on
`contradicted_count == 0`. Agents reading `projmem notes` see
`contradicted_count > 0` as a STOP signal before they spend tokens
on stale assumptions.

The tool exists because nothing else does this. Indexers (SCIP, LSIF,
ctags) give you symbols; analyzers (Semgrep, CodeQL) give you
patterns; LLM scratchpads give you text. **None give you "the thing I
saved last week is no longer true and here's what changed."** That
sentence is the entire product.

---

## Real benchmark numbers (not vibes)

Two independent test results across **57 agent runs** with
`claude-sonnet-4-6`:

### 1. Controlled benchmark (N=7 reps × 3 arms, ysoserial)

| | Baseline (no memory) | **Projmem** | Free-form `notes.md` |
|---|---:|---:|---:|
| Correctness | 0/7 (truthful NO_RECORD) | **7/7** | 7/7 |
| Mean tokens / session-2 | 1330 | **1051** | 1688 |
| **vs `notes.md`** | — | **−38%** | baseline |

Projmem ties scratchpad on correctness but uses **38% fewer tokens**
because the agent reads pre-computed `staleness: contradicted` from
`projmem notes` instead of re-investigating each finding.

### 2. In-the-wild benchmark (Codex builds, Claude audits)

A different LLM (Codex) built a fresh FastAPI/SQLAlchemy app
(`tasktrak`, ~1100 LOC) with no chance of training-data leakage to
the auditor. Then Claude Sonnet audited it across two sessions —
identifying 5 specific symbols, then re-verifying after the operator
renamed two functions, deleted one file, and moved a third symbol.

| | Baseline (no memory) | **Projmem** |
|---|---:|---:|
| Session-2 verdicts correct | 0/5 (truthful NO_RECORD) | **5/5** |
| Total cost across 2 sessions | $0.08 | $0.16 |
| **Verifiable answers / dollar** | 0 | **30** |

The decisive transcript (projmem session 2):

> *"The five session-1 findings (notes 6-10) map directly. Based on staleness:*
> *— Note 6: `complete_task` @ task_service.py:42 — **contradicted***
> *— Note 7: `verify_password` @ auth_service.py:16 — **contradicted***
> *— Note 8: `Task` @ task.py:17 — fresh*
> *— Note 9: `TaskStatus` @ task.py:10 — fresh*
> *— Note 10: `legacy_list_tasks` @ legacy.py:17 — **contradicted**"*

The agent ran `projmem notes` once, READ the per-note `staleness`,
and answered. No file investigation needed. This is what the verifier
buys.

Full benchmark methodology + harness in `bench/multisession/`.

---

## 5-minute demo (with auto-extract — write prose, not JSON)

```bash
pip install -e '.[treesitter]'
cd /path/to/your/repo
projmem init claude    # one-shot: index + drop CLAUDE.md/AGENTS.md
```

Save a finding as PROSE. projmem auto-extracts a structured FACT
claim — no `--claims` JSON, no `@predicate(...)` syntax to learn:

```bash
projmem note add src/compiler/sys.ts --kind note \
  "\`TSC_WATCHFILE\` is defined at src/compiler/sys.ts:1516"
```

projmem's response includes the auto-extracted claim:

```json
{
  "id": 1,
  "auto_extracted_claims": [{
    "subject": "TSC_WATCHFILE",
    "predicate": "defined-at",
    "object": "src/compiler/sys.ts:1516",
    "truth_class": "FACT",
    "status": "VERIFIED"
  }],
  "staleness": "fresh"
}
```

Now break the claim — rename the symbol:

```bash
sed -i '' 's/TSC_WATCHFILE/TSC_WATCH_FILE/' src/compiler/sys.ts
projmem refresh    # auto-reindexes by default
projmem notes
```

projmem now reports `contradicted_count: 1` and the saved note has
`staleness: contradicted` — your agent (or you, tomorrow) sees that
the prior belief is REFUTED by current code, with the new evidence
attached.

That signal is the entire product.

---

## The seven verbs that actually matter

`projmem` ships with a long catalog (`projmem usage` for the full
list), but real benchmark data showed an LLM agent only ever uses
seven of them. They are:

| Verb | Use it for |
|---|---|
| `projmem note add <target> "<prose>"` | save a finding (auto-extracts FACT claims from prose) |
| `projmem notes` | project-wide summary, surfaces `contradicted_count` blocker |
| `projmem session <target>` | per-target bootstrap (notes + neighbors + freshness) |
| `projmem conclude "<text>"` | save a one-line conclusion, parses inline `@predicate(...)` |
| `projmem fact-check "<draft>"` | verify claims in your draft text BEFORE shipping (exit 2 on REFUTED) |
| `projmem task` | session-continuity (start / step / blocked / resume / close) |
| `projmem refresh` | incremental reindex after edits (auto-applies) |

Run bare `projmem` to see this list at any time.

---

## How the verifier engages

1. **Write prose.** ``setupmethod` is defined at src/foo.py:42` →
   projmem auto-extracts to `@defined-at(setupmethod, src/foo.py:42)`
   with `truth_class: FACT`.
2. **Code drifts.** A teammate renames `setupmethod`. Next
   `projmem refresh` (or background `projmem watch`) reindexes the
   touched files.
3. **Verifier re-runs.** The note's structured claim no longer matches
   the index. Note staleness flips: `fresh` → `contradicted`.
4. **Agent / CI sees the blocker.** Every projmem read returns a
   `repo_memory.contradicted_count` field; `> 0` means a saved FACT
   was REFUTED. Use as a CI gate or a session-start blocker.

Four claim verdicts: `VERIFIED`, `MOVED` (same file, new line),
`REFUTED` (gone or in a different file), `UNCHECKABLE` (predicate
not in the catalog — see `projmem predicates`).

---

## Install

```bash
git clone <repo-url> projmem
cd projmem
pip install -e '.[treesitter]'    # treesitter extra strongly recommended
```

Verify:

```bash
python3 -m pytest tests/ -q       # 679 tests should pass
projmem --help
```

Optional: drop the agent-instruction file into your codebase so any
LLM agent (Claude Code, Codex, Cursor, Continue) picks up the
workflow automatically:

```bash
cd /path/to/your/repo
projmem init claude               # writes CLAUDE.md
projmem init codex                # writes AGENTS.md
projmem init auto                 # detects the env, writes both if needed
```

---

## Status

- 679 tests passing
- Two real-LLM benchmarks (controlled + in-the-wild) — see above
- MCP server included (`projmem mcp-server`) for Cursor / Claude
  Desktop / Continue
- Trace-replay primitive (`bench/multisession/regrade.py`) so old
  benchmark runs can be re-graded with new judges for $0
- 60+ commands available beyond the seven core ones; see
  `projmem usage --json` for the full catalog

---

## License

[PolyForm Noncommercial 1.0.0](./LICENSE) — free for personal,
research, educational, and other noncommercial use. **Commercial use
requires a separate license from the author.** Open an issue or
contact via the address in `CITATION.cff` for commercial inquiries.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Issues + PRs welcome.

## Citation

If you use projmem in research, see [CITATION.cff](./CITATION.cff).
