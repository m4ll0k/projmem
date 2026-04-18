"""bench/aggregate.py — multi-run aggregation.

Given a list of scored-run dicts (as produced by ``score.py``),
aggregate across repeats per task, per family, and overall. This
layer exists because SPEC.md §6 requires reporting mean AND median —
mean is sensitive to one lucky run, median hides variance. Both.

Output shape (stable, used by downstream reporters / tests):

    {
      "per_task":   { task_id: { ... } },
      "per_family": { family:  { ... } },
      "overall":    { ... },
    }
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from bench.score import SCHEMA_VERSION


def _median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def _mean(xs: list[float]) -> float:
    return float(statistics.fmean(xs)) if xs else 0.0


def _stddev(xs: list[float]) -> float:
    # ``pstdev`` so a single run yields 0.0 rather than raising.
    return float(statistics.pstdev(xs)) if xs else 0.0


def _family_of(task_id: str, family_map: dict[str, str]) -> str:
    return family_map.get(task_id, "unknown")


def _iter_scored_inputs(scored: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept either dicts produced by score_run().to_dict() or raw
    run-result dicts that have already been scored; normalise to a
    list of dicts with at least ``task_id`` / ``driver`` / ``primitives``
    / ``composite``."""
    out: list[dict[str, Any]] = []
    for s in scored:
        if "primitives" not in s or "composite" not in s:
            # Not scored yet — skip (scoring is the caller's job).
            continue
        out.append(s)
    return out


def aggregate(scored: Iterable[dict[str, Any]],
              family_map: dict[str, str] | None = None
              ) -> dict[str, Any]:
    """Aggregate per-task, per-family, and overall.

    ``family_map`` maps ``task_id → family_name``. If a task is
    missing, it's placed under ``"unknown"``.
    """
    family_map = family_map or {}
    rows = _iter_scored_inputs(scored)

    # Bucket by task.
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)

    per_task: dict[str, dict[str, Any]] = {}
    for tid, runs in sorted(by_task.items()):
        composites = [float(r["composite"]) for r in runs]
        passes = [1.0 if float(r["primitives"]["success"]) >= 100.0 else 0.0
                  for r in runs]
        covs = [float(r["primitives"]["coverage"]) for r in runs]
        precs = [float(r["primitives"]["precision"]) for r in runs]
        tok_eff = [float(r["primitives"]["token_efficiency"]) for r in runs]
        time_eff = [float(r["primitives"]["time_efficiency"]) for r in runs]
        toks = []
        times = []
        for r in runs:
            m = (r.get("raw") or {}).get("metrics") or {}
            if isinstance(m.get("tokens"), int):
                toks.append(float(m["tokens"]))
            if isinstance(m.get("time_s"), (int, float)):
                times.append(float(m["time_s"]))
        per_task[tid] = {
            "runs":              len(runs),
            "family":            _family_of(tid, family_map),
            "pass_rate":         round(_mean(passes), 4),
            "mean_composite":    round(_mean(composites), 4),
            "median_composite":  round(_median(composites), 4),
            "stddev_composite":  round(_stddev(composites), 4),
            "mean_coverage":     round(_mean(covs), 4),
            "mean_precision":    round(_mean(precs), 4),
            "mean_token_eff":    round(_mean(tok_eff), 4),
            "mean_time_eff":     round(_mean(time_eff), 4),
            "median_tokens":     round(_median(toks), 4),
            "median_time_s":     round(_median(times), 4),
        }

    # Roll up per family.
    by_family: dict[str, list[dict[str, Any]]] = {}
    for tid, summary in per_task.items():
        by_family.setdefault(summary["family"], []).append(summary)

    per_family: dict[str, dict[str, Any]] = {}
    for fam, summaries in sorted(by_family.items()):
        comps = [s["mean_composite"] for s in summaries]
        passes = [s["pass_rate"] for s in summaries]
        per_family[fam] = {
            "tasks":            len(summaries),
            "pass_rate":        round(_mean(passes), 4),
            "mean_composite":   round(_mean(comps), 4),
            "median_composite": round(_median(comps), 4),
        }

    # Overall.
    all_comps = [float(r["composite"]) for r in rows]
    all_passes = [1.0 if float(r["primitives"]["success"]) >= 100.0 else 0.0
                  for r in rows]
    overall = {
        "schema_version":   SCHEMA_VERSION,
        "tasks":             len(per_task),
        "total_runs":        len(rows),
        "pass_rate":         round(_mean(all_passes), 4),
        "mean_composite":    round(_mean(all_comps), 4),
        "median_composite":  round(_median(all_comps), 4),
        "stddev_composite":  round(_stddev(all_comps), 4),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "per_task":   per_task,
        "per_family": per_family,
        "overall":    overall,
    }


def aggregate_file(scores_json: Path,
                   family_map: dict[str, str] | None = None
                   ) -> dict[str, Any]:
    """Aggregate from a scores.json file written by ``score.py``.

    Accepts either a list of scored rows or an object with a
    ``results`` / ``scores`` key holding the list."""
    blob = json.loads(Path(scores_json).read_text())
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict):
        rows = blob.get("results") or blob.get("scores") or []
    else:
        rows = []
    return aggregate(rows, family_map=family_map)


# ---------------------------------------------------------------------------
# Comparison between two arms
# ---------------------------------------------------------------------------


def _pct_change(baseline: float, challenger: float) -> float | None:
    if baseline == 0:
        return None
    return round((challenger - baseline) / baseline * 100.0, 4)


def compare(baseline_scored: Iterable[dict[str, Any]],
            challenger_scored: Iterable[dict[str, Any]],
            family_map: dict[str, str] | None = None,
            baseline_arm: str = "baseline",
            challenger_arm: str = "challenger",
            ) -> dict[str, Any]:
    """Produce a SPEC §10 "compare" object comparing two arms.

    Headline fields: pass-rate lift (absolute + relative), coverage
    lift, token/time reductions, composite relative gain. Per-task
    and per-family breakdowns are included.
    """
    b_agg = aggregate(baseline_scored, family_map=family_map)
    c_agg = aggregate(challenger_scored, family_map=family_map)

    b_over = b_agg["overall"]
    c_over = c_agg["overall"]

    # Coverage / token / time means across all runs (overall signal,
    # not per-task), derived from the overall bucket via the per_task
    # means weighted by runs.
    def _weighted_mean(by_task: dict[str, dict[str, Any]], key: str) -> float:
        total_w = 0
        total = 0.0
        for s in by_task.values():
            w = int(s.get("runs", 0))
            if w <= 0:
                continue
            total_w += w
            total += w * float(s.get(key, 0.0))
        return total / total_w if total_w else 0.0

    b_cov = _weighted_mean(b_agg["per_task"], "mean_coverage")
    c_cov = _weighted_mean(c_agg["per_task"], "mean_coverage")
    b_tok = _weighted_mean(b_agg["per_task"], "median_tokens")
    c_tok = _weighted_mean(c_agg["per_task"], "median_tokens")
    b_time = _weighted_mean(b_agg["per_task"], "median_time_s")
    c_time = _weighted_mean(c_agg["per_task"], "median_time_s")

    def _reduction_pct(base: float, new: float) -> float:
        if base <= 0:
            return 0.0
        return round((base - new) / base * 100.0, 4)

    headline = {
        "pass_rate_baseline":          b_over["pass_rate"],
        "pass_rate_challenger":        c_over["pass_rate"],
        "pass_rate_lift_abs_points":   round(
            (c_over["pass_rate"] - b_over["pass_rate"]) * 100.0, 4),
        "pass_rate_lift_relative_pct": _pct_change(
            b_over["pass_rate"], c_over["pass_rate"]),
        "coverage_baseline_mean":      round(b_cov, 4),
        "coverage_challenger_mean":    round(c_cov, 4),
        "coverage_lift_abs_points":    round(c_cov - b_cov, 4),
        "token_reduction_pct":         _reduction_pct(b_tok, c_tok),
        "time_reduction_pct":          _reduction_pct(b_time, c_time),
        "composite_baseline":          b_over["mean_composite"],
        "composite_challenger":        c_over["mean_composite"],
        "composite_relative_gain_pct": _pct_change(
            b_over["mean_composite"], c_over["mean_composite"]),
    }

    # Per-task side-by-side.
    all_tasks = sorted(set(b_agg["per_task"]) | set(c_agg["per_task"]))
    per_task_rows = []
    for tid in all_tasks:
        b = b_agg["per_task"].get(tid)
        c = c_agg["per_task"].get(tid)
        per_task_rows.append({
            "task_id": tid,
            "family":  (b or c or {}).get("family", "unknown"),
            "baseline":   b,
            "challenger": c,
        })

    # Per-family side-by-side.
    all_fams = sorted(set(b_agg["per_family"]) | set(c_agg["per_family"]))
    per_family_rows = []
    for fam in all_fams:
        per_family_rows.append({
            "family":     fam,
            "baseline":   b_agg["per_family"].get(fam),
            "challenger": c_agg["per_family"].get(fam),
        })

    return {
        "schema_version":  SCHEMA_VERSION,
        "baseline_arm":    baseline_arm,
        "challenger_arm":  challenger_arm,
        "headline":        headline,
        "per_task":        per_task_rows,
        "per_family":      per_family_rows,
    }
