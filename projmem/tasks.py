"""projmem/tasks.py — session-continuity task state.

Tier-1B capability. Without this, an interrupted session loses its
context: session 2 has memory ABOUT the code but no memory ABOUT what
was being attempted. Tasks fill that gap.

A task is a named goal with append-only events. Files touched during
an active task are auto-linked via the edit_log; notes saved during
an active task are auto-linked via a synthetic `task-note` event.

Design principles:
  - One "active focus" per session — `task resume` returns the most
    recently updated active task first.
  - Tasks are agent-authored; no automatic task creation. The explicit
    `start` boundary is the signal of intent projmem needs.
  - Blocked state persists across sessions. If session 1 blocked on
    "need clarification on retry policy," session 2 sees that blocker
    and can address it directly.
  - Idempotent `start`: if a task with the exact same goal is
    already active, return its id instead of creating a duplicate.
"""
from __future__ import annotations
import time
from typing import Any, Dict, List, Optional


ACTIVE, BLOCKED, DONE = "active", "blocked", "done"


def _now() -> float:
    return time.time()


def _find_active_by_goal(store, goal: str) -> Optional[int]:
    row = store.conn.execute(
        "SELECT id FROM tasks WHERE goal=? AND status IN ('active','blocked') "
        "ORDER BY updated_at DESC LIMIT 1", (goal,)).fetchone()
    return row["id"] if row else None


def _insert_event(store, task_id: int, kind: str,
                    detail: Optional[str] = None,
                    ref: Optional[str] = None) -> int:
    cur = store.conn.execute(
        "INSERT INTO task_events (task_id, kind, detail, ref, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, kind, detail, ref, _now()))
    store.conn.execute(
        "UPDATE tasks SET updated_at=? WHERE id=?",
        (_now(), task_id))
    return cur.lastrowid


def start(store, goal: str,
           author: Optional[str] = None) -> Dict[str, Any]:
    """Start a task. Idempotent: returns existing id if a matching
    active/blocked task is already open.

    F019 (round-7): the response now carries `task_id` to match
    `step` / `blocked` / `unblock` / `close`. The legacy `id` key
    stays in the payload as a backward-compat alias so existing
    scripts pinned to the old shape don't break in this round —
    drop after one minor version.
    """
    existing = _find_active_by_goal(store, goal)
    if existing is not None:
        return {"task_id": existing, "id": existing,
                "goal": goal, "status": "reopened",
                "note": "task with this goal is already active; returning "
                        "existing id"}
    cur = store.conn.execute(
        "INSERT INTO tasks (goal, status, author, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (goal, ACTIVE, author, _now(), _now()))
    task_id = cur.lastrowid
    _insert_event(store, task_id, "status", detail="started", ref=ACTIVE)
    store.conn.commit()
    return {"task_id": task_id, "id": task_id,
            "goal": goal, "status": ACTIVE,
            "created_at": _now()}


def step(store, detail: str,
          task_id: Optional[int] = None,
          ref: Optional[str] = None) -> Dict[str, Any]:
    """Append a progress step to the most recent active task (or the
    explicit task_id). `ref` can be a file path or note id."""
    tid = task_id if task_id else _current_task_id(store)
    if tid is None:
        return {"error": "no active task",
                "hint": "run `projmem task start \"<goal>\"` first."}
    evt_id = _insert_event(store, tid, "step", detail=detail, ref=ref)
    store.conn.commit()
    return {"task_id": tid, "event_id": evt_id, "kind": "step",
            "detail": detail}


def blocked(store, detail: str,
              task_id: Optional[int] = None) -> Dict[str, Any]:
    """Mark a task blocked on an open question. Persists across
    sessions — session 2 will see it first."""
    tid = task_id if task_id else _current_task_id(store)
    if tid is None:
        return {"error": "no active task"}
    store.conn.execute(
        "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
        (BLOCKED, _now(), tid))
    _insert_event(store, tid, "blocked", detail=detail)
    store.conn.commit()
    return {"task_id": tid, "status": BLOCKED, "detail": detail}


def unblock(store, detail: Optional[str] = None,
              task_id: Optional[int] = None) -> Dict[str, Any]:
    """Move a blocked task back to active."""
    tid = task_id if task_id else _current_task_id(store, status=BLOCKED)
    if tid is None:
        return {"error": "no blocked task to unblock"}
    store.conn.execute(
        "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
        (ACTIVE, _now(), tid))
    _insert_event(store, tid, "unblocked", detail=detail)
    store.conn.commit()
    return {"task_id": tid, "status": ACTIVE, "detail": detail}


def _truncate_on_whitespace(s: str, limit: int) -> str:
    """Truncate `s` to at most `limit` chars at the last whitespace
    boundary before the cutoff. Benchmark v2 bug: a hard byte-slice
    cut `src/shared/enums/notification.ts:5` → `src/...ts:` (colon
    dangling, line number dropped). Always break on whitespace so
    file:line tokens stay intact.
    """
    if len(s) <= limit:
        return s
    # Look back for whitespace within the last 30% of the budget so we
    # don't truncate to a tiny fragment when whitespace is sparse.
    cutoff = limit
    min_acceptable = int(limit * 0.7)
    ws = s.rfind(" ", min_acceptable, cutoff)
    if ws == -1:
        return s[:limit].rstrip(":/.,-") + "…"
    return s[:ws].rstrip(":/.,-") + "…"


def _synthesize_close_summary(store, task_id: int) -> str:
    """Build a one-line summary from the last steps when the caller
    didn't supply `--detail`. Benchmark showed closed tasks becoming
    'black boxes' because agents forget the detail flag."""
    last_steps = list(store.conn.execute(
        "SELECT detail FROM task_events WHERE task_id=? AND kind='step' "
        "ORDER BY ts DESC LIMIT 3", (task_id,)))
    total_steps = store.conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id=? "
        "AND kind='step'", (task_id,)).fetchone()["n"]
    if last_steps:
        # Per-step budget bumped from 80 to 160 chars AND truncation
        # now respects whitespace boundaries so file:line tokens don't
        # get cut mid-citation. Benchmark v2 Bug 3 fix.
        tips = "; ".join(
            _truncate_on_whitespace(
                (r["detail"] or "").split("\n")[0], 160)
            for r in last_steps if r["detail"])
        return (f"closed; last steps: {tips} "
                f"[auto-synthesized from {total_steps} step(s)]")
    return "closed [no steps recorded; consider passing --detail next time]"


def close(store, task_id: Optional[int] = None,
           detail: Optional[str] = None) -> Dict[str, Any]:
    """Close the most recent open (active OR blocked) task, or the
    explicit task_id. Blocked tasks count as open for close purposes
    because resolving the blocker and moving on to done is the common
    path.

    When --detail is omitted, auto-synthesize a summary from the last
    N steps. Benchmark R1 finding: without this, `task resume` on the
    closed task is a black box — no way to recover 'what the audit
    found'. The synthesized summary preserves at least a fingerprint
    of the work."""
    if task_id is None:
        row = store.conn.execute(
            "SELECT id FROM tasks WHERE status IN ('active','blocked') "
            "ORDER BY updated_at DESC LIMIT 1").fetchone()
        task_id = row["id"] if row else None
    tid = task_id
    if tid is None:
        return {"error": "no open task to close"}
    auto_summary = False
    if not detail:
        detail = _synthesize_close_summary(store, tid)
        auto_summary = True
    now = _now()
    store.conn.execute(
        "UPDATE tasks SET status=?, closed_at=?, updated_at=? WHERE id=?",
        (DONE, now, now, tid))
    _insert_event(store, tid, "status", detail=detail or "closed",
                    ref=DONE)
    store.conn.commit()
    out: Dict[str, Any] = {"task_id": tid, "status": DONE,
                            "close_summary": detail}
    if auto_summary:
        out["hint"] = ("Summary auto-synthesized from step log. Pass "
                       "`--detail \"<1-line summary>\"` on close next "
                       "time so session 2 inherits a real summary, "
                       "not a fingerprint.")
    return out


def _current_task_id(store, *,
                      status: str = ACTIVE) -> Optional[int]:
    row = store.conn.execute(
        "SELECT id FROM tasks WHERE status=? "
        "ORDER BY updated_at DESC LIMIT 1", (status,)).fetchone()
    return row["id"] if row else None


def _touched_files_in_window(store, since_ts: float,
                              until_ts: Optional[float] = None,
                              limit: int = 25) -> List[Dict[str, Any]]:
    """Files whose edit_log ts falls inside [since_ts, until_ts]. When
    until_ts is None, window is open-ended (for active tasks).

    Benchmark v3 Bug 1 fix: closed tasks previously used `since_ts`
    alone, so notes / files modified AFTER the task closed leaked into
    the task's resume view — making per-task attribution meaningless."""
    if until_ts is not None:
        rows = list(store.conn.execute(
            "SELECT path, COUNT(*) AS n, MAX(ts) AS last_ts, "
            "MAX(summary) AS summary "
            "FROM file_edits WHERE ts >= ? AND ts <= ? GROUP BY path "
            "ORDER BY last_ts DESC LIMIT ?",
            (since_ts, until_ts, limit)))
    else:
        rows = list(store.conn.execute(
            "SELECT path, COUNT(*) AS n, MAX(ts) AS last_ts, "
            "MAX(summary) AS summary "
            "FROM file_edits WHERE ts >= ? GROUP BY path "
            "ORDER BY last_ts DESC LIMIT ?",
            (since_ts, limit)))
    return [dict(r) for r in rows]


def _notes_saved_in_window(store, since_ts: float,
                            until_ts: Optional[float] = None,
                            limit: int = 25) -> List[Dict[str, Any]]:
    """Notes created inside [since_ts, until_ts]. Same scoping fix as
    _touched_files_in_window. See Bug 1."""
    if until_ts is not None:
        rows = list(store.conn.execute(
            "SELECT id, target, kind, author, created_at, truth_class "
            "FROM annotations WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (since_ts, until_ts, limit)))
    else:
        rows = list(store.conn.execute(
            "SELECT id, target, kind, author, created_at, truth_class "
            "FROM annotations WHERE created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (since_ts, limit)))
    return [dict(r) for r in rows]


# Legacy aliases — still used where "since creation" (no upper bound)
# is the intended scope. Prefer the *_in_window versions for new call
# sites that have a task boundary.
def _touched_files_since(store, since_ts: float,
                          limit: int = 25) -> List[Dict[str, Any]]:
    return _touched_files_in_window(store, since_ts, None, limit)


def _notes_saved_since(store, since_ts: float,
                         limit: int = 25) -> List[Dict[str, Any]]:
    return _notes_saved_in_window(store, since_ts, None, limit)


def _task_event_arc(store, task_id: int, *,
                      max_events: int = 200) -> Dict[str, Any]:
    """Return the full event arc for a task: steps in chronological
    order, all blockers (resolved or open), and close-summary info.

    Benchmark v4 Bug 3 fix: previously read LIMIT 25 and reported
    event_count from the bounded read. After 50 task step calls the
    user saw event_count: 25 — looks like silent data loss but was
    actually a display cap. Now we read up to 200 events AND query
    the TRUE total separately so callers can detect when truncation
    happened.
    """
    rows = list(store.conn.execute(
        "SELECT kind, detail, ref, ts FROM task_events "
        "WHERE task_id=? ORDER BY ts ASC LIMIT ?",
        (task_id, max_events)))
    # True count (independent of the read limit) so the agent sees
    # the real history size even if events_returned is capped.
    try:
        true_total = int(store.conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id=?",
            (task_id,)).fetchone()["n"])
    except Exception:
        true_total = len(rows)
    steps = [dict(r) for r in rows if r["kind"] == "step"]
    blockers = [dict(r) for r in rows if r["kind"] == "blocked"]
    unblocks = [dict(r) for r in rows if r["kind"] == "unblocked"]
    status_events = [dict(r) for r in rows if r["kind"] == "status"]
    close_event = next((e for e in reversed(status_events)
                         if e["ref"] == DONE), None)
    return {
        "steps":            steps,
        "blockers":         blockers,
        "unblocks":         unblocks,
        "close_summary":    (close_event or {}).get("detail"),
        "event_count":      true_total,
        "events_returned":  len(rows),
        "events_truncated": true_total > len(rows),
    }


def resume(store, *, limit: int = 5) -> Dict[str, Any]:
    """Show open tasks (active + blocked) and their progress. For
    CLOSED tasks in recently_closed, also include the full event arc
    (steps, blockers, close summary) — benchmark feedback: closed
    tasks were a 'black box post-close' without this.

    Returned shape:
      open_tasks:      [{id, goal, status, age_hours, steps,
                         blockers, files_touched, notes_saved,
                         event_count}]
      recently_closed: [{id, goal, closed_at, steps, blockers,
                         close_summary, event_count}]
    """
    open_rows = list(store.conn.execute(
        "SELECT id, goal, status, author, created_at, updated_at "
        "FROM tasks WHERE status IN ('active','blocked') "
        "ORDER BY "
        "CASE status WHEN 'blocked' THEN 0 ELSE 1 END, "
        "updated_at DESC LIMIT ?", (limit,)))
    open_tasks: List[Dict[str, Any]] = []
    for r in open_rows:
        tid = r["id"]
        arc = _task_event_arc(store, tid)
        # Bug fix iter-2: use first step timestamp as lower bound so that
        # notes/files created between task creation and the first step are
        # NOT attributed to this task.  Fall back to created_at when no
        # steps exist yet (task was just opened).
        first_step_ts = (arc["steps"][0]["ts"]
                         if arc["steps"] else r["created_at"])
        files = _touched_files_in_window(store, first_step_ts)
        notes = _notes_saved_in_window(store, first_step_ts)
        now = _now()
        open_tasks.append({
            "id":                tid,
            "goal":              r["goal"],
            "status":            r["status"],
            "author":            r["author"],
            "age_hours":         round((now - r["created_at"]) / 3600.0, 1),
            "last_update_hours": round((now - r["updated_at"]) / 3600.0, 1),
            "steps":             arc["steps"][-5:],   # last 5 steps
            "last_step":         (arc["steps"][-1] if arc["steps"] else None),
            "blockers":          arc["blockers"],
            "files_touched":     files,
            "notes_saved":       notes,
            "event_count":       arc["event_count"],
            "events_returned":   arc.get("events_returned"),
            "events_truncated":  arc.get("events_truncated"),
        })
    closed_rows = list(store.conn.execute(
        "SELECT id, goal, created_at, closed_at FROM tasks WHERE status='done' "
        "ORDER BY closed_at DESC LIMIT ?", (limit,)))
    recently_closed: List[Dict[str, Any]] = []
    for r in closed_rows:
        tid = r["id"]
        arc = _task_event_arc(store, tid)
        # Closed tasks: window is [first_step_ts, closed_at].
        # Bug fix iter-2: use first step timestamp as lower bound (same as
        # open-task fix) so notes created before the first step are not
        # attributed here.  Also preserves the v3 Bug 1 fix (upper bound
        # = closed_at) so post-close notes never leak in.
        first_step_ts_c = (arc["steps"][0]["ts"]
                           if arc["steps"] else r["created_at"])
        files = _touched_files_in_window(
            store, first_step_ts_c, r["closed_at"])
        notes = _notes_saved_in_window(
            store, first_step_ts_c, r["closed_at"])
        recently_closed.append({
            "id":              tid,
            "goal":            r["goal"],
            "closed_at":       r["closed_at"],
            "close_summary":   arc["close_summary"],
            "steps":           arc["steps"],
            "blockers":        arc["blockers"],
            "unblocks":        arc["unblocks"],
            "files_touched":   files,
            "notes_saved":     notes,
            "event_count":     arc["event_count"],
            "events_returned": arc.get("events_returned"),
            "events_truncated": arc.get("events_truncated"),
        })
    if not open_tasks:
        hint = ("No open tasks. If you're about to start non-trivial "
                "work, run `projmem task start \"<goal>\"` so the "
                "next session can resume.")
    else:
        top = open_tasks[0]
        hint = (f"Resume: {top['goal']!r} ({top['status']}). "
                f"Last update {top['last_update_hours']}h ago; "
                f"{len(top['files_touched'])} file(s) touched.")
    return {
        "open_tasks":      open_tasks,
        "recently_closed": recently_closed,
        "hint":            hint,
    }


def list_all(store, *, status: Optional[str] = None,
              limit: int = 50, verbose: bool = False) -> Dict[str, Any]:
    """Dump all tasks (filtered optionally by status). With
    `verbose=True`, expand each row's full event arc (steps,
    blockers, close summary) inline — the default omits them so
    `task step "..."` becomes a write-only log unless the caller
    knows to ask for the events."""
    if status:
        rows = list(store.conn.execute(
            "SELECT id, goal, status, author, created_at, updated_at, "
            "closed_at FROM tasks WHERE status=? "
            "ORDER BY updated_at DESC LIMIT ?", (status, limit)))
    else:
        rows = list(store.conn.execute(
            "SELECT id, goal, status, author, created_at, updated_at, "
            "closed_at FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (limit,)))
    out_tasks: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if verbose:
            arc = _task_event_arc(store, r["id"])
            d["steps"] = arc.get("steps", [])
            d["blockers"] = arc.get("blockers", [])
            d["unblocks"] = arc.get("unblocks", [])
            d["close_summary"] = arc.get("close_summary")
            d["event_count"] = arc.get("event_count")
        out_tasks.append(d)
    return {"tasks": out_tasks, "count": len(out_tasks),
            "verbose": verbose}
