"""projmem daemon — local HTTP + WebSocket surface for the v2 UI.

Run as ``projmem daemon [--port 7777]``. Binds to ``127.0.0.1`` only;
refuses to start if asked to bind anywhere else (no auth, local-only
design — exposing on the LAN is never the right call for v2).

The daemon is OPTIONAL. The CLI works fine without it. ``projmem ui``
(Step 6) spawns it; bare CLI does not. Hooks (`PreToolUse` /
`PostToolUse`) write events to a Unix socket under ``.projmem/`` —
the daemon reads from that socket and rebroadcasts to every connected
WebSocket client so the UI shows real-time activity.

HTTP endpoints
--------------
  POST /notes                    — add a guidance note
  POST /critical                 — add a critical note (cosigner check)
  POST /control/pause            — flip the pause flag (read by hooks)
  POST /control/resume
  POST /control/approve/<lease>  — manual lease gating for critical-blocked
  POST /control/deny/<lease>
  GET  /state                    — current activity + open leases + last N events
  GET  /healthz                  — liveness probe (the only one suitable for
                                    a remote check; everything else is local)
  WS   /events                   — broadcast stream

All HTTP responses are JSON. Errors come back as
``{"error": "<code>", "message": "..."}``.

Dependencies
------------
FastAPI + uvicorn + websockets. Listed under the ``[daemon]`` extra
in ``pyproject.toml``. Importing this module without those deps
installed raises ImportError at the function level — the
``projmem daemon`` CLI handler catches it and surfaces a structured
"missing-dependency" error envelope.

Threading model
---------------
The daemon process is single-threaded asyncio. The Unix-socket
reader runs as an asyncio task; each WebSocket client gets its own
broadcast queue (asyncio.Queue). HTTP handlers acquire a fresh
``Store`` per request — SQLite WAL mode lets reader queries proceed
concurrently with writes from the indexer or CLI.
"""
# NOTE: `from __future__ import annotations` is deliberately NOT used in
# this module. FastAPI's WebSocket parameter introspection requires the
# `WebSocket` class to be a live reference, not a stringified annotation.
# Stringified annotations (PEP 563) cause FastAPI 0.110+ to treat the
# parameter as a missing query field and emit close-code 1008 on connect.

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


SOCKET_NAME = "daemon.sock"        # under .projmem/
EVENT_BUFFER_SIZE = 200            # last-N events retained for GET /state
DEFAULT_PORT = 7777
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DaemonError(Exception):
    code = "daemon-error"


class BindRefusedError(DaemonError):
    code = "bind-refused-non-loopback"


# ---------------------------------------------------------------------------
# State container — process-local; shared between HTTP + WS handlers
# ---------------------------------------------------------------------------

class DaemonState:
    def __init__(self, root: str):
        self.root = root
        self.paused: bool = False
        self.events: List[Dict[str, Any]] = []
        self.subscribers: "Set[asyncio.Queue[Dict[str, Any]]]" = set()
        self.lock = asyncio.Lock()
        # Lease-id → asyncio.Event for /control/approve gating
        self.approval_waiters: Dict[str, asyncio.Event] = {}
        # Lease-id → "approve" | "deny" decision once made
        self.decisions: Dict[str, str] = {}

    def store(self):
        from . import config as _cfg
        from .store import Store
        cfg = _cfg.load(self.root)
        return Store(cfg.db_path)

    def record(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event.setdefault("at", time.time())
        self.events.append(event)
        if len(self.events) > EVENT_BUFFER_SIZE:
            self.events = self.events[-EVENT_BUFFER_SIZE:]
        return event

    async def broadcast(self, event: Dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer — drop


# ---------------------------------------------------------------------------
# FastAPI app factory (lazy — keeps fastapi off the projmem import path)
# ---------------------------------------------------------------------------

def _ui_dist_dir() -> Optional[str]:
    """Path to the built UI bundle, or None if it isn't shipped.

    The Python package ships ``projmem/ui/dist/`` via package_data so
    pip-installed users get the bundle without a Node toolchain. Local
    development reads from the source tree.
    """
    candidate = os.path.join(os.path.dirname(__file__), "ui", "dist")
    if os.path.isdir(candidate) and os.path.isfile(
            os.path.join(candidate, "index.html")):
        return candidate
    return None


def build_app(state: DaemonState, *, serve_ui: bool = True):
    try:
        from fastapi import (FastAPI, HTTPException, Request,
                              WebSocket, WebSocketDisconnect)
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise DaemonError(
            "daemon optional deps not installed; "
            "`pip install 'projmem[daemon]'` or "
            "`pip install fastapi uvicorn[standard] websockets`"
        ) from e

    app = FastAPI(title="projmem-daemon", version="0.1.0")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "paused": state.paused,
                "subscribers": len(state.subscribers),
                "events_buffered": len(state.events)}

    @app.get("/graph")
    async def get_graph(include_ghosts: bool = False, limit: int = 5000,
                        include_symbols: bool = False,
                        symbol_kinds: str = "function,class,method"):
        """Nodes + edges for the Step 7 graph view.

        Nodes = active lifelines (one per current file path). Each
        carries staleness (driven by attached notes), critical flag,
        reverse-dep count (for node size), and currently-leased flag.
        With ``include_ghosts=true`` the response also includes
        tombstoned lifelines as dashed/faded ghosts, plus dashed
        replaced-by edges pointing at successor lifelines.
        """
        store = state.store()
        try:
            # Active lifelines + their staleness rollup (worst-case across
            # notes pinned to the path). The verifier writes staleness
            # per-annotation; we project it onto the file.
            active_rows = store.conn.execute(
                "SELECT fl.id, fl.current_path, fl.created_at "
                "FROM file_lifeline fl WHERE fl.tombstoned_at IS NULL "
                "ORDER BY fl.created_at DESC LIMIT ?", (limit,),
            ).fetchall()
            paths = [r["current_path"] for r in active_rows]
            # Worst staleness per target.
            staleness_by_path: Dict[str, str] = {}
            critical_by_path: Dict[str, bool] = {}
            if paths:
                placeholders = ",".join("?" * len(paths))
                rows = store.conn.execute(
                    f"SELECT target, kind, staleness FROM annotations "
                    f"WHERE target IN ({placeholders})",
                    tuple(paths),
                ).fetchall()
                rank = {"contradicted": 4, "strongly_stale": 3,
                        "weakly_stale": 2, "fresh": 1, "unknown": 0}
                for r in rows:
                    cur = staleness_by_path.get(r["target"], "unknown")
                    if rank.get(r["staleness"] or "", 0) > rank.get(cur, 0):
                        staleness_by_path[r["target"]] = (
                            r["staleness"] or "unknown")
                    if r["kind"] == "critical":
                        critical_by_path[r["target"]] = True
            # Currently-leased paths drive the pulse halo.
            leased_paths = set()
            for r in store.conn.execute(
                "SELECT fl.current_path FROM edit_lease el "
                "JOIN file_lifeline fl ON fl.id = el.lifeline_id "
                "WHERE el.state IN ('open','pending_approval')"
            ):
                leased_paths.add(r["current_path"])
            # Reverse-dep counts for node sizing.
            rev_counts: Dict[str, int] = {}
            for r in store.conn.execute(
                "SELECT dst, COUNT(*) AS n FROM edges "
                "WHERE type='imports' GROUP BY dst"
            ):
                rev_counts[r["dst"]] = r["n"]

            nodes = []
            for r in active_rows:
                p = r["current_path"]
                nodes.append({
                    "id":          r["id"],
                    "path":        p,
                    "staleness":   staleness_by_path.get(p, "fresh"),
                    "critical":    critical_by_path.get(p, False),
                    "rev_deps":    rev_counts.get(p, 0),
                    "leased":      p in leased_paths,
                    "ghost":       False,
                })
            edges = []
            path_to_id = {r["current_path"]: r["id"] for r in active_rows}
            if paths:
                edge_rows = store.conn.execute(
                    f"SELECT src, dst, type FROM edges "
                    f"WHERE type='imports' "
                    f"AND src IN ({placeholders}) "
                    f"AND dst IN ({placeholders})",
                    tuple(paths) + tuple(paths),
                ).fetchall()
                for er in edge_rows:
                    s_id = path_to_id.get(er["src"])
                    d_id = path_to_id.get(er["dst"])
                    if s_id and d_id:
                        edges.append({"source": s_id, "target": d_id,
                                       "kind": "imports"})

            if include_ghosts:
                ghost_rows = store.conn.execute(
                    "SELECT id, current_path, tombstoned_reason, "
                    "tombstoned_at, replaced_by FROM file_lifeline "
                    "WHERE tombstoned_at IS NOT NULL "
                    "ORDER BY tombstoned_at DESC LIMIT 200"
                ).fetchall()
                for gr in ghost_rows:
                    nodes.append({
                        "id":                gr["id"],
                        "path":              gr["current_path"],
                        "staleness":         "tombstoned",
                        "critical":          False,
                        "rev_deps":          0,
                        "leased":            False,
                        "ghost":             True,
                        "tombstoned_at":     gr["tombstoned_at"],
                        "tombstoned_reason": gr["tombstoned_reason"],
                    })
                    if gr["replaced_by"]:
                        try:
                            successors = json.loads(gr["replaced_by"])
                        except (TypeError, ValueError):
                            successors = []
                        for succ_path in successors:
                            s_id = path_to_id.get(succ_path)
                            if s_id:
                                edges.append({
                                    "source": gr["id"], "target": s_id,
                                    "kind":   "replaced_by",
                                })

            # Symbol-level nodes for monolithic files. The graph
            # treats each function/class/method as a child node
            # connected to its containing file with a 'contains' edge.
            # Defaults to function/class/method only; pass &symbol_kinds=...
            # to widen (comma-separated).
            if include_symbols and paths:
                allowed_kinds = tuple(
                    k.strip() for k in (symbol_kinds or "").split(",") if k.strip()
                )
                if allowed_kinds:
                    kind_placeholders = ",".join("?" * len(allowed_kinds))
                    path_placeholders = ",".join("?" * len(paths))
                    sym_rows = store.conn.execute(
                        f"SELECT file, name, kind, line FROM symbols "
                        f"WHERE file IN ({path_placeholders}) "
                        f"AND kind IN ({kind_placeholders}) "
                        f"ORDER BY file, line LIMIT 3000",
                        tuple(paths) + tuple(allowed_kinds),
                    ).fetchall()
                    for sr in sym_rows:
                        parent_id = path_to_id.get(sr["file"])
                        if not parent_id:
                            continue
                        sym_node_id = f"sym:{sr['file']}:{sr['name']}:{sr['line']}"
                        nodes.append({
                            "id":        sym_node_id,
                            "path":      sr["file"],
                            "label":     sr["name"],
                            "symbol":    True,
                            "symbol_kind": sr["kind"],
                            "line":      sr["line"],
                            "staleness": "fresh",
                            "critical":  False,
                            "rev_deps":  0,
                            "leased":    False,
                            "ghost":     False,
                        })
                        edges.append({
                            "source": parent_id,
                            "target": sym_node_id,
                            "kind":   "contains",
                        })

            # Compute file labels (basename without dir) on the server side
            # so the UI can render text alongside circles without splitting
            # paths client-side every frame.
            import os as _os
            for n in nodes:
                if "label" in n:
                    continue
                p = n.get("path")
                n["label"] = _os.path.basename(p) if p else "—"

            return {"nodes": nodes, "edges": edges,
                    "include_ghosts": include_ghosts,
                    "include_symbols": include_symbols,
                    "node_count": len(nodes), "edge_count": len(edges)}
        finally:
            store.close()

    @app.get("/refs/{rel_path:path}")
    async def get_ref(rel_path: str):
        """Serve files under ``.projmem/refs/<...>`` to the UI.

        Use case: research papers / PDFs / notes you want to link from
        a guidance or critical note. The user drops files under
        ``.projmem/refs/`` (any depth); a note body referencing
        ``.projmem/refs/papers/SEC-204.pdf`` then renders as a clickable
        link served by this endpoint. Path-traversal guarded with a
        realpath check against ``.projmem/refs/``.
        """
        import os as _os
        refs_root = _os.path.realpath(
            _os.path.join(state.root, ".projmem", "refs"))
        candidate = _os.path.realpath(_os.path.join(refs_root, rel_path))
        if candidate != refs_root and not candidate.startswith(refs_root + _os.sep):
            return JSONResponse(
                {"error": "path-outside-refs", "path": rel_path},
                status_code=400,
            )
        if not _os.path.isfile(candidate):
            return JSONResponse(
                {"error": "not-found", "path": rel_path}, status_code=404)
        # Sniff content-type by extension. We deliberately keep this
        # tiny — no full mimetypes lookup, just the formats we expect
        # someone to drop in a research library.
        ext = candidate.rsplit(".", 1)[-1].lower() if "." in candidate else ""
        ct_map = {
            "pdf":  "application/pdf",
            "md":   "text/markdown; charset=utf-8",
            "txt":  "text/plain; charset=utf-8",
            "html": "text/html; charset=utf-8",
            "png":  "image/png",
            "jpg":  "image/jpeg",
            "jpeg": "image/jpeg",
            "svg":  "image/svg+xml",
            "json": "application/json",
        }
        from fastapi.responses import FileResponse
        return FileResponse(candidate,
                             media_type=ct_map.get(ext, "application/octet-stream"))

    @app.get("/refs-list")
    async def list_refs():
        """List every file under ``.projmem/refs/`` so the UI can show
        a browsable research library. Returns relative paths only;
        the operator's filesystem layout is opaque to the browser."""
        import os as _os
        refs_root = _os.path.join(state.root, ".projmem", "refs")
        if not _os.path.isdir(refs_root):
            return {"refs": [], "root_exists": False}
        out = []
        for dirpath, _dirs, files in _os.walk(refs_root):
            for f in files:
                full = _os.path.join(dirpath, f)
                rel = _os.path.relpath(full, refs_root)
                try:
                    st = _os.stat(full)
                except OSError:
                    continue
                out.append({"path": rel, "size": st.st_size,
                             "mtime": st.st_mtime})
        out.sort(key=lambda r: r["path"])
        return {"refs": out, "root_exists": True, "root": refs_root}

    @app.get("/file")
    async def get_file(path: str, max_bytes: int = 200_000):
        """Read a source file under the project root. Hard guard against
        path traversal — every request is realpath-checked to live
        inside the index root."""
        import os as _os
        real_root = _os.path.realpath(state.root)
        candidate = _os.path.realpath(_os.path.join(real_root, path))
        if (candidate != real_root
                and not candidate.startswith(real_root + _os.sep)):
            return JSONResponse(
                {"error": "path-outside-repo", "path": path},
                status_code=400,
            )
        try:
            size = _os.path.getsize(candidate)
        except OSError as e:
            return JSONResponse(
                {"error": "file-not-found", "path": path, "detail": str(e)},
                status_code=404,
            )
        truncated = size > max_bytes
        try:
            with open(candidate, "rb") as f:
                blob = f.read(max_bytes)
        except OSError as e:
            return JSONResponse(
                {"error": "read-failed", "path": path, "detail": str(e)},
                status_code=500,
            )
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            # Binary or non-UTF8 file — surface the size + a hint
            # rather than spamming the UI with mojibake.
            return {"path": path, "binary": True, "size": size}
        return {"path": path, "size": size, "truncated": truncated,
                "text": text}

    @app.get("/lifeline/{lifeline_id}")
    async def get_lifeline(lifeline_id: str):
        """Per-node detail for the inspector tabs.

        Returns the lifeline row, every file_event sorted oldest-first
        (the History tab), every annotation pinned at the current path
        (the Notes tab), and any critical notes (Critical tab — empty
        list when nothing's pinned).
        """
        store = state.store()
        try:
            lifeline_row = store.conn.execute(
                "SELECT * FROM file_lifeline WHERE id=?", (lifeline_id,),
            ).fetchone()
            if lifeline_row is None:
                from fastapi import HTTPException as _HE
                raise _HE(404, "no such lifeline")
            events = [dict(r) for r in store.conn.execute(
                "SELECT * FROM file_event WHERE lifeline_id=? "
                "ORDER BY at ASC", (lifeline_id,)
            )]
            notes = [dict(r) for r in store.conn.execute(
                "SELECT * FROM annotations WHERE target=? "
                "ORDER BY created_at DESC",
                (lifeline_row["current_path"],),
            )]
            critical = [n for n in notes if n.get("kind") == "critical"]
            return {
                "lifeline": dict(lifeline_row),
                "events":   events,
                "notes":    notes,
                "critical": critical,
            }
        finally:
            store.close()

    @app.get("/state")
    async def get_state():
        store = state.store()
        try:
            open_leases = [dict(r) for r in store.conn.execute(
                "SELECT * FROM edit_lease WHERE state IN "
                "('open','pending_approval') ORDER BY opened_at DESC LIMIT 50"
            )]
            recent_events = list(state.events[-50:])
            return {
                "root":         state.root,
                "paused":       state.paused,
                "open_leases":  open_leases,
                "recent_events": recent_events,
            }
        finally:
            store.close()

    @app.post("/notes")
    async def post_note(payload: Dict[str, Any]):
        target = payload.get("target")
        body   = payload.get("body")
        kind   = payload.get("kind", "guidance")
        severity = payload.get("severity")
        if not target or not body:
            raise HTTPException(400, "target + body required")
        store = state.store()
        try:
            ann_id = store.add_annotation(
                target=target, kind=kind, body=body,
                severity=severity,
            )
        finally:
            store.close()
        event = state.record({"kind": "note_added", "id": ann_id,
                               "target": target, "note_kind": kind})
        await state.broadcast(event)
        return {"id": ann_id}

    @app.post("/critical")
    async def post_critical(payload: Dict[str, Any]):
        from . import critical as _crit
        target = payload.get("target")
        if not target:
            raise HTTPException(400, "target required")
        store = state.store()
        try:
            try:
                result = _crit.add_critical(
                    store, target,
                    reason=payload.get("reason", ""),
                    category=payload.get("category", "other"),
                    approved_by=payload.get("approved_by") or (),
                    self_cosign=bool(payload.get("self_cosign")),
                    incident_refs=payload.get("incident_refs") or (),
                    review_window_days=int(
                        payload.get("review_window_days", 90)),
                    blast_radius_hops=int(
                        payload.get("blast_radius_hops", 1)),
                    blocks_edits=bool(payload.get("blocks_edits", True)),
                )
            except _crit.CriticalError as e:
                return JSONResponse(e.envelope(), status_code=400)
        finally:
            store.close()
        event = state.record({"kind": "critical_added", **result})
        await state.broadcast(event)
        return result

    @app.post("/control/pause")
    async def control_pause():
        state.paused = True
        event = state.record({"kind": "paused"})
        await state.broadcast(event)
        return {"paused": True}

    @app.post("/control/resume")
    async def control_resume():
        state.paused = False
        event = state.record({"kind": "resumed"})
        await state.broadcast(event)
        return {"paused": False}

    @app.post("/control/approve/{lease_id}")
    async def control_approve(lease_id: str):
        store = state.store()
        try:
            store.conn.execute(
                "UPDATE edit_lease SET state='open' "
                "WHERE id=? AND state='pending_approval'",
                (lease_id,),
            )
            store.conn.commit()
        finally:
            store.close()
        state.decisions[lease_id] = "approve"
        if lease_id in state.approval_waiters:
            state.approval_waiters[lease_id].set()
        event = state.record({"kind": "lease_approved", "lease_id": lease_id})
        await state.broadcast(event)
        return {"lease_id": lease_id, "decision": "approve"}

    @app.post("/control/deny/{lease_id}")
    async def control_deny(lease_id: str):
        from . import mutation_verbs as _mv
        store = state.store()
        try:
            _mv.close_lease(store, lease_id, kind="abandoned",
                             reason="denied by human via daemon")
        except _mv.MutationError:
            pass
        finally:
            store.close()
        state.decisions[lease_id] = "deny"
        if lease_id in state.approval_waiters:
            state.approval_waiters[lease_id].set()
        event = state.record({"kind": "lease_denied", "lease_id": lease_id})
        await state.broadcast(event)
        return {"lease_id": lease_id, "decision": "deny"}

    @app.websocket("/events")
    async def ws_events(websocket: WebSocket):
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        state.subscribers.add(q)
        try:
            # On connect, replay the buffer so the UI has context.
            for ev in state.events[-50:]:
                await websocket.send_json(ev)
            while True:
                ev = await q.get()
                await websocket.send_json(ev)
        except WebSocketDisconnect:
            pass
        finally:
            state.subscribers.discard(q)

    # Static UI bundle (Step 6). MUST be mounted LAST: Starlette
    # resolves routes in registration order, so mounting "/" earlier
    # would shadow /healthz, /state, /events, etc. — including
    # WebSocket upgrades (StaticFiles asserts scope['type'] == 'http'
    # and the WS handshake never gets a chance). If the bundle isn't
    # on disk (Node-less install with dist/ deleted), the mount is
    # skipped and the daemon stays usable for headless flows.
    if serve_ui:
        dist = _ui_dist_dir()
        if dist:
            app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app


# ---------------------------------------------------------------------------
# Unix-socket listener — receives hook events, broadcasts to WS subscribers
# ---------------------------------------------------------------------------

async def serve_socket(state: DaemonState) -> None:
    """Bind a Unix socket at ``<root>/.projmem/daemon.sock`` and read
    newline-delimited JSON events. Each event is recorded + broadcast.

    The hook scripts (Step 2) don't speak this protocol yet — they
    write directly through the projmem CLI. This listener is the
    forward-compat path for future hooks that want to push live
    events (e.g. "tool started" before projmem editing finishes).
    """
    sock_path = os.path.join(state.root, ".projmem", SOCKET_NAME)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_socket_client(r, w, state),
        path=sock_path,
    )
    os.chmod(sock_path, 0o600)  # owner-only
    async with server:
        await server.serve_forever()


async def poll_file_events(state: DaemonState, *,
                           interval: float = 0.5) -> None:
    """Tail the ``file_event`` table and broadcast every new row.

    Every projmem mutation verb (editing / creating / moving / deleting
    / done / abandoned) appends to ``file_event`` from the CLI's own
    process — the daemon never sees those calls directly. Without
    this poll, the UI's WS subscribers only receive events triggered
    by the daemon's HTTP endpoints, and a `projmem editing` from a
    terminal would silently change disk state.

    Best-effort: any SQLite error (busy, locked, race during reindex)
    is swallowed and retried next tick. We also batch up to 100 rows
    per tick to keep latency reasonable under stress.
    """
    last_id = 0
    try:
        s = state.store()
        try:
            row = s.conn.execute(
                "SELECT MAX(id) FROM file_event").fetchone()
            if row and row[0]:
                last_id = int(row[0])
        finally:
            s.close()
    except Exception:
        pass

    while True:
        await asyncio.sleep(interval)
        try:
            s = state.store()
            try:
                rows = s.conn.execute(
                    "SELECT fe.id, fe.kind, fe.at, fe.reason, fe.lifeline_id, "
                    "fl.current_path FROM file_event fe "
                    "JOIN file_lifeline fl ON fl.id = fe.lifeline_id "
                    "WHERE fe.id > ? ORDER BY fe.id ASC LIMIT 100",
                    (last_id,),
                ).fetchall()
            finally:
                s.close()
        except Exception:
            continue
        for r in rows:
            event = {
                "kind":        r["kind"],
                "at":          r["at"],
                "lifeline_id": r["lifeline_id"],
                "path":        r["current_path"],
                "reason":      r["reason"],
                "source":      "file_event-poller",
            }
            state.record(event)
            try:
                await state.broadcast(event)
            except Exception:
                pass
            if r["id"] > last_id:
                last_id = r["id"]


async def _handle_socket_client(reader, writer, state: DaemonState) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                event = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            state.record(event)
            await state.broadcast(event)
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _assert_loopback(host: str) -> None:
    """Refuse to bind to anything other than the loopback interface.

    Anyone passing a public bind here is almost certainly making a
    mistake — the daemon has no auth by design and exposing it on
    the LAN is a credential-theft surface. The check is paranoid:
    we resolve the hostname and require every resolved address to
    be a loopback IP.
    """
    if host in LOOPBACK_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise BindRefusedError(
            f"refusing to bind to {host!r}: cannot resolve ({e})"
        ) from e
    for info in infos:
        ip = info[4][0]
        if ip not in LOOPBACK_HOSTS and not ip.startswith("127.") and ip != "::1":
            raise BindRefusedError(
                f"refusing to bind to {host!r} → {ip!r}: "
                "daemon is local-only; bind to 127.0.0.1 (the default)."
            )


def run(
    root: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
    open_browser: bool = False, serve_ui: bool = True,
) -> int:
    """Block forever serving the daemon. Returns process exit code.

    With ``open_browser=True`` the function spawns the user's default
    browser pointed at ``http://host:port`` after a short delay — this
    is the ``projmem ui`` entry path. With ``serve_ui=False`` the
    static-bundle mount is skipped (used by tests + headless CI).
    """
    _assert_loopback(host)
    try:
        import uvicorn  # type: ignore
    except ImportError as e:
        raise DaemonError(
            "daemon optional deps not installed; "
            "`pip install 'projmem[daemon]'`"
        ) from e

    state = DaemonState(root)
    app = build_app(state, serve_ui=serve_ui)

    @app.on_event("startup")
    async def _start_socket():
        asyncio.create_task(serve_socket(state))
        asyncio.create_task(poll_file_events(state))
        if open_browser:
            asyncio.create_task(_open_browser_soon(host, port))

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.run()
    return 0


async def _open_browser_soon(host: str, port: int, delay: float = 0.8) -> None:
    """Wait a moment for uvicorn to bind, then open the browser."""
    import webbrowser
    await asyncio.sleep(delay)
    webbrowser.open(f"http://{host}:{port}")
