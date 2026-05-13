#!/usr/bin/env python3
"""bench/tokcost — measure token cost of tool outputs on Claude 4.7.

Same question asked three ways (bare shell / rg / projmem). Each arm's
raw stdout is tokenized and priced against Claude Opus 4.7 to produce
a defensible "tokens per tool call" and "dollars per tool call" table.

MODES
-----

1. api      — calls POST /v1/messages/count_tokens on claude-opus-4-6
              AND claude-opus-4-7, one request per artifact per model.
              Free (no inference). Requires ANTHROPIC_API_KEY.

2. offline  — uses the empirical chars/token ratios published in
              Abhishek Ray's "I Measured Claude 4.7's New Tokenizer"
              (2026-04-17), classified per content-type (JSON / code /
              shell / prose). Produces ESTIMATES clearly labelled as
              such. No API key required, fully reproducible.

Output: RESULTS.md (human) + results.json (machine).

Pricing (Opus 4.7, USD per 1M tokens, as of 2026-04):

    input        $15.00
    output       $75.00
    cache-write  $18.75 (5-minute TTL)
    cache-read   $ 1.50

The benchmark reports "cost per read" = input-price × tokens, i.e. the
dollars you pay ONCE to admit the tool output into the model's
context. Cached-read cost is also shown for long sessions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ARTIFACTS = HERE / "artifacts"

# Opus 4.7 pricing (USD per 1M tokens). If these change, edit here.
PRICE_INPUT   = 15.00
PRICE_CACHE_W = 18.75
PRICE_CACHE_R = 1.50
PRICE_OUTPUT  = 75.00

# ---------------------------------------------------------------------------
# Offline tokenizer — chars/token per content type, derived from Ray (2026)
# table of 4.7 tokenizer measurements. Also records the 4.6→4.7 ratio so
# the "migration cost" column is reproducible without the API.
# ---------------------------------------------------------------------------
#                     chars/tok  chars/tok  4.6→4.7
#                     on 4.6     on 4.7     ratio
PROFILES = {
    "prose_en":      (4.33,     3.60,     1.20),
    "tech_docs":     (5.32,     3.61,     1.47),
    "markdown_code": (3.94,     2.93,     1.34),
    "python":        (3.68,     2.86,     1.29),
    "typescript":    (3.66,     2.69,     1.36),
    "shell":         (2.55,     1.83,     1.39),
    "terminal":      (3.39,     2.63,     1.29),
    "stack_trace":   (3.03,     2.42,     1.25),
    "json_dense":    (3.45,     3.06,     1.13),
    "tool_schema":   (3.42,     3.05,     1.12),
    "csv":           (1.89,     1.76,     1.07),
    "code_diff":     (3.70,     3.05,     1.21),
    "git_log":       (3.13,     2.33,     1.34),
}

# Tool → content profile. Edit here to reclassify.
TOOL_PROFILE = {
    "grep":             "terminal",
    "rg":               "terminal",
    "grep_l":           "terminal",
    "grep_main":        "terminal",
    "find":             "shell",
    "find_all":         "shell",
    "ls":               "shell",
    "cat":              "python",        # cat'ing a .py file
    "head200":          "python",
    "projmem_symbol":   "json_dense",
    "projmem_reverse":  "json_dense",
    "projmem_pack":     "json_dense",
    "projmem_entry":    "json_dense",
    "projmem_files":    "json_dense",
}


@dataclass
class Sample:
    scenario: str
    tool: str
    path: str
    bytes: int
    profile: str
    tokens_46: int     # source: API or offline estimate
    tokens_47: int
    ratio: float
    source: str        # "api" or "offline"

    def cost_input_usd(self) -> float:
        return self.tokens_47 / 1_000_000 * PRICE_INPUT

    def cost_cacheread_usd(self) -> float:
        return self.tokens_47 / 1_000_000 * PRICE_CACHE_R


# ---------------------------------------------------------------------------
# Offline estimator
# ---------------------------------------------------------------------------
def estimate_offline(text: str, profile: str) -> tuple[int, int, float]:
    cpt_46, cpt_47, ratio = PROFILES[profile]
    nchars = len(text)
    t46 = max(1, round(nchars / cpt_46))
    t47 = max(1, round(nchars / cpt_47))
    return t46, t47, ratio


# ---------------------------------------------------------------------------
# API counter (optional path)
# ---------------------------------------------------------------------------
def count_api(text: str, model: str, client) -> int:
    r = client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": text}],
    )
    return int(r.input_tokens)


def try_build_api_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "no ANTHROPIC_API_KEY"
    try:
        from anthropic import Anthropic   # type: ignore
    except ImportError:
        return None, "anthropic SDK not installed (pip install anthropic)"
    return Anthropic(), "ok"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def iter_artifacts():
    for scen_dir in sorted(ARTIFACTS.iterdir()):
        if not scen_dir.is_dir():
            continue
        for f in sorted(scen_dir.glob("*.out")):
            yield scen_dir.name, f.stem, f


def run(mode: str) -> list[Sample]:
    client, reason = (None, "offline-forced")
    if mode == "api":
        client, reason = try_build_api_client()
        if client is None:
            print(f"warning: falling back to offline ({reason})", file=sys.stderr)
            mode = "offline"

    samples: list[Sample] = []
    for scenario, tool, path in iter_artifacts():
        text = path.read_text(errors="replace")
        profile = TOOL_PROFILE.get(tool, "prose_en")

        if mode == "api" and client is not None:
            try:
                t46 = count_api(text, "claude-opus-4-6", client)
                t47 = count_api(text, "claude-opus-4-7", client)
                ratio = round(t47 / t46, 4) if t46 else 0.0
                source = "api"
            except Exception as e:  # noqa: BLE001
                print(f"warning: api failed on {scenario}/{tool}: {e} — offline",
                      file=sys.stderr)
                t46, t47, ratio = estimate_offline(text, profile)
                source = "offline"
        else:
            t46, t47, ratio = estimate_offline(text, profile)
            source = "offline"

        samples.append(Sample(
            scenario=scenario, tool=tool, path=str(path),
            bytes=len(text.encode()), profile=profile,
            tokens_46=t46, tokens_47=t47, ratio=ratio, source=source,
        ))
    return samples


def emit_markdown(samples: list[Sample], out: Path) -> None:
    by_scen: dict[str, list[Sample]] = {}
    for s in samples:
        by_scen.setdefault(s.scenario, []).append(s)

    lines: list[str] = [
        "# bench/tokcost — tokens and dollars per tool call",
        "",
        "Same question, different tools. Tokens counted on Claude Opus 4.7.",
        "",
        f"Pricing (USD/MTok): input={PRICE_INPUT} cache-write={PRICE_CACHE_W} "
        f"cache-read={PRICE_CACHE_R} output={PRICE_OUTPUT}",
        "",
        "Token source per row:",
        f"- api     → Anthropic `messages.count_tokens` (exact)",
        f"- offline → chars/tok from Ray (2026-04-17) by content profile (estimate)",
        "",
    ]

    for scen, rows in by_scen.items():
        lines += [
            f"## {scen}",
            "",
            "| tool | bytes | tokens 4.6 | tokens 4.7 | ratio | $ input | $ cache-read | source |",
            "| ---- | ----: | ---------: | ---------: | ----: | ------: | -----------: | :----- |",
        ]
        rows.sort(key=lambda s: s.tokens_47)
        cheapest = rows[0]
        for s in rows:
            mark = " ◂ cheapest" if s is cheapest else ""
            lines.append(
                f"| `{s.tool}`{mark} | {s.bytes:,} | {s.tokens_46:,} "
                f"| {s.tokens_47:,} | {s.ratio:.3f} "
                f"| ${s.cost_input_usd():.5f} "
                f"| ${s.cost_cacheread_usd():.5f} | {s.source} |"
            )
        lines.append("")

    # Overall summary: sum of tokens per tool family.
    lines += [
        "## overall",
        "",
        "| tool | total bytes | total tokens 4.7 | total $ input |",
        "| ---- | ----------: | ---------------: | ------------: |",
    ]
    by_tool: dict[str, tuple[int, int, float]] = {}
    for s in samples:
        b, t, d = by_tool.get(s.tool, (0, 0, 0.0))
        by_tool[s.tool] = (b + s.bytes, t + s.tokens_47, d + s.cost_input_usd())
    for tool in sorted(by_tool, key=lambda k: by_tool[k][1]):
        b, t, d = by_tool[tool]
        lines.append(f"| `{tool}` | {b:,} | {t:,} | ${d:.5f} |")

    lines += [
        "",
        "## reading the table",
        "",
        "- `$ input` is what you pay once to admit the tool output into the",
        "  model's context on a cold turn (no cache hit).",
        "- `$ cache-read` is what you pay per subsequent turn once the",
        "  prefix is cached — 10x cheaper. Long Claude Code sessions spend",
        "  most of their input budget here.",
        "- `ratio` is claude-opus-4-7 tokens ÷ claude-opus-4-6 tokens. The",
        "  migration guide quotes 1.0–1.35x; Ray (2026-04-17) measured 1.47x",
        "  on technical docs and 1.445x on a real CLAUDE.md.",
        "",
    ]

    out.write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("api", "offline"), default="offline",
                    help="api requires ANTHROPIC_API_KEY + `pip install anthropic`")
    ap.add_argument("--out", type=Path, default=HERE / "RESULTS.md")
    ap.add_argument("--json-out", type=Path, default=HERE / "results.json")
    args = ap.parse_args()

    if not ARTIFACTS.is_dir() or not any(ARTIFACTS.iterdir()):
        print(f"error: no artifacts in {ARTIFACTS} — run ./capture.sh first",
              file=sys.stderr)
        return 1

    samples = run(args.mode)
    emit_markdown(samples, args.out)
    args.json_out.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": args.mode,
        "pricing_usd_per_mtok": {
            "input": PRICE_INPUT, "cache_write": PRICE_CACHE_W,
            "cache_read": PRICE_CACHE_R, "output": PRICE_OUTPUT,
        },
        "samples": [asdict(s) for s in samples],
    }, indent=2))
    print(f"wrote {args.out} and {args.json_out} ({len(samples)} samples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
