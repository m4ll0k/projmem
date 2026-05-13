import { useEffect } from "react";
import { TopBar } from "./components/TopBar";
import { ActivityFeed } from "./components/ActivityFeed";
import { Inspector } from "./components/Inspector";
import { api, connectEvents } from "./api";
import { useStore } from "./store";

export function App() {
  const setStatus      = useStore((s) => s.setStatus);
  const pushEvent      = useStore((s) => s.pushEvent);
  const setPaused      = useStore((s) => s.setPaused);
  const setOpenLeases  = useStore((s) => s.setOpenLeases);
  const setProjectRoot = useStore((s) => s.setProjectRoot);

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

    const disconnect = connectEvents(
      pushEvent,
      (s) => setStatus(s),
    );

    return () => {
      cancelled = true;
      clearInterval(t);
      disconnect();
    };
  }, [setStatus, pushEvent, setPaused, setOpenLeases, setProjectRoot]);

  return (
    <div className="h-full flex flex-col">
      <TopBar />
      <main className="flex-1 grid grid-cols-[260px_1fr_320px] min-h-0">
        <section className="border-r border-line bg-white min-h-0">
          <div className="border-b border-line px-3 py-2 text-xs font-semibold tracking-tight">
            Activity
          </div>
          <ActivityFeed />
        </section>
        <section className="flex items-center justify-center bg-white text-xs text-muted">
          graph view — coming in Step 7
        </section>
        <Inspector />
      </main>
    </div>
  );
}
