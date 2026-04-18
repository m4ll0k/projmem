"""Unit tests for bench/score.py — lock the SPEC.md §2 formulas.

Any edit that silently bends the weights or clamps is caught here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from bench.score import (
    DEFAULT_WEIGHTS,
    composite,
    score_coverage,
    score_precision,
    score_run,
    score_success,
    score_time_efficiency,
    score_token_efficiency,
    score_directory,
    compute_baseline,
)
from bench.aggregate import aggregate, compare


# ---- primitives ---------------------------------------------------------


def test_success_binary():
    assert score_success({"passed": True})  == 100.0
    assert score_success({"passed": False}) == 0.0
    assert score_success({})                == 0.0


def test_coverage_weighted_70_30():
    # 2/3 files, 1/2 regex  →  0.7·(2/3) + 0.3·(1/2) = 0.4667 + 0.15 = 0.6167
    g = {"details": {
        "required_files_total": 3,
        "required_files_touched": 2,
        "regex_checks_total": 2,
        "regex_checks_passed": 1,
    }}
    s = score_coverage(g) / 100.0
    assert abs(s - (0.7 * 2/3 + 0.3 * 0.5)) < 1e-6


def test_coverage_missing_denominator_is_one():
    # No regex checks → regex component defaults to 1.0 → pure file ratio.
    g = {"details": {"required_files_total": 2,
                     "required_files_touched": 1}}
    s = score_coverage(g) / 100.0
    assert abs(s - (0.7 * 0.5 + 0.3 * 1.0)) < 1e-6


def test_precision_penalty_scaling():
    # 1 decoy touched → precision = max(0, 1 − 0.5) = 0.5 → 50
    g = {"details": {"decoy_files_touched": 1,
                     "claim_violations": 0,
                     "extra_regex_over_matches": 0}}
    assert score_precision(g) == 50.0

    # 1 claim violation → 1 − 0.3 = 0.7 → 70
    g2 = {"details": {"decoy_files_touched": 0,
                      "claim_violations": 1,
                      "extra_regex_over_matches": 0}}
    assert score_precision(g2) == 70.0

    # Over-penalty is floored at 0.
    g3 = {"details": {"decoy_files_touched": 4,
                      "claim_violations": 4,
                      "extra_regex_over_matches": 4}}
    assert score_precision(g3) == 0.0


def test_efficiency_clamped_to_0_and_1():
    # Used exactly the baseline → efficiency 0 (no saving).
    eff, present = score_token_efficiency(1000, 1000)
    assert eff == 0.0 and present
    # Used HALF the baseline → efficiency 0.5 → 50.
    eff, _ = score_token_efficiency(500, 1000)
    assert eff == 50.0
    # Used MORE than baseline → efficiency clamped to 0.
    eff, _ = score_token_efficiency(1500, 1000)
    assert eff == 0.0
    # Used zero → efficiency 1.0 → 100.
    eff, _ = score_token_efficiency(0, 1000)
    assert eff == 100.0
    # Missing baseline → neutral 50 + present=False.
    eff, present = score_token_efficiency(100, None)
    assert eff == 50.0 and not present
    eff, present = score_time_efficiency(None, 1.0)
    assert eff == 50.0 and not present


# ---- composite ----------------------------------------------------------


def test_composite_weights_match_spec():
    # Weights from SPEC.md §2: 0.40/0.25/0.15/0.10/0.10 sum to 1.0
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
    assert DEFAULT_WEIGHTS["success"]          == 0.40
    assert DEFAULT_WEIGHTS["coverage"]         == 0.25
    assert DEFAULT_WEIGHTS["precision"]        == 0.15
    assert DEFAULT_WEIGHTS["token_efficiency"] == 0.10
    assert DEFAULT_WEIGHTS["time_efficiency"]  == 0.10


def test_composite_formula():
    prims = {"success": 100, "coverage": 80, "precision": 60,
             "token_efficiency": 50, "time_efficiency": 40}
    # 0.40·100 + 0.25·80 + 0.15·60 + 0.10·50 + 0.10·40 = 40+20+9+5+4 = 78
    assert composite(prims) == 78.0


def test_composite_rejects_zero_weights():
    with pytest.raises(ValueError):
        composite({"success": 100}, weights={"success": 0, "coverage": 0})


# ---- top-level score_run ------------------------------------------------


def test_score_run_passing_task_with_baseline():
    result = {
        "task_id": "t1",
        "driver":  "proj",
        "passed":  True,
        "details": {
            "required_files_total": 2, "required_files_touched": 2,
            "decoy_files_touched": 0,
            "regex_checks_total": 1, "regex_checks_passed": 1,
            "claim_violations": 0, "extra_regex_over_matches": 0,
        },
        "metrics": {"tokens": 500, "time_s": 1.0},
    }
    sr = score_run(result, baseline_tokens=1000, baseline_time_s=2.0)
    assert sr.primitives["success"]          == 100.0
    assert sr.primitives["coverage"]         == 100.0
    assert sr.primitives["precision"]        == 100.0
    assert sr.primitives["token_efficiency"] == 50.0
    assert sr.primitives["time_efficiency"]  == 50.0
    # 0.4·100 + 0.25·100 + 0.15·100 + 0.1·50 + 0.1·50 = 90
    assert abs(sr.composite - 90.0) < 1e-6
    assert sr.baseline_note == "baseline present"


def test_score_run_missing_baseline_neutral_50():
    result = {
        "task_id": "t1", "driver": "proj", "passed": True,
        "details": {
            "required_files_total": 1, "required_files_touched": 1,
            "decoy_files_touched": 0,
            "regex_checks_total": 0, "regex_checks_passed": 0,
            "claim_violations": 0, "extra_regex_over_matches": 0,
        },
        "metrics": {"tokens": 500, "time_s": 1.0},
    }
    sr = score_run(result)  # no baselines
    assert sr.primitives["token_efficiency"] == 50.0
    assert sr.primitives["time_efficiency"]  == 50.0
    assert sr.baseline_note == "baseline missing"


# ---- directory-level ----------------------------------------------------


def _write_result(dirp: Path, name: str, task_id: str,
                  passed: bool, tokens: int, time_s: float) -> Path:
    dirp.mkdir(parents=True, exist_ok=True)
    p = dirp / name
    p.write_text(json.dumps({
        "task_id": task_id,
        "driver":  dirp.name,
        "passed":  passed,
        "details": {
            "required_files_total":    2,
            "required_files_touched":  2 if passed else 0,
            "decoy_files_touched":     0,
            "regex_checks_total":      1,
            "regex_checks_passed":     1 if passed else 0,
            "claim_violations":        0,
            "extra_regex_over_matches": 0,
        },
        "metrics": {"tokens": tokens, "time_s": time_s},
    }))
    return p


def test_compute_baseline_median(tmp_path):
    _write_result(tmp_path, "a.json", "t1", False, 1000, 1.0)
    _write_result(tmp_path, "b.json", "t1", False, 2000, 2.0)
    _write_result(tmp_path, "c.json", "t1", False, 3000, 3.0)
    bt, btime = compute_baseline(list(tmp_path.glob("*.json")), task_id="t1")
    assert bt == 2000
    assert btime == 2.0


def test_score_directory_uses_baseline(tmp_path):
    base = tmp_path / "bare"
    chal = tmp_path / "projmem"
    _write_result(base, "r1.json", "t1", False, 4000, 4.0)
    _write_result(base, "r2.json", "t1", False, 4000, 4.0)
    _write_result(chal, "r1.json", "t1", True,  1000, 1.0)
    scored = score_directory(chal, baseline_dir=base)
    assert len(scored) == 1
    # Tokens 1000 vs baseline 4000 → efficiency = 1 − 0.25 = 0.75 → 75
    assert scored[0]["primitives"]["token_efficiency"] == 75.0
    assert scored[0]["primitives"]["time_efficiency"]  == 75.0


# ---- aggregate + compare -----------------------------------------------


def test_aggregate_rolls_up_per_task_and_family():
    rows = [
        {"task_id": "t1", "driver": "d",
         "primitives": {"success": 100, "coverage": 90, "precision": 100,
                        "token_efficiency": 50, "time_efficiency": 50},
         "composite": 90.0, "raw": {"metrics": {"tokens": 100, "time_s": 1}}},
        {"task_id": "t1", "driver": "d",
         "primitives": {"success": 0,   "coverage": 50, "precision": 100,
                        "token_efficiency": 50, "time_efficiency": 50},
         "composite": 40.0, "raw": {"metrics": {"tokens": 300, "time_s": 3}}},
    ]
    agg = aggregate(rows, family_map={"t1": "fam-a"})
    pt = agg["per_task"]["t1"]
    assert pt["runs"] == 2
    assert pt["pass_rate"] == 0.5
    assert pt["mean_composite"] == 65.0
    assert pt["median_composite"] == 65.0
    assert pt["family"] == "fam-a"
    assert agg["per_family"]["fam-a"]["tasks"] == 1
    assert agg["overall"]["total_runs"] == 2


def test_compare_headline_pass_rate_lift():
    base = [
        {"task_id": "t1", "driver": "bare",
         "primitives": {"success": 0, "coverage": 20, "precision": 100,
                        "token_efficiency": 50, "time_efficiency": 50},
         "composite": 20.0, "raw": {"metrics": {"tokens": 1000, "time_s": 5}}},
    ]
    chal = [
        {"task_id": "t1", "driver": "projmem",
         "primitives": {"success": 100, "coverage": 100, "precision": 100,
                        "token_efficiency": 75, "time_efficiency": 75},
         "composite": 95.0, "raw": {"metrics": {"tokens": 250, "time_s": 1.25}}},
    ]
    out = compare(base, chal, family_map={"t1": "fam-a"})
    h = out["headline"]
    assert h["pass_rate_baseline"]   == 0.0
    assert h["pass_rate_challenger"] == 1.0
    assert h["pass_rate_lift_abs_points"] == 100.0
    assert h["pass_rate_lift_relative_pct"] is None  # div-by-zero guard
    assert h["composite_baseline"]   == 20.0
    assert h["composite_challenger"] == 95.0
    assert h["composite_relative_gain_pct"] == 375.0
    assert h["token_reduction_pct"] > 0
    assert h["time_reduction_pct"] > 0
