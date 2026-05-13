"""SQLite-backed index store.

Schema favors readability and inspection. Tables:
  files, symbols, refs, edges, contracts, entrypoints, meta, evidence.

Confidence values: 'high' | 'medium' | 'low' | 'unknown'.

Ref roles (SCIP-shaped SymbolRole bitset):
  The low 7 bits match SCIP exactly so a future SCIP importer can copy bits
  straight through. Higher bits are projmem extensions that SCIP folds into
  Read (call/new/callback/shorthand) but we preserve for ref-kind queries.
"""
from __future__ import annotations
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Bits 0..6 mirror scip.proto SymbolRole exactly — DO NOT renumber.
ROLE_DEFINITION       = 1 << 0  # 0x001
ROLE_IMPORT           = 1 << 1  # 0x002
ROLE_WRITE            = 1 << 2  # 0x004
ROLE_READ             = 1 << 3  # 0x008
ROLE_GENERATED        = 1 << 4  # 0x010
ROLE_TEST             = 1 << 5  # 0x020
ROLE_FORWARD_DEF      = 1 << 6  # 0x040
# Bits 7..11 are projmem extensions for finer ref-kind queries.
ROLE_CALL             = 1 << 7  # 0x080 — `foo()`
ROLE_NEW              = 1 << 8  # 0x100 — `new Foo()`
ROLE_IMPORT_BINDING   = 1 << 9  # 0x200 — `const {X}=require(...)` / `import {X} from ...`
ROLE_CALLBACK         = 1 << 10 # 0x400 — `.map(foo)`
ROLE_SHORTHAND        = 1 << 11 # 0x800 — `{foo}` in object literal

# kind-string → roles mapping for backward-compat emitters.
REF_KIND_TO_ROLES: Dict[str, int] = {
    "call":            ROLE_CALL | ROLE_READ,
    "new":             ROLE_NEW | ROLE_READ,
    "import_binding":  ROLE_IMPORT_BINDING | ROLE_IMPORT | ROLE_READ,
    "callback":        ROLE_CALLBACK | ROLE_READ,
    "shorthand":       ROLE_SHORTHAND | ROLE_READ,
    "name":            ROLE_READ,
    # M3: inheritance refs. Use ReadAccess role since a subtype reads its
    # parent's contract. No separate SCIP bit; captured via kind string.
    "extends":         ROLE_READ,
    "implements":      ROLE_READ,
}


def roles_describe(roles: int) -> List[str]:
    """Human-readable list of role names for a bitset. Used in output."""
    labels = [
        (ROLE_DEFINITION, "def"), (ROLE_IMPORT, "import"),
        (ROLE_WRITE, "write"), (ROLE_READ, "read"),
        (ROLE_GENERATED, "generated"), (ROLE_TEST, "test"),
        (ROLE_FORWARD_DEF, "forward_def"),
        (ROLE_CALL, "call"), (ROLE_NEW, "new"),
        (ROLE_IMPORT_BINDING, "import_binding"),
        (ROLE_CALLBACK, "callback"), (ROLE_SHORTHAND, "shorthand"),
    ]
    return [name for bit, name in labels if roles & bit]


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  lang TEXT,
  hash TEXT,
  mtime REAL,
  size INTEGER,
  parser TEXT,          -- 'ast' | 'regex' | 'none'
  indexed_at REAL,
  stale INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS symbols (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file TEXT, name TEXT, kind TEXT,
  line INTEGER, col INTEGER, exported INTEGER,
  confidence TEXT,
  end_line INTEGER,        -- M2: enclosing_range upper bound (SCIP-shaped)
  end_col INTEGER,
  symbol_id TEXT           -- M8: SCIP-shaped deterministic ID; <file>#<name><suffix>
);
CREATE INDEX IF NOT EXISTS idx_sym_symbol_id ON symbols(symbol_id);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file);

CREATE TABLE IF NOT EXISTS refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file TEXT, name TEXT, kind TEXT,
  line INTEGER, confidence TEXT,
  roles INTEGER DEFAULT 0           -- SCIP-shaped SymbolRole bitset; see store.ROLE_*
);
CREATE INDEX IF NOT EXISTS idx_ref_name ON refs(name);
CREATE INDEX IF NOT EXISTS idx_ref_file ON refs(file);

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src TEXT, dst TEXT, type TEXT,
  confidence TEXT, evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edge_type ON edges(type);

-- JS ↔ native binding edges (Node/V8 style `SetMethod(..., "name", CppFn)`).
-- Stored separately from `edges` so they can be scoped to a source file and
-- cleaned up on reindex, while still surfacing as structural evidence.
CREATE TABLE IF NOT EXISTS bindings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file TEXT,
  line INTEGER,
  js_name TEXT,
  cpp_name TEXT,
  cpp_symbol_id TEXT,
  confidence REAL,
  reason TEXT,
  evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_bind_file ON bindings(file);
CREATE INDEX IF NOT EXISTS idx_bind_js ON bindings(js_name);
CREATE INDEX IF NOT EXISTS idx_bind_cpp ON bindings(cpp_name);

CREATE TABLE IF NOT EXISTS contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,            -- 'flag' | 'env' | 'schema_field' | 'token'
  name TEXT,
  file TEXT,
  line INTEGER,
  role TEXT,            -- 'declare' | 'parse' | 'read' | 'write' | 'use' | 'occurrence'
  confidence TEXT,
  context TEXT
);
CREATE INDEX IF NOT EXISTS idx_con_name ON contracts(name);
CREATE INDEX IF NOT EXISTS idx_con_kind ON contracts(kind);
CREATE INDEX IF NOT EXISTS idx_con_file ON contracts(file);

CREATE TABLE IF NOT EXISTS entrypoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file TEXT, kind TEXT, confidence TEXT, evidence TEXT,
  indexed INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, target TEXT, kind TEXT, note TEXT, ts REAL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);

-- Command log: every CLI invocation gets a row so the next agent
-- can ask "what has been examined here recently?". Captures
-- (timestamp, command, target, args, author). Auto-logged at the
-- command boundary in cli.py — agents do not need to call manually.
CREATE TABLE IF NOT EXISTS command_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  command TEXT NOT NULL,
  target TEXT,
  args TEXT,
  author TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmdlog_ts ON command_log(ts);
CREATE INDEX IF NOT EXISTS idx_cmdlog_target ON command_log(target);

-- Annotations: human/agent assertions about a file or symbol that
-- survive reindex. The killer use-case is "I already verified this
-- code is safe — don't re-investigate." When `projmem pack` returns
-- a target's context, its annotations are surfaced at the top with
-- `confidence: human-asserted` so the consumer sees the prior verdict
-- BEFORE re-running the analysis.
--
-- `target` is the exact string the user provided. Lookup matches:
--   * exact string equality
--   * if target is a file, also pulls anything keyed on `file#name` or
--     `file#name.` (SCIP suffix) for symbols inside that file
-- Stored kinds are conventional, not enforced: `note`, `refute`,
-- `verified-safe`, `documented-footgun`, `todo`, `link`, `risk`. Tools
-- can extend.
CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  author TEXT,
  created_at REAL NOT NULL,
  expires_at REAL,
  -- Integrity fields (SPEC: claim schema + change-impact hashing +
  -- freshness). ``fingerprint`` captures the code state the note was
  -- created against; ``staleness`` is recomputed on access by
  -- projmem/integrity.py::revalidate.
  confidence REAL DEFAULT 0.5,
  confidence_base REAL,    -- asserted baseline confidence (pre-decay)
  evidence TEXT,            -- JSON: list[{file, line, snippet?}]
  assumptions TEXT,         -- JSON: list[str]
  scope TEXT,               -- 'symbol' | 'file' | 'subsystem'
  truth_class TEXT DEFAULT 'INFERENCE',
                            -- 'FACT' | 'INFERENCE' | 'ASSUMPTION' | 'UNKNOWN'
  fingerprint TEXT,         -- JSON: {file_hash, symbol_hash, callers_hash, contracts_hash}
  last_verified_at REAL,
  staleness TEXT DEFAULT 'unknown'
                            -- 'fresh' | 'weakly_stale' | 'strongly_stale' | 'contradicted' | 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_ann_target ON annotations(target);
CREATE INDEX IF NOT EXISTS idx_ann_kind ON annotations(kind);

-- Contract snapshots: labelled copies of the `contracts` table frozen at a
-- point in time. Drives `contract-diff` (added/removed/moved) and — by
-- projection — the obligation graph. One row per (label, kind, name, file,
-- line, role). Intentionally mirrors the `contracts` schema so diff joins
-- don't need column remapping.
-- Symbol snapshots — mirror of contract_snapshots for the symbols
-- table. Drives `projmem symbol-diff` (added/removed/moved).
CREATE TABLE IF NOT EXISTS symbol_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  ts REAL NOT NULL,
  file TEXT,
  name TEXT,
  kind TEXT,
  line INTEGER,
  symbol_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_symsnap_label ON symbol_snapshots(label);
CREATE INDEX IF NOT EXISTS idx_symsnap_lookup ON symbol_snapshots(label, file, name);

CREATE TABLE IF NOT EXISTS contract_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  ts REAL NOT NULL,
  kind TEXT,
  name TEXT,
  file TEXT,
  line INTEGER,
  role TEXT,
  confidence TEXT,
  context TEXT
);
CREATE INDEX IF NOT EXISTS idx_csnap_label ON contract_snapshots(label);
CREATE INDEX IF NOT EXISTS idx_csnap_lookup ON contract_snapshots(label, kind, name);

-- Tasks: agent-visible work-in-progress state. Each row is an
-- ongoing or completed goal. Carries session-continuity data so
-- session 2 can resume where session 1 left off.
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal TEXT NOT NULL,                -- "add webhook type SLACK"
  status TEXT NOT NULL DEFAULT 'active',  -- active | blocked | done
  author TEXT,                        -- agent / session id that opened it
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  closed_at REAL                      -- NULL while open
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at);

-- task_events: append-only progress log per task. Records steps,
-- blockers, file touches, and claim saves so `task resume` can
-- reconstruct the full arc.
CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,    -- 'step' | 'blocked' | 'unblocked' | 'file' | 'note' | 'status'
  detail TEXT,
  ref TEXT,              -- optional pointer: file path, note id, etc.
  ts REAL NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_ts ON task_events(ts);

-- Append-only log of every file hash transition seen by the indexer.
-- Survives re-indexing (which rotates the pre-index snapshot); this is
-- the data that powers "what did we edit since last session?" across
-- multiple `projmem index` or `projmem complete` runs.
CREATE TABLE IF NOT EXISTS file_edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  prev_hash TEXT,       -- NULL when the file is newly added
  new_hash TEXT,        -- NULL when the file was deleted
  ts REAL NOT NULL,     -- epoch seconds
  session_id TEXT,      -- optional: ties edits from one index run together
  summary TEXT          -- 1-line NL description: "added foo(); removed bar"
);
CREATE INDEX IF NOT EXISTS idx_file_edits_ts ON file_edits(ts);
CREATE INDEX IF NOT EXISTS idx_file_edits_path_ts ON file_edits(path, ts);
"""


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.path = db_path
        # `timeout=30.0` makes sqlite3 driver wait up to 30s for a locked
        # database before raising OperationalError. Combined with WAL this
        # makes `projmem stats` / `symbol` safe to run concurrently with a
        # long-running `index` — the round-2 report showed `stats` failing
        # with "database is locked" during reindex.
        self.conn = sqlite3.connect(db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        try:
            # WAL allows concurrent readers during a writer. `synchronous=NORMAL`
            # is the recommended pairing for WAL on local dev tools — durable
            # enough, much faster than FULL.
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA busy_timeout=30000;")
        except sqlite3.OperationalError:
            pass  # some fs (network) reject WAL; fall through to default journal.
        self.conn.executescript(SCHEMA)
        # Minimal schema migrations for round-3 columns. Safe to run every
        # open — duplicate ADD COLUMN raises OperationalError which we swallow.
        for stmt in (
            "ALTER TABLE entrypoints ADD COLUMN indexed INTEGER DEFAULT 1",
            "ALTER TABLE refs ADD COLUMN roles INTEGER DEFAULT 0",
            "ALTER TABLE symbols ADD COLUMN end_line INTEGER",
            "ALTER TABLE symbols ADD COLUMN end_col INTEGER",
            "ALTER TABLE symbols ADD COLUMN symbol_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_sym_symbol_id ON symbols(symbol_id)",
            "ALTER TABLE refs ADD COLUMN target_symbol_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_ref_target_symbol_id ON refs(target_symbol_id)",
            # Annotations table — make sure indexes exist on legacy DBs
            # that may have been created before this feature shipped.
            "CREATE INDEX IF NOT EXISTS idx_ann_target ON annotations(target)",
            "CREATE INDEX IF NOT EXISTS idx_ann_kind ON annotations(kind)",
            # Integrity fields for annotations. Additive so legacy DBs
            # keep their existing notes — defaults preserve old behaviour
            # (confidence=0.5 neutral, staleness='unknown' surfaces as
            # "needs verification" in pack output).
            "ALTER TABLE annotations ADD COLUMN confidence REAL DEFAULT 0.5",
            "ALTER TABLE annotations ADD COLUMN confidence_base REAL",
            "ALTER TABLE annotations ADD COLUMN evidence TEXT",
            "ALTER TABLE annotations ADD COLUMN assumptions TEXT",
            "ALTER TABLE annotations ADD COLUMN scope TEXT",
            "ALTER TABLE annotations ADD COLUMN truth_class TEXT DEFAULT 'INFERENCE'",
            "ALTER TABLE annotations ADD COLUMN fingerprint TEXT",
            "ALTER TABLE annotations ADD COLUMN last_verified_at REAL",
            "ALTER TABLE annotations ADD COLUMN staleness TEXT DEFAULT 'unknown'",
            # Command log indexes (legacy DB compat)
            "CREATE INDEX IF NOT EXISTS idx_cmdlog_ts ON command_log(ts)",
            "CREATE INDEX IF NOT EXISTS idx_cmdlog_target ON command_log(target)",
            # file_edits natural-language summary column (additive)
            "ALTER TABLE file_edits ADD COLUMN summary TEXT",
        ):
            try: self.conn.execute(stmt)
            except sqlite3.OperationalError: pass
        self.conn.commit()
        # Versioned v2+ migrations. The pre-v2 ALTERs above stay because
        # they may run against legacy DBs whose schema_version stamp is
        # never set; the runner below tracks every migration that lands
        # on v2-dev or later under projmem/migrations/.
        from projmem import migrations as _migrations
        _migrations.apply_pending(self.conn)

    # ---- file bookkeeping ----
    def get_file(self, path: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    def upsert_file(self, path: str, lang: str, hash_: str, mtime: float,
                    size: int, parser: str) -> None:
        self.conn.execute(
            "INSERT INTO files(path,lang,hash,mtime,size,parser,indexed_at,stale) "
            "VALUES(?,?,?,?,?,?,?,0) "
            "ON CONFLICT(path) DO UPDATE SET lang=excluded.lang,hash=excluded.hash,"
            "mtime=excluded.mtime,size=excluded.size,parser=excluded.parser,"
            "indexed_at=excluded.indexed_at,stale=0",
            (path, lang, hash_, mtime, size, parser, time.time()),
        )
        self._ensure_lifeline(path)

    def _ensure_lifeline(self, path: str) -> None:
        """Create a lifeline + 'created' file_event if this file has none.

        Bridges the gap between migration-time backfill (which only sees
        files already in the index) and post-migration indexing of new
        files. Idempotent: if the file already carries a `lifeline_id`,
        this is a single SELECT and returns.

        Files indexed via plain `projmem index` (without going through
        the Step 1 `creating` verb) get `created_reason =
        "indexed — no explicit creation event"` so the UI can later
        render them with a distinct treatment, the same way implicit
        leases will be marked.
        """
        row = self.conn.execute(
            "SELECT lifeline_id FROM files WHERE path=?", (path,),
        ).fetchone()
        if not row or row[0]:
            return
        import uuid
        lid = str(uuid.uuid4())
        now = time.time()
        reason = "indexed — no explicit creation event"
        self.conn.execute(
            "INSERT INTO file_lifeline(id, current_path, created_at, "
            "created_reason) VALUES(?, ?, ?, ?)",
            (lid, path, now, reason),
        )
        self.conn.execute(
            "INSERT INTO file_event(lifeline_id, kind, at, reason) "
            "VALUES(?, 'created', ?, ?)",
            (lid, now, reason),
        )
        self.conn.execute(
            "UPDATE files SET lifeline_id=? WHERE path=?", (lid, path),
        )

    def delete_file_data(self, path: str) -> None:
        for t in ("symbols", "refs", "contracts"):
            self.conn.execute(f"DELETE FROM {t} WHERE file=?", (path,))
        self.conn.execute("DELETE FROM edges WHERE src=?", (path,))
        self.conn.execute("DELETE FROM bindings WHERE file=?", (path,))
        self.conn.execute("DELETE FROM entrypoints WHERE file=?", (path,))

    def remove_file(self, path: str) -> None:
        self.delete_file_data(path)
        # Also drop inbound edges since the target file no longer exists.
        self.conn.execute("DELETE FROM edges WHERE dst=?", (path,))
        self.conn.execute("DELETE FROM files WHERE path=?", (path,))

    def expire_annotations_for_deleted_file(self, path: str) -> int:
        """Soft-expire all non-expired notes whose target is a file that no
        longer exists on disk.  Returns the count of notes expired.

        Bug fix bench iter 5 — orphan_verified_regression / Bug 2:
        `projmem complete` removed the file from the index but left its
        notes counted as 'contradicted', which kept contradicted_count
        elevated and blocked agents on every subsequent session.
        """
        now = time.time()
        cur = self.conn.execute(
            "UPDATE annotations SET expires_at=? "
            "WHERE target=? AND (expires_at IS NULL OR expires_at > ?)",
            (now, path, now),
        )
        return cur.rowcount

    def all_files(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM files"))

    def mark_stale(self, path: str) -> None:
        self.conn.execute("UPDATE files SET stale=1 WHERE path=?", (path,))

    # ---- inserts ----
    def add_symbol(self, **k):
        """Insert a symbol. Auto-derives `symbol_id` (SCIP-shaped) when not
        supplied. Format: `<file>#<name><suffix>`. M8 milestone."""
        if "symbol_id" not in k:
            from . import symbol_id as _sid
            k["symbol_id"] = _sid.build(
                k.get("file") or "", k.get("name") or "", k.get("kind") or "")
        self._ins("symbols", k)

    def add_ref(self, **k):
        """Insert a ref. If `roles` not supplied, derives it from `kind` via
        `REF_KIND_TO_ROLES` so all emitters keep working without explicit
        knowledge of the bitset.
        """
        if "roles" not in k:
            k["roles"] = REF_KIND_TO_ROLES.get(k.get("kind") or "", 0)
        self._ins("refs", k)

    def add_edge(self, **k):
        """Insert an edge, skipping exact duplicates on (src, dst, type).

        Round-3 report: `reverse scanner.js` showed the same
        `builtin:node:child_process` import edge repeatedly because every
        `require('child_process')` call site emitted its own row. Dedupe is
        cheap at insert time — we only keep distinct (src, dst, type).
        """
        if self.conn.execute(
            "SELECT 1 FROM edges WHERE src=? AND dst=? AND type=? LIMIT 1",
            (k.get("src"), k.get("dst"), k.get("type"))
        ).fetchone():
            return
        self._ins("edges", k)
    def add_contract(self, **k): self._ins("contracts", k)
    def add_entrypoint(self, **k): self._ins("entrypoints", k)
    def add_evidence(self, **k):
        k.setdefault("ts", time.time()); self._ins("evidence", k)

    def _ins(self, table: str, k: Dict[str, Any]) -> None:
        cols = ",".join(k.keys())
        qs = ",".join(["?"] * len(k))
        self.conn.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", tuple(k.values()))

    # ---- queries ----
    def symbols_by_name(self, name: str) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM symbols WHERE name=?", (name,)))

    def symbol_by_id(self, sid: str) -> Optional[sqlite3.Row]:
        """M8: O(1) lookup via SCIP-shaped symbol_id."""
        return self.conn.execute(
            "SELECT * FROM symbols WHERE symbol_id=? LIMIT 1", (sid,)).fetchone()

    def resolve_name_to_symbol_id(self, name: str,
                                  prefer_kind: Optional[str] = None
                                  ) -> Optional[str]:
        """Best-effort: find a unique symbol_id for `name`. Returns None if
        zero or multiple defs exist (caller should treat as unresolved).
        If `prefer_kind` is set, it narrows ambiguity when the kind matches.

        ORDER CAVEAT: this is called during indexing, so the answer depends
        on which files have been indexed so far. In a clean full reindex
        the file walk is deterministic, but a same-name def in a later
        file won't disambiguate an edge written from an earlier file.
        Mitigation (future M8.5): a deferred "edge re-resolution" pass at
        end-of-index that downgrades single-resolved edges to ambiguous
        when post-hoc resolution would now find multiple matches.
        """
        rows = list(self.conn.execute(
            "SELECT symbol_id, kind FROM symbols WHERE name=?", (name,)))
        if not rows:
            return None
        if prefer_kind:
            kind_match = [r for r in rows if r["kind"] == prefer_kind]
            if len(kind_match) == 1:
                return kind_match[0]["symbol_id"]
        if len(rows) == 1:
            return rows[0]["symbol_id"]
        return None  # ambiguous

    def refs_by_name(self, name: str,
                     kinds: Optional[tuple] = None) -> List[sqlite3.Row]:
        """Rows from `refs` with this name. If `kinds` is given (e.g.
        `("call",)`), restrict the result to those ref kinds. Used by
        `trace_call_chain` strict mode to avoid crossing non-call edges
        (reads, imports, callbacks) which would manufacture semantically
        meaningless paths.
        """
        if kinds:
            ph = ",".join("?" * len(kinds))
            return list(self.conn.execute(
                f"SELECT * FROM refs WHERE name=? AND kind IN ({ph})",
                (name, *kinds)))
        return list(self.conn.execute("SELECT * FROM refs WHERE name=?", (name,)))

    def edges_from(self, src: str, type_: Optional[str] = None) -> List[sqlite3.Row]:
        if type_:
            return list(self.conn.execute("SELECT * FROM edges WHERE src=? AND type=?", (src, type_)))
        return list(self.conn.execute("SELECT * FROM edges WHERE src=?", (src,)))

    def edges_to(self, dst: str, type_: Optional[str] = None) -> List[sqlite3.Row]:
        if type_:
            return list(self.conn.execute("SELECT * FROM edges WHERE dst=? AND type=?", (dst, type_)))
        return list(self.conn.execute("SELECT * FROM edges WHERE dst=?", (dst,)))

    def contracts_by_name(self, name: str, kind: Optional[str] = None) -> List[sqlite3.Row]:
        if kind:
            return list(self.conn.execute(
                "SELECT * FROM contracts WHERE name=? AND kind=?", (name, kind)))
        return list(self.conn.execute("SELECT * FROM contracts WHERE name=?", (name,)))

    def contracts_in_file(self, file: str) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM contracts WHERE file=?", (file,)))

    def entrypoints(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM entrypoints"))

    def stats(self) -> Dict[str, int]:
        out = {}
        for t in ("files", "symbols", "refs", "edges", "bindings",
                  "contracts", "entrypoints", "evidence"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        out["stale_files"] = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE stale=1").fetchone()[0]
        return out

    # ---- bindings (JS ↔ native) ----
    def add_binding(self, *, file: str, line: int,
                    js_name: str, cpp_name: str,
                    cpp_symbol_id: Optional[str] = None,
                    confidence: float = 0.9,
                    reason: str = "",
                    evidence: str = "") -> None:
        self.conn.execute(
            "INSERT INTO bindings(file, line, js_name, cpp_name, cpp_symbol_id, "
            "confidence, reason, evidence) VALUES(?,?,?,?,?,?,?,?)",
            (file, int(line), js_name, cpp_name, cpp_symbol_id,
             float(confidence), reason, evidence))

    def bindings_for_js_name(self, js_name: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM bindings WHERE js_name=? ORDER BY file, line",
            (js_name,)))

    def bindings_for_cpp_symbol_id(self, cpp_symbol_id: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM bindings WHERE cpp_symbol_id=? ORDER BY file, line",
            (cpp_symbol_id,)))

    def bindings_for_cpp_name(self, cpp_name: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM bindings WHERE cpp_name=? ORDER BY file, line",
            (cpp_name,)))

    # ---- symbol snapshots (symbol-diff substrate) ----
    def snapshot_symbols(self, label: str) -> int:
        """Freeze the current `symbols` table under `label`. Overwrites
        any prior snapshot with the same label."""
        now = time.time()
        self.conn.execute(
            "DELETE FROM symbol_snapshots WHERE label=?", (label,))
        self.conn.execute(
            "INSERT INTO symbol_snapshots"
            "(label, ts, file, name, kind, line, symbol_id) "
            "SELECT ?, ?, file, name, kind, line, symbol_id FROM symbols",
            (label, now))
        self.set_meta(f"symbol_snapshot_label:{label}", str(now))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM symbol_snapshots WHERE label=?",
            (label,)).fetchone()[0]
        self.conn.commit()
        return count

    def symbol_snapshot_rows(self, label: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT file, name, kind, line, symbol_id "
            "FROM symbol_snapshots WHERE label=? "
            "ORDER BY file, name, line",
            (label,))]

    def live_symbol_rows(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT file, name, kind, line, symbol_id FROM symbols "
            "ORDER BY file, name, line")]

    def list_symbol_snapshots(self) -> List[Dict[str, Any]]:
        rows = {r["label"]: dict(r) for r in self.conn.execute(
            "SELECT label, MIN(ts) AS taken_at, COUNT(*) AS count "
            "FROM symbol_snapshots GROUP BY label")}
        for r in self.conn.execute(
                "SELECT key, value FROM meta "
                "WHERE key LIKE 'symbol_snapshot_label:%'"):
            label = r["key"].split(":", 1)[1]
            if label not in rows:
                rows[label] = {"label": label,
                               "taken_at": float(r["value"]),
                               "count": 0}
        return sorted(rows.values(), key=lambda s: -s["taken_at"])

    # ---- contract snapshots (contract-diff substrate) ----
    def snapshot_contracts(self, label: str) -> int:
        """Freeze the current `contracts` table under `label`. Overwrites any
        previous snapshot with the same label. Returns row count.

        Label existence is tracked in `meta` as `snapshot_label:<label>` so
        an empty snapshot (first index run, or a repo with no contracts)
        still shows up in `list_snapshots`. Without this, `contract-diff`
        after the very first `projmem index` would complain that
        `pre-index` does not exist."""
        now = time.time()
        self.conn.execute(
            "DELETE FROM contract_snapshots WHERE label=?", (label,))
        self.conn.execute(
            "INSERT INTO contract_snapshots"
            "(label, ts, kind, name, file, line, role, confidence, context) "
            "SELECT ?, ?, kind, name, file, line, role, confidence, context "
            "FROM contracts",
            (label, now))
        self.set_meta(f"snapshot_label:{label}", str(now))
        count = self.conn.execute(
            "SELECT COUNT(*) FROM contract_snapshots WHERE label=?",
            (label,)).fetchone()[0]
        self.conn.commit()
        return count

    def delete_snapshot(self, label: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM contract_snapshots WHERE label=?", (label,))
        self.conn.execute(
            "DELETE FROM meta WHERE key=?", (f"snapshot_label:{label}",))
        self.conn.commit()
        return cur.rowcount

    def list_snapshots(self) -> List[Dict[str, Any]]:
        # Row-level group (gives row counts).
        rows = {r["label"]: dict(r) for r in self.conn.execute(
            "SELECT label, MIN(ts) AS taken_at, COUNT(*) AS count "
            "FROM contract_snapshots GROUP BY label")}
        # Augment with meta-tracked labels (so empty snapshots still show).
        for r in self.conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'snapshot_label:%'"):
            label = r["key"].split(":", 1)[1]
            if label not in rows:
                rows[label] = {"label": label,
                               "taken_at": float(r["value"]),
                               "count": 0}
        return sorted(rows.values(), key=lambda s: -s["taken_at"])

    def snapshot_rows(self, label: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT kind, name, file, line, role, confidence, context "
            "FROM contract_snapshots WHERE label=? ORDER BY kind, name, file, line",
            (label,))]

    def live_contract_rows(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT kind, name, file, line, role, confidence, context "
            "FROM contracts ORDER BY kind, name, file, line")]

    # ---- command log (auto-trail of CLI invocations) ----
    def log_command(self, command: str, target: Optional[str] = None,
                    args: Optional[str] = None,
                    author: Optional[str] = None) -> None:
        try:
            self.conn.execute(
                "INSERT INTO command_log(ts, command, target, args, author) "
                "VALUES(?, ?, ?, ?, ?)",
                (time.time(), command, target, args, author))
            self.conn.commit()
        except Exception:
            pass  # never let logging block a command

    def audit_trail(self, target: Optional[str] = None,
                    command: Optional[str] = None,
                    limit: int = 100) -> List[Dict[str, Any]]:
        q = "SELECT * FROM command_log WHERE 1=1"
        params: List[Any] = []
        if target:
            # Match exact OR file prefix (so target='src/foo.cc' also pulls
            # 'src/foo.cc#bar' rows).
            q += " AND (target = ? OR target LIKE ?)"
            params.extend([target, target + "#%"])
        if command:
            q += " AND command = ?"
            params.append(command)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(q, params)]

    # ---- annotations (human/agent assertions; survive reindex) ----
    def add_annotation(self, target: str, kind: str, body: str,
                       author: Optional[str] = None,
                       expires_at: Optional[float] = None,
                       confidence: Optional[float] = None,
                       evidence: Optional[Any] = None,
                       assumptions: Optional[Any] = None,
                       scope: Optional[str] = None,
                       truth_class: Optional[str] = None,
                       fingerprint: Optional[Any] = None,
                       staleness: Optional[str] = None) -> int:
        import json as _json
        # Sanitize the BODY only — strips control chars, normalizes line
        # endings, caps length. Idempotent. We deliberately do NOT
        # canonicalize `target` here because annotations use multiple
        # target shapes (`@project`, `pkg/`, `file.py`, `Symbol`,
        # `file.py#sym`) and `os.path.normpath` would mangle the
        # trailing-slash form that signals "directory subsystem".
        from . import security as _sec
        body = _sec.sanitize_note_body(body)
        ev = _json.dumps(evidence) if evidence is not None else None
        asm = _json.dumps(assumptions) if assumptions is not None else None
        fp = _json.dumps(fingerprint) if fingerprint is not None else None
        cur = self.conn.execute(
            "INSERT INTO annotations(target, kind, body, author, "
            "created_at, expires_at, confidence, confidence_base, evidence, assumptions, "
            "scope, truth_class, fingerprint, last_verified_at, staleness) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target, kind, body, author, time.time(), expires_at,
             confidence if confidence is not None else 0.5,
             confidence if confidence is not None else 0.5,
             ev, asm, scope,
             truth_class or "INFERENCE",
             fp,
             time.time() if fingerprint is not None else None,
             staleness or ("fresh" if fingerprint is not None else "unknown")))
        self.conn.commit()
        return cur.lastrowid

    def update_annotation_integrity(self, ann_id: int, *,
                                    fingerprint: Optional[Any] = None,
                                    staleness: Optional[str] = None,
                                    confidence: Optional[float] = None,
                                    confidence_base: Optional[float] = None,
                                    last_verified_at: Optional[float] = None,
                                    evidence: Optional[Any] = None,
                                    ) -> None:
        """Mutator used by integrity.revalidate. Only updates the
        fields passed; other fields are left alone.

        `evidence` accepts a list (will be JSON-encoded) — used by
        `refute add --note-id` to persist a sticky `_manual_refute`
        marker into the disputed note so subsequent revalidations
        don't auto-clear the contradicted state.
        """
        import json as _json
        sets: List[str] = []
        params: List[Any] = []
        if fingerprint is not None:
            sets.append("fingerprint=?"); params.append(_json.dumps(fingerprint))
        if staleness is not None:
            sets.append("staleness=?"); params.append(staleness)
        if confidence is not None:
            sets.append("confidence=?"); params.append(float(confidence))
        if confidence_base is not None:
            sets.append("confidence_base=?"); params.append(float(confidence_base))
        if last_verified_at is not None:
            sets.append("last_verified_at=?"); params.append(float(last_verified_at))
        if evidence is not None:
            sets.append("evidence=?"); params.append(_json.dumps(evidence))
        if not sets:
            return
        params.append(ann_id)
        self.conn.execute(
            f"UPDATE annotations SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def delete_annotation(self, ann_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM annotations WHERE id=?", (ann_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_annotations(self, target: Optional[str] = None,
                         kind: Optional[str] = None,
                         include_expired: bool = False
                         ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM annotations WHERE 1=1"
        params: List[Any] = []
        if target:
            q += " AND target=?"
            params.append(target)
        if kind:
            q += " AND kind=?"
            params.append(kind)
        if not include_expired:
            now = time.time()
            q += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(now)
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(q, params)]

    def search_annotations(self, query: str,
                           include_expired: bool = False
                           ) -> List[Dict[str, Any]]:
        like = f"%{query}%"
        q = ("SELECT * FROM annotations "
             "WHERE (body LIKE ? OR target LIKE ? OR kind LIKE ?)")
        params: List[Any] = [like, like, like]
        if not include_expired:
            q += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(time.time())
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(q, params)]

    def annotations_for_pack(self, file: Optional[str] = None,
                             symbol_ids: Optional[List[str]] = None,
                             names_in_file: Optional[List[str]] = None,
                             *,
                             include_project: bool = False,
                             include_dir_prefixes: bool = False
                             ) -> List[Dict[str, Any]]:
        """Pull annotations relevant to a pack target.

        Match strategy (union):
          * exact `file` match
          * exact `symbol_id` match for each id in `symbol_ids`
          * `file#name` shorthand match for each name in `names_in_file`
          * when include_project: include `@project` notes
          * when include_dir_prefixes: include directory-prefix targets
            for `file` (e.g. `src/` and `src/auth/` for `src/auth/x.py`)
        """
        if not (file or symbol_ids or names_in_file or include_project):
            return []
        targets: set = set()
        if include_project:
            targets.add("@project")
        if file:
            targets.add(file)
            if include_dir_prefixes:
                # Add directory-prefix targets, deepest-first. We use
                # trailing-slash strings to avoid ambiguity with file paths.
                p = (file or "").replace("\\", "/").lstrip("./")
                parts = [s for s in p.split("/") if s]
                for i in range(max(0, len(parts) - 1)):
                    targets.add("/".join(parts[: i + 1]) + "/")
            for n in names_in_file or []:
                # Support both bare `file#name` and SCIP-suffixed forms.
                targets.add(f"{file}#{n}")
                for suffix in (".", "#", "/", "!"):
                    targets.add(f"{file}#{n}{suffix}")
        for sid in symbol_ids or []:
            targets.add(sid)
        if not targets:
            return []
        placeholders = ",".join(["?"] * len(targets))
        now = time.time()
        rows = self.conn.execute(
            f"SELECT * FROM annotations "
            f"WHERE target IN ({placeholders}) "
            f"  AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY created_at DESC",
            (*sorted(targets), now)).fetchall()
        return [dict(r) for r in rows]

    def set_meta(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))

    def get_meta(self, k: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r["value"] if r else None

    def commit(self): self.conn.commit()
    def close(self): self.conn.close()
