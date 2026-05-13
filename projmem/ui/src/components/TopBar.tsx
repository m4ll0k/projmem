import { useStore } from "../store";
import { api } from "../api";

export function TopBar() {
  const paused       = useStore((s) => s.paused);
  const wsStatus     = useStore((s) => s.wsStatus);
  const setPaused    = useStore((s) => s.setPaused);
  const projectRoot  = useStore((s) => s.projectRoot);
  const theme        = useStore((s) => s.theme);
  const toggleTheme  = useStore((s) => s.toggleTheme);
  const liveLeases   = useStore((s) => s.liveLeasedPaths);

  const togglePause = async () => {
    try {
      const r = paused ? await api.resume() : await api.pause();
      setPaused((r as { paused: boolean }).paused);
    } catch {
      /* surface in a future toast */
    }
  };

  const statusColor =
    wsStatus === "open"   ? "bg-good"
    : wsStatus === "error"  ? "bg-bad"
    : wsStatus === "closed" ? "bg-warn"
    : "bg-muted";

  return (
    <header className="flex items-center gap-4 border-b border-line bg-elev px-4 py-2 shadow-soft">
      <div className="flex items-center gap-2">
        <span className="inline-block w-2 h-2 rounded-full bg-accent" />
        <span className="font-semibold tracking-tight text-ink">projmem</span>
      </div>
      <div className="text-xs text-muted font-mono truncate max-w-[40vw]"
           title={projectRoot}>
        {projectRoot || "—"}
      </div>
      {liveLeases.length > 0 && (
        <span className="text-[11px] text-accent font-medium px-1.5 py-0.5 rounded bg-accent/10 border border-accent/20">
          {liveLeases.length} editing
        </span>
      )}
      <div className="flex-1" />
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className={`inline-block h-2 w-2 rounded-full ${statusColor}`} />
        <span>{wsStatus}</span>
      </div>
      <button
        onClick={toggleTheme}
        title={`switch to ${theme === "dark" ? "light" : "dark"} mode`}
        className="rounded px-2 py-1 text-xs border border-line hover:bg-sunken text-muted hover:text-ink"
      >
        {theme === "dark" ? "☀ light" : "☾ dark"}
      </button>
      <button
        onClick={togglePause}
        className={`rounded px-3 py-1 text-xs font-medium border transition-colors
          ${paused
            ? "border-bad text-bad bg-bad/10 hover:bg-bad/20"
            : "border-line text-ink hover:bg-sunken"}`}
      >
        {paused ? "▶ Resume agent" : "⏸ Pause agent"}
      </button>
    </header>
  );
}
