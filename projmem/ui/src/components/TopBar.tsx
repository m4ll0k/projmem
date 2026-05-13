import { useStore } from "../store";
import { api } from "../api";

export function TopBar() {
  const paused      = useStore((s) => s.paused);
  const wsStatus    = useStore((s) => s.wsStatus);
  const setPaused   = useStore((s) => s.setPaused);
  const projectRoot = useStore((s) => s.projectRoot);

  const toggle = async () => {
    try {
      const r = paused ? await api.resume() : await api.pause();
      setPaused((r as { paused: boolean }).paused);
    } catch {
      /* surface in a future toast */
    }
  };

  const statusColor =
    wsStatus === "open" ? "bg-good"
    : wsStatus === "error" ? "bg-bad"
    : wsStatus === "closed" ? "bg-warn"
    : "bg-muted";

  return (
    <header className="flex items-center gap-4 border-b border-line bg-white px-4 py-2">
      <div className="font-semibold tracking-tight">projmem</div>
      <div className="text-xs text-muted font-mono truncate" title={projectRoot}>
        {projectRoot || "—"}
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-2 text-xs">
        <span className={`inline-block h-2 w-2 rounded-full ${statusColor}`} />
        <span className="text-muted">{wsStatus}</span>
      </div>
      <button
        onClick={toggle}
        className={`rounded px-3 py-1 text-xs font-medium border
          ${paused
            ? "border-bad bg-red-50 text-bad hover:bg-red-100"
            : "border-line bg-paper hover:bg-line"}`}
      >
        {paused ? "▶ Resume agent" : "⏸ Pause agent"}
      </button>
    </header>
  );
}
