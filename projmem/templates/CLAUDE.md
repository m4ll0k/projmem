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

## One-shot? Use these instead.

For a quick "is this true?" with no session bookkeeping:

```bash
projmem check "<text>"                       # lean verdict, nothing else
projmem check-line src/foo.ts:10 myFn        # constructs the @defined-at for you
```

Both return only `{verdict, verified, moved, refuted, uncheckable}` — no
claims array, no parse-errors detail. Exit 2 on REFUTED.

## Wrap your work with the gate

End every task with the exit gate so a contradicted note can't ship:

```bash
projmem complete || exit 2
```

`projmem complete` exits 1 on any HIGH finding (drifted file, contradicted
note, dangling ref). The wrapper above propagates that to the calling
shell — CI / git hooks / your wrapper script all see a non-zero rc.

## Session loop

**Start:** `projmem task resume` (blocked tasks first, then active).
If empty, run `projmem notes` for prior conclusions. If
`contradicted_count > 0` in any `repo_memory` block, STOP — a saved
FACT was REFUTED.

**During:** `projmem task start "<goal>"`, then `task step "<what>"`
and `task blocked "<question>"` as you work. Tasks persist across
context resets.

**Before shipping:** `projmem fact-check "<your draft>"`. The four
verdicts you'll see:

| Verdict     | Meaning                                                   | Exit |
|---          |---                                                        |---   |
| `VERIFIED`  | Claim matches the indexed code at the cited location.     | 0    |
| `MOVED`     | Symbol still in the cited file, but at a different line.  | 0    |
| `REFUTED`   | Symbol absent or in a different file. Wrong claim.        | 2    |
| `UNCHECKABLE` | Predicate isn't in the catalog (`projmem predicates`).  | 0    |

`MOVED` carries a `moved_to` line so you can re-cite without
re-investigating; treat as a soft warning, not a refutation.

The summary surfaces every status: `verified`, `moved`, `refuted`,
`uncheckable`. Strict CI gates: check `refuted == 0 AND moved == 0`,
not just `verified == extracted_count`. Top-level `verdict` field
collapses the buckets: `all_verified | has_moved | has_refuted |
has_uncheckable | parse_errors | empty | stale_index`. `parse_errors`
fires when an attempted `@predicate(...)` was botched (no `)` or no
`,`); fix the syntax and re-run.

**Capture:** `projmem conclude "The @defined-at(foo, src/a.ts:10)
helper is @exported-from(foo, src/a.ts)."`  Inline
`@predicate(subject, object)` becomes structured claims. No JSON
file. First cited path → target.

**Capture from prose:** `projmem note add <target> --kind note "<body>"`
auto-extracts FACT claims from the body when it contains either
`@predicate(s, o)` syntax or natural-language patterns like
``X` is defined at file:line` (backticks around the symbol name).
Auto-extracted claims become structured FACT claims that the
verifier can revalidate later — write your findings as
``parseRequest` is defined at src/coyote/parser.java:301` and
projmem fact-checks them automatically. The response includes
`auto_extracted_claims` so you can audit what was inferred. Pass
`--no-auto-extract` to disable.

**End:** `projmem complete` (refresh + gate, exit 1 on HIGH).

## When you don't know what to call

```bash
projmem ask "who uses src/foo.ts?"
projmem ask "what changed since last session?"
projmem ask "safe to delete <name>?"
```

## Blocker signals (halt work, address first)

| Signal | Meaning |
|---|---|
| `contradicted_count > 0` | Saved FACT has been REFUTED |
| `has_refuted` from `fact-check` | Draft has wrong claims |
| `freshness_warning` | File changed on disk since index |
| `drifted_on_disk > 0` in `changes` | Edits newer than the index |
| Open `blocked` task | Previous session had an open question |

## Need more detail

```bash
projmem guide workflow    # full session loop
projmem guide commands    # command catalog
projmem guide capture     # claim authoring
projmem guide signals     # all blocker signals
projmem usage             # one-screen reference
```

JSON mode: pass `--json`. Parse it.
