# projmem — drift-aware code memory for Copilot Chat

This repository ships persistent code memory at `.projmem/`. It
verifies claims against the current code and surfaces REFUTED beliefs
before they ship. Always consult it before non-trivial edits.

## Before editing a file

Run `projmem session <file_or_symbol>` (the agent has shell tool
access). If `repo_memory.contradicted_count > 0`, STOP — a saved FACT
has been REFUTED. Inspect with `projmem notes`.

## Three calls that matter most

```bash
projmem task resume      # session start: what was I doing?
projmem fact-check "..." # pre-ship: are my claims still true?
projmem conclude "..."   # capture: save a one-line conclusion
```

## Capture facts inline

```bash
projmem conclude "The @defined-at(foo, src/a.ts:10) helper is @exported-from(foo, src/a.ts)."
```

`@predicate(subject, object)` → structured claim verified next session.

## Visual digest

```bash
projmem report            # one-page projmem-out/REPORT.md
projmem graph <target>    # SVG/DOT/Mermaid at projmem-out/
```

All commands accept `--json`. Parse the JSON.
