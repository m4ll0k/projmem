#!/usr/bin/env python3
"""bench/multisession/run.py — multi-session benchmark harness.

Single executable that runs ONE multisession task across N arms,
killing the agent process between sessions and applying real drift
events (git-apply patches) to the working tree. Designed to surface
the "did the agent remember session N-1?" signal that single-session
harnesses can't see.

Arms:
  A  baseline       no projmem, no scratchpad, fresh agent every session
  C  projmem        `.projmem/` survives between sessions
  D  scratchpad     `notes.md` (free-form) survives between sessions

Each (task, arm) gets a fresh COW copy of the source repo. Between
sessions: the agent process is killed, the prompt cache is defeated
(unique --session-id), the next drift_events item is `git apply`d,
and a fresh agent is spawned with the next session's prompt. Nothing
else carries over — only the persistence-layer files survive.

Outputs: `results/<task>__<arm>__<rep>/run.json` with per-session
metrics (tokens, time, rc, claim text), drift_caught booleans, and
the agent's final artifact.
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

ROOT = Path(__file__).resolve().parent
BENCH_ROOT = ROOT.parent
REPO_ROOT = BENCH_ROOT.parent


def _log(msg: str) -> None:
    """Tee progress to stderr so a long run is observable."""
    sys.stderr.write(f"[multisession] {msg}\n")
    sys.stderr.flush()


def _materialize(source_repo: Path, work: Path) -> None:
    """Copy source_repo → work, excluding .git history (we re-init for
    apply support but throw away history). Copy-on-write semantics
    via straight cp -R; .git is re-initialized so git apply works
    against a clean baseline."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(source_repo, work, symlinks=False,
                     ignore=shutil.ignore_patterns(
                         ".git", "node_modules", "__pycache__",
                         ".pytest_cache", ".mypy_cache", "dist",
                         "build", ".tox", "*.egg-info",
                         # Strip every projmem trace from the source —
                         # an earlier benchmark run on the same source
                         # repo had left `projmem-out/` (graph.svg /
                         # report.md / etc.) behind, and baseline
                         # agents inferred "projmem must be available"
                         # and self-installed it. Now baseline truly
                         # has no projmem hint inside the working tree.
                         ".projmem", "projmem-out",
                         "CLAUDE.md", "AGENTS.md", ".cursorrules"))
    # No git init — drift is applied via POSIX `patch` so `.git/` is
    # never present, which means agents can't `git log` their way
    # back to the prior state.


def _setup_arm(arm: str, work: Path) -> dict[str, Any]:
    """Set up persistence layer for the arm. Returns metadata about
    what was done (for the result blob)."""
    info: dict[str, Any] = {"arm": arm}
    if arm == "A":
        # Strip any agent-instruction file the test repo might already
        # carry, so the BASELINE arm doesn't accidentally see projmem
        # hints. Each Claude session starts cold.
        for fname in ("CLAUDE.md", "AGENTS.md", ".cursorrules"):
            fp = work / fname
            if fp.exists():
                fp.unlink()
        info["persistence"] = "none"
    elif arm == "C":
        # Run `projmem init claude --reindex` so the agent sees the
        # AGENTS.md / CLAUDE.md primer + a fresh index.
        r = subprocess.run(
            ["projmem", "--path", str(work),
              "init", "claude", "--reindex"],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(
                f"projmem init failed: rc={r.returncode} "
                f"stderr={r.stderr[:300]}")
        info["persistence"] = "projmem"
        info["projmem_init_stdout_preview"] = (r.stdout or "")[:200]
    elif arm == "D":
        # Free-form scratchpad. Strip projmem hints (no .projmem dir,
        # no CLAUDE.md mentioning it). Drop a tiny notes.md primer.
        for fname in ("CLAUDE.md", "AGENTS.md", ".cursorrules"):
            fp = work / fname
            if fp.exists():
                fp.unlink()
        (work / "notes.md").write_text(
            "# Agent notes — append findings here.\n"
            "# This file persists between sessions; nothing else does.\n"
        )
        info["persistence"] = "scratchpad"
    else:
        raise ValueError(f"unknown arm {arm!r}")
    return info


def _build_dynamic_patch(work: Path,
                           dynamic: dict[str, Any]) -> tuple[str, str]:
    """Generate a unified-diff patch at runtime by mutating the file
    in-place under a temp copy and `diff -u`'ing.

    Currently supports kind=`delete_trailing_setupmethod_blocks`:
    deletes the trailing N decorated `@setupmethod` method blocks
    from the named file. A "block" is one `@setupmethod` line plus
    the contiguous indented body that follows, until the next
    `@setupmethod`, top-level def, or class boundary.

    Returns (patch_text, summary).
    """
    rel = dynamic["file"]
    full = work / rel
    kind = dynamic["kind"]
    # Round-2 drift kinds for the factcheck task. Direct file mutations
    # rather than diff generation; we still return a "patch" string for
    # logging consistency, but apply via the stdlib (no `patch` cmd).
    if kind == "create_file":
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(dynamic["content"])
        return ("(create_file: " + rel + ")",
                f"created {rel} ({len(dynamic['content'])} bytes)")
    if kind == "append_text":
        if not full.exists():
            raise FileNotFoundError(f"drift target missing: {full}")
        with open(full, "a", encoding="utf-8") as f:
            f.write(dynamic["content"])
        return ("(append_text: " + rel + ")",
                f"appended {len(dynamic['content'])} bytes to {rel}")
    if kind == "delete_file":
        if not full.exists():
            raise FileNotFoundError(f"drift target missing: {full}")
        full.unlink()
        return ("(delete_file: " + rel + ")",
                f"deleted file {rel}")
    if kind == "delete_python_def":
        if not full.exists():
            raise FileNotFoundError(f"drift target missing: {full}")
        name = dynamic["name"]
        src = full.read_text()
        lines = src.splitlines(keepends=True)
        # Find `def NAME(` at column 0 (top-level def).
        target = -1
        for i, ln in enumerate(lines):
            if ln.startswith(f"def {name}(") or ln.startswith(
                    f"def {name} ("):
                target = i
                break
        if target < 0:
            raise RuntimeError(
                f"`def {name}(` not found at top level in {rel}")
        # Walk forward to find the end of the def block. Stop at the
        # next top-level def/class or EOF.
        end = len(lines)
        for j in range(target + 1, len(lines)):
            ln = lines[j]
            if ln and not ln[0].isspace() and ln.strip():
                end = j
                break
        del lines[target:end]
        full.write_text("".join(lines))
        return ("(delete_python_def: " + name + " from " + rel + ")",
                f"deleted def {name} from {rel} (lines {target+1}..{end})")
    if not full.exists():
        raise FileNotFoundError(f"drift target missing: {full}")
    if kind != "delete_trailing_setupmethod_blocks":
        raise NotImplementedError(
            f"unknown dynamic drift kind: {kind!r}")
    src = full.read_text()
    lines = src.splitlines(keepends=True)
    # Find every line index (0-based) that holds `@setupmethod`.
    deco_idx = [i for i, ln in enumerate(lines)
                 if ln.lstrip().startswith("@setupmethod")]
    if len(deco_idx) < dynamic["count"]:
        raise RuntimeError(
            f"only {len(deco_idx)} @setupmethod blocks; need "
            f"{dynamic['count']} to delete")
    # Pick the trailing N. For each, the block runs from the
    # @setupmethod line through the END of the method body. We
    # stop at the next @setupmethod or the next line that's
    # outdented to ≤ class level (4 spaces here).
    targets = deco_idx[-dynamic["count"]:]
    # Compute slice (start, end) for each target. Sort, then walk.
    deletions: list[tuple[int, int]] = []
    for ti in targets:
        end = len(lines)
        # Walk forward; stop at next decorated method or outdent.
        for j in range(ti + 1, len(lines)):
            ln = lines[j]
            stripped = ln.lstrip()
            if stripped.startswith("@") and not ln.startswith(" " * 8):
                end = j
                break
            if (ln.strip() == "" and j + 1 < len(lines)
                    and not lines[j + 1].startswith(" ")):
                end = j + 1
                break
            # Outdent to class level (4 spaces, no indent under)?
            if ln.startswith("class ") or (ln and not ln[0].isspace()
                                              and ln.strip()):
                end = j
                break
        deletions.append((ti, end))
    # Apply deletions back-to-front so indices stay valid.
    new_lines = list(lines)
    for s, e in sorted(deletions, key=lambda x: -x[0]):
        del new_lines[s:e]
    new_src = "".join(new_lines)
    # Build a unified diff manually with stable headers.
    import difflib as _difflib
    patch_lines = list(_difflib.unified_diff(
        lines, new_lines,
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
    return "".join(patch_lines), (
        f"deleted {dynamic['count']} trailing @setupmethod blocks "
        f"from {rel} (lines {[t+1 for t in targets]})")


def _apply_drift(work: Path, patch_text: str,
                  description: str) -> dict[str, Any]:
    """Apply a unified diff via the POSIX `patch` command (no git
    needed). Decoupled from `git apply` so we can strip `.git/` from
    the working tree to defeat the git-log cheat without breaking
    drift application. Returns {applied, error, stderr}."""
    p = subprocess.run(
        ["patch", "-p1", "--no-backup-if-mismatch", "--silent"],
        input=patch_text, capture_output=True, text=True,
        cwd=str(work))
    out: dict[str, Any] = {
        "drift_description": description,
        "rc":                p.returncode,
        "stderr":            (p.stderr or "")[:500],
        "stdout":            (p.stdout or "")[:500],
    }
    out["applied"] = (p.returncode == 0)
    return out


def _kill_orphan_claude_processes() -> None:
    """Defensive cleanup — if a previous session's claude is still
    running on this user, kill it so the next session starts cold.
    Errors silently because the user may not have any."""
    try:
        subprocess.run(["pkill", "-f", "claude --print"],
                        capture_output=True, timeout=5)
    except Exception:
        pass


def _isolate_persistence(work: Path, arm: str,
                          source_repo: Path,
                          drifts_to_reapply: list) -> None:
    """Between-session cleanup so agent-created files don't leak.

    Round-7-bench-followup: arm A scored 1/2 on the drift-check task
    by self-creating a `notes.md` in session 1 that survived to
    session 2 (the work tree persists across sessions for ALL arms;
    nothing was deleting it). Now between sessions we:

      1. Save the canonical persistence files for this arm:
         - arm C: `.projmem/`
         - arm D: `notes.md`
         - arm A: nothing
      2. Re-materialize the work tree from the source repo.
      3. Re-apply the drift events that happened so far.
      4. Restore the saved persistence files.

    Result: the agent's session 2 sees a clean post-drift tree PLUS
    only the persistence-layer file its arm is supposed to have.
    """
    import tempfile
    save_root = Path(tempfile.mkdtemp(prefix="msm_persist_"))
    saved: list[tuple[Path, Path]] = []
    if arm == "C" and (work / ".projmem").exists():
        dst = save_root / ".projmem"
        shutil.copytree(work / ".projmem", dst, symlinks=False)
        saved.append((dst, work / ".projmem"))
    if arm == "D" and (work / "notes.md").exists():
        dst = save_root / "notes.md"
        shutil.copyfile(work / "notes.md", dst)
        saved.append((dst, work / "notes.md"))
    # Re-materialize fresh.
    _materialize(source_repo, work)
    _setup_arm(arm, work)
    # Re-apply drifts that have been applied so far.
    for d in drifts_to_reapply:
        if d.get("dynamic_kind"):
            # Reconstruct via _build_dynamic_patch — need original
            # spec entry. Caller passes us the reconstituted spec
            # entry directly via `drifts_to_reapply`.
            patch_text, _ = _build_dynamic_patch(work, d["_spec"])
            if not patch_text.startswith("("):
                _apply_drift(work, patch_text, d.get("drift_description", ""))
        elif d.get("_spec_static"):
            _apply_drift(work, d["_spec_static"]["patch"],
                          d.get("drift_description", ""))
    # Restore saved persistence.
    for src, dst in saved:
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=False)
        else:
            shutil.copyfile(src, dst)
    shutil.rmtree(save_root, ignore_errors=True)
    # Arm C: refresh the structural index so the verifier sees
    # post-drift symbols when revalidating the restored notes. The
    # notes/annotations table is preserved (separate lifecycle).
    if arm == "C":
        subprocess.run(
            ["projmem", "--path", str(work), "index", "--force"],
            capture_output=True, timeout=300)


def _strip_git_history(work: Path) -> None:
    """Defeat the `git log` cheat. After applying drift we ALSO strip
    `.git/` from the working tree so an agent can't `git log -p` its
    way back to the prior state. This forces session 2 to actually
    rely on whatever persistence layer its arm provides — projmem
    state, scratchpad notes, or nothing.

    Without this, every arm scored full_recall by reading git log
    from inside the materialised tree (the harness committed the
    drift as a separate commit). The git history WAS the prior state.
    Now removed before the agent ever touches the tree.
    """
    git_dir = work / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)


def _path_for_arm(arm: str) -> str:
    """Return a PATH the agent should see for this arm.

    Arms A and D MUST NOT have `projmem` reachable on PATH — otherwise
    the agent self-installs it and pollutes the baseline (verified
    real on the factcheck round 1: arm A scored 5/5 because the agent
    ran `projmem init` of its own accord). We strip the directory
    holding the projmem binary from PATH for those arms; arm C keeps
    the full PATH.
    """
    full_path = os.environ.get("PATH", "")
    if arm == "C":
        return full_path
    pm_bin = shutil.which("projmem")
    if not pm_bin:
        return full_path
    pm_dir = os.path.dirname(pm_bin)
    parts = [p for p in full_path.split(":") if p and p != pm_dir]
    return ":".join(parts)


def _run_session(work: Path, prompt: str, *,
                  session_idx: int,
                  timeout: float = 240.0,
                  budget_usd: float = 1.50,
                  model: str | None = None,
                  arm: str = "A"
                  ) -> dict[str, Any]:
    """Run one Claude session. Returns metrics + claim text.

    Note on cache defeat: each spawn is a fresh `claude` process with
    no carried session id. That alone defeats the in-process cache;
    Anthropic's server-side prompt cache may still hit but we count
    real wall-clock and tokens on Claude's side, not ours.
    """
    bin_path = os.environ.get("CLAUDE_BIN") or "claude"
    cmd: list[str] = [
        bin_path, "-p",
        "--output-format", "json",
        "--add-dir", str(work),
        # `bypassPermissions` is the documented mode for full
        # auto-approval. The deprecated `--allow-dangerously-skip-
        # permissions` flag was treated as advisory by the model in
        # this CLI version — Bash and Write calls were still blocked
        # per the agent's perception, silently breaking the projmem
        # / scratchpad arms in earlier runs.
        "--permission-mode", "bypassPermissions",
        prompt,
    ]
    if model:
        cmd[1:1] = ["--model", model]
    if budget_usd:
        cmd[1:1] = ["--max-budget-usd", f"{budget_usd:.2f}"]
    t0 = time.time()
    env = dict(os.environ)
    env["PATH"] = _path_for_arm(arm)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout, cwd=str(work),
                            env=env)
    except subprocess.TimeoutExpired:
        return {"session_idx": session_idx, "rc": 124,
                "claim": "", "time_s": round(time.time() - t0, 2),
                "error": "claude_timeout"}
    elapsed = round(time.time() - t0, 2)
    metrics: dict[str, Any] = {
        "session_idx": session_idx,
        "rc":          r.returncode,
        "time_s":      elapsed,
        "stdout_len":  len(r.stdout or ""),
        "stderr_len":  len(r.stderr or ""),
    }
    text = (r.stdout or "").strip()
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            metrics["claim"] = text[:8000]
        else:
            metrics["claim"] = (
                data.get("result") or data.get("text")
                or data.get("output") or "")[:8000]
            usage = data.get("usage") or data.get("token_usage") or {}
            if isinstance(usage, dict):
                metrics["input_tokens"] = usage.get("input_tokens")
                metrics["output_tokens"] = usage.get("output_tokens")
                if (usage.get("input_tokens") is not None
                        and usage.get("output_tokens") is not None):
                    metrics["tokens"] = (int(usage["input_tokens"])
                                         + int(usage["output_tokens"]))
            if "total_cost_usd" in data:
                metrics["cost_usd"] = data["total_cost_usd"]
            if "model" in data:
                metrics["model"] = data["model"]
    else:
        metrics["claim"] = ""
        metrics["raw_stderr_tail"] = (r.stderr or "")[-300:]
    return metrics


def _grade(spec: dict[str, Any],
            sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Grade the run mechanically.

    Two grader profiles, picked by which keys the spec carries:

    1) Keyword grader (legacy): `must_mention_in_final` /
       `forbidden_in_final` / `drift_caught_keywords`.

    2) Inventory-comparison grader: `expected_prior_count`,
       `expected_current_count`, `expected_removed_methods`. Reads the
       agent's `PRIOR_COUNT:` / `CURRENT_COUNT:` / `REMOVED_LINES:`
       lines and scores accuracy. Recognises the truthful
       `PRIOR_COUNT: NO_RECORD` admission as a distinct outcome
       (`memory_admit_truthful`) so we don't conflate "agent was
       honest about no memory" with "agent failed".
    """
    final = (sessions[-1]["claim"] if sessions else "") or ""
    g = spec["grading"]

    # Profile 3: per-claim verdict comparison (factcheck task).
    if "expected_verdicts" in g:
        return _grade_factcheck(final, g)
    # Profile 2: inventory comparison.
    if "expected_prior_count" in g:
        return _grade_inventory(final, g)

    # Profile 1: keyword.
    final_lower = final.lower()
    must_mention   = g.get("must_mention_in_final") or []
    forbidden      = g.get("forbidden_in_final") or []
    drift_keywords = g.get("drift_caught_keywords") or []
    mentions = {kw: (kw.lower() in final_lower) for kw in must_mention}
    forbidden_hits = {kw: (kw.lower() in final_lower)
                      for kw in forbidden}
    drift_caught = any(kw.lower() in final_lower
                        for kw in drift_keywords)
    coverage_pct = (sum(1 for v in mentions.values() if v)
                     / max(1, len(mentions)) * 100.0)
    precision_pct = (sum(1 for v in forbidden_hits.values() if not v)
                      / max(1, len(forbidden_hits)) * 100.0
                      if forbidden_hits else 100.0)
    return {
        "grader":                "keyword",
        "drift_caught":          drift_caught,
        "must_mention":          mentions,
        "must_mention_coverage": round(coverage_pct, 1),
        "forbidden_hits":        forbidden_hits,
        "precision_pct":         round(precision_pct, 1),
        "final_answer_length":   len(final),
        "final_answer_preview":  final[:1500],
    }


def _grade_factcheck(final: str, g: dict[str, Any]) -> dict[str, Any]:
    """Per-claim verdict grader for the multisession_factcheck_drift
    family. Parses `Cn: <VERDICT> | current_location: <loc>` lines and
    compares to spec.expected_verdicts. The decoy_check captures the
    common failure mode: agent reports VERIFIED for the moved claim
    because it found the decoy under the same name in a different
    file."""
    import re as _re
    expected = g["expected_verdicts"]
    decoy    = g.get("decoy_check") or {}
    # Match `Cn: <VERDICT> | current_location: <loc>` while tolerating
    # markdown decorations the agent may sprinkle around the verdict
    # token: `**REFUTED**`, `*MOVED*`, `_VERIFIED_`, `<VERIFIED>`, etc.
    # Original regex `^\s*(C\d+):\s*([A-Z_]+)` failed silently when
    # Sonnet bolded the verdict, scoring an otherwise-correct answer
    # as 0/5 (round-1 factcheck arm C rep1 false negative).
    line_rx = _re.compile(
        r"^\s*[*_>\-`]*\s*([A-Z]\d+)[*_`]*\s*:\s*"
        r"[*_`<]*\s*([A-Z_]+)(?=\s|[*_`>|\n]|$)\s*[*_`>]*"
        r"(?:\s*\|\s*current_location:\s*([^\n]+))?",
        _re.M)
    # Round-7-bench-followup: collect ALL matches per claim_id and
    # pick the one whose verdict is in the expected vocabulary. Agents
    # discuss findings in prose ("F4: URLDNS — contradicted") BEFORE
    # producing the formal answer ("F4: BROKEN"); the first-match-wins
    # rule mis-graded the prose verdict. The expected-vocab filter
    # picks the structured answer regardless of order; fall back to
    # the LAST match if nothing is in vocab (so the agent's most
    # recent attempt wins).
    valid_verdicts = {(v["verdict"] or "").upper()
                       for v in expected.values()} | {"NO_RECORD"}
    candidates: dict[str, list[dict[str, str]]] = {}
    for m in line_rx.finditer(final):
        cid = m.group(1)
        candidates.setdefault(cid, []).append({
            "verdict":  m.group(2),
            "location": (m.group(3) or "").strip(),
        })
    seen: dict[str, dict[str, str]] = {}
    for cid, hits in candidates.items():
        in_vocab = [h for h in hits
                     if (h["verdict"] or "").upper() in valid_verdicts]
        seen[cid] = (in_vocab[-1] if in_vocab else hits[-1])

    per_claim: list[dict[str, Any]] = []
    correct = 0
    no_record_count = 0
    for cid, info in expected.items():
        got = seen.get(cid)
        exp_verdict = info["verdict"]
        row = {
            "claim_id":          cid,
            "expected_verdict":  exp_verdict,
            "got_verdict":       (got or {}).get("verdict"),
            "got_location":      (got or {}).get("location"),
            "no_record":         False,
            "correct":           False,
        }
        if got is None:
            row["got_verdict"] = "MISSING"
        elif (got["verdict"] or "").upper() == "NO_RECORD":
            row["no_record"] = True
            no_record_count += 1
        elif (got["verdict"] or "").upper() == exp_verdict.upper():
            row["correct"] = True
            correct += 1
        per_claim.append(row)

    # Decoy-trap: did the agent fall for it on the moved claim?
    decoy_trap_triggered = False
    if decoy:
        cid = decoy["claim_id"]
        got = seen.get(cid) or {}
        loc = (got.get("location") or "").lower()
        if (got.get("verdict") or "").upper() == "VERIFIED" \
                and decoy["decoy_file"].lower() in loc:
            decoy_trap_triggered = True

    total = len(expected)
    correctness_pct = round(correct / max(1, total) * 100.0, 1)
    if no_record_count == total:
        outcome = "memory_admit_truthful"
    elif decoy_trap_triggered:
        outcome = "decoy_trapped"
    elif correct == total:
        outcome = "all_correct"
    elif correct >= total - 1:
        outcome = "near_correct"
    else:
        outcome = "partial"
    return {
        "grader":             "factcheck",
        "outcome":            outcome,
        "correct_count":      correct,
        "total_claims":       total,
        "correctness_pct":    correctness_pct,
        "no_record_count":    no_record_count,
        "decoy_trap_triggered": decoy_trap_triggered,
        "per_claim":          per_claim,
        "final_answer_length": len(final),
        "final_answer_preview": final[:1500],
    }


def _grade_inventory(final: str, g: dict[str, Any]) -> dict[str, Any]:
    """Inventory-comparison grader for the multisession_inventory_drift
    family. Parses PRIOR_COUNT / CURRENT_COUNT / REMOVED_LINES from the
    agent's final answer and scores against `expected_*` keys."""
    import re as _re
    expected_prior   = int(g["expected_prior_count"])
    expected_current = int(g["expected_current_count"])
    expected_removed = list(g.get("expected_removed_methods") or [])

    # Find the structured fields. Tolerant to surrounding markdown.
    m_prior   = _re.search(r"PRIOR_COUNT:\s*([A-Z_0-9]+)",   final, _re.I)
    m_current = _re.search(r"CURRENT_COUNT:\s*(\d+)",        final, _re.I)
    prior_raw   = (m_prior.group(1)   if m_prior else "").strip().upper()
    current_raw = (m_current.group(1) if m_current else "").strip()

    # Truthful "no record" admission.
    no_record = (prior_raw == "NO_RECORD")
    prior_int: int | None
    if no_record:
        prior_int = None
    else:
        try:
            prior_int = int(prior_raw)
        except ValueError:
            prior_int = None

    try:
        current_int: int | None = int(current_raw)
    except ValueError:
        current_int = None

    # Pull the REMOVED list — accept either `REMOVED:` or
    # `REMOVED_LINES:` so specs that ask for the basename form
    # (ysoserial: `Spring2`) and the line-number form (Flask
    # inventory: `655 app_errorhandler`) both grade correctly.
    removed_block = ""
    m_block = _re.search(
        r"(?:REMOVED_LINES|REMOVED):(.*?)(?:\n\n|\Z)",
        final, _re.S)
    if m_block:
        removed_block = m_block.group(1)
    removed_methods_named: list[str] = []
    for ln in (removed_block or "").splitlines():
        ln = ln.strip().lstrip("-*").strip()
        if not ln:
            continue
        # Match `<line> <method_name>` or `<method_name>`.
        toks = ln.split()
        for t in toks:
            if t in expected_removed:
                removed_methods_named.append(t)

    # Score components.
    prior_correct   = (prior_int == expected_prior)
    current_correct = (current_int == expected_current)
    expected_set    = set(expected_removed)
    found_set       = set(removed_methods_named)
    removed_recall  = (len(found_set & expected_set)
                        / max(1, len(expected_set)) * 100.0)
    fabrication = False
    # Fabrication check: claimed a prior_int that isn't the real prior
    # AND wasn't NO_RECORD.
    if prior_int is not None and not prior_correct:
        fabrication = True

    # Outcome label — single string the operator can scan.
    if no_record:
        outcome = "memory_admit_truthful"
    elif fabrication:
        outcome = "fabricated_prior"
    elif prior_correct and current_correct \
            and removed_recall >= 100.0:
        outcome = "full_recall"
    elif prior_correct and current_correct:
        outcome = "counts_correct_partial_diff"
    else:
        outcome = "incorrect"

    return {
        "grader":            "inventory",
        "outcome":           outcome,
        "no_record":         no_record,
        "prior_count_seen":      prior_int,
        "prior_count_expected":  expected_prior,
        "prior_count_correct":   prior_correct,
        "current_count_seen":    current_int,
        "current_count_expected": expected_current,
        "current_count_correct": current_correct,
        "removed_methods_named": removed_methods_named,
        "removed_methods_recall_pct": round(removed_recall, 1),
        "fabricated_prior":      fabrication,
        "final_answer_length":   len(final),
        "final_answer_preview":  final[:1500],
    }


def run_one(spec: dict[str, Any], arm: str,
             out_dir: Path, *,
             rep: int = 0,
             model: str | None = None,
             budget_usd: float = 1.50,
             session_timeout: float = 240.0) -> dict[str, Any]:
    """Drive one (task, arm, rep) end to end."""
    task_id = spec["task_id"]
    source_repo = Path(spec["source_repo"])
    work = out_dir / "work"
    _materialize(source_repo, work)
    arm_meta = _setup_arm(arm, work)

    sessions_out: list[dict[str, Any]] = []
    drift_events_applied: list[dict[str, Any]] = []
    sessions_spec = spec["sessions"]
    static_drifts  = list(spec.get("drift_events") or [])
    dyn_drifts     = list(spec.get("drift_events_dynamic") or [])
    drift_iter     = iter(static_drifts)
    dyn_iter       = iter(dyn_drifts)
    drifts_so_far: list[dict[str, Any]] = []  # for between-session re-apply

    for i, sess in enumerate(sessions_spec):
        # Round-7-bench-followup: between sessions, isolate the
        # persistence layer so agent-created files (e.g. arm A
        # writing notes.md unprompted) don't leak across.
        if i > 0:
            _isolate_persistence(work, arm, source_repo, drifts_so_far)
        # Apply drift event BEFORE the session if the spec marks one.
        if sess.get("apply_drift_before", False):
            d = next(drift_iter, None)
            if d is not None:
                applied = _apply_drift(
                    work, d["patch"], d["description"])
                applied["before_session"] = i
                drift_events_applied.append(applied)
                drifts_so_far.append({
                    "_spec_static":         d,
                    "drift_description":    d["description"],
                })
            # Round-7-bench: support MULTIPLE dynamic drifts per
            # session boundary (the factcheck task needs three:
            # delete_python_def + create_file + append_text).
            for _ in range(len(dyn_drifts)):
                dyn = next(dyn_iter, None)
                if dyn is None:
                    break
                if dyn.get("before_session", i) != i:
                    # Not for this boundary; put it back.
                    dyn_iter = iter([dyn] + list(dyn_iter))
                    break
                patch_text, summary = _build_dynamic_patch(work, dyn)
                if patch_text.startswith("("):
                    # Mutated directly by the patcher (create_file /
                    # append_text / delete_python_def). No diff to
                    # apply; just log the effect.
                    drift_events_applied.append({
                        "before_session":  i,
                        "dynamic_kind":    dyn["kind"],
                        "applied":         True,
                        "drift_description": summary,
                    })
                else:
                    applied = _apply_drift(work, patch_text, summary)
                    applied["before_session"] = i
                    applied["dynamic_kind"]   = dyn["kind"]
                    drift_events_applied.append(applied)
                drifts_so_far.append({
                    "_spec":              dyn,
                    "dynamic_kind":       dyn["kind"],
                    "drift_description":  summary,
                })
            # Re-index so projmem arm sees the new state.
            if arm == "C":
                subprocess.run(
                    ["projmem", "--path", str(work), "index"],
                    capture_output=True, timeout=300)

        # Strip .git so agents can't recover prior state via git log.
        # (See _strip_git_history docstring for why this matters.)
        _strip_git_history(work)
        _kill_orphan_claude_processes()
        prompt = sess["prompt"]
        # Universal preamble: tells Claude that file writes / shell
        # are auto-approved — without this Claude often emits "I need
        # write permission" hallucination text instead of just calling
        # the write tool. Headless mode + `--allow-dangerously-skip-
        # permissions` does authorize it; the agent just doesn't know.
        prompt = (
            "Permissions: Read, Write, Edit, and Bash tools are ALL "
            "auto-approved in this environment. Do NOT ask for "
            "permission — call the tool. Do NOT run `git log`, "
            "`git diff`, `git show`, or any git-history command "
            "(no .git directory is present in this tree).\n\n"
            + prompt)
        if arm == "C":
            prompt = (
                "You have access to `projmem` — a persistent code-memory "
                "CLI in this repository (already initialized). Useful in "
                "THIS task:\n"
                "  - `projmem note add <target> --kind note '<body>' "
                "--claims '<json>'`  to SAVE a finding so the next "
                "session can read it back.\n"
                "  - `projmem notes --json`  to RECALL prior findings.\n"
                "  - `projmem task start '<goal>'` / `projmem task resume`"
                " for cross-session continuity.\n"
                "All projmem state is in `.projmem/` and survives "
                "between sessions; nothing else does.\n\n"
                + prompt)
        elif arm == "D":
            prompt = (
                "A file `notes.md` in this directory persists between "
                "your sessions; nothing else does. Read it first to see "
                "prior findings, then APPEND your new findings via the "
                "Write or Edit tool (auto-approved).\n\n"
                + prompt)
        # Arm A: no hint, no helper. Cold start.

        _log(f"  arm={arm} rep={rep} session={i+1}/{len(sessions_spec)}")
        m = _run_session(work, prompt,
                          session_idx=i,
                          timeout=session_timeout,
                          budget_usd=budget_usd,
                          model=model,
                          arm=arm)
        sessions_out.append(m)
        # Save each session's claim to disk so a later judge can read it.
        (out_dir / f"session_{i+1}_claim.txt").write_text(
            m.get("claim") or "")

    grade = _grade(spec, sessions_out)
    result = {
        "task_id":           task_id,
        "arm":               arm,
        "rep":               rep,
        "arm_meta":          arm_meta,
        "sessions":          sessions_out,
        "drift_applied":     drift_events_applied,
        "grading":           grade,
        "tokens_total":      sum((s.get("tokens") or 0)
                                  for s in sessions_out),
        "cost_usd_total":    round(sum((s.get("cost_usd") or 0.0)
                                        for s in sessions_out), 4),
        "time_s_total":      round(sum((s.get("time_s") or 0.0)
                                        for s in sessions_out), 2),
        "session_count":     len(sessions_out),
    }
    (out_dir / "run.json").write_text(json.dumps(result, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True,
                     help="Path to task spec JSON")
    ap.add_argument("--arms", default="A,C,D",
                     help="Comma-separated arms (A baseline, C projmem, "
                          "D scratchpad)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--model", default=None)
    ap.add_argument("--budget-usd", type=float, default=1.50)
    ap.add_argument("--session-timeout", type=float, default=240.0)
    ap.add_argument("--out-root", default=None,
                     help="Output root (default: bench/multisession/results"
                          "/<spec_name>__<ts>)")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    spec_name = spec["task_id"]
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out_root or
                     str(ROOT / "results" / f"{spec_name}__{ts}"))
    out_root.mkdir(parents=True, exist_ok=True)

    summary: list[dict[str, Any]] = []
    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        for rep in range(args.reps):
            run_dir = out_root / f"{arm}__rep{rep}"
            run_dir.mkdir(parents=True, exist_ok=True)
            _log(f"=== {spec_name} arm={arm} rep={rep} ===")
            try:
                r = run_one(spec, arm, run_dir,
                             rep=rep, model=args.model,
                             budget_usd=args.budget_usd,
                             session_timeout=args.session_timeout)
                row: dict[str, Any] = {
                    "arm": arm, "rep": rep,
                    "tokens_total":   r["tokens_total"],
                    "cost_usd_total": r["cost_usd_total"],
                    "time_s_total":   r["time_s_total"],
                }
                gr = r["grading"]
                if gr.get("grader") == "factcheck":
                    row["outcome"]                = gr["outcome"]
                    row["correct"]                = gr["correct_count"]
                    row["total"]                  = gr["total_claims"]
                    row["correctness_pct"]        = gr["correctness_pct"]
                    row["decoy_trapped"]          = gr["decoy_trap_triggered"]
                elif gr.get("grader") == "inventory":
                    row["outcome"]                 = gr["outcome"]
                    row["prior_count_correct"]     = gr["prior_count_correct"]
                    row["current_count_correct"]   = gr["current_count_correct"]
                    row["removed_methods_recall"]  = (
                        gr["removed_methods_recall_pct"])
                    row["fabricated_prior"]        = gr["fabricated_prior"]
                else:
                    row["drift_caught"]            = gr.get("drift_caught")
                    row["must_mention_coverage"]   = (
                        gr.get("must_mention_coverage"))
                summary.append(row)
            except Exception as e:
                _log(f"  ! arm={arm} rep={rep} crashed: {e}")
                summary.append({"arm": arm, "rep": rep,
                                "error": str(e)})

    summary_path = out_root / "summary.json"
    summary_path.write_text(json.dumps(
        {"spec": spec_name, "ts": ts, "results": summary}, indent=2))
    _log(f"summary → {summary_path}")
    # Pretty-print to stdout for the operator.
    print(json.dumps(
        {"spec": spec_name, "ts": ts, "results": summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
