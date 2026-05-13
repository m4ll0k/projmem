import { useEffect, useState } from "react";
import { useStore } from "../store";
import { api } from "../api";
import type {
  DaemonEvent, OpenLease, LifelineDetail, Annotation, FileEvent,
} from "../types";

function fmtDuration(s: number): string {
  if (s < 60)   return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function fmtTime(t?: number | null): string {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString();
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
        <div className="mt-1 text-xs text-muted line-clamp-2">{lease.intent}</div>
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

function NoteRow({ note }: { note: Annotation }) {
  const sev = note.severity || note.kind;
  const stalenessColor = (
    note.staleness === "contradicted" ? "text-bad font-medium" :
    note.staleness === "strongly_stale" ? "text-warn" :
    note.staleness === "fresh" ? "text-good" : "text-muted"
  );
  return (
    <div className="rounded border border-line bg-white px-2 py-1.5">
      <div className="flex items-center justify-between text-[11px]">
        <span className="font-medium">[{sev}]</span>
        <span className={stalenessColor}>{note.staleness || "—"}</span>
      </div>
      <div className="mt-1 text-xs whitespace-pre-wrap break-words">
        {note.body}
      </div>
    </div>
  );
}

function HistoryRow({ ev }: { ev: FileEvent }) {
  return (
    <div className="border-l-2 border-line pl-2 py-1">
      <div className="text-[10px] font-mono text-muted">{fmtTime(ev.at)}</div>
      <div className="text-xs">
        <span className="font-medium">{ev.kind}</span>
        {ev.reason && <span className="text-muted"> — {ev.reason}</span>}
      </div>
    </div>
  );
}

type Tab = "notes" | "guidance" | "critical" | "history";

export function Inspector() {
  const selectedEvent    = useStore((s) => s.selectedEvent);
  const openLeases       = useStore((s) => s.openLeases);
  const selectedLifeline = useStore((s) => s.selectedLifeline);

  const [tab, setTab]       = useState<Tab>("notes");
  const [detail, setDetail] = useState<LifelineDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedLifeline) { setDetail(null); return; }
    let cancelled = false;
    setLoading(true);
    api.lifeline(selectedLifeline)
      .then((d) => !cancelled && setDetail(d))
      .catch(()  => !cancelled && setDetail(null))
      .finally(()=> !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [selectedLifeline]);

  const guidance = (detail?.notes ?? []).filter((n) =>
    ["guidance", "constraint", "preference"].includes(n.kind),
  );
  const plainNotes = (detail?.notes ?? []).filter((n) =>
    !["guidance", "constraint", "preference", "critical"].includes(n.kind),
  );

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
          {openLeases.length === 0 && <div className="text-xs text-muted">(none)</div>}
          <div className="space-y-1.5">
            {openLeases.map((l) => <LeaseRow key={l.id} lease={l} />)}
          </div>
        </section>

        {selectedLifeline && (
          <section>
            <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
              Selected node
            </div>
            {loading && <div className="text-xs text-muted">loading…</div>}
            {detail && (
              <>
                <div className="text-xs font-mono mb-2 break-all">
                  {detail.lifeline.current_path ?? "(tombstoned)"}
                </div>
                <div className="flex gap-1 text-[11px] mb-2">
                  {(["notes", "guidance", "critical", "history"] as Tab[]).map((t) => (
                    <button
                      key={t}
                      onClick={() => setTab(t)}
                      className={`px-2 py-0.5 rounded border ${
                        tab === t
                          ? "border-accent bg-accent/10 text-accent"
                          : "border-line text-muted hover:bg-line/40"
                      }`}
                    >{t}</button>
                  ))}
                </div>
                {tab === "notes" && (
                  <div className="space-y-1.5">
                    {plainNotes.length === 0 && <div className="text-xs text-muted">(no notes)</div>}
                    {plainNotes.map((n) => <NoteRow key={n.id} note={n} />)}
                  </div>
                )}
                {tab === "guidance" && (
                  <div className="space-y-1.5">
                    {guidance.length === 0 && <div className="text-xs text-muted">(no guidance)</div>}
                    {guidance.map((n) => <NoteRow key={n.id} note={n} />)}
                  </div>
                )}
                {tab === "critical" && (
                  <div className="space-y-1.5">
                    {detail.critical.length === 0 && <div className="text-xs text-muted">(no critical notes)</div>}
                    {detail.critical.map((n) => <NoteRow key={n.id} note={n} />)}
                  </div>
                )}
                {tab === "history" && (
                  <div className="space-y-1">
                    {detail.events.length === 0 && <div className="text-xs text-muted">(no events)</div>}
                    {detail.events.map((e) => <HistoryRow key={e.id} ev={e} />)}
                  </div>
                )}
              </>
            )}
          </section>
        )}

        {selectedEvent && !selectedLifeline && (
          <section>
            <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
              Selected event
            </div>
            <pre className="text-[11px] font-mono leading-snug bg-white border border-line rounded p-2 overflow-x-auto">
{JSON.stringify(selectedEvent, null, 2)}
            </pre>
          </section>
        )}
      </div>
    </aside>
  );
}
