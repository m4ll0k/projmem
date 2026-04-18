"""Minimal runtime evidence ingester (JSONL).

Each line: {"file": "path", "symbol": "optional", "kind": "log|trace|event", "note": "..."}.
We attach entries as `evidence` rows referencing the given file/symbol. We do NOT claim
causality — this is declarative, not inferred.
"""
from __future__ import annotations
import json
from typing import Dict

from .store import Store


def ingest_jsonl(store: Store, jsonl_path: str) -> Dict[str, int]:
    """Ingest a JSONL stream of runtime evidence rows.

    F016 (round-7): the prior implementation accepted any string as
    `target`, including obvious typos like `not-a-real-target`. That
    poisoned `drift` (a junk target counts as "exercised" forever).
    We now require target to resolve against the index — either an
    indexed file path OR a known symbol name. Unknown targets are
    counted as `invalid_target` and dropped, NOT stored.

    Accepted shapes per line (one of):
      {"file":   "path/to/file.ts", ...}
      {"symbol": "myFunction",      ...}
      {"target": "<file or symbol>", ...}    # convenience alias
    """
    counts: Dict[str, int] = {
        "ok":             0,
        "bad":            0,   # JSON parse failures
        "missing_target": 0,   # row had no file/symbol/target key
        "invalid_target": 0,   # target didn't match any indexed file/symbol
    }
    invalid_examples: list = []
    # Pre-pull the indexed sets once. Cheap; lookups are O(1).
    indexed_files = {r["path"] for r in store.conn.execute(
        "SELECT path FROM files")}
    indexed_symbols = {r["name"] for r in store.conn.execute(
        "SELECT DISTINCT name FROM symbols")}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                counts["bad"] += 1
                continue
            file = (obj.get("file") or "").strip()
            sym  = (obj.get("symbol") or "").strip()
            tgt  = (obj.get("target") or "").strip()
            target = file or sym or tgt
            if not target:
                counts["missing_target"] += 1
                continue
            # Validate against the index. A target is OK when it's
            # either an indexed file path or a known symbol name.
            if (target not in indexed_files
                    and target not in indexed_symbols):
                counts["invalid_target"] += 1
                if len(invalid_examples) < 10:
                    invalid_examples.append({
                        "line":   line_no,
                        "target": target,
                    })
                continue
            store.add_evidence(
                source=jsonl_path,
                target=target,
                kind=str(obj.get("kind") or "runtime"),
                note=str(obj.get("note") or ""),
            )
            counts["ok"] += 1
    store.commit()
    if invalid_examples:
        counts_out: Dict[str, object] = dict(counts)
        counts_out["invalid_target_examples"] = invalid_examples
        return counts_out  # type: ignore[return-value]
    return counts
