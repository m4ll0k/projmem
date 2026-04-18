#!/usr/bin/env python3
"""Naive grep driver — simulates the failure mode of an LLM that
only has ``grep``: edits the FIRST file whose name matches a keyword
from the prompt, regardless of semantic correctness. It also
over-claims completion.

By SPEC.md §4 this driver MUST fail every well-formed task.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

KEYWORDS_PER_TASK = {
    # Per-task heuristic: the first filename-word that looks related
    # to the prompt. Deliberately wrong vs the correct target so we
    # prove the grader distinguishes right from "plausibly edited".
    "flag_propagation_orphan": "shadow",
    "same_name_decoy":         "rate",
}


def _pick_target(task_id: str, work: Path, prompt: str) -> Path | None:
    kw = KEYWORDS_PER_TASK.get(task_id)
    if not kw:
        tok = re.findall(r"\b[a-z_]{4,}\b", (prompt or "").lower())
        kw = tok[0] if tok else None
    if not kw:
        return None
    for p in sorted(work.rglob("*.py")):
        if kw in p.name.lower():
            return p
    return None


def _naive_touch(p: Path) -> None:
    """Drop a no-op comment at the top of the file — minimal diff."""
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError):
        return
    p.write_text("# naive grep driver touched this file\n" + text)


def _spec_for(task_id: str) -> dict:
    """Read the task spec — only used to detect investigation kind."""
    bench_root = Path(__file__).parent.parent.resolve()
    p = bench_root / "tasks" / task_id / "spec.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _ripgrep(pattern: str, root: Path) -> list[str]:
    """Best-effort `rg -l`. Falls back to a python regex walk so this
    driver still produces output on machines without ripgrep."""
    import subprocess as _sp
    try:
        r = _sp.run(["rg", "-l", pattern, str(root)],
                     capture_output=True, text=True, timeout=60)
        if r.returncode in (0, 1):
            return [ln for ln in r.stdout.splitlines() if ln]
    except (OSError, _sp.TimeoutExpired):
        pass
    out: list[str] = []
    pat = re.compile(pattern)
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            if pat.search(f.read_text()):
                out.append(str(f))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _investigation_answer(task_id: str, work: Path,
                           spec: dict, prompt: str) -> int:
    """Naive investigation strategy: extract a likely subject from the
    prompt, dump every rg-matched path into ANSWER.md. Deliberately
    over-cites (definition + every text mention) so the grader's
    max_matches checks fail — exactly the failure mode a grep-only
    LLM would produce on a same-name ambiguity question."""
    camel = re.findall(r"\b[A-Z][a-zA-Z0-9]{4,}\b", prompt or "")
    snake = re.findall(r"\b[a-z][a-z0-9_]{4,}\b", prompt or "")
    candidates = sorted(camel, key=len, reverse=True) + sorted(
        snake, key=len, reverse=True)
    pattern = candidates[0] if candidates else "TODO"
    hits = _ripgrep(pattern, work)
    rel_hits: list[str] = []
    for h in hits:
        try:
            rel_hits.append(str(Path(h).relative_to(work)))
        except ValueError:
            rel_hits.append(h)
    answer = work / "ANSWER.md"
    body = ["# Naive grep result", "",
             f"Pattern: `{pattern}`",
             f"Total file hits: {len(rel_hits)}", ""]
    for r in sorted(rel_hits):
        body.append(f"consumer: {r}")
    answer.write_text("\n".join(body) + "\n")
    print(f"naive_grep: dumped {len(rel_hits)} file path(s) to ANSWER.md")
    return 0


def main() -> int:
    _self, task_id, work_dir, prompt_path = sys.argv
    t0 = time.time()
    prompt = ""
    try:
        prompt = Path(prompt_path).read_text()
    except OSError:
        pass
    work = Path(work_dir)
    spec = _spec_for(task_id)
    if spec.get("kind") == "investigation":
        rc = _investigation_answer(task_id, work, spec, prompt)
        elapsed = round(time.time() - t0, 4)
        print(f"metrics: {json.dumps({'tokens': 0, 'time_s': elapsed})}",
              file=sys.stderr)
        return rc

    target = _pick_target(task_id, work, prompt)
    if target is not None:
        _naive_touch(target)
        print(f"edited {target.name}; all consumers updated.")
    else:
        print("nothing obvious found; assuming complete.")
    elapsed = round(time.time() - t0, 4)
    print(f"metrics: {json.dumps({'tokens': 0, 'time_s': elapsed})}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
