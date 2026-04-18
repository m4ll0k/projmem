"""projmem/watch.py — live drift surfacing.

Background watcher: on every file save under `<root>` (or the `paths`
arg), re-verify each annotation whose target falls inside the changed
file. Print one line per status TRANSITION (not on no-op verifies, so
the terminal stays quiet on a green workspace).

Implementation: stdlib-only polling loop (no `watchdog` dependency).
We hash each previously-indexed file every `interval` seconds; on
hash change we revalidate matching notes and emit transitions.

Stop with Ctrl-C — finally-block prints a one-line summary of what
the session caught.
"""
from __future__ import annotations
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .utils import hash_file


@dataclass
class WatchState:
    """Mutable bookkeeping for one watch session."""
    file_hashes:    Dict[str, str] = field(default_factory=dict)
    note_status:    Dict[int, str] = field(default_factory=dict)
    transitions:    int = 0
    refuted_total:  int = 0
    started_at:     float = 0.0


def watch(cfg, store, *,
          paths: Optional[List[str]] = None,
          interval: float = 1.0,
          once: bool = False,
          writer: Callable[[str], None] = print,
          stop_after: Optional[float] = None) -> Dict[str, Any]:
    """Run the watch loop. Blocks until SIGINT (Ctrl-C) or `once=True`.

    Args:
      paths       : optional subset (paths relative to repo root). Default:
                    every file currently in the index.
      interval    : poll cadence in seconds (default 1.0). Smaller =
                    snappier surfacing, more CPU.
      once        : single sweep then return — useful for tests / CI.
      writer      : where to send transition lines. Default: print.
      stop_after  : seconds; loop self-exits after this (test hook).

    Returns the final WatchState as a dict.
    """
    state = WatchState(started_at=time.time())
    repo_root = cfg.root

    # Seed file_hashes from the INDEX, not from current disk. This way
    # the very first sweep can already detect "drift since last index"
    # — useful for `watch --once` (CI / pre-edit smoke test) where the
    # operator wants to know what's stale right now. Long-running
    # `watch` mode then continues to detect future edits the same way.
    initial_files = _initial_paths(store, paths)
    indexed_hash: Dict[str, str] = {}
    for r in store.conn.execute(
            "SELECT path, hash FROM files WHERE hash IS NOT NULL"):
        indexed_hash[r["path"]] = r["hash"]
    for p in initial_files:
        # Use the index hash if we know one — otherwise fall back to
        # the current on-disk hash (covers paths the user passed via
        # --paths that aren't in the index yet).
        state.file_hashes[p] = indexed_hash.get(p) or _hash_or_empty(
            repo_root, p)

    # Seed note_status snapshot.
    for row in store.list_annotations(include_expired=False):
        nid = int(row["id"])
        state.note_status[nid] = row.get("staleness") or "unknown"

    writer(f"projmem watch: {len(initial_files)} indexed file(s), "
           f"{len(state.note_status)} note(s) — Ctrl-C to stop.")

    stopped = {"value": False}

    def _stop(*_args):
        stopped["value"] = True
    try:
        signal.signal(signal.SIGINT, _stop)
    except (ValueError, OSError):
        # Not always available (non-main thread, embedded).
        pass

    deadline: Optional[float] = (state.started_at + stop_after
                                  if stop_after else None)
    try:
        while not stopped["value"]:
            changed = _detect_changes(repo_root, state, initial_files)
            if changed:
                _process_changes(cfg, store, state, repo_root, changed, writer)
            if once:
                break
            if deadline and time.time() >= deadline:
                break
            time.sleep(max(0.1, interval))
    finally:
        elapsed = time.time() - state.started_at
        writer(f"projmem watch: stopped after {elapsed:.1f}s. "
               f"Transitions: {state.transitions}, "
               f"REFUTED total: {state.refuted_total}.")
    # Compute files_drifted_on_disk: indexed_hash != current on-disk hash.
    # This is the right "did anything change?" signal when no notes are
    # attached — `transitions` only counts NOTE status flips, so a repo
    # without notes always reports 0 transitions even on heavy drift.
    drifted: List[str] = []
    try:
        for r in store.conn.execute(
                "SELECT path, hash FROM files WHERE hash IS NOT NULL"):
            cur = _hash_or_empty(repo_root, r["path"])
            if cur and cur != r["hash"]:
                drifted.append(r["path"])
    except Exception:
        pass
    return {
        "started_at":            state.started_at,
        "stopped_at":            time.time(),
        "transitions":           state.transitions,
        "refuted_total":         state.refuted_total,
        "files_watched":         len(state.file_hashes),
        "files_drifted_on_disk": len(drifted),
        "drifted_paths":         drifted[:25],
        "hint": ("`transitions` counts NOTE status flips; "
                 "`files_drifted_on_disk` counts files whose on-disk "
                 "hash diverged from the indexed hash. Both can be "
                 "non-zero independently — drift without notes is "
                 "still meaningful."),
    }


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

def _initial_paths(store, paths: Optional[List[str]]) -> Set[str]:
    if paths:
        return set(paths)
    out: Set[str] = set()
    for r in store.conn.execute("SELECT path FROM files"):
        out.add(r["path"])
    return out


def _hash_or_empty(repo_root: str, rel: str) -> str:
    full = os.path.join(repo_root, rel)
    if not os.path.isfile(full):
        return ""
    try:
        return hash_file(full)
    except OSError:
        return ""


def _detect_changes(repo_root: str, state: WatchState,
                    paths: Set[str]) -> List[str]:
    """Return list of paths whose on-disk content differs from the
    last-known hash. Updates state.file_hashes in place."""
    changed: List[str] = []
    for p in paths:
        cur = _hash_or_empty(repo_root, p)
        prev = state.file_hashes.get(p)
        if cur != prev:
            state.file_hashes[p] = cur
            if prev is not None:
                # Only fire on TRANSITIONS, not the initial population.
                changed.append(p)
    return changed


def _process_changes(cfg, store, state: WatchState, repo_root: str,
                     changed_files: List[str],
                     writer: Callable[[str], None]) -> None:
    """For each changed file, find every note whose target points inside
    it, revalidate, and emit one line per status transition."""
    from . import integrity as _intg
    seen_note_ids: Set[int] = set()
    for path in changed_files:
        rows = store.annotations_for_pack(file=path,
                                           include_dir_prefixes=True)
        # Also pick up bare-symbol notes whose def lives in this file.
        sym_names = [r["name"] for r in store.conn.execute(
            "SELECT DISTINCT name FROM symbols WHERE file=?", (path,))]
        for n in sym_names:
            for ann in store.list_annotations(target=n,
                                               include_expired=False):
                rows.append(ann)
        for ann in rows:
            nid = int(ann["id"])
            if nid in seen_note_ids:
                continue
            seen_note_ids.add(nid)
            try:
                res = _intg.revalidate_annotation(
                    store, repo_root, dict(ann), persist=True)
            except Exception as e:
                writer(f"  [error] note {nid} ({ann.get('target')}): {e}")
                continue
            prev = state.note_status.get(nid, "unknown")
            now = res.now
            if prev != now:
                state.transitions += 1
                if now == "contradicted" or any(
                        v.get("status") == "REFUTED"
                        for v in (res.claim_verdicts or [])):
                    state.refuted_total += 1
                writer(_format_transition(ann, prev, now, path, res))
                state.note_status[nid] = now


def _format_transition(ann: Dict[str, Any], prev: str, now: str,
                       path: str, res) -> str:
    """One-line transition message — designed to grep cleanly."""
    target = ann.get("target") or "?"
    refuted_count = sum(
        1 for v in (res.claim_verdicts or [])
        if v.get("status") == "REFUTED")
    suffix = ""
    if refuted_count:
        suffix = f"  ({refuted_count} REFUTED)"
    if now == "contradicted":
        marker = "[REFUTED]"
    elif now in ("strongly_stale", "weakly_stale"):
        marker = "[STALE]"
    elif now == "fresh" or now == "RECOVERED":
        marker = "[FRESH]"
    else:
        marker = "[" + (now or "?").upper() + "]"
    return (f"{marker} {target}  ({prev} -> {now})  via {path}"
            f"{suffix}")
