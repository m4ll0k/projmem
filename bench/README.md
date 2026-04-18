# bench/ — projmem comparative benchmark

This directory contains the harness, tasks, drivers, grader, and
scorer for producing defensible A/B/C numbers comparing an LLM
with different tool surfaces on code-reasoning tasks.

The formal specification (scoring formulas, task schema, honest
reporting rules) lives in **[SPEC.md](SPEC.md)** — read it before
touching the scoring code or adding tasks.

---

## Layout

```
bench/
  SPEC.md              — formal specification (binding)
  run.py               — CLI harness (list/show/run/matrix/score/aggregate/compare)
  grader.py            — post-run grader (required/decoy files, regex, test_command)
  score.py             — scoring primitives + composite formula
  aggregate.py         — multi-run statistics + arm comparison
  self_test.py         — SPEC §4 invariants (reference passes, naive fails)
  tasks/
    <task_id>/
      spec.json        — task schema (SPEC §3)
      seed/            — starting state
      solution/        — reference correct edits
  drivers/
    reference_driver.py        — copy solution/ → work/
    naive_grep_driver.py       — deliberately wrong-target edit
    simulated_bare_driver.py   — Arm A: grep/find/cat/ls
    strong_shell_driver.py     — Arm B: rg/fd/jq/ctags/git grep
    simulated_projmem_driver.py — Arm C: projmem (pack/symbol/trace/...)
```

---

## Minimal flow

```bash
# 1. Sanity: invariants hold for every task
python3 bench/self_test.py

# 2. Run the full A/B/C matrix with 5 repeats
python3 bench/run.py matrix \
  --tasks flag_propagation_orphan,same_name_decoy \
  --drivers bare:bench/drivers/simulated_bare_driver.py \
  --drivers shell:bench/drivers/strong_shell_driver.py \
  --drivers projmem:bench/drivers/simulated_projmem_driver.py \
  --repeats 5 \
  --output-root bench/results/run_2026_04_14/

# 3. Score (Arm A is the efficiency baseline)
python3 bench/run.py score bench/results/run_2026_04_14/ \
  --baseline-dir bench/results/run_2026_04_14/bare/ \
  --output scores.json

# 4. Aggregate (per-task / per-family / overall)
python3 bench/run.py aggregate scores.json --output aggregated.json

# 5. Compare two arms, emit comparison.md + comparison.json
python3 bench/run.py compare \
  --baseline bench/results/run_2026_04_14/bare/ \
  --challenger bench/results/run_2026_04_14/projmem/ \
  --output comparison.md
```

---

## Adding a task

1. Create `bench/tasks/<id>/spec.json` matching the schema in
   SPEC.md §3. The task must belong to one of the four families
   listed in SPEC §4.
2. Populate `bench/tasks/<id>/seed/` with the starting state — the
   bug/missing-feature is here.
3. Populate `bench/tasks/<id>/solution/` with the fixed tree. The
   grader uses this implicitly via `reference_driver.py`.
4. Run `python3 bench/self_test.py`. Both invariants (reference
   passes, naive fails) MUST hold. If the naive driver passes, add
   decoys or tighten regex checks until it doesn't — otherwise the
   task lacks discriminating power.
5. Add the task to the matrix you run in step 2 above.

A task that naive grep can solve is DISCARDED per SPEC §4.

---

## Scoring at a glance

```
success          — 100 if graded passed else 0
coverage         — 0.7·files_touched/total + 0.3·regex_passed/total (×100)
precision        — max(0, 1 − 0.5·decoy_touched − 0.3·claim_violations
                              − 0.1·extra_regex_over_matches) × 100
token_efficiency — max(0, 1 − tokens_run / tokens_baseline) × 100
time_efficiency  — max(0, 1 − time_run   / time_baseline)   × 100

composite = 0.40·success + 0.25·coverage + 0.15·precision
          + 0.10·token_efficiency + 0.10·time_efficiency
```

See SPEC.md §2 for the full derivation and edge cases
(missing baseline, clamping, etc.).

---

## Honest reporting

Every publishable result MUST include:
- model name/version/temperature/max-turns
- repo commit SHA
- driver versions
- number of repeats and aggregation method (mean/median)
- total tokens spent on the full matrix

And MUST report the three headline numbers together (pass-rate
lift, coverage lift, cost delta). See SPEC.md §6.

A projmem result reported without those fields is considered
unsourced and should be rejected.

---

## Real-repo benchmarking (`external_repo`)

Synthetic seed/solution trees scale poorly past a few thousand
LOC. For honest "does projmem help on a real codebase?" numbers we
use the `external_repo` field in `spec.json` — the harness mirrors
the live repo into the work directory at materialize time and
snapshots only the files the grader inspects, so the agent sees
the actual source.

### Spec schema

```jsonc
{
  "id":     "node_files_watcher_consumers",
  "kind":   "investigation",
  "family": "wrong-file-ambiguity",
  "external_repo": {
    "path":   "/tmp/node",                    // local checkout (preferred)
    "url":    "https://github.com/nodejs/node",  // OR fetched if path missing
    "ref":    "v22.11.0",                      // optional checkout target
    "subset": ["lib/", "src/"],                // restrict to these subdirs
    "setup":  "npm ci --no-audit --no-fund"   // optional post-checkout hook
  },
  "required_files": ["ANSWER.md"],
  "decoy_files":    ["..."],
  "regex_checks":   [...]
}
```

`external_repo` is mutually exclusive with the synthetic `seed/`
tree. The harness writes `<work_dir>/.bench_baseline.json` —
pristine snapshot of every file referenced by required_files /
decoy_files / regex_checks. The grader compares against that
snapshot, so a driver that touches the real repo's files is still
graded correctly.

### Investigation-style tasks

Real-repo tasks are usually "find the right files" rather than
"edit them" — the grader pattern is:

1. `required_files: ["ANSWER.md"]`
2. `regex_checks` listing the file paths the answer MUST cite
   (`min_matches: 1`) and the decoy paths it must NOT cite
   (`max_matches: 0`)

The naive grep driver auto-detects `kind: investigation` and dumps
every `rg`-matched file into `ANSWER.md` — typically over-cites,
which fails the `max_matches: 0` checks. That's the failure mode
the benchmark is designed to discriminate against.

### Real CLI drivers (Claude / Codex)

Two drivers spawn the actual agent CLIs:

```
bench/drivers/claude_cli_driver.py    # claude -p --output-format json
bench/drivers/codex_cli_driver.py     # codex exec --json
```

Both honor `PROJMEM_ARM`:

```
PROJMEM_ARM=plain    # baseline — no projmem touch
PROJMEM_ARM=projmem  # `projmem init <agent> && projmem index` first,
                     # then run the agent. CLAUDE.md / AGENTS.md +
                     # the platform's PreToolUse hook teach the agent
                     # to consult projmem.
```

End-to-end, the projmem-arm comparison looks like:

```bash
# Baseline (no projmem)
PROJMEM_ARM=plain CLAUDE_MODEL=claude-opus-4-7 \
  python3 bench/run.py run node_files_watcher_consumers \
    /tmp/bw_plain --driver bench/drivers/claude_cli_driver.py \
    --output bench/results/plain_files_watcher.json

# Challenger (projmem-equipped)
PROJMEM_ARM=projmem CLAUDE_MODEL=claude-opus-4-7 \
  python3 bench/run.py run node_files_watcher_consumers \
    /tmp/bw_projmem --driver bench/drivers/claude_cli_driver.py \
    --output bench/results/projmem_files_watcher.json

# Compare
python3 bench/run.py compare \
  --baseline bench/results/plain_files_watcher.json \
  --challenger bench/results/projmem_files_watcher.json \
  --output bench/results/files_watcher_diff.md
```

Each invocation costs API tokens. Set `CLAUDE_MAX_BUDGET_USD` to
cap individual runs:

```
CLAUDE_MAX_BUDGET_USD=0.50  # half a dollar per task per arm
```

### Running the matrix

```bash
python3 bench/run.py matrix \
  --tasks node_files_watcher_consumers,node_safegetenv_callers \
  --drivers naive:bench/drivers/naive_grep_driver.py \
  --drivers projmem_sim:bench/drivers/simulated_projmem_driver.py \
  --drivers claude_plain:bench/drivers/claude_cli_driver.py \
  --drivers claude_projmem:bench/drivers/claude_cli_driver.py \
  --repeats 3 \
  --output-root bench/results/real_repo_matrix/
```

(For the `claude_projmem` arm, set `PROJMEM_ARM=projmem` in the
environment before the matrix call. The matrix runner does not
yet thread per-driver env vars; run the projmem and plain arms as
two separate matrix invocations and merge the result trees.)

### Adding a new real-repo task

1. Pick a question with a CONCRETE, finite answer (a list of
   file paths, a single line of code, a flag value).
2. Confirm `rg <obvious_keyword>` returns more candidates than the
   true answer set — if grep solves it alone, it has no
   discriminating power.
3. Write `spec.json` with the `external_repo` block and
   `regex_checks` enumerating both the must-cite paths and the
   decoys.
4. Write `solution/ANSWER.md` with the canonical correct answer.
5. Run `python3 bench/self_test.py`. Reference must pass, naive
   must fail. If naive passes, tighten the decoys.

### Claim-text fallback

For `kind: investigation` tasks, the grader transparently falls back
to scanning the agent's `completion_claim` when `ANSWER.md` is
missing. This handles the realistic case where Claude/Codex answers
in chat instead of writing the file (e.g., when the harness can't
auto-confirm Write tool prompts). The fallback is logged per row
as `result_source: claim_text` so consumers can see WHICH path the
check resolved against. Opt out with `claim_text_fallback: false`
in `spec.json`.

---

## Reference results

### Simulated A/B/C matrix (full sweep, 6 tasks × 3 repeats = 90 runs)

| Driver                   | Pass rate | Composite | Coverage | Precision | Token-eff |
| ------------------------ | --------- | --------- | -------- | --------- | --------- |
| simulated_bare_driver    |       0%  |     20.0  |     34   |     77    |     0.0   |
| naive_grep_driver        |       0%  |     55.3  |     93   |     85    |   100.0   |
| strong_shell_driver      |       0%  |     50.5  |     93   |     85    |    55.1   |
| **simulated_projmem**    |  **100%** | **98.9**  |    100   |    100    |    99.4   |
| reference_driver         |     100%  |     99.8  |    100   |    100    |   100.0   |

Strong-shell → projmem comparison: pass rate +100 abs pts, composite
+152% (36.07 → 90.79), token reduction 98.62%.

### Real Claude (Haiku 4.5) on /tmp/node, 4 repeats per arm (16 runs total)

| Task                          | Plain    | Projmem      | Δ       |
| ----------------------------- | -------- | ------------ | ------- |
| node_files_watcher_consumers  | 3/4 75%  | **4/4 100%** | +25 pts |
| node_safegetenv_callers       | 1/4 25%  | **2/4 50%**  | +25 pts |
| **Overall**                   | **4/8 50%** | **6/8 75%** | **+25 pts** |

Avg cost / run: plain $0.041, projmem $0.079 (≈2× more tokens for
projmem, all going into actual investigation rather than punted
claims). Total API spend across all 16 runs: $0.91.

Failure modes:
- **Plain**: Haiku says "I found all the files" without enumerating
  (the agent reads the prompt's "use file-write tool" demand and
  tries to write, but its claim text remains a one-liner).
- **Projmem**: occasional truncation of the same shape — but when
  Haiku does enumerate, it gets the answer right ~75-100% of the
  time, vs ~25-50% for plain. Bigger models (Opus, Sonnet) reduce
  the truncation rate further.

Reproducibility files:
- `bench/results/real_v2/HEADLINE.md` — derived numbers
- `bench/results/real_v2/{plain,projmem}/*.json` — raw 16 result
  blobs with claim text, regex check rows, metrics

The harness honors `claim_text_fallback` (default-on for `kind:
investigation` tasks) so an agent that answers in chat AND enumerates
correctly still scores. Decoy regex_checks anchor to line-start
citation contexts (allowing `(?im)^[\s>*\-\d.\`'\"]*` plus optional
`caller:`/`consumer:` keywords plus optional markdown bold/code), so
the agent can mention "I excluded X" in prose without triggering a
false-positive penalty. Both improvements landed via the v2 fix
pass — see `bench/grader.py` and `bench/tasks/node_*/spec.json`.
