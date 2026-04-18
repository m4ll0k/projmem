"""projmem/changes.py — "what's been edited since last session?"

Fills the gap a fresh-session agent walks into when there is no git
history. Combines four signals projmem collects:

  1. file_edits log    — append-only hash transitions emitted by the
                         indexer, one row per re-index where a file's
                         hash changed. Survives `pre-index` rotation.
                         This is the PRIMARY source — answers the
                         actual "what did we edit last session?"
                         question even after `projmem complete` has
                         re-snapshotted pre-index past the edits.
  2. file hash drift   — files.hash vs current on-disk SHA-1 (for
                         edits made AFTER the most recent index run)
  3. symbol-level diff — added/removed symbols vs a snapshot
  4. contract-level diff — added/removed contracts vs a snapshot

`projmem changes` (no args) returns the last 50 entries from the edit
log, grouped by session, plus any current on-disk drift. For the
snapshot-based diff mode, pass `--since <label>`.

This is a *read* operation; it does not mutate the index.
"""
from __future__ import annotations
import os
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import freshness as _freshness


def _pick_default_snapshot(store) -> Optional[str]:
    """Pick a sensible default `--since`. Prefer `pre-index` if it
    exists in BOTH contract and symbol snapshot tables; otherwise the
    most recently-taken contract snapshot. Returns None when no
    snapshots exist (caller should error out)."""
    contract_labels = {s["label"]: s.get("taken_at") or 0
                       for s in store.list_snapshots()}
    symbol_labels = {s["label"] for s in store.list_symbol_snapshots()}
    if "pre-index" in contract_labels and "pre-index" in symbol_labels:
        return "pre-index"
    # Most recent label that has BOTH a contract and symbol snapshot.
    candidates = [(ts, lbl) for lbl, ts in contract_labels.items()
                  if lbl in symbol_labels]
    if candidates:
        candidates.sort(key=lambda t: -t[0])
        return candidates[0][1]
    # Fallback: any contract label.
    if contract_labels:
        return max(contract_labels, key=lambda l: contract_labels[l])
    return None


def _file_hash_drift(store, repo_root: str) -> List[Dict[str, Any]]:
    """List every indexed file whose on-disk hash differs from the
    indexed hash, or that's missing from disk. Empty list when the
    index is fully up-to-date.
    """
    rows = list(store.conn.execute("SELECT path FROM files"))
    paths = [r["path"] for r in rows]
    return _freshness.check_paths(store, repo_root, paths)


def _symbol_diff_per_file(store, since_label: str
                           ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Per-file added/removed symbols between `since_label` snapshot and
    the live symbol table. Returns {file: {added:[...], removed:[...]}}.

    `moved` symbols (same name+kind, different file) appear under both
    files: as `removed` on the old and `added` on the new. Callers can
    correlate at the top level if needed.
    """
    base_rows = store.symbol_snapshot_rows(since_label)
    head_rows = store.live_symbol_rows()

    def _key(r):
        return (r["file"], r["name"], r["kind"])

    base_by_key = {_key(r): r for r in base_rows}
    head_by_key = {_key(r): r for r in head_rows}
    per_file: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for k in head_by_key:
        if k in base_by_key:
            continue
        f = head_by_key[k]["file"]
        per_file.setdefault(f, {"added": [], "removed": []})["added"].append({
            "name": head_by_key[k]["name"],
            "kind": head_by_key[k]["kind"],
            "line": head_by_key[k].get("line"),
        })
    for k in base_by_key:
        if k in head_by_key:
            continue
        f = base_by_key[k]["file"]
        per_file.setdefault(f, {"added": [], "removed": []})["removed"].append({
            "name": base_by_key[k]["name"],
            "kind": base_by_key[k]["kind"],
            "line": base_by_key[k].get("line"),
        })
    return per_file


def _contract_diff_per_file(store, since_label: str
                             ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Per-file added/removed contracts between `since_label` snapshot
    and the live contracts table. Same shape as the symbol version."""
    base_rows = store.snapshot_rows(since_label)
    head_rows = store.live_contract_rows()

    def _key(r):
        return (r.get("file"), r.get("kind"), r.get("name"))

    base_by_key = {_key(r): r for r in base_rows
                   if r.get("file") and r.get("file") != "<config>"}
    head_by_key = {_key(r): r for r in head_rows
                   if r.get("file") and r.get("file") != "<config>"}
    per_file: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for k in head_by_key:
        if k in base_by_key:
            continue
        f, kind, name = k
        per_file.setdefault(f, {"added": [], "removed": []})["added"].append({
            "kind": kind, "name": name,
            "line": head_by_key[k].get("line"),
            "role": head_by_key[k].get("role"),
        })
    for k in base_by_key:
        if k in head_by_key:
            continue
        f, kind, name = k
        per_file.setdefault(f, {"added": [], "removed": []})["removed"].append({
            "kind": kind, "name": name,
            "line": base_by_key[k].get("line"),
            "role": base_by_key[k].get("role"),
        })
    return per_file


def _edit_log_available(store) -> bool:
    """True iff the file_edits table exists AND has any rows. Old
    databases created before the schema migration won't have the table;
    fresh ones with no index runs yet won't have rows."""
    try:
        row = store.conn.execute(
            "SELECT 1 FROM file_edits LIMIT 1").fetchone()
        return row is not None
    except Exception:
        return False


def _recent_edits_from_log(store, *, hours: Optional[float] = None,
                            last: Optional[int] = None,
                            session_id: Optional[str] = None
                            ) -> List[Dict[str, Any]]:
    """Pull rows from file_edits. At most one of hours/last/session_id is
    honored; absent all three, returns the most recent 50 edits."""
    q = ("SELECT path, prev_hash, new_hash, ts, session_id, summary "
         "FROM file_edits")
    args: List[Any] = []
    where: List[str] = []
    if session_id:
        where.append("session_id = ?")
        args.append(session_id)
    if hours is not None:
        import time as _t
        cutoff = _t.time() - float(hours) * 3600.0
        where.append("ts >= ?")
        args.append(cutoff)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(int(last) if last is not None else 200)
    try:
        return [dict(r) for r in store.conn.execute(q, args)]
    except Exception:
        return []


def _most_recent_session_id(store) -> Optional[str]:
    """The session_id of the most recent index run, or None if the
    edit log is empty."""
    try:
        row = store.conn.execute(
            "SELECT session_id FROM file_edits "
            "WHERE session_id IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        return row["session_id"] if row else None
    except Exception:
        return None


def _compute_changes_from_log(store, repo_root: str, *,
                                mode: str = "auto",
                                hours: Optional[float] = None,
                                last: Optional[int] = None,
                                max_files: int = 50,
                                ) -> Dict[str, Any]:
    """Build the report from the edit log. `mode` controls scope:
      - 'auto'         — last index session only (most recent session_id)
      - 'hours'        — rows within the last `hours` hours
      - 'last'         — last `last` rows regardless of session/time
      - 'all'          — every edit row in the log (capped for safety)
    """
    if mode == "auto":
        sid = _most_recent_session_id(store)
        rows = (_recent_edits_from_log(store, session_id=sid)
                if sid else _recent_edits_from_log(store, last=50))
    elif mode == "hours":
        rows = _recent_edits_from_log(store, hours=hours or 24.0)
    elif mode == "last":
        rows = _recent_edits_from_log(store, last=last or 50)
    else:
        rows = _recent_edits_from_log(store)

    # Group by path — collapse multiple hash transitions on the same
    # file into a single entry (first seen earliest, last seen latest),
    # so a file edited 3 times in one session reads as one row.
    by_path: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        p = r["path"]
        entry = by_path.get(p)
        if entry is None:
            # First-seen (most recent because rows were DESC).
            entry = {
                "file":         p,
                "last_edit_ts": r["ts"],
                "first_edit_ts": r["ts"],
                "prev_hash":    r["prev_hash"],
                "new_hash":     r["new_hash"],
                "edit_count":   1,
                "session_ids":  [r["session_id"]],
                "was_deleted":  r["new_hash"] is None,
                "was_added":    r["prev_hash"] is None,
                "summary":      r["summary"] if "summary" in r.keys() else None,
                "summaries":    [r["summary"]] if r["summary"] else [],
            }
            by_path[p] = entry
        else:
            entry["edit_count"] += 1
            if r["ts"] < entry["first_edit_ts"]:
                entry["first_edit_ts"] = r["ts"]
                entry["prev_hash"] = r["prev_hash"]
            if r["session_id"] and r["session_id"] not in entry["session_ids"]:
                entry["session_ids"].append(r["session_id"])
            if r["new_hash"] is None:
                entry["was_deleted"] = True
            if r["prev_hash"] is None:
                entry["was_added"] = True
            # Phase C: accumulate per-edit summaries across repeated
            # edits to the same file. First element is most recent.
            if "summary" in r.keys() and r["summary"]:
                entry.setdefault("summaries", []).append(r["summary"])

    # Also detect CURRENT on-disk drift vs the index. Useful because
    # the user may have edited files AFTER the most recent index run —
    # those edits aren't in the log yet.
    drift = _file_hash_drift(store, repo_root)
    drift_by_path = {d["path"]: d for d in drift}

    # Merge: a file in the drift list but not the log is a post-index
    # edit that hasn't been re-indexed yet. Tag it accordingly.
    for p, d in drift_by_path.items():
        entry = by_path.setdefault(p, {
            "file":          p, "edit_count": 0, "session_ids": [],
            "prev_hash":     d.get("indexed_hash"),
            "new_hash":      d.get("current_hash"),
            "last_edit_ts":  None, "first_edit_ts": None,
            "was_added":     False, "was_deleted": False,
        })
        entry["drifted_on_disk"] = True
        entry["drift_reason"] = d.get("reason")

    # Sort by most-recent edit first; files with no ts (drift-only)
    # float to the top because they're "uncommitted" to the index.
    rows_out = sorted(
        by_path.values(),
        key=lambda e: (0 if e.get("drifted_on_disk") else 1,
                        -(e.get("last_edit_ts") or 0),
                        e["file"]))
    truncated = len(rows_out) > max_files
    rows_out = rows_out[:max_files]

    # Hint: which action does the agent take next?
    drift_count = len(drift_by_path)
    if drift_count:
        hint = (f"{drift_count} file(s) drifted on disk vs the index. "
                "These edits are NEWER than the last `projmem index` "
                "run. Run `projmem index` to capture them before "
                "relying on trace / reverse / pack.")
    elif rows_out:
        sessions = sorted({s for e in rows_out for s in e.get("session_ids") or ()})
        hint = (f"{len(rows_out)} file(s) edited across "
                f"{len(sessions)} index session(s). The index is "
                "in sync with disk; this is the durable edit history.")
    else:
        hint = "No edits recorded. Run `projmem index` after making changes."

    return {
        "source":          "edit_log",
        "mode":            mode,
        "summary": {
            "changed_files":  len(rows_out) + (0 if not truncated else 1),
            "drifted_on_disk": drift_count,
            "truncated":      truncated,
        },
        "changed_files":   rows_out,
        "drifted_on_disk_paths": [d["path"] for d in drift][:max_files],
        "hint":            hint,
    }


def compute_changes(store, repo_root: str, *,
                    since: Optional[str] = None,
                    max_files: int = 50,
                    max_per_file: int = 25,
                    include_unchanged_files: bool = False,
                    mode: str = "auto",
                    hours: Optional[float] = None,
                    last: Optional[int] = None,
                    ) -> Dict[str, Any]:
    """Return the "what changed" report.

    A file is "changed" when ANY of:
      - its on-disk SHA-1 differs from `files.hash` (or it was deleted)
      - its symbol set differs from the `since` snapshot
      - its contract set differs from the `since` snapshot

    The returned dict has:
      since                — snapshot label used for diff
      since_taken_at       — wall-clock when that snapshot was taken
      summary              — totals (changed_files, added_symbols, ...)
      changed_files        — list ordered by total churn (added+removed
                              symbols+contracts), capped at max_files
      drifted_on_disk      — files where current hash != indexed hash
                              (re-index needed before querying)
      hint                 — agent-facing next-step guidance

    Defaults to reading the edit log when `--since` is not passed and
    the log has rows (typical case: a normal `projmem index` or
    `projmem complete` session). Falls back to snapshot-diff mode when
    the log is empty OR when `--since <snapshot>` is explicit.
    """
    # Prefer the edit log unless the caller asked for a snapshot diff.
    # The edit log survives `pre-index` rotation; snapshot diff does not.
    if since is None and _edit_log_available(store):
        return _compute_changes_from_log(
            store, repo_root,
            mode=mode, hours=hours, last=last, max_files=max_files)

    chosen = since or _pick_default_snapshot(store)
    if not chosen:
        return {
            "error": "no snapshots exist; cannot compute changes",
            "hint": ("Run `projmem snapshot <label> --symbols` to create "
                     "a baseline. `projmem index` auto-creates `pre-index` "
                     "on every run."),
        }

    contract_labels = {s["label"]: s.get("taken_at")
                       for s in store.list_snapshots()}
    symbol_labels = {s["label"] for s in store.list_symbol_snapshots()}
    if chosen not in contract_labels:
        return {
            "error": f"contract snapshot {chosen!r} not found",
            "available": sorted(contract_labels),
        }
    if chosen not in symbol_labels:
        return {
            "error": f"symbol snapshot {chosen!r} not found",
            "available": sorted(symbol_labels),
            "hint": ("symbol snapshots are taken with `projmem snapshot "
                     "<label> --symbols`; the bare `--symbols`-less form "
                     "only freezes contracts."),
        }

    # Drift on disk (files whose hash differs from the indexed hash).
    drift = _file_hash_drift(store, repo_root)
    drift_by_path = {d["path"]: d for d in drift}

    # Symbol + contract diffs against the snapshot.
    sym_diff = _symbol_diff_per_file(store, chosen)
    con_diff = _contract_diff_per_file(store, chosen)

    all_files: Set[str] = (set(sym_diff) | set(con_diff)
                            | set(drift_by_path))
    if include_unchanged_files:
        all_files |= {r["path"] for r in store.conn.execute(
            "SELECT path FROM files")}

    rows: List[Dict[str, Any]] = []
    total_added_sym = total_removed_sym = 0
    total_added_con = total_removed_con = 0
    for f in all_files:
        s = sym_diff.get(f, {"added": [], "removed": []})
        c = con_diff.get(f, {"added": [], "removed": []})
        d = drift_by_path.get(f)
        churn = len(s["added"]) + len(s["removed"]) \
                + len(c["added"]) + len(c["removed"])
        if churn == 0 and d is None and not include_unchanged_files:
            continue
        total_added_sym += len(s["added"])
        total_removed_sym += len(s["removed"])
        total_added_con += len(c["added"])
        total_removed_con += len(c["removed"])
        # mtime + indexed-hash for the row (helps the agent decide
        # whether to re-index before trusting query answers).
        meta = store.conn.execute(
            "SELECT mtime, hash, parser FROM files WHERE path=?",
            (f,)).fetchone()
        rows.append({
            "file":            f,
            "churn":           churn,
            "drifted_on_disk": d is not None,
            "drift_reason":    d["reason"] if d else None,
            "indexed_mtime":   meta["mtime"] if meta else None,
            "indexed_parser":  meta["parser"] if meta else None,
            "added_symbols":   s["added"][:max_per_file],
            "removed_symbols": s["removed"][:max_per_file],
            "added_contracts": c["added"][:max_per_file],
            "removed_contracts": c["removed"][:max_per_file],
        })

    rows.sort(key=lambda r: (-r["churn"], r["file"]))
    truncated = len(rows) > max_files
    rows = rows[:max_files]

    # Hint: tell the agent whether to re-index before trusting reads.
    drift_count = len(drift)
    if drift_count:
        hint = (f"{drift_count} file(s) drifted on disk vs the index. "
                "Re-index with `projmem index` (or `projmem refresh "
                "--reindex`) before relying on trace / reverse / pack "
                "answers — they read from stale rows otherwise.")
    elif rows:
        hint = (f"{len(rows)} file(s) changed since {chosen!r}. The "
                "index is in sync with disk; the differences are real "
                "edits between the snapshot and now.")
    else:
        hint = (f"No changes since {chosen!r}. Index and snapshot agree.")

    return {
        "since":          chosen,
        "since_taken_at": contract_labels.get(chosen),
        "summary": {
            "changed_files":      len(rows) + (0 if not truncated else 1),
            "drifted_on_disk":    drift_count,
            "added_symbols":      total_added_sym,
            "removed_symbols":    total_removed_sym,
            "added_contracts":    total_added_con,
            "removed_contracts":  total_removed_con,
            "truncated":          truncated,
        },
        "changed_files":  rows,
        "drifted_on_disk_paths": [d["path"] for d in drift][:max_files],
        "hint":           hint,
    }
