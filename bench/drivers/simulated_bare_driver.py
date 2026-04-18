#!/usr/bin/env python3
"""Arm A — simulated "bare shell" driver.

Models an LLM whose only tools are ``grep``, ``find``, ``cat``,
``ls`` and raw file I/O. It spends tokens exploring, then makes a
shallow, often-wrong edit (same heuristic as the naive driver) and
over-claims in its completion text.

Token cost is simulated: each simulated shell call bills a fixed
number of "tokens" (prompt + response stand-in) so the
efficiency metric has something to compare.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

GREP_TOKENS_PER_CALL = 800    # expensive tool — dumps raw match lines
LS_TOKENS_PER_CALL = 200


def _tok_grep(pattern: str, cwd: Path) -> int:
    """Run grep purely to count how much the agent would see."""
    try:
        r = subprocess.run(
            ["grep", "-rnI", pattern, str(cwd)],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return GREP_TOKENS_PER_CALL
    lines = (r.stdout or "").splitlines()
    # Every match line ≈ ~30 tokens at typical widths.
    return GREP_TOKENS_PER_CALL + 30 * len(lines)


def _tok_ls(cwd: Path) -> int:
    n = sum(1 for _ in cwd.rglob("*"))
    return LS_TOKENS_PER_CALL + 5 * n


def _pick_wrong_target(work: Path, prompt: str) -> Path | None:
    tokens = re.findall(r"\b[a-z_]{4,}\b", (prompt or "").lower())
    kw = tokens[0] if tokens else None
    if not kw:
        return None
    # Deliberately pick the FIRST hit — the decoy gets touched.
    for p in sorted(work.rglob("*.py")):
        if kw in p.name.lower() or kw in p.as_posix().lower():
            return p
    return None


def _shallow_edit(p: Path) -> None:
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError):
        return
    p.write_text("# bare-arm touched this file (heuristic)\n" + text)


def _spec_for(task_id: str) -> dict:
    bench_root = Path(__file__).parent.parent.resolve()
    p = bench_root / "tasks" / task_id / "spec.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _investigation_answer(work: Path, prompt: str) -> int:
    """Bare-shell investigation: `grep -rln <kw>` and dump every
    matched path into ANSWER.md. Larger noise floor than rg → worse
    precision than strong-shell."""
    snake = re.findall(r"\b[a-z][a-z0-9_]{4,}\b", prompt or "")
    camel = re.findall(r"\b[A-Z][a-zA-Z0-9]{4,}\b", prompt or "")
    pattern = (sorted(camel, key=len, reverse=True)
                + sorted(snake, key=len, reverse=True)
                + ["TODO"])[0]
    try:
        r = subprocess.run(
            ["grep", "-rln", pattern, str(work)],
            capture_output=True, text=True, timeout=60)
        hits = [ln for ln in (r.stdout or "").splitlines() if ln]
    except (OSError, subprocess.TimeoutExpired):
        hits = []
    rel: list[str] = []
    for h in hits:
        try:
            rel.append(str(Path(h).relative_to(work)))
        except ValueError:
            rel.append(h)
    body = ["# Bare-shell grep result", "",
             f"Pattern: `{pattern}`", ""]
    for r2 in sorted(rel):
        body.append(f"consumer: {r2}")
    (work / "ANSWER.md").write_text("\n".join(body) + "\n")
    return 0


def main() -> int:
    _self, task_id, work_dir, prompt_path = sys.argv
    t0 = time.time()
    work = Path(work_dir)
    prompt = Path(prompt_path).read_text() if Path(prompt_path).exists() else ""
    spec = _spec_for(task_id)

    tokens = 0
    tokens += _tok_ls(work)
    for kw in re.findall(r"\b[a-z_]{4,}\b", prompt.lower())[:4]:
        tokens += _tok_grep(kw, work)

    if spec.get("kind") == "investigation":
        _investigation_answer(work, prompt)
        print("bare-arm: grep result dumped to ANSWER.md")
        elapsed = round(time.time() - t0, 4)
        print(f"metrics: {json.dumps({'tokens': tokens, 'time_s': elapsed})}",
              file=sys.stderr)
        return 0

    target = _pick_wrong_target(work, prompt)
    if target is not None:
        _shallow_edit(target)

    # Over-claim (trips must_not_claim heuristics in many tasks).
    print("bare-arm: edited matching file; all consumers updated.")
    elapsed = round(time.time() - t0, 4)
    print(f"metrics: {json.dumps({'tokens': tokens, 'time_s': elapsed})}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
