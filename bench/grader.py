"""bench/grader.py — deterministic grader.

Consumes a task spec + the post-driver work directory and emits a
structured result. The structure is what ``bench/score.py`` consumes
directly (see SPEC.md §3 / §10).

Never imports projmem — the grader must be usable against ANY
driver, including the naive baseline.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Task family lookup — used to tag results for per-family rollups.
# Keep in sync with bench/tasks/<id>/spec.json ``family`` field.
FAMILY_FALLBACK = "unknown"


@dataclass
class GradeResult:
    passed: bool
    scores: dict[str, Any]
    details: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed":  self.passed,
            "scores":  self.scores,
            "details": self.details,
            "metrics": self.metrics,
        }


def _read_spec(task_dir: Path) -> dict[str, Any]:
    p = task_dir / "spec.json"
    if not p.is_file():
        raise FileNotFoundError(f"missing spec.json under {task_dir}")
    return json.loads(p.read_text())


_BASELINE_SENTINEL = "<<absent>>"


def _seed_files(task_dir: Path,
                work_dir: Path | None = None) -> dict[str, str]:
    """Collect the baseline file contents the driver started from.

    Two sources, in order:

      1. `<work_dir>/.bench_baseline.json` — written at materialization
         time for external_repo tasks. Holds only the files the grader
         needs (required / decoy / regex_check targets); pristine
         snapshot of the real-repo state, so the diff is meaningful.

      2. `<task_dir>/seed/` — the historical synthetic-task layout.
         Walked recursively. Used when no baseline.json is present.

    A file in the baseline JSON with a `null` value means "absent at
    materialization time" — translated to the `<<absent>>` sentinel so
    `_was_created` and `_was_deleted` work the same way as before.
    """
    if work_dir is not None:
        baseline = work_dir / ".bench_baseline.json"
        if baseline.is_file():
            try:
                data = json.loads(baseline.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            files = data.get("files") or {}
            return {rel: (val if val is not None else _BASELINE_SENTINEL)
                    for rel, val in files.items()}
    seed_dir = task_dir / "seed"
    out: dict[str, str] = {}
    if not seed_dir.is_dir():
        return out
    for p in seed_dir.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(seed_dir))
            try:
                out[rel] = p.read_text()
            except (OSError, UnicodeDecodeError):
                out[rel] = ""
    return out


def _was_modified(rel: str, seed: dict[str, str], work_dir: Path) -> bool:
    target = work_dir / rel
    if not target.exists():
        return False
    try:
        now = target.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return now != seed.get(rel, _BASELINE_SENTINEL)


def _was_created(rel: str, seed: dict[str, str], work_dir: Path) -> bool:
    target = work_dir / rel
    if not target.exists():
        return False
    # `rel not in seed` covers the synthetic-task layout. The
    # external_repo flow always writes every required/decoy/regex
    # target into the baseline (with null when absent at materialize
    # time), so we also recognize the explicit-absent sentinel.
    return rel not in seed or seed.get(rel) == _BASELINE_SENTINEL


def _was_deleted(rel: str, seed: dict[str, str], work_dir: Path) -> bool:
    target = work_dir / rel
    return (not target.exists()) and (rel in seed
                                       and seed[rel] != _BASELINE_SENTINEL)


def _run_regex_checks(checks: list[dict[str, Any]],
                      work_dir: Path,
                      *,
                      fallback_text: str | None = None
                      ) -> tuple[int, int, int, list[dict[str, Any]]]:
    """Returns (passed, total, extra_over_matches, per-check details).

    ``extra_over_matches`` counts how many checks exceeded their
    ``max_matches`` bound — used in the precision penalty.

    ``fallback_text``: when the on-disk file the check targets is
    missing, fall back to scanning this string. Used so an agent that
    answered the investigation question in chat (instead of writing
    ANSWER.md) still gets credit IF its claim text contains the right
    citations. The fallback is logged in `result_source`.
    """
    passed = 0
    total = len(checks)
    extra = 0
    rows: list[dict[str, Any]] = []
    for c in checks:
        row = {
            "file":          c.get("file"),
            "pattern":       c.get("pattern"),
            "reason":        c.get("reason"),
            "result":        "unknown",
            "result_source": "file",
            "matches":       0,
        }
        fpath = work_dir / c["file"]
        text: str | None = None
        if fpath.is_file():
            try:
                text = fpath.read_text()
            except (OSError, UnicodeDecodeError):
                row["result"] = "read_error"
                rows.append(row)
                continue
        elif fallback_text is not None:
            text = fallback_text
            row["result_source"] = "claim_text"
        else:
            row["result"] = "file_missing"
            rows.append(row)
            continue
        matches = re.findall(c["pattern"], text or "")
        row["matches"] = len(matches)
        lo = int(c.get("min_matches", 0))
        hi = c.get("max_matches")
        hi_i = int(hi) if hi is not None else None
        ok_lo = row["matches"] >= lo
        ok_hi = (hi_i is None) or (row["matches"] <= hi_i)
        if ok_lo and ok_hi:
            row["result"] = "pass"
            passed += 1
        else:
            row["result"] = "fail"
            if hi_i is not None and row["matches"] > hi_i:
                extra += 1
        rows.append(row)
    return passed, total, extra, rows


def _run_test_command(cmd: str, cwd: Path,
                      timeout_s: int) -> dict[str, Any]:
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=str(cwd), timeout=timeout_s,
            capture_output=True, text=True)
        return {
            "ran":       True,
            "rc":        r.returncode,
            "stdout":    r.stdout[-4000:],
            "stderr":    r.stderr[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"ran": True, "rc": 124, "stdout": "", "stderr": "timeout"}
    except OSError as e:
        return {"ran": False, "rc": -1, "stdout": "", "stderr": str(e)}


def _claim_violations(spec: dict[str, Any],
                      completion_claim: str,
                      reality_passed: bool) -> int:
    """Return a count of must_not_claim phrases that appear while
    reality contradicts them. "Reality contradicts" currently means
    ``reality_passed`` is False — a task might add finer rules later.
    """
    if reality_passed:
        return 0
    phrases = spec.get("must_not_claim") or []
    text = (completion_claim or "").lower()
    return sum(1 for ph in phrases if ph.lower() in text)


def grade(task_dir: Path, work_dir: Path,
          completion_claim: str = "",
          driver_metrics: dict[str, Any] | None = None,
          test_timeout_s: int = 120) -> GradeResult:
    """Grade a single post-driver run. See SPEC.md §2 / §3."""
    spec = _read_spec(task_dir)
    seed = _seed_files(task_dir, work_dir)

    required = list(spec.get("required_files") or [])
    decoys = list(spec.get("decoy_files") or [])
    must_delete = list(spec.get("must_delete_files") or [])
    regex_checks = list(spec.get("regex_checks") or [])
    test_cmd = spec.get("test_command")

    touched_required = []
    missed_required = []
    for rel in required:
        if _was_modified(rel, seed, work_dir) or _was_created(rel, seed, work_dir):
            touched_required.append(rel)
        else:
            missed_required.append(rel)

    # When fallback is enabled and the agent answered in chat, treat the
    # missing required file as touched IF every regex_check that targeted
    # it passes against the claim text. Computed AFTER the regex pass
    # below — placeholder for now.

    touched_decoys = [rel for rel in decoys
                      if _was_modified(rel, seed, work_dir)]

    delete_failures = [rel for rel in must_delete
                       if not _was_deleted(rel, seed, work_dir)]

    # Investigation tasks: an agent that answered correctly in CHAT
    # (instead of writing ANSWER.md) should still get partial credit
    # if its claim text contains the right citations. Spec.json can
    # opt OUT by setting `claim_text_fallback: false`.
    fallback_text: str | None = None
    fallback_enabled = (
        spec.get("kind") == "investigation"
        and spec.get("claim_text_fallback") is not False)
    if fallback_enabled and completion_claim:
        fallback_text = completion_claim

    regex_pass, regex_total, extra_over, regex_rows = _run_regex_checks(
        regex_checks, work_dir, fallback_text=fallback_text)

    # Promote claim-text answers: if every regex check resolved against
    # claim_text (i.e., the on-disk file was missing) and they all passed,
    # rescue the missing required_files from the missed list. Without this
    # an agent that answers correctly in chat fails on file_missing alone.
    if fallback_enabled and missed_required and regex_total > 0:
        all_via_claim = all(
            r.get("result_source") == "claim_text" and r.get("result") == "pass"
            for r in regex_rows
            if r.get("file") in set(missed_required))
        if all_via_claim:
            for rel in list(missed_required):
                touched_required.append(rel)
            missed_required = []

    test_out = None
    if test_cmd:
        test_out = _run_test_command(test_cmd, work_dir, test_timeout_s)

    passed = (
        len(missed_required) == 0
        and len(touched_decoys) == 0
        and len(delete_failures) == 0
        and regex_pass == regex_total
        and (test_out is None or test_out.get("rc") == 0)
    )

    claim_vio = _claim_violations(spec, completion_claim, passed)
    if claim_vio > 0:
        passed = False

    details = {
        "required_files_total":    len(required),
        "required_files_touched":  len(touched_required),
        "required_files_missed":   missed_required,
        "decoy_files_touched":     len(touched_decoys),
        "decoy_files_list":        touched_decoys,
        "must_delete_failures":    delete_failures,
        "regex_checks_total":      regex_total,
        "regex_checks_passed":     regex_pass,
        "regex_checks":            regex_rows,
        "extra_regex_over_matches": extra_over,
        "test_command":            test_cmd,
        "test_result":             test_out,
        "claim_violations":        claim_vio,
    }
    scores = {
        "required_coverage": (
            len(touched_required) / len(required)) if required else 1.0,
        "decoy_penalty_count": len(touched_decoys),
        "regex_coverage": (regex_pass / regex_total) if regex_total else 1.0,
    }
    metrics = dict(driver_metrics or {})
    metrics.setdefault("tokens", None)
    metrics.setdefault("time_s", None)

    return GradeResult(passed=passed, scores=scores,
                       details=details, metrics=metrics)


def build_family_map(tasks_root: Path) -> dict[str, str]:
    """Read every ``tasks/<id>/spec.json`` and return task_id → family."""
    out: dict[str, str] = {}
    if not tasks_root.is_dir():
        return out
    for d in tasks_root.iterdir():
        if not d.is_dir():
            continue
        spec_p = d / "spec.json"
        if not spec_p.is_file():
            continue
        try:
            s = json.loads(spec_p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        tid = s.get("id") or d.name
        out[tid] = s.get("family") or s.get("kind") or FAMILY_FALLBACK
    return out
