# projmem v2 — full design summary

This is the consolidated record of everything we worked through, plus a final pass of "wow features" that fall out naturally once the foundation is in place. Save this somewhere — paste it into `docs/v2-design.md` in the repo. When you're three weeks deep in Cosmograph layout code, this is what you come back to.

---

## Part I — The reframe

**What projmem v1 is:** drift-aware code memory. The agent saves beliefs as structured claims; the verifier re-checks them on every read; stale beliefs flip to `contradicted` and a CI gate fires. It's a defensive tool. It catches one specific failure mode: the agent shipping wrong answers built on refuted premises.

**What projmem v2 becomes:** the **operating system for AI coding sessions.**

Every intent is announced. Every belief is verified. Every action is replayable. Every load-bearing surface is protected. Nothing important is lost between sessions.

The shift is from a passive verifier you query, to an active layer the agent *operates through* — and a human surface where you can watch, steer, and intervene in real time. The verifier is now one capability inside a bigger story, not the whole story.

The pitch sentence becomes something close to: **"projmem is where your AI agent thinks out loud — and where you check whether it's still right."** Workshop the line; the right one is probably something you find by saying versions out loud. But the shape is: *the contrast (think-out-loud vs. check) + a verb (think / check) + a posture (real-time, never-lying).*

---

## Part II — The four conceptual pillars

### Pillar 1: Lifelines

Files in projmem are no longer rows that disappear when deleted. They're **event-sourced entities** with stable identity over time.

A lifeline has:
- A stable UUID that survives renames and moves
- A creation event with a `--reason`
- A timeline of edit events, each with a `--reason`
- Optional move/rename events that update the path but preserve identity
- A tombstone event (never genuine deletion) with a `--reason` and optional `replaced_by[]` pointer

Why this matters: git tracks *what changed*. projmem tracks *why*. Those are different streams and nobody has unified them at the file-identity level with a verifier on top. When Claude tries next session to recreate a file you killed, projmem can say "this existed and was deleted 19 days ago, reason: consolidated into shared/validators.ts, replaced by: shared/validators.ts. Do not re-create. Use the replacement." That sentence is unobtainable from git, from LSP, from any existing tool. It's the wedge.

Notes, guidance, and critical annotations attach to `lifeline_id` — not path. When a file moves, all its context follows automatically. When a file dies, its context doesn't disappear; it archives with the lifeline and remains queryable forever.

### Pillar 2: Announce-before-action

Four mutation verbs cover every change Claude can make to the codebase:

```
projmem editing   <path> [--symbol X] --reason "..."
projmem creating  <path>               --reason "..."
projmem moving    <old> <new>          --reason "..."
projmem deleting  <path>               --reason "..."
```

Each returns a lease ID. The agent runs `projmem done <lease_id>` after, or `projmem abandoned <lease_id>` if it gave up. Leases auto-expire after 5 minutes of inactivity to handle crashes.

The key design choice: `editing` is **double duty**. The same call that announces intent also returns the guidance notes, history warnings, and critical context for that path. One call, two purposes. The agent has zero excuse to skip it.

A `creating` call against a path that was previously tombstoned returns warnings naming the prior deletion reason. An `editing` call on a path with critical notes can block, requiring human approval in the UI. A `moving` call preserves the lifeline, so notes survive refactors that would otherwise orphan all your hard-won context.

The verbs are enforced two ways: explicitly in CLAUDE.md (the constitution — see Pillar 4), and mechanically via the Claude Code `PreToolUse` hook (which intercepts every Edit/Write/Read and runs `editing` regardless of whether Claude remembered).

### Pillar 3: Note kinds with different semantics

v1 has one kind: notes that contain FACT claims and get verified. v2 expands the vocabulary:

| Kind | Verified? | Injected at edit time? | Special behavior |
|---|---|---|---|
| `note` (v1 default) | Yes | No | Existing v1 surface |
| `guidance` | Symbols only | Yes | Surfaced to agent on `editing`; severity tiers; optional TTL |
| `constraint` | No | Yes | Environmental facts, project conventions |
| `preference` | No | Yes | User-style preferences ("tabs over spaces") |
| `critical` | Yes + cosigned | Yes, as `⚠ CRITICAL CONTEXT` | Requires cosigner to author; blocks edits until approved; propagates 1-hop |

`critical` is the load-bearing-code feature. It's how you mark "this file signs every API token, three prior changes caused production incidents, edits require human review." It's deliberately harder to author than `guidance` (cosigner required, longer reason, category field, optional incident references), because the value of `critical` collapses to noise the moment everyone marks their pet module as critical. The friction is the feature.

Critical notes also propagate: when computing guidance for a file, the system walks 1-hop reverse-dependencies and inherits critical warnings from neighbors ("you're editing a 1-hop dependent of `src/auth/jwt.py` which is marked critical"). Edit thin ice and you'll know.

Authoring also has review cadence. Every critical note has `last_reviewed_at` and a configurable review window (default 90 days). Past the window, the UI surfaces "review needed." Not invalid — flagged. A human re-confirms or amends. The verifier can check whether the symbols still exist; it cannot check whether the *reasoning* is still right. Only humans can. projmem at least makes sure the human is reminded.

### Pillar 4: The CLAUDE.md constitution

CLAUDE.md changes from polite suggestions to a constitution. Imperatives only:

- "Before editing any file, run `projmem editing <path>`."
- "If `projmem notes` returns `contradicted_count > 0`, STOP and report which beliefs are refuted."
- "If projmem is unavailable, STOP. Do not proceed without it."
- "If you see `⚠ CRITICAL CONTEXT`, you MUST: state intended change, confirm whether it touches listed concerns, halt if yes."

The seven core v1 verbs are listed verbatim with examples — never replaced with "see `projmem --help`." The four v2 mutation verbs are added with the same treatment. Evidence appears in the prompt itself ("agents skipping `projmem editing` reintroduced deleted code 4/5 runs in v2 benchmark"). Claude responds dramatically better to rules framed as facts about the world than as suggestions framed as opinions about good behavior.

But CLAUDE.md is only the *intent* layer. The *enforcement* layer is the `PreToolUse` hook. Claude can drift in long sessions; the hook can't. Belt + suspenders: constitution explains why, hook makes sure it happens.

---

## Part III — The architecture, layer by layer

### Layer 0: Storage (extends v1)

```
file_lifeline (
  id              uuid pk,
  current_path    text | null,        -- null after tombstone
  created_at      timestamp,
  created_reason  text not null,
  created_by      session_id,
  tombstoned_at   timestamp | null,
  tombstoned_reason text | null,
  replaced_by     uuid[] | null
)

file_event (
  lifeline_id     uuid fk,
  kind            enum (created | edited | moved | deleted | abandoned),
  at              timestamp,
  reason          text not null,
  session_id      uuid,
  diff_summary    text,                -- "added 12 lines, removed 3"
  symbols_affected text[]
)

edit_lease (
  id              uuid pk,
  lifeline_id     uuid fk,
  opened_at       timestamp,
  expires_at      timestamp,
  closed_at       timestamp | null,
  closed_kind     enum (done | abandoned | expired | implicit),
  agent_id        text,
  intent          text
)

note (extended from v1)
  + kind           enum (note | guidance | constraint | preference | critical)
  + severity       enum (info | warn | critical) | null
  + ttl_days       int | null
  + lifeline_id    uuid fk              -- replaces direct path attachment

critical_note (extends note with kind=critical)
  + category          enum (security | compliance | performance |
                            business_logic | data_integrity | other)
  + incident_refs     text[]
  + approved_by       user_id[] not null
  + last_reviewed_at  timestamp not null
  + review_window     interval default '90 days'
  + blast_radius_hops int default 1
  + blocks_edits      bool default true
```

Migrations are additive — every v1 row gets a backfilled lifeline_id; existing surfaces don't break.

### Layer 1: Symbol-level granularity (for the 1M-line case)

The storage unit becomes the symbol, not the file. Files become *containers* of symbols, shown in the graph as clusters that expand on zoom.

Symbol records: `{file, qualified_name, kind, byte_range, line_range, parent_symbol}`. Edges at symbol level: calls, define-use, modifies-global-X. Tree-sitter incremental parsing (`parser.parse(new_source, old_tree)`) gives you `changed_ranges` cheaply on every save — you reindex only the affected symbol ranges, not the whole 1M-line file.

UI uses semantic zoom: directory bubbles at zoom-out → file cluster bubbles at mid-zoom → individual symbol nodes at high zoom. Default filter shows 1-hop from current focus. "Show full graph" toggle warns if node count > 5000.

Symbol-level lifelines (where individual functions have stable identity across edits, splits, renames) is a real research problem and parks for v3. File-level lifelines deliver 80% of the value at 20% of the work.

### Layer 2: The daemon

A new local process: `projmem daemon` on `localhost:7777`. FastAPI + websockets. Binds to `127.0.0.1` only, refuses to start if bound publicly (paranoid check). No auth, local-only.

Receives events from hooks via Unix socket. Broadcasts to connected websocket clients. HTTP endpoints for UI actions:

- `POST /notes`, `POST /critical` — authoring
- `POST /control/pause`, `/resume` — flip pause-mode that hooks read
- `POST /control/approve/<lease_id>`, `/deny/<lease_id>` — manual gating for critical-blocked paths
- `GET /state` — current activity, open leases, recent events

The daemon stays optional. CLI works without it. `projmem ui` spawns it; bare CLI doesn't.

### Layer 3: The hook layer

Claude Code: `projmem hook install --claude-code` writes:

- **PreToolUse** for `Edit`/`Write`/`Read`: extracts `file_path` from `$TOOL_INPUT`, calls `projmem editing` (or `creating`/`moving`/`deleting` based on the tool), formats the response (lease ID, guidance, history warnings, critical preludes) as an injection block, returns via the hook protocol so Claude Code adds it to the tool result Claude actually sees.
- **PostToolUse**: closes the lease via `projmem done`. If no active lease exists (Claude bypassed `editing`), auto-creates an *implicit lease* (`closed_kind=implicit`) for the audit trail.

Two non-negotiable safety properties: hooks must be no-op on missing projmem state (never break Claude Code sessions); note text is treated as data only (never eval'd, never shell-interpolated into commands).

Codex / Cursor / Continue: fall back to the MCP server, which now exposes the four mutation verbs alongside the existing tools. Less bulletproof (relies on the agent reading the system prompt) but works everywhere.

### Layer 4: The UI

Stack: Vite + React + TypeScript + Tailwind + shadcn/ui + Zustand + native WebSocket. Cosmograph for the graph (WebGL, handles 50k+ nodes). No Next.js — local tool, no SSR needed.

Single command: `projmem ui` spawns daemon, opens browser, done.

Layout:

**Left rail — Activity feed.** Streaming list of every tool call and lease event. Timestamps, clickable, links to the affected node. Implicit leases render with a dashed border (visual "agent forgot to announce" signal). This panel alone is the shippable first slice of the UI — the Twitter screenshot release.

**Center — Graph canvas.** Cosmograph with semantic zoom. Node color = staleness. Node size = reverse-dep count. Node halo/pulse = currently leased, decays over 30s. Edge color = relation type (import / reverse-dep / test-of / replaced-by). Critical files: red outline + warning glyph. **Ghost nodes toggle** — tombstoned lifelines appear faded with dashed edges to successors; hover for deletion reason. This is the screenshot feature.

**Right rail — Node inspector.** Tabs: Notes (verifier verdicts), Guidance (authoring panel), Critical (read-only + review button if past window), History (lifeline timeline, reasons inline, diffable).

**Top bar — Global controls.** Pause/resume agent, one-shot system message inject, project switcher, search.

**Bottom strip — Live diff.** Streaming unified diff during an edit (Google Docs-style live cursor). Pure candy. Critical for demo videos.

**Pause-agent feature** deserves a callout: it's a single switch with surprising depth. When pause-mode is on, the PreToolUse hook blocks on a websocket message from the UI before returning. The agent's next tool call waits for you. Modes: full-auto / review-every-edit / review-only-paths-matching-glob / off. Once you've seen Claude *pause for you in real time and resume on your nod*, you don't go back. This is the feature people screenshot and remember.

---

## Part IV — The build order (strict; do not reorder)

Each step is a shippable milestone. Tag a release after each. Don't start step N+1 until step N is committed, dogfooded, and the test suite is still green.

0. **Foundation** — `v1.0.0` tag, `v2-dev` branch, `docs/v2-design.md`, schema migration, new tables (additive). User-visible CLI unchanged. *Tag `v2.0.0-alpha.0`.*
1. **Four mutation verbs + lease model** — CLI + MCP. Reason quality gates. Use them yourself from this point forward. *Tag `v2.0.0-alpha.1`.*
2. **`guidance` kind + Claude Code hook installer** — `PreToolUse`/`PostToolUse` scripts. Implicit lease detection. Inject guidance at edit time. *Tag `v2.0.0-alpha.2`.*
3. **`critical` kind + cosigner + edit-blocking** — `projmem critical add`/`list`/`review`/`pending-review`. Blast-radius propagation. Mark 3 critical notes on projmem's own load-bearing files (dogfood). *Tag `v2.0.0-alpha.3`.*
4. **CLAUDE.md constitution** — imperative template. Update `projmem init claude` generator. Same for `AGENTS.md`. *Tag `v2.0.0-alpha.4`.*
5. **Daemon** — FastAPI + websockets on `127.0.0.1:7777`. Pause-mode endpoint. Loopback-only safety check. *Tag `v2.0.0-alpha.5`.*
6. **UI — activity feed only.** No graph yet. This is the Twitter release. Commit a screencast GIF. *Tag `v2.0.0-alpha.6`.*
7. **Graph + ghost nodes + node inspector.** Full UI. Performance budget: 5k nodes interactive in <2s. *Tag `v2.0.0-rc.1`.*
8. **Empirical validation + new README + license decision.** Three new bench setups (reintroduction, guidance-injection, critical-block). README hero rewrite. License explicit. PyPI publish. *Tag `v2.0.0`.*

The discipline that produced the "seven verbs that matter" insight in v1 — ship, watch, observe — is the same discipline that needs to run through v2. Don't try to land it all at once. The implicit-lease metric, the announcement-compliance rate, the guidance-effectiveness number — all of these only emerge if you stage releases and watch real usage.

---

## Part V — Out of scope (resist scope creep)

- Symbol-level lifelines (file-level is 80/20 — park for v3)
- Multi-user / team-shared mode (local-only stays the v2 design; leave schema room but don't build the relay)
- LSP shim (UI delivers same value, more flexibly)
- Cloud anything; auth; RBAC
- Trigger-scoped and phase-scoped custom prompts (only path-scoped for v2)
- Auto-merging lifelines without human confirm
- Replay scrubber (planned for v2.1 — activity feed is enough for v2.0)

---

## Part VI — The "wow" layer

Everything below falls out naturally from the pillars above. None of it requires new infrastructure — just clever use of what you'll already have. These are the features that turn the README from "interesting tool" into "okay I have to try this."

### 1. Session replay as a scrubbable video

Once every action is announced with intent and timestamped, your event log *is* the replay. Build a horizontal scrubber along the bottom of the UI. Drag it back; the graph rewinds. Each event is a row: timestamp, verb, target, reason, outcome, duration. Click an event → the graph snaps to that moment, the node inspector shows what guidance was injected, the diff strip shows what changed. You let Claude run for two hours, come back, scrub the replay in 90 seconds, find the one bad decision. Demo this once at a meetup and you'll have a line of people asking how to install. (v2.1 feature — but design the event log for it from day one.)

### 2. The "blast radius preview" before an edit

When Claude calls `projmem editing src/auth/jwt.py`, before the lease is granted, project what's about to change. Compute the 2-hop reverse-dependency closure. Surface it in the response: *"Editing this file touches 12 importers, 4 of which have failing tests as of last `pytest` run, 1 of which is marked critical."* The agent now knows the scope of its blast radius *before* it strikes. Cheap to compute on top of your existing graph. Massively useful for both the agent's reasoning and the human watching.

### 3. The "did Claude actually understand what it edited" check

After `projmem done`, fire a verification pass: did the edit match the announced `--reason`? You can sanity-check this cheaply by asking a small model "does this diff plausibly implement the stated intent" or by running the agent's own claims against the verifier. A mismatch means Claude said it was doing X but actually did Y. That's a screenshot-able failure mode worth flagging. (Out of scope for v2.0 — but a clean v2.x add.)

### 4. The "trap detector"

A pattern emerges from your benchmarks: certain files get edited and reverted repeatedly across sessions. That's a footgun. projmem already has the data — every edit is in `file_event`. Run a periodic analyzer: "this lifeline has been edited and reverted N times in the last M sessions, last revert reason: '...'." Automatically promote it from `note` to a draft `guidance` note suggesting "this file has been edited-and-reverted 4 times; review before changing." You're turning the event log into self-improving institutional memory. No other tool does this because no other tool has the event log.

### 5. The "where would Claude get stuck" precomputation

When you start a session, the daemon can precompute likely problem zones: files with contradicted notes, files with critical edit-blocks, files with high blast radius and stale guidance. Surface them as a "session weather report" in the activity feed: *"5 files have stale critical notes. 2 files have contradicted FACTs blocking edits. Recommended: resolve before agent session."* You become the pre-flight checklist nobody had.

### 6. Inline "second opinion" notes

A guidance note kind that triggers when Claude is *about to do something specific* — not just edit a file. `projmem second-opinion add --trigger "any call to subprocess.run with shell=True" --advice "use shlex.split instead, see SEC-117"`. The `PreToolUse` hook scans the proposed `new_string` against trigger patterns and injects matching second opinions before the tool runs. Effectively a Semgrep rule that talks to the agent instead of failing the build. (Power-user feature; ship after v2.0.)

### 7. The "explain this file" command driven by lifelines

`projmem explain src/auth/jwt.py` returns a chronologically-ordered narrative: *"Created 2025-08-12 to extract token logic from middleware.py (reason: 'separation of concerns for testability'). Edited 14 times. 3 critical notes attached. Most recent edit changed RS256 to HS256, was reverted next session with reason 'compliance — must remain RS256'. Marked critical 2026-01-03 after incident #247."* You've made the file's autobiography readable. No other tool has the raw data to produce this paragraph because no other tool captures the *why*. This single command in a screen recording is probably the best 30-second pitch for the product.

### 8. The "diff Claude's mental model against reality" tool

A v2 superpower nobody else can ship: take the agent's session-end summary of "what I changed and why," cross-reference it against `file_event` entries from that session, surface mismatches. *"You reported you 'simplified the auth flow' but file_events show 0 edits to anything under `src/auth/`. The actual changes were to `src/routing/middleware.py`. The mental model differs from the actions."* This is the kind of feature that makes senior engineers trust AI agents more — and makes them trust agents *less* in exactly the right ways.

### 9. The "agent compliance dashboard"

A small panel in the UI: rolling stats from the implicit-lease counter. *"Last 100 tool calls: 87 announced, 13 implicit. Compliance rate: 87%. Trend: improving (vs. 71% last week)."* You can correlate this against task success rates and use it to tune your CLAUDE.md prompt. You're not just shipping a tool; you're shipping the instrumentation to measure whether your tool is working. That's rare.

### 10. The "ghost cluster" view

Toggle "show all history" on the graph and watch the ghost nodes light up — every file ever created, every rename, every deletion, faded but visible. Hover any ghost to see its full obituary. Suddenly the graph isn't just a snapshot of the codebase; it's a *time-lapse* of every decision ever made about it. This is the screenshot that goes viral. Pair it with a slider that filters by date range and you've made the codebase's evolution legible in a way `git log` fundamentally cannot.

---

## Part VII — The empirical commitments

Every claim in the v2 README needs a number behind it. Three new benchmarks:

1. **Reintroduction bench** — agent task that would naturally recreate a file deleted last session. Measure: reintroduction rate baseline vs. v1 vs. v2-with-creating-warnings.
2. **Guidance bench** — same task seeded with 3-5 guidance notes flagging a known footgun. Measure: correctness, token cost.
3. **Critical bench** — agent tries to "simplify" a critical-blocked file. Daemon in pause-mode. Measure: time-to-human-spotted vs. ctrl-c-and-restart baseline.

If guidance doesn't move the correctness number, the feature isn't earning its weight and you should rethink it. If critical-block doesn't reduce bad-edit-shipped rate, the prelude format is too soft. The bench tells you, not your intuition.

Keep the existing v1 numbers (629 tests, 57 runs, −38% tokens) prominently in v2 README. They're the credibility throughline.

---

## Part VIII — License and positioning

Decide before v2.0.0 ships. Three options, in `docs/v2-license-decision.md`:

1. Stay PolyForm NC. Safe; blocks the corp-scale path.
2. BSL with 2-year conversion to Apache 2.0. Standard modern choice.
3. Open-core split: core PolyForm NC, daemon + UI under a commercial license. Most monetizable; complicates the codebase.

Pick one with explicit reasoning. Don't ship without this committed.

README positioning: don't apologize for the scope shift. Write the v2 README as if this was always the plan. Move the "seven verbs that matter" table near the top — it's the best signal of taste in the doc. Add a parallel "four mutation verbs" table. Keep total visible CLI surface under 15. Empirical bias stays throughout.

---

## Part IX — The one-line summary to come back to

When you're lost in week three of UI work, the load-bearing sentence is this:

**Git tracks what changed. projmem tracks why it changed, whether it's still true, who edited it, what it touches, and when it shouldn't have been touched at all — and it tells your AI agent all of that, in real time, every time the agent is about to make a move.**

That's the product. Everything else is implementation detail.

---

Go build. The brief in the previous message is what you paste into Claude Code; this document is what you paste into `docs/v2-design.md` for the human and the agent to come back to. Together they're enough to ship v2 without losing the plot. Good luck.
