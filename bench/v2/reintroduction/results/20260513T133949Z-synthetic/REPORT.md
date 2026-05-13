# Reintroduction bench — synthetic run

_Run at 2026-05-13T13:39:52Z.
Synthetic: agent behavior is deterministic from the signals the arm observes; no LLM API calls. The signals themselves are real (we run the actual projmem CLI per arm)._

| Arm | N | reintroduced | rate | used_replacement | mean s/run |
|---|---:|---:|---:|---:|---:|
| A_baseline | 3 | 3 | 1.00 | 0 | 0.24 |
| C_v1_projmem | 3 | 3 | 1.00 | 0 | 0.31 |
| D_v2_with_creating_warning | 3 | 0 | 0.00 | 3 | 0.32 |

## Reading

- **A_baseline** reintroduces every time (no memory, no signal).
- **C_v1_projmem** reintroduces every time — v1 stores annotations but has no first-class tombstone surface; the wedge belongs to v2.
- **D_v2_with_creating_warning** reintroduces 0× (the wedge warning surfaces the deletion + replacement; the simulated agent reads it and backs off). On a real LLM run this number is expected to be > 0 (model doesn't always heed warnings) but materially lower than C.

## Reproduce

```bash
python3 -m bench.v2.reintroduction.run --reps 3
```

## Caveats

- This run is synthetic — the agent decision step is modeled, not LLM-driven. The wedge-fires-↦-agent-backs-off relationship is the v2 hypothesis being tested; a real-LLM run validates it.
- The wedge-warning text the agent reads is real (produced by the projmem CLI). The model's response to that text is the only thing that's stubbed.