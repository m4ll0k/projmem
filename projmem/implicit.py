"""Implicit-usage gap detector.

Motivation: projmem's structured ref counts come from AST captures. For
names that participate in macro expansion, X-macros, code generation, or
string-based dispatch, the AST-grounded refs are necessarily a subset of
what a naive text search would find. Presenting the structured count as
an exhaustive answer hides this blind spot.

This module compares structured ref counts to raw text occurrences of the
bare name within the indexed scope, and returns a structured verdict the
CLI can fold into its output. The detector is deliberately conservative:
- Word-boundary matches only (`\bNAME\b`)
- Bounded by file count and per-file size to keep latency low
- Skips binary files / files we can't decode

The verdict never *adds* refs to the index. It only tells the caller "there
are more text matches than we captured — investigate with text search."
"""
from __future__ import annotations
import os
import re
from typing import Dict, List, Optional, Tuple

from .store import Store


_WORD_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_identifier(name: str) -> bool:
    """True if `name` looks like a plain identifier suitable for a
    word-boundary regex scan. Reject dotted forms, `file#sym`, or
    names with regex metacharacters — those would need a different
    scan strategy and risk false positives."""
    return bool(name) and bool(_WORD_IDENTIFIER.match(name))


def count_text_occurrences(store: Store, root: str, name: str,
                           file_budget: int = 2000,
                           max_bytes_per_file: int = 2_000_000
                           ) -> Dict[str, object]:
    """Word-boundary text occurrences of `name` across the indexed files.

    Bounded to `file_budget` files and `max_bytes_per_file` bytes per
    file to keep this cheap on large repos. Returns:
      text_count        — total matches (>= 0)
      files_scanned     — how many files we actually read
      files_skipped     — budget-exceeded or unreadable
      files_with_hits   — distinct files where the name appeared
      truncated         — True if we stopped because file_budget was hit
    """
    if not is_identifier(name):
        return {
            "text_count": 0,
            "files_scanned": 0,
            "files_skipped": 0,
            "files_with_hits": 0,
            "truncated": False,
            "skipped_reason": "name is not a plain identifier",
        }
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    paths = [r["path"] for r in store.all_files()]
    scanned = 0
    skipped = 0
    total = 0
    hit_files = 0
    truncated = False
    for rel_path in paths:
        if scanned >= file_budget:
            truncated = True
            break
        full = os.path.join(root, rel_path) if root else rel_path
        try:
            sz = os.path.getsize(full)
        except OSError:
            skipped += 1
            continue
        if sz > max_bytes_per_file:
            skipped += 1
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            skipped += 1
            continue
        scanned += 1
        matches = pattern.findall(src)
        if matches:
            total += len(matches)
            hit_files += 1
    return {
        "text_count": total,
        "files_scanned": scanned,
        "files_skipped": skipped,
        "files_with_hits": hit_files,
        "truncated": truncated,
    }


def detect_implicit_usage(structured_count: int,
                          text_count: int,
                          text_scan_truncated: bool = False,
                          *,
                          absolute_gap_threshold: int = 5,
                          ratio_threshold: float = 1.25
                          ) -> Dict[str, object]:
    """Decide whether the coverage gap between `structured_count` and
    `text_count` is large enough to warrant an implicit-usage warning.

    Tuned conservatively:
      - At least `absolute_gap_threshold` more text matches than refs, AND
      - At least `ratio_threshold`x more text matches than refs.

    Both conditions must hold to flag — this avoids false alarms on tiny
    symbols (e.g. a symbol with 1 ref and 2 text hits, which is probably
    just a comment or a string literal).

    The 'missing_estimate' is `text_count - structured_count`. Text matches
    are a loose upper bound — strings/comments/etc. count too — so the
    estimate is clearly labeled "upper-bound estimate" and not a hard count.
    """
    gap = max(0, text_count - structured_count)
    ratio = (text_count / structured_count) if structured_count > 0 else \
            (float("inf") if text_count > 0 else 0.0)
    detected = bool(
        gap >= absolute_gap_threshold
        and ratio >= ratio_threshold
        and text_count > 0
    )
    out: Dict[str, object] = {
        "implicit_refs_detected": detected,
        "structured_ref_count": int(structured_count),
        "text_match_count": int(text_count),
        "missing_estimate_upper_bound": int(gap),
        "text_to_structured_ratio": round(ratio, 3) if ratio != float("inf") else None,
    }
    if detected:
        out["warning"] = (
            "Structured symbol coverage incomplete. Possible macro, X-macro, "
            "code-generation, or string-based dispatch usage detected. Use "
            "text search (e.g. `rg -w " + "<name>" + "`) to verify "
            "completeness before relying on the structured refs."
        )
    if text_scan_truncated:
        out.setdefault("notes", []).append(
            "Text scan was truncated by file budget; text_match_count is "
            "itself a lower bound."
        )
    return out


def scan_text_matches(store: Store, root: str, name: str, *,
                      capture_limit: int = 5000,
                      include_line_text: bool = True
                      ) -> Dict[str, object]:
    """Exhaustive word-boundary scan of every indexed file for `name`.

    This is the `--exhaustive` companion to count_text_occurrences:
    - NO file budget (reads every indexed file)
    - Returns both the total match count and up to `capture_limit`
      concrete match sites for auditability / repro.

    The count is the main parity signal; the site list may be truncated.
    """
    if not is_identifier(name):
        return {
            "text_count": 0,
            "files_scanned": 0,
            "files_skipped": 0,
            "matches": [],
            "matches_truncated": False,
            "capture_limit": capture_limit,
            "skipped_reason": "name is not a plain identifier",
        }
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    total = 0
    scanned = 0
    skipped = 0
    matches: List[Dict[str, object]] = []
    for row in store.all_files():
        rel_path = row["path"]
        full = os.path.join(root, rel_path) if root else rel_path
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                for ln, line in enumerate(f, start=1):
                    for m in pattern.finditer(line):
                        total += 1
                        if len(matches) < capture_limit:
                            rec: Dict[str, object] = {
                                "file": rel_path,
                                "line": ln,
                                "col": int(m.start()) + 1,
                            }
                            if include_line_text:
                                rec["text"] = line.rstrip("\n")
                            matches.append(rec)
        except OSError:
            skipped += 1
            continue
        scanned += 1
    return {
        "text_count": total,
        "files_scanned": scanned,
        "files_skipped": skipped,
        "matches": matches,
        "matches_truncated": total > len(matches),
        "capture_limit": capture_limit,
    }
