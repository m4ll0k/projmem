import { useEffect, useState } from "react";
import hljs from "highlight.js/lib/common";
import { useStore } from "../store";
import { api } from "../api";
import type {
  OpenLease, LifelineDetail, Annotation, FileEvent,
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

// ── Lease card — "Open leases — for what reason?" ──────────────────────────

function leaseOrigin(intent: string | null | undefined): {
  label: string; tone: string;
} {
  if (!intent) return { label: "—",         tone: "text-muted" };
  if (intent.startsWith("implicit"))   return { label: "implicit",   tone: "text-warn" };
  if (intent.startsWith("claude-code"))return { label: "Claude Code",tone: "text-accent" };
  if (intent.startsWith("inferred"))   return { label: "inferred",   tone: "text-muted" };
  return { label: "manual", tone: "text-ink" };
}

function LeaseCard({ lease, dimmed }: { lease: OpenLease; dimmed: boolean }) {
  const remaining = lease.expires_at - Date.now() / 1000;
  const isPending = lease.state === "pending_approval";
  const origin    = leaseOrigin(lease.intent);

  return (
    <div
      className={`rounded-md border px-2.5 py-2 transition-opacity ${
        dimmed ? "opacity-50" : "opacity-100"
      } ${isPending
        ? "border-bad/40 bg-bad/5"
        : "border-line bg-elev hover:border-accent/40"}`}
    >
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className={`text-[10px] font-mono uppercase tracking-wider ${origin.tone}`}>
          {origin.label}
        </span>
        <span className={isPending ? "text-bad text-[11px] font-medium" : "text-muted text-[10px] font-mono"}>
          {isPending ? "PENDING APPROVAL" : lease.state}
        </span>
      </div>
      {/* Reason — promoted to the headline per user feedback */}
      <div className="text-xs leading-snug mb-1.5">
        {lease.intent || <span className="text-muted">(no reason given)</span>}
      </div>
      <div className="flex items-center gap-2 text-[10px] font-mono text-muted tabular-nums">
        <span title="lease id">{lease.id.slice(0, 8)}…</span>
        <span>·</span>
        <span title="time to expiry">{fmtDuration(Math.max(0, remaining))}</span>
        {lease.agent_id && <>
          <span>·</span>
          <span>{lease.agent_id}</span>
        </>}
      </div>
      {isPending && (
        <div className="mt-2 flex gap-1">
          <button
            onClick={() => api.approve(lease.id)}
            className="rounded bg-good text-white text-[11px] px-2 py-0.5 hover:opacity-90"
          >Approve</button>
          <button
            onClick={() => api.deny(lease.id)}
            className="rounded bg-bad text-white text-[11px] px-2 py-0.5 hover:opacity-90"
          >Deny</button>
        </div>
      )}
    </div>
  );
}

// ── Notes / Guidance / Critical rendering ──────────────────────────────────

function NoteRow({ note }: { note: Annotation }) {
  const sev = note.severity || note.kind;
  const stalenessColor = (
    note.staleness === "contradicted"   ? "text-bad font-medium" :
    note.staleness === "strongly_stale" ? "text-warn"             :
    note.staleness === "fresh"          ? "text-good"             :
                                          "text-muted"
  );
  return (
    <div className="rounded-md border border-line bg-elev px-2.5 py-2">
      <div className="flex items-center justify-between text-[11px] mb-1">
        <span className="font-medium">[{sev}]</span>
        <span className={`${stalenessColor} text-[10px] uppercase tracking-wider`}>
          {note.staleness || "—"}
        </span>
      </div>
      <div className="text-xs whitespace-pre-wrap break-words leading-snug">
        {note.body}
      </div>
    </div>
  );
}

function HistoryRow({ ev }: { ev: FileEvent }) {
  return (
    <div className="border-l-2 border-line pl-2.5 py-1">
      <div className="text-[10px] font-mono text-muted tabular-nums">{fmtTime(ev.at)}</div>
      <div className="text-xs leading-snug">
        <span className="font-medium">{ev.kind}</span>
        {ev.reason && <span className="text-muted"> — {ev.reason}</span>}
      </div>
    </div>
  );
}

// ── Inline AddNote / AddGuidance form ──────────────────────────────────────

function AddNoteForm({ target, kind, onSaved }: {
  target: string;
  kind: "note" | "guidance";
  onSaved: () => void;
}) {
  const [open, setOpen]         = useState(false);
  const [body, setBody]         = useState("");
  const [severity, setSeverity] = useState<"info" | "warn" | "critical">("info");
  const [saving, setSaving]     = useState(false);
  const [err, setErr]           = useState<string | null>(null);

  const save = async () => {
    if (!body.trim()) return;
    setSaving(true); setErr(null);
    try {
      await api.addNote({
        target, body, kind,
        ...(kind === "guidance" ? { severity } : {}),
      });
      setBody(""); setOpen(false); onSaved();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-[11px] text-accent hover:underline"
      >+ add {kind}</button>
    );
  }
  return (
    <div className="rounded-md border border-accent/30 bg-accent/5 p-2 space-y-1.5">
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        rows={3}
        placeholder={kind === "guidance"
          ? "Advice for the agent. Inline `name` is defined at file:line auto-extracts a FACT claim."
          : "Free-text note. Backtick-around-symbol-name + file:line auto-extracts a FACT claim."}
        className="w-full text-xs font-mono bg-bg border border-line rounded p-1.5 text-ink"
      />
      {kind === "guidance" && (
        <div className="flex items-center gap-1 text-[11px]">
          <span className="text-muted">severity:</span>
          {(["info", "warn", "critical"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSeverity(s)}
              className={`px-1.5 py-0.5 rounded border ${
                severity === s
                  ? "border-accent text-accent bg-accent/10"
                  : "border-line text-muted hover:bg-sunken"
              }`}
            >{s}</button>
          ))}
        </div>
      )}
      {err && <div className="text-[11px] text-bad">{err}</div>}
      <div className="flex gap-1">
        <button onClick={save} disabled={saving}
                className="rounded bg-accent text-accent-fg text-[11px] px-2 py-0.5 disabled:opacity-50">
          {saving ? "saving…" : "save"}
        </button>
        <button onClick={() => { setOpen(false); setBody(""); setErr(null); }}
                className="rounded border border-line text-[11px] px-2 py-0.5 text-muted hover:bg-sunken">cancel</button>
      </div>
    </div>
  );
}

// ── Code tab with line numbers + highlight.js ──────────────────────────────

function langFromPath(p: string): string | undefined {
  const ext = p.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "py":   return "python";
    case "ts":   return "typescript";
    case "tsx":  return "typescript";
    case "js":   return "javascript";
    case "jsx":  return "javascript";
    case "rs":   return "rust";
    case "go":   return "go";
    case "java": return "java";
    case "rb":   return "ruby";
    case "sh":   return "bash";
    case "json": return "json";
    case "yml":
    case "yaml": return "yaml";
    case "md":   return "markdown";
    case "html": return "xml";
    case "css":  return "css";
    case "c":
    case "h":    return "c";
    case "cpp":
    case "cc":
    case "hpp":  return "cpp";
    case "sql":  return "sql";
    case "toml": return "ini";
    default:     return undefined;
  }
}

function CodeTab({ target }: { target: string }) {
  const [content, setContent] = useState<{text?: string; binary?: boolean; truncated?: boolean; size?: number} | null>(null);
  const [err, setErr]         = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setErr(null);
    api.file(target)
      .then((d) => !cancelled && setContent(d))
      .catch((e) => !cancelled && setErr(String(e?.message ?? e)))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [target]);
  if (loading) return <div className="text-xs text-muted">loading…</div>;
  if (err)     return <div className="text-xs text-bad">{err}</div>;
  if (!content) return <div className="text-xs text-muted">(no content)</div>;
  if (content.binary) {
    return <div className="text-xs text-muted">
      binary file ({content.size} bytes) — not shown
    </div>;
  }
  const text = content.text ?? "";
  const lang = langFromPath(target);
  let highlighted: string;
  try {
    highlighted = lang
      ? hljs.highlight(text, { language: lang, ignoreIllegals: true }).value
      : hljs.highlightAuto(text).value;
  } catch {
    highlighted = text.replace(/[&<>]/g,
      (c) => c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;");
  }
  const lineCount = text.split("\n").length;
  return (
    <div className="rounded-md border border-line bg-code-bg overflow-hidden">
      {content.truncated && (
        <div className="text-[10px] text-warn px-2 py-1 border-b border-line">
          truncated to 200 KB ({content.size} bytes total)
        </div>
      )}
      <div className="flex max-h-[60vh] overflow-auto text-[11px] font-mono leading-[1.45]">
        <div className="select-none text-right text-muted px-2 py-2 tabular-nums border-r border-line-soft">
          {Array.from({ length: lineCount }, (_, i) => i + 1).map((n) => (
            <div key={n}>{n}</div>
          ))}
        </div>
        <pre className="px-3 py-2 flex-1 whitespace-pre overflow-x-auto">
          <code className="hljs"
                dangerouslySetInnerHTML={{ __html: highlighted }} />
        </pre>
      </div>
    </div>
  );
}

// ── Inspector main ─────────────────────────────────────────────────────────

type Tab = "notes" | "guidance" | "critical" | "history" | "code";

export function Inspector() {
  const selectedEvent    = useStore((s) => s.selectedEvent);
  const openLeases       = useStore((s) => s.openLeases);
  const selectedLifeline = useStore((s) => s.selectedLifeline);
  const liveLeasedPaths  = useStore((s) => s.liveLeasedPaths);

  const [tab, setTab]               = useState<Tab>("notes");
  const [detail, setDetail]         = useState<LifelineDetail | null>(null);
  const [loading, setLoading]       = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    if (!selectedLifeline) { setDetail(null); return; }
    let cancelled = false;
    setLoading(true);
    api.lifeline(selectedLifeline)
      .then((d) => !cancelled && setDetail(d))
      .catch(()  => !cancelled && setDetail(null))
      .finally(()=> !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [selectedLifeline, refreshTick]);

  const guidance   = (detail?.notes ?? []).filter((n) =>
    ["guidance", "constraint", "preference"].includes(n.kind));
  const plainNotes = (detail?.notes ?? []).filter((n) =>
    !["guidance", "constraint", "preference", "critical"].includes(n.kind));

  // When a node is selected, prefer showing its own lease prominently +
  // collapsing the others. When no selection, all leases are equally
  // visible (helps the "what is going on?" overview).
  const selectedPath = detail?.lifeline?.current_path ?? null;
  const sortedLeases = [...openLeases].sort((a, b) => {
    const aOwn = a.lifeline_id === selectedLifeline ? 0 : 1;
    const bOwn = b.lifeline_id === selectedLifeline ? 0 : 1;
    return aOwn - bOwn || a.opened_at - b.opened_at;
  });

  return (
    <aside className="h-full w-80 border-l border-line bg-bg flex flex-col">
      <div className="border-b border-line px-3 py-2 text-xs font-semibold tracking-tight">
        Inspector
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        <section>
          <div className="flex items-baseline justify-between mb-1.5">
            <div className="text-[11px] uppercase tracking-wider text-muted">
              Open leases ({openLeases.length})
            </div>
            {liveLeasedPaths.length > 0 && (
              <div className="text-[10px] text-accent">
                {liveLeasedPaths.length} live
              </div>
            )}
          </div>
          {openLeases.length === 0 && <div className="text-xs text-muted">(none)</div>}
          <div className="space-y-1.5">
            {sortedLeases.map((l) => (
              <LeaseCard
                key={l.id}
                lease={l}
                dimmed={selectedLifeline != null
                         && l.lifeline_id !== selectedLifeline}
              />
            ))}
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
                <div className="flex gap-1 text-[11px] mb-2 flex-wrap">
                  {(["notes", "guidance", "critical", "history", "code"] as Tab[]).map((t) => (
                    <button
                      key={t}
                      onClick={() => setTab(t)}
                      className={`px-2 py-0.5 rounded border transition-colors ${
                        tab === t
                          ? "border-accent text-accent bg-accent/10"
                          : "border-line text-muted hover:bg-sunken"
                      }`}
                    >{t}</button>
                  ))}
                </div>
                {tab === "notes" && selectedPath && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedPath} kind="note"
                      onSaved={() => setRefreshTick((n) => n + 1)}
                    />
                    {plainNotes.length === 0 && <div className="text-xs text-muted">(no notes)</div>}
                    {plainNotes.map((n) => <NoteRow key={n.id} note={n} />)}
                  </div>
                )}
                {tab === "guidance" && selectedPath && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedPath} kind="guidance"
                      onSaved={() => setRefreshTick((n) => n + 1)}
                    />
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
                {tab === "code" && selectedPath && (
                  <CodeTab target={selectedPath} />
                )}
                {tab === "code" && !selectedPath && (
                  <div className="text-xs text-muted">(tombstoned — no current file)</div>
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
            <pre className="text-[11px] font-mono leading-snug bg-code-bg border border-line rounded p-2 overflow-x-auto">
{JSON.stringify(selectedEvent, null, 2)}
            </pre>
          </section>
        )}
      </div>
    </aside>
  );
}
