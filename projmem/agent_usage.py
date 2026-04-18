"""projmem/agent_usage.py — LLM-facing self-documentation.

Purpose: any LLM (Claude, GPT, Llama, Mistral, anything that can shell
out) can run `projmem usage` to learn what the tool does and how to
chain commands. The output is intentionally:

  - markdown (consumable directly into a prompt)
  - bounded (~3 KB, no risk of blowing context)
  - imperative (tells the agent the workflow, not just the surface)
  - example-driven (exact commands, not abstract descriptions)

Use case: a user can drop "If you're working in this repo, first run
`projmem usage`" into their CLAUDE.md / AGENTS.md / .cursorrules. The
LLM then learns the tool by calling it once.

`--json` returns a structured form for harnesses that want to parse the
command catalog programmatically.
"""
from __future__ import annotations
from typing import Any, Dict, List


# Ordered, agent-prioritized command catalog. Each entry:
#   id     — the CLI verb
#   when   — when the agent should reach for it
#   call   — exact invocation template
#   reads  — what the agent learns from the output
COMMANDS: List[Dict[str, str]] = [
    {
        "id":    "session",
        "when":  "FIRST CALL on any task. Bootstrap context for a target.",
        "call":  "projmem session <file_or_symbol>",
        "reads": "Existing notes + their claim verdicts, integrity score, "
                 "doctor warnings, recent agent activity on this target.",
    },
    {
        "id":    "doctor",
        "when":  "First time you see a repo, or whenever results look off.",
        "call":  "projmem doctor",
        "reads": "Health issues: tree-sitter missing, foreign index, "
                 "stale files, low ref binding, artifact bleed. Severity-tagged.",
    },
    {
        "id":    "audit",
        "when":  "Any time you want to re-check what's known about a file/symbol.",
        "call":  "projmem audit <file_or_symbol>",
        "reads": "Per-claim VERIFIED/REFUTED/UNCHECKABLE status across all "
                 "notes on the target. Refuted claims include `current_evidence`.",
    },
    {
        "id":    "symbol",
        "when":  "You need defs + refs of a name. Default behavior is "
                 "source-only refs; pass --include-artifacts for full set.",
        "call":  "projmem --json symbol <name> [--context auto] [--strict-ambiguity]",
        "reads": "defs, refs, ref_count, ambiguity_warning (with primary_guess), "
                 "freshness_warning (if any indexed file drifted on disk), "
                 "artifact_refs (separate bucket).",
    },
    {
        "id":    "reverse",
        "when":  "Blast-radius for a file: who imports / depends on it.",
        "call":  "projmem reverse <file>",
        "reads": "reverse_dependencies (source-only by default), "
                 "direct_dependencies, artifact_reverse_dependencies (separate).",
    },
    {
        "id":    "trace",
        "when":  "EXPERIMENTAL. Call-chain investigation only — same-name "
                 "collisions are over-approximated. Prefer `reverse` / "
                 "`analyze-change` / `pack` for trustworthy answers.",
        "call":  "projmem trace <source> <sink> [--mode strict|relaxed]",
        "reads": "Path of hops with edge_type per hop. Output carries "
                 "`experimental: true` and a `caveat` string.",
    },
    {
        "id":    "flow",
        "when":  "Trace a contract (env var, flag) through its consumers.",
        "call":  "projmem flow <name> [--kind env|flag]",
        "reads": "Read sites → local aliases → switch/conditional/read consumers, "
                 "including cross-file consumers via importer scan. "
                 "Each consumer carries `confidence` + `ast_confirmed`.",
    },
    {
        "id":    "pack",
        "when":  "Build a context pack for triage. Heaviest read; budget for it.",
        "call":  "projmem pack <file_or_symbol> [--include-source] [--snippets]",
        "reads": "Bounded context pack with notes (claim verdicts), "
                 "deps, semantic contracts, tests, target_integrity score.",
    },
    {
        "id":    "contracts",
        "when":  "Find env vars / flags / schema fields by name or in a file.",
        "call":  "projmem contracts <name_or_file> [--kind env|flag|schema_field]",
        "reads": "Per-occurrence file:line, role (read/write/declare), confidence.",
    },
    {
        "id":    "note add --claims",
        "when":  "AFTER you conclude something non-trivial. Save it as "
                 "structured claims so future sessions can verify it.",
        "call":  "projmem note add <target> --kind note 'short body' "
                 "--claims <path-to-json> --truth-class FACT",
        "reads": "Returns id + claim_count. Claims are subject/predicate/object "
                 "triples; supported predicates: defined-at, exported-from, "
                 "env-read-at, flag-read-at, reexported-via, reverse-dependency-of.",
    },
    {
        "id":    "note-verify",
        "when":  "Quick re-check of a single target's notes. `audit` is broader.",
        "call":  "projmem note-verify <target>",
        "reads": "Per-note transitions (previous → now), per-claim status, "
                 "drifted_fields, claim_overall_status.",
    },
    {
        "id":    "checklist",
        "when":  "BEFORE claiming you're done editing. Catches forgotten "
                 "updates: open contract obligations, dangling refs, "
                 "broken repo-relative imports.",
        "call":  "projmem checklist",
        "reads": "Items requiring action with severity tags. Empty list "
                 "means the post-edit gate is clean.",
    },
    {
        "id":    "complete",
        "when":  "LAST CALL on every task. Combines incremental refresh "
                 "(picks up modified+added+deleted files) with the "
                 "checklist gate. Run instead of `index --reindex` after "
                 "each task — far cheaper than a full rebuild.",
        "call":  "projmem complete",
        "reads": "refresh.{modified,added,deleted,applied} + checklist "
                 "report + next_steps_hint. Exit 1 if any HIGH finding.",
    },
]


WORKFLOW_TEXT = """\
# Recommended workflow for an agent

**Step 0 — Every projmem output carries a `repo_memory` block.** Check
it on every call. Fields: `has_memory`, `total_notes`,
`contradicted_count`, `recent_activity_7d`, `discover`, `hint`.

- `contradicted_count > 0` → STOP. Run `projmem notes` to see which
  prior FACT claims were refuted. Don't re-investigate without reading
  the refutations first.
- `total_notes > 0` and you haven't called `projmem notes` yet this
  session → do so. Memory exists; use it.
- `has_memory == false` → you're on a fresh repo. Build memory as you go
  via `projmem note add --claims`.

The header is UNAVOIDABLE — it's in every read response so agents can't
forget memory exists. This replaces the brittle "remember to call
note-list" pattern.

**Step 1 — ALWAYS start every task with `projmem session <target>`.**
This single call returns: actionable doctor warnings (no separate
doctor call needed), every prior note on the target with per-claim
verdicts (VERIFIED / REFUTED / UNCHECKABLE), integrity score, recent
agent activity, dependency neighbors, freshness warnings, and a
`next_steps_hint`. If `next_steps_hint` is non-empty, do those steps
before anything else.

**Step 2 — Treat these signals as BLOCKERS in any output:**

- `freshness_warning` → a touched file changed on disk after the
  index was built. Run `projmem index --include <file>` (targeted)
  or `projmem index --reindex` (full) before trusting other reads.
- `ambiguity_warning` → multiple defs share the name. Use
  `primary_guess` or pin with `projmem symbol <file>#<name>`.
- `claim_overall_status: contradicted` → a FACT claim was REFUTED.
  The prior conclusion is no longer true. Re-investigate using
  `current_evidence` from the refuted claim.

**Step 3 — Investigate using the read commands.** Pick the narrowest
command that answers your question (see "Commands" table above). All
read commands return JSON when `--json` is set; parse the structured
output, don't grep markdown.

**Step 4 — Save what you concluded.** When you discover something
non-trivial about the code — that `X is read at file:Y`, that
`function F is defined at G:H`, that `barrel A re-exports leaf B` —
save it as a structured claim so the NEXT session sees it verified
or refuted automatically:

    cat > /tmp/c.json <<'EOF'
    [{"subject":"...", "predicate":"env-read-at",
      "object":"file.ts:123", "truth_class":"FACT"}]
    EOF
    projmem note add <target> --kind note "short body" \\
      --claims /tmp/c.json --truth-class FACT

**Step 5 — End every task with `projmem complete`.**
ONE call that does an incremental refresh (picks up files you added,
modified, or deleted during the task) AND runs the checklist gate
(catches forgotten contract obligations, dangling refs, broken
repo-relative imports). Far cheaper than a full `projmem index`
rebuild — typically seconds, not minutes. Exit code 1 if any HIGH
finding, so CI / wrapper scripts can gate on it.

# Operational tips

- **Keep the index scope wide enough.** If `projmem session` shows
  `reverse_dep_count: 0` AND `direct_dep_count: 0` on a non-trivial
  file, your index probably excluded too much (e.g. you ran
  `projmem index --include` with a narrow glob and missed neighbor
  dirs). Re-run `projmem init --reindex` or `projmem index` without
  the include filter.

- **`projmem complete` after every task; `projmem init --reindex`
  rarely.** `complete` is incremental (re-parses only the files you
  changed) and finishes in seconds. `init --reindex` is a full rebuild
  and can take minutes on large repos. Reach for the full rebuild only
  after schema changes, an upgrade, or when you suspect index
  corruption.

- **`projmem doctor` is a tier-2 health check.** You normally don't
  need it; `session` already includes its actionable findings. Run
  it directly only when results look off or when migrating to a new
  machine.

# What this is NOT

- Not a grep replacement. `rg` is still right for one-shot text search.
- Not a refactoring tool. It tells you what's true; you make changes.
- Not a vulnerability scanner.
"""


def render_markdown() -> str:
    """Compact markdown the LLM can ingest directly."""
    out: List[str] = []
    out.append("# projmem — usage for AI agents\n")
    out.append("> grep tells you what is in the code.")
    out.append("> projmem tells you whether what you believed about the "
                "code is still true.\n")
    # Lead with the entry point — no ambiguity about what to call first.
    out.append("**Always start with:**\n")
    out.append("```")
    out.append("projmem session <file_or_symbol>")
    out.append("```\n")
    out.append("Returns one bounded JSON blob with everything you need: "
                "doctor warnings, prior notes with per-claim verdicts, "
                "integrity score, recent activity, dep neighbors, and a "
                "`next_steps_hint`. Honor `next_steps_hint` before doing "
                "anything else.\n")
    # Workflow comes BEFORE the catalog — the agent reads the workflow
    # top-to-bottom and pulls commands from the catalog as needed.
    out.append(WORKFLOW_TEXT)
    out.append("# Commands (agent-priority order)\n")
    out.append("| When | Call |")
    out.append("|---|---|")
    for c in COMMANDS:
        # Markdown table — escape `|` inside cells.
        when = c["when"].replace("|", "\\|")
        call = "`" + c["call"].replace("|", "\\|") + "`"
        out.append(f"| {when} | {call} |")
    out.append("")
    out.append("## What each command tells you\n")
    for c in COMMANDS:
        out.append(f"### `{c['id']}`")
        out.append(c["reads"])
        out.append("")
    return "\n".join(out)


def _all_verbs() -> List[Dict[str, Any]]:
    """Pull the live argparse subcommand catalog. The hand-curated
    `COMMANDS` list above ranks the agent-priority handful; `_all_verbs`
    enumerates the COMPLETE set so `usage --json` doesn't lie about
    coverage. Each entry: id, help (one-liner), curated (bool).
    """
    try:
        from . import cli as _cli  # local import — cli imports us
        parser = _cli.build_parser()
    except Exception:
        return []
    # Find the subparsers action by class name (works whether the
    # subparsers live on the root or a nested group).
    sub_action = None
    for a in parser._actions:
        if a.__class__.__name__ == "_SubParsersAction":
            sub_action = a
            break
    if sub_action is None or not getattr(sub_action, "choices", None):
        return []
    curated_ids = {c["id"] for c in COMMANDS}
    out: List[Dict[str, Any]] = []
    for verb, sub in sorted(sub_action.choices.items()):
        try:
            usage_line = sub.format_usage().strip().splitlines()[0]
        except Exception:
            usage_line = ""
        out.append({
            "id":      verb,
            "help":    usage_line,
            "curated": verb in curated_ids,
        })
    return out


def render_json() -> Dict[str, Any]:
    """Structured form for harnesses / agent SDKs.

    Round-5 P3: previously this returned only the curated 13-verb list,
    which made `usage --json` look like the entire CLI surface was 13
    commands when it's actually 60+. Now it returns BOTH the curated
    list (priority order, with intent + reads) AND the full subcommand
    enumeration under `all_verbs` so harnesses can discover everything.
    """
    full = _all_verbs()
    return {
        "tool":     "projmem",
        "thesis":   "drift-aware code memory for AI agents",
        "entry_point": "projmem session <file_or_symbol>",
        "commands": COMMANDS,
        "all_verbs": full,
        "command_counts": {
            "curated": len(COMMANDS),
            "total":   len(full),
        },
        "workflow": [
            "0. Every projmem read returns a `repo_memory` block "
            "(total_notes, contradicted_count, hint). Check it EVERY "
            "call — this is the unavoidable memory-presence signal.",
            "1. ALWAYS start with `projmem session <target>` — single "
            "call, includes doctor warnings + notes + integrity + "
            "neighbors + next_steps_hint.",
            "2. Honor `next_steps_hint` from session before anything else.",
            "3. Treat freshness_warning, ambiguity_warning, "
            "claim_overall_status=contradicted, AND "
            "repo_memory.contradicted_count>0 as BLOCKERS.",
            "4. Use the narrowest read command from `commands` to "
            "answer your question. Pass `--json` on all reads.",
            "5. Save concluded facts as structured claims via "
            "`note add --claims --truth-class FACT` so the next session "
            "verifies them automatically.",
            "6. Before claiming done, run `projmem complete` (end-of-task "
            "primitive: incremental refresh + checklist gate).",
        ],
        "blockers": [
            "freshness_warning",
            "ambiguity_warning",
            "claim_overall_status=contradicted",
            "repo_memory.contradicted_count > 0",
        ],
        "predicates_supported": [
            "defined-at", "exported-from",
            "env-read-at", "flag-read-at",
            "reexported-via", "reverse-dependency-of",
        ],
        "operational_tips": [
            "If session shows reverse_dep_count=0 AND direct_dep_count=0 "
            "on a non-trivial file, the index scope is probably too narrow "
            "(e.g. `projmem index --include` filtered out neighbor dirs). "
            "Re-run `projmem init --reindex` without the include filter.",
            "`projmem doctor` is a tier-2 health check; session already "
            "includes its actionable findings.",
        ],
    }
