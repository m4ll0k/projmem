import { useEffect } from "react";
import { TopBar } from "./components/TopBar";
import { ActivityFeed } from "./components/ActivityFeed";
import { Inspector } from "./components/Inspector";
import { GraphView } from "./components/Graph";
import { GraphSchema } from "./components/GraphSchema";
import { TreeView } from "./components/Tree";
import { api, connectEvents } from "./api";
import { useStore } from "./store";

export function App() {
  // Apply the theme to <html> as a data attribute on every change.
  const theme           = useStore((s) => s.theme);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  // Global Esc — clear every selection. Skips while typing.
  const setSelectedLifeline  = useStore((s) => s.setSelectedLifeline);
  const setSelectedDirectory = useStore((s) => s.setSelectedDirectory);
  const setSelectedEvent     = useStore((s) => s.setSelectedEvent);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const t = e.target as HTMLElement | null;
      if (t) {
        const tag = t.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || t.isContentEditable) {
          return;
        }
      }
      setSelectedLifeline(null);
      setSelectedDirectory(null);
      setSelectedEvent(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setSelectedLifeline, setSelectedDirectory, setSelectedEvent]);

  const setStatus       = useStore((s) => s.setStatus);
  const pushEvent       = useStore((s) => s.pushEvent);
  const setPaused       = useStore((s) => s.setPaused);
  const setOpenLeases   = useStore((s) => s.setOpenLeases);
  const setProjectRoot  = useStore((s) => s.setProjectRoot);
  const markLeased      = useStore((s) => s.markLeased);
  const markReleased    = useStore((s) => s.markReleased);
  const resetLiveLeases = useStore((s) => s.resetLiveLeases);

  const setExclusions    = useStore((s) => s.setExclusions);

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
      try {
        const ex = await api.exclusions();
        if (cancelled) return;
        setExclusions(ex.exclusions.map((e) => ({
          target: e.target, body: e.body,
        })));
      } catch {
        /* exclusions are non-critical; tolerate */
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
    <div className="h-full flex flex-col bg-bg text-ink overflow-hidden">
      <TopBar />
      <main className="flex-1 min-h-0 flex flex-col lg:grid lg:grid-cols-[240px_1fr_320px] xl:grid-cols-[280px_1fr_360px]">
        {/* Activity feed — collapses to a horizontal strip on narrow
            viewports so the center pane gets the screen. */}
        <section className="border-r border-line bg-bg min-h-0
                            max-h-[34vh] lg:max-h-none flex flex-col">
          <div className="border-b border-line px-3 py-2 text-xs font-semibold tracking-tight">
            Activity
          </div>
          <div className="flex-1 min-h-0">
            <ActivityFeed />
          </div>
        </section>
        <CenterPane />
        <Inspector />
      </main>
    </div>
  );
}


function CenterPane() {
  const centerView    = useStore((s) => s.centerView);
  const setCenterView = useStore((s) => s.setCenterView);
  const labels: Record<typeof centerView, string> = {
    tree:   "stable, sortable, searchable",
    schema: "hierarchical · directory tree",
    graph:  "force-directed · import edges",
  };
  return (
    <section className="bg-bg min-h-0 min-w-0 relative flex flex-col">
      <div className="border-b border-line px-3 py-1.5 flex items-center gap-2 bg-bg">
        <div className="inline-flex rounded border border-line overflow-hidden">
          {(["tree", "schema", "graph"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setCenterView(v)}
              className={`px-2.5 py-0.5 text-xs ${
                centerView === v
                  ? "bg-accent text-accent-fg"
                  : "text-muted hover:bg-sunken"
              }`}
            >{v}</button>
          ))}
        </div>
        <span className="text-[11px] text-muted">{labels[centerView]}</span>
      </div>
      <div className="flex-1 min-h-0">
        {centerView === "tree"   ? <TreeView />     :
         centerView === "schema" ? <GraphSchema /> :
                                   <GraphView />}
      </div>
    </section>
  );
}
