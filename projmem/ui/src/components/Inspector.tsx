import { useEffect, useState } from "react";
import hljs from "highlight.js/lib/common";
import { useStore } from "../store";
import { api } from "../api";
import { Button } from "./Button";
import type {
  OpenLease, LifelineDetail, Annotation, FileEvent,
} from "../types";

// ─── utilities ─────────────────────────────────────────────────────────────

function fmtDuration(s: number): string {
  if (s < 60)   return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function fmtTime(t?: number | null): string {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString();
}

function fmtRelative(t?: number | null): string {
  if (!t) return "—";
  const dt = Math.floor(Date.now() / 1000 - t);
  if (dt < 60)        return `${dt}s ago`;
  if (dt < 3600)      return `${Math.floor(dt / 60)}m ago`;
  if (dt < 86400)     return `${Math.floor(dt / 3600)}h ago`;
  return `${Math.floor(dt / 86400)}d ago`;
}

// ─── Open-leases card ──────────────────────────────────────────────────────

function leaseOrigin(intent: string | null | undefined): {
  label: string; tone: string;
} {
  if (!intent) return { label: "—",         tone: "text-muted" };
  if (intent.startsWith("implicit"))    return { label: "implicit",    tone: "text-warn" };
  if (intent.startsWith("claude-code")) return { label: "Claude Code", tone: "text-accent" };
  if (intent.startsWith("inferred"))    return { label: "inferred",    tone: "text-muted" };
  return { label: "manual", tone: "text-ink" };
}

function LeaseCard({ lease, dimmed }: { lease: OpenLease; dimmed: boolean }) {
  const remaining = lease.expires_at - Date.now() / 1000;
  const isPending = lease.state === "pending_approval";
  const origin    = leaseOrigin(lease.intent);
  const setSelectedLifeline = useStore((s) => s.setSelectedLifeline);

  // Fetch lifeline detail to surface attached notes / critical count
  // INSIDE the lease card — answers "what's pinned to this file?"
  // at a glance without expanding the inspector tabs.
  const [detail, setDetail] = useState<LifelineDetail | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.lifeline(lease.lifeline_id)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, [lease.lifeline_id]);

  const noteCount = (detail?.notes ?? []).filter((n) =>
    !["guidance", "constraint", "preference", "critical"].includes(n.kind),
  ).length;
  const guidanceCount = (detail?.notes ?? []).filter((n) =>
    ["guidance", "constraint", "preference"].includes(n.kind),
  ).length;
  const criticalCount = (detail?.critical ?? []).length;

  return (
    <div
      onClick={() => setSelectedLifeline(lease.lifeline_id)}
      className={`rounded-md border px-2.5 py-2 transition cursor-pointer ${
        dimmed ? "opacity-50" : "opacity-100"
      } ${isPending
        ? "border-bad/40 bg-bad/5"
        : "border-line bg-elev hover:border-accent/50"}`}
    >
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className={`text-[10px] font-mono uppercase tracking-wider ${origin.tone}`}>
          {origin.label}
        </span>
        <span className={isPending
          ? "text-bad text-[11px] font-medium"
          : "text-muted text-[10px] font-mono"}>
          {isPending ? "PENDING APPROVAL" : lease.state}
        </span>
      </div>

      {/* Path — clickable to focus the inspector + (in graph view) pan */}
      {detail?.lifeline?.current_path && (
        <div className="text-[11px] font-mono mb-1 break-all text-ink">
          {detail.lifeline.current_path}
        </div>
      )}

      {/* Reason — headline */}
      <div className="text-xs leading-snug mb-1.5">
        {lease.intent || <span className="text-muted">(no reason given)</span>}
      </div>

      {/* Linked notes summary */}
      {detail && (noteCount + guidanceCount + criticalCount > 0) && (
        <div className="flex items-center gap-1.5 mb-1.5 text-[10px]">
          {criticalCount > 0 && (
            <span className="px-1 py-px rounded bg-bad/15 text-bad border border-bad/30">
              ⚠ {criticalCount} critical
            </span>
          )}
          {guidanceCount > 0 && (
            <span className="px-1 py-px rounded bg-accent/15 text-accent border border-accent/30">
              {guidanceCount} guidance
            </span>
          )}
          {noteCount > 0 && (
            <span className="px-1 py-px rounded bg-warn/10 text-warn border border-warn/20">
              {noteCount} note{noteCount > 1 ? "s" : ""}
            </span>
          )}
        </div>
      )}

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
        <div className="mt-2 flex gap-1.5">
          <Button
            variant="success" size="sm"
            onClick={(e) => { e.stopPropagation(); api.approve(lease.id); }}
          >Approve</Button>
          <Button
            variant="danger" size="sm"
            onClick={(e) => { e.stopPropagation(); api.deny(lease.id); }}
          >Deny</Button>
        </div>
      )}
    </div>
  );
}

// ─── Notes / Guidance rendering ────────────────────────────────────────────

// Parse markdown-style links out of free-text. Returns alternating
// text + link parts so the renderer can wrap external refs (papers,
// PDFs, internal docs) as clickable affordances.
type BodyPart =
  | { type: "text"; value: string }
  | { type: "link"; title: string; url: string };

function parseBody(text: string): BodyPart[] {
  const parts: BodyPart[] = [];
  let lastEnd = 0;
  const re = /\[([^\]]+)\]\(([^)]+)\)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastEnd) {
      parts.push({ type: "text", value: text.slice(lastEnd, m.index) });
    }
    parts.push({ type: "link", title: m[1], url: m[2] });
    lastEnd = m.index + m[0].length;
  }
  if (lastEnd < text.length) {
    parts.push({ type: "text", value: text.slice(lastEnd) });
  }
  return parts;
}

function resolveRefUrl(url: string): string {
  if (url.startsWith("http://") || url.startsWith("https://")) return url;
  if (url.startsWith(".projmem/refs/")) {
    return api.refUrl(url.slice(".projmem/refs/".length));
  }
  if (url.startsWith("refs/")) {
    return api.refUrl(url.slice("refs/".length));
  }
  // Bare filename — assume it's already relative to .projmem/refs/.
  return api.refUrl(url);
}

function isExternalUrl(url: string): boolean {
  return url.startsWith("http://") || url.startsWith("https://");
}

function NoteRow({ note, onShowCode }: {
  note: Annotation;
  onShowCode?: (line?: number) => void;
}) {
  const sev = note.severity || note.kind;
  const stalenessColor = (
    note.staleness === "contradicted"   ? "text-bad font-medium" :
    note.staleness === "strongly_stale" ? "text-warn"             :
    note.staleness === "fresh"          ? "text-good"             :
                                          "text-muted"
  );
  const lineMatch = (note.body || "").match(/:(\d+)\b/);
  const cited = lineMatch ? parseInt(lineMatch[1], 10) : undefined;
  const bodyParts = parseBody(note.body || "");
  const linkCount = bodyParts.filter((p) => p.type === "link").length;

  return (
    <div className="rounded-md border border-line bg-elev px-2.5 py-2">
      <div className="flex items-center justify-between text-[11px] mb-1">
        <span className="font-medium">[{sev}]</span>
        <div className="flex items-center gap-2">
          {linkCount > 0 && (
            <span className="text-[10px] text-muted">📎 {linkCount}</span>
          )}
          {onShowCode && cited && (
            <button onClick={() => onShowCode(cited)}
                    className="text-[10px] text-accent hover:underline">
              code:{cited}
            </button>
          )}
          <span className={`${stalenessColor} text-[10px] uppercase tracking-wider`}>
            {note.staleness || "—"}
          </span>
        </div>
      </div>
      <div className="text-xs whitespace-pre-wrap break-words leading-snug">
        {bodyParts.map((p, i) =>
          p.type === "text"
            ? <span key={i}>{p.value}</span>
            : <a key={i}
                 href={resolveRefUrl(p.url)}
                 target="_blank" rel="noopener noreferrer"
                 className="inline-flex items-center gap-0.5 text-accent
                            hover:underline break-all"
                 title={p.url}>
                {isExternalUrl(p.url) ? "🌐" : "📄"} {p.title}
              </a>
        )}
      </div>
    </div>
  );
}

// ─── History as a vertical timeline ────────────────────────────────────────

function HistoryEvent({ ev, last }: { ev: FileEvent; last: boolean }) {
  const kindStyle: Record<string, { color: string; glyph: string; bg: string }> = {
    created:   { color: "text-good",   glyph: "+", bg: "bg-good"   },
    edited:    { color: "text-accent", glyph: "✎", bg: "bg-accent" },
    leased:    { color: "text-accent", glyph: "◯", bg: "bg-accent" },
    released:  { color: "text-good",   glyph: "✓", bg: "bg-good"   },
    abandoned: { color: "text-warn",   glyph: "✗", bg: "bg-warn"   },
    moved:     { color: "text-accent", glyph: "→", bg: "bg-accent" },
    deleted:   { color: "text-bad",    glyph: "−", bg: "bg-bad"    },
  };
  const s = kindStyle[ev.kind] ?? { color: "text-muted", glyph: "·", bg: "bg-muted" };

  return (
    <li className="relative pl-7 pb-3">
      {/* timeline dot */}
      <span className={`absolute left-[8px] top-1 inline-flex items-center
                        justify-center w-4 h-4 rounded-full text-[10px]
                        text-white ${s.bg}`}>
        {s.glyph}
      </span>
      {/* vertical rail to the next event */}
      {!last && (
        <span className="absolute left-[15px] top-5 bottom-0 w-px bg-line" />
      )}
      <div className="text-[10px] font-mono text-muted tabular-nums leading-tight mb-0.5">
        {fmtTime(ev.at)} · <span title={fmtTime(ev.at)}>{fmtRelative(ev.at)}</span>
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className={`text-xs font-semibold ${s.color}`}>{ev.kind}</span>
      </div>
      {ev.reason && (
        <div className="text-[11px] text-ink leading-snug mt-0.5 break-words">
          {ev.reason}
        </div>
      )}
      {ev.diff_summary && (
        <div className="text-[10px] font-mono text-muted mt-1">
          {ev.diff_summary}
        </div>
      )}
    </li>
  );
}

function HistoryTimeline({ events }: { events: FileEvent[] }) {
  if (events.length === 0) {
    return <div className="text-xs text-muted">(no events)</div>;
  }
  // Show newest at the bottom (chronological reading order).
  return (
    <ul className="list-none m-0 p-0">
      {events.map((e, i) => (
        <HistoryEvent key={e.id} ev={e} last={i === events.length - 1} />
      ))}
    </ul>
  );
}

// ─── Add-note inline form ──────────────────────────────────────────────────

function AddNoteForm({ target, kind, onSaved, onPickLine }: {
  target: string;
  kind: "note" | "guidance";
  onSaved: () => void;
  onPickLine?: () => number | undefined;
}) {
  const [open, setOpen]         = useState(false);
  const [body, setBody]         = useState("");
  const [line, setLine]         = useState<string>("");
  const [severity, setSeverity] = useState<"info" | "warn" | "critical">("info");
  const [saving, setSaving]     = useState(false);
  const [err, setErr]           = useState<string | null>(null);

  const save = async () => {
    if (!body.trim()) return;
    setSaving(true); setErr(null);
    try {
      let finalBody = body;
      const ln = line.trim();
      if (ln && /^\d+$/.test(ln)) {
        // Prepend a file:line citation so the v1 auto-extractor picks
        // up the FACT shape.
        finalBody = `${target}:${ln} — ${body}`;
      }
      await api.addNote({
        target, body: finalBody, kind,
        ...(kind === "guidance" ? { severity } : {}),
      });
      setBody(""); setLine(""); setOpen(false); onSaved();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <Button
        variant="subtle" size="xs"
        onClick={() => setOpen(true)}
        aria-label={`add ${kind}`}
      >
        + add {kind}
      </Button>
    );
  }
  return (
    <div className="rounded-md border border-accent/30 bg-accent/5 p-2 space-y-1.5">
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        rows={3}
        placeholder={kind === "guidance"
          ? "Advice for the agent. Backtick around symbol + file:line auto-extracts a FACT claim."
          : "Free-text note. Backtick-around-symbol-name + file:line auto-extracts a FACT claim."}
        className="w-full text-xs font-mono bg-bg border border-line rounded p-1.5 text-ink"
      />
      <div className="flex items-center gap-2 text-[11px] flex-wrap">
        <span className="text-muted">line:</span>
        <input
          value={line}
          onChange={(e) => setLine(e.target.value.replace(/[^\d]/g, ""))}
          placeholder="optional"
          className="w-16 bg-bg border border-line rounded px-1 py-0.5 text-ink font-mono"
        />
        <AttachRefButton onPick={(md) => setBody((b) => b ? b + " " + md : md)} />
        {kind === "guidance" && (
          <>
            <span className="text-muted ml-2">severity:</span>
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
          </>
        )}
      </div>
      {err && (
        <div className="text-[11px] text-bad bg-bad/5 border border-bad/20 rounded px-1.5 py-1">
          {err}
        </div>
      )}
      <div className="flex gap-1.5">
        <Button
          variant="primary" size="sm"
          loading={saving}
          onClick={save}
          disabled={!body.trim()}
        >
          save
        </Button>
        <Button
          variant="secondary" size="sm"
          onClick={() => { setOpen(false); setBody(""); setLine(""); setErr(null); }}
        >
          cancel
        </Button>
      </div>
    </div>
  );
}

// ─── Attach-reference button ───────────────────────────────────────────────
// Lets the operator insert a `[title](url-or-path)` markdown link into
// the note body. Two flavors: external URL (paste the URL) or pick
// from the project's .projmem/refs/ library. The latter is the
// game-changer for "we have a paper that justifies this guidance" —
// drop the PDF in .projmem/refs/, link to it from the note, the
// daemon serves it through GET /refs/<path> with the right MIME type.

function AttachRefButton({ onPick }: { onPick: (md: string) => void }) {
  const [open, setOpen]     = useState(false);
  const [title, setTitle]   = useState("");
  const [url, setUrl]       = useState("");
  const [refs, setRefs]     = useState<{path: string; size: number}[]>([]);
  const [refsErr, setRefsErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    api.refsList()
      .then((d) => setRefs(d.refs))
      .catch((e) => setRefsErr(String(e?.message ?? e)));
  }, [open]);

  const insert = () => {
    if (!title.trim() || !url.trim()) return;
    onPick(`[${title.trim()}](${url.trim()})`);
    setTitle(""); setUrl(""); setOpen(false);
  };

  if (!open) {
    return (
      <Button
        variant="secondary" size="xs"
        onClick={() => setOpen(true)}
        aria-label="attach a reference"
      >
        📎 attach
      </Button>
    );
  }
  return (
    <div className="w-full mt-1 rounded-md border border-accent/30 bg-accent/5 p-1.5 space-y-1.5 text-[11px]">
      <div className="flex items-stretch gap-1 flex-wrap sm:flex-nowrap">
        <input value={title}
               onChange={(e) => setTitle(e.target.value)}
               placeholder="title (e.g. SEC-204 advisory)"
               className="flex-1 min-w-0 bg-bg border border-line rounded px-1.5 py-1 text-ink h-7"/>
        <input value={url}
               onChange={(e) => setUrl(e.target.value)}
               placeholder="https://… or refs/papers/foo.pdf"
               className="flex-[2] min-w-0 bg-bg border border-line rounded px-1.5 py-1 text-ink font-mono h-7"/>
        <Button
          variant="primary" size="sm"
          onClick={insert}
          disabled={!title.trim() || !url.trim()}
        >add</Button>
        <Button
          variant="ghost" size="sm"
          onClick={() => setOpen(false)}
          aria-label="cancel attach"
        >✕</Button>
      </div>
      {refsErr && <div className="text-bad">{refsErr}</div>}
      {refs.length > 0 ? (
        <div>
          <div className="text-muted mb-0.5">
            from <span className="font-mono">.projmem/refs/</span>:
          </div>
          <div className="flex flex-wrap gap-1 max-h-24 overflow-y-auto">
            {refs.slice(0, 25).map((r) => (
              <Button
                key={r.path}
                variant="secondary" size="xs"
                onClick={() => {
                  setUrl(`refs/${r.path}`);
                  if (!title) setTitle(r.path.split("/").pop() || r.path);
                }}
                title={r.path}
                className="font-mono"
              >
                {r.path}
              </Button>
            ))}
          </div>
        </div>
      ) : (
        <div className="text-muted">
          drop files in <span className="font-mono">.projmem/refs/</span> to
          surface them here (papers, PDFs, design docs, …)
        </div>
      )}
    </div>
  );
}

// ─── Code tab ──────────────────────────────────────────────────────────────

function langFromPath(p: string): string | undefined {
  const ext = p.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "py":   return "python";
    case "ts":
    case "tsx":  return "typescript";
    case "js":
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

function CodeTab({ target, scrollToLine }: {
  target: string; scrollToLine?: number;
}) {
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

  useEffect(() => {
    if (!scrollToLine) return;
    const el = document.getElementById(`code-line-${scrollToLine}`);
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [scrollToLine, content]);

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
  // Split into lines after highlighting; render each with its own
  // gutter row so we can scroll-to-line on demand.
  const lines = highlighted.split("\n");
  return (
    <div className="rounded-md border border-line bg-code-bg overflow-hidden">
      {content.truncated && (
        <div className="text-[10px] text-warn px-2 py-1 border-b border-line">
          truncated to 200 KB ({content.size} bytes total)
        </div>
      )}
      <div className="flex max-h-[60vh] overflow-auto text-[11px] font-mono leading-[1.45]">
        <div className="select-none text-right text-muted px-2 py-2 tabular-nums border-r border-line-soft">
          {lines.map((_, i) => (
            <div key={i + 1} id={`code-line-${i + 1}`}>{i + 1}</div>
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

// ─── Inspector main ────────────────────────────────────────────────────────

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
  const [codeLine, setCodeLine]     = useState<number | undefined>(undefined);

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

  const guidance = (detail?.notes ?? []).filter((n) =>
    ["guidance", "constraint", "preference"].includes(n.kind));
  const plainNotes = (detail?.notes ?? []).filter((n) =>
    !["guidance", "constraint", "preference", "critical"].includes(n.kind));

  const selectedPath = detail?.lifeline?.current_path ?? null;
  const sortedLeases = [...openLeases].sort((a, b) => {
    const aOwn = a.lifeline_id === selectedLifeline ? 0 : 1;
    const bOwn = b.lifeline_id === selectedLifeline ? 0 : 1;
    return aOwn - bOwn || a.opened_at - b.opened_at;
  });

  const jumpToCode = (line?: number) => {
    setCodeLine(line);
    setTab("code");
  };

  return (
    <aside className="lg:h-full w-full lg:w-auto border-t lg:border-t-0 lg:border-l border-line bg-bg flex flex-col min-h-0 max-h-[40vh] lg:max-h-none">
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
          {openLeases.length === 0 && (
            <div className="text-xs text-muted">(none — no agent is editing)</div>
          )}
          <div className="space-y-1.5">
            {sortedLeases.map((l) => (
              <LeaseCard
                key={l.id}
                lease={l}
                dimmed={selectedLifeline != null &&
                         l.lifeline_id !== selectedLifeline}
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
                    <Button
                      key={t}
                      size="xs"
                      variant={tab === t ? "primary" : "secondary"}
                      onClick={() => setTab(t)}
                    >{t}</Button>
                  ))}
                </div>

                {tab === "notes" && selectedPath && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedPath} kind="note"
                      onSaved={() => setRefreshTick((n) => n + 1)}
                    />
                    {plainNotes.length === 0 && <div className="text-xs text-muted">(no notes)</div>}
                    {plainNotes.map((n) => (
                      <NoteRow key={n.id} note={n} onShowCode={jumpToCode} />
                    ))}
                  </div>
                )}
                {tab === "guidance" && selectedPath && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedPath} kind="guidance"
                      onSaved={() => setRefreshTick((n) => n + 1)}
                    />
                    {guidance.length === 0 && <div className="text-xs text-muted">(no guidance)</div>}
                    {guidance.map((n) => (
                      <NoteRow key={n.id} note={n} onShowCode={jumpToCode} />
                    ))}
                  </div>
                )}
                {tab === "critical" && (
                  <div className="space-y-1.5">
                    {detail.critical.length === 0 && <div className="text-xs text-muted">(no critical notes)</div>}
                    {detail.critical.map((n) => (
                      <NoteRow key={n.id} note={n} onShowCode={jumpToCode} />
                    ))}
                  </div>
                )}
                {tab === "history" && (
                  <HistoryTimeline events={detail.events} />
                )}
                {tab === "code" && selectedPath && (
                  <CodeTab target={selectedPath} scrollToLine={codeLine} />
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
