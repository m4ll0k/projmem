# projmem — Constitution for AI agents working in this repo

These are not suggestions. They are conditions under which the AI agent
is allowed to take actions in this codebase. Each rule below either
holds, or the agent stops and reports the violation.

---

## Non-negotiables

### 1. projmem must be available

> **If projmem is unavailable, STOP and report. Do not proceed without it.**

If the `projmem` binary is not on PATH, or the repo has no `.projmem/`
index, that condition holds. The agent's working memory model assumes
projmem is present; without it, beliefs go unverified and the
wrong-answer-on-refuted-premise failure mode reasserts.

### 2. `contradicted_count > 0` halts work

If `projmem notes` or any `repo_memory` block returns
`contradicted_count > 0`, **STOP**. A FACT claim saved earlier is now
refuted. Resolve the contradiction (re-investigate, update the note,
or delete it) before any further code change. Do not write code on
top of a known-wrong premise.

### 3. Announce every edit before you make it

Before touching ANY file, run the matching v2 mutation verb. No
exceptions — the PreToolUse hook enforces this mechanically, but the
agent is responsible for not bypassing it.

```bash
projmem editing  <path> --reason "..."    # before Edit / Write / Read on existing file
projmem creating <path> --reason "..."    # before creating a NEW file
projmem moving   <old> <new> --reason "..."   # before rename / move
projmem deleting <path> --reason "..."    # before delete
projmem done     <lease_id>                # after successful change
projmem abandoned <lease_id> --reason "..."   # after abandoned attempt
```

Reasons must be ≥ 20 chars and contain a verb + object. `"cleanup"` is
rejected; `"consolidating with shared/validators.ts"` is accepted. The
reason is recorded forever in `file_event` — write it so a human
reading the timeline in six months still understands the why.

### 4. ⚠ CRITICAL CONTEXT means HALT and engage

When `projmem editing` returns a `critical_prelude` field starting
with `⚠ CRITICAL CONTEXT`, you MUST do all three:

1. **State the intended change** in plain text before any tool call.
2. **Confirm the change does NOT touch any concern listed in the
   prelude** — name each concern and your reason.
3. **Halt if the change overlaps** any listed concern. Wait for human
   approval (the lease is in `pending_approval` state; the human
   grants it via the projmem UI).

A critical prelude is not advisory. It marks load-bearing code —
files where prior edits caused production incidents. Treat the
prelude with the same weight as a compile error.

### 5. End every task with the gate

```bash
projmem complete || exit 2
```

`projmem complete` exits 1 on any HIGH finding (drifted file,
contradicted note, dangling ref, broken pair rule). The wrapper above
propagates the failure to the calling shell so CI / git hooks / the
human's harness all see a non-zero rc.

---

## The seven core verbs (v1 surface)

These are what the agent uses for actual memory work; every one
returns structured JSON via `--json`.

| Verb | One-liner | Example |
|---|---|---|
| `projmem note add <target> "<prose>"` | Save a finding. Prose like `` `X` is defined at file:line `` auto-extracts a FACT claim. | `projmem note add src/auth.py "\`verify_token\` is defined at src/auth.py:42"` |
| `projmem notes` | Project-wide summary. Surfaces `contradicted_count` — the STOP signal. | `projmem notes --json` |
| `projmem session <target>` | Per-target bootstrap — notes, freshness, neighbors. | `projmem session src/auth.py` |
| `projmem conclude "<text>"` | Save a one-line session conclusion; inline `@predicate(s, o)` becomes structured claims. | `projmem conclude "@defined-at(verify_token, src/auth.py:42)"` |
| `projmem fact-check "<draft>"` | Verify claims in a draft BEFORE shipping. Exit 2 on REFUTED. | `projmem fact-check "the helper is at src/auth.py:42"` |
| `projmem task ...` | Cross-session continuity: start / step / blocked / unblock / close / resume / list. | `projmem task resume` |
| `projmem refresh` | Incremental reindex after edits; auto-applies by default. | `projmem refresh` |

Use `projmem usage` for the full 60+ verb catalog; the seven above
are what real benchmarks showed agents reach for.

---

## The four v2 mutation verbs (this is what the hook enforces)

Same shape — JSON out, structured errors with `--json`.

| Verb | What it does | Returns |
|---|---|---|
| `projmem editing <path> --reason "..."` | Open a lease + bundle guidance + history + ⚠ CRITICAL CONTEXT for the path in one call. | `{lease_id, expires_at, guidance[], history{}, warnings[], lease_state, critical_prelude?}` |
| `projmem creating <path> --reason "..."` | Open a lease for a NEW file. Warns if the path was previously tombstoned. | `{lease_id, lifeline_id, warnings[]}` |
| `projmem moving <old> <new> --reason "..."` | Rename / move preserving lifeline + every attached note. | `{lifeline_id, old_path, new_path}` |
| `projmem deleting <path> --reason "..."` | Tombstone (never genuinely delete). Use `--replaced-by <path>` to point future `creating` calls at the successor. | `{lifeline_id, tombstoned_at, replaced_by}` |
| `projmem done <lease_id>` | Close a lease as successful. Idempotent. | `{closed_kind, duration_s}` |
| `projmem abandoned <lease_id> [--reason ...]` | Close a lease as abandoned (work not completed). | `{closed_kind: abandoned}` |

Read-only inspection without opening a lease: `projmem context <path>
[--include-stale] [--format agent-prelude|human|json]`.

---

## Guidance, constraint, preference, critical — pick the right kind

```bash
projmem note add <target> --kind guidance   --severity warn  "..."   # injected at edit time
projmem note add <target> --kind constraint                  "..."   # environmental fact
projmem note add <target> --kind preference --severity info  "..."   # style choice
projmem critical add <target> --reason "..." --category security \
    --approved-by <user> [--incident-ref INC-…]                      # load-bearing — cosigner required
```

- **guidance** / **constraint** / **preference**: free to author,
  shown to the agent at `editing` time, optional `--severity
  info|warn|critical`, optional `--expires-days N`.
- **critical**: requires ≥ 1 `--approved-by <user>` (or
  `--self-cosign`), reason ≥ 40 chars, picks a category, has a 90-day
  review window, blocks edits by default, propagates 1-hop via
  reverse-dep graph. Friction is intentional — the kind collapses to
  noise the moment everyone marks their pet module as critical.

---

## Session loop

**Start.** Run `projmem task resume` (blocked tasks first, then
active). If empty, `projmem notes` for prior conclusions. If any
`contradicted_count > 0` — stop and report. **Never** write code on
top of contradicted memory.

**Investigate.** `projmem session <target>` for per-target bootstrap.
Add `projmem note add <target> "<prose>"` as you learn things.
Backtick-around-symbol-name plus `file:line` auto-extracts FACT
claims you can verify later.

**Edit.** Every Edit / Write / Read tool call goes through `projmem
editing` (the hook handles this if installed; if not, call it
manually). Pay attention to the response — warnings about
contradicted notes, history showing prior edits, critical preludes
all carry weight.

**Close.** `projmem done <lease_id>` on success, `projmem abandoned`
otherwise. Don't leave leases open — they auto-expire after 5 min,
but that produces noise.

**Ship.** `projmem fact-check "<your draft answer>"` before responding
to the human. `projmem complete || exit 2` before claiming the task
is done.

---

## Blocker signals (halt work, address first)

| Signal | Meaning |
|---|---|
| `contradicted_count > 0` | A saved FACT was REFUTED — re-investigate |
| `has_refuted` from `fact-check` | Your draft contains wrong claims |
| `critical_prelude` from `editing` | Load-bearing code — state intent, confirm scope, halt if overlap |
| `lease_state: pending_approval` | Critical-blocked; wait for human approval |
| `freshness_warning` | File changed on disk since index — `projmem refresh` |
| `drifted_on_disk > 0` from `changes` | Disk edits newer than index |
| Open `blocked` task | Previous session left a question — read it |

---

## Why this exists (failure-mode evidence)

- v1 benchmarks: agents without projmem produced 0/7 truthful answers
  on multi-session memory tasks; agents with projmem produced 7/7,
  using 38% fewer tokens than free-form scratchpad. The verifier
  catches drift the scratchpad cannot.
- v2 hypothesis (benchmark TBD in `bench/v2/`): agents bypassing
  `projmem editing` reintroduce deleted code at a measurable rate;
  agents using the four mutation verbs do not. When the numbers land
  they will replace this paragraph.

The rules above are not opinions about good behavior. They are the
conditions under which the agent's output is trustworthy.

---

## When you don't know what to call

```bash
projmem ask "who uses src/foo.ts?"
projmem ask "what changed since last session?"
projmem ask "safe to delete <name>?"
projmem guide workflow | projmem guide commands | projmem guide capture
projmem usage
```

JSON mode everywhere: pass `--json` and parse the result. Don't
parse the human-readable output — it's not a contract.
