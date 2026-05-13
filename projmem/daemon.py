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

def build_app(state: DaemonState):
    try:
        from fastapi import (FastAPI, HTTPException, Request,
                              WebSocket, WebSocketDisconnect)
        from fastapi.responses import JSONResponse
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


def run(root: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> int:
    """Block forever serving the daemon. Returns process exit code."""
    _assert_loopback(host)
    try:
        import uvicorn  # type: ignore
    except ImportError as e:
        raise DaemonError(
            "daemon optional deps not installed; "
            "`pip install 'projmem[daemon]'`"
        ) from e

    state = DaemonState(root)
    app = build_app(state)

    @app.on_event("startup")
    async def _start_socket():
        asyncio.create_task(serve_socket(state))

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.run()
    return 0
