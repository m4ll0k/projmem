#!/usr/bin/env python3
"""bench/multisession/report.py — render a results-dir to markdown.

Produces a table with one row per (arm, rep), the outcome label, and
the cost/time/token columns the operator needs to reason about
whether the spread is meaningful at the chosen N.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def render(out_dir: Path) -> str:
    summary = json.loads((out_dir / "summary.json").read_text())
    rows = summary["results"]
    lines: list[str] = []
    lines.append(f"# {summary['spec']} — {summary['ts']}\n")
    lines.append(f"Path: `{out_dir}`\n")
    has_inventory = any("outcome" in r for r in rows)
    if has_inventory:
        lines.append("| arm | outcome | prior✓ | curr✓ | "
                      "rm_recall | fab? | tokens | cost $ | time s |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['arm']} | {r.get('outcome','?')} | "
                f"{'Y' if r.get('prior_count_correct') else 'N'} | "
                f"{'Y' if r.get('current_count_correct') else 'N'} | "
                f"{r.get('removed_methods_recall','?')}% | "
                f"{'Y' if r.get('fabricated_prior') else 'N'} | "
                f"{r.get('tokens_total','?')} | "
                f"{r.get('cost_usd_total','?')} | "
                f"{r.get('time_s_total','?')} |")
    else:
        lines.append("| arm | drift_caught | coverage | tokens | "
                      "cost $ | time s |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['arm']} | {r.get('drift_caught','?')} | "
                f"{r.get('must_mention_coverage','?')}% | "
                f"{r.get('tokens_total','?')} | "
                f"{r.get('cost_usd_total','?')} | "
                f"{r.get('time_s_total','?')} |")

    lines.append("\n## Per-arm session-2 final answer (preview)\n")
    for r in rows:
        arm = r["arm"]
        rep = r["rep"]
        run = json.loads((out_dir / f"{arm}__rep{rep}" /
                           "run.json").read_text())
        gr = run.get("grading", {})
        prev = (gr.get("final_answer_preview") or "")[:600]
        lines.append(f"### arm {arm} rep {rep}\n")
        lines.append("```")
        lines.append(prev)
        lines.append("```\n")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    args = ap.parse_args()
    out = Path(args.out_dir)
    md = render(out)
    md_path = out / "report.md"
    md_path.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
