#!/usr/bin/env python3
"""Reference driver — copies every file from ``solution/`` into the
work directory, overwriting the seed. Used to prove a task's grader
is solvable. See SPEC.md §4 (``reference_driver`` must pass)."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    _self, task_id, work_dir, _prompt = sys.argv
    t0 = time.time()
    bench_root = Path(__file__).parent.parent.resolve()
    sol = bench_root / "tasks" / task_id / "solution"
    if not sol.is_dir():
        print(f"reference_driver: no solution/ under {sol}", file=sys.stderr)
        print(f"metrics: {json.dumps({'tokens': 0, 'ok': False})}",
              file=sys.stderr)
        return 2

    work = Path(work_dir)
    for p in sol.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(sol)
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)

    print("reference solution applied to work_dir")
    elapsed = round(time.time() - t0, 4)
    print(f"metrics: {json.dumps({'tokens': 0, 'time_s': elapsed})}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
