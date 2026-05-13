// Shapes broadcast by the daemon over /events. Kept loose on purpose —
// new event kinds can be added by the backend without breaking the UI.

export type EventKind =
  | "note_added"
  | "critical_added"
  | "paused"
  | "resumed"
  | "lease_approved"
  | "lease_denied"
  | "leased"
  | "released"
  | "abandoned"
  | "created"
  | "edited"
  | "moved"
  | "deleted"
  | string;

export interface DaemonEvent {
  kind: EventKind;
  at?: number;
  // The rest is event-shaped; pulled out lazily in the UI.
  [k: string]: unknown;
}

export interface OpenLease {
  id:           string;
  lifeline_id:  string;
  opened_at:    number;
  expires_at:   number;
  state:        "open" | "pending_approval" | "closed";
  agent_id?:    string | null;
  intent?:      string | null;
}

export interface DaemonState {
  root:           string;
  paused:         boolean;
  open_leases:    OpenLease[];
  recent_events:  DaemonEvent[];
}

// ── /graph (Step 7) ───────────────────────────────────────────────────────

export type Staleness =
  | "fresh"
  | "weakly_stale"
  | "strongly_stale"
  | "contradicted"
  | "tombstoned"
  | "unknown";

export interface GraphNode {
  id:                string;          // lifeline_id OR `sym:<file>:<name>:<line>`
  path:              string | null;   // current_path; null possible for ghosts
  label:             string;          // basename for files, symbol name for symbols
  staleness:         Staleness;
  critical:          boolean;
  rev_deps:          number;
  leased:            boolean;
  ghost:             boolean;
  symbol?:           boolean;
  symbol_kind?:      string;          // "function" | "class" | "method" | …
  line?:             number;
  tombstoned_at?:    number | null;
  tombstoned_reason?: string | null;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind:   "imports" | "replaced_by" | string;
}

export interface GraphPayload {
  nodes:          GraphNode[];
  edges:          GraphEdge[];
  include_ghosts: boolean;
  node_count:     number;
  edge_count:     number;
}

// ── /lifeline/{id} (Step 7 inspector) ─────────────────────────────────────

export interface FileEvent {
  id:                number;
  lifeline_id:       string;
  kind:              string;
  at:                number;
  reason?:           string | null;
  diff_summary?:     string | null;
  symbols_affected?: string | null;
}

export interface Annotation {
  id:           number;
  target:       string;
  kind:         string;
  body:         string;
  severity?:    string | null;
  staleness?:   string | null;
  category?:    string | null;
  approved_by?: string | null;
  created_at?:  number | null;
}

export interface LifelineDetail {
  lifeline:  {
    id:                string;
    current_path:      string | null;
    created_at:        number;
    created_reason:    string;
    tombstoned_at?:    number | null;
    tombstoned_reason?: string | null;
  };
  events:    FileEvent[];
  notes:     Annotation[];
  critical:  Annotation[];
}
