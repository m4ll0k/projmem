"""Pytest integration for the bench/ harness.

The bench is its own runtime (bench/run.py is a CLI), but we want the
core invariants to be enforced by the main test suite:

  * Every task passes under `reference_driver` (solution is correct).
  * Every task fails under `naive_grep_driver` (grader actually grades).

If either of these drifts, the bench itself has regressed and the
comparative results it produces lose meaning.
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent.resolve()
BENCH = ROOT / "bench"
RUN_PY = BENCH / "run.py"
REFERENCE_DRIVER = BENCH / "drivers" / "reference_driver.py"
NAIVE_DRIVER = BENCH / "drivers" / "naive_grep_driver.py"


def _task_ids():
    """Yield pytest.param entries for every spec.

    Tasks whose `external_repo.path` is missing on disk are emitted with a
    skip mark rather than collected silently — this keeps the test catalog
    visible while skipping cleanly in fresh checkouts where the external
    repos haven't been fetched.
    """
    tasks_dir = BENCH / "tasks"
    if not tasks_dir.is_dir():
        return []
    out = []
    for p in sorted(tasks_dir.iterdir()):
        spec_path = p / "spec.json"
        if not (p.is_dir() and spec_path.is_file()):
            continue
        marks = []
        try:
            spec = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError):
            spec = {}
        ext = spec.get("external_repo") if isinstance(spec, dict) else None
        ext_path = ext.get("path") if isinstance(ext, dict) else None
        if ext_path and not Path(ext_path).is_dir():
            marks.append(pytest.mark.skip(
                reason=f"external_repo.path {ext_path!r} not present"))
        out.append(pytest.param(p.name, marks=marks))
    return out


def _run_bench(task_id: str, driver: Path, work: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(RUN_PY), "run", task_id, str(work),
         "--driver", str(driver)],
        capture_output=True, text=True)
    assert r.returncode == 0, (
        f"bench runner exited {r.returncode}:\nSTDOUT:\n{r.stdout}\n"
        f"STDERR:\n{r.stderr}")
    return json.loads(r.stdout)


@pytest.mark.parametrize("task_id", _task_ids())
def test_reference_driver_passes_every_task(task_id, tmp_path):
    """The stored solution/ directory must satisfy the grader.
    A failure here means either the solution is wrong or the grader
    over-constrains the task."""
    result = _run_bench(task_id, REFERENCE_DRIVER, tmp_path / "work")
    assert result["passed"], (
        f"reference_driver FAILED {task_id}:\n"
        f"{json.dumps(result['details'], indent=2)}")


@pytest.mark.parametrize("task_id", _task_ids())
def test_naive_driver_fails_every_task(task_id, tmp_path):
    """The naive grep-and-patch driver must NOT satisfy the grader.
    A pass here means the grader is too lenient or the task lacks
    enough decoys/checks to distinguish correct from wrong."""
    result = _run_bench(task_id, NAIVE_DRIVER, tmp_path / "work")
    assert not result["passed"], (
        f"naive_grep_driver unexpectedly PASSED {task_id}; task is too "
        f"easy or grader too lenient. Result:\n"
        f"{json.dumps(result['scores'], indent=2)}")
