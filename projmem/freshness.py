"""projmem/freshness.py — on-demand file-hash drift check for read commands.

The index DB stores a ``hash`` column per file, set at index time. When a
user edits the file between ``projmem index`` and the next read command, the
query answer is computed against stale rows. Today the only signal is the
``files.stale`` column, which is only set when the indexer itself has been
run and noticed the mtime drift — that's still stale-after-the-fact.

This module provides a *read-time* freshness probe: given a set of relative
paths, re-hash them on disk and compare against the stored hash. Any file
whose hash diverges (or can't be read) is returned as a stale entry so the
read command can surface it in its output.

Keep it cheap: single SQL lookup + one SHA-1 read per path. Skipping the
check is trivially optional — callers pass a small path list.
"""
from __future__ import annotations
import hashlib
import os
from typing import Any, Dict, Iterable, List, Optional


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _hash_file(full_path: str) -> Optional[str]:
    try:
        with open(full_path, "rb") as f:
            return _sha1(f.read())
    except (OSError, IsADirectoryError):
        return None


def check_paths(store, repo_root: str,
                paths: Iterable[str]) -> List[Dict[str, Any]]:
    """Check each rel path's current on-disk hash against its stored hash.

    Returns a list of stale-file records, one per path whose current hash
    disagrees with the indexed hash OR whose file is missing/unreadable:

        {path, indexed_hash, current_hash, reason}

    `reason` is one of:
      - "hash-mismatch"      — both hashes present, differ
      - "file-missing"       — path not on disk (deleted)
      - "read-error"         — path exists but unreadable
      - "not-indexed"        — path not present in the files table
                               (skipped; caller may treat as fresh)

    Paths not in the index are SKIPPED (we can't compare). We don't treat
    those as stale — they might be virtual targets like `module:foo`.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for p in paths:
        if not p or p in seen:
            continue
        seen.add(p)
        row = store.get_file(p) if hasattr(store, "get_file") else None
        if row is None:
            # Try generic lookup for stores with no get_file helper.
            try:
                r = store.conn.execute(
                    "SELECT hash FROM files WHERE path=?", (p,)).fetchone()
                row = {"hash": r["hash"]} if r else None
            except Exception:
                row = None
        if row is None:
            continue  # not indexed, skip
        indexed_hash = row["hash"] if hasattr(row, "__getitem__") else None
        full = os.path.join(repo_root, p) if repo_root else p
        if not os.path.exists(full):
            out.append({
                "path": p,
                "indexed_hash": indexed_hash,
                "current_hash": None,
                "reason": "file-missing",
            })
            continue
        current = _hash_file(full)
        if current is None:
            out.append({
                "path": p,
                "indexed_hash": indexed_hash,
                "current_hash": None,
                "reason": "read-error",
            })
            continue
        if indexed_hash and current != indexed_hash:
            out.append({
                "path": p,
                "indexed_hash": indexed_hash,
                "current_hash": current,
                "reason": "hash-mismatch",
            })
    return out


def auto_refresh_if_stale(cfg, store, paths: Iterable[str],
                            max_files: int = 32) -> Dict[str, Any]:
    """Lazy-refresh touched files when their on-disk hash has drifted.

    Audit P0#3: stale state must be impossible to trust silently. Read
    commands call this on the files they're about to report on. When a
    small number of files (<= max_files) are stale, we re-index them
    in-place so the same read returns current data. The caller gets a
    report describing what was refreshed; the read then runs against
    fresh rows.

    Bounded: when more than `max_files` are stale, we DO NOT auto-
    refresh (could be a git-checkout-mass-rebase situation where a full
    `projmem index` is cheaper). The caller still sees a
    freshness_warning and can decide.

    Returns:
      {
        "checked":      N,                # paths probed
        "stale":        [...],            # stale records (hash-mismatch / missing)
        "refreshed":    [paths],          # paths actually re-indexed
        "skipped_full_refresh_needed": bool,
        "max_files_threshold": N,
      }

    NEVER raises — failure paths degrade to "report stale, don't
    refresh" so a read command can always produce output.
    """
    stale = check_paths(store, cfg.root, paths)
    out: Dict[str, Any] = {
        "checked":    sum(1 for _ in paths) if not hasattr(paths, "__len__")
                       else len(list(paths)),
        "stale":      stale,
        "refreshed":  [],
        "skipped_full_refresh_needed": False,
        "max_files_threshold":         max_files,
    }
    if not stale:
        return out
    # Over-threshold → bail. Full `projmem index` is the right tool.
    to_refresh = [r["path"] for r in stale
                   if r["reason"] == "hash-mismatch"]
    if len(to_refresh) > max_files:
        out["skipped_full_refresh_needed"] = True
        return out
    if not to_refresh:
        # Only deletions / read-errors — no in-place refresh helps those.
        return out
    try:
        from . import indexer as _indexer
        _indexer.index_all(cfg, store, paths=to_refresh, force=True)
        out["refreshed"] = to_refresh
    except Exception as e:
        out["refresh_error"] = f"{type(e).__name__}: {e}"
    return out


def freshness_warning(stale_records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build the `freshness_warning` block a read command should include
    when `stale_records` is non-empty. Returns None for an empty list so
    callers can keep their output clean."""
    if not stale_records:
        return None
    paths = [r["path"] for r in stale_records]
    reasons = {r["reason"] for r in stale_records}
    return {
        "severity": "high",
        "stale_file_count": len(stale_records),
        "stale_files": stale_records,
        "paths": paths,
        "reasons": sorted(reasons),
        "message": (
            f"{len(stale_records)} file(s) changed on disk since the last "
            "index; query results may be out-of-date. Run `projmem index` "
            "(or `projmem refresh`) to rebuild."
        ),
    }
