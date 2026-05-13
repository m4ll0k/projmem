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

function NoteRow({ note, onShowCode, isFresh, onDeleted, onEdited }: {
  note: Annotation;
  onShowCode?: (line?: number) => void;
  isFresh?: boolean;          // pulse a moment when the row is newly saved
  onDeleted?: () => void;
  onEdited?: () => void;
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
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting]     = useState(false);
  const [editing, setEditing]       = useState(false);
  const [draft, setDraft]           = useState(note.body || "");
  const [editSeverity, setEditSeverity] = useState<string | undefined>(
    note.severity ?? undefined,
  );
  const [savingEdit, setSavingEdit] = useState(false);
  const [editErr, setEditErr]       = useState<string | null>(null);
  // Auto-revert the confirm state if the user wanders away.
  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 5000);
    return () => clearTimeout(t);
  }, [confirming]);
  // Re-sync the edit draft if the underlying note changes (refetch
  // landed while edit-mode was open).
  useEffect(() => {
    if (!editing) setDraft(note.body || "");
  }, [note.body, editing]);

  const isGuidance = ["guidance", "constraint", "preference"].includes(note.kind);
  const canEditSeverity = isGuidance;

  const handleDelete = async () => {
    if (!confirming) { setConfirming(true); return; }
    setDeleting(true);
    try {
      await api.deleteNote(note.id);
      onDeleted?.();
    } catch (e: any) {
      // surface failure in-place — most likely the row is already
      // gone from a concurrent delete elsewhere, in which case the
      // refresh will reconcile.
      setConfirming(false);
    } finally {
      setDeleting(false);
    }
  };

  const handleSaveEdit = async () => {
    if (!draft.trim()) return;
    setSavingEdit(true); setEditErr(null);
    try {
      await api.patchNote(note.id, {
        body: draft.trim(),
        ...(canEditSeverity && editSeverity ? { severity: editSeverity } : {}),
      });
      setEditing(false);
      onEdited?.();
    } catch (e: any) {
      setEditErr(String(e?.message ?? e));
    } finally {
      setSavingEdit(false);
    }
  };

  if (editing) {
    return (
      <div className={`rounded-md border border-accent/60 bg-accent/5 px-2.5 py-2 space-y-1.5`}>
        <div className="text-[10px] font-mono uppercase tracking-wider text-accent">
          editing [{sev}] #{note.id}
        </div>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={Math.max(3, Math.min(10, draft.split("\n").length + 1))}
          className="w-full text-xs font-mono bg-bg border border-line rounded p-1.5 text-ink"
          autoFocus
        />
        {canEditSeverity && (
          <div className="flex items-center gap-1 text-[11px]">
            <span className="text-muted">severity:</span>
            {(["info", "warn", "critical"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setEditSeverity(s)}
                className={`px-1.5 py-0.5 rounded border ${
                  editSeverity === s
                    ? "border-accent text-accent bg-accent/10"
                    : "border-line text-muted hover:bg-sunken"
                }`}
              >{s}</button>
            ))}
          </div>
        )}
        {editErr && (
          <div className="text-[11px] text-bad bg-bad/5 border border-bad/20 rounded px-1.5 py-1">
            {editErr}
          </div>
        )}
        <div className="flex gap-1.5 justify-end">
          <Button variant="primary" size="sm"
                  loading={savingEdit}
                  onClick={handleSaveEdit}
                  disabled={!draft.trim()}>save</Button>
          <Button variant="secondary" size="sm"
                  onClick={() => {
                    setEditing(false);
                    setDraft(note.body || "");
                    setEditErr(null);
                  }}>cancel</Button>
        </div>
      </div>
    );
  }

  return (
    <div className={`rounded-md border bg-elev px-2.5 py-2 transition-colors ${
      isFresh ? "border-accent shadow-soft" : "border-line"
    }`}>
      {/* Header — kind + claim metadata + staleness verdict */}
      <div className="flex items-center justify-between text-[11px] mb-1 gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="font-medium">[{sev}]</span>
          {note.truth_class && note.truth_class !== "INFERENCE" && (
            <span className="text-[9px] font-mono uppercase tracking-wider
                              px-1 rounded border border-line text-muted">
              {note.truth_class}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {linkCount > 0 && (
            <span className="text-[10px] text-muted" title={`${linkCount} attached refs`}>
              📎 {linkCount}
            </span>
          )}
          {onShowCode && cited && (
            <Button variant="subtle" size="xs"
                    onClick={() => onShowCode(cited)}
                    aria-label={`jump to line ${cited}`}>
              code:{cited}
            </Button>
          )}
          <span className={`${stalenessColor} text-[10px] uppercase tracking-wider`}>
            {note.staleness || "—"}
          </span>
          <Button size="xs" variant="ghost"
                  onClick={() => { setEditing(true); setDraft(note.body || ""); }}
                  aria-label="edit this note"
                  title="edit this note">✎</Button>
          {/* Delete — two-stage so an accidental click can't lose the
              note. First click → button flips to red "confirm?"; second
              click within ~5s commits. confirming auto-resets after
              the timeout via a useEffect inside the wrapper. */}
          {confirming ? (
            <Button size="xs" variant="danger"
                    loading={deleting}
                    onClick={handleDelete}
                    title="confirm delete">
              confirm?
            </Button>
          ) : (
            <Button size="xs" variant="ghost"
                    onClick={handleDelete}
                    aria-label="delete this note"
                    title="delete this note">🗑</Button>
          )}
        </div>
      </div>

      {/* Body — markdown links surfaced */}
      <div className="text-xs whitespace-pre-wrap break-words leading-snug">
        {bodyParts.map((p, i) =>
          p.type === "text"
            ? <span key={i}>{p.value}</span>
            : <a key={i}
                 href={resolveRefUrl(p.url)}
                 target="_blank" rel="noopener noreferrer"
                 className="inline-flex items-center gap-0.5 text-accent
                            hover:underline break-all mx-0.5"
                 title={p.url}>
                {isExternalUrl(p.url) ? "🌐" : "📄"} {p.title}
              </a>
        )}
      </div>

      {/* Footer — file (truncated), author, time */}
      <div className="flex items-center gap-2 mt-1.5 text-[10px] font-mono text-muted tabular-nums">
        <span className="truncate flex-1 min-w-0" title={note.target}>
          {note.target}
        </span>
        {note.author && (
          <span title={`authored by ${note.author}`}>{note.author}</span>
        )}
        {note.created_at && (
          <span title={fmtTime(note.created_at)}>
            {fmtRelative(note.created_at)}
          </span>
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

function AddNoteForm({ target, kind, onSaved }: {
  target: string;
  kind: "note" | "guidance";
  onSaved: (newId: number) => void;
}) {
  const [open, setOpen]         = useState(false);
  const [body, setBody]         = useState("");
  const [line, setLine]         = useState<string>("");
  const [severity, setSeverity] = useState<"info" | "warn" | "critical">("info");
  const [saving, setSaving]     = useState(false);
  const [err, setErr]           = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  const save = async () => {
    if (!body.trim()) return;
    setSaving(true); setErr(null);
    try {
      let finalBody = body;
      const ln = line.trim();
      if (ln && /^\d+$/.test(ln)) {
        finalBody = `${target}:${ln} — ${body}`;
      }
      const res = await api.addNote({
        target, body: finalBody, kind,
        ...(kind === "guidance" ? { severity } : {}),
      });
      setBody(""); setLine("");
      setSavedMsg(`saved as #${res.id} — fetching…`);
      onSaved(res.id);
      setTimeout(() => { setOpen(false); setSavedMsg(null); }, 700);
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
      {savedMsg && (
        <div className="text-[11px] text-good bg-good/5 border border-good/20 rounded px-1.5 py-1 flex items-center gap-1">
          ✓ {savedMsg}
        </div>
      )}
      <div className="flex items-center justify-between gap-1.5">
        <div className="text-[10px] font-mono text-muted truncate flex-1 min-w-0"
             title={target}>
          will attach to: <span className="text-ink">{target}</span>
        </div>
        <div className="flex gap-1.5 flex-shrink-0">
          <Button
            variant="primary" size="sm"
            loading={saving}
            onClick={save}
            disabled={!body.trim()}
          >save</Button>
          <Button
            variant="secondary" size="sm"
            onClick={() => { setOpen(false); setBody(""); setLine(""); setErr(null); setSavedMsg(null); }}
          >cancel</Button>
        </div>
      </div>
    </div>
  );
}

// ─── Add-critical inline form ──────────────────────────────────────────────
// Critical notes are the "this rule blocks future edits" tier — they
// need a category, a long-form reason (≥40 chars enforced server-side),
// and a cosigner. The UI here uses the self-cosign shortcut so a solo
// operator can author one in the inspector; multi-person teams will
// still flip to `projmem critical add` and route through review.

function AddCriticalForm({ target, onSaved }: {
  target: string;
  onSaved: () => void;
}) {
  // Categories mirror projmem.critical.CATEGORIES exactly — any
  // mismatch surfaces as a 400 from /critical with a CategoryError
  // envelope, which is exactly what bit the operator last session.
  const CRITICAL_CATEGORIES = [
    "security", "compliance", "performance",
    "business_logic", "data_integrity", "other",
  ] as const;
  type CriticalCategory = typeof CRITICAL_CATEGORIES[number];

  const [open, setOpen]         = useState(false);
  const [reason, setReason]     = useState("");
  const [category, setCategory] = useState<CriticalCategory>("security");
  const [blastHops, setBlastHops] = useState(1);
  const [saving, setSaving]     = useState(false);
  const [err, setErr]           = useState<string | null>(null);

  const tooShort = reason.trim().length < 40;

  const save = async () => {
    if (tooShort) return;
    setSaving(true); setErr(null);
    try {
      await api.addCritical({
        target,
        reason: reason.trim(),
        category,
        self_cosign: true,
        blast_radius_hops: blastHops,
        blocks_edits: true,
      });
      setReason(""); setOpen(false);
      onSaved();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <Button
        variant="danger" size="xs"
        onClick={() => setOpen(true)}
      >+ add critical rule</Button>
    );
  }
  return (
    <div className="rounded-md border border-bad/40 bg-bad/5 p-2 space-y-1.5">
      <div className="text-[10px] font-mono uppercase tracking-wider text-bad">
        ⚠ critical — blocks edits until reviewer approves
      </div>
      <textarea
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        rows={4}
        placeholder="Long-form reason ≥40 chars: the constraint, the incident, the consequence. Example: 'session tokens must never be logged — incident #234 exposed credentials; legal flagged for compliance'."
        className="w-full text-xs font-mono bg-bg border border-line rounded p-1.5 text-ink"
      />
      <div className="flex items-center gap-2 text-[11px] flex-wrap">
        <span className="text-muted">category:</span>
        {CRITICAL_CATEGORIES.map((c) => (
          <button
            key={c}
            onClick={() => setCategory(c)}
            className={`px-1.5 py-0.5 rounded border ${
              category === c
                ? "border-bad text-bad bg-bad/10"
                : "border-line text-muted hover:bg-sunken"
            }`}
          >{c}</button>
        ))}
        <span className="text-muted ml-2">blast hops:</span>
        <input
          value={blastHops}
          onChange={(e) => setBlastHops(
            Math.max(0, Math.min(5, parseInt(e.target.value || "0", 10) || 0)),
          )}
          type="number" min={0} max={5}
          className="w-12 bg-bg border border-line rounded px-1 py-0.5 text-ink font-mono"
        />
      </div>
      {tooShort && reason.length > 0 && (
        <div className="text-[10px] text-warn">
          {40 - reason.trim().length} more characters needed
        </div>
      )}
      {err && (
        <div className="text-[11px] text-bad bg-bad/5 border border-bad/20 rounded px-1.5 py-1">
          {err}
        </div>
      )}
      <div className="flex items-center justify-between gap-1.5">
        <div className="text-[10px] font-mono text-muted truncate flex-1 min-w-0" title={target}>
          locks: <span className="text-ink">{target}</span> · self-cosigned
        </div>
        <div className="flex gap-1.5 flex-shrink-0">
          <Button variant="danger" size="sm"
                  loading={saving}
                  onClick={save}
                  disabled={tooShort}>add critical</Button>
          <Button variant="secondary" size="sm"
                  onClick={() => { setOpen(false); setReason(""); setErr(null); }}>
            cancel
          </Button>
        </div>
      </div>
    </div>
  );
}

// ─── Exclude-subtree toggle ────────────────────────────────────────────────
// Tags a directory (or @project) with kind=exclude so the agent's
// next `projmem editing` on any path inside surfaces the brief
// 🚫 OUT OF SCOPE warning. The toggle reads from + writes to the
// daemon's exclusions store, and the store's `exclusions` slice
// also drives the tree/schema visual markers.

function ExcludeToggle({ target, onChange }: {
  target: string;
  onChange?: () => void;
}) {
  const exclusions = useStore((s) => s.exclusions);
  const setExclusions = useStore((s) => s.setExclusions);
  const setSelectedDirectory = useStore((s) => s.setSelectedDirectory);
  const existing = exclusions.find((e) => e.target === target);
  // Surface every exclusion stored UNDER the current target. Helps the
  // operator see "this directory itself isn't excluded, but `util/`
  // below it is" — without that hint, exclusions look invisible from
  // any ancestor scope and the operator thinks they didn't apply.
  const childExclusions = exclusions.filter((e) => (
    e.target !== target &&
    target !== "@project" &&
    e.target.startsWith(target)
  )).concat(
    target === "@project"
      ? exclusions.filter((e) => e.target !== "@project")
      : [],
  );
  const [open, setOpen]   = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy]   = useState(false);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);

  // Auto-revert "are you sure" prompt after a few seconds so a
  // distracted operator doesn't accidentally confirm on next click.
  useEffect(() => {
    if (!confirmRemove) return;
    const t = setTimeout(() => setConfirmRemove(false), 5000);
    return () => clearTimeout(t);
  }, [confirmRemove]);

  const refresh = async () => {
    try {
      const r = await api.exclusions();
      setExclusions(r.exclusions.map((e) => ({ id: e.id, target: e.target, body: e.body })));
    } catch { /* ignore */ }
    onChange?.();
  };

  const add = async () => {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      await api.addNote({ target, body: reason.trim(), kind: "exclude" });
      setReason(""); setOpen(false);
      await refresh();
    } catch { /* ignore */ } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    setRemovingId(id);
    try {
      await api.deleteNote(id);
      await refresh();
    } catch { /* ignore — refresh will reconcile */ }
    finally {
      setRemovingId(null);
      setConfirmRemove(false);
    }
  };

  if (existing) {
    return (
      <div className="mt-1.5 rounded-md border border-bad/30 bg-bad/5 px-2 py-1.5 text-[11px]">
        <div className="flex items-center justify-between gap-2 mb-0.5">
          <span className="text-bad font-medium">🚫 Excluded from agent scope</span>
          {confirmRemove ? (
            <Button size="xs" variant="danger"
                    loading={removingId === existing.id}
                    onClick={() => remove(existing.id)}
                    title="confirm — remove this exclusion">
              confirm remove?
            </Button>
          ) : (
            <Button size="xs" variant="ghost"
                    onClick={() => setConfirmRemove(true)}
                    aria-label="remove exclusion"
                    title="remove this exclusion (subtree becomes in-scope again)">
              🗑 remove
            </Button>
          )}
        </div>
        <div className="text-ink leading-snug mb-1">{existing.body}</div>
        <div className="text-[10px] text-muted">
          Agent calls to <span className="font-mono">projmem editing</span> on files in
          this subtree surface an OUT OF SCOPE warning in their context.
        </div>
      </div>
    );
  }

  if (!open) {
    return (
      <div className="space-y-1.5">
        <Button
          size="xs" variant="secondary"
          onClick={() => setOpen(true)}
          title="mark this subtree as out-of-scope for the AI agent"
        >🚫 exclude this subtree from agent scope</Button>
        {childExclusions.length > 0 && (
          <div className="rounded-md border border-bad/20 bg-bad/5 px-2 py-1.5 text-[10px]">
            <div className="text-bad font-medium mb-1">
              {childExclusions.length} exclusion{childExclusions.length > 1 ? "s" : ""} active inside this scope
            </div>
            <div className="space-y-0.5 max-h-24 overflow-y-auto">
              {childExclusions.slice(0, 8).map((ex) => (
                <div key={ex.id} className="flex items-center gap-1.5">
                  <button
                    onClick={() => setSelectedDirectory(ex.target)}
                    className="flex-1 min-w-0 text-left font-mono text-bad
                                hover:underline truncate"
                    title={`open ${ex.target}`}
                  >🚫 {ex.target}</button>
                  <Button size="xs" variant="ghost"
                          loading={removingId === ex.id}
                          onClick={() => remove(ex.id)}
                          aria-label={`remove exclusion at ${ex.target}`}
                          title="remove this exclusion">🗑</Button>
                </div>
              ))}
              {childExclusions.length > 8 && (
                <div className="text-muted">+ {childExclusions.length - 8} more</div>
              )}
            </div>
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="mt-1.5 rounded-md border border-bad/30 bg-bad/5 p-1.5 space-y-1.5 text-[11px]">
      <div className="text-bad font-medium text-[10px] uppercase tracking-wider">
        🚫 mark out of scope
      </div>
      <textarea
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        rows={2}
        placeholder="Why this is out of scope. e.g. 'Linux-only — operator is on macOS', 'vendored mem0 — re-cloneable, don't read', 'auto-generated, deprecated' …"
        className="w-full text-xs font-mono bg-bg border border-line rounded p-1.5 text-ink"
      />
      <div className="flex items-center justify-between gap-1.5">
        <div className="text-[10px] text-muted">
          Saves agent tokens on irrelevant code.
        </div>
        <div className="flex gap-1">
          <Button size="sm" variant="danger" loading={busy}
                  onClick={add} disabled={!reason.trim()}>
            exclude
          </Button>
          <Button size="sm" variant="secondary"
                  onClick={() => { setOpen(false); setReason(""); }}>
            cancel
          </Button>
        </div>
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

// Per-line inline form opened from a gutter-line click. Compact
// variant of AddNoteForm + AddCriticalForm: hard-codes self-cosign
// for critical to keep the inline experience snappy. The note body
// is prefixed with `target:line — body` so the existing staleness
// verifier and the "code:N" jump button keep working out of the box.

const CRITICAL_CATEGORIES_INLINE = [
  "security", "compliance", "performance",
  "business_logic", "data_integrity", "other",
] as const;

function LineAnnotationForm({
  target, line, kind, onSaved, onCancel,
}: {
  target: string;
  line: number;
  kind: "note" | "guidance" | "critical";
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [body, setBody]         = useState("");
  const [severity, setSeverity] = useState<"info" | "warn" | "critical">("info");
  const [category, setCategory] = useState<typeof CRITICAL_CATEGORIES_INLINE[number]>("security");
  const [saving, setSaving]     = useState(false);
  const [err, setErr]           = useState<string | null>(null);
  const bumpDataVersion = useStore((s) => s.bumpDataVersion);

  const minReason = kind === "critical" ? 40 : 1;
  const tooShort = body.trim().length < minReason;

  const save = async () => {
    if (tooShort) return;
    setSaving(true); setErr(null);
    try {
      const cited = `${target}:${line} — ${body.trim()}`;
      if (kind === "critical") {
        await api.addCritical({
          target,
          reason: cited,
          category,
          self_cosign: true,
          blast_radius_hops: 1,
          blocks_edits: true,
        });
      } else {
        await api.addNote({
          target, body: cited,
          kind,
          ...(kind === "guidance" ? { severity } : {}),
        });
      }
      bumpDataVersion();
      onSaved();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setSaving(false);
    }
  };

  const borderTone = kind === "critical" ? "border-bad/40 bg-bad/5"
                   : kind === "guidance" ? "border-accent/40 bg-accent/5"
                                         : "border-warn/40 bg-warn/5";

  return (
    <div className={`rounded-md border ${borderTone} p-2 mt-1 mb-1 space-y-1.5`}>
      <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-wider">
        <span className={kind === "critical" ? "text-bad" :
                          kind === "guidance" ? "text-accent" : "text-warn"}>
          + {kind} @ line {line}
        </span>
        <span className="ml-auto text-muted normal-case font-sans">
          target: <span className="text-ink">{target}:{line}</span>
        </span>
      </div>
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        rows={kind === "critical" ? 4 : 3}
        autoFocus
        placeholder={
          kind === "critical"
            ? "Long-form reason ≥40 chars: the constraint, the incident, the consequence."
            : kind === "guidance"
              ? "Guidance for the agent about this line — convention, contract, gotcha."
              : "Free-text note about this line."
        }
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
      {kind === "critical" && (
        <div className="flex items-center gap-1 text-[11px] flex-wrap">
          <span className="text-muted">category:</span>
          {CRITICAL_CATEGORIES_INLINE.map((c) => (
            <button
              key={c}
              onClick={() => setCategory(c)}
              className={`px-1.5 py-0.5 rounded border ${
                category === c
                  ? "border-bad text-bad bg-bad/10"
                  : "border-line text-muted hover:bg-sunken"
              }`}
            >{c}</button>
          ))}
        </div>
      )}
      {tooShort && body.length > 0 && (
        <div className="text-[10px] text-warn">
          {minReason - body.trim().length} more characters needed
        </div>
      )}
      {err && (
        <div className="text-[11px] text-bad bg-bad/5 border border-bad/20 rounded px-1.5 py-1">
          {err}
        </div>
      )}
      <div className="flex justify-end gap-1.5">
        <Button
          variant={kind === "critical" ? "danger" : "primary"}
          size="sm" loading={saving}
          onClick={save} disabled={tooShort}
        >save</Button>
        <Button variant="secondary" size="sm" onClick={onCancel}>cancel</Button>
      </div>
    </div>
  );
}

// Tiny dropdown over a gutter line number — the choose-kind step.
function LineKindMenu({ onPick, onClose }: {
  onPick: (k: "note" | "guidance" | "critical") => void;
  onClose: () => void;
}) {
  return (
    <div
      className="absolute left-10 z-10 bg-elev border border-line rounded-md
                 shadow-soft text-[11px] min-w-[140px]"
      onClick={(e) => e.stopPropagation()}
    >
      <button
        onClick={() => onPick("note")}
        className="block w-full text-left px-2.5 py-1.5 hover:bg-sunken border-b border-line-soft"
      >📝 add note</button>
      <button
        onClick={() => onPick("guidance")}
        className="block w-full text-left px-2.5 py-1.5 hover:bg-sunken border-b border-line-soft text-accent"
      >🧭 add guidance</button>
      <button
        onClick={() => onPick("critical")}
        className="block w-full text-left px-2.5 py-1.5 hover:bg-sunken text-bad"
      >⚠ add critical</button>
      <button
        onClick={onClose}
        className="block w-full text-left px-2.5 py-1 hover:bg-sunken text-muted border-t border-line-soft"
      >cancel</button>
    </div>
  );
}

function CodeTab({ target, scrollToLine, notes, critical }: {
  target: string;
  scrollToLine?: number;
  notes: Annotation[];           // every annotation attached to this lifeline
  critical: Annotation[];        // critical rows live on a separate column in the daemon response
}) {
  const [content, setContent] = useState<{text?: string; binary?: boolean; truncated?: boolean; size?: number} | null>(null);
  const [err, setErr]         = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Per-line menu / form coordination. Only one row's UI is "open"
  // at a time; cancelling collapses everything back to the plain code.
  const [openMenuLine, setOpenMenuLine] = useState<number | null>(null);
  const [activeForm, setActiveForm]     = useState<{
    line: number; kind: "note" | "guidance" | "critical";
  } | null>(null);
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

  // Build a line → annotations map by parsing the canonical citation
  // pattern (`target:line` or `:line`). Used to render gutter dots so
  // the operator can see at a glance which lines already have notes.
  const lineAnnotations = (() => {
    const m = new Map<number, Annotation[]>();
    const all = [...notes, ...critical];
    for (const n of all) {
      // First try the precise citation (target:line) — that's what
      // the inline form writes. Fall back to any `:N` in the body.
      const re = new RegExp(
        `(?:${target.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&")}|):(\\d+)\\b`,
      );
      const match = (n.body || "").match(re);
      if (!match) continue;
      const ln = parseInt(match[1], 10);
      if (!ln) continue;
      const arr = m.get(ln) ?? [];
      arr.push(n);
      m.set(ln, arr);
    }
    return m;
  })();

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
  const lines = highlighted.split("\n");
  // Worst-case annotation-tone derivation for a line's gutter dot:
  // critical > guidance > plain note.
  const dotTone = (lineNo: number): string | null => {
    const ann = lineAnnotations.get(lineNo);
    if (!ann || ann.length === 0) return null;
    if (ann.some((a) => a.kind === "critical")) return "bg-bad";
    if (ann.some((a) => ["guidance", "constraint", "preference"].includes(a.kind))) return "bg-accent";
    return "bg-warn";
  };
  return (
    <div className="rounded-md border border-line bg-code-bg overflow-hidden">
      {content.truncated && (
        <div className="text-[10px] text-warn px-2 py-1 border-b border-line">
          truncated to 200 KB ({content.size} bytes total)
        </div>
      )}
      <div className="flex max-h-[60vh] overflow-auto text-[11px] font-mono leading-[1.45] relative">
        <div className="select-none text-right text-muted py-2 tabular-nums border-r border-line-soft bg-code-bg">
          {lines.map((_, i) => {
            const lineNo = i + 1;
            const tone = dotTone(lineNo);
            const annCount = lineAnnotations.get(lineNo)?.length ?? 0;
            const isOpen = openMenuLine === lineNo;
            return (
              <div key={lineNo} id={`code-line-${lineNo}`} className="relative pr-2 pl-2 group">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setOpenMenuLine(isOpen ? null : lineNo);
                    setActiveForm(null);
                  }}
                  className={`inline-flex items-center justify-end gap-1
                              hover:text-accent cursor-pointer
                              ${isOpen ? "text-accent font-semibold" : ""}`}
                  title={annCount > 0
                    ? `${annCount} annotation${annCount > 1 ? "s" : ""} on this line — click to add another`
                    : "click to add note/guidance/critical at this line"}
                >
                  {tone && (
                    <span className={`inline-block w-1.5 h-1.5 rounded-full ${tone}`} />
                  )}
                  <span>{lineNo}</span>
                </button>
                {isOpen && (
                  <LineKindMenu
                    onPick={(k) => {
                      setActiveForm({ line: lineNo, kind: k });
                      setOpenMenuLine(null);
                    }}
                    onClose={() => setOpenMenuLine(null)}
                  />
                )}
              </div>
            );
          })}
        </div>
        <pre className="px-3 py-2 flex-1 whitespace-pre overflow-x-auto"
             onClick={() => { setOpenMenuLine(null); }}>
          <code className="hljs"
                dangerouslySetInnerHTML={{ __html: highlighted }} />
        </pre>
      </div>
      {activeForm && (
        <div className="border-t border-line bg-bg p-2">
          <LineAnnotationForm
            target={target}
            line={activeForm.line}
            kind={activeForm.kind}
            onSaved={() => setActiveForm(null)}
            onCancel={() => setActiveForm(null)}
          />
        </div>
      )}
    </div>
  );
}

// ─── Inspector main ────────────────────────────────────────────────────────

type Tab = "notes" | "guidance" | "critical" | "history" | "code";

export function Inspector() {
  const selectedEvent      = useStore((s) => s.selectedEvent);
  const openLeases         = useStore((s) => s.openLeases);
  const selectedLifeline   = useStore((s) => s.selectedLifeline);
  const selectedDirectory  = useStore((s) => s.selectedDirectory);
  const liveLeasedPaths    = useStore((s) => s.liveLeasedPaths);
  const dataVersion        = useStore((s) => s.dataVersion);
  const bumpDataVersion    = useStore((s) => s.bumpDataVersion);

  const [tab, setTab]               = useState<Tab>("notes");
  const [detail, setDetail]         = useState<LifelineDetail | null>(null);
  const [dirDetail, setDirDetail]   = useState<{
    notes: Annotation[];
    critical: Annotation[];
    scope_files: { id: string; current_path: string }[];
    scope_count: number;
  } | null>(null);
  const [showScope, setShowScope]   = useState(false);
  const [loading, setLoading]       = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const [codeLine, setCodeLine]     = useState<number | undefined>(undefined);
  const [freshNoteId, setFreshNoteId] = useState<number | null>(null);

  const setCenterView        = useStore((s) => s.setCenterView);
  const setSelectedLifeline  = useStore((s) => s.setSelectedLifeline);
  const setSelectedDirectory = useStore((s) => s.setSelectedDirectory);

  useEffect(() => {
    if (!selectedLifeline) { setDetail(null); return; }
    let cancelled = false;
    setLoading(true);
    api.lifeline(selectedLifeline)
      .then((d) => !cancelled && setDetail(d))
      .catch(()  => !cancelled && setDetail(null))
      .finally(()=> !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [selectedLifeline, refreshTick, dataVersion]);

  // Directory mode — pulls annotations for the dir-scoped target
  // (trailing-slash convention) or `@project`. No lifeline lookup
  // because directories aren't files; they're just shared scopes.
  useEffect(() => {
    if (!selectedDirectory) { setDirDetail(null); return; }
    let cancelled = false;
    setLoading(true);
    api.notesByTarget(selectedDirectory)
      .then((d) => !cancelled && setDirDetail({
        notes: d.notes as Annotation[],
        critical: d.critical as Annotation[],
        scope_files: d.scope_files,
        scope_count: d.scope_count,
      }))
      .catch(()  => !cancelled && setDirDetail(null))
      .finally(()=> !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [selectedDirectory, refreshTick, dataVersion]);

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
      <div className="border-b border-line px-3 py-2 flex items-center justify-between gap-2">
        <span className="text-xs font-semibold tracking-tight">Inspector</span>
        <Button
          size="xs" variant="ghost"
          onClick={() => {
            // Refresh both: bumpDataVersion re-runs the lifeline + dir
            // fetches, and refreshTick is the legacy hook some local
            // forms still poke. Cheap to do both.
            bumpDataVersion();
            setRefreshTick((t) => t + 1);
          }}
          title="re-fetch notes, guidance, critical from the daemon (also runs automatically on note add / delete / edit broadcasts)"
          aria-label="sync notes from daemon"
        >↻ sync</Button>
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
            <div className="flex items-center justify-between mb-1">
              <div className="text-[11px] uppercase tracking-wider text-muted">
                Selected file
              </div>
              <Button
                size="xs" variant="ghost"
                onClick={() => setSelectedLifeline(null)}
                title="clear selection (Esc)"
                aria-label="clear selection"
              >✕</Button>
            </div>
            {loading && <div className="text-xs text-muted">loading…</div>}
            {detail && (
              <>
                {/* Headline path card — copyable + jump-to-graph + jump-to-tree */}
                <div className="rounded-md border border-line bg-elev px-2.5 py-2 mb-2">
                  <div className="text-xs font-mono break-all leading-snug text-ink mb-1.5">
                    {detail.lifeline.current_path ?? (
                      <span className="text-muted italic">(tombstoned — no current path)</span>
                    )}
                  </div>

                  {/* Scope hopper — every ancestor directory of this
                      file + @project, as one-click chips. Lets the
                      operator add notes at any scope (this file → its
                      parent dir → its grandparent → the whole project)
                      without leaving the inspector. */}
                  {detail.lifeline.current_path && (
                    <div className="mb-1.5">
                      <div className="text-[10px] text-muted mb-0.5">
                        add note at scope:
                      </div>
                      <div className="flex items-center gap-1 flex-wrap">
                        {(() => {
                          const parts = detail.lifeline.current_path!.split("/").filter(Boolean);
                          const chips: { label: string; target: string }[] = [];
                          let accum = "";
                          for (let i = 0; i < parts.length - 1; i++) {
                            accum = accum ? accum + "/" + parts[i] : parts[i];
                            chips.push({ label: parts[i] + "/", target: accum + "/" });
                          }
                          chips.push({ label: "@project", target: "@project" });
                          return chips.map((c) => (
                            <Button
                              key={c.target}
                              size="xs" variant="secondary"
                              onClick={() => setSelectedDirectory(c.target)}
                              title={`switch inspector to ${c.target} — applies to every file under that scope`}
                            >{c.label}</Button>
                          ));
                        })()}
                      </div>
                    </div>
                  )}

                  <div className="flex items-center gap-1.5 flex-wrap">
                    {detail.lifeline.current_path && (
                      <Button
                        size="xs" variant="secondary"
                        onClick={() => {
                          navigator.clipboard
                            ?.writeText(detail.lifeline.current_path!)
                            .catch(() => { /* ignore */ });
                        }}
                        aria-label="copy path"
                      >📋 copy</Button>
                    )}
                    <Button
                      size="xs" variant="secondary"
                      onClick={() => setCenterView("graph")}
                    >→ graph</Button>
                    <Button
                      size="xs" variant="secondary"
                      onClick={() => setCenterView("schema")}
                    >→ schema</Button>
                    <Button
                      size="xs" variant="secondary"
                      onClick={() => setCenterView("tree")}
                    >→ tree</Button>
                    <span className="ml-auto text-[10px] font-mono text-muted">
                      lifeline {detail.lifeline.id.slice(0, 8)}…
                    </span>
                  </div>
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
                      onSaved={(id) => {
                        setFreshNoteId(id);
                        setRefreshTick((n) => n + 1);
                        setTimeout(() => setFreshNoteId(null), 2000);
                      }}
                    />
                    {plainNotes.length === 0 && <div className="text-xs text-muted">(no notes — use the form above)</div>}
                    {plainNotes.map((n) => (
                      <NoteRow key={n.id} note={n}
                               onShowCode={jumpToCode}
                               isFresh={n.id === freshNoteId}
                               onDeleted={() => setRefreshTick((t) => t + 1)}
                               onEdited={() => setRefreshTick((t) => t + 1)} />
                    ))}
                  </div>
                )}
                {tab === "guidance" && selectedPath && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedPath} kind="guidance"
                      onSaved={(id) => {
                        setFreshNoteId(id);
                        setRefreshTick((n) => n + 1);
                        setTimeout(() => setFreshNoteId(null), 2000);
                      }}
                    />
                    {guidance.length === 0 && <div className="text-xs text-muted">(no guidance — use the form above)</div>}
                    {guidance.map((n) => (
                      <NoteRow key={n.id} note={n}
                               onShowCode={jumpToCode}
                               isFresh={n.id === freshNoteId}
                               onDeleted={() => setRefreshTick((t) => t + 1)}
                               onEdited={() => setRefreshTick((t) => t + 1)} />
                    ))}
                  </div>
                )}
                {tab === "critical" && selectedPath && (
                  <div className="space-y-1.5">
                    <AddCriticalForm
                      target={selectedPath}
                      onSaved={() => {
                        setRefreshTick((n) => n + 1);
                      }}
                    />
                    {detail.critical.length === 0 && <div className="text-xs text-muted">(no critical notes yet)</div>}
                    {detail.critical.map((n) => (
                      <NoteRow key={n.id} note={n}
                               onShowCode={jumpToCode}
                               isFresh={n.id === freshNoteId}
                               onDeleted={() => setRefreshTick((t) => t + 1)}
                               onEdited={() => setRefreshTick((t) => t + 1)} />
                    ))}
                  </div>
                )}
                {tab === "history" && (
                  <HistoryTimeline events={detail.events} />
                )}
                {tab === "code" && selectedPath && (
                  <CodeTab
                    target={selectedPath}
                    scrollToLine={codeLine}
                    notes={detail.notes}
                    critical={detail.critical}
                  />
                )}
                {tab === "code" && !selectedPath && (
                  <div className="text-xs text-muted">(tombstoned — no current file)</div>
                )}
              </>
            )}
          </section>
        )}

        {selectedDirectory && !selectedLifeline && (
          <section>
            <div className="flex items-center justify-between mb-1">
              <div className="text-[11px] uppercase tracking-wider text-muted">
                Selected directory
              </div>
              <Button
                size="xs" variant="ghost"
                onClick={() => setSelectedDirectory(null)}
                title="clear (Esc)"
                aria-label="clear directory selection"
              >✕</Button>
            </div>
            <div className="rounded-md border border-line bg-elev px-2.5 py-2 mb-2">
              <div className="text-xs font-mono break-all leading-snug text-ink mb-1">
                {selectedDirectory === "@project" ? "🏠 " : "📁 "}{selectedDirectory}
              </div>
              <div className="text-[10px] text-muted mb-1.5">
                Notes added here apply to <span className="text-ink font-semibold">
                {selectedDirectory === "@project"
                  ? "the entire project — every file the agent edits"
                  : "every file in this subtree, recursively"}
                </span>.
                {" "}Claude reads them when it runs `projmem editing` on any matching file.
              </div>

              {/* Exclude-this-subtree action. Adds a kind=exclude
                  annotation that the editing-lease response surfaces
                  as 🚫 OUT OF SCOPE. Saves tokens on subtrees that
                  shouldn't be read at all (vendored copies, Linux-
                  only, generated, deprecated). */}
              <ExcludeToggle target={selectedDirectory!}
                              onChange={() => setRefreshTick((n) => n + 1)} />

              {/* Scope chip + expandable file list */}
              {dirDetail && dirDetail.scope_count > 0 && (
                <div>
                  <button
                    onClick={() => setShowScope((s) => !s)}
                    className="text-[10px] text-accent hover:underline inline-flex items-center gap-1"
                    title="show the files this note would apply to"
                  >
                    {showScope ? "▼" : "▶"} applies to {dirDetail.scope_count} file{dirDetail.scope_count !== 1 ? "s" : ""}
                  </button>
                  {showScope && (
                    <div className="mt-1.5 max-h-32 overflow-y-auto rounded
                                    border border-line bg-bg text-[10px]
                                    font-mono">
                      {dirDetail.scope_files.slice(0, 100).map((f) => (
                        <button
                          key={f.id}
                          onClick={() => setSelectedLifeline(f.id)}
                          className="block w-full text-left px-1.5 py-0.5
                                      hover:bg-sunken text-ink truncate"
                          title={f.current_path}
                        >{f.current_path}</button>
                      ))}
                      {dirDetail.scope_files.length > 100 && (
                        <div className="px-1.5 py-0.5 text-muted">
                          + {dirDetail.scope_files.length - 100} more
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
            {loading && <div className="text-xs text-muted">loading…</div>}
            {dirDetail && (
              <>
                <div className="flex gap-1 text-[11px] mb-2 flex-wrap">
                  {(["notes", "guidance", "critical"] as Tab[]).map((t) => (
                    <Button
                      key={t}
                      size="xs"
                      variant={tab === t ? "primary" : "secondary"}
                      onClick={() => setTab(t)}
                    >{t}</Button>
                  ))}
                </div>
                {tab === "notes" && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedDirectory} kind="note"
                      onSaved={(id) => {
                        setFreshNoteId(id);
                        setRefreshTick((n) => n + 1);
                        setTimeout(() => setFreshNoteId(null), 2000);
                      }}
                    />
                    {dirDetail.notes.filter((n) =>
                      !["guidance","constraint","preference","critical"].includes(n.kind)
                    ).length === 0 && (
                      <div className="text-xs text-muted">
                        (no notes — explain why this directory exists)
                      </div>
                    )}
                    {dirDetail.notes
                      .filter((n) =>
                        !["guidance","constraint","preference","critical"].includes(n.kind))
                      .map((n) => (
                        <NoteRow key={n.id} note={n}
                                 isFresh={n.id === freshNoteId}
                                 onDeleted={() => setRefreshTick((t) => t + 1)}
                                 onEdited={() => setRefreshTick((t) => t + 1)} />
                      ))}
                  </div>
                )}
                {tab === "guidance" && (
                  <div className="space-y-1.5">
                    <AddNoteForm
                      target={selectedDirectory} kind="guidance"
                      onSaved={(id) => {
                        setFreshNoteId(id);
                        setRefreshTick((n) => n + 1);
                        setTimeout(() => setFreshNoteId(null), 2000);
                      }}
                    />
                    {dirDetail.notes.filter((n) =>
                      ["guidance","constraint","preference"].includes(n.kind)
                    ).length === 0 && (
                      <div className="text-xs text-muted">
                        (no guidance — convention rules, style preferences, …)
                      </div>
                    )}
                    {dirDetail.notes
                      .filter((n) => ["guidance","constraint","preference"].includes(n.kind))
                      .map((n) => (
                        <NoteRow key={n.id} note={n}
                                 isFresh={n.id === freshNoteId}
                                 onDeleted={() => setRefreshTick((t) => t + 1)}
                                 onEdited={() => setRefreshTick((t) => t + 1)} />
                      ))}
                  </div>
                )}
                {tab === "critical" && (
                  <div className="space-y-1.5">
                    <AddCriticalForm
                      target={selectedDirectory}
                      onSaved={() => setRefreshTick((n) => n + 1)}
                    />
                    {dirDetail.critical.length === 0 && (
                      <div className="text-xs text-muted">
                        (no critical notes yet — these block edits until a reviewer approves)
                      </div>
                    )}
                    {dirDetail.critical.map((n) => (
                      <NoteRow key={n.id} note={n}
                               isFresh={n.id === freshNoteId}
                               onDeleted={() => setRefreshTick((t) => t + 1)}
                               onEdited={() => setRefreshTick((t) => t + 1)} />
                    ))}
                  </div>
                )}
              </>
            )}
          </section>
        )}

        {selectedEvent && !selectedLifeline && !selectedDirectory && (
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
