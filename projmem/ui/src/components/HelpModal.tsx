import { useEffect } from "react";
import { Button } from "./Button";

// ─── Help modal ────────────────────────────────────────────────────────────
// A small popup that explains the two halves of projmem to whoever
// just opened the UI: what they're looking at (graph/tree/schema/
// inspector/activity feed), and how to tell an AI agent to use
// projmem during a coding session. Kept short and scannable — anyone
// reading more than a few paragraphs has already moved past the
// "first run" moment.

export function HelpModal({ open, onClose }: {
  open: boolean;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center
                 bg-black/40 backdrop-blur-sm p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="help-title"
    >
      <div
        className="bg-elev border border-line rounded-lg shadow-soft
                   max-w-2xl w-full max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-4 py-3 flex items-center justify-between">
          <h2 id="help-title" className="text-base font-semibold tracking-tight">
            How projmem works
          </h2>
          <Button size="sm" variant="ghost" onClick={onClose} aria-label="close help">
            ✕
          </Button>
        </div>

        <div className="px-4 py-3 space-y-5 text-sm leading-relaxed">

          <Section title="What you're looking at">
            <p>
              projmem is a <b>drift-aware memory</b> for AI coding agents.
              The daemon watches your repo; this UI is the human view onto
              the index it builds.
            </p>
            <ul className="list-disc list-inside space-y-1 mt-1.5">
              <li><b>Activity feed</b> (left) — every file event in real time.
                Click a path to hop into it.</li>
              <li><b>Tree · Graph · Schema</b> (center) — three lenses on the
                same index. Click a node and the others sync.</li>
              <li><b>Inspector</b> (right) — notes, guidance, critical rules,
                edit history, source code with line annotations.</li>
            </ul>
          </Section>

          <Section title="Adding context for the agent">
            <p>
              Select a file or directory in any view, then in the inspector:
            </p>
            <ul className="list-disc list-inside space-y-1 mt-1.5">
              <li>
                <b>Notes</b> — free-text observations. Useful for "this is fragile",
                "see ticket X".
              </li>
              <li>
                <b>Guidance</b> — rules the agent should follow when editing.
                Pick severity <span className="text-muted">(info · warn · critical)</span>.
              </li>
              <li>
                <b>Critical</b> — load-bearing rules. ≥40 chars, category, blast
                radius. The agent's edit lease will <i>pend approval</i> until
                you clear it.
              </li>
              <li>
                <b>Exclude</b> on a directory — agent gets an <span className="text-bad">🚫
                OUT OF SCOPE</span> warning when it tries to read files inside.
                Saves tokens on vendored/legacy/generated subtrees.
              </li>
              <li>
                <b>Refs</b> — drop PDFs, design docs, advisories in
                <span className="font-mono"> .projmem/refs/</span>; attach them
                to any note via <span className="font-mono">📎 attach</span>.
              </li>
              <li>
                <b>Line annotations</b> — in the <b>code</b> tab, click any
                line number to add note / guidance / critical scoped to that
                exact line. The dot in the gutter marks lines that already
                have notes.
              </li>
            </ul>
          </Section>

          <Section title="What the agent sees (Claude / Codex / Gemini …)">
            <p>
              Tell your agent to call <span className="font-mono">projmem</span> before
              editing. The two key commands:
            </p>
            <pre className="bg-code-bg border border-line rounded p-2 text-[11px]
                            font-mono leading-relaxed overflow-x-auto mt-1.5">
{`# Announce intent + receive context (notes, guidance, critical, history, exclusions).
projmem editing path/to/file.py --reason "fix race in poller"

# Read-only briefing without opening a lease.
projmem context path/to/file.py

# Pin a fact during the session.
projmem note add path/to/file.py "imports run() from helpers.py"

# Close the lease when done.
projmem done <lease-id>`}
            </pre>
            <p className="text-xs text-muted">
              The <span className="font-mono">editing</span> response surfaces the
              highest-leverage context first: line-scoped notes for the file,
              then file notes, then directory notes, then project-wide notes.
              An exclusion or critical rule shows up in <span className="font-mono">warnings[]</span> so
              the agent can't miss it.
            </p>
          </Section>

          <Section title="Live controls">
            <ul className="list-disc list-inside space-y-1">
              <li><b>⏸ Pause agent</b> in the top bar — blocks new edit leases
                until you resume.</li>
              <li><b>Pending approval</b> badge on a lease card — the agent hit a
                critical rule and is waiting. Click <span className="font-mono">approve</span> or
                <span className="font-mono"> deny</span>.</li>
              <li><b>↻ sync</b> in the inspector header — refetches notes if you
                edited from the CLI in another terminal.</li>
              <li><b>Esc</b> — clear current selection.</li>
            </ul>
          </Section>

          <Section title="Quick start in one line">
            <pre className="bg-code-bg border border-line rounded p-2 text-[11px]
                            font-mono overflow-x-auto">
{`projmem init && projmem index && projmem ui`}
            </pre>
            <p className="text-xs text-muted">
              Then tell your agent: "before editing any file, run
              <span className="font-mono"> projmem editing &lt;file&gt; --reason &lt;why&gt;</span> and
              read the warnings + guidance it returns."
            </p>
          </Section>

        </div>

        <div className="border-t border-line px-4 py-2.5 flex items-center justify-between
                        text-[11px] text-muted">
          <span>Press <kbd className="px-1 py-px rounded bg-sunken border border-line text-ink">Esc</kbd> to close</span>
          <Button size="sm" variant="primary" onClick={onClose}>got it</Button>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted mb-1.5">
        {title}
      </h3>
      <div className="text-ink">{children}</div>
    </section>
  );
}
