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
