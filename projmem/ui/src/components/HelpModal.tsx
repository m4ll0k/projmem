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

          <Section title="Pick your agent — projmem speaks all of them">
            <p>
              <span className="font-mono">projmem init &lt;agent&gt;</span> drops the
              right instruction file(s) for whatever assistant you use. The
              agent then knows to call projmem before editing.
            </p>
            <div className="overflow-x-auto mt-1.5">
              <table className="text-[11px] font-mono w-full">
                <thead className="text-muted">
                  <tr className="text-left">
                    <th className="pr-3 pb-1 font-normal">init flag</th>
                    <th className="pr-3 pb-1 font-normal">drops</th>
                    <th className="pb-1 font-normal">for</th>
                  </tr>
                </thead>
                <tbody className="text-ink">
                  <tr><td className="pr-3 py-0.5">claude</td>     <td className="pr-3">CLAUDE.md</td>      <td>Claude Code · Anthropic API · MCP</td></tr>
                  <tr><td className="pr-3 py-0.5">codex</td>      <td className="pr-3">AGENTS.md</td>      <td>OpenAI Codex CLI</td></tr>
                  <tr><td className="pr-3 py-0.5">gemini</td>     <td className="pr-3">GEMINI.md</td>      <td>Gemini CLI · Code Assist</td></tr>
                  <tr><td className="pr-3 py-0.5">cursor</td>     <td className="pr-3">.cursorrules</td>   <td>Cursor editor</td></tr>
                  <tr><td className="pr-3 py-0.5">copilot</td>    <td className="pr-3">.github/copilot-instructions.md</td><td>GitHub Copilot</td></tr>
                  <tr><td className="pr-3 py-0.5">aider</td>      <td className="pr-3">AGENTS.md</td>      <td>aider · droid · trae · hermes · openclaw</td></tr>
                  <tr><td className="pr-3 py-0.5">opencode</td>   <td className="pr-3">opencode.md</td>    <td>OpenCode</td></tr>
                  <tr><td className="pr-3 py-0.5">antigravity</td><td className="pr-3">antigravity.md</td><td>Antigravity</td></tr>
                  <tr><td className="pr-3 py-0.5">kiro</td>       <td className="pr-3">.kiro/steering/</td><td>Kiro</td></tr>
                  <tr><td className="pr-3 py-0.5">all</td>        <td className="pr-3">every file above</td><td>multi-agent setup</td></tr>
                  <tr><td className="pr-3 py-0.5 text-accent">auto</td><td className="pr-3 text-accent">picks from env vars</td><td className="text-accent">default if you omit the flag</td></tr>
                </tbody>
              </table>
            </div>
          </Section>

          <Section title="The CLI surface (what your agent calls)">
            <pre className="bg-code-bg border border-line rounded p-2 text-[11px]
                            font-mono leading-relaxed overflow-x-auto mt-1.5">
{`# Announce intent + receive context (notes, guidance, critical, history, exclusions).
projmem editing path/to/file.py --reason "fix race in poller"

# Read-only briefing without opening a lease.
projmem context path/to/file.py

# Line-precise context (file:line).
projmem at path/to/file.py:42

# Pin a fact during the session.
projmem note add path/to/file.py "imports run() from helpers.py"

# Close the lease when done.
projmem done <lease-id>`}
            </pre>
            <p className="text-xs text-muted">
              The <span className="font-mono">editing</span> response surfaces the
              highest-leverage context first: line-scoped notes &gt; symbol &gt;
              file &gt; dir &gt; project-wide. Critical rules and exclusions
              show up in <span className="font-mono">warnings[]</span> so the
              agent can't miss them, and a pending-approval lease blocks the
              edit until you click <i>approve</i> in this UI.
            </p>
          </Section>

          <Section title="Ghosts, lifelines & the history trail">
            <p>
              projmem doesn't forget. Every file has a <b>lifeline</b> — one
              stable id that survives renames, moves, and deletes. So a note
              you pinned on <span className="font-mono">src/auth.py</span>
              still applies after a rename to
              <span className="font-mono"> src/authn.py</span>.
            </p>
            <ul className="list-disc list-inside space-y-1 mt-1.5">
              <li>
                <b>Ghost file</b> — a lifeline whose file was deleted. Its
                history, notes, and links stay in the index so you (or the
                agent) can still answer "what was in that module?" or
                "what depended on it before we removed it?".
              </li>
              <li>
                <b>Tombstone</b> — the deletion event itself, recorded on the
                lifeline with a timestamp and the reason from
                <span className="font-mono"> projmem deleting</span>.
              </li>
              <li>
                <b>Show ghosts</b> — toggle on the graph to surface deleted
                files as dimmed nodes. Useful for archaeology
                ("why was this module removed?") and for following blast
                radius across renames.
              </li>
              <li>
                <b>History tab</b> in the inspector — chronological event
                stream for any file: created · leased · edited · moved ·
                deleted. Implicit edits (agent edited without announcing) get
                a dashed warn border in the activity feed.
              </li>
              <li>
                <b>Moves</b> — recorded as a single <span className="font-mono">moved</span> event on the lifeline rather than
                a delete + create pair, so the agent doesn't lose context.
              </li>
            </ul>
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

          <Section title="Live disk sync">
            <p>
              <span className="font-mono">projmem ui</span> keeps the UI in
              sync with disk automatically. A background sweeper re-indexes
              every <b>5 seconds</b> by default, so:
            </p>
            <ul className="list-disc list-inside space-y-1 mt-1.5">
              <li>
                Files dropped by <span className="font-mono">projmem init</span>
                (CLAUDE.md, AGENTS.md, …) show up on their own.
              </li>
              <li>
                Files your agent writes via its <i>own</i> tools (not via
                <span className="font-mono"> projmem creating</span>) get
                detected, indexed, and broadcast as <span className="font-mono">created</span>/
                <span className="font-mono">deleted</span>/<span className="font-mono">edited</span> events
                — the activity feed, graph, tree, and schema refresh
                without a manual <span className="font-mono">projmem index</span>.
              </li>
              <li>
                On a quiet workspace the sweeper is cheap — the indexer
                skips files whose hash hasn't changed.
              </li>
            </ul>
            <p className="text-xs text-muted mt-2">
              Disable with <span className="font-mono">projmem ui --no-watch</span>
              {" "}(useful when you'd rather drive everything through explicit
              <span className="font-mono"> projmem creating/editing</span> calls), or
              tune the cadence with <span className="font-mono">--watch-interval 1.0</span>.
            </p>
          </Section>

          <Section title="Quick start">
            <p className="mb-1.5">
              <b>Fresh project</b> — drop instructions for your agent, build
              the index, open the UI:
            </p>
            <pre className="bg-code-bg border border-line rounded p-2 text-[11px]
                            font-mono overflow-x-auto">
{`cd path/to/your/repo
projmem init claude       # or codex / gemini / cursor / copilot / all / auto
projmem index
projmem ui --port 7777`}
            </pre>
            <p className="mt-2.5 mb-1.5">
              <b>Existing project</b> — the repo already has a
              <span className="font-mono"> .projmem/</span> dir, you just want
              to refresh:
            </p>
            <pre className="bg-code-bg border border-line rounded p-2 text-[11px]
                            font-mono overflow-x-auto">
{`projmem init claude --reindex   # or just: projmem index --force
projmem ui --port 7777`}
            </pre>
            <p className="mt-2.5 mb-1.5">
              <b>Switching agents</b> or running multiple at once — overwrite
              with <span className="font-mono">--force</span>, or drop every
              instruction file in one shot:
            </p>
            <pre className="bg-code-bg border border-line rounded p-2 text-[11px]
                            font-mono overflow-x-auto">
{`projmem init all --force         # CLAUDE.md + AGENTS.md + GEMINI.md + .cursorrules + …
projmem init gemini --force      # just swap to a different one`}
            </pre>
            <p className="text-xs text-muted mt-2">
              Once the instruction file is in place your agent will read it on
              every session and call <span className="font-mono">projmem editing</span> before
              touching files. You don't need to remind it each turn.
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
