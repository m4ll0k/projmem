---
inclusion: always
---

# projmem — external code memory

Persistent memory at `.projmem/`. Verifies beliefs against current
code. Catches REFUTED claims before they ship.

## Before any edit

```bash
projmem session <file_or_symbol>
```

If `repo_memory.contradicted_count > 0`, STOP. A saved FACT was
REFUTED. Investigate with `projmem notes`.

## Three calls that matter most

```bash
projmem task resume      # what was I doing last session?
projmem fact-check "…"   # are my claims still true?
projmem conclude "…"     # save a one-line conclusion
```

## Capture facts inline

```bash
projmem conclude "The @defined-at(foo, src/a.ts:10) helper is @exported-from(foo, src/a.ts)."
```

Inline `@predicate(subject, object)` → structured claim.

## Visual digest

```bash
projmem report            # projmem-out/REPORT.md
projmem graph <target>    # projmem-out/graph.svg
```

JSON output everywhere: `--json`.
