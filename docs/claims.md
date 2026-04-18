# Claim-level note verification

This is the load-bearing feature that makes `projmem` different from a
code search tool. A **claim** is a structured belief about the code that
the indexer can prove or refute.

## The shape of a claim

```json
{
  "subject":     "TSC_WATCHFILE",
  "predicate":   "env-read-at",
  "object":      "src/compiler/sys.ts:1516",
  "truth_class": "FACT",
  "confidence":  0.9,
  "note":        "optional free-text rationale"
}
```

- **subject** — the identifier being claimed about (env name, symbol name, file).
- **predicate** — the relation (see supported predicates below).
- **object** — the concrete location, usually `<file>:<line>` or a file path.
- **truth_class** — `FACT` (immutable belief) / `INFERENCE` (derived) /
  `ASSUMPTION` (unproven) / `UNKNOWN`. FACT refutations escalate note
  staleness to `contradicted`.
- **confidence** — 0.0 – 1.0. Informs aggregate confidence scoring.
- **note** — free-text rationale, optional. Shown in `note-verify` output.

Claims live inside the annotation's `evidence` JSON array alongside legacy
`{file, line, note}` entries — backward compatible by shape.

## Supported predicates

### Symbol identity

| Predicate | subject | object | Verifies |
|---|---|---|---|
| `defined-at` | symbol name | `file:line` | `symbols` table has a matching row |
| `exported-from` | symbol name | `file` | symbol present in file AND `exported` column set |

### Contracts

| Predicate | subject | object | Verifies |
|---|---|---|---|
| `env-read-at` | env var | `file:line` | `contracts` row with `kind='env'` at site |
| `flag-read-at` | flag name | `file:line` | `contracts` row with `kind='flag'` at site |

### Structural consumers

| Predicate | subject | object | Verifies |
|---|---|---|---|
| `reexported-via` | leaf file | barrel file | `edges` row `type='reexport_star'` from barrel → leaf |
| `reverse-dependency-of` | importee file | importer file | `edges` row `type='imports'` |

## Verdict statuses

- **VERIFIED** — current code supports the claim exactly as written.
- **REFUTED** — current code contradicts the claim. `current_evidence`
  shows what's there now (e.g. the env var at the claimed line is now
  a DIFFERENT name — the classic rename case).
- **UNCHECKABLE** — the claim's input is malformed, the predicate is
  unsupported, or disambiguation would require guessing. Never treated
  as VERIFIED.

## Aggregate note status

When a note carries multiple claims, `note-verify` rolls them up:

| Aggregate | When |
|---|---|
| `fresh` | every claim VERIFIED |
| `weakly_stale` | VERIFIED + UNCHECKABLE mix, no REFUTED |
| `strongly_stale` | at least one REFUTED claim (non-FACT) |
| `contradicted` | at least one REFUTED claim with `truth_class: FACT` |
| `unknown` | all UNCHECKABLE |

The aggregate overrides the fingerprint-based label so the consumer sees
the more specific answer.

## Adding claims

### From a JSON file

```bash
cat > claims.json <<'EOF'
[
  {"subject": "TSC_WATCHFILE", "predicate": "env-read-at",
   "object": "src/compiler/sys.ts:1516", "truth_class": "FACT"}
]
EOF

projmem note add src/compiler/sys.ts --kind note \
  "Watch-mode env vars" --claims claims.json
```

### Inline JSON

```bash
projmem note add src/compiler/sys.ts --kind note "Watch-mode env vars" \
  --claims '[{"subject":"TSC_WATCHFILE","predicate":"env-read-at",
              "object":"src/compiler/sys.ts:1516"}]'
```

### Reading the output

```bash
projmem note-verify src/compiler/sys.ts
projmem audit       src/compiler/sys.ts
```

`audit` groups refuted claims by subject and lists contradicted note IDs
— useful when triaging "what broke in this file?"

## Failure policy

- Ambiguous inputs (malformed `object`, unknown predicate, symbol that
  exists at multiple locations with no way to pick) return UNCHECKABLE
  with a reason string. **Never silently VERIFIED.**
- Unknown predicates produce UNCHECKABLE + the list of supported
  predicates. No crashes.
- Legacy notes without any claims fall through to fingerprint-based
  verification unchanged — no behavior regression.
- Claim verification side-effects nothing: it never injects refs, never
  mutates the DB outside the annotations' staleness+confidence columns.

## Extending the predicate set

Adding a new predicate means:

1. Write a verifier function in `projmem/claims.py` that takes
   `(store, claim) -> ClaimVerdict`. Conservative on ambiguity.
2. Add it to `PREDICATES` dict.
3. Add a regression test in `tests/test_claims.py` covering VERIFIED,
   REFUTED (with `current_evidence`), and UNCHECKABLE paths.
4. Document it here.

The test bar is VERY high for verifiers: a verifier that returns VERIFIED
for an ambiguous input is worse than a verifier that didn't exist, because
it inflates trust in the whole system.
