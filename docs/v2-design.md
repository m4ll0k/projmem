# `projmem` v2 — design

> Status: in-flight on the `v2-dev` branch. v1 is frozen at tag `v1.0.0`.
> This file is the source of truth for v2 scope. Update it when scope changes;
> do not let scope drift via PRs without updating this doc first.

## One-sentence pitch

projmem v2 turns projmem from a passive verifier into the **operating system for
AI coding sessions**: every intent is announced, every belief verified, every
action replayable, every load-bearing surface protected.

## The three pillars on top of v1

1. **Lifelines + intent log.** Every file mutation is announced with a reason.
   Files become event-sourced entities that survive renames and deletions. Git
   tracks the what; projmem tracks the why.
2. **Live observability + steering.** A local UI shows what the agent is doing
   in real time. A human can drop guidance notes mid-flight; the agent picks
   them up on its next tool call. Pause-agent-on-touch for critical files.
3. **Load-bearing protection.** Critical notes are stronger than guidance —
   they require cosigner approval to author, force review cadence, block edits
   until human approves, and propagate warnings to dependents.

## Hard rules for every step

- Strict build order. No starting step N+1 until N is committed, tested, and
  the existing 629 tests still pass.
- Each step ends with a release tag (`v2.0.0-alpha.N` → `v2.0.0-rc.1` → `v2.0.0`).
- All new verbs/migrations/hook scripts get tests in `tests/`.
- Existing v1 surface and data must survive.
- Dogfood: every step ships only after the new capability has been used on
  projmem itself for at least one real change.

## Out of scope for v2 (park to `docs/v3-roadmap.md` if you find yourself building these)

- Symbol-level lifelines (file-level is 80/20).
- Multi-user / team-shared mode. Local-only stays the v2 design.
- LSP shim (UI delivers same value more flexibly).
- Cloud anything.
- Auth / RBAC.
- Trigger-scoped / phase-scoped custom prompts (path-scoped only).
- Auto-merging lifelines without human confirm.
- Replay scrubber UI (planned for v2.1 / v3).

---

## Step 0 — foundation (no user-visible changes)

**Branching / tags**
- Cut `git tag v1.0.0` from current `main` and push.
- Create `v2-dev` from `main`. All v2 work lands there.

**Schema additions** (all migrations under `projmem/migrations/` with version stamps,
both up and down paths; existing data must survive)

```sql
-- existing files/notes tables get a nullable lifeline_id column
ALTER TABLE files ADD COLUMN lifeline_id TEXT;       -- UUID
ALTER TABLE annotations ADD COLUMN lifeline_id TEXT; -- UUID, follows the file on move

CREATE TABLE file_lifeline (
  id                  TEXT PRIMARY KEY,             -- UUID
  current_path        TEXT NOT NULL,
  created_at          REAL NOT NULL,                -- epoch seconds
  created_reason      TEXT NOT NULL,
  created_by          TEXT,                         -- agent_id or null
  tombstoned_at       REAL,
  tombstoned_reason   TEXT,
  replaced_by         TEXT                          -- JSON array of UUIDs
);

CREATE TABLE file_event (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  lifeline_id         TEXT NOT NULL,
  kind                TEXT NOT NULL,                -- created|edited|moved|deleted|leased|released
  at                  REAL NOT NULL,
  reason              TEXT,
  session_id          TEXT,
  diff_summary        TEXT,                         -- JSON: { lines_added, lines_removed, ... }
  symbols_affected    TEXT,                         -- JSON array
  FOREIGN KEY(lifeline_id) REFERENCES file_lifeline(id)
);

CREATE TABLE edit_lease (
  id                  TEXT PRIMARY KEY,             -- UUID
  lifeline_id         TEXT NOT NULL,
  opened_at           REAL NOT NULL,
  expires_at          REAL NOT NULL,
  closed_at           REAL,
  closed_kind         TEXT,                         -- done|abandoned|expired|denied
  agent_id            TEXT,
  intent              TEXT,                         -- the --reason argument
  state               TEXT NOT NULL DEFAULT 'open', -- open|pending_approval|closed
  FOREIGN KEY(lifeline_id) REFERENCES file_lifeline(id)
);

CREATE INDEX file_event_lifeline_idx ON file_event(lifeline_id, at);
CREATE INDEX edit_lease_open_idx ON edit_lease(state, expires_at);
CREATE INDEX file_lifeline_path_idx ON file_lifeline(current_path);
CREATE INDEX file_lifeline_tombstone_idx ON file_lifeline(tombstoned_at);
```

**Backfill**: every existing file gets a fresh `lifeline_id` with
`created_at = first commit timestamp` (best-effort `git log --diff-filter=A
--follow --reverse <path>`) if available, else `now()`, and
`created_reason = "backfilled — pre-v2 lifeline"`.

**Note kinds enum extension** — extend existing kinds (`note`, `task`, …) with
`guidance`, `constraint`, `preference`, `critical`. Existing `note` kind stays
as default.

**Ship criteria**: pytest green, CLI surface unchanged from user POV, schema
migrated up + down cleanly on a real v1 db. Tag `v2.0.0-alpha.0`.

---

## Step 1 — the four mutation verbs

```text
projmem editing  <path>           [--symbol X] [--reason "..."]
projmem creating <path>                          --reason "..."
projmem moving   <old> <new>                     --reason "..."
projmem deleting <path>                          --reason "..."
projmem done     <lease_id>
projmem abandoned <lease_id> [--reason "..."]
```

Behavior:

- `editing` returns `{ lease_id, expires_at, guidance[], history{}, warnings[] }`
  in one call — guidance for file + parent directories + 1-hop dependencies,
  no separate `context` call needed (collapsed into `editing`).
- Leases auto-expire 5 minutes after last activity heartbeat. Background sweep
  marks them `closed_kind = "expired"`.
- **Reason quality gate**: reject reasons under 20 chars, reject single-word
  reasons, with message:
  `"reason needs a verb and an object — e.g. 'consolidating with shared/validators.ts' not 'cleanup'"`.
- `creating` checks for prior tombstoned lifelines at the same path. If any,
  `warnings[]` contains `"this path was deleted N days ago, reason: '<reason>',
  replaced by: <path or null>. Consider editing the replacement instead."`.
- `moving` preserves `lifeline_id`; notes follow automatically; appends a
  `moved` `file_event`.
- `deleting` sets tombstone fields; lifeline stays queryable forever.
  Add `projmem forget <lifeline_id> --yes-really-purge` as a separate rare-purge verb.
- `done` is idempotent.
- All four verbs work via CLI **and** MCP server.

**Ship criteria**: tests for round-trip, idempotency, tombstone-then-recreate,
move-preserves-notes, lease expiry, reason gate. Tag `v2.0.0-alpha.1`. From
this point forward every edit I make goes through `editing`/`done`.

---

## Step 2 — `--kind guidance` notes + Claude Code hook

- Extend `projmem note add` to accept `--kind guidance` (and `constraint`,
  `preference` — same injection path, different categories).
- Guidance notes get `severity` (`info` / `warn` / `critical`) and
  optional `ttl_days`.
- New verb: `projmem context <path> [--format=agent-prelude|human|json]`
  returns guidance notes for path + parent dirs + 1-hop deps, filtered by
  staleness. (Note: `editing` already inlines this — `context` is for
  read-only inspection without opening a lease.)
- Verifier runs on guidance notes: stale-symbol references are marked
  `stale` and excluded from injection by default.
- New verb: `projmem hook install --claude-code [--dry-run]` writes
  `.claude/hooks/PreToolUse` and `.claude/hooks/PostToolUse` scripts.

Hook behavior:

- `PreToolUse` for `Edit`/`Write`/`Read`:
  extract `file_path` from `$TOOL_INPUT`, call `projmem editing
  "$file_path" --reason "claude-code PreToolUse"`, format response as agent
  prelude, return via the hook protocol so Claude Code injects it into the
  tool result.
- `PostToolUse`: close the lease via `projmem done`. If `PostToolUse`
  fires without an active lease (Claude bypassed `editing`), auto-create an
  *implicit* lease (`opened_at = now() - 1s`, `agent_id = 'implicit'`) and
  log it. The UI renders implicit leases with a distinct style — this gives
  the "announcement compliance rate" metric.
- Hooks MUST be no-op (exit 0) if projmem isn't initialized in the repo.
- **Security**: treat all note text as data — never `eval`, never
  shell-interpolate note bodies into commands. Hook scripts pass through
  `jq` / `python -c` argv-style only.

**Ship criteria**: a real Claude Code session in another project receives
guidance text in tool results. Transcript committed to
`bench/v2-smoke/claude-code-hook-demo.md`. Tag `v2.0.0-alpha.2`.

---

## Step 3 — `--kind critical` notes + cosigner + edit-blocking

Critical notes = guidance notes with stricter semantics. Extension fields:

```text
category          security | compliance | performance | business_logic | data_integrity | other
incident_refs[]   string array (issue urls, post-mortem ids)
approved_by[]     min 1 — REQUIRED at authoring time
last_reviewed_at  epoch seconds
review_window     default 90 days
blast_radius_hops default 1
blocks_edits      default true
```

New verbs:

```text
projmem critical add <path> --reason "..." --category <c>
                            [--incident-ref "..."] [--review-window 90d]
                            [--blocks-edits] --approved-by <user>
                            [--self-cosign]      # explicit confirm for sole-maintainer
projmem critical list
projmem critical review <id>          # resets last_reviewed_at
projmem critical pending-review       # past review_window
```

**Authoring gate**: `critical add` requires `--approved-by <user>` (or
`--self-cosign` with explicit confirmation). This is what prevents critical-note
inflation.

**Edit-blocking**: when `editing` opens a lease on a path with active critical
notes where `blocks_edits=true`, the lease enters `pending_approval` state.
The daemon (Step 5) grants/denies. Until the daemon exists, the lease grants
immediately but the response includes a **prominent** `⚠ CRITICAL CONTEXT`
prelude the agent must engage with.

**Blast-radius propagation**: when computing guidance for a file, walk
reverse-deps up to `blast_radius_hops` and include critical notes from those
neighbors with a `"1-hop dependent of critical file X"` prefix.

**Ship criteria**: tag `v2.0.0-alpha.3`. Three critical notes added to
projmem's own most load-bearing files: `projmem/claims.py` (verifier core),
`projmem/store.py` (index schema), `projmem/mcp_server.py` (MCP entry).

---

## Step 4 — the CLAUDE.md constitution

Rewrite `projmem init claude` template. **Imperative, not advisory.** Mandatory
elements:

- Top-line non-negotiable: *"If projmem is unavailable, STOP and report. Do not
  proceed without it."*
- Explicit STOP condition on `contradicted_count > 0`.
- The seven verbs from v1 (verbatim, with one-line description + one example
  each — do NOT replace with "see `projmem --help`").
- The four v2 mutation verbs in the protocol section with the same treatment.
- Behavior on `⚠ CRITICAL CONTEXT`: state intent, confirm scope, halt if scope
  overlaps locked concerns.
- Failure-mode evidence (the v2-benchmark number filled in at Step 8).

Same updates to the `AGENTS.md` template (for Codex / Cursor / Continue), with
MCP equivalents of the verbs.

**Ship criteria**: `projmem init claude` in a fresh repo produces the new
constitution. Tag `v2.0.0-alpha.4`.

---

## Step 5 — the daemon

New process: `projmem daemon [--port 7777]`. FastAPI + websockets.

Receives events from hooks via a local Unix socket (named pipe on Windows).
Broadcasts to connected websocket clients.

HTTP endpoints:

```text
POST /notes                          add a guidance note
POST /critical                       add a critical (cosigner check)
POST /control/pause                  flip pause flag (read by PreToolUse hook)
POST /control/resume
POST /control/approve/<lease_id>     manual lease gating
POST /control/deny/<lease_id>
GET  /state                          current activity, open leases, last N events
WS   /events                         event stream
```

Daemon stays optional — CLI works without it for headless/CI use.
`projmem ui` (Step 6) spawns it; bare CLI does not.

**Security**: no auth (local-only). **Bind to `127.0.0.1` only, never `0.0.0.0`.**
Paranoid check that refuses to start if bound publicly.

Tests:
- Hook → socket → daemon → websocket roundtrip with a mock client.
- Pause-mode actually blocks `PreToolUse` until `/control/approve` arrives.
- Daemon refuses to bind to non-loopback.

**Ship criteria**: tag `v2.0.0-alpha.5`.

---

## Step 6 — UI minimal slice (activity feed only, no graph)

Frontend stack: **Vite + React + TypeScript + Tailwind + shadcn/ui + Zustand +
native WebSocket**. No Next.js. No Webpack.

Single command `projmem ui` spawns daemon + opens `http://localhost:7777`.

Layout for this step:
- Left rail: streaming activity feed (every `editing` / `creating` / `moving`
  / `deleting` / lease close).
- Top bar: pause/resume toggle, project name, search.
- Center: empty (graph slot, "coming soon" placeholder).
- Right rail: clicked-item shows that lifeline's notes + lease detail.

Visual treatment for implicit leases (dashed border, "agent forgot to announce"
tooltip).

**Ship criteria**: build passes, websocket roundtrip works. Twitter-shareable
screenshot at `docs/screenshots/v2-activity-demo.gif`. Tag `v2.0.0-alpha.6`.

---

## Step 7 — graph view + ghost nodes + node inspector

Center pane: Cosmograph-based graph. Semantic zoom — directory cluster bubbles
at zoom-out, individual file nodes at mid-zoom, symbols at high zoom.

Node visual encoding:
- Color = staleness (fresh=green, moved=yellow, contradicted=red,
  uncheckable=gray)
- Size = reverse-dep count
- Halo/pulse on currently-leased nodes, decays over 30s after lease close
- Critical notes: red outline + warning glyph

Edge encoding:
- import = solid
- reverse-dep = dashed
- test-of = dotted
- replaced-by = dashed-arrow to ghost

**Ghost nodes**: toggle "show history" — tombstoned lifelines appear faded with
dashed edges to their `replaced_by` successors. Hover shows deletion reason.

Default graph filter: 1-hop from currently-leased node. Toggle "show full graph"
warns if `node count > 5000`.

Right-rail node inspector tabs:
- **Notes**: existing projmem notes + verifier verdicts.
- **Guidance**: add/edit guidance, severity dropdown, scope toggle, instant save.
- **Critical**: shown only if any exist; read-only display + "review" button.
- **History**: full lifeline as a vertical timeline.

Performance budget: 5k-node graph layout interactive in <2s on a 2020 MacBook.
For >50k nodes, default to cluster-bubble view; never auto-expand the full
hairball.

**Ship criteria**: tag `v2.0.0-rc.1`. New README hero screenshot.

---

## Step 8 — empirical validation + new README + license decision

New bench harness `bench/v2/` — three setups:

1. **Reintroduction bench**: agent gets a task that would naturally reintroduce
   a file deleted last session. Baseline vs. v1 vs. v2-with-creating-warnings.
   Measure: reintroduction rate.
2. **Guidance bench**: same task, baseline vs. v2-without-guidance vs.
   v2-with-3-seeded-guidance-notes. Measure: correctness, token cost.
3. **Critical bench**: agent tries to "simplify" a file marked critical with
   `blocks_edits=true`. Daemon in pause-mode. Measure: time-to-human-spotted
   vs. ctrl-c-and-restart baseline.

README updates:
- New hero pitch. Working candidate:
  *"projmem is where your AI agent thinks out loud — and where you check
  whether it's still right."*
- Move the "seven verbs" table near the top.
- Add a parallel "four mutation verbs" table.
- New benchmark numbers replace placeholders.
- v2 positioned as evolution, not pivot.
- Total CLI verb table stays under 15 visible commands. Power-user verbs live
  in `projmem usage --json`.

**License decision** — must be made before `v2.0.0` ships. Three options to
evaluate in `docs/v2-license-decision.md`:

1. Stay PolyForm NC.
2. Move to BSL with 2-year conversion to Apache 2.0.
3. Open-core split: core stays PolyForm NC, daemon + UI become a separate paid
   component under a commercial license.

**Ship criteria**: `v2.0.0` final tag. README has real numbers. License is
explicit. PyPI publish.

---

## Engineering notes

- Migrations live in `projmem/migrations/`; each has `up.sql` + `down.sql` +
  a version stamp.
- A new internal module `projmem/lifelines.py` houses lifeline + lease logic.
- Hooks: shell-injection safety is non-negotiable. A note containing
  `$(rm -rf /)` must not execute under any path. Pass note bodies via argv to
  receiver tools, never expand inline in `sh -c`.
- Daemon bind check: refuse if `--port` resolves to a non-loopback address;
  add a startup assertion.
- Every new public verb returns JSON via `--json` (default behavior; matches
  v1 surface).

## Glossary

- **Lifeline** — a file's persistent identity across renames/moves/deletions.
  Survives forever once created; never garbage-collected outside an explicit
  `projmem forget`.
- **Lease** — an open intent to edit. Has a `lifeline_id`, a reason, a TTL,
  and a closure (`done` / `abandoned` / `expired` / `denied`).
- **Guidance note** — a human-authored or agent-authored hint shown to the
  agent at next `editing` of the scope. Has severity, optional TTL.
- **Critical note** — a guidance note with stricter authoring gate, review
  cadence, edit-blocking flag, and blast-radius propagation.
- **Implicit lease** — auto-created when `PostToolUse` fires without a
  matching `editing` (the agent edited without announcing). Distinct UI
  treatment; used to compute announcement compliance rate.
