# bench/tokcost — tokens and dollars per tool call

Same question, different tools. Tokens counted on Claude Opus 4.7.

Pricing (USD/MTok): input=15.0 cache-write=18.75 cache-read=1.5 output=75.0

Token source per row:
- api     → Anthropic `messages.count_tokens` (exact)
- offline → chars/tok from Ray (2026-04-17) by content profile (estimate)

## s1_setupmethod

| tool | bytes | tokens 4.6 | tokens 4.7 | ratio | $ input | $ cache-read | source |
| ---- | ----: | ---------: | ---------: | ----: | ------: | -----------: | :----- |
| `grep_l` ◂ cheapest | 84 | 25 | 32 | 1.290 | $0.00048 | $0.00005 | offline |
| `projmem_reverse` | 508 | 147 | 166 | 1.130 | $0.00249 | $0.00025 | offline |
| `projmem_symbol` | 1,739 | 504 | 568 | 1.130 | $0.00852 | $0.00085 | offline |
| `grep` | 2,323 | 685 | 883 | 1.290 | $0.01324 | $0.00132 | offline |
| `rg` | 2,323 | 685 | 883 | 1.290 | $0.01324 | $0.00132 | offline |

## s2_read_app

| tool | bytes | tokens 4.6 | tokens 4.7 | ratio | $ input | $ cache-read | source |
| ---- | ----: | ---------: | ---------: | ----: | ------: | -----------: | :----- |
| `head200` ◂ cheapest | 8,256 | 2,243 | 2,887 | 1.290 | $0.04330 | $0.00433 | offline |
| `cat` | 65,423 | 17,778 | 22,875 | 1.290 | $0.34313 | $0.03431 | offline |
| `projmem_pack` | 140,618 | 40,759 | 45,954 | 1.130 | $0.68931 | $0.06893 | offline |

## s3_entrypoints

| tool | bytes | tokens 4.6 | tokens 4.7 | ratio | $ input | $ cache-read | source |
| ---- | ----: | ---------: | ---------: | ----: | ------: | -----------: | :----- |
| `find` ◂ cheapest | 49 | 19 | 27 | 1.390 | $0.00040 | $0.00004 | offline |
| `grep_main` | 139 | 41 | 53 | 1.290 | $0.00080 | $0.00008 | offline |
| `projmem_entry` | 1,396 | 405 | 456 | 1.130 | $0.00684 | $0.00068 | offline |
| `ls` | 1,360 | 533 | 743 | 1.390 | $0.01114 | $0.00111 | offline |

## s4_files

| tool | bytes | tokens 4.6 | tokens 4.7 | ratio | $ input | $ cache-read | source |
| ---- | ----: | ---------: | ---------: | ----: | ------: | -----------: | :----- |
| `find_all` ◂ cheapest | 583 | 229 | 319 | 1.390 | $0.00479 | $0.00048 | offline |
| `projmem_files` | 34,536 | 10,010 | 11,286 | 1.130 | $0.16929 | $0.01693 | offline |

## overall

| tool | total bytes | total tokens 4.7 | total $ input |
| ---- | ----------: | ---------------: | ------------: |
| `find` | 49 | 27 | $0.00040 |
| `grep_l` | 84 | 32 | $0.00048 |
| `grep_main` | 139 | 53 | $0.00080 |
| `projmem_reverse` | 508 | 166 | $0.00249 |
| `find_all` | 583 | 319 | $0.00479 |
| `projmem_entry` | 1,396 | 456 | $0.00684 |
| `projmem_symbol` | 1,739 | 568 | $0.00852 |
| `ls` | 1,360 | 743 | $0.01114 |
| `grep` | 2,323 | 883 | $0.01324 |
| `rg` | 2,323 | 883 | $0.01324 |
| `head200` | 8,256 | 2,887 | $0.04330 |
| `projmem_files` | 34,536 | 11,286 | $0.16929 |
| `cat` | 65,423 | 22,875 | $0.34313 |
| `projmem_pack` | 140,618 | 45,954 | $0.68931 |

## reading the table

- `$ input` is what you pay once to admit the tool output into the
  model's context on a cold turn (no cache hit).
- `$ cache-read` is what you pay per subsequent turn once the
  prefix is cached — 10x cheaper. Long Claude Code sessions spend
  most of their input budget here.
- `ratio` is claude-opus-4-7 tokens ÷ claude-opus-4-6 tokens. The
  migration guide quotes 1.0–1.35x; Ray (2026-04-17) measured 1.47x
  on technical docs and 1.445x on a real CLAUDE.md.
