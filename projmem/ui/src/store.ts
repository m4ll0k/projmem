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
  selectedLifeline: string | null;
  showGhosts:       boolean;
  projectRoot:      string;

  setPaused:           (b: boolean) => void;
  setStatus:           (s: UIState["wsStatus"]) => void;
  pushEvent:           (ev: DaemonEvent) => void;
  resetEvents:         (evs: DaemonEvent[]) => void;
  setOpenLeases:       (l: OpenLease[]) => void;
  setSelectedEvent:    (ev: DaemonEvent | null) => void;
  setSelectedLifeline: (id: string | null) => void;
  setShowGhosts:       (b: boolean) => void;
  setProjectRoot:      (r: string) => void;
}

export const useStore = create<UIState>((set) => ({
  paused:           false,
  wsStatus:         "connecting",
  events:           [],
  openLeases:       [],
  selectedEvent:    null,
  selectedLifeline: null,
  showGhosts:       false,
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
  setSelectedLifeline: (id) => set({ selectedLifeline: id }),
  setShowGhosts:       (b) => set({ showGhosts: b }),
  setProjectRoot:      (r) => set({ projectRoot: r }),
}));
