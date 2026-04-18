# bench/SPEC.md — Formal benchmark specification

This document defines the scoring formulas, task schema, run
protocol, and honesty rules for the projmem benchmark. It is
binding on anyone producing or consuming benchmark results with
this suite.

---

## 1. Goal

Produce **defensible numbers** comparing an LLM's ability to
complete code-reasoning tasks with different tool configurations.
The expected comparison is:

| Arm                | Tools available                                                                      |
| ------------------ | ------------------------------------------------------------------------------------ |
| A. Naive           | `grep`, `find`, `cat`, `ls`, raw file I/O                                             |
| B. Strong shell    | Arm A + `rg`, `fd`, `jq`, `ctags`, `git grep`                                         |
| C. Projmem         | Arm A + `projmem` (pack, symbol, trace, reverse, forward, events, notes, contract-diff) |

The same LLM / same prompt / same token budget / same repo snapshot
is used across all arms. The only variable is the tool surface.

---

## 2. Scoring model

Every run produces five primitive scores in [0, 100]:

```
success             — did the graded tests pass? (binary × 100)
coverage            — did it find all required items?
precision           — did it avoid wrong items (decoys, false claims)?
token_efficiency    — tokens used vs baseline
time_efficiency     — wall-clock vs baseline
```

And one derived composite:

```
composite = 0.40·success + 0.25·coverage + 0.15·precision
          + 0.10·token_efficiency + 0.10·time_efficiency
```

Weights are stored in `bench/score.py::DEFAULT_WEIGHTS` and can be
overridden via `--weights`. The defaults encode the prompt
philosophy: *success* matters most, but *coverage* (completeness)
and *precision* (no fabrication) together outweigh raw efficiency.

### 2.1 `success` — binary gate

```
success = 100 if graded passed else 0
```

The grader's `passed` field is true iff:
- every `required_files` entry was modified (or created, or
  deleted per `must_delete_files`)
- no `decoy_files` entry was touched
- every `regex_check` met its `min_matches` / `max_matches` bound
- `test_command` exited with code 0 (if specified)
- no `must_not_claim` phrase appeared in `completion_claim` while
  reality contradicted it

### 2.2 `coverage` — completeness

```
coverage = 0.7 × coverage_files + 0.3 × coverage_regex
coverage_files = required_files_touched / required_files_total
coverage_regex = regex_checks_passed / regex_checks_total
```

File coverage is the primary signal; regex coverage is the
secondary invariant.

### 2.3 `precision` — no fabrication / no false positives

```
precision = max(0, 1 − 0.5·decoy_files_touched
                       − 0.3·claim_violations
                       − 0.1·extra_regex_over_matches)
```

Penalty scales with wrongness. A single decoy touch cuts precision
in half. A single claim violation (e.g. "all tests pass" while they
don't) cuts 30 points. The denominators are chosen so one wrong
action visibly moves the score without zeroing it entirely — we
still want to distinguish "completely wrong" from "mostly right but
touched one decoy".

### 2.4 `token_efficiency` and `time_efficiency`

Relative to a **per-task baseline** (the median of the Arm A runs
for that same task):

```
token_efficiency = max(0, min(1, 1 − tokens_run / tokens_baseline))
time_efficiency  = max(0, min(1, 1 − time_run   / time_baseline))
```

Both are clamped to [0, 1]. If the run used MORE tokens than the
baseline, efficiency is 0. If it used half the tokens, efficiency
is 0.5. If it used 0 tokens (possible for simulated drivers),
efficiency is 1.0.

If the baseline is not provided (e.g. first run, no Arm A data
yet), efficiency defaults to 0.5 — neutral. Stated explicitly in
the score output as `baseline: missing`.

### 2.5 Composite

```
composite = 0.40·success + 0.25·coverage + 0.15·precision
          + 0.10·token_efficiency + 0.10·time_efficiency
```

All scaled 0–100. **Report composite AND the five primitives** —
never composite alone. A high composite with 40% success is still
a failing run.

### 2.6 Relative gain

For comparing two arms:

```
relative_gain_% = (composite_B − composite_A) / composite_A × 100
```

Report with mean AND median across repeats — mean is sensitive to
one lucky run, median is robust but hides variance.

---

## 3. Task schema (`spec.json`)

```jsonc
{
  // Identity
  "id": "flag_propagation_orphan",         // snake_case; matches dir name
  "kind": "edit-completeness",             // task family (§4)

  // Description / prompt
  "description": "Short human-readable.",  // ≤ 1 line
  "prompt": "Full prompt fed to the LLM. This is the ONLY text the driver is allowed to use to infer the task. No side-channel info via required_files or regex_checks.",

  // Grader inputs
  "required_files": [                      // files that MUST be modified/created/deleted
    "cli/args.py",
    "src/output.py",
    "tests/test_output.py"
  ],
  "decoy_files": [                         // files that MUST NOT be touched
    "legacy/shadow_old.py"
  ],
  "must_delete_files": [                   // files that must end up absent
    "src/legacy_helpers.py"
  ],
  "regex_checks": [                        // per-file regex invariants
    {
      "file": "cli/args.py",
      "pattern": "shadow[_-]mode",
      "min_matches": 1,
      "reason": "flag must be declared"
    },
    {
      "file": "src/legacy.py",
      "pattern": "print\\(",
      "max_matches": 0,
      "reason": "legacy file must not grow new prints"
    }
  ],
  "test_command": "python3 -m pytest tests/ -q",
  "must_not_claim": [                      // phrases the driver claim must not assert while reality contradicts
    "all consumers updated",
    "fully wired"
  ]
}
```

Every field except `id`, `prompt`, and `required_files` is optional.
Empty lists (e.g. `decoy_files: []`) are fine.

---

## 4. The 4 task families

| Family                        | What it tests                                                    | Grep-alone expected result |
| ----------------------------- | ---------------------------------------------------------------- | -------------------------- |
| **wrong-file / same-name ambiguity** | Same symbol exists in multiple files; only one is correct target | FAIL — grep returns all matches; agent picks wrong |
| **cross-file migration / refactor**   | Rename / signature change must touch all true callers           | PARTIAL — grep finds literal occurrences but misses aliases + imports |
| **hidden semantic contract**          | Contract (env, flag, event, schema) used indirectly              | FAIL — grep misses propagation across transforms |
| **exhaustiveness / dependency topology** | Find every place X is implemented; or delete-safe with deps    | PARTIAL — grep finds most but may miss dynamic / plugin dispatch |

**The invariant rule**: a task is only valid in the suite if
`naive_grep_driver` FAILS it and `reference_driver` PASSES it. If
grep can solve the task easily, the task is DISCARDED — it has no
discriminating power.

The `bench/self_test.py` enforces this: every task must pass
reference + fail naive.

---

## 5. Run protocol

### 5.1 Single run

```bash
python3 bench/run.py run <task_id> <work_dir> --driver <path-to-driver.py> --output <result.json>
```

Produces one `result.json` with grader output + driver metrics.

### 5.2 Multi-run (recommended)

```bash
python3 bench/run.py run <task_id> <work_dir_prefix> \
  --driver <path-to-driver.py> \
  --repeats 5 \
  --output-dir <results/task_id/>
```

Produces 5 result files: `task_id_run1.json` … `task_id_run5.json`.
Run-to-run variance on deterministic drivers is zero; on real LLM
drivers it's where you learn the actual distribution.

### 5.3 A/B/C full matrix

```bash
python3 bench/run.py matrix \
  --tasks flag_propagation_orphan,rename_with_dynamic_dispatch,... \
  --drivers bare:bench/drivers/simulated_bare_driver.py \
  --drivers shell:bench/drivers/strong_shell_driver.py \
  --drivers projmem:bench/drivers/simulated_projmem_driver.py \
  --repeats 5 \
  --output-root bench/results/run_2026_04_14/
```

Produces the full (tasks × drivers × repeats) result tree.

### 5.4 Score + aggregate + compare

```bash
# Compute composite scores from result files
python3 bench/run.py score bench/results/run_2026_04_14/ \
  --baseline-dir bench/results/run_2026_04_14/bare/ \
  --output scores.json

# Aggregate across repeats
python3 bench/run.py aggregate scores.json --output aggregated.json

# Compare two arms
python3 bench/run.py compare \
  --baseline bench/results/run_2026_04_14/bare/ \
  --challenger bench/results/run_2026_04_14/projmem/ \
  --output comparison.md
```

---

## 6. Honest reporting rules

### Three headline numbers, always together

1. **Pass rate lift**:
   > "Projmem improved task pass rate from A% to B% (absolute +N points, relative +M%)."

2. **Coverage lift**:
   > "Projmem improved required-item coverage from A% to B%."

3. **Cost delta**:
   > "Projmem reduced median tokens by N% and median wall-clock by M%."

Reporting any one in isolation is misleading. A 50% pass-rate lift
that tripled token cost is a worse tool for most budgets.

### Required transparency

Every published benchmark MUST include:

- [FACT] model name + version + temperature + max-turns
- [FACT] repo commit SHA + any applied patches
- [FACT] system prompt (or its diff from a canonical version)
- [FACT] exact driver versions used
- [FACT] number of repeats + aggregation method (mean or median)
- [FACT] total tokens spent on the full matrix
- [INFERENCE] composite scores for each arm
- [INFERENCE] relative_gain_% with confidence interval if repeats ≥ 5

### Forbidden claims

- "Projmem solves 92% of tasks" without specifying the task set.
- "Projmem is 3× faster" without a token-cost matched-baseline.
- "Projmem finds bugs" — it doesn't find bugs; it helps an agent
  look at the right code. See §8 of this spec.
- Single-run results presented as evidence of general capability.

---

## 7. Task difficulty tiers

| Tier   | Criteria                                                | Example tasks                          |
| ------ | ------------------------------------------------------- | -------------------------------------- |
| Easy   | Naive driver passes at least 1 of 5 repeats             | (discarded from the suite — no power)  |
| Medium | Naive always fails; shell baseline sometimes passes     | cross-file refactor, simple renames    |
| Hard   | Both naive AND shell fail; projmem sometimes passes     | same-name three-way, hidden contracts  |
| Elite  | All baselines fail; projmem + discipline needed         | temporal event-pair, graph-topology    |

The current 10-task suite is split:
- medium: `flag_propagation_orphan`, `rename_with_dynamic_dispatch`, `env_rename_database_url`, `delete_with_importers`, `enum_exhaustiveness`
- hard: `same_name_decoy`, `circular_import_silent_break`, `migration_version_drift`
- elite: `event_mismatch_fix`, `shadowed_name_wrong_import`

---

## 8. What NOT to benchmark (from prompt.md Section 0)

projmem is a **navigation tool**. Do not benchmark:

- "Can the LLM find a 0-day?" — static nav doesn't produce 0-days.
- "Can the LLM understand the repo?" — not measurable.
- "Can the LLM summarize architecture?" — flowery prose, not
  actionable signal.
- "Time to first plausible answer" — rewards hallucination.

DO benchmark:

- Can it find the RIGHT file to edit?
- Can it find ALL the files that need to change?
- Can it avoid touching decoys?
- Can it verify its own claim before declaring done?
- Can it skip re-investigation when a prior session left a
  `refute` note?

---

## 9. Suggested minimum task set

For a benchmark result to be publishable, use at minimum:

- **20 tasks**: 5 per family × 4 families
- **3 drivers** (arms): bare, strong-shell, projmem
- **5 repeats** per (task, driver) cell
- **Same model** across all runs (GPT-4.1 / Claude Sonnet 4.5 /
  etc.)
- **Total runs**: 20 × 3 × 5 = 300

That gives enough per-cell data to report mean + median + IQR and
defensibly claim or deny an improvement with confidence intervals.

Scaling down (e.g. 10 tasks × 2 arms × 3 repeats = 60 runs) is OK
for an internal sanity check but not for a public claim.

---

## 10. Output format (for every `run.py score` invocation)

```json
{
  "task_id": "flag_propagation_orphan",
  "driver": "simulated_projmem",
  "raw": {                          // from grader
    "passed": true,
    "scores": {...},
    "details": {...},
    "metrics": {...}
  },
  "primitives": {
    "success": 100.0,
    "coverage": 100.0,
    "precision": 100.0,
    "token_efficiency": 50.0,       // 50 = baseline missing or equal
    "time_efficiency": 75.0
  },
  "composite": 91.25,               // weighted sum
  "baseline_note": "baseline present, N=5 reps"
}
```

And for every `aggregate` invocation:

```json
{
  "per_task": {
    "flag_propagation_orphan": {
      "runs": 5,
      "mean_composite": 91.25,
      "median_composite": 91.25,
      "stddev_composite": 0.0,
      "pass_rate": 1.0,
      "mean_coverage": 100.0,
      "mean_precision": 100.0,
      "median_tokens": 0,
      "median_time_s": 1.43
    },
    ...
  },
  "per_family": {
    "edit-completeness":    {"pass_rate": 1.0, "mean_composite": 91.0},
    "adversarial-uncertainty": {"pass_rate": 1.0, "mean_composite": 89.3},
    ...
  },
  "overall": {
    "tasks": 10,
    "total_runs": 50,
    "pass_rate": 1.0,
    "mean_composite": 90.1,
    "median_composite": 91.25
  }
}
```

And for every `compare` invocation:

```json
{
  "baseline_arm":   "simulated_bare",
  "challenger_arm": "simulated_projmem",
  "headline": {
    "pass_rate_baseline": 0.0,
    "pass_rate_challenger": 1.0,
    "pass_rate_lift_abs_points": 100.0,
    "pass_rate_lift_relative_pct": null,     // relative division by zero handled
    "coverage_baseline_mean": 42.3,
    "coverage_challenger_mean": 100.0,
    "coverage_lift_abs_points": 57.7,
    "token_reduction_pct": 0.0,              // both = 0 in simulated
    "time_reduction_pct": 0.0,
    "composite_baseline": 38.1,
    "composite_challenger": 91.25,
    "composite_relative_gain_pct": 139.5
  },
  "per_task": [...],
  "per_family": [...]
}
```

---

## 11. Canonical citation line

When reporting a projmem benchmark result publicly:

> "On the projmem bench suite (10 tasks, 4 families, 5 repeats, same
> model, 95% tree-sitter index coverage), the projmem arm scored
> composite **91.25** vs bare-arm **38.10**, a relative gain of
> **+139.5%**. Pass rate went from 0/10 to 10/10. Token + time
> efficiency neutral (simulated drivers). Evidence: scores.json,
> comparison.md, aggregated.json."

Shorter versions drop transparency and should be refused in
publishable contexts.

---

## 12. Schema version

```
bench_schema_version: 2
```

Bumped when the scoring formula, weights, or task schema change.
Consumers can check this field and refuse to aggregate across
mismatched versions.
