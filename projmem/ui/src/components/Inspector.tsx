import { useStore } from "../store";
import { api } from "../api";
import type { DaemonEvent, OpenLease } from "../types";

function fmtDuration(s: number): string {
  if (s < 60)      return `${Math.round(s)}s`;
  if (s < 3600)    return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function LeaseRow({ lease }: { lease: OpenLease }) {
  const remaining = lease.expires_at - Date.now() / 1000;
  const isPending = lease.state === "pending_approval";
  return (
    <div className={`rounded border px-2 py-1.5 ${
      isPending ? "border-bad bg-red-50" : "border-line bg-paper"
    }`}>
      <div className="flex items-center justify-between text-xs">
        <span className="font-mono">{lease.id.slice(0, 8)}…</span>
        <span className={isPending ? "text-bad font-medium" : "text-muted"}>
          {lease.state}
        </span>
      </div>
      {lease.intent && (
        <div className="mt-1 text-xs text-muted line-clamp-2">
          {lease.intent}
        </div>
      )}
      <div className="mt-1 text-[10px] font-mono text-muted">
        ttl: {fmtDuration(Math.max(0, remaining))}
        {lease.agent_id && <> · agent: {lease.agent_id}</>}
      </div>
      {isPending && (
        <div className="mt-2 flex gap-1">
          <button
            onClick={() => api.approve(lease.id)}
            className="rounded bg-good px-2 py-0.5 text-[11px] text-white hover:bg-green-700"
          >Approve</button>
          <button
            onClick={() => api.deny(lease.id)}
            className="rounded bg-bad px-2 py-0.5 text-[11px] text-white hover:bg-red-700"
          >Deny</button>
        </div>
      )}
    </div>
  );
}

function EventDetail({ ev }: { ev: DaemonEvent }) {
  return (
    <div>
      <div className="text-xs font-semibold tracking-tight mb-1">
        {ev.kind}
      </div>
      <pre className="text-[11px] font-mono leading-snug bg-white border border-line rounded p-2 overflow-x-auto">
{JSON.stringify(ev, null, 2)}
      </pre>
    </div>
  );
}

export function Inspector() {
  const selectedEvent = useStore((s) => s.selectedEvent);
  const openLeases    = useStore((s) => s.openLeases);

  return (
    <aside className="h-full w-80 border-l border-line bg-paper flex flex-col">
      <div className="border-b border-line px-3 py-2 text-xs font-semibold tracking-tight">
        Inspector
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        <section>
          <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
            Open leases ({openLeases.length})
          </div>
          {openLeases.length === 0 && (
            <div className="text-xs text-muted">(none)</div>
          )}
          <div className="space-y-1.5">
            {openLeases.map((l) => <LeaseRow key={l.id} lease={l} />)}
          </div>
        </section>
        <section>
          <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
            Selected event
          </div>
          {selectedEvent
            ? <EventDetail ev={selectedEvent} />
            : <div className="text-xs text-muted">
                click an activity row to inspect
              </div>}
        </section>
      </div>
    </aside>
  );
}
