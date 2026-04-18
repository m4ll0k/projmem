"""projmem/diff_check.py — fact-check a unified diff against saved notes.

Round-6 user-feedback gap #1: the workflow agents would actually reach
for is "I'm about to commit these changes, what notes / FACT claims
just became stale?". `fact-check` today takes prose / stdin / file but
not a unified diff. This module fills that gap.

Heuristic, not exact: we parse hunks (no patch application required),
identify touched files and changed line ranges, then classify each
note's @defined-at(symbol, file:line) claim as one of:

  * `at_risk`    — the cited line falls inside a hunk that REMOVED
                   lines. The symbol may have been deleted, renamed,
                   or moved — caller must re-verify post-apply.
  * `moved`      — the cited line is AFTER a hunk that net-shifted
                   line numbers. Updated `predicted_new_line` lets
                   the caller re-cite without re-investigating.
  * `unaffected` — file isn't touched OR the line sits before any
                   hunk in the file. The note still holds.

Why heuristic: properly classifying REFUTED vs MOVED inside a hunk
requires applying the patch and re-parsing, which is much heavier
than what `fact-check --diff` is shaped for (sub-second pre-commit
gate). The heuristic surfaces every claim that needs human judgment
without claiming false certainty.
"""
from __future__ import annotations
import re
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Unified-diff hunk header: `@@ -<old_start>,<old_count> +<new_start>,<new_count> @@`.
# Counts default to 1 when omitted. Captures the four ints.
_HUNK_RX = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
    re.MULTILINE,
)

# `+++ b/path/to/file` — the post-image filename. We prefer this over
# `--- a/path` because git's `--- /dev/null` for new files would
# otherwise leak as the target.
_PLUS_FILE_RX = re.compile(r"^\+\+\+\s+b?/?(.+?)\s*$", re.MULTILINE)


def _strip_git_prefix(path: str) -> str:
    """`b/src/foo.ts` → `src/foo.ts`. `/dev/null` → '' (deletion)."""
    if path == "/dev/null":
        return ""
    if path.startswith("b/"):
        return path[2:]
    return path


def parse_diff(diff_text: str) -> Dict[str, List[Tuple[int, int, int, int]]]:
    """Parse a unified diff into `{file_path: [(old_start, old_count,
    new_start, new_count), ...]}`.

    Hunks are listed in the order they appear in the diff. `file_path`
    is the post-image filename (the `+++ b/...` line) so renames
    surface under the new name; deletions (post-image `/dev/null`) are
    keyed under the pre-image name instead so callers can still find
    notes targeting the deleted file.
    """
    out: Dict[str, List[Tuple[int, int, int, int]]] = {}
    if not diff_text:
        return out
    # Walk file blocks. A block is delimited by `diff --git` (preferred)
    # or by the next `--- ... / +++ ...` pair. We scan once, splitting
    # on a regex that anchors on the file pair.
    file_pair_rx = re.compile(
        r"^---\s+(.+?)\s*\n\+\+\+\s+(.+?)\s*$", re.MULTILINE)
    file_positions: List[Tuple[int, str, str]] = []
    for m in file_pair_rx.finditer(diff_text):
        file_positions.append((m.end(), m.group(1).strip(),
                                 m.group(2).strip()))
    if not file_positions:
        return out
    # Append a sentinel so the last block has an end position.
    file_positions.append((len(diff_text), "", ""))
    for i in range(len(file_positions) - 1):
        block_start, pre, post = file_positions[i]
        block_end = file_positions[i + 1][0]
        block_end -= 1  # don't pull in the next file pair's `---` line
        body = diff_text[block_start:block_end]
        post_path = _strip_git_prefix(post)
        pre_path  = _strip_git_prefix(pre)
        # Deletion: post is /dev/null → key under pre.
        target = post_path or pre_path
        if not target:
            continue
        hunks: List[Tuple[int, int, int, int]] = []
        for hm in _HUNK_RX.finditer(body):
            old_start = int(hm.group(1))
            old_count = int(hm.group(2)) if hm.group(2) else 1
            new_start = int(hm.group(3))
            new_count = int(hm.group(4)) if hm.group(4) else 1
            hunks.append((old_start, old_count, new_start, new_count))
        if hunks:
            out.setdefault(target, []).extend(hunks)
    return out


def _classify_line(line_no: int,
                    hunks: List[Tuple[int, int, int, int]]
                    ) -> Tuple[str, Optional[int]]:
    """Classify `line_no` against this file's hunks.

    Returns (status, predicted_new_line). status is one of
    `at_risk`, `moved`, `unaffected`. `predicted_new_line` is None
    when the original line position is unchanged or unknown.
    """
    cumulative_shift = 0
    for old_start, old_count, _new_start, new_count in hunks:
        old_end = old_start + max(old_count, 1) - 1
        if old_count == 0:
            # Pure addition at this position; old range is empty so
            # the cited line CAN'T fall inside it. Just shift.
            if line_no >= old_start:
                cumulative_shift += new_count
            continue
        if line_no < old_start:
            # Hunk is below us — no impact yet.
            continue
        if old_start <= line_no <= old_end:
            return "at_risk", None
        # Line is past the hunk — accumulate the net shift.
        cumulative_shift += new_count - old_count
    if cumulative_shift == 0:
        return "unaffected", None
    return "moved", line_no + cumulative_shift


def check_diff(store, diff_text: str) -> Dict[str, Any]:
    """Run a fact-check pass over a unified diff. Returns a buckets
    blob the CLI surfaces under the standard fact-check shape:

      {
        files_touched, hunks_total, notes_examined,
        at_risk:    [{note_id, file, line, claim, hunk}, ...],
        moved:      [{note_id, file, old_line, new_line, claim}, ...],
        unaffected: [{note_id, file, line, claim}, ...],
        verdict:    "at_risk" | "moved_only" | "unaffected" | "no_diff",
        hint:       <actionable sentence>,
      }
    """
    hunks_by_file = parse_diff(diff_text or "")
    files_touched = sorted(hunks_by_file)
    if not files_touched:
        return {
            "verdict":         "no_diff",
            "files_touched":   [],
            "hunks_total":     0,
            "notes_examined":  0,
            "at_risk":         [],
            "moved":           [],
            "unaffected":      [],
            "hint": ("Could not parse any file/hunk from the input. "
                      "Pipe a unified diff (`git diff` / `git diff "
                      "HEAD~1`) on stdin, or pass `--file <patch>`."),
        }

    at_risk:    List[Dict[str, Any]] = []
    moved:      List[Dict[str, Any]] = []
    unaffected: List[Dict[str, Any]] = []
    notes_examined = 0
    import json as _json
    # Pull every note whose target intersects a touched file. Targets
    # come in three shapes: bare file path, `file#symbol`, or symbol_id.
    # We match on the file PREFIX of the target (everything before `#`).
    for f in files_touched:
        # Targets exactly equal to f, OR `f#...`, OR symbol_ids that
        # carry `f` as the file segment.
        rows = list(store.conn.execute(
            "SELECT id, target, evidence FROM annotations "
            "WHERE target = ? OR target LIKE ?",
            (f, f + "#%")))
        for r in rows:
            notes_examined += 1
            ev_raw = r["evidence"]
            try:
                ev = _json.loads(ev_raw) if ev_raw else []
            except Exception:
                ev = []
            for claim in ev:
                if not isinstance(claim, dict):
                    continue
                obj = str(claim.get("object") or "")
                # Only care about line-bearing claims (`file:line`).
                if ":" not in obj:
                    continue
                cited_file, _, cited_line_s = obj.rpartition(":")
                if cited_file != f:
                    continue
                try:
                    line_no = int(cited_line_s)
                except ValueError:
                    continue
                status, predicted = _classify_line(line_no,
                                                     hunks_by_file[f])
                base = {
                    "note_id":   r["id"],
                    "file":      f,
                    "claim":     {
                        "subject":   claim.get("subject"),
                        "predicate": claim.get("predicate"),
                        "object":    obj,
                        "truth_class": claim.get("truth_class"),
                    },
                }
                if status == "at_risk":
                    at_risk.append({**base, "line": line_no})
                elif status == "moved":
                    moved.append({**base, "old_line": line_no,
                                   "new_line": predicted})
                else:
                    unaffected.append({**base, "line": line_no})

    if at_risk:
        verdict = "at_risk"
        hint = (f"{len(at_risk)} note claim(s) cite a line inside a "
                "removed hunk — re-verify after applying the diff. "
                "Use `projmem check '@defined-at(<subj>, <file:line>)'` "
                "on each.")
    elif moved:
        verdict = "moved_only"
        hint = (f"{len(moved)} note claim(s) shifted line numbers but "
                "no claim sits inside a removed hunk. Update the cited "
                "line to `new_line` before shipping.")
    else:
        verdict = "unaffected"
        hint = ("No saved note claims intersect this diff. Safe on the "
                "memory front.")

    return {
        "verdict":         verdict,
        "files_touched":   files_touched,
        "hunks_total":     sum(len(v) for v in hunks_by_file.values()),
        "notes_examined":  notes_examined,
        "at_risk":         at_risk,
        "moved":           moved,
        "unaffected":      unaffected,
        "hint":            hint,
    }
