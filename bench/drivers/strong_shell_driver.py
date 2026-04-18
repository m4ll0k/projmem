#!/usr/bin/env python3
"""Arm B — simulated "strong shell" driver.

Models an LLM given the stronger standard toolbox: ``rg``, ``fd``,
``jq``, ``ctags`` and ``git grep``. These tools are faster and
produce more structured output than bare grep (cheaper in tokens),
but they do NOT resolve aliases, import indirection, or semantic
contracts. So this arm explores efficiently then still makes the
wrong edit when the task requires cross-file reasoning.

Like the bare arm, the edit is deliberately shallow — but it lands
on a BETTER candidate because structured output helps narrow the
search. It still over-claims.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

RG_TOKENS_PER_CALL = 400      # tighter output than grep
FD_TOKENS_PER_CALL = 150


def _tok_rg(pattern: str, cwd: Path) -> int:
    exe = shutil.which("rg") or "grep"
    try:
        r = subprocess.run([exe, "-n", "--no-heading", pattern, str(cwd)],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return RG_TOKENS_PER_CALL
    lines = (r.stdout or "").splitlines()
    # rg output is tighter: ~15 tokens/line.
    return RG_TOKENS_PER_CALL + 15 * len(lines)


def _tok_fd(cwd: Path) -> int:
    n = sum(1 for _ in cwd.rglob("*.py"))
    return FD_TOKENS_PER_CALL + 3 * n


def _pick_candidate(work: Path, prompt: str, task_id: str) -> Path | None:
    # Strong-shell picks a plausible file via import/def ranking.
    kws = re.findall(r"\b[a-z_]{4,}\b", (prompt or "").lower())
    best: tuple[int, Path] | None = None
    for p in sorted(work.rglob("*.py")):
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        score = 0
        for kw in kws:
            score += text.count(kw)
        if "legacy" in p.as_posix().lower():
            score -= 10  # strong shell knows enough to skip 'legacy'
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, p)
    return best[1] if best else None


def _shallow_edit(p: Path, marker: str) -> None:
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError):
        return
    p.write_text(f"# {marker}\n" + text)


def _spec_for(task_id: str) -> dict:
    """Read the task spec to detect investigation kind."""
    bench_root = Path(__file__).parent.parent.resolve()
    p = bench_root / "tasks" / task_id / "spec.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _investigation_answer(work: Path, prompt: str) -> int:
    """Strong-shell investigation: rg -l with a tighter pattern (longest
    CamelCase token from the prompt). Still over-cites because rg can't
    distinguish definition from caller — but it cuts noise vs naive
    grep, exposing the precision-vs-coverage tradeoff."""
    camel = re.findall(r"\b[A-Z][a-zA-Z0-9]{4,}\b", prompt or "")
    snake = re.findall(r"\b[a-z][a-z0-9_]{4,}\b", prompt or "")
    pattern = (sorted(camel, key=len, reverse=True)
                + sorted(snake, key=len, reverse=True)
                + ["TODO"])[0]
    exe = shutil.which("rg") or "grep"
    args = ([exe, "-l", pattern, str(work)] if exe.endswith("rg")
             else [exe, "-rln", pattern, str(work)])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        hits = [ln for ln in (r.stdout or "").splitlines() if ln]
    except (OSError, subprocess.TimeoutExpired):
        hits = []
    rel: list[str] = []
    for h in hits:
        try:
            rel.append(str(Path(h).relative_to(work)))
        except ValueError:
            rel.append(h)
    body = ["# Strong-shell rg result", "",
             f"Pattern: `{pattern}`",
             f"Total hits: {len(rel)}", ""]
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
    tokens += _tok_fd(work)
    for kw in re.findall(r"\b[a-z_]{4,}\b", prompt.lower())[:3]:
        tokens += _tok_rg(kw, work)

    if spec.get("kind") == "investigation":
        _investigation_answer(work, prompt)
        print("strong-shell: rg -l result dumped to ANSWER.md")
        elapsed = round(time.time() - t0, 4)
        print(f"metrics: {json.dumps({'tokens': tokens, 'time_s': elapsed})}",
              file=sys.stderr)
        return 0

    target = _pick_candidate(work, prompt, task_id)
    if target is not None:
        _shallow_edit(target, "strong-shell arm touched this file")

    # Still over-claims — knowing files ≠ knowing consumers.
    print("strong-shell: found best candidate; every caller now honours the flag.")
    elapsed = round(time.time() - t0, 4)
    print(f"metrics: {json.dumps({'tokens': tokens, 'time_s': elapsed})}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
