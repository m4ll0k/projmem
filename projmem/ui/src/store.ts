// Zustand store — single source of truth for the UI. State pieces:
//   paused           — daemon pause-mode mirror
//   wsStatus         — connection status pill at the top
//   events           — append-only ring buffer of DaemonEvents (cap 500)
//   openLeases       — most recent /state snapshot
//   selectedEvent    — highlighted event in the activity feed (drives inspector)
//
// The hook usage stays trivial: the activity feed reads `events`, the
// top bar reads `paused` + `wsStatus`, the inspector reads `selectedEvent`
// + `openLeases`. No middleware, no derived slices — Step 6 is a
// straight tap from the WS firehose into the DOM.

import { create } from "zustand";
import type { DaemonEvent, OpenLease } from "./types";

const MAX_EVENTS = 500;

interface UIState {
  paused:           boolean;
  wsStatus:         "open" | "closed" | "error" | "connecting";
  events:           DaemonEvent[];
  openLeases:       OpenLease[];
  selectedEvent:    DaemonEvent | null;
  selectedLifeline:  string | null;
  selectedDirectory: string | null;        // dir-scoped target (trailing slash)
  showGhosts:       boolean;
  showSymbols:      boolean;
  nodeLevel:        "all" | "dirs" | "files";
  liveLeasedPaths:  string[];        // paths with an open lease right now
  exclusions:       { target: string; body: string }[];  // kind=exclude targets
  theme:            "light" | "dark";
  centerView:       "tree" | "graph" | "schema";
  projectRoot:      string;

  setPaused:           (b: boolean) => void;
  setStatus:           (s: UIState["wsStatus"]) => void;
  pushEvent:           (ev: DaemonEvent) => void;
  resetEvents:         (evs: DaemonEvent[]) => void;
  setOpenLeases:       (l: OpenLease[]) => void;
  setSelectedEvent:    (ev: DaemonEvent | null) => void;
  setSelectedLifeline:  (id: string | null) => void;
  setSelectedDirectory: (target: string | null) => void;
  setShowGhosts:       (b: boolean) => void;
  setShowSymbols:      (b: boolean) => void;
  setNodeLevel:        (v: "all" | "dirs" | "files") => void;
  setProjectRoot:      (r: string) => void;
  markLeased:          (path: string) => void;
  markReleased:        (path: string) => void;
  resetLiveLeases:     (paths: string[]) => void;
  setExclusions:       (e: { target: string; body: string }[]) => void;
  toggleTheme:         () => void;
  setTheme:            (t: "light" | "dark") => void;
  setCenterView:       (v: "tree" | "graph" | "schema") => void;
}


// Preferred theme — saved override beats prefers-color-scheme beats light.
function initialTheme(): "light" | "dark" {
  try {
    const v = localStorage.getItem("projmem.theme");
    if (v === "light" || v === "dark") return v;
  } catch { /* ignore */ }
  if (typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

export const useStore = create<UIState>((set) => ({
  paused:           false,
  wsStatus:         "connecting",
  events:           [],
  openLeases:       [],
  selectedEvent:    null,
  selectedLifeline:  null,
  selectedDirectory: null,
  showGhosts:       false,
  showSymbols:      false,
  nodeLevel:        ((): "all" | "dirs" | "files" => {
    try {
      const v = localStorage.getItem("projmem.nodeLevel");
      if (v === "all" || v === "dirs" || v === "files") return v;
    } catch { /* ignore */ }
    return "all";
  })(),
  liveLeasedPaths:  [],
  exclusions:       [],
  theme:            initialTheme(),
  centerView:       ((): "tree" | "graph" | "schema" => {
    try {
      const v = localStorage.getItem("projmem.centerView");
      if (v === "graph" || v === "tree" || v === "schema") return v;
    } catch { /* ignore */ }
    return "tree";
  })(),
  projectRoot:      "",

  setPaused:           (b) => set({ paused: b }),
  setStatus:           (s) => set({ wsStatus: s }),
  pushEvent:           (ev) => set((st) => {
    const next = [...st.events, ev];
    return { events: next.length > MAX_EVENTS
      ? next.slice(next.length - MAX_EVENTS)
      : next };
  }),
  resetEvents:         (evs) => set({ events: evs }),
  setOpenLeases:       (l) => set({ openLeases: l }),
  setSelectedEvent:    (ev) => set({ selectedEvent: ev }),
  // Clearing one selection clears the other — operator can only have
  // one focus target at a time, file OR dir, never both.
  setSelectedLifeline:  (id) => set({
    selectedLifeline:  id,
    selectedDirectory: id ? null : (undefined as any),
  } as any),
  setSelectedDirectory: (t) => set({
    selectedDirectory: t,
    selectedLifeline:  t ? null : (undefined as any),
  } as any),
  setShowGhosts:       (b) => set({ showGhosts: b }),
  setShowSymbols:      (b) => set({ showSymbols: b }),
  setNodeLevel:        (v) => set(() => {
    try { localStorage.setItem("projmem.nodeLevel", v); } catch { /* ignore */ }
    return { nodeLevel: v };
  }),
  setProjectRoot:      (r) => set({ projectRoot: r }),
  markLeased:          (path) => set((st) => (
    st.liveLeasedPaths.includes(path)
      ? st
      : { liveLeasedPaths: [...st.liveLeasedPaths, path] }
  )),
  markReleased:        (path) => set((st) => ({
    liveLeasedPaths: st.liveLeasedPaths.filter((p) => p !== path),
  })),
  resetLiveLeases:     (paths) => set({ liveLeasedPaths: paths }),
  setExclusions:       (e) => set({ exclusions: e }),
  toggleTheme:         () => set((st) => {
    const next: "light" | "dark" = st.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem("projmem.theme", next); } catch { /* ignore */ }
    return { theme: next };
  }),
  setTheme:            (t) => set(() => {
    try { localStorage.setItem("projmem.theme", t); } catch { /* ignore */ }
    return { theme: t };
  }),
  setCenterView:       (v) => set(() => {
    try { localStorage.setItem("projmem.centerView", v); } catch { /* ignore */ }
    return { centerView: v };
  }),
}));
