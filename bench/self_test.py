#!/usr/bin/env python3
"""bench/self_test.py — enforce the SPEC §4 invariants.

Every task MUST:
  1. Pass under reference_driver (the solution is correct).
  2. Fail under naive_grep_driver (the grader actually discriminates).

If either drifts, the benchmark has lost power and subsequent
results are meaningless. Run this before publishing any numbers.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
BENCH = ROOT / "bench"
RUN = BENCH / "run.py"
REF = BENCH / "drivers" / "reference_driver.py"
NAI = BENCH / "drivers" / "naive_grep_driver.py"


def _task_ids() -> list[str]:
    td = BENCH / "tasks"
    if not td.is_dir():
        return []
    return sorted(p.name for p in td.iterdir()
                  if p.is_dir() and (p / "spec.json").is_file())


def _run(task: str, driver: Path, work: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(RUN), "run", task, str(work),
         "--driver", str(driver)],
        capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise SystemExit(
            f"bench/run.py exited unexpectedly ({r.returncode}):\n"
            f"{r.stderr}")
    return json.loads(r.stdout)


def main() -> int:
    tasks = _task_ids()
    if not tasks:
        print("no tasks found under bench/tasks/")
        return 2

    total = len(tasks) * 2
    passed = 0
    fails: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        for tid in tasks:
            # Reference must pass.
            r = _run(tid, REF, tmp_root / f"ref-{tid}")
            if r.get("passed"):
                passed += 1
                print(f"[ok]  ref   pass  {tid}")
            else:
                fails.append(f"reference_driver FAILED {tid}")
                print(f"[FAIL] ref   pass  {tid}")

            # Naive must fail.
            r = _run(tid, NAI, tmp_root / f"nai-{tid}")
            if not r.get("passed"):
                passed += 1
                print(f"[ok]  naive FAIL  {tid}")
            else:
                fails.append(f"naive_grep_driver unexpectedly PASSED {tid}")
                print(f"[FAIL] naive FAIL  {tid}")

    print(f"\n{passed}/{total} invariants hold")
    if fails:
        print("\nFailures:")
        for line in fails:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
