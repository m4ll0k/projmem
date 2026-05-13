# projmem — Constitution for AI agents working in this repo

These are not suggestions. They are conditions under which the AI
agent is allowed to take actions in this codebase. Each rule below
either holds, or the agent stops and reports the violation.

This is the generic agent surface — same rules as `CLAUDE.md`, with
MCP-equivalent verbs where applicable.

---

## Non-negotiables

### 1. projmem must be available

> **If projmem is unavailable, STOP and report. Do not proceed without it.**

That covers: the `projmem` binary missing from PATH, the MCP server
unresponsive for MCP-aware clients, the repo lacking a `.projmem/`
index. The agent's memory model assumes projmem is present; without
it, beliefs go unverified.

### 2. `contradicted_count > 0` halts work

If `projmem notes` (CLI) or `projmem_notes` (MCP) returns
`contradicted_count > 0`, **STOP**. A FACT claim saved earlier is
now refuted. Resolve before any further code change.

### 3. Announce every edit before you make it

Before touching ANY file, run the matching mutation verb. The CLI:

```bash
projmem editing  <path> --reason "..."        # before Edit / Write / Read
projmem creating <path> --reason "..."        # before creating a NEW file
projmem moving   <old> <new> --reason "..."   # before rename / move
projmem deleting <path> --reason "..."        # before delete
projmem done     <lease_id>                   # after successful change
projmem abandoned <lease_id> --reason "..."   # after abandoned attempt
```

The MCP-exposed equivalents are `projmem_editing`, `projmem_creating`,
`projmem_moving`, `projmem_deleting`, `projmem_done`,
`projmem_abandoned` (same args, JSON-shaped).

Reasons must be ≥ 20 chars with a verb + object. `"cleanup"` is
rejected; `"consolidating with shared/validators.ts"` is accepted.

### 4. ⚠ CRITICAL CONTEXT means HALT and engage

When `projmem editing` returns a `critical_prelude` field starting
with `⚠ CRITICAL CONTEXT`, you MUST:

1. State the intended change in plain text BEFORE any tool call.
2. Confirm the change does NOT touch any listed concern — name each
   concern and your reason.
3. Halt if the change overlaps. The lease is in `pending_approval`;
   wait for human approval through the projmem UI.

A critical prelude marks load-bearing code — files where prior edits
caused production incidents. It carries the weight of a compile
error.

### 5. End every task with the gate

```bash
projmem complete || exit 2
```

Exit 1 on any HIGH finding (drifted file, contradicted note,
dangling ref). Propagated to CI / git hooks / your harness.

---

## The seven core verbs (v1 surface)

| Verb | One-liner | Example |
|---|---|---|
| `projmem note add <target> "<prose>"` | Save a finding. Prose like `` `X` is defined at file:line `` auto-extracts FACT claims. | `projmem note add src/auth.py "\`verify_token\` is defined at src/auth.py:42"` |
| `projmem notes` | Project-wide summary; `contradicted_count` is the STOP signal. | `projmem notes --json` |
| `projmem session <target>` | Per-target bootstrap. | `projmem session src/auth.py` |
| `projmem conclude "<text>"` | Save a one-line conclusion; inline `@predicate(s, o)` → structured claims. | `projmem conclude "@defined-at(verify_token, src/auth.py:42)"` |
| `projmem fact-check "<draft>"` | Verify claims in a draft BEFORE shipping. Exit 2 on REFUTED. | `projmem fact-check "the helper is at src/auth.py:42"` |
| `projmem task ...` | Cross-session continuity: start / step / blocked / unblock / close / resume / list. | `projmem task resume` |
| `projmem refresh` | Incremental reindex after edits; auto-applies by default. | `projmem refresh` |

MCP equivalents: `projmem_note_add`, `projmem_notes`, `projmem_session`,
`projmem_fact_check`. Plus `projmem_symbol`, `projmem_reverse`,
`projmem_forward`, `projmem_search` for navigation.

`projmem usage` prints the full 60+ verb catalog when you need it.

---

## The four v2 mutation verbs

| Verb | What it does |
|---|---|
| `projmem editing <path> --reason "..."` | Open a lease + bundle guidance + history + ⚠ CRITICAL CONTEXT in one call. |
| `projmem creating <path> --reason "..."` | Open a lease for a NEW file. Warns if previously tombstoned. |
| `projmem moving <old> <new> --reason "..."` | Rename/move preserving lifeline + every attached note. |
| `projmem deleting <path> --reason "..."` | Tombstone (never genuinely delete). |
| `projmem done <lease_id>` | Close as success. Idempotent. |
| `projmem abandoned <lease_id>` | Close as abandoned. |

Read-only inspection: `projmem context <path> [--format
agent-prelude|human|json]`.

---

## Guidance kinds — which to use when

```bash
projmem note add <target> --kind guidance   --severity warn  "..."   # injected at edit
projmem note add <target> --kind constraint                  "..."   # environmental fact
projmem note add <target> --kind preference --severity info  "..."   # style choice
projmem critical add <target> --reason "..." --category security \
    --approved-by <user> [--incident-ref INC-…]                      # load-bearing
```

`critical` requires ≥ 1 cosigner (or `--self-cosign`), reason ≥ 40
chars, picks a category, has a 90-day review window, blocks edits by
default, propagates 1-hop via the reverse-dep graph.

---

## Session loop

1. **Start.** `projmem task resume`. Empty? `projmem notes`. If any
   `contradicted_count > 0` — stop and report.
2. **Investigate.** `projmem session <target>` for per-target
   bootstrap. `projmem note add <target> "<prose>"` as you learn.
3. **Edit.** Every Edit / Write / Read goes through `projmem
   editing` (the hook handles it if installed). Pay attention to
   the response.
4. **Close.** `projmem done <lease_id>` on success, `projmem
   abandoned` otherwise.
5. **Ship.** `projmem fact-check "<draft answer>"` before responding.
   `projmem complete || exit 2` before claiming done.

---

## Blocker signals (halt work, address first)

| Signal | Meaning |
|---|---|
| `contradicted_count > 0` | A saved FACT was REFUTED — re-investigate |
| `has_refuted` from `fact-check` | Draft has wrong claims |
| `critical_prelude` from `editing` | Load-bearing code — engage |
| `lease_state: pending_approval` | Critical-blocked; wait for human |
| `freshness_warning` | File changed on disk since index |
| `drifted_on_disk > 0` | Disk edits newer than index |
| Open `blocked` task | Previous session left a question |

---

## Why this exists

- v1 benchmarks: 0/7 truthful answers without projmem vs 7/7 with
  it on multi-session memory tasks. −38% tokens vs free-form
  scratchpad. The verifier catches drift the scratchpad cannot.
- v2 hypothesis (benchmark TBD): agents bypassing `projmem editing`
  reintroduce deleted code at a measurable rate; the four mutation
  verbs eliminate that failure mode. Numbers replace this paragraph
  when they land.

The rules are not opinions about good behavior. They are the
conditions under which the agent's output is trustworthy.

---

## When you don't know what to call

```bash
projmem ask "who uses src/foo.ts?"
projmem ask "what changed since last session?"
projmem guide workflow | projmem guide commands | projmem usage
```

JSON mode everywhere: pass `--json` and parse the result. Don't
grep the human output — it's not a contract.
