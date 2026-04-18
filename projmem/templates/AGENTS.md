# projmem — external code memory

This repo has persistent memory at `.projmem/`. It survives session
boundaries, verifies beliefs against current code, and catches wrong
claims before you ship them.

## The three calls that matter most

```bash
projmem task resume      # session start: what was I doing?
projmem fact-check "…"   # pre-ship: are my claims still true?
projmem conclude "…"     # capture: save a one-line conclusion
```

Anything else is optional.

## Session loop

**Start:** `projmem task resume` (blocked tasks first, then active).
If empty, run `projmem notes` to see prior conclusions. If
`contradicted_count > 0` in any `repo_memory` block, STOP and
investigate — a prior FACT was REFUTED.

**During:** `projmem task start "<goal>"`, then `task step "<what>"`
and `task blocked "<question>"` as you work. Tasks persist across
context resets.

**Before shipping:** `projmem fact-check "<your draft>"`. Exit code
2 on any REFUTED claim. Revise using `current_evidence`.

**Capture:** `projmem conclude "The @defined-at(foo, src/a.ts:10)
helper is @exported-from(foo, src/a.ts)."`  Inline `@predicate(subject,
object)` becomes a structured claim. No JSON file needed. First cited
path → target.

**End:** `projmem complete` (refresh + gate, exit 1 on HIGH findings).

## When you don't know what to call

```bash
projmem ask "who uses src/foo.ts?"
projmem ask "what changed since last session?"
projmem ask "safe to delete <name>?"
```

## Blocker signals (halt work, address first)

| Signal | Meaning |
|---|---|
| `contradicted_count > 0` | A saved FACT has been REFUTED |
| `has_refuted` from `fact-check` | Your draft has wrong claims |
| `freshness_warning` | File changed on disk since index |
| `drifted_on_disk > 0` in `changes` | Edits newer than the index |
| Open `blocked` task | Previous session had an unanswered question |

## Need more detail

```bash
projmem guide workflow    # full session loop
projmem guide commands    # command catalog
projmem guide capture     # claim authoring
projmem guide signals     # all blocker signals
projmem usage             # one-screen command reference
```

All read commands return JSON when `--json` is set. Parse it; don't
grep the markdown.

---
*Memory only helps if you write to it. When you learn something
non-trivial, `projmem conclude` it. The next session inherits.*
