"""bench/score.py — per-run scoring calculator.

Implements the scoring formulas defined in bench/SPEC.md §2.

A "run" is one (task, driver) execution. A raw grader output + the
driver's token/time metrics produce five primitive scores in [0, 100]
and one composite score.

Primitives
----------
success             — did the graded tests pass? (binary × 100)
coverage            — did it find all required items?
precision           — did it avoid wrong items (decoys, false claims)?
token_efficiency    — tokens used vs baseline
time_efficiency     — wall-clock vs baseline

Composite
---------
composite = 0.40·success + 0.25·coverage + 0.15·precision
          + 0.10·token_efficiency + 0.10·time_efficiency

All outputs are clamped to [0, 100]. Baselines may be missing
(first Arm-A run has no peer baseline); in that case both
efficiencies default to 50.0 and the result is annotated
``baseline_note: baseline missing``.

The JSON schema of the per-run score output is fixed in SPEC.md §10;
tests enforce that every key is present.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

SCHEMA_VERSION = 2

DEFAULT_WEIGHTS: dict[str, float] = {
    "success":          0.40,
    "coverage":         0.25,
    "precision":        0.15,
    "token_efficiency": 0.10,
    "time_efficiency":  0.10,
}


# ---------------------------------------------------------------------------
# Primitive calculators
# ---------------------------------------------------------------------------


def score_success(graded: dict[str, Any]) -> float:
    """§2.1 — binary gate."""
    return 100.0 if graded.get("passed") else 0.0


def score_coverage(graded: dict[str, Any]) -> float:
    """§2.2 — weighted file + regex coverage.

    coverage = 0.7 · files_touched/files_total + 0.3 · regex_passed/regex_total

    When a denominator is zero (task has no regex checks, or no
    required files), that component is treated as 1.0 (nothing to
    fail) so the other component carries the full weight.
    """
    details = graded.get("details", {}) or {}

    files_total = int(details.get("required_files_total", 0))
    files_touched = int(details.get("required_files_touched", 0))
    regex_total = int(details.get("regex_checks_total", 0))
    regex_passed = int(details.get("regex_checks_passed", 0))

    files_ratio = (files_touched / files_total) if files_total else 1.0
    regex_ratio = (regex_passed / regex_total) if regex_total else 1.0

    files_ratio = max(0.0, min(1.0, files_ratio))
    regex_ratio = max(0.0, min(1.0, regex_ratio))

    return 100.0 * (0.7 * files_ratio + 0.3 * regex_ratio)


def score_precision(graded: dict[str, Any]) -> float:
    """§2.3 — no fabrication / no false positives.

    precision = max(0, 1 − 0.5·decoys_touched
                          − 0.3·claim_violations
                          − 0.1·extra_regex_over_matches)
    """
    details = graded.get("details", {}) or {}

    decoys = int(details.get("decoy_files_touched", 0))
    claims = int(details.get("claim_violations", 0))
    extra = int(details.get("extra_regex_over_matches", 0))

    raw = 1.0 - 0.5 * decoys - 0.3 * claims - 0.1 * extra
    return 100.0 * max(0.0, min(1.0, raw))


def score_token_efficiency(run_tokens: int | None,
                           baseline_tokens: float | None) -> tuple[float, bool]:
    """§2.4 — tokens vs baseline.

    Returns (score, baseline_present). When baseline is missing or
    zero, returns (50.0, False) — a neutral placeholder.
    """
    if run_tokens is None:
        return 50.0, False
    if baseline_tokens is None or baseline_tokens <= 0:
        return 50.0, False
    ratio = run_tokens / baseline_tokens
    eff = 1.0 - ratio
    return 100.0 * max(0.0, min(1.0, eff)), True


def score_time_efficiency(run_time_s: float | None,
                          baseline_time_s: float | None) -> tuple[float, bool]:
    """§2.4 — wall-clock vs baseline."""
    if run_time_s is None:
        return 50.0, False
    if baseline_time_s is None or baseline_time_s <= 0:
        return 50.0, False
    ratio = run_time_s / baseline_time_s
    eff = 1.0 - ratio
    return 100.0 * max(0.0, min(1.0, eff)), True


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def composite(primitives: dict[str, float],
              weights: dict[str, float] | None = None) -> float:
    """§2.5 — weighted sum of the five primitives."""
    w = weights or DEFAULT_WEIGHTS
    total_w = sum(w.values())
    if total_w <= 0:
        raise ValueError("weights sum to zero")
    s = 0.0
    for k, wk in w.items():
        s += wk * float(primitives.get(k, 0.0))
    return s / total_w


# ---------------------------------------------------------------------------
# Top-level scorer
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    task_id: str
    driver: str
    primitives: dict[str, float]
    composite: float
    baseline_note: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "driver": self.driver,
            "primitives": self.primitives,
            "composite": round(self.composite, 4),
            "baseline_note": self.baseline_note,
            "raw": self.raw,
        }


def score_run(result: dict[str, Any],
              baseline_tokens: float | None = None,
              baseline_time_s: float | None = None,
              weights: dict[str, float] | None = None) -> ScoreResult:
    """Score a single run.

    ``result`` is the JSON blob written by bench/run.py — it contains
    ``task_id``, ``driver``, ``passed``, ``scores``, ``details``, and
    optionally ``metrics.tokens`` / ``metrics.time_s``.
    """
    graded = result  # run.py writes grader output at the top level
    metrics = result.get("metrics") or {}

    succ = score_success(graded)
    cov = score_coverage(graded)
    prec = score_precision(graded)
    tok_eff, tok_present = score_token_efficiency(
        metrics.get("tokens"), baseline_tokens)
    time_eff, time_present = score_time_efficiency(
        metrics.get("time_s"), baseline_time_s)

    primitives = {
        "success":          round(succ, 4),
        "coverage":         round(cov, 4),
        "precision":        round(prec, 4),
        "token_efficiency": round(tok_eff, 4),
        "time_efficiency":  round(time_eff, 4),
    }
    comp = composite(primitives, weights)

    if tok_present and time_present:
        note = "baseline present"
    elif tok_present or time_present:
        note = "baseline partial"
    else:
        note = "baseline missing"

    return ScoreResult(
        task_id=str(result.get("task_id", "")),
        driver=str(result.get("driver", "")),
        primitives=primitives,
        composite=comp,
        baseline_note=note,
        raw=graded,
    )


# ---------------------------------------------------------------------------
# Baseline computation (from a directory of Arm-A runs)
# ---------------------------------------------------------------------------


def compute_baseline(baseline_result_files: list[Path],
                     task_id: str | None = None
                     ) -> tuple[float | None, float | None]:
    """Compute per-task median baseline token / time from a list of
    result JSON files. If ``task_id`` is given, only files whose
    ``task_id`` matches contribute."""
    tokens: list[int] = []
    times: list[float] = []
    for p in baseline_result_files:
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if task_id is not None and d.get("task_id") != task_id:
            continue
        m = d.get("metrics") or {}
        t = m.get("tokens")
        if isinstance(t, int) and t >= 0:
            tokens.append(t)
        ts = m.get("time_s")
        if isinstance(ts, (int, float)) and ts >= 0:
            times.append(float(ts))
    bt = median(tokens) if tokens else None
    btime = median(times) if times else None
    return bt, btime


# ---------------------------------------------------------------------------
# Directory-level scoring
# ---------------------------------------------------------------------------


def score_directory(results_dir: Path,
                    baseline_dir: Path | None = None,
                    weights: dict[str, float] | None = None
                    ) -> list[dict[str, Any]]:
    """Score every result JSON file in ``results_dir`` (recursively).

    If ``baseline_dir`` is given, per-task medians of its result files
    are used as the efficiency baselines; otherwise the efficiency
    scores default to 50.0.
    """
    results = sorted(p for p in Path(results_dir).rglob("*.json")
                     if p.is_file())
    baseline_files: list[Path] = []
    if baseline_dir is not None and Path(baseline_dir).is_dir():
        baseline_files = [p for p in Path(baseline_dir).rglob("*.json")
                          if p.is_file()]

    out: list[dict[str, Any]] = []
    for rp in results:
        try:
            d = json.loads(rp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "task_id" not in d:
            continue  # not a run-result file
        bt, btime = compute_baseline(baseline_files, task_id=d.get("task_id"))
        scored = score_run(d, baseline_tokens=bt, baseline_time_s=btime,
                           weights=weights).to_dict()
        scored["source_file"] = str(rp)
        out.append(scored)
    return out
