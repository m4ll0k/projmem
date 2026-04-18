"""projmem/integrity.py — note integrity layer.

Maps directly to the 16-improvement prompt. Shipping in this module:

  #1  Confidence decay          — via staleness transitions
  #3  Structured claim schema   — evidence/assumptions columns (store.py)
  #4  Contradiction detection   — detect_contradictions()
  #6  Revalidation on access    — revalidate_annotation()
  #9  Per-target integrity      — integrity_score()
  #10 Ambiguity detection       — ambiguity_for_target()
  #14 Change-impact hashing     — compute_fingerprint()
  #15 Truth classification      — carried via the truth_class column

The layering is: fingerprint captures the state a note was written
against; revalidation recomputes it and labels the note
fresh/weakly_stale/strongly_stale/contradicted; pack ordering and
the target integrity score consume those labels.

Deliberately kept independent of packs.py so the integrity layer
can be unit-tested without spinning up a pack. packs.py calls into
this module, never the reverse.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Staleness / truth taxonomy
# ---------------------------------------------------------------------------

# Staleness labels. Keep the set tight — pack ordering depends on it.
FRESH              = "fresh"
RECOVERED          = "recovered"
WEAKLY_STALE       = "weakly_stale"
STRONGLY_STALE     = "strongly_stale"
CONTRADICTED       = "contradicted"
UNKNOWN            = "unknown"

STALENESS_ORDER = {
    FRESH:          0,
    RECOVERED:      1,   # previously stale, now matches baseline again
    UNKNOWN:        2,   # no fingerprint ever captured → needs verification
    WEAKLY_STALE:   3,
    STRONGLY_STALE: 4,
    CONTRADICTED:   5,
}

# Confidence multipliers applied during revalidation. Weakly stale
# (one input moved) halves confidence; strongly stale (multiple
# inputs moved) drops to a quarter; contradicted zeros it.
CONFIDENCE_DECAY = {
    FRESH:          1.00,
    RECOVERED:      1.00,
    UNKNOWN:        0.75,
    WEAKLY_STALE:   0.50,
    STRONGLY_STALE: 0.25,
    CONTRADICTED:   0.00,
}

TRUTH_CLASSES = ("FACT", "INFERENCE", "ASSUMPTION", "UNKNOWN")


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


@dataclass
class Fingerprint:
    """Change-impact hash bundle per SPEC #14.

    A note's fingerprint captures the code inputs whose change would
    invalidate it. When ANY component hash drifts between creation
    and revalidation, the note becomes stale; how many components
    drifted determines weakly vs strongly.
    """
    file_hash:      Optional[str] = None   # whole-file content hash
    symbol_hash:    Optional[str] = None   # hash of def X(...) body
    callers_hash:   Optional[str] = None   # hash of sorted caller-file set
    contracts_hash: Optional[str] = None   # hash of relevant flags/env/schema

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_hash":      self.file_hash,
            "symbol_hash":    self.symbol_hash,
            "callers_hash":   self.callers_hash,
            "contracts_hash": self.contracts_hash,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Fingerprint":
        if not isinstance(d, dict):
            return cls()
        return cls(
            file_hash=      d.get("file_hash"),
            symbol_hash=    d.get("symbol_hash"),
            callers_hash=   d.get("callers_hash"),
            contracts_hash= d.get("contracts_hash"),
        )


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def _parse_target(target: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a target string into (file, symbol_name).

    Accepts:
      ``path/to/file.py``               → (file, None)
      ``path/to/file.py#name``          → (file, name)
      ``path/to/file.py#name.``         → (file, name)   (SCIP suffix)
      Anything else                     → (None, None)   (symbol_id etc.)
    """
    if not target:
        return None, None
    if "#" in target:
        f, _, rest = target.partition("#")
        # Strip SCIP suffix (".", "#", "/", "!")
        name = re.split(r"[.#/!]", rest, maxsplit=1)[0]
        return f or None, name or None
    # Heuristic: has a filesystem extension → treat as file.
    if "." in os.path.basename(target) and not target.startswith("$"):
        return target, None
    return None, None


def _file_content_hash(repo_root: str, file_rel: str) -> Optional[str]:
    abs_path = os.path.join(repo_root, file_rel)
    try:
        with open(abs_path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except (OSError, IsADirectoryError):
        return None


def _symbol_body_hash(repo_root: str, file_rel: str,
                     symbol_name: str) -> Optional[str]:
    """Locate ``def <symbol_name>`` (or ``function <name>``, ``class``)
    in the file and hash its body. Python-biased but works for any
    language that uses a ``def`` / ``function`` keyword as a prefix.
    """
    abs_path = os.path.join(repo_root, file_rel)
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None

    # Try Python-style indented block first.
    m = re.search(rf"^(\s*)def\s+{re.escape(symbol_name)}\b",
                  text, flags=re.MULTILINE)
    if m:
        indent = m.group(1)
        start = m.start()
        # Capture lines until dedent below indent level or EOF.
        lines = text[start:].splitlines(keepends=True)
        body = [lines[0]]
        for line in lines[1:]:
            if line.strip() == "":
                body.append(line); continue
            lead = line[:len(line) - len(line.lstrip())]
            if len(lead) <= len(indent) and line.strip():
                break
            body.append(line)
        return _sha1("".join(body))

    # Try brace-delimited (JS/TS/Go/Rust/C/C++): match def line then
    # balance braces.
    m = re.search(
        rf"(?:function|func|fn|class)\s+{re.escape(symbol_name)}\b",
        text)
    if m:
        start = m.start()
        depth = 0
        in_body = False
        end = start
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1; in_body = True
            elif ch == "}":
                depth -= 1
                if in_body and depth == 0:
                    end = i + 1; break
        if end > start:
            return _sha1(text[start:end])

    # Fallback: hash the entire file.
    return _file_content_hash(repo_root, file_rel)


def _callers_hash(store, file_rel: str, symbol_name: str) -> Optional[str]:
    """Hash the sorted set of (caller_file) for a given symbol.
    Uses store.refs_by_name as the cheapest call-site proxy; the
    hash only needs to change when the *set* of callers changes."""
    if not symbol_name:
        return None
    try:
        rows = store.refs_by_name(symbol_name)
    except Exception:
        return None
    files = sorted({r["file"] for r in rows if r["file"]})
    return _sha1("\n".join(files))


def _contracts_hash(store, file_rel: Optional[str],
                    symbol_name: Optional[str]) -> Optional[str]:
    """Hash the contracts (flags/env/schema/event/entrypoint) that
    touch the file or symbol. This is the knob SPEC #14 wants — when
    a flag flips, every note referencing the file should go stale."""
    entries: List[str] = []
    try:
        if file_rel:
            for r in store.contracts_in_file(file_rel) or ():
                entries.append(
                    f"{r['kind']}|{r['name']}|{r['file']}|"
                    f"{r['line']}|{r['role']}")
        if symbol_name:
            for r in store.contracts_by_name(symbol_name) or ():
                entries.append(
                    f"{r['kind']}|{r['name']}|{r['file']}|"
                    f"{r['line']}|{r['role']}")
    except Exception:
        pass
    if not entries:
        return None
    return _sha1("\n".join(sorted(set(entries))))


def compute_fingerprint(store, repo_root: str, target: str
                        ) -> Fingerprint:
    """SPEC #14 — compute the current fingerprint for ``target``.

    Fields left ``None`` when not applicable (e.g. symbol_hash is
    None if target is a file with no symbol component).
    """
    file_rel, symbol_name = _parse_target(target)
    fp = Fingerprint()
    if file_rel:
        fp.file_hash = _file_content_hash(repo_root, file_rel)
        if symbol_name:
            fp.symbol_hash = _symbol_body_hash(
                repo_root, file_rel, symbol_name)
        fp.callers_hash   = _callers_hash(store, file_rel, symbol_name or "")
        fp.contracts_hash = _contracts_hash(store, file_rel, symbol_name)
    else:
        # Symbol-id targets (no file prefix): best-effort.
        fp.contracts_hash = _contracts_hash(store, None, None)
    return fp


# ---------------------------------------------------------------------------
# Revalidation
# ---------------------------------------------------------------------------


@dataclass
class RevalidationResult:
    ann_id:          int
    previous:        str           # previous staleness label
    now:             str           # new staleness label
    previous_fp:     Fingerprint   # fingerprint stored on the note
    current_fp:      Fingerprint   # fingerprint computed now
    drifted_fields:  List[str] = field(default_factory=list)
    new_confidence:  float = 0.0
    # Claim-level verification output (when the note carries structured
    # claims in its evidence array). Empty list / None when no claims.
    claim_verdicts:  List[Dict[str, Any]] = field(default_factory=list)
    claim_overall_status: Optional[str] = None
    # Body-text consistency: identifiers / paths / file:line citations the
    # author embedded in the note's prose, checked against the live index.
    # When the body cites a vanished symbol or file, the note can no longer
    # be trusted as-written even if the structural fingerprint says fresh.
    # Audit fix #10 (notes "fresh" while body text is stale).
    body_consistency: Dict[str, Any] = field(default_factory=dict)


def _drifted_fields(prev: Fingerprint, now: Fingerprint) -> List[str]:
    out: List[str] = []
    for attr in ("file_hash", "symbol_hash",
                 "callers_hash", "contracts_hash"):
        a, b = getattr(prev, attr), getattr(now, attr)
        # Count drift when the note had a baseline value and the current
        # computed value is missing OR differs. This prevents a deleted file
        # from incorrectly appearing "fresh" just because the new hash is None.
        if a is not None and (b is None or a != b):
            out.append(attr)
    return out


def classify_staleness(drifted: List[str], *,
                       contradicted: bool = False) -> str:
    if contradicted:
        return CONTRADICTED
    if not drifted:
        return FRESH
    if len(drifted) == 1:
        return WEAKLY_STALE
    return STRONGLY_STALE


# Body-citation patterns. Each pattern's first capture group yields the
# concrete reference we'll check against the live index. Audit fix #10.
# Matches anything inside backticks; secondary filtering (below) decides
# whether the captured token is identifier-shaped vs. a header / env name
# / kebab-case prose token, which would never appear in the symbols table
# and was producing false positives on the OpsCanvas benchmark
# (`x-opscanvas-signature`, `OPS_CANVAS_FF_<KEY>`).
_BODY_BACKTICK_IDENT_RX = re.compile(r"`([^`\n]{2,120})`")
_BODY_FILE_LINE_RX = re.compile(
    r"\b([\w./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|"
    r"h|hpp|hh|hxx|java|rb|kt|swift|php|scala|prisma|sql|json)):"
    r"(\d+)\b")
# Path regex requires at least one '/' so that prose like "Next.js" or
# "FooBar.ts" (a class name discussed without context) doesn't match. A
# real file citation almost always carries a directory component or a
# leading `./` / `../`.
_BODY_PATH_RX = re.compile(
    r"\b((?:\.{1,2}/)?[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)+"
    r"\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|"
    r"h|hpp|hh|hxx|java|rb|kt|swift|php|scala|prisma|sql))(?![:\w])")

# A backticked token is a candidate "missing identifier" only when it
# looks like a real JS/TS/Python identifier. The rules:
#   - matches /^[A-Za-z_$][\w$]*$/
#   - NOT a kebab-style header / hyphenated string (`x-opscanvas-signature`,
#     `content-type`)
#   - NOT an ALL_UPPER env-style template (`OPS_CANVAS_FF_KEY`,
#     `WEBHOOK_DELIVER`) — these are conventionally string constants /
#     env names that authors mention in prose; they live in contracts,
#     not symbols.
#   - NOT a one-or-two char token (too many false matches in prose)
_PURE_IDENT_RX = re.compile(r"^[A-Za-z_$][\w$]{2,79}$")
_ALL_UPPER_RX = re.compile(r"^[A-Z][A-Z0-9_]+$")


def _is_real_identifier_citation(token: str) -> bool:
    """True iff `token` (already stripped of backticks) is shaped like a
    real JS/TS identifier we can sensibly look up in the symbols table.
    Filters out headers, env-name templates, and prose tokens."""
    if not _PURE_IDENT_RX.match(token):
        return False
    if _ALL_UPPER_RX.match(token):
        # ALL_UPPER tokens are almost always env vars / constants /
        # message strings, never function/class names you'd grep for in
        # the symbols table.
        return False
    return True


def verify_note_body(store, body: str, repo_root: str,
                      *, max_checks: int = 50) -> Dict[str, Any]:
    """Scan a note's prose for concrete code citations and verify each
    against the live index.

    Three citation classes are checked:
      * Backtick-quoted identifiers (`functionName`): looked up in the
        symbols table by name; reported missing if no def exists.
      * Path references (`src/foo.ts`): looked up in the files table;
        reported missing if not indexed AND not present on disk.
      * `file:line` citations (`src/foo.ts:42`): the line is checked
        against the file's current line count; line drift past EOF is
        flagged.

    Returns a dict shaped for downstream consumption — `is_stale=True`
    when ANY missing citation is found. Bounded by `max_checks` so
    pathological notes don't dominate revalidation cost.
    """
    out: Dict[str, Any] = {
        "scanned": 0,
        "missing_identifiers": [],
        "missing_paths": [],
        "line_drift": [],
        "is_stale": False,
    }
    if not body:
        return out

    # 1. Backtick identifiers. Only check tokens that look like real JS/TS
    # identifiers (not kebab headers, not ALL_UPPER env names, not prose).
    seen_idents: set = set()
    checks = 0
    for m in _BODY_BACKTICK_IDENT_RX.finditer(body):
        if checks >= max_checks:
            break
        nm = m.group(1)
        if not _is_real_identifier_citation(nm):
            continue
        if nm in seen_idents:
            continue
        seen_idents.add(nm)
        checks += 1
        out["scanned"] += 1
        try:
            rows = store.symbols_by_name(nm)
        except Exception:
            rows = []
        if not rows:
            out["missing_identifiers"].append(nm)

    # 2. Path references (no `:line` suffix). A hit can be:
    #   (a) exact match in the files table
    #   (b) suffix-match in the files table — e.g. body says
    #       `worker/handlers/foo.ts` and the file is actually at
    #       `src/worker/handlers/foo.ts`. Common when authors drop the
    #       `src/` prefix in prose.
    #   (c) exists on disk under repo_root.
    # Only when none of these apply do we flag missing_paths. Without
    # the suffix-match, normal prose like "see worker/handlers/foo.ts"
    # produced false positives on /tmp/projectX.
    seen_paths: set = set()
    for m in _BODY_PATH_RX.finditer(body):
        if checks >= max_checks:
            break
        p = m.group(1).lstrip("./")
        if p in seen_paths:
            continue
        seen_paths.add(p)
        checks += 1
        out["scanned"] += 1
        indexed = bool(store.conn.execute(
            "SELECT 1 FROM files WHERE path=? LIMIT 1", (p,)).fetchone())
        if indexed:
            continue
        # Suffix match — accept any indexed file ending in `/<p>`.
        suffix_hit = bool(store.conn.execute(
            "SELECT 1 FROM files WHERE path LIKE ? LIMIT 1",
            (f"%/{p}",)).fetchone())
        if suffix_hit:
            continue
        on_disk = os.path.isfile(os.path.join(repo_root, p))
        if not on_disk:
            out["missing_paths"].append(p)

    # 3. file:line citations — check that the cited line still exists.
    seen_pairs: set = set()
    for m in _BODY_FILE_LINE_RX.finditer(body):
        if checks >= max_checks:
            break
        path, line_s = m.group(1).lstrip("./"), m.group(2)
        try:
            line = int(line_s)
        except ValueError:
            continue
        key = (path, line)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        checks += 1
        out["scanned"] += 1
        abs_path = os.path.join(repo_root, path)
        if not os.path.isfile(abs_path):
            out["missing_paths"].append(path)
            continue
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                line_count = sum(1 for _ in fh)
        except OSError:
            continue
        if line > line_count:
            out["line_drift"].append({
                "file": path, "cited_line": line,
                "current_line_count": line_count,
            })

    out["is_stale"] = bool(out["missing_identifiers"]
                            or out["missing_paths"]
                            or out["line_drift"])
    return out


def revalidate_annotation(store, repo_root: str,
                          ann_row: Dict[str, Any],
                          *, persist: bool = True
                          ) -> RevalidationResult:
    """Recompute fingerprint for the note's target and update
    staleness + decayed confidence. If ``persist`` is True, writes
    the new state back to the store.
    """
    target = ann_row["target"]
    # Sticky manual refutation. `refute add --note-id N` writes a
    # `_manual_refute` marker into note N's evidence so subsequent
    # revalidations can see it. Without this short-circuit, the
    # revalidator would recompute against the underlying claim and
    # erase the user's manual contradiction the moment any read
    # command (`notes`, `session`) ran.
    _ev_raw = _safe_json(ann_row.get("evidence")) or []
    if isinstance(_ev_raw, list):
        for _it in _ev_raw:
            if isinstance(_it, dict) and _it.get("_manual_refute"):
                # Build a minimal RevalidationResult that carries the
                # contradicted state forward without crossing the rest
                # of the function (we don't want claim verification to
                # un-flip the staleness).
                _refuted_by = _it.get("refuted_by_note_id")
                cur_fp_short = compute_fingerprint(store, repo_root, target)
                base_conf = ann_row.get("confidence_base")
                if base_conf is None:
                    base_conf = ann_row.get("confidence")
                base_conf = float(base_conf or 0.5)
                new_conf = base_conf * CONFIDENCE_DECAY.get(CONTRADICTED, 0.1)
                result = RevalidationResult(
                    ann_id=int(ann_row["id"]),
                    previous=ann_row.get("staleness") or UNKNOWN,
                    now=CONTRADICTED,
                    previous_fp=Fingerprint.from_dict(
                        _safe_json(ann_row.get("fingerprint")) or {}),
                    current_fp=cur_fp_short,
                    drifted_fields=["_manual_refute"],
                    new_confidence=round(new_conf, 4),
                    claim_verdicts=[],
                    claim_overall_status=CONTRADICTED,
                    body_consistency={
                        "is_stale":            True,
                        "manually_refuted":    True,
                        "refuted_by_note_id":  _refuted_by,
                    })
                if persist:
                    store.update_annotation_integrity(
                        result.ann_id,
                        staleness=CONTRADICTED,
                        confidence=new_conf,
                        last_verified_at=time.time())
                return result
    prev_fp = Fingerprint.from_dict(
        _safe_json(ann_row.get("fingerprint")) or {})
    cur_fp = compute_fingerprint(store, repo_root, target)
    prev_label = ann_row.get("staleness") or UNKNOWN

    drifted = _drifted_fields(prev_fp, cur_fp)
    new_label = classify_staleness(drifted)

    # If the previous state was "unknown" (no fingerprint stored),
    # treat the first revalidation as fresh ONLY if we could compute
    # every component; otherwise keep it as UNKNOWN so consumers
    # know they haven't verified.
    baseline_empty = all(
        getattr(prev_fp, a) is None for a in
        ("file_hash", "symbol_hash", "callers_hash", "contracts_hash"))
    if prev_label == UNKNOWN and baseline_empty:
        new_label = FRESH if any(
            v for v in cur_fp.to_dict().values()) else UNKNOWN

    # Recovery: when a note was previously stale and the current fingerprint
    # matches its baseline again, label as RECOVERED (distinct from "fresh"
    # so audit workflows can see the transition explicitly).
    if new_label == FRESH and prev_label in (WEAKLY_STALE, STRONGLY_STALE) \
            and not baseline_empty:
        new_label = RECOVERED

    # Confidence is derived from the asserted baseline confidence, not from
    # compounding prior decays. This prevents repeated `note verify` calls
    # from ratcheting confidence toward 0 even when the underlying staleness
    # classification is stable.
    base_conf = ann_row.get("confidence_base")
    if base_conf is None:
        base_conf = ann_row.get("confidence")
    base_conf = float(base_conf or 0.5)
    new_conf = base_conf * CONFIDENCE_DECAY[new_label]
    new_conf = max(0.0, min(1.0, new_conf))

    # Claim-level verification. When the note carries structured claims in
    # its evidence array, each claim is verified independently against the
    # current index. The aggregate status OVERRIDES the fingerprint-based
    # classification because it's more specific: a note whose concrete
    # claims still hold shouldn't be marked stale just because the file
    # hash changed for unrelated reasons, and a note whose claims are
    # REFUTED deserves strongly_stale regardless of file-level drift.
    claim_verdicts: List[Dict[str, Any]] = []
    claim_overall: Optional[str] = None
    try:
        from . import claims as _claims
        claim_report = _claims.verify_note(store, ann_row)
        claim_verdicts = claim_report.get("claims") or []
        claim_overall = claim_report.get("overall_status")
        # Only override when claims exist AND yielded a defined status.
        if claim_verdicts and claim_overall is not None:
            new_label = claim_overall
            # Keep confidence decay coherent with the new label — reuse
            # CONFIDENCE_DECAY so pack ordering stays stable.
            new_conf = base_conf * CONFIDENCE_DECAY.get(new_label,
                                                         CONFIDENCE_DECAY[UNKNOWN])
            new_conf = max(0.0, min(1.0, new_conf))
    except Exception:
        # Claim verification must never break revalidation. Fall back to
        # fingerprint-only behavior.
        claim_verdicts = []
        claim_overall = None

    # Body-text consistency check (Audit fix #10). If the note's prose
    # cites a vanished symbol, missing file, or line-drifted citation, the
    # note is no longer trustworthy as written even when fingerprint /
    # claim checks say fresh. Downgrade FRESH/RECOVERED to WEAKLY_STALE in
    # that case and surface the missing references for the caller.
    body_consistency: Dict[str, Any] = {}
    try:
        body_consistency = verify_note_body(
            store, ann_row.get("body") or "", repo_root)
    except Exception:
        body_consistency = {"is_stale": False, "scanned": 0,
                             "missing_identifiers": [],
                             "missing_paths": [], "line_drift": []}
    if body_consistency.get("is_stale") and new_label in (FRESH, RECOVERED):
        new_label = WEAKLY_STALE
        new_conf = base_conf * CONFIDENCE_DECAY[WEAKLY_STALE]
        new_conf = max(0.0, min(1.0, new_conf))

    result = RevalidationResult(
        ann_id=int(ann_row["id"]),
        previous=prev_label, now=new_label,
        previous_fp=prev_fp, current_fp=cur_fp,
        drifted_fields=drifted,
        new_confidence=round(new_conf, 4),
        claim_verdicts=claim_verdicts,
        claim_overall_status=claim_overall,
        body_consistency=body_consistency)

    if persist:
        store.update_annotation_integrity(
            result.ann_id,
            # Fingerprint is the baseline state the note was created against.
            # Do NOT overwrite it on revalidation — otherwise the system cannot
            # detect recovery after a temporary drift. The one exception is
            # legacy notes that never captured a fingerprint: set the baseline
            # once when we can compute it.
            fingerprint=cur_fp.to_dict() if (baseline_empty and any(cur_fp.to_dict().values())) else None,
            staleness=new_label,
            confidence=new_conf,
            confidence_base=base_conf if ann_row.get("confidence_base") is None else None,
            last_verified_at=time.time())
    return result


def _safe_json(s: Any) -> Any:
    if s is None or s == "":
        return None
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------


# Phrases that encode a binary claim. Used by detect_contradictions to
# decide what the note asserts about the code. If the body contains
# a "safe"/"refute" phrase AND the code now clearly contradicts it,
# we flag it.
SAFE_PHRASES = {
    "safe", "no bug", "no vulnerability", "verified-safe",
    "not exploitable", "cannot be exploited", "benign",
}
UNSAFE_PHRASES = {
    "bug", "unsafe", "vulnerability", "exploit", "crash",
    "memory corruption", "injection", "insecure",
}

CONTRADICTION_MARKERS = {
    "missing_symbol":    "note references a symbol that no longer exists",
    "file_deleted":      "note references a file that was deleted",
    "kind_conflict":     "two notes with opposing verdicts on same target",
    "evidence_missing":  "evidence file or line no longer resolves",
    "tier_contradiction":"code state disagrees with note's verdict",
}


@dataclass
class Conflict:
    severity: str          # 'high' | 'medium' | 'low'
    marker:   str          # key from CONTRADICTION_MARKERS
    reason:   str
    ann_ids:  List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "marker": self.marker,
                "reason": self.reason, "ann_ids": self.ann_ids}


def _body_polarity(body: str) -> Optional[str]:
    """Classify a note body as 'safe' / 'unsafe' / None (neutral)."""
    low = (body or "").lower()
    safe = any(p in low for p in SAFE_PHRASES)
    unsafe = any(p in low for p in UNSAFE_PHRASES)
    if safe and not unsafe:
        return "safe"
    if unsafe and not safe:
        return "unsafe"
    return None


def _code_polarity(repo_root: str, target: str,
                   bug_markers: Optional[List[str]] = None) -> Optional[str]:
    """Scan the target file for textual bug markers. Heuristic — not
    a static analyzer. Markers are tokens like ``# BUG``, ``TODO: fix``,
    ``# SECURITY``, ``FIXME`` that explicitly signal unsafety."""
    file_rel, _ = _parse_target(target)
    if not file_rel:
        return None
    abs_path = os.path.join(repo_root, file_rel)
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    markers = bug_markers or [
        r"\bBUG\b", r"\bFIXME\b", r"\bSECURITY\b",
        r"\bXXX\b", r"# ?TODO:?\s*(fix|security|insecure)",
    ]
    for pat in markers:
        if re.search(pat, text):
            return "unsafe"
    return None


def detect_contradictions(store, repo_root: str, target: str
                          ) -> List[Conflict]:
    """Return every Conflict between notes on ``target`` and the
    current code state. Non-destructive — callers are free to ignore
    or surface. SPEC #4.

    Three families of conflict are emitted:
      * ``missing_symbol`` / ``file_deleted`` — the note's subject
        has disappeared.
      * ``kind_conflict`` — contradictory kinds on the same target
        (e.g. a ``verified-safe`` and a ``refute`` coexist).
      * ``tier_contradiction`` — the note asserts "safe" but the code
        now contains bug markers (and vice-versa).
    """
    conflicts: List[Conflict] = []
    file_rel, symbol_name = _parse_target(target)

    # File / symbol existence check.
    if file_rel:
        abs_path = os.path.join(repo_root, file_rel)
        if not os.path.exists(abs_path):
            # Every note on this target is contradicted.
            rows = store.list_annotations(target=target,
                                          include_expired=False)
            conflicts.append(Conflict(
                severity="high", marker="file_deleted",
                reason=CONTRADICTION_MARKERS["file_deleted"],
                ann_ids=[int(r["id"]) for r in rows]))

    # Kind-level conflict (sugar over packs.py existing detection).
    rows = store.list_annotations(target=target, include_expired=False)
    kinds = {r["kind"] for r in rows}
    conflicting_pairs = [
        ({"verified-safe", "refute"}, "high"),
        ({"verified-safe", "risk"},   "high"),
        ({"verified-safe", "documented-footgun"}, "medium"),
        ({"refute",        "todo"},   "low"),
    ]
    for pair, sev in conflicting_pairs:
        if pair.issubset(kinds):
            ids = [int(r["id"]) for r in rows if r["kind"] in pair]
            conflicts.append(Conflict(
                severity=sev, marker="kind_conflict",
                reason=(f"conflicting kinds on same target: "
                        f"{sorted(pair)}"),
                ann_ids=ids))

    # Tier contradiction — scan for bug markers. ``note_pol`` is the
    # UNION of body text polarity and kind polarity: a note whose
    # kind is ``verified-safe`` asserts "safe" even if its body
    # doesn't use the literal word, and ``refute`` / ``risk`` /
    # ``documented-footgun`` assert "unsafe".
    code_pol = _code_polarity(repo_root, target)
    _kind_polarity = {
        "verified-safe":      "safe",
        "refute":             "unsafe",
        "risk":               "unsafe",
        "documented-footgun": "unsafe",
    }
    for r in rows:
        note_pol = _body_polarity(r["body"]) or _kind_polarity.get(r["kind"])
        if note_pol and code_pol and note_pol != code_pol:
            conflicts.append(Conflict(
                severity="high", marker="tier_contradiction",
                reason=(f"note (kind={r['kind']!r}) asserts {note_pol!r} "
                        f"but code shows {code_pol!r}-marker"),
                ann_ids=[int(r["id"])]))

    return conflicts


# ---------------------------------------------------------------------------
# Ambiguity detection (SPEC #10)
# ---------------------------------------------------------------------------


@dataclass
class Ambiguity:
    kind:         str              # 'same_name' | 'unresolved_import' | 'multiple_def'
    severity:     str              # 'high' | 'medium' | 'low'
    symbol:       Optional[str] = None
    files:        List[str] = field(default_factory=list)
    reason:       str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity,
                "symbol": self.symbol, "files": self.files,
                "reason": self.reason}


def ambiguity_for_target(store, target: str) -> List[Ambiguity]:
    """Enumerate retrieval-ambiguity signals for a target. The
    dominant one for SPEC is same-name symbols across files: when
    multiple files define ``step``, a consumer needs to know it's
    ambiguous before trusting a trace.
    """
    out: List[Ambiguity] = []
    file_rel, symbol_name = _parse_target(target)

    if symbol_name:
        try:
            rows = store.symbols_by_name(symbol_name)
        except Exception:
            rows = []
        files = sorted({r["file"] for r in rows if r["file"]})
        if len(files) > 1:
            out.append(Ambiguity(
                kind="same_name",
                severity="high" if len(files) >= 3 else "medium",
                symbol=symbol_name,
                files=files,
                reason=(f"symbol {symbol_name!r} is defined in "
                        f"{len(files)} files; a plain grep cannot "
                        "disambiguate.")))
    return out


# ---------------------------------------------------------------------------
# Per-target integrity score (SPEC #9)
# ---------------------------------------------------------------------------


@dataclass
class IntegrityScore:
    score: float                       # 0..1
    factors: Dict[str, float] = field(default_factory=dict)
    guidance: List[str] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "score": round(self.score, 4),
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "guidance": self.guidance,
        }
        if self.extras:
            out["extras"] = self.extras
        return out


# Weights chosen so notes dominate (memory is the thesis); ambiguity,
# contradictions, and implicit-usage gaps together cap the score.
INTEGRITY_WEIGHTS = {
    "retrieval_confidence":       0.25,
    "note_freshness":             0.22,
    "contradiction_penalty":      0.17,
    "ambiguity_penalty":          0.13,
    "structural_coverage":        0.07,
    # Explicit blind-spot penalties (macro/implicit usage, native boundary,
    # module resolution, and note recovery).
    "macro_gap_penalty":          0.10,
    "binding_gap_penalty":        0.03,
    "module_resolution_penalty":  0.02,
    "stale_recovery_penalty":     0.01,
}


def integrity_score(store, repo_root: str, target: str,
                    annotations: Optional[List[Dict[str, Any]]] = None
                    ) -> IntegrityScore:
    """Compute per-target integrity (SPEC #9 / #16).

    Inputs we have directly:
      * annotations for the target (freshness, contradictions)
      * ambiguity signals from the index

    Inputs we estimate:
      * retrieval_confidence — proxy by whether the file/symbol resolves
      * structural_coverage  — proxy by whether we have >0 refs / contracts
    """
    if annotations is None:
        annotations = store.list_annotations(target=target,
                                             include_expired=False)

    # Freshness factor: mean of CONFIDENCE_DECAY for every note's
    # staleness label. If there are no notes, "neutral" 0.7 — no
    # memory means nothing to stale, but also no compounded trust.
    if annotations:
        fresh_scores = [CONFIDENCE_DECAY.get(a.get("staleness") or UNKNOWN,
                                             0.5)
                        for a in annotations]
        note_freshness = sum(fresh_scores) / len(fresh_scores)
    else:
        note_freshness = 0.7

    # Contradiction penalty: 1 − (severity-weighted count / max).
    conflicts = detect_contradictions(store, repo_root, target)
    high = sum(1 for c in conflicts if c.severity == "high")
    med = sum(1 for c in conflicts if c.severity == "medium")
    low = sum(1 for c in conflicts if c.severity == "low")
    contradiction_penalty = max(
        0.0, 1.0 - (0.6 * high + 0.3 * med + 0.1 * low))

    # Ambiguity penalty.
    ambig = ambiguity_for_target(store, target)
    amb_high = sum(1 for a in ambig if a.severity == "high")
    amb_med = sum(1 for a in ambig if a.severity == "medium")
    ambiguity_penalty = max(
        0.0, 1.0 - (0.5 * amb_high + 0.25 * amb_med))

    # Retrieval confidence (does the target resolve?).
    file_rel, symbol_name = _parse_target(target)
    retrieval = 0.5
    if file_rel and os.path.exists(os.path.join(repo_root, file_rel)):
        retrieval = 0.9 if not conflicts else 0.7
    elif symbol_name:
        try:
            if store.symbols_by_name(symbol_name):
                retrieval = 0.8
        except Exception:
            retrieval = 0.4

    # Structural coverage proxy.
    structural = 0.5
    try:
        if file_rel:
            refs = store.conn.execute(
                "SELECT COUNT(*) AS n FROM refs WHERE file=?",
                (file_rel,)).fetchone()
            if refs and refs["n"] > 0:
                structural = 0.9 if refs["n"] >= 5 else 0.7
    except Exception:
        pass

    # Implicit-usage penalty. When the target is a symbol and a cheap
    # word-boundary text scan of the index finds substantially more
    # occurrences than the structured ref count, we assume the gap is
    # real (macros, X-macros, code generation, string dispatch) and
    # penalize the score so callers don't treat structured counts as
    # exhaustive. If the target is a file or the target isn't a plain
    # identifier, the penalty defaults to 1.0 (no penalty).
    macro_gap_penalty = 1.0
    implicit_info: Optional[Dict[str, Any]] = None
    try:
        if symbol_name:
            from . import implicit as _implicit
            if _implicit.is_identifier(symbol_name):
                struct_total = store.conn.execute(
                    "SELECT COUNT(*) AS n FROM refs WHERE name=?",
                    (symbol_name,)).fetchone()["n"]
                struct_total += store.conn.execute(
                    "SELECT COUNT(*) AS n FROM symbols WHERE name=?",
                    (symbol_name,)).fetchone()["n"]
                scan = _implicit.count_text_occurrences(
                    store, repo_root, symbol_name)
                verdict = _implicit.detect_implicit_usage(
                    structured_count=struct_total,
                    text_count=scan["text_count"],
                    text_scan_truncated=scan["truncated"])
                if verdict.get("implicit_refs_detected"):
                    ratio = verdict.get("text_to_structured_ratio") or 1.0
                    # Map ratio into penalty: 1.25 → 0.85, 2.0 → 0.6,
                    # 4.0 → 0.3, >=8 → 0.1. Clamped.
                    if ratio >= 8.0:
                        macro_gap_penalty = 0.10
                    elif ratio >= 4.0:
                        macro_gap_penalty = 0.30
                    elif ratio >= 2.0:
                        macro_gap_penalty = 0.60
                    elif ratio >= 1.5:
                        macro_gap_penalty = 0.75
                    else:
                        macro_gap_penalty = 0.85
                implicit_info = verdict
    except Exception:
        macro_gap_penalty = 1.0

    # Native binding gap penalty. Applies only when we have strong hints that
    # `symbol_name` is a JS-facing function exposed from native code (e.g. the
    # caller file imports an internalBinding target), but no binding edges were
    # extracted. This avoids penalizing ordinary external symbols.
    binding_gap_penalty = 1.0
    binding_info: Optional[Dict[str, Any]] = None
    try:
        if symbol_name:
            from . import implicit as _implicit
            if _implicit.is_identifier(symbol_name):
                defs_n = store.conn.execute(
                    "SELECT COUNT(*) AS n FROM symbols WHERE name=?",
                    (symbol_name,)).fetchone()["n"]
                if defs_n == 0:
                    call_refs = list(store.conn.execute(
                        "SELECT DISTINCT file FROM refs WHERE name=? "
                        "AND kind IN ('call','new') LIMIT 200",
                        (symbol_name,)))
                    if call_refs:
                        native_hint = False
                        checked = 0
                        for r in call_refs:
                            f = r["file"]
                            if not f:
                                continue
                            checked += 1
                            for e in store.edges_from(f, type_="imports"):
                                ev = (e["evidence"] or "")
                                dst = (e["dst"] or "")
                                if ("internalBinding(" in ev
                                        or dst.startswith("binding:")
                                        or dst.endswith(".cc")):
                                    native_hint = True
                                    break
                            if native_hint or checked >= 50:
                                break
                        if native_hint:
                            binds = list(store.bindings_for_js_name(symbol_name))
                            if not binds:
                                binding_gap_penalty = 0.30
                            binding_info = {
                                "native_hint": True,
                                "binding_edges": len(binds),
                            }
    except Exception:
        binding_gap_penalty = 1.0

    # Module-resolution penalty: unresolved INTERNAL relative imports on a
    # file target degrade reverse-dep and delete-safety correctness.
    module_resolution_penalty = 1.0
    module_info: Optional[Dict[str, Any]] = None
    try:
        if file_rel:
            unresolved_internal = 0
            unresolved_samples: List[str] = []
            for e in store.edges_from(file_rel, type_="imports"):
                dst = (e["dst"] or "")
                if not (dst.startswith("module:") or dst.startswith("binding:")):
                    continue
                spec = dst.split(":", 1)[1] if ":" in dst else dst
                if spec.startswith(".") or spec.startswith("/"):
                    unresolved_internal += 1
                    if len(unresolved_samples) < 5:
                        unresolved_samples.append(spec)
            if unresolved_internal > 0:
                # Conservative penalty: each unresolved internal import is a real
                # blind spot for reverse-dep reasoning.
                module_resolution_penalty = max(0.30, 1.0 - 0.10 * unresolved_internal)
                module_info = {
                    "unresolved_internal_imports": unresolved_internal,
                    "samples": unresolved_samples,
                }
    except Exception:
        module_resolution_penalty = 1.0

    # Stale-recovery penalty: if stale notes have no baseline fingerprint,
    # projmem cannot reliably detect drift reversal/recovery.
    stale_recovery_penalty = 1.0
    recovery_info: Optional[Dict[str, Any]] = None
    try:
        missing_fp = 0
        for a in annotations or []:
            if (a.get("staleness") in (WEAKLY_STALE, STRONGLY_STALE)
                    and not _safe_json(a.get("fingerprint"))):
                missing_fp += 1
        if missing_fp > 0:
            stale_recovery_penalty = 0.70
            recovery_info = {"stale_without_fingerprint": missing_fp}
    except Exception:
        stale_recovery_penalty = 1.0

    factors = {
        "retrieval_confidence":       retrieval,
        "note_freshness":             note_freshness,
        "contradiction_penalty":      contradiction_penalty,
        "ambiguity_penalty":          ambiguity_penalty,
        "structural_coverage":        structural,
        # Backward-compat key (old name) plus new explicit key.
        "implicit_usage_penalty":     macro_gap_penalty,
        "macro_gap_penalty":          macro_gap_penalty,
        "binding_gap_penalty":        binding_gap_penalty,
        "module_resolution_penalty":  module_resolution_penalty,
        "stale_recovery_penalty":     stale_recovery_penalty,
    }
    score = sum(INTEGRITY_WEIGHTS[k] * factors[k]
                for k in INTEGRITY_WEIGHTS)

    guidance = _guidance(conflicts, ambig, annotations)
    if implicit_info and implicit_info.get("implicit_refs_detected"):
        guidance.append(
            "IMPLICIT USAGE: text search finds "
            f"{implicit_info.get('text_match_count')} matches vs "
            f"{implicit_info.get('structured_ref_count')} structured refs. "
            "Macro / string-based / generated usage may hide real sites. "
            "Verify with `rg -w <name>` before trusting ref counts.")
    if binding_gap_penalty < 1.0:
        guidance.append(
            "NATIVE BINDING GAP: symbol appears to be called from a file that "
            "imports an internalBinding surface, but no JS↔native binding "
            "edge was extracted. Cross-language resolution may be incomplete.")
    if module_resolution_penalty < 1.0:
        guidance.append(
            "MODULE RESOLUTION: one or more internal relative imports were "
            "unresolved; reverse dependencies and delete-safety may be "
            "incomplete.")
    if stale_recovery_penalty < 1.0:
        guidance.append(
            "STALE RECOVERY: one or more stale notes lack a baseline "
            "fingerprint; recovery after restore cannot be proven.")

    extras: Dict[str, Any] = {}
    if implicit_info:
        extras["implicit_usage"] = implicit_info
    if binding_info:
        extras["binding_gap"] = binding_info
    if module_info:
        extras["module_resolution"] = module_info
    if recovery_info:
        extras["stale_recovery"] = recovery_info
    return IntegrityScore(score=round(score, 4), factors=factors,
                          guidance=guidance, extras=extras)


def _guidance(conflicts: List[Conflict],
              ambig: List[Ambiguity],
              annotations: List[Dict[str, Any]]) -> List[str]:
    """SPEC #16 — emit short, actionable strings when risk is high.
    Returned inline in pack output so agents can't miss them."""
    msgs: List[str] = []
    if conflicts:
        for c in conflicts:
            if c.severity == "high":
                msgs.append(
                    f"CONTRADICTION ({c.marker}): {c.reason}. "
                    f"Revalidate note(s) before trusting.")
    for a in ambig:
        if a.severity == "high":
            msgs.append(
                f"AMBIGUITY: {a.reason} Use `projmem symbol {a.symbol} "
                "--context` to disambiguate.")
    stale = [a for a in annotations
             if (a.get("staleness") in (STRONGLY_STALE, CONTRADICTED))]
    if stale:
        ids = ",".join(str(a["id"]) for a in stale)
        msgs.append(
            f"STALE NOTES: ids=[{ids}] are stale; "
            "`projmem note verify <target>` to refresh.")
    return msgs


# ---------------------------------------------------------------------------
# Batch helpers (called by packs.py)
# ---------------------------------------------------------------------------


def revalidate_for_pack(store, repo_root: str,
                        annotations: List[Dict[str, Any]]
                        ) -> List[Dict[str, Any]]:
    """Revalidate every annotation passed in. Returns the same rows
    with ``staleness``, ``confidence``, and ``fingerprint`` updated.

    Safe to call on a read-only pack build — the update is durable
    but idempotent. If revalidation itself fails (e.g. a file read
    error), we keep the old staleness label and add a
    ``revalidation_error`` note to the row so the caller can choose
    to surface it.
    """
    out: List[Dict[str, Any]] = []
    for ann in annotations:
        try:
            r = revalidate_annotation(store, repo_root, ann, persist=True)
            ann2 = dict(ann)
            ann2["staleness"] = r.now
            ann2["confidence"] = r.new_confidence
            ann2["drifted_fields"] = r.drifted_fields
            # Propagate claim verdicts so packs.py can surface "which belief
            # became false" rather than just the coarse staleness label.
            if r.claim_verdicts:
                ann2["claim_verdicts"] = r.claim_verdicts
                ann2["claim_overall_status"] = r.claim_overall_status
                ann2["verified_count"] = sum(
                    1 for v in r.claim_verdicts if v.get("status") == "VERIFIED")
                ann2["refuted_count"] = sum(
                    1 for v in r.claim_verdicts if v.get("status") == "REFUTED")
                ann2["uncheckable_count"] = sum(
                    1 for v in r.claim_verdicts if v.get("status") == "UNCHECKABLE")
            out.append(ann2)
        except Exception as e:
            ann2 = dict(ann)
            ann2["revalidation_error"] = str(e)
            out.append(ann2)
    return out


def sort_annotations_for_pack(annotations: List[Dict[str, Any]]
                              ) -> List[Dict[str, Any]]:
    """SPEC #5 — fresh first, then unknown, then weakly_stale, then
    strongly_stale, then contradicted. Within a bucket, higher
    confidence first, then newer first."""
    def key(a):
        order = STALENESS_ORDER.get(a.get("staleness") or UNKNOWN, 99)
        conf = -float(a.get("confidence") or 0)
        ts = -float(a.get("created_at") or 0)
        return (order, conf, ts)
    return sorted(annotations, key=key)
