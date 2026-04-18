#!/usr/bin/env python3
"""bench/run.py — benchmark CLI harness.

Subcommands (see SPEC.md §5):

    list                 — list task ids
    show <task_id>       — dump a task's spec.json
    materialize <task_id> <work_dir>
                         — copy the task's seed/ tree into work_dir
    grade <task_id> <work_dir>
                         — grade an already-driven work_dir
    run <task_id> <work_dir>
                         — materialize + drive + grade in one shot
                           (optionally --repeats N, --output-dir DIR)
    matrix               — (task × driver × repeat) sweep
    score <results_dir>  — score every result json under results_dir
    aggregate <scores_json>
                         — roll up per-task/per-family/overall
    compare --baseline DIR --challenger DIR
                         — side-by-side, writes comparison.{json,md}

Every non-trivial output is JSON (or a markdown report) so scripts
can consume it downstream.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Make bench importable whether invoked as ``python3 bench/run.py`` or
# ``python3 -m bench.run`` — both paths are needed by the tests.
ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.grader import grade, build_family_map                 # noqa: E402
from bench.score import (                                        # noqa: E402
    DEFAULT_WEIGHTS, score_directory, score_run, compute_baseline)
from bench.aggregate import aggregate, compare as agg_compare    # noqa: E402

BENCH_DIR = Path(__file__).parent.resolve()
TASKS_DIR = BENCH_DIR / "tasks"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _task_dir(task_id: str) -> Path:
    p = TASKS_DIR / task_id
    if not p.is_dir() or not (p / "spec.json").is_file():
        raise SystemExit(f"bench: no such task '{task_id}' under {TASKS_DIR}")
    return p


def _materialize(task_id: str, dest: Path) -> None:
    """Materialize task `task_id` into `dest`.

    Two modes:
      - Synthetic task (`seed/` tree present): copytree into `dest`.
      - External-repo task (`spec.external_repo` set): clone/copy the
        real repo, optionally checkout a ref, optionally run a setup
        command. Then snapshot every file referenced by the grader
        (`required_files` / `decoy_files` / `regex_checks[*].file`) into
        `<dest>/.bench_baseline.json` so the grader can diff against
        the pristine state without re-cloning.
    """
    td = _task_dir(task_id)
    spec = json.loads((td / "spec.json").read_text())
    ext = spec.get("external_repo") or None

    if dest.exists():
        shutil.rmtree(dest)

    if ext is None:
        src = td / "seed"
        if not src.is_dir():
            raise SystemExit(f"bench: task '{task_id}' is missing seed/ "
                             f"tree and has no external_repo")
        shutil.copytree(src, dest)
        return

    _materialize_external_repo(task_id, spec, ext, dest)


def _materialize_external_repo(task_id: str, spec: dict[str, Any],
                               ext: dict[str, Any], dest: Path) -> None:
    """Populate `dest` from an external_repo spec."""
    dest.mkdir(parents=True, exist_ok=True)
    ref = ext.get("ref")
    setup_cmd = ext.get("setup")
    subset = list(ext.get("subset") or [])
    local_path = ext.get("path")
    url = ext.get("url")

    if local_path:
        local = Path(os.path.expanduser(local_path)).resolve()
        if not local.is_dir():
            raise SystemExit(
                f"bench: task '{task_id}' external_repo.path "
                f"{local_path!r} does not exist. Clone the repo to "
                f"that path or use `url` + network access.")
        _copy_from_local(local, dest, subset)
        if ref:
            _git_checkout(dest, ref)
    elif url:
        _git_clone(url, dest, ref)
    else:
        raise SystemExit(
            f"bench: task '{task_id}' external_repo needs `path` or `url`.")

    if setup_cmd:
        subprocess.run(setup_cmd, shell=True, cwd=str(dest),
                       check=False, timeout=600)

    _write_baseline_snapshot(spec, dest)


def _copy_from_local(src: Path, dest: Path, subset: list[str]) -> None:
    """Mirror `src` into `dest`. When `subset` is empty, copy everything;
    otherwise copy only the listed subdirectories/files (plus the
    .git dir so git operations work inside dest).

    We use rsync when available — faster on big trees (node, typescript)
    and honors --exclude patterns. Falls back to shutil.copytree.
    """
    if shutil.which("rsync"):
        cmd = ["rsync", "-a",
               "--exclude=.bench_baseline.json",
               "--exclude=node_modules/",
               "--exclude=.projmem/"]
        if subset:
            # rsync with --include-from-implied subset paths. We include
            # the subset paths plus .git (needed for `git checkout`).
            cmd += [f"--include=/{p}"
                    for p in ([".git/"] + subset)]
            cmd += [f"--include=/{p.rstrip('/')}/**"
                    for p in ([".git"] + subset)]
            cmd += ["--exclude=/*"]
        cmd += [str(src) + "/", str(dest) + "/"]
        subprocess.run(cmd, check=True, timeout=900)
        return
    # Fallback: pure-python copytree. Slower on big repos, no subset.
    if subset:
        for p in subset:
            s = src / p
            d = dest / p
            if s.is_dir():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(s, d, dirs_exist_ok=True)
            elif s.is_file():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
        # Also copy `.git` so optional checkout works.
        gdir = src / ".git"
        if gdir.is_dir() and not (dest / ".git").exists():
            shutil.copytree(gdir, dest / ".git")
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True,
                         ignore=shutil.ignore_patterns(
                             "node_modules", ".projmem",
                             ".bench_baseline.json"))


def _git_clone(url: str, dest: Path, ref: str | None) -> None:
    """Clone `url` into `dest`, optionally checking out `ref`. Shallow
    clone for speed when a ref is given; full clone otherwise."""
    cmd = ["git", "clone"]
    if ref:
        # Shallow fetch the specific ref — orders of magnitude faster
        # on big monorepos.
        cmd += ["--depth", "1", "--branch", ref]
    cmd += [url, str(dest)]
    subprocess.run(cmd, check=True, timeout=900)


def _git_checkout(dest: Path, ref: str) -> None:
    """Detach at `ref` inside `dest`. Non-fatal on failure so a local
    path without a matching ref still runs (we log and proceed)."""
    r = subprocess.run(
        ["git", "-C", str(dest), "checkout", "--detach", ref],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        sys.stderr.write(
            f"bench: warning: `git checkout {ref}` in {dest} failed: "
            f"{r.stderr.strip()[:200]}\n")


def _write_baseline_snapshot(spec: dict[str, Any], dest: Path) -> None:
    """Persist the pristine contents of every file the grader inspects,
    so it can compare the driver's post-edit state against the snapshot
    without re-materializing. Written as JSON so it's diffable and the
    grader can skip the `seed/` walk entirely."""
    watched: set[str] = set()
    watched.update(spec.get("required_files") or [])
    watched.update(spec.get("decoy_files") or [])
    watched.update(spec.get("must_delete_files") or [])
    for c in spec.get("regex_checks") or []:
        if c.get("file"):
            watched.add(c["file"])
    baseline: dict[str, Any] = {}
    for rel in sorted(watched):
        p = dest / rel
        if not p.is_file():
            baseline[rel] = None   # baseline: absent
            continue
        try:
            baseline[rel] = p.read_text()
        except (OSError, UnicodeDecodeError):
            baseline[rel] = None
    (dest / ".bench_baseline.json").write_text(
        json.dumps({"files": baseline,
                    "materialized_at": time.time()}, indent=2))


def _invoke_driver(driver_path: Path, task_id: str,
                   work_dir: Path,
                   spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Call the driver. A driver is a Python script that reads:

        argv: [driver_path, task_id, work_dir, prompt_path]

    ...and writes its completion_claim to stdout and metrics JSON to
    stderr's LAST line ("``metrics: {...}``"). We tolerate drivers
    that emit nothing — they just score 0 on efficiency.
    """
    prompt_path = work_dir.parent / f"{task_id}.prompt.txt"
    prompt_path.write_text(spec.get("prompt") or spec.get("description") or "")
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, str(driver_path), task_id,
         str(work_dir), str(prompt_path)],
        capture_output=True, text=True, timeout=600)
    elapsed = time.time() - t0
    claim = r.stdout.strip()
    metrics: dict[str, Any] = {"time_s": round(elapsed, 4)}

    # Last stderr line starting with "metrics:" carries driver JSON.
    tail = (r.stderr or "").strip().splitlines()
    if tail:
        last = tail[-1].strip()
        if last.startswith("metrics:"):
            try:
                parsed = json.loads(last[len("metrics:"):].strip())
                if isinstance(parsed, dict):
                    metrics.update(parsed)
                    metrics["time_s"] = round(elapsed, 4)  # keep wall clock
            except json.JSONDecodeError:
                pass
    metrics.setdefault("tokens", None)
    metrics["driver_exit"] = r.returncode
    return claim, metrics


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_list(_args: argparse.Namespace) -> int:
    if not TASKS_DIR.is_dir():
        print("[]")
        return 0
    ids = sorted(d.name for d in TASKS_DIR.iterdir()
                 if d.is_dir() and (d / "spec.json").is_file())
    print(json.dumps(ids, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    spec = json.loads((_task_dir(args.task_id) / "spec.json").read_text())
    print(json.dumps(spec, indent=2))
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    _materialize(args.task_id, Path(args.work_dir))
    print(json.dumps({"materialized": args.task_id,
                      "work_dir": str(args.work_dir)}, indent=2))
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    td = _task_dir(args.task_id)
    result = grade(td, Path(args.work_dir),
                   completion_claim=args.claim or "").to_dict()
    result["task_id"] = args.task_id
    result["driver"]  = args.driver_name or "unknown"
    out = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(out)
    print(out)
    return 0 if result["passed"] else 1


def _run_once(task_id: str, work_dir: Path,
              driver_path: Path,
              output_path: Path | None) -> dict[str, Any]:
    spec = json.loads((_task_dir(task_id) / "spec.json").read_text())
    _materialize(task_id, work_dir)
    claim, metrics = _invoke_driver(driver_path, task_id, work_dir, spec)
    g = grade(_task_dir(task_id), work_dir,
              completion_claim=claim,
              driver_metrics=metrics).to_dict()
    g["task_id"] = task_id
    g["driver"]  = driver_path.stem
    g["completion_claim"] = claim
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(g, indent=2))
    return g


def cmd_run(args: argparse.Namespace) -> int:
    driver_path = Path(args.driver).resolve()
    repeats = max(1, int(args.repeats or 1))
    out_dir = Path(args.output_dir) if args.output_dir else None

    last_exit = 0
    for i in range(1, repeats + 1):
        if repeats == 1:
            work = Path(args.work_dir)
            out_path = Path(args.output) if args.output else None
        else:
            work = Path(f"{args.work_dir}_run{i}")
            out_path = (out_dir / f"{args.task_id}_run{i}.json"
                        if out_dir else None)
        g = _run_once(args.task_id, work, driver_path, out_path)
        if repeats == 1:
            print(json.dumps(g, indent=2))
        else:
            # Multi-run: print a compact one-liner per run, full JSON to files.
            print(json.dumps({
                "task_id": g["task_id"], "driver": g["driver"],
                "run": i, "passed": g["passed"],
                "time_s": g.get("metrics", {}).get("time_s"),
            }))
            if not g["passed"]:
                last_exit = 1
    return last_exit


def _parse_drivers(raw: list[str]) -> list[tuple[str, Path]]:
    """``--drivers name:path`` list → [(name, path), ...]."""
    out: list[tuple[str, Path]] = []
    for entry in raw:
        if ":" not in entry:
            raise SystemExit(
                f"bench: --drivers expects name:path, got {entry!r}")
        name, path = entry.split(":", 1)
        out.append((name.strip(), Path(path).resolve()))
    return out


def cmd_matrix(args: argparse.Namespace) -> int:
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    drivers = _parse_drivers(args.drivers)
    out_root = Path(args.output_root)
    out_root.mkdir(parents=True, exist_ok=True)

    repeats = max(1, int(args.repeats or 1))
    summary: list[dict[str, Any]] = []
    for dname, dpath in drivers:
        d_out = out_root / dname
        d_out.mkdir(parents=True, exist_ok=True)
        for tid in tasks:
            for i in range(1, repeats + 1):
                work = out_root / f"_work/{dname}/{tid}_run{i}"
                work.parent.mkdir(parents=True, exist_ok=True)
                out_path = d_out / f"{tid}_run{i}.json"
                g = _run_once(tid, work, dpath, out_path)
                summary.append({
                    "driver": dname, "task": tid, "run": i,
                    "passed": g["passed"],
                    "time_s": g.get("metrics", {}).get("time_s"),
                })
                print(json.dumps(summary[-1]))
    (out_root / "matrix_summary.json").write_text(
        json.dumps({"runs": summary}, indent=2))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    baseline = Path(args.baseline_dir) if args.baseline_dir else None
    scored = score_directory(Path(args.results_dir), baseline_dir=baseline)
    blob = {"schema_version": 2, "results": scored}
    out = json.dumps(blob, indent=2)
    if args.output:
        Path(args.output).write_text(out)
    else:
        print(out)
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    blob = json.loads(Path(args.scores_json).read_text())
    rows = blob.get("results") if isinstance(blob, dict) else blob
    fam_map = build_family_map(TASKS_DIR)
    agg = aggregate(rows or [], family_map=fam_map)
    out = json.dumps(agg, indent=2)
    if args.output:
        Path(args.output).write_text(out)
    else:
        print(out)
    return 0


def _score_arm(results_dir: Path,
               baseline_dir: Path | None) -> list[dict[str, Any]]:
    return score_directory(results_dir, baseline_dir=baseline_dir)


def _render_comparison_md(cmp_blob: dict[str, Any]) -> str:
    h = cmp_blob["headline"]
    lines = [
        f"# Bench comparison — {cmp_blob['baseline_arm']} → "
        f"{cmp_blob['challenger_arm']}",
        "",
        "## Headline",
        "",
        f"- Pass rate: {h['pass_rate_baseline']:.2f} → "
        f"{h['pass_rate_challenger']:.2f} "
        f"(abs {h['pass_rate_lift_abs_points']:+.1f} pts, "
        f"rel {h['pass_rate_lift_relative_pct']}%)",
        f"- Coverage (mean): {h['coverage_baseline_mean']:.2f} → "
        f"{h['coverage_challenger_mean']:.2f} "
        f"(abs {h['coverage_lift_abs_points']:+.2f} pts)",
        f"- Composite: {h['composite_baseline']:.2f} → "
        f"{h['composite_challenger']:.2f} "
        f"(rel {h['composite_relative_gain_pct']}%)",
        f"- Token reduction: {h['token_reduction_pct']:.2f}%",
        f"- Time reduction:  {h['time_reduction_pct']:.2f}%",
        "",
        "## Per family",
        "",
        "| family | baseline pass | challenger pass | "
        "baseline composite | challenger composite |",
        "|---|---|---|---|---|",
    ]
    for row in cmp_blob["per_family"]:
        b = row["baseline"] or {}
        c = row["challenger"] or {}
        lines.append(
            f"| {row['family']} | "
            f"{b.get('pass_rate', 0):.2f} | {c.get('pass_rate', 0):.2f} | "
            f"{b.get('mean_composite', 0):.2f} | "
            f"{c.get('mean_composite', 0):.2f} |")
    lines += [
        "",
        "## Per task",
        "",
        "| task | family | base pass | chal pass | "
        "base comp | chal comp |",
        "|---|---|---|---|---|---|",
    ]
    for row in cmp_blob["per_task"]:
        b = row["baseline"] or {}
        c = row["challenger"] or {}
        lines.append(
            f"| {row['task_id']} | {row['family']} | "
            f"{b.get('pass_rate', 0):.2f} | "
            f"{c.get('pass_rate', 0):.2f} | "
            f"{b.get('mean_composite', 0):.2f} | "
            f"{c.get('mean_composite', 0):.2f} |")
    return "\n".join(lines) + "\n"


def cmd_compare(args: argparse.Namespace) -> int:
    b_dir = Path(args.baseline)
    c_dir = Path(args.challenger)
    b_scored = _score_arm(b_dir, baseline_dir=b_dir)
    c_scored = _score_arm(c_dir, baseline_dir=b_dir)  # eff vs baseline
    fam_map = build_family_map(TASKS_DIR)
    blob = agg_compare(b_scored, c_scored,
                       family_map=fam_map,
                       baseline_arm=b_dir.name or "baseline",
                       challenger_arm=c_dir.name or "challenger")
    out_json = json.dumps(blob, indent=2)
    if args.output:
        out_path = Path(args.output)
        if out_path.suffix == ".md":
            out_path.write_text(_render_comparison_md(blob))
            sidecar = out_path.with_suffix(".json")
            sidecar.write_text(out_json)
        else:
            out_path.write_text(out_json)
    else:
        print(out_json)
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench/run.py",
                                description="projmem benchmark harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list task ids")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="dump a task's spec.json")
    sp.add_argument("task_id")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("materialize", help="copy seed/ into work_dir")
    sp.add_argument("task_id")
    sp.add_argument("work_dir")
    sp.set_defaults(func=cmd_materialize)

    sp = sub.add_parser("grade", help="grade an existing work_dir")
    sp.add_argument("task_id")
    sp.add_argument("work_dir")
    sp.add_argument("--claim", default="",
                    help="driver's completion_claim text")
    sp.add_argument("--driver-name", default="manual")
    sp.add_argument("--output")
    sp.set_defaults(func=cmd_grade)

    sp = sub.add_parser("run", help="materialize + drive + grade")
    sp.add_argument("task_id")
    sp.add_argument("work_dir")
    sp.add_argument("--driver", required=True)
    sp.add_argument("--repeats", type=int, default=1)
    sp.add_argument("--output")
    sp.add_argument("--output-dir")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("matrix", help="task × driver × repeat sweep")
    sp.add_argument("--tasks", required=True,
                    help="comma-separated task ids")
    sp.add_argument("--drivers", action="append", required=True,
                    help="name:/path/to/driver.py (repeatable)")
    sp.add_argument("--repeats", type=int, default=1)
    sp.add_argument("--output-root", required=True)
    sp.set_defaults(func=cmd_matrix)

    sp = sub.add_parser("score", help="score every result JSON in a dir")
    sp.add_argument("results_dir")
    sp.add_argument("--baseline-dir",
                    help="Arm-A results dir used for efficiency baselines")
    sp.add_argument("--output")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("aggregate", help="aggregate a scores.json")
    sp.add_argument("scores_json")
    sp.add_argument("--output")
    sp.set_defaults(func=cmd_aggregate)

    sp = sub.add_parser("compare", help="compare two arms")
    sp.add_argument("--baseline", required=True)
    sp.add_argument("--challenger", required=True)
    sp.add_argument("--output")
    sp.set_defaults(func=cmd_compare)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
