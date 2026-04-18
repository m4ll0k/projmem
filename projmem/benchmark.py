"""projmem/benchmark.py — one-line trust ribbon printed at the end of
high-traffic commands (index, note-verify, session, complete).

Format (printed to stderr so JSON output stays clean for piping):

    projmem: <claims> claims tracked · <refuted> REFUTED this run ·
             <freshness> freshness_warnings · <drift> drifted_on_disk

If every counter is zero, the line is suppressed — no point in noisy
output when there's nothing to surface. The ribbon is the cheapest
"proof of value" projmem can emit; it costs ~one query per command.
"""
from __future__ import annotations
import sys
import time
from typing import Optional


def compute(store, repo_root: str,
            refuted_in_run: Optional[int] = None) -> dict:
    """Return the four counters that go into the ribbon.

    `refuted_in_run` (when known) is plumbed by the caller — e.g.,
    `cmd_note_verify` already knows how many notes flipped to REFUTED
    in this invocation. Falls back to a global "currently REFUTED"
    count when not provided."""
    from . import render as _render
    # 1. Claims tracked: total claims across all annotations.
    import json as _json
    claims = 0
    for row in store.conn.execute(
            "SELECT evidence FROM annotations WHERE evidence IS NOT NULL"):
        ev = row["evidence"]
        try:
            data = _json.loads(ev) if isinstance(ev, str) else ev
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            claims += sum(1 for it in data if isinstance(it, dict)
                          and (it.get("predicate") or it.get("status")))

    # 2. REFUTED this run: caller-provided OR currently-contradicted count.
    if refuted_in_run is None:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM annotations "
            "WHERE staleness='contradicted' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (time.time(),)).fetchone()
        refuted_in_run = int(row["n"]) if row else 0

    # 3. Freshness warnings: files whose stale flag is set in the index.
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE stale=1").fetchone()
    freshness = int(row["n"]) if row else 0

    # 4. Drifted on disk: hash-sample every indexed file. Same
    # implementation as report.py uses; bounded by file count.
    paths = [r["path"] for r in store.conn.execute(
        "SELECT path FROM files")]
    drifted = len(_render.drifted_paths(store, repo_root, paths))

    return {
        "claims_tracked":       claims,
        "refuted_this_run":     int(refuted_in_run or 0),
        "freshness_warnings":   freshness,
        "drifted_on_disk":      drifted,
    }


def emit(store, repo_root: str, *,
         refuted_in_run: Optional[int] = None,
         writer=None) -> None:
    """Print the ribbon to stderr (default) or `writer`. Suppressed
    when every counter is zero so the line never adds noise to a
    completely-clean repo."""
    counts = compute(store, repo_root, refuted_in_run=refuted_in_run)
    if not any(counts.values()):
        return
    line = (f"projmem: {counts['claims_tracked']} claims tracked"
            f" · {counts['refuted_this_run']} REFUTED this run"
            f" · {counts['freshness_warnings']} freshness_warnings"
            f" · {counts['drifted_on_disk']} drifted_on_disk")
    out = writer or (lambda s: sys.stderr.write(s + "\n"))
    out(line)
