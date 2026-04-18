#!/usr/bin/env python3
"""bench/multisession/regrade.py — re-grade a saved multisession run
without re-spending API budget.

Reads a `results/<run_dir>/<arm>__<rep>/run.json`, re-applies the
grader against the recorded session-claim text, and emits a fresh
grading dict. Drop-in replacement for the original grader so a
better grader (LLM judge, human-tuned regex) can be backported to
old runs.

Usage:
    python3 bench/multisession/regrade.py <run_dir> [--spec PATH]

When `--spec` is omitted we resolve the spec from the run's
`task_id` field by searching `bench/multisession/specs/`.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent))

from bench.multisession.run import _grade  # noqa: E402


def regrade(run_dir: Path, spec_path: Path) -> dict:
    run = json.loads((run_dir / "run.json").read_text())
    spec = json.loads(spec_path.read_text())
    new_grade = _grade(spec, run["sessions"])
    out = {
        "task_id":     run["task_id"],
        "arm":         run["arm"],
        "rep":         run["rep"],
        "tokens_total":   run.get("tokens_total"),
        "cost_usd_total": run.get("cost_usd_total"),
        "time_s_total":   run.get("time_s_total"),
        "old_grading": run.get("grading"),
        "new_grading": new_grade,
    }
    (run_dir / "regrade.json").write_text(json.dumps(out, indent=2))
    return out


def _resolve_spec(task_id: str) -> Path:
    for p in (ROOT / "specs").glob("*.json"):
        try:
            sp = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if sp.get("task_id") == task_id:
            return p
    raise FileNotFoundError(
        f"no spec under {ROOT / 'specs'} matches task_id={task_id!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir",
                     help="`results/<task>__<ts>/<arm>__rep<N>` dir, "
                          "OR `results/<task>__<ts>` to regrade every "
                          "arm/rep under it.")
    ap.add_argument("--spec", default=None,
                     help="Override the spec auto-resolution.")
    args = ap.parse_args()
    target = Path(args.run_dir)
    candidates: list[Path] = []
    if (target / "run.json").exists():
        candidates = [target]
    else:
        for p in target.iterdir():
            if (p / "run.json").exists():
                candidates.append(p)
    if not candidates:
        sys.stderr.write(f"no run.json under {target}\n")
        return 2
    rows = []
    for c in candidates:
        run = json.loads((c / "run.json").read_text())
        spec_path = (Path(args.spec) if args.spec
                     else _resolve_spec(run["task_id"]))
        rows.append(regrade(c, spec_path))
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
