import { useEffect } from "react";
import { TopBar } from "./components/TopBar";
import { ActivityFeed } from "./components/ActivityFeed";
import { Inspector } from "./components/Inspector";
import { GraphView } from "./components/Graph";
import { api, connectEvents } from "./api";
import { useStore } from "./store";

export function App() {
  // Apply the theme to <html> as a data attribute on every change.
  const theme           = useStore((s) => s.theme);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const setStatus       = useStore((s) => s.setStatus);
  const pushEvent       = useStore((s) => s.pushEvent);
  const setPaused       = useStore((s) => s.setPaused);
  const setOpenLeases   = useStore((s) => s.setOpenLeases);
  const setProjectRoot  = useStore((s) => s.setProjectRoot);
  const markLeased      = useStore((s) => s.markLeased);
  const markReleased    = useStore((s) => s.markReleased);
  const resetLiveLeases = useStore((s) => s.resetLiveLeases);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const st = await api.state();
        if (cancelled) return;
        setPaused(st.paused);
        setOpenLeases(st.open_leases);
        setProjectRoot(st.root);
      } catch {
        /* tolerate transient disconnects */
      }
    };
    refresh();
    const t = setInterval(refresh, 5000);

    // Keep liveLeasedPaths in sync with the daemon's broadcast stream.
    // Lease state transitions are the truth source — node halos in the
    // graph flip on/off the moment the event arrives.
    const onEvent = (ev: any) => {
      pushEvent(ev);
      const path = ev?.path;
      if (typeof path === "string" && path.length > 0) {
        if (ev.kind === "leased")                 markLeased(path);
        else if (ev.kind === "released")          markReleased(path);
        else if (ev.kind === "abandoned")         markReleased(path);
        else if (ev.kind === "deleted")           markReleased(path);
      }
    };

    const disconnect = connectEvents(
      onEvent,
      (s) => setStatus(s),
    );

    // On reconnect, drop stale live-lease state and let the next batch
    // of events repopulate.
    const dropStale = () => resetLiveLeases([]);
    window.addEventListener("focus", dropStale);

    return () => {
      cancelled = true;
      clearInterval(t);
      window.removeEventListener("focus", dropStale);
      disconnect();
    };
  }, [setStatus, pushEvent, setPaused, setOpenLeases, setProjectRoot,
      markLeased, markReleased, resetLiveLeases]);

  return (
    <div className="h-full flex flex-col bg-bg text-ink">
      <TopBar />
      <main className="flex-1 grid grid-cols-[260px_1fr_320px] min-h-0">
        <section className="border-r border-line bg-bg min-h-0">
          <div className="border-b border-line px-3 py-2 text-xs font-semibold tracking-tight">
            Activity
          </div>
          <ActivityFeed />
        </section>
        <section className="bg-bg min-h-0 min-w-0 relative">
          <GraphView />
        </section>
        <Inspector />
      </main>
    </div>
  );
}
