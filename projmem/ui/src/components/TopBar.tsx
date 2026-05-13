import { useState } from "react";
import { useStore } from "../store";
import { api } from "../api";
import { Button } from "./Button";

export function TopBar() {
  const paused       = useStore((s) => s.paused);
  const wsStatus     = useStore((s) => s.wsStatus);
  const setPaused    = useStore((s) => s.setPaused);
  const projectRoot  = useStore((s) => s.projectRoot);
  const theme        = useStore((s) => s.theme);
  const toggleTheme  = useStore((s) => s.toggleTheme);
  const liveLeases   = useStore((s) => s.liveLeasedPaths);

  const [pauseBusy, setPauseBusy] = useState(false);
  const togglePause = async () => {
    setPauseBusy(true);
    try {
      const r = paused ? await api.resume() : await api.pause();
      setPaused((r as { paused: boolean }).paused);
    } catch {
      /* surface in a future toast */
    } finally {
      setPauseBusy(false);
    }
  };

  const statusColor =
    wsStatus === "open"   ? "bg-good"
    : wsStatus === "error"  ? "bg-bad"
    : wsStatus === "closed" ? "bg-warn"
    : "bg-muted";

  return (
    <header className="flex items-center gap-3 border-b border-line bg-elev px-3 sm:px-4 py-2 shadow-soft min-w-0">
      <div className="flex items-center gap-2 min-w-0 flex-shrink-0">
        <span className="inline-block w-2 h-2 rounded-full bg-accent" />
        <span className="font-semibold tracking-tight text-ink">projmem</span>
      </div>
      <div className="text-[11px] text-muted font-mono truncate flex-1 min-w-0"
           title={projectRoot}>
        {projectRoot || "—"}
      </div>
      {liveLeases.length > 0 && (
        <span className="hidden sm:inline-flex text-[11px] text-accent font-medium px-1.5 py-0.5 rounded bg-accent/10 border border-accent/20 flex-shrink-0">
          {liveLeases.length} editing
        </span>
      )}
      <div className="flex items-center gap-1.5 text-xs text-muted flex-shrink-0">
        <span className={`inline-block h-2 w-2 rounded-full ${statusColor}`}
              title={`websocket: ${wsStatus}`} />
        <span className="hidden md:inline">{wsStatus}</span>
      </div>
      <Button
        variant="ghost" size="sm"
        onClick={toggleTheme}
        title={`switch to ${theme === "dark" ? "light" : "dark"} mode`}
        aria-label="toggle theme"
      >
        {theme === "dark" ? "☀" : "☾"}
        <span className="hidden sm:inline">{theme === "dark" ? "light" : "dark"}</span>
      </Button>
      <Button
        variant={paused ? "danger" : "secondary"}
        size="sm"
        loading={pauseBusy}
        onClick={togglePause}
      >
        {paused ? "▶ Resume agent" : "⏸ Pause agent"}
      </Button>
    </header>
  );
}
