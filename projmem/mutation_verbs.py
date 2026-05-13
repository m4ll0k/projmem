"""Mutation verbs — the v2 lease + lifeline state machine.

Public functions used by the CLI + MCP layers:

    open_editing_lease(store, path, *, symbol, reason, agent_id)
    open_creating_lease(store, path, *, reason, agent_id)
    move_path(store, old_path, new_path, *, reason, agent_id)
    delete_path(store, path, *, reason, agent_id)
    close_lease(store, lease_id, *, kind, reason)
    sweep_expired_leases(store)
    forget_lifeline(store, lifeline_id, *, yes_really_purge)

Every entry point returns a plain ``dict`` so the CLI can emit it as
JSON unchanged. Exceptions inherit from :class:`MutationError` so the
CLI/MCP layer can map them to structured error envelopes.

The brief in ``docs/v2-design.md`` is authoritative; this module
implements Pillar 2 ("Announce-before-action") + the lease half of
Pillar 1 ("Lifelines"). Step 1 deliberately leaves the
guidance-injection set to "every annotation visible at the path, plus
1-hop reverse-dependency annotations." Step 2 will refine the filter
once the ``guidance`` kind ships.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants and errors
# ---------------------------------------------------------------------------

LEASE_TTL_SECONDS: int = 300            # 5 minutes per the brief
MIN_REASON_CHARS: int = 20
LEASE_STATES_OPEN = ("open", "pending_approval")


class MutationError(Exception):
    """Base for every CLI-mappable error from this module."""

    code: str = "mutation-error"

    def envelope(self) -> Dict[str, Any]:
        return {"error": self.code, "message": str(self)}


class ReasonQualityError(MutationError):
    code = "reason-quality"


class LeaseNotFoundError(MutationError):
    code = "lease-not-found"


class LifelineNotFoundError(MutationError):
    code = "lifeline-not-found"


class PathExistsError(MutationError):
    code = "path-exists"


class PathMissingError(MutationError):
    code = "path-missing"


class PurgeRefusedError(MutationError):
    code = "purge-refused"


# ---------------------------------------------------------------------------
# Reason quality gate
# ---------------------------------------------------------------------------

_REASON_HINT = (
    "reason needs a verb and an object — "
    "e.g. 'consolidating with shared/validators.ts' not 'cleanup'"
)


def _validate_reason(reason: Optional[str]) -> str:
    """Reject reasons that don't meet the v2 quality gate."""
    cleaned = (reason or "").strip()
    if len(cleaned) < MIN_REASON_CHARS:
        raise ReasonQualityError(
            f"{_REASON_HINT} (got {len(cleaned)} chars, need ≥ {MIN_REASON_CHARS})"
        )
    if len(cleaned.split()) < 2:
        raise ReasonQualityError(_REASON_HINT)
    return cleaned


# ---------------------------------------------------------------------------
# Lifeline helpers
# ---------------------------------------------------------------------------

def _find_active_lifeline(conn: sqlite3.Connection, path: str) -> Optional[sqlite3.Row]:
    """Return the most-recent NON-tombstoned lifeline for a path, or None."""
    return conn.execute(
        "SELECT * FROM file_lifeline WHERE current_path=? "
        "AND tombstoned_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (path,),
    ).fetchone()


def _find_recent_tombstone(conn: sqlite3.Connection, path: str) -> Optional[sqlite3.Row]:
    """Return the most-recent tombstoned lifeline at the given path, or None."""
    return conn.execute(
        "SELECT * FROM file_lifeline WHERE current_path=? "
        "AND tombstoned_at IS NOT NULL ORDER BY tombstoned_at DESC LIMIT 1",
        (path,),
    ).fetchone()


def _ensure_lifeline_for_path(
    conn: sqlite3.Connection, path: str, *,
    reason: str, agent_id: Optional[str],
) -> str:
    """Return the lifeline_id for an existing active path, or create one.

    Used by ``editing`` when an agent edits a path that exists on disk
    but was never explicitly created via ``creating`` (e.g. a legacy
    file pre-dating projmem). Creates with ``created_reason`` set to
    the editing reason so the lifeline has provenance.
    """
    row = _find_active_lifeline(conn, path)
    if row is not None:
        return row["id"]
    lid = str(uuid.uuid4())
    now = time.time()
    conn.execute(
        "INSERT INTO file_lifeline(id, current_path, created_at, "
        "created_reason, created_by) VALUES(?, ?, ?, ?, ?)",
        (lid, path, now, f"inferred from editing — {reason}", agent_id),
    )
    conn.execute(
        "INSERT INTO file_event(lifeline_id, kind, at, reason) "
        "VALUES(?, 'created', ?, ?)",
        (lid, now, f"inferred from editing — {reason}"),
    )
    conn.execute(
        "UPDATE files SET lifeline_id=? WHERE path=?", (lid, path),
    )
    return lid


# ---------------------------------------------------------------------------
# Guidance + history computation (called by editing)
# ---------------------------------------------------------------------------

def _annotation_summary(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id":          row["id"],
        "target":      row["target"],
        "kind":        row["kind"],
        "body":        row["body"],
        "staleness":   row["staleness"],
        "confidence":  row["confidence"],
        "truth_class": row["truth_class"],
    }


def _annotations_for_path(conn: sqlite3.Connection, path: str) -> List[Dict[str, Any]]:
    """All annotations whose target matches the path (exact or `path#symbol`)."""
    rows = conn.execute(
        "SELECT * FROM annotations "
        "WHERE target = ? OR target LIKE ? || '#%' "
        "ORDER BY created_at DESC LIMIT 50",
        (path, path),
    ).fetchall()
    return [_annotation_summary(r) for r in rows]


def _parent_dir_annotations(conn: sqlite3.Connection, path: str) -> List[Dict[str, Any]]:
    """Directory-scoped annotations whose target is a parent of `path`.

    Directory-prefix targets are stored as ``some/dir/`` (trailing
    slash) per the v1 convention.
    """
    parents: List[str] = []
    current = os.path.dirname(path)
    while current:
        parents.append(current + "/")
        nxt = os.path.dirname(current)
        if nxt == current:
            break
        current = nxt
    parents.append("@project")
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT * FROM annotations WHERE target IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 50",
        tuple(parents),
    ).fetchall()
    return [_annotation_summary(r) for r in rows]


def _onehop_dep_annotations(
    conn: sqlite3.Connection, path: str,
) -> List[Dict[str, Any]]:
    """Annotations on files that import (or are imported by) ``path``."""
    rows = conn.execute(
        "SELECT DISTINCT src FROM edges WHERE dst=? AND type='imports' "
        "UNION SELECT DISTINCT dst FROM edges WHERE src=? AND type='imports'",
        (path, path),
    ).fetchall()
    neighbors = [r[0] for r in rows if r[0]]
    if not neighbors:
        return []
    placeholders = ",".join("?" * len(neighbors))
    arows = conn.execute(
        f"SELECT * FROM annotations WHERE target IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 50",
        tuple(neighbors),
    ).fetchall()
    return [
        {**_annotation_summary(r), "via": "1-hop dep"} for r in arows
    ]


def context_for_path(
    store, path: str, *, include_stale: bool = False,
) -> Dict[str, Any]:
    """Read-only version of ``editing``'s guidance bundle.

    Returns the same shape that ``editing`` inlines, minus the lease.
    Useful when an agent (or the UI's node inspector) wants the
    context for a path without committing to an edit. The CLI surface
    is ``projmem context <path>``.

    ``include_stale=False`` (default) drops every annotation with
    ``staleness in ('contradicted', 'strongly_stale')`` so the agent
    isn't fed refuted beliefs. The dropped notes are reported in
    ``stale_excluded`` so the human can still find them.
    """
    conn = store.conn
    merged = (
        _annotations_for_path(conn, path)
        + _parent_dir_annotations(conn, path)
        + _onehop_dep_annotations(conn, path)
    )
    stale_excluded: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    for note in merged:
        if (not include_stale
                and note.get("staleness") in ("contradicted",
                                                 "strongly_stale")):
            stale_excluded.append(note)
        else:
            kept.append(note)
    return {
        "path":           path,
        "guidance":       kept,
        "stale_excluded": stale_excluded,
        "history":        _history_for_lifeline_by_path(conn, path),
    }


def _history_for_lifeline_by_path(
    conn: sqlite3.Connection, path: str, limit: int = 10,
) -> Optional[Dict[str, Any]]:
    """History block for whichever lifeline is currently at ``path`` (or None)."""
    row = _find_active_lifeline(conn, path)
    if row is None:
        return None
    return _history_for_lifeline(conn, row["id"], limit=limit)


# ---------------------------------------------------------------------------
# Hook-helper: look up an open lease for a path (used by PostToolUse)
# ---------------------------------------------------------------------------

def find_open_lease_for_path(store, path: str) -> Optional[Dict[str, Any]]:
    """Return the most-recent OPEN lease for ``path``, or None.

    The hook scripts use this on PostToolUse: if Claude went through
    PreToolUse, there's an open lease to close; otherwise the hook
    creates an implicit lease retroactively.
    """
    row = store.conn.execute(
        "SELECT el.* FROM edit_lease el "
        "JOIN file_lifeline fl ON fl.id = el.lifeline_id "
        "WHERE fl.current_path = ? AND el.state IN ('open','pending_approval') "
        "ORDER BY el.opened_at DESC LIMIT 1",
        (path,),
    ).fetchone()
    return dict(row) if row else None


def open_implicit_lease(
    store, path: str, *, reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a lease retroactively for a path Claude edited without announcing.

    Used by the PostToolUse hook to keep the audit trail intact when
    the agent bypassed ``editing``. ``agent_id`` is set to ``"implicit"``
    so the UI can render this lease with a distinct style and the
    implicit-lease metric ticks. The reason gate is RELAXED — the
    whole point of an implicit lease is that the agent didn't supply
    a reason; we record what we can.
    """
    conn = store.conn
    intent = reason or "implicit lease — PostToolUse without prior `editing`"
    lifeline_id = _ensure_lifeline_for_path(
        conn, path, reason=intent, agent_id="implicit",
    )
    lease_id = str(uuid.uuid4())
    now = time.time()
    conn.execute(
        "INSERT INTO edit_lease(id, lifeline_id, opened_at, expires_at, "
        "agent_id, intent, state) VALUES(?, ?, ?, ?, 'implicit', ?, 'open')",
        (lease_id, lifeline_id, now, now + LEASE_TTL_SECONDS, intent),
    )
    _emit_file_event(
        conn, lifeline_id, kind="leased",
        reason="implicit (agent bypassed editing)",
    )
    conn.commit()
    return {
        "lease_id":    lease_id,
        "lifeline_id": lifeline_id,
        "opened_at":   now,
        "implicit":    True,
    }


def _history_for_lifeline(
    conn: sqlite3.Connection, lifeline_id: str, limit: int = 10,
) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT kind, at, reason, session_id, diff_summary "
        "FROM file_event WHERE lifeline_id=? ORDER BY at DESC LIMIT ?",
        (lifeline_id, limit),
    ).fetchall()
    return {
        "lifeline_id": lifeline_id,
        "events": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Lease lifecycle
# ---------------------------------------------------------------------------

def _new_lease(
    conn: sqlite3.Connection, *, lifeline_id: str, intent: str,
    agent_id: Optional[str],
) -> Dict[str, Any]:
    lease_id = str(uuid.uuid4())
    now = time.time()
    expires_at = now + LEASE_TTL_SECONDS
    conn.execute(
        "INSERT INTO edit_lease(id, lifeline_id, opened_at, expires_at, "
        "agent_id, intent, state) VALUES(?, ?, ?, ?, ?, ?, 'open')",
        (lease_id, lifeline_id, now, expires_at, agent_id, intent),
    )
    return {"lease_id": lease_id, "opened_at": now, "expires_at": expires_at}


def _emit_file_event(
    conn: sqlite3.Connection, lifeline_id: str, *, kind: str, reason: str,
    session_id: Optional[str] = None, diff_summary: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO file_event(lifeline_id, kind, at, reason, session_id, "
        "diff_summary) VALUES(?, ?, ?, ?, ?, ?)",
        (lifeline_id, kind, time.time(), reason, session_id, diff_summary),
    )


# ---------------------------------------------------------------------------
# Public verbs
# ---------------------------------------------------------------------------

def open_editing_lease(
    store, path: str, *, symbol: Optional[str] = None,
    reason: str, agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Announce intent to edit ``path``; return lease + guidance + history.

    Returns ``{lease_id, expires_at, guidance[], history{}, warnings[]}``.
    The ``editing`` verb is double-duty: same call that opens the lease
    also bundles the context the agent needs before touching the file.
    """
    reason = _validate_reason(reason)
    conn = store.conn
    lifeline_id = _ensure_lifeline_for_path(
        conn, path, reason=reason, agent_id=agent_id,
    )
    lease = _new_lease(
        conn, lifeline_id=lifeline_id, intent=reason, agent_id=agent_id,
    )
    _emit_file_event(conn, lifeline_id, kind="leased", reason=reason)
    conn.commit()

    guidance = (
        _annotations_for_path(conn, path)
        + _parent_dir_annotations(conn, path)
        + _onehop_dep_annotations(conn, path)
    )
    warnings: List[str] = []
    # Surface a warning if any of the guidance items have already been
    # contradicted — the agent should resolve those before editing.
    contradicted = [g for g in guidance if g.get("staleness") == "contradicted"]
    if contradicted:
        warnings.append(
            f"{len(contradicted)} note(s) on this scope are contradicted — "
            "resolve before editing."
        )

    return {
        "lease_id":   lease["lease_id"],
        "expires_at": lease["expires_at"],
        "opened_at":  lease["opened_at"],
        "path":       path,
        "symbol":     symbol,
        "lifeline_id": lifeline_id,
        "guidance":   guidance,
        "history":    _history_for_lifeline(conn, lifeline_id),
        "warnings":   warnings,
    }


def open_creating_lease(
    store, path: str, *, reason: str, agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Announce intent to create a NEW file.

    If a lifeline at this path was previously tombstoned, surface a
    warning naming the prior deletion reason — that's the wedge against
    Claude re-creating things you deliberately killed.
    """
    reason = _validate_reason(reason)
    conn = store.conn
    warnings: List[str] = []

    existing = _find_active_lifeline(conn, path)
    if existing is not None:
        raise PathExistsError(
            f"{path!r} already has an active lifeline — use `editing` instead."
        )

    tombstoned = _find_recent_tombstone(conn, path)
    if tombstoned is not None:
        days_ago = max(0, int((time.time() - tombstoned["tombstoned_at"]) / 86400))
        replaced = tombstoned["replaced_by"]
        try:
            replaced_paths = json.loads(replaced) if replaced else []
        except (TypeError, ValueError):
            replaced_paths = []
        replaced_msg = (", ".join(replaced_paths)
                          if replaced_paths else "null")
        warnings.append(
            f"this path was deleted {days_ago} day(s) ago, "
            f"reason: '{tombstoned['tombstoned_reason']}', "
            f"replaced by: {replaced_msg}. "
            "Consider editing the replacement instead."
        )

    lid = str(uuid.uuid4())
    now = time.time()
    conn.execute(
        "INSERT INTO file_lifeline(id, current_path, created_at, "
        "created_reason, created_by) VALUES(?, ?, ?, ?, ?)",
        (lid, path, now, reason, agent_id),
    )
    _emit_file_event(conn, lid, kind="created", reason=reason)
    # If the file already exists in `files`, attach the lifeline.
    conn.execute(
        "UPDATE files SET lifeline_id=? WHERE path=? AND lifeline_id IS NULL",
        (lid, path),
    )
    lease = _new_lease(conn, lifeline_id=lid, intent=reason, agent_id=agent_id)
    _emit_file_event(conn, lid, kind="leased", reason=reason)
    conn.commit()

    return {
        "lease_id":   lease["lease_id"],
        "expires_at": lease["expires_at"],
        "opened_at":  lease["opened_at"],
        "path":       path,
        "lifeline_id": lid,
        "warnings":   warnings,
    }


def move_path(
    store, old_path: str, new_path: str, *,
    reason: str, agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename/move while preserving the lifeline + every attached note."""
    reason = _validate_reason(reason)
    conn = store.conn
    lifeline = _find_active_lifeline(conn, old_path)
    if lifeline is None:
        raise PathMissingError(
            f"no active lifeline at {old_path!r} to move."
        )
    if _find_active_lifeline(conn, new_path) is not None:
        raise PathExistsError(
            f"{new_path!r} already has an active lifeline — pick a different target."
        )
    conn.execute(
        "UPDATE file_lifeline SET current_path=? WHERE id=?",
        (new_path, lifeline["id"]),
    )
    _emit_file_event(
        conn, lifeline["id"], kind="moved",
        reason=f"{old_path} → {new_path}: {reason}",
    )
    # Notes ride with the path: rewrite annotations.target so a future
    # `notes` / `session <new_path>` finds them.
    conn.execute(
        "UPDATE annotations SET target=? WHERE target=?",
        (new_path, old_path),
    )
    conn.execute(
        "UPDATE annotations SET target = ? || substr(target, ?) "
        "WHERE target LIKE ? || '#%'",
        (new_path, len(old_path) + 1, old_path),
    )
    # Files table: rename the row if the indexer already saw old_path.
    conn.execute(
        "UPDATE files SET path=?, lifeline_id=? WHERE path=?",
        (new_path, lifeline["id"], old_path),
    )
    conn.commit()
    return {
        "lifeline_id": lifeline["id"],
        "old_path":    old_path,
        "new_path":    new_path,
        "moved_at":    time.time(),
    }


def delete_path(
    store, path: str, *, reason: str, agent_id: Optional[str] = None,
    replaced_by: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Tombstone the path; lifeline stays queryable forever."""
    reason = _validate_reason(reason)
    conn = store.conn
    lifeline = _find_active_lifeline(conn, path)
    if lifeline is None:
        raise PathMissingError(
            f"no active lifeline at {path!r} to delete."
        )
    now = time.time()
    replaced_json = json.dumps(replaced_by) if replaced_by else None
    conn.execute(
        "UPDATE file_lifeline SET tombstoned_at=?, tombstoned_reason=?, "
        "replaced_by=? WHERE id=?",
        (now, reason, replaced_json, lifeline["id"]),
    )
    _emit_file_event(conn, lifeline["id"], kind="deleted", reason=reason)
    # Close any open leases on this lifeline — the file is gone.
    conn.execute(
        "UPDATE edit_lease SET state='closed', closed_at=?, closed_kind='abandoned' "
        "WHERE lifeline_id=? AND state IN ('open','pending_approval')",
        (now, lifeline["id"]),
    )
    conn.commit()
    return {
        "lifeline_id":     lifeline["id"],
        "path":            path,
        "tombstoned_at":   now,
        "replaced_by":     replaced_by or [],
    }


def close_lease(
    store, lease_id: str, *, kind: str = "done",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Close a lease. Idempotent: closing an already-closed lease returns success."""
    if kind not in ("done", "abandoned"):
        raise MutationError(f"unsupported close kind: {kind!r}")
    conn = store.conn
    row = conn.execute(
        "SELECT * FROM edit_lease WHERE id=?", (lease_id,),
    ).fetchone()
    if row is None:
        raise LeaseNotFoundError(f"no lease with id {lease_id!r}")
    if row["state"] == "closed":
        return {
            "lease_id":    lease_id,
            "already_closed": True,
            "closed_kind": row["closed_kind"],
            "closed_at":   row["closed_at"],
            "duration_s":  (row["closed_at"] or 0) - row["opened_at"],
        }
    now = time.time()
    conn.execute(
        "UPDATE edit_lease SET state='closed', closed_at=?, closed_kind=? "
        "WHERE id=?", (now, kind, lease_id),
    )
    event_kind = "released" if kind == "done" else "abandoned"
    _emit_file_event(
        conn, row["lifeline_id"], kind=event_kind,
        reason=reason or f"lease {lease_id[:8]} {kind}",
    )
    conn.commit()
    return {
        "lease_id":    lease_id,
        "closed_kind": kind,
        "closed_at":   now,
        "duration_s":  now - row["opened_at"],
    }


def sweep_expired_leases(store) -> Dict[str, Any]:
    """Mark every lease past its TTL as ``closed_kind='expired'``."""
    conn = store.conn
    now = time.time()
    cur = conn.execute(
        "SELECT id, lifeline_id, opened_at FROM edit_lease "
        "WHERE state IN ('open','pending_approval') AND expires_at < ?",
        (now,),
    ).fetchall()
    expired_ids: List[str] = []
    for row in cur:
        conn.execute(
            "UPDATE edit_lease SET state='closed', closed_at=?, closed_kind='expired' "
            "WHERE id=?", (now, row["id"]),
        )
        _emit_file_event(
            conn, row["lifeline_id"], kind="abandoned",
            reason="lease expired (5 min inactivity)",
        )
        expired_ids.append(row["id"])
    conn.commit()
    return {"expired": expired_ids, "count": len(expired_ids), "at": now}


def forget_lifeline(
    store, lifeline_id: str, *, yes_really_purge: bool,
) -> Dict[str, Any]:
    """Permanently delete a lifeline + all attached events + leases + notes.

    Refuses without explicit ``--yes-really-purge`` because lifelines
    are designed to survive forever; ``forget`` exists only for
    accidental noise from CI runs or test fixtures.
    """
    if not yes_really_purge:
        raise PurgeRefusedError(
            "forget refuses without --yes-really-purge; lifelines are "
            "designed to survive forever. Use only for genuine garbage."
        )
    conn = store.conn
    row = conn.execute(
        "SELECT id, current_path FROM file_lifeline WHERE id=?", (lifeline_id,),
    ).fetchone()
    if row is None:
        raise LifelineNotFoundError(f"no lifeline with id {lifeline_id!r}")
    conn.execute("DELETE FROM file_event WHERE lifeline_id=?", (lifeline_id,))
    conn.execute("DELETE FROM edit_lease WHERE lifeline_id=?", (lifeline_id,))
    conn.execute("UPDATE files SET lifeline_id=NULL WHERE lifeline_id=?", (lifeline_id,))
    conn.execute("UPDATE annotations SET lifeline_id=NULL WHERE lifeline_id=?", (lifeline_id,))
    conn.execute("DELETE FROM file_lifeline WHERE id=?", (lifeline_id,))
    conn.commit()
    return {
        "purged":      lifeline_id,
        "former_path": row["current_path"],
    }
