"""projmem/guide.py — on-demand detail docs.

The shortened AGENTS.md/CLAUDE.md only covers the 20% an agent needs at
session start. Deeper detail (full session loop, command catalog,
claim-authoring conventions, all blocker signals) lives here and is
pulled on demand via `projmem guide <topic>`.

Keeps the session-start template small (faster agent bootstrap, less
context consumed) while preserving every piece of detail for the
moments an agent actually needs it.
"""
from __future__ import annotations
from typing import Dict, List


_WORKFLOW = """\
# Session loop — full detail

## Start of a fresh session

1. `projmem task resume`
   - Returns open tasks (blocked first, then active). Each task carries
     progress steps, blockers, files touched, notes saved.
   - If any task is `blocked`, address it before starting new work.
   - If there are no open tasks, run `projmem notes`.

2. `projmem notes` (only if task resume was empty)
   - Totals: total_notes, by_staleness, by_truth_class.
   - `contradicted_count > 0` → STOP. A prior FACT has been REFUTED
     by current code. Inspect before acting.
   - `risk_targets` lists files where saved beliefs are shakiest.

3. `projmem session <file_or_symbol>` (when you know the target)
   - One bundled response: doctor warnings, per-target notes with
     claim verdicts, integrity score, direct + reverse deps,
     freshness warnings, recent agent activity.
   - If `next_steps_hint` is non-empty, do those first.

## During the task

- `projmem task start "<goal>"` — idempotent; returns existing id if
  a task with the same goal is open.
- `projmem task step "<what you did>" [--ref <file>]` — append progress.
- `projmem task blocked "<open question>"` — marks task blocked;
  survives session boundary.
- `projmem task close [--detail ...]` — mark done.

## Before shipping

- `projmem fact-check "<your draft text>"` — extracts claims
  (backticked `X is defined at …`, file:line refs, inline
  `@predicate(subject, object)` patterns), verifies each, exits with
  code 2 if any REFUTED. Revise using the `current_evidence` field.

## Capture

- `projmem conclude "<body>"` — inline `@predicate(subject, object)`
  becomes structured claims automatically. First file path in body →
  target. Supported predicates: defined-at, exported-from,
  env-read-at, flag-read-at, reexported-via, reverse-dependency-of.

## Session end

- `projmem complete` — incremental refresh + checklist (contract
  obligations, dangling refs, broken imports, cross-layer enum
  mismatches). Exit 1 on HIGH findings.
- `projmem conclude-session --transcript <file>` — optional. Mines
  a transcript for VERIFIED claims and saves them as notes.
"""


_COMMANDS = """\
# Full command catalog

## Memory primitives
- `task resume | start | step | blocked | unblock | close | list`
- `notes` — project-wide memory summary
- `session <target>` — per-target bootstrap
- `fact-check "<text>"` — pre-ship claim verifier
- `conclude "<body>"` — one-line save
- `conclude-session --transcript <file>` — transcript extraction
- `seed` — cold-start INFERENCE notes

## Read queries
- `symbol <name> [--context auto]` — defs + refs
- `reverse <file>` — inbound import deps
- `forward <file>` — outbound
- `trace <src> <sink> [--mode strict|relaxed]` — call chain
- `flow <name> --kind env|flag|token|schema_field` — consumer chain
- `contracts <target> [--kind ...]` — contract inventory
- `pack <file_or_symbol|.>` — bounded context; `.` → repo overview
- `ask "<question>"` — NL dispatcher
- `changes` — edit log since last session
- `entrypoints` — declared entry points
- `audit-trail` — command history

## Verification
- `note-verify <target>` — re-verify claims on a target
- `audit <target>` — aggregate verdicts + metadata
- `integrity <target>` — score + factors
- `checklist` — post-edit gate
- `complete` — refresh + checklist (session-end)
- `doctor` — health check

## Memory writes
- `note add <target> <body> [--claims <file>]`
- `note delete <id>`
- `note list [--target ...] [--kind ...]`
- `refute add <target> <body> [--evidence ...]`
- `snapshot <label> [--symbols]`

## Index management
- `init [claude|codex|cursor|auto]` — setup
- `index [--include GLOB] [--force]` — full/incremental
- `refresh [--reindex]` — drift detection
- `stats` — coverage numbers

All read commands support `--json` for structured output.
"""


_CAPTURE = """\
# Claim authoring — how to make your beliefs durable

## Fastest: inline `@predicate(subject, object)` in the body

```bash
projmem conclude "The @defined-at(withContext, src/server/http/withContext.ts:10) gateway is @exported-from(withContext, src/server/http/withContext.ts)."
```

No JSON file. First cited file path → target. Subject and object are
comma-separated inside the parentheses. Backticks around the subject
are optional but readable.

## Supported predicates

| Predicate | Object shape | Verifies |
|---|---|---|
| `defined-at` | `file:line` | Symbol def exists at that exact line |
| `exported-from` | `file` | Symbol is exported from that file |
| `env-read-at` | `file:line` | Env var read exists at that site |
| `flag-read-at` | `file:line` | Flag read exists at that site |
| `reexported-via` | `file` | Barrel file re-exports the leaf |
| `reverse-dependency-of` | `file` | Subject file is imported by object |

Unknown predicates are ACCEPTED (stored as-is) but return UNCHECKABLE
on verify.

## When inline isn't enough

```bash
cat > /tmp/c.json <<'EOF'
[{"subject":"<NAME>", "predicate":"env-read-at",
  "object":"<file>:<line>", "truth_class":"FACT"}]
EOF
projmem note add <target> "short body" \\
  --kind note --claims /tmp/c.json --truth-class FACT
```

## Truth-class taxonomy

| Class | Meaning | Decay |
|---|---|---|
| FACT | Code-grounded; defines-this-exists-here | Aggressive — REFUTED becomes `contradicted` |
| INFERENCE | Reasoned from graph shape | Moderate |
| ASSUMPTION | Best-guess | Permissive |
| UNKNOWN | Author doesn't know | Never flipped |

`projmem conclude` defaults to FACT (caller is asserting).
`projmem seed` uses INFERENCE (heuristic). Hand-authored notes
default to INFERENCE unless `--truth-class` is passed.
"""


_SIGNALS = """\
# All blocker signals

## In `repo_memory` header (every command)

| Signal | Trigger | Action |
|---|---|---|
| `contradicted_count > 0` | FACT claim REFUTED on current code | `projmem notes`; inspect contradiction |
| `has_memory: false` | No notes yet | `projmem seed` or `projmem conclude` |

## In `fact-check` output

| Signal | Trigger | Action |
|---|---|---|
| `verdict: has_refuted` | Draft contains wrong claim | Revise using `current_evidence` |
| `verdict: has_uncheckable` | Some claims ambiguous | Decide case-by-case |

## In `session <target>` output

| Signal | Trigger | Action |
|---|---|---|
| `freshness_warning` | Target file changed since index | Run `projmem index` |
| `ambiguity_warning` | Multiple defs share name | Pin with `file#name` |
| `index_scope_warning` | Target has zero neighbors | Re-run `projmem init --reindex` without narrow --include |
| `claim_overall_status: contradicted` | Structural claim failed | Re-investigate |

## In `changes` output

| Signal | Trigger | Action |
|---|---|---|
| `drifted_on_disk > 0` | Edits newer than index | Run `projmem index` first |
| `was_deleted: true` | Tracked file removed | Verify no dangling refs |

## In `task resume` output

| Signal | Trigger | Action |
|---|---|---|
| open `blocked` task | Previous session had an open question | Address blocker or `task unblock` |

## In `doctor` output

| Signal | Trigger | Action |
|---|---|---|
| `low_internal_ref_binding` | Indexed refs can't resolve | Check parser coverage |
| `stale_files` | Files changed since index | Refresh |
"""


_TOPICS: Dict[str, str] = {
    "workflow":   _WORKFLOW,
    "commands":   _COMMANDS,
    "capture":    _CAPTURE,
    "signals":    _SIGNALS,
}


def guide(topic: str = "") -> Dict[str, object]:
    """Return the requested guide topic, or the list of available
    topics when no topic is given."""
    topic = (topic or "").strip().lower()
    if not topic or topic in ("help", "list", "topics"):
        return {
            "topics": sorted(_TOPICS),
            "hint": ("Pass a topic: projmem guide workflow | commands | "
                     "capture | signals. For a one-screen quick "
                     "reference use `projmem usage` instead."),
        }
    if topic not in _TOPICS:
        # Round-5-r2 F009: standardized envelope shape — kebab tag in
        # `error`, sentence in `message`, the catalog under
        # `available_options`, plus an actionable `hint`.
        return {
            "error":   "unknown-topic",
            "message": f"unknown guide topic {topic!r}",
            "available_options": sorted(_TOPICS),
            "hint": ("Pass one of `available_options`. Example: "
                      "`projmem guide workflow`."),
        }
    return {"topic": topic, "body": _TOPICS[topic]}
