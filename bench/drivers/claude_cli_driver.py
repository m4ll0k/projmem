#!/usr/bin/env python3
"""Claude Code real-CLI driver — invokes `claude -p` against the work
directory and captures the completion + token usage.

Two arms via the `PROJMEM_ARM` env var (set by the harness wrapper or
the operator before invoking):

    PROJMEM_ARM=plain    → run claude with no projmem setup (baseline)
    PROJMEM_ARM=projmem  → `projmem init claude && projmem index` first,
                            then run claude. The CLAUDE.md drop +
                            settings.json hook are how the agent
                            learns to consult projmem.

Captures into the metrics line:
    tokens         — input+output tokens reported by Claude
    cost_usd       — cost reported by Claude (when available)
    time_s         — wall clock
    arm            — `plain` or `projmem`
    model          — the model claude actually used

Failure modes (any → driver_exit != 0, but we still emit the metrics
line so the grader has SOMETHING to score):
    - claude binary missing                → exit 127
    - claude returned non-zero              → exit code propagated
    - projmem init failed (projmem arm)     → exit 1
    - timeout exceeded (default 600 s)      → exit 124

Usage from the harness:
    bench/run.py run <task_id> <work_dir> --driver claude_cli_driver.py

The driver respects:
    PROJMEM_ARM            : plain | projmem    (default: plain)
    CLAUDE_BIN             : path to claude     (default: `claude` on PATH)
    CLAUDE_MODEL           : --model override
    CLAUDE_TIMEOUT_SECS    : per-invocation timeout (default 600)
    CLAUDE_MAX_BUDGET_USD  : --max-budget-usd guard (default 5.00)
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _emit_metrics(metrics: dict[str, Any]) -> None:
    """Per the bench protocol: last stderr line starting with `metrics:`
    is parsed as JSON. Always emit one even on failure."""
    sys.stderr.write("metrics: " + json.dumps(metrics) + "\n")
    sys.stderr.flush()


def _setup_projmem(work: Path) -> tuple[bool, str]:
    """Run `projmem init claude` then `projmem index` inside `work`.
    Returns (ok, message)."""
    if not shutil.which("projmem"):
        return False, "projmem binary not found on PATH"
    try:
        r = subprocess.run(
            ["projmem", "--path", str(work), "init", "claude", "--reindex"],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"projmem init failed: {e}"
    if r.returncode != 0:
        return False, f"projmem init exit={r.returncode}: {r.stderr[:200]}"
    return True, ""


def _run_claude(work: Path, prompt: str, *,
                model: str | None,
                timeout: float,
                budget_usd: float | None) -> tuple[int, str, dict[str, Any]]:
    """Spawn claude in headless JSON mode. Returns (rc, claim, metrics)."""
    bin_path = os.environ.get("CLAUDE_BIN") or "claude"
    cmd: list[str] = [
        bin_path, "-p",
        "--output-format", "json",
        "--add-dir", str(work),
        "--allow-dangerously-skip-permissions",
        prompt,
    ]
    if model:
        cmd[1:1] = ["--model", model]
    if budget_usd is not None and budget_usd > 0:
        cmd[1:1] = ["--max-budget-usd", f"{budget_usd:.2f}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(work))
    except subprocess.TimeoutExpired:
        return 124, "", {"error": "claude_timeout",
                          "timeout_secs": timeout}
    except OSError as e:
        return 127, "", {"error": f"claude_exec: {e}"}

    metrics: dict[str, Any] = {"claude_rc": r.returncode}
    claim = ""
    # Headless json output: we expect a single JSON object on stdout.
    text = (r.stdout or "").strip()
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            claim = text
        else:
            # Best-effort field plucking — Claude's headless schema
            # has evolved (`result`, `text`, `output`, `messages`).
            claim = (data.get("result")
                      or data.get("text")
                      or data.get("output")
                      or "")
            usage = (data.get("usage") or data.get("token_usage")
                      or {})
            if isinstance(usage, dict):
                metrics["input_tokens"] = usage.get("input_tokens")
                metrics["output_tokens"] = usage.get("output_tokens")
                if usage.get("input_tokens") is not None and \
                        usage.get("output_tokens") is not None:
                    metrics["tokens"] = (
                        int(usage["input_tokens"])
                        + int(usage["output_tokens"]))
            if "total_cost_usd" in data:
                metrics["cost_usd"] = data["total_cost_usd"]
            if "model" in data:
                metrics["model"] = data["model"]
    return r.returncode, claim, metrics


def main() -> int:
    _self, task_id, work_dir, prompt_path = sys.argv
    t0 = time.time()
    work = Path(work_dir)
    arm = os.environ.get("PROJMEM_ARM", "plain").lower().strip()
    model = os.environ.get("CLAUDE_MODEL") or None
    timeout = float(os.environ.get("CLAUDE_TIMEOUT_SECS") or 600)
    raw_budget = os.environ.get("CLAUDE_MAX_BUDGET_USD")
    budget = float(raw_budget) if raw_budget else 5.00

    try:
        prompt = Path(prompt_path).read_text()
    except OSError:
        prompt = ""

    metrics: dict[str, Any] = {
        "arm":     arm,
        "tokens":  None,
        "time_s":  None,
    }

    if arm == "projmem":
        ok, msg = _setup_projmem(work)
        if not ok:
            print(f"projmem setup failed: {msg}")
            metrics["error"] = msg
            metrics["time_s"] = round(time.time() - t0, 4)
            _emit_metrics(metrics)
            return 1
        # Augment the prompt with a SHORT toolbox primer. Tuned for
        # investigation-style tasks: agents that read this should call
        # `projmem reverse / symbol / search` before resorting to grep,
        # because the structural answer is usually already indexed.
        # Earlier versions told the agent to "run session before
        # editing" — confusing for investigation tasks where there is
        # no edit target.
        prompt = (
            "Tool available: `projmem` (a code-memory CLI in this "
            "repository).\n"
            "Useful subcommands for THIS task:\n"
            "  - `projmem reverse <file>` — who imports/depends on it\n"
            "  - `projmem symbol <name>` — where it's defined/used\n"
            "  - `projmem search <query>` — substring across notes/symbols/files\n"
            "  - `projmem session <target>` — full per-target bootstrap\n"
            "All commands accept `--json`. Prefer these over `grep -r` "
            "where applicable — they distinguish definitions from use "
            "sites and resolve aliases.\n\n"
            + prompt)

    rc, claim, run_metrics = _run_claude(
        work, prompt, model=model, timeout=timeout, budget_usd=budget)
    metrics.update(run_metrics)
    metrics["time_s"] = round(time.time() - t0, 4)

    if claim:
        print(claim)
    else:
        print(f"(claude returned no parseable result; rc={rc})")
    _emit_metrics(metrics)
    return rc


if __name__ == "__main__":
    sys.exit(main())
