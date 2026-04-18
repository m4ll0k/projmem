# Workflow: projmem-driven session

## 1. Session bootstrap

```bash
projmem task resume    # blocked tasks first, then active
projmem notes          # what's remembered? Any contradicted notes?
```

If `contradicted_count > 0`, address contradictions before new work.

## 2. Per-target investigation

```bash
projmem session <file_or_symbol>
```

Returns notes, integrity score, neighbor map, freshness state in one
call. Treat the output as the authoritative starting context — don't
re-investigate what's already verified.

## 3. While editing

```bash
projmem task start "<goal>"
projmem task step  "<what changed>"
projmem task blocked "<open question>"
```

Tasks survive context resets and surface in the next `task resume`.

## 4. Pre-ship gate

```bash
projmem fact-check "<your draft text>"   # exit 2 on REFUTED claim
projmem complete                          # checklist gate, exit 1 on HIGH
```

## 5. Capture

```bash
projmem conclude "The @defined-at(foo, src/a.ts:10) helper is @exported-from(foo, src/a.ts)."
```

Inline `@predicate(subject, object)` becomes a structured claim. The
next session verifies it automatically.
