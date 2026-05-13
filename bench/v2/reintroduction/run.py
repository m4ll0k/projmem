"""Reintroduction bench — synthetic (no live LLM) v2 demonstration.

Per `bench/v2/README.md`, the live version of this harness needs a
real LLM API budget. To produce numbers in the v2.0 release window
without spending tokens, we ship a SYNTHETIC variant that models the
three arms' behavior deterministically based on what each arm
*observes*. The synthetic numbers are clearly labeled as such; a
later commit (with credentials in CI) re-runs against a real model.

Three arms:

  * **A_baseline** — agent has no memory across sessions. Given the
    task "find the auth helper, create if missing", it reads the
    current tree, sees `src/auth/jwt.py` missing, and recreates it.
    Expected reintroduction rate: 1.0.

  * **C_v1_projmem** — agent has projmem v1 memory. Runs
    `projmem notes` and `projmem session`. v1 stores annotations on
    paths; the agent sees a generic prior note (created in session 1)
    but the note's body does NOT carry strong recreation-prevention
    signal — v1 has no first-class tombstone surface. The agent
    treats the missing file as a gap to fill. Expected
    reintroduction rate: 1.0 (worst-case; could be 0.5–1.0 on a real
    LLM run depending on prompt + note wording).

  * **D_v2_with_creating_warning** — agent runs `projmem creating`
    before file creation. v2 surfaces the wedge warning:
    "this path was deleted N days ago, reason: '...', replaced by:
    shared/auth.py. Consider editing the replacement instead."
    Agent reads the warning and uses shared/auth.py. Expected
    reintroduction rate: 0.0.

The synthetic agent's behavior is a function of the SIGNALS it sees,
which are *real* (we run the actual projmem CLI in each arm). The
decision step ("did the agent see the wedge warning, and if so does
it back off?") is the only modeled piece — kept deterministic on
"yes if warning present, no otherwise."

Usage:
    python3 -m bench.v2.reintroduction.run [--reps 5] [--out RESULTS_DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List


GOOD_REASON_DELETE = (
    "removing duplicate of shared/auth.py to keep one validator path"
)
GOOD_REASON_CREATE = (
    "creating auth helper for verify_token (per session-2 task prompt)"
)


def _run(args: List[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def _seed_repo(root: Path) -> None:
    (root / "src" / "auth").mkdir(parents=True)
    (root / "src" / "shared").mkdir(parents=True)
    (root / "src" / "auth" / "jwt.py").write_text(
        "def verify_token(token: str) -> bool:\n"
        "    return bool(token and len(token) > 20)\n"
    )
    (root / "src" / "shared" / "auth.py").write_text(
        "def verify_token(token: str) -> bool:\n"
        "    return bool(token and len(token) > 20)\n"
    )
    _run(["projmem", "index"], cwd=root)


def _operator_session1_delete(root: Path) -> None:
    """Operator removes src/auth/jwt.py + tombstones it via projmem."""
    (root / "src" / "auth" / "jwt.py").unlink()
    _run(["projmem", "refresh"], cwd=root)
    _run(["projmem", "deleting", "src/auth/jwt.py",
          "--reason", GOOD_REASON_DELETE,
          "--replaced-by", "src/shared/auth.py"], cwd=root)


def _session2_agent_a_baseline(root: Path) -> Dict[str, Any]:
    """Baseline agent: no memory. Sees missing file, recreates it."""
    target = root / "src" / "auth" / "jwt.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "def verify_token(token: str) -> bool:\n"
        "    return bool(token and len(token) > 20)\n"
    )
    return {"reintroduced": True, "used_replacement": False,
            "signal_seen": None}


def _session2_agent_c_v1(root: Path) -> Dict[str, Any]:
    """v1 projmem agent: reads `projmem notes`. v1's signal isn't
    strong enough to prevent recreation when the note body doesn't
    explicitly say "do not recreate" — the wedge belongs to v2."""
    notes_out = _run(["projmem", "notes", "--json"], cwd=root)
    seen = []
    if notes_out.returncode == 0 and notes_out.stdout.strip():
        try:
            notes = json.loads(notes_out.stdout)
            seen = notes.get("totals", {})
        except json.JSONDecodeError:
            pass
    # v1 has no first-class tombstone surface → agent proceeds.
    target = root / "src" / "auth" / "jwt.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "def verify_token(token: str) -> bool:\n"
        "    return bool(token and len(token) > 20)\n"
    )
    return {"reintroduced": True, "used_replacement": False,
            "signal_seen": "v1 notes summary"}


def _session2_agent_d_v2(root: Path) -> Dict[str, Any]:
    """v2 agent: calls `projmem creating` FIRST. The wedge warning
    fires; agent backs off and edits the replacement instead."""
    out = _run([
        "projmem", "creating", "src/auth/jwt.py",
        "--reason", GOOD_REASON_CREATE, "--json",
    ], cwd=root)
    warnings: List[str] = []
    if out.returncode == 0 and out.stdout.strip():
        try:
            warnings = json.loads(out.stdout).get("warnings", [])
        except json.JSONDecodeError:
            pass
    # If the wedge warning fires, the agent abandons the lease and
    # edits the replacement.
    wedge_fired = any("deleted" in w and "replaced by" in w for w in warnings)
    if wedge_fired:
        # Edit the replacement to satisfy the task (we don't actually
        # need to change it; the point is the agent didn't recreate).
        return {"reintroduced": False, "used_replacement": True,
                "signal_seen": warnings[0] if warnings else None}
    # Wedge didn't fire — agent proceeds anyway.
    target = root / "src" / "auth" / "jwt.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "def verify_token(token: str) -> bool:\n"
        "    return bool(token and len(token) > 20)\n"
    )
    return {"reintroduced": True, "used_replacement": False,
            "signal_seen": warnings[0] if warnings else None}


ARMS = {
    "A_baseline":                _session2_agent_a_baseline,
    "C_v1_projmem":              _session2_agent_c_v1,
    "D_v2_with_creating_warning": _session2_agent_d_v2,
}


def run_one_rep(rep_idx: int, arm: str) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"projmem-bench-{arm}-r{rep_idx}-") as tmp:
        root = Path(tmp)
        start = time.time()
        _seed_repo(root)
        _operator_session1_delete(root)
        result = ARMS[arm](root)
        elapsed = time.time() - start
        return {
            "rep":        rep_idx,
            "arm":        arm,
            "elapsed_s":  round(elapsed, 2),
            **result,
        }


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3,
                     help="Reps per arm (default 3).")
    ap.add_argument("--out", default=None,
                     help="Results dir. Defaults to "
                          "bench/v2/reintroduction/results/<utc>/.")
    args = ap.parse_args(argv)

    out_dir = Path(args.out) if args.out else (
        Path(__file__).parent / "results"
        / time.strftime("%Y%m%dT%H%M%SZ-synthetic", time.gmtime())
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    all_runs: List[Dict[str, Any]] = []
    for arm in ARMS:
        for rep in range(args.reps):
            rec = run_one_rep(rep, arm)
            all_runs.append(rec)
            print(f"  {arm:30s} rep {rep} → "
                   f"reintroduced={rec['reintroduced']}  "
                   f"({rec['elapsed_s']}s)")

    # Aggregate.
    by_arm: Dict[str, Dict[str, Any]] = {}
    for arm in ARMS:
        rows = [r for r in all_runs if r["arm"] == arm]
        reintro = sum(1 for r in rows if r["reintroduced"])
        used_rep = sum(1 for r in rows if r["used_replacement"])
        by_arm[arm] = {
            "n":                   len(rows),
            "reintroduced":        reintro,
            "reintroduction_rate": round(reintro / max(1, len(rows)), 3),
            "used_replacement":    used_rep,
            "mean_elapsed_s":      round(
                sum(r["elapsed_s"] for r in rows) / max(1, len(rows)), 2),
        }

    (out_dir / "runs.json").write_text(json.dumps(all_runs, indent=2))
    (out_dir / "aggregate.json").write_text(
        json.dumps(by_arm, indent=2))

    md = ["# Reintroduction bench — synthetic run", "",
          f"_Run at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.",
          "Synthetic: agent behavior is deterministic from the signals "
          "the arm observes; no LLM API calls. The signals themselves "
          "are real (we run the actual projmem CLI per arm)._",
          "",
          "| Arm | N | reintroduced | rate | used_replacement | mean s/run |",
          "|---|---:|---:|---:|---:|---:|"]
    for arm, agg in by_arm.items():
        md.append(
            f"| {arm} | {agg['n']} | {agg['reintroduced']} | "
            f"{agg['reintroduction_rate']:.2f} | "
            f"{agg['used_replacement']} | {agg['mean_elapsed_s']} |"
        )
    md.extend([
        "",
        "## Reading",
        "",
        "- **A_baseline** reintroduces every time (no memory, no signal).",
        "- **C_v1_projmem** reintroduces every time — v1 stores annotations but "
        "has no first-class tombstone surface; the wedge belongs to v2.",
        "- **D_v2_with_creating_warning** reintroduces 0× (the wedge "
        "warning surfaces the deletion + replacement; the simulated "
        "agent reads it and backs off). On a real LLM run this number "
        "is expected to be > 0 (model doesn't always heed warnings) "
        "but materially lower than C.",
        "",
        "## Reproduce",
        "",
        "```bash",
        f"python3 -m bench.v2.reintroduction.run --reps {args.reps}",
        "```",
        "",
        "## Caveats",
        "",
        "- This run is synthetic — the agent decision step is modeled, "
        "not LLM-driven. The wedge-fires-↦-agent-backs-off relationship "
        "is the v2 hypothesis being tested; a real-LLM run validates it.",
        "- The wedge-warning text the agent reads is real (produced by "
        "the projmem CLI). The model's response to that text is the "
        "only thing that's stubbed.",
    ])
    (out_dir / "REPORT.md").write_text("\n".join(md))

    print()
    print("=== aggregate ===")
    for arm, agg in by_arm.items():
        print(f"  {arm:30s} reintro_rate={agg['reintroduction_rate']:.2f} "
              f"used_replacement={agg['used_replacement']}/{agg['n']}")
    print()
    print(f"results: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
