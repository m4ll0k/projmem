# bench/v2 — Three benchmarks that earn v2's headline claims

Per `docs/v2-design.md` Part VII ("The empirical commitments"), every
v2 claim in the README needs a number. Three setups land here:

| Setup | Question it answers | Measures |
|---|---|---|
| **reintroduction** | Does `creating` warning prevent agents from re-creating files we deleted? | reintroduction rate (baseline vs v1 vs v2-with-warnings) |
| **guidance**       | Does seeding 3-5 guidance notes on a known footgun change agent correctness or cost? | correctness; tokens/run |
| **critical**       | Does `critical` + edit-blocking + pause-mode produce a faster human-spotted bad-edit than the agent-runs-free baseline? | wall-clock to human-spotted; bad-edits-shipped rate |

Each harness lives under `bench/v2/<name>/`:

```
bench/v2/
├── reintroduction/
│   ├── spec.json          — task definition (prompt, scoring rules)
│   ├── seed.py            — sets up the repo + projmem state for one run
│   ├── grade.py           — applies scoring rules to the agent's output
│   └── run.py             — orchestrates N reps × M arms, writes results
├── guidance/
│   └── …same shape
└── critical/
    └── …same shape
```

## Why these aren't run automatically

A real run costs real money (anthropic API + bench-task LLMs).
Provisioning credentials in CI invites accidental burn. The harness
is here; the runs are operator-triggered:

```bash
ANTHROPIC_API_KEY=… python3 -m bench.v2.reintroduction.run \
    --reps 3 --arms baseline,v1,v2 --budget-usd 1.50
```

Each run writes JSON + a Markdown report under
`bench/v2/<name>/results/<utc_iso>/`.

## What "ship the numbers" means

When all three setups have a run with N ≥ 3 and within-arm variance
below their respective gates, the README hero rewrites from the
v2-hypothesis placeholder to the empirical line:

> *"…agents bypassing `projmem editing` reintroduced deleted code
> X/Y runs in the v2 benchmark."*

Until then the README placeholder explicitly says "TBD — bench
TBD" so we never publish a number we haven't actually measured.
