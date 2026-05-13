# bench/tokcost — tokens and dollars per tool call

What it answers: **when the agent needs to find something in a repo,
how many tokens does each tool actually burn on Claude 4.7?**

Same question, three families of tools:

| family       | tools measured                                  |
| ------------ | ----------------------------------------------- |
| bare shell   | `grep`, `grep -l`, `find`, `ls`, `cat`, `head`  |
| strong shell | `rg` (ripgrep)                                  |
| projmem      | `projmem symbol / reverse / pack / entrypoints / files` |

Each tool's real stdout is captured to `artifacts/<scenario>/<tool>.out`,
then the bytes are tokenized under the Claude Opus 4.7 tokenizer and
priced against the current Opus 4.7 rate card.

---

## Why this benchmark exists

Abhishek Ray's *I Measured Claude 4.7's New Tokenizer* (2026-04-17)
showed the 4.7 tokenizer packs code and English text 1.29–1.47x
tighter than 4.6 — more tokens per byte, same price per token. That
makes every tool call more expensive **per byte of output**. The
question we want to answer empirically: which tools are cheapest on
the new tokenizer, and by how much?

The existing A/B/C harness (`bench/run.py`) scores agent runs on
*task success*. This benchmark is complementary — it ignores success
and measures only the raw token cost of the tool outputs themselves,
so you can price a tool surface before ever running an agent.

---

## Layout

```
bench/tokcost/
  README.md         — this file
  capture.sh        — runs each tool on each scenario, dumps stdout to artifacts/
  run.py            — tokenizes + prices every artifact, emits RESULTS.md + results.json
  artifacts/        — raw stdout bytes (committed so results are reproducible)
  RESULTS.md        — generated table (checked in after every re-run)
  results.json      — generated machine output
```

---

## Reproduce

```bash
# 1. Capture tool outputs (requires /tmp/flask cloned + projmem indexed)
REPO=/tmp/flask bash bench/tokcost/capture.sh

# 2a. Offline tokenization (no API key required; uses Ray 2026-04
#     chars/token ratios by content profile — estimates, clearly labelled)
python3 bench/tokcost/run.py --mode offline

# 2b. Exact tokenization via Anthropic's free count_tokens endpoint
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python3 bench/tokcost/run.py --mode api
```

`run.py --mode api` makes two `POST /v1/messages/count_tokens` calls
per artifact (4.6 and 4.7) — free, no inference billed — and records
both counts plus the 4.6→4.7 ratio in `results.json`. The same
methodology Ray used.

---

## Tokenizer source

**API mode (authoritative):** Anthropic's `messages.count_tokens`
against `claude-opus-4-6` and `claude-opus-4-7`.

**Offline mode (estimate):** per-profile chars/token ratios taken
from Ray (2026-04-17). Each tool output is tagged with a content
profile in `TOOL_PROFILE` (e.g. `grep → terminal`,
`projmem pack → json_dense`, `cat → python`). The profile's 4.6 and
4.7 chars/token are applied to the artifact's character count.

A single run labels every row `offline` or `api` so consumers can
tell which is which.

---

## Pricing assumption

Opus 4.7 rate card as of 2026-04 (edit `PRICE_*` at the top of
`run.py` if these change):

```
input        $15.00 / MTok
cache-write  $18.75 / MTok
cache-read   $ 1.50 / MTok
output       $75.00 / MTok
```

`$ input` is what you pay on a cold turn (first time the tool output
enters context). `$ cache-read` is what you pay every subsequent turn
while the prefix stays in the 5-minute cache — 10x cheaper, and where
most of a long session's input budget actually goes.

---

## Honest caveats

1. **"Cheapest tokens" ≠ "best tool."** `grep -l` is cheaper than
   `projmem reverse` on raw bytes, but grep returns filenames and
   projmem returns typed structure — the agent may need several grep
   rounds (+ several `cat`s) to reach the same conclusion one
   projmem call gives. `bench/run.py` scores *task success*, not raw
   tokens, precisely because of this trade.

2. **Not all projmem output is small.** `projmem pack <file>` is
   larger than `cat <file>` on the same target, because it includes
   imports, cross-refs, and surrounding context the agent would
   otherwise have to fetch separately. If all you want is the file
   contents, `cat` / `Read` is cheaper. Don't use pack as a read
   substitute.

3. **Offline ratios are estimates.** A row labelled `offline` uses a
   published per-profile chars/token factor — accurate in aggregate,
   not exact per artifact. For publishable numbers, use `--mode api`.

4. **Tokenizer is one contributor.** Ray's piece also showed the
   migration guide's "1.0–1.35x" range understates real-world
   content, which clusters near the top (1.445x on CLAUDE.md,
   1.47x on tech docs). Our `ratio` column reproduces that
   on captured tool output — most rows fall 1.13–1.39x.

---

## Extending

Add a scenario by appending to `capture.sh`:

```bash
run sN_name tool_label  -- <command args>
```

and a tool-to-profile entry to `TOOL_PROFILE` in `run.py` if the new
tool's output style isn't already covered. Re-run `capture.sh` then
`run.py`; RESULTS.md regenerates from scratch every time.
