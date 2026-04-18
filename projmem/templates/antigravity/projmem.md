# projmem — external code memory

Persistent memory at `.projmem/`. Survives session boundaries. Verifies
beliefs against current code. Catches REFUTED claims before they ship.

## Always

Before editing a file or symbol:

```bash
projmem session <target>
```

If `repo_memory.contradicted_count > 0` → STOP and inspect with
`projmem notes`. A saved FACT has been REFUTED.

## Capture conclusions

```bash
projmem conclude "The @defined-at(foo, src/a.ts:10) helper is @exported-from(foo, src/a.ts)."
```

Inline `@predicate(subject, object)` → structured claim. Verified
automatically next session.

## Pre-ship gate

```bash
projmem fact-check "<draft>"   # exit 2 on REFUTED claim
projmem complete                # refresh + checklist gate
```

## Visual digest

```bash
projmem report            # projmem-out/REPORT.md
projmem graph <target>    # SVG/DOT/Mermaid at projmem-out/
```

All commands accept `--json`.
