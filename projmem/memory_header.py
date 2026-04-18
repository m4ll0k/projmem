"""projmem/memory_header.py — unavoidable memory-presence header.

Real-world feedback from external agent testing: even with `projmem notes`
and well-written AGENTS.md templates, a second-session agent didn't KNOW
memory existed until it manually ran `projmem note list`. Having a
discovery command doesn't help if the agent doesn't call it.

The fix is to make memory impossible to miss. EVERY read command returns
a compact `repo_memory` header as a top-level key in its JSON output:

    "repo_memory": {
      "total_notes":          15,
      "contradicted_count":   2,
      "recent_activity_7d":   5,
      "has_memory":           true,
      "discover":             "projmem notes",
      "hint":                 "2 note(s) currently contradicted — treat as blockers"
    }

When the repo has zero notes, the header is still present (with
has_memory=false) so the agent learns the mechanism works. A later call
to the same command on a repo WITH memory will show the same shape with
counts populated — the agent can rely on the key always being there.

Intentionally tiny (~150-300 bytes) so it fits in every response
without meaningful context cost.
"""
from __future__ import annotations
import time
from typing import Any, Dict


_SEVEN_DAYS_SECS = 7 * 24 * 3600


_HEADER_TTL_SECS = 5 * 60  # 5 min — typical session-burst window


def _marker_path(store) -> str:
    """Where the cross-call header marker lives. One per indexed root."""
    import os as _os
    try:
        root = store.get_meta("root") or ""
    except Exception:
        root = ""
    if not root:
        return ""
    return _os.path.join(root, ".projmem", ".last_header_seen")


def _seen_recently(store) -> bool:
    """True when this process recently emitted a full header for this
    repo. Used to collapse the second-and-later header in a session
    burst to the bare counters; the FIRST call gets the full hint."""
    p = _marker_path(store)
    if not p:
        return False
    import os as _os
    try:
        mtime = _os.path.getmtime(p)
    except OSError:
        return False
    return (time.time() - mtime) < _HEADER_TTL_SECS


def _touch_marker(store) -> None:
    """Stamp the marker so the next call within TTL collapses."""
    p = _marker_path(store)
    if not p:
        return
    import os as _os
    try:
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def build_header(store) -> Dict[str, Any]:
    """Return the compact memory-presence header.

    Safe to call from any read command. Never raises — if the store
    query fails for any reason, falls back to a "has_memory=unknown"
    marker rather than propagating an exception into the primary read.
    """
    try:
        total = store.conn.execute(
            "SELECT COUNT(*) AS n FROM annotations "
            "WHERE expires_at IS NULL OR expires_at > ?",
            (time.time(),)).fetchone()["n"]
        contradicted = store.conn.execute(
            "SELECT COUNT(*) AS n FROM annotations "
            "WHERE staleness='contradicted' AND (expires_at IS NULL "
            "OR expires_at > ?)", (time.time(),)).fetchone()["n"]
        recent = store.conn.execute(
            "SELECT COUNT(*) AS n FROM annotations "
            "WHERE created_at > ? AND (expires_at IS NULL OR expires_at > ?)",
            (time.time() - _SEVEN_DAYS_SECS, time.time())).fetchone()["n"]
    except Exception:
        return {
            "has_memory": None,
            "discover":   "projmem notes",
            "hint":       "memory status unavailable; run `projmem notes`",
        }

    has_memory = total > 0
    out: Dict[str, Any] = {
        "has_memory":          has_memory,
        "total_notes":         int(total),
        "contradicted_count":  int(contradicted),
        "recent_activity_7d":  int(recent),
        "discover":            "projmem notes",
    }

    # Benchmark v4 Bug 4: warn if CWD diverges from the indexed root.
    # Caught in real benchmark testing: an agent ran projmem from
    # one repo while believing it was in another. Silent mismatch was
    # hard to notice. Now every repo_memory header exposes indexed_root
    # and flags mismatch prominently.
    import os as _os_mh
    try:
        indexed_root = store.get_meta("root") or ""
    except Exception:
        indexed_root = ""
    out["indexed_root"] = indexed_root
    try:
        cwd = _os_mh.getcwd()
    except Exception:
        cwd = ""
    # Normalize both sides (realpath) before comparison so that symlinks
    # like /tmp vs /private/tmp on macOS don't false-alarm.
    try:
        cwd_real = _os_mh.path.realpath(cwd) if cwd else ""
        ir_real  = _os_mh.path.realpath(indexed_root) if indexed_root else ""
    except Exception:
        cwd_real = cwd
        ir_real  = indexed_root
    # A real mismatch: CWD is outside the indexed tree AND indexed_root
    # is not a prefix of CWD (which is the legitimate nested case).
    mismatch = bool(indexed_root and cwd_real and ir_real
                     and not cwd_real.startswith(ir_real))
    if mismatch:
        out["root_mismatch_warning"] = {
            "cwd":           cwd,
            "indexed_root":  indexed_root,
            "reason": ("You are running projmem from a directory outside "
                       "the indexed project root. Queries may return "
                       "empty or unexpected results because the DB was "
                       "built against a different tree."),
            "hint": ("cd into the project you want to query, or run "
                     "`projmem init` there to build a new index."),
        }

    # Cross-call header collapse. Within a 5-minute session burst we
    # drop the onboarding/status hint on the SECOND and later call —
    # the structured counters still tell the agent what's true, but
    # the prose that used to repeat on every read command is silent.
    # Blockers (contradicted_count > 0) ALWAYS surface their hint,
    # regardless of the marker, so a real problem can't be smothered
    # by a recent-header stamp.
    recently_seen = _seen_recently(store)
    if contradicted > 0:
        out["hint"] = (
            f"{contradicted} note(s) currently contradicted — prior "
            "FACT claims refuted. Treat as BLOCKERS: run "
            "`projmem notes` for detail before proceeding.")
    elif not has_memory and not recently_seen:
        out["hint"] = ("No prior notes in this repo yet. Save concluded "
                        "facts with `projmem note add --claims` so the "
                        "next session verifies them automatically.")
    # else: collapsed — structured fields carry the signal.
    out["_header_collapsed"] = recently_seen and contradicted == 0
    _touch_marker(store)

    # Round-6 user feedback (banner verbosity): when nothing actionable
    # is in the header AND the caller hasn't opted into the verbose
    # form, drop everything except the two keys an agent on a context
    # budget actually needs to gate on. The full header still ships
    # whenever there's news (contradicted, no memory, root mismatch,
    # missing meta) so blockers can't be hidden by lean mode.
    has_news = (
        contradicted > 0
        or not has_memory
        or "root_mismatch_warning" in out
        or out.get("hint")
    )
    verbose = (_os_mh.environ.get("PROJMEM_VERBOSE_MEMORY") == "1")
    if not has_news and not verbose:
        return {
            "has_memory":         True,
            "contradicted_count": 0,
            "_lean":              True,
        }
    return out


def attach(out: Dict[str, Any], store) -> Dict[str, Any]:
    """Convenience: attach the header under `repo_memory` key on the
    output dict. Mutates and returns the same dict so callers can write
    `return attach(out, store)` inline."""
    out["repo_memory"] = build_header(store)
    return out
