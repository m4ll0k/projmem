// Tiny HTTP + WebSocket client for the local daemon. The daemon binds
// to 127.0.0.1 only; this client respects that — every URL is same-origin
// when served from the daemon's static handler, and explicitly localhost
// when running `npm run dev` (Vite's proxy bridges).

import type { DaemonEvent, DaemonState, GraphPayload, LifelineDetail } from "./types";

const isDev = import.meta.env.DEV;
const HTTP_BASE = isDev ? "http://127.0.0.1:7777" : "";
const WS_URL = isDev
  ? "ws://127.0.0.1:7777/events"
  : `ws://${window.location.host}/events`;

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(`${HTTP_BASE}${path}`);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json() as Promise<T>;
}

async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${HTTP_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json() as Promise<T>;
}

export const api = {
  state:    () => getJSON<DaemonState>("/state"),
  healthz:  () => getJSON<{ ok: boolean }>("/healthz"),
  graph:    (includeGhosts: boolean, includeSymbols: boolean = false) =>
    getJSON<GraphPayload>(
      `/graph?include_ghosts=${includeGhosts ? 1 : 0}` +
      `&include_symbols=${includeSymbols ? 1 : 0}`,
    ),
  lifeline: (id: string) => getJSON<LifelineDetail>(`/lifeline/${id}`),
  file:     (path: string) =>
    getJSON<{path: string; text?: string; binary?: boolean; size?: number; truncated?: boolean}>(
      `/file?path=${encodeURIComponent(path)}`,
    ),
  addNote:  (payload: {target: string; body: string; kind?: string; severity?: string}) =>
    postJSON<{id: number}>("/notes", payload),
  refsList: () => getJSON<{refs: {path: string; size: number; mtime: number}[]; root_exists: boolean; root?: string}>("/refs-list"),
  refUrl:   (relPath: string) => `${HTTP_BASE}/refs/${relPath.split("/").map(encodeURIComponent).join("/")}`,
  pause:    () => postJSON("/control/pause"),
  resume:   () => postJSON("/control/resume"),
  approve:  (lease: string) => postJSON(`/control/approve/${lease}`),
  deny:     (lease: string) => postJSON(`/control/deny/${lease}`),
};

// Opens a WebSocket that re-connects with linear backoff. Callers
// receive every event by calling `onEvent`; lifecycle hooks let the
// UI render a "reconnecting…" pill if needed.
export function connectEvents(
  onEvent: (ev: DaemonEvent) => void,
  onStatus: (status: "open" | "closed" | "error") => void,
): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let backoff = 500;

  const connect = () => {
    if (closed) return;
    try {
      ws = new WebSocket(WS_URL);
    } catch (err) {
      onStatus("error");
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 8000);
      return;
    }
    ws.onopen = () => { onStatus("open"); backoff = 500; };
    ws.onerror = () => onStatus("error");
    ws.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as DaemonEvent;
        onEvent(ev);
      } catch {
        /* ignore non-JSON frames */
      }
    };
    ws.onclose = () => {
      onStatus("closed");
      if (!closed) {
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 8000);
      }
    };
  };

  connect();
  return () => { closed = true; ws?.close(); };
}
