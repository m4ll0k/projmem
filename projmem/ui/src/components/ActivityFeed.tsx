import { useEffect, useRef } from "react";
import { useStore } from "../store";
import type { DaemonEvent } from "../types";

function styleForKind(kind: string): { glyph: string; color: string } {
  switch (kind) {
    case "leased":         return { glyph: "✎", color: "text-accent" };
    case "released":       return { glyph: "✓", color: "text-good" };
    case "abandoned":      return { glyph: "✗", color: "text-warn" };
    case "created":        return { glyph: "+", color: "text-good" };
    case "edited":         return { glyph: "✎", color: "text-accent" };
    case "moved":          return { glyph: "→", color: "text-accent" };
    case "deleted":        return { glyph: "−", color: "text-bad" };
    case "note_added":     return { glyph: "📝", color: "text-muted" };
    case "critical_added": return { glyph: "⚠", color: "text-bad" };
    case "paused":         return { glyph: "⏸", color: "text-warn" };
    case "resumed":        return { glyph: "▶", color: "text-good" };
    case "lease_approved": return { glyph: "✓", color: "text-good" };
    case "lease_denied":   return { glyph: "⊘", color: "text-bad" };
    default:               return { glyph: "·", color: "text-muted" };
  }
}

function isImplicit(ev: DaemonEvent): boolean {
  const intent = (ev as { intent?: string; reason?: string }).intent
              ?? (ev as { reason?: string }).reason;
  return typeof intent === "string" && intent.startsWith("implicit");
}

function shortTime(t?: number): string {
  if (!t) return "";
  return new Date(t * 1000).toLocaleTimeString(undefined, { hour12: false });
}

function shortPath(ev: DaemonEvent): string | null {
  const target = (ev as { target?: string }).target;
  const path   = (ev as { current_path?: string; path?: string }).current_path
              ?? (ev as { path?: string }).path
              ?? target;
  return path ? String(path) : null;
}

export function ActivityFeed() {
  const events      = useStore((s) => s.events);
  const selected    = useStore((s) => s.selectedEvent);
  const setSelected = useStore((s) => s.setSelectedEvent);
  const bottomRef   = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-muted">
        waiting for events…
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <ul className="divide-y divide-line-soft">
        {events.map((ev, i) => {
          const { glyph, color } = styleForKind(ev.kind);
          const implicit = isImplicit(ev);
          const path = shortPath(ev);
          const isSelected = selected === ev;
          return (
            <li
              key={i}
              onClick={() => setSelected(ev)}
              className={`flex cursor-pointer items-start gap-2 px-3 py-1.5 text-xs
                hover:bg-sunken transition-colors
                ${isSelected ? "bg-accent/10" : ""}
                ${implicit ? "border-l-2 border-dashed border-warn pl-2" : ""}`}
              title={implicit ? "agent edited without announcing" : undefined}
            >
              <span className="font-mono text-muted w-14 tabular-nums">{shortTime(ev.at)}</span>
              <span className={`${color} w-5 text-center`}>{glyph}</span>
              <span className="flex-1 break-words">
                <span className="font-medium">{ev.kind}</span>
                {path && (
                  <span className="ml-2 font-mono text-muted">{path}</span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      <div ref={bottomRef} />
    </div>
  );
}
