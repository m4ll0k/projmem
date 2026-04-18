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

## Before any edit

Call `projmem session <file_or_symbol>`. If
`repo_memory.contradicted_count > 0`, STOP — a saved FACT has been
REFUTED. Inspect with `projmem notes` before editing.

## Session loop

**Start:** `projmem task resume` (blocked tasks first, then active).

**During:** `projmem task start "<goal>"`, `task step "<what>"`,
`task blocked "<question>"` — these survive context resets.

**Before shipping:** `projmem fact-check "<draft>"`. Exit code 2 on
any REFUTED claim. Revise using `current_evidence`.

**Capture:** `projmem conclude "The @defined-at(foo, src/a.ts:10)
helper is @exported-from(foo, src/a.ts)."`  Inline `@predicate(subject,
object)` becomes a structured claim. No JSON file. First cited path
→ target.

**End:** `projmem complete` (refresh + gate, exit 1 on HIGH).

## Visual artifacts

```bash
projmem report            # one-page digest at projmem-out/REPORT.md
projmem graph <target>    # SVG/DOT/Mermaid at projmem-out/
```

## Blocker signals

| Signal | Meaning |
|---|---|
| `contradicted_count > 0` | Saved FACT has been REFUTED |
| `has_refuted` from `fact-check` | Draft has wrong claims |
| `freshness_warning` | File changed on disk since index |
| `drifted_on_disk > 0` in `changes` | Edits newer than the index |

JSON mode: pass `--json`. Parse it.
