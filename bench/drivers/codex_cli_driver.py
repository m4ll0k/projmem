#!/usr/bin/env python3
"""Codex real-CLI driver — invokes `codex exec --json` against the
work directory and captures the completion + token usage.

Mirrors `claude_cli_driver.py` so A/B comparisons across the two
agents stay clean.

Two arms via the `PROJMEM_ARM` env var:

    PROJMEM_ARM=plain    → run codex with no projmem setup (baseline)
    PROJMEM_ARM=projmem  → `projmem init codex && projmem index` first,
                            then run codex. AGENTS.md + .codex/hooks.json
                            wire the agent to consult projmem.

Captures into the metrics line:
    tokens         — input+output tokens reported by Codex
    cost_usd       — when reported
    time_s         — wall clock
    arm            — `plain` or `projmem`
    model          — the model codex actually used

Env knobs:
    PROJMEM_ARM           : plain | projmem      (default plain)
    CODEX_BIN             : path to codex        (default `codex` on PATH)
    CODEX_MODEL           : -m override
    CODEX_TIMEOUT_SECS    : per-invocation timeout (default 600)
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
    sys.stderr.write("metrics: " + json.dumps(metrics) + "\n")
    sys.stderr.flush()


def _setup_projmem(work: Path) -> tuple[bool, str]:
    if not shutil.which("projmem"):
        return False, "projmem binary not found on PATH"
    try:
        r = subprocess.run(
            ["projmem", "--path", str(work), "init", "codex", "--reindex"],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"projmem init failed: {e}"
    if r.returncode != 0:
        return False, f"projmem init exit={r.returncode}: {r.stderr[:200]}"
    return True, ""


def _run_codex(work: Path, prompt: str, *,
               model: str | None,
               timeout: float) -> tuple[int, str, dict[str, Any]]:
    """Spawn codex exec in JSON mode. Returns (rc, claim, metrics)."""
    bin_path = os.environ.get("CODEX_BIN") or "codex"
    last_msg = work.parent / f".{work.name}.codex_last.txt"
    cmd: list[str] = [
        bin_path, "exec",
        "--json",
        "--cd", str(work),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-o", str(last_msg),
        prompt,
    ]
    if model:
        cmd[2:2] = ["-m", model]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", {"error": "codex_timeout",
                          "timeout_secs": timeout}
    except OSError as e:
        return 127, "", {"error": f"codex_exec: {e}"}

    metrics: dict[str, Any] = {"codex_rc": r.returncode}
    claim = ""
    if last_msg.is_file():
        try:
            claim = last_msg.read_text().strip()
        except OSError:
            claim = ""

    # Codex --json streams JSONL events to stdout; the LAST event with
    # a `usage` block carries the cumulative token count.
    in_tok = out_tok = 0
    model_used = None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Token usage fields vary by codex version; pick whichever
        # exists.
        usage = (ev.get("usage") or ev.get("token_usage")
                  or ev.get("response", {}).get("usage")
                  or {})
        if isinstance(usage, dict):
            for k in ("input_tokens", "prompt_tokens"):
                if k in usage:
                    try:
                        in_tok = max(in_tok, int(usage[k]))
                    except (TypeError, ValueError):
                        pass
            for k in ("output_tokens", "completion_tokens"):
                if k in usage:
                    try:
                        out_tok = max(out_tok, int(usage[k]))
                    except (TypeError, ValueError):
                        pass
        if "model" in ev and not model_used:
            model_used = ev["model"]
    if in_tok or out_tok:
        metrics["input_tokens"] = in_tok or None
        metrics["output_tokens"] = out_tok or None
        metrics["tokens"] = (in_tok or 0) + (out_tok or 0) or None
    if model_used:
        metrics["model"] = model_used
    return r.returncode, claim, metrics


def main() -> int:
    _self, task_id, work_dir, prompt_path = sys.argv
    t0 = time.time()
    work = Path(work_dir)
    arm = os.environ.get("PROJMEM_ARM", "plain").lower().strip()
    model = os.environ.get("CODEX_MODEL") or None
    timeout = float(os.environ.get("CODEX_TIMEOUT_SECS") or 600)

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
        prompt = (
            "This repository has projmem available. Run "
            "`projmem session <target>` BEFORE editing, and "
            "`projmem note add` AFTER concluding non-trivial facts. "
            "If `repo_memory.contradicted_count > 0`, STOP and "
            "investigate.\n\n"
            + prompt)

    rc, claim, run_metrics = _run_codex(
        work, prompt, model=model, timeout=timeout)
    metrics.update(run_metrics)
    metrics["time_s"] = round(time.time() - t0, 4)

    if claim:
        print(claim)
    else:
        print(f"(codex returned no parseable result; rc={rc})")
    _emit_metrics(metrics)
    return rc


if __name__ == "__main__":
    sys.exit(main())
