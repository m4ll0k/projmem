"""projmem/claims.py — claim-level verification for notes.

MemTrace's differentiator vs. grep is durable conclusions: a note survives
reindex, carries provenance, and decays when the underlying code moves. But
until this module, a stale note only told the user *some* input shifted —
not *which belief is now false*. This module closes that gap.

A **claim** is a subject-predicate-object triple about the code. Example:

    { subject:   "TSC_WATCHFILE",
      predicate: "env-read-at",
      object:    "src/compiler/sys.ts:1516",
      truth_class: "FACT",
      confidence:  0.9 }

Notes can carry claims inside their ``evidence`` JSON array alongside legacy
``{file, line, note}`` evidence entries — backward compatible by shape.

On ``note-verify`` we evaluate each claim independently against the current
index and return one of:

  VERIFIED    — current code supports the claim
  REFUTED     — current code contradicts it (with the CURRENT evidence)
  UNCHECKABLE — insufficient data to prove or refute; reason attached

Aggregate status over a note's claims:
  - any REFUTED on a FACT claim                → contradicted
  - any REFUTED                                 → strongly_stale
  - only UNCHECKABLE or mix with VERIFIED      → weakly_stale
  - all VERIFIED                                → fresh
  - claims absent                               → delegate to fingerprint path

The verifiers are deliberately *narrow*. Rather than over-fit every possible
predicate, we ship a well-tested core:

  A. Symbol identity
     - defined-at        subject has a def at file:line
     - exported-from     subject is exported from file

  B. Contracts
     - env-read-at       env var subject is read at file:line
     - flag-read-at      flag subject is read at file:line

  C. Structural consumers
     - reexported-via    subject file is re-exported via object file
     - reverse-dependency-of  subject file is imported by object file

Failure policy: if a claim's input is ambiguous (e.g. multiple defs with
the same name in the target file), the verdict is UNCHECKABLE. We never
silently pick one and claim VERIFIED.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Status taxonomy
# ---------------------------------------------------------------------------

VERIFIED    = "VERIFIED"
REFUTED     = "REFUTED"
UNCHECKABLE = "UNCHECKABLE"
# MOVED = symbol is still present in the cited file with the same name
# and kind, but at a different line. A non-semantic edit (adding an
# import, renaming a local) shouldn't contradict a defined-at claim;
# previously it flipped the note to REFUTED and then to `contradicted`
# for no real reason. MOVED aggregates as weakly_stale — visible drift,
# no blocking signal — and surfaces `moved_to: {file, line}` so agents
# (or a future auto-update pass) can refresh the claim.
MOVED       = "MOVED"

CLAIM_STATUSES = (VERIFIED, REFUTED, UNCHECKABLE, MOVED)


# Note-level staleness labels we may produce from an aggregate over claims.
# These intentionally overlap with integrity.STALENESS_ORDER so downstream
# consumers don't need to learn a second taxonomy.
FRESH          = "fresh"
WEAKLY_STALE   = "weakly_stale"
STRONGLY_STALE = "strongly_stale"
CONTRADICTED   = "contradicted"
UNKNOWN        = "unknown"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    """Structured belief about the code.

    Minimal required fields: subject, predicate, object. All three are
    string-typed for portability; object encodes a compound location as
    ``<file>:<line>`` when the predicate needs a site.
    """
    subject:     str
    predicate:   str
    object:      str
    truth_class: str = "INFERENCE"      # FACT | INFERENCE | ASSUMPTION | UNKNOWN
    confidence:  float = 0.7            # 0.0-1.0
    note:        Optional[str] = None   # free-form rationale; optional

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "subject":     self.subject,
            "predicate":   self.predicate,
            "object":      self.object,
            "truth_class": self.truth_class,
            "confidence":  self.confidence,
        }
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Claim":
        return cls(
            subject=     str(d.get("subject") or ""),
            predicate=   str(d.get("predicate") or ""),
            object=      str(d.get("object") or ""),
            truth_class= str(d.get("truth_class") or "INFERENCE"),
            confidence=  float(d.get("confidence") or 0.7),
            note=        d.get("note"),
        )


# Inline claim syntax: `@<predicate>(<subject>, <object>)` embedded in a
# note body. Lowers capture friction from "write JSON, pass --claims" to
# one line of prose. Audit: capture friction is the biggest adoption
# barrier; query-side polish can't help if nobody writes to memory.
import re as _re_cl
_INLINE_CLAIM_RX = _re_cl.compile(
    r"@([a-z][a-z0-9_\-]{1,40})\(([^)]{1,400})\)", _re_cl.IGNORECASE)


def parse_inline_claims(body: str,
                         default_truth_class: str = "FACT"
                         ) -> List["Claim"]:
    """Extract `@<predicate>(<subject>, <object>)` patterns from a note
    body into structured claims. Unknown predicates are accepted; the
    verifier returns UNCHECKABLE on them rather than crashing.

    Accepted shapes:
      @defined-at(withContext, src/server/http/withContext.ts:10)
      @exported-from(handleWebhookDeliverJob, src/worker/handlers/webhookDeliver.ts)
      @env-read-at(DATABASE_URL, src/config/env.ts:4)

    Subject and object are comma-separated (first comma splits). Both
    sides are stripped of surrounding whitespace and backticks so a body
    like "@defined-at(`foo`, path.ts:1)" works.
    """
    if not body:
        return []
    claims: List["Claim"] = []
    seen: set = set()
    for m in _INLINE_CLAIM_RX.finditer(body):
        predicate = m.group(1).lower()
        inner = m.group(2)
        # First comma splits subject from object; further commas are
        # left inside the object (tolerates `file:line` notation with
        # no comma and `a,b,c` multi-arg predicates as future work).
        if "," not in inner:
            continue
        subj, obj = inner.split(",", 1)
        subj = subj.strip().strip("`'\"")
        obj = obj.strip().strip("`'\"")
        if not subj or not obj:
            continue
        key = (subj, predicate, obj)
        if key in seen:
            continue
        seen.add(key)
        claims.append(Claim(
            subject=subj,
            predicate=predicate,
            object=obj,
            truth_class=default_truth_class,
            confidence=0.85,  # slightly-lower than explicit --claims files
        ))
    return claims


# Round-7-bench follow-up: real benchmark data (24 agent runs, 2 repos)
# showed Sonnet *never* reaches for `--claims` unprompted on `note add`
# and *rarely* writes `@predicate(...)` syntax in note bodies. It writes
# prose: "setupmethod is defined at src/flask/sansio/scaffold.py:42".
# Without claims, projmem's verifier is dead weight — the very feature
# that differentiates projmem from a markdown scratchpad never engages.
#
# `auto_extract_claims` widens the extractor to cover both the
# `@predicate(...)` form AND natural-language patterns the fact-check
# module already recognises ("X is defined at file:line", bare
# `file:line` mentions, "X is exported from file"). Same shapes that
# fact-check verifies on the read side, now extracted on the write side
# — so a fresh `note add` ends up with structured FACT claims even when
# the agent only wrote prose.


def auto_extract_claims(body: str,
                          default_truth_class: str = "FACT"
                          ) -> List["Claim"]:
    """Extract structured claims from a note body using BOTH the
    inline `@predicate(subject, object)` syntax AND natural-language
    patterns. Returns deduped Claim objects.

    Recognised forms:
      - `@defined-at(foo, src/a.ts:10)`            (inline)
      - "`foo` is defined at src/a.ts:10"          (NL)
      - "`foo` is exported from src/a.ts"          (NL)
      - "src/a.ts:10"                              (bare file:line)

    The NL forms require backticked symbol names — that's strict on
    purpose. Loosening the requirement would invent claims out of
    casual prose mentions and pollute memory faster than it would
    help. Agents that want their findings captured wrap names in
    backticks (a markdown reflex Sonnet has by default).

    `default_truth_class` is applied to claims that don't carry one
    of their own. Pass "FACT" when the caller is asserting ground
    truth (the typical `note add` case); the verifier flips to
    `contradicted` when a FACT is REFUTED.
    """
    if not body:
        return []
    # Lazy-import to avoid a fact-check ↔ claims circular import.
    from . import factcheck as _fc
    out: List["Claim"] = []
    seen: set = set()

    def _add(c: "Claim") -> None:
        # Force the caller's default_truth_class onto claims that
        # didn't specify one (parse_inline_claims defaults to FACT
        # already, but the NL extractors hard-code "FACT" too).
        if not c.truth_class:
            c.truth_class = default_truth_class
        key = (c.subject, c.predicate, c.object)
        if key in seen:
            return
        seen.add(key)
        out.append(c)

    # 1. Inline `@predicate(...)` — strongest signal.
    for c in parse_inline_claims(body, default_truth_class):
        _add(c)
    # 2. NL "X is defined at file:line".
    for c in _fc._extract_nl_defined_at(body):
        _add(c)
    # 3. NL "X is exported from file".
    for c in _fc._extract_nl_exported_from(body):
        _add(c)
    return out


@dataclass
class ClaimVerdict:
    """Result of verifying a single claim against the current index."""
    claim:              Claim
    status:             str
    reason:             Optional[str] = None
    current_evidence:   List[Dict[str, Any]] = field(default_factory=list)
    # Note-quality signals: even a VERIFIED claim may be structurally
    # weak (based on a literal pattern when computed access is in play,
    # based on a single site when schema-declared elsewhere). Downstream
    # consumers surface these as warnings without flipping the verdict.
    quality_warnings:   List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            **self.claim.to_dict(),
            "status": self.status,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.current_evidence:
            out["current_evidence"] = self.current_evidence
        if self.quality_warnings:
            out["quality_warnings"] = self.quality_warnings
        return out


# ---------------------------------------------------------------------------
# Claim detection from evidence field
# ---------------------------------------------------------------------------

def is_claim(entry: Any) -> bool:
    """A dict is a claim iff it carries subject + predicate + object.
    Legacy evidence entries (``{file, line, note}``) lack those keys and
    are left alone."""
    if not isinstance(entry, dict):
        return False
    return all(k in entry and entry[k] for k in ("subject", "predicate", "object"))


def parse_claims(evidence: Any) -> List[Claim]:
    """Extract structured claims from an annotation's evidence array.

    Accepts the list as-is, a JSON string, or None. Non-claim entries are
    skipped silently; they remain valid evidence under the old schema.
    """
    if evidence is None:
        return []
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except (TypeError, ValueError):
            return []
    if not isinstance(evidence, list):
        return []
    out: List[Claim] = []
    for e in evidence:
        if is_claim(e):
            try:
                out.append(Claim.from_dict(e))
            except (TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------------------
# Object parsing helpers
# ---------------------------------------------------------------------------

def _parse_location(obj: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse an object of the form ``<file>:<line>`` → (file, line).
    Missing line returns (file, None). Returns (None, None) when empty."""
    if not obj:
        return None, None
    if ":" in obj:
        file_part, _, line_part = obj.rpartition(":")
        # rpartition keeps file_part empty if there's no ':' — guard that.
        if file_part == "":
            return obj, None
        try:
            return file_part, int(line_part)
        except ValueError:
            # Object was `foo:bar` where `bar` isn't a line — treat whole thing
            # as a file path. Real files with colons are rare.
            return obj, None
    return obj, None


def _resolve_cited_file(store, cited: str
                         ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Map a user-cited file path to an indexed path.

    Returns (resolved_path, ambiguity_info). If ``resolved_path`` is not
    None, use it for lookups. If ``ambiguity_info`` is not None, the
    caller should return UNCHECKABLE with that dict as evidence — the
    cite matched multiple indexed files and silent picking would destroy
    signal. Both None means the path doesn't exist in the index at all;
    the caller decides whether that's REFUTED or UNCHECKABLE.

    Resolution order:
      1. Exact match in ``files.path`` — use as-is.
      2. Suffix match on ``path LIKE '%/<cited>'`` — if exactly one, use
         it. If >1, return ambiguity info. If 0, fall through.

    This lets agents cite ``env.cc:937`` and have it resolve to
    ``src/env.cc:937`` when the suffix match is unambiguous. Previously
    the stricter "exact file-rel match" path produced REFUTED for any
    abbreviated path, conflating "wrong claim" with "inconvenient
    shorthand".
    """
    if not cited:
        return None, None
    try:
        row = store.conn.execute(
            "SELECT path FROM files WHERE path=? LIMIT 1",
            (cited,)).fetchone()
    except Exception:
        return None, None
    if row:
        return cited, None
    try:
        candidates = list(store.conn.execute(
            "SELECT path FROM files WHERE path LIKE ? LIMIT 5",
            (f"%/{cited}",)))
    except Exception:
        candidates = []
    if len(candidates) == 1:
        return candidates[0]["path"], None
    if len(candidates) > 1:
        return None, {
            "reason": ("ambiguous path — citation matches multiple "
                       "indexed files"),
            "cited_path": cited,
            "candidates": [c["path"] for c in candidates],
            "hint": ("use a repo-root-relative path, e.g. "
                     f"`{candidates[0]['path']}`"),
        }
    return None, None


# ---------------------------------------------------------------------------
# Per-predicate verifiers
# ---------------------------------------------------------------------------
#
# Each verifier takes (store, claim) and returns a ClaimVerdict. Verifiers
# DO NOT mutate the store. They query the existing index only.

def _verify_defined_at(store, claim: Claim) -> ClaimVerdict:
    """VERIFIED if symbol <subject> has a def at <object>=file:line.
    REFUTED if the symbol exists in the index but at different file:line,
    OR if the cited file no longer exists on disk (orphan-note case).
    UNCHECKABLE if the symbol is ambiguous or unknown.

    Benchmark v4 Bug 2 fix: previously, a stale symbols-table entry
    for a deleted file produced VERIFIED. The verifier now cross-checks
    that the cited file actually exists on disk; if missing, returns
    REFUTED with a "target file deleted" reason. The repo_root is
    discovered via store.get_meta('root') so callers don't need to
    pass it through.
    """
    file_rel, line = _parse_location(claim.object)
    if not file_rel or line is None:
        return ClaimVerdict(
            claim, UNCHECKABLE,
            reason=f"object must be 'file:line', got {claim.object!r}")

    # Normalize cited path: let `env.cc:937` resolve to `src/env.cc:937`
    # when the suffix is unambiguous. Without this, abbreviated paths
    # silently REFUTE correct claims and agents can't tell whether the
    # belief is wrong or the citation was just short.
    resolved, ambiguity = _resolve_cited_file(store, file_rel)
    if ambiguity is not None:
        return ClaimVerdict(claim, UNCHECKABLE,
                             reason=ambiguity["reason"],
                             current_evidence=[ambiguity])
    cited_path = file_rel
    if resolved is not None:
        file_rel = resolved

    # Disk-existence guard. If we know the repo root and the cited
    # file isn't on disk, the index is lying — the symbol row is
    # stale. Don't return VERIFIED based on stale evidence.
    import os as _os
    repo_root = ""
    try:
        repo_root = store.get_meta("root") or ""
    except Exception:
        repo_root = ""
    if repo_root and file_rel:
        full = _os.path.join(repo_root, file_rel)
        if not _os.path.exists(full):
            return ClaimVerdict(
                claim, REFUTED,
                reason=(f"target file {file_rel!r} no longer exists on "
                        "disk; index is stale (orphan claim)"),
                current_evidence=[{
                    "file": file_rel, "line": line,
                    "file_missing_on_disk": True,
                }])

    rows = list(store.conn.execute(
        "SELECT file, line, kind, symbol_id, exported FROM symbols "
        "WHERE name=?", (claim.subject,)))
    if not rows:
        return ClaimVerdict(
            claim, REFUTED,
            reason=f"no symbol named {claim.subject!r} in the index",
            current_evidence=[])
    exact = [r for r in rows if r["file"] == file_rel and int(r["line"]) == line]
    if exact:
        ev: List[Dict[str, Any]] = [{
            "file": r["file"], "line": int(r["line"]),
            "kind": r["kind"], "symbol_id": r["symbol_id"],
        } for r in exact]
        if resolved is not None and cited_path != resolved:
            ev[0]["resolved_from_suffix"] = True
            ev[0]["cited_path"] = cited_path
        return ClaimVerdict(claim, VERIFIED, current_evidence=ev)
    # Symbol exists in the cited file but at a DIFFERENT line — a
    # non-semantic edit (adding an import above, reshuffling methods)
    # shifted it. This is MOVED, not REFUTED. Agents/consumers treat
    # MOVED as "warning, update the claim" rather than "contradicted".
    same_file = [r for r in rows if r["file"] == file_rel]
    if same_file:
        primary = same_file[0]
        return ClaimVerdict(
            claim, MOVED,
            reason=(f"symbol {claim.subject!r} is still in "
                    f"{file_rel} but now at line "
                    f"{int(primary['line'])} (claim said "
                    f"{line}). Non-semantic drift."),
            current_evidence=[{
                "file":         r["file"],
                "line":         int(r["line"]),
                "kind":         r["kind"],
                "symbol_id":    r["symbol_id"],
                "moved_from":   line,
                "moved_to":     int(r["line"]),
            } for r in same_file[:10]])
    # Symbol exists but in a DIFFERENT file entirely — that's REFUTED.
    # The caller cited the wrong location; line drift isn't the cause.
    return ClaimVerdict(
        claim, REFUTED,
        reason=(f"symbol {claim.subject!r} is not in {file_rel} "
                f"(currently defined elsewhere)"),
        current_evidence=[{
            "file": r["file"], "line": int(r["line"]),
            "kind": r["kind"], "symbol_id": r["symbol_id"],
        } for r in rows[:10]])


def _verify_exported_from(store, claim: Claim) -> ClaimVerdict:
    """VERIFIED if <subject> is exported from <object>=file.
    REFUTED if the symbol exists in the file but not exported, or not
    in the file.

    Benchmark v2 fix: also accept enum-member names. `TASK_ASSIGNED`
    isn't a symbol of its own — it's a member of the `NotificationType`
    enum_shape contract. When the direct symbol lookup misses, look up
    the `enum_shape` contracts in `object_file` and check whether the
    subject appears in any enum's members (the `context` column stores
    `ts:A,B,C` / `prisma:A,B` / etc). Without this, modern TypeScript
    codebases that use `as const` arrays (OpsCanvas pattern) see every
    enum-member claim get silently REFUTED at verify time.
    """
    file_rel = claim.object
    if not file_rel:
        return ClaimVerdict(claim, UNCHECKABLE,
                            reason="object must be a file path")
    resolved, ambiguity = _resolve_cited_file(store, file_rel)
    if ambiguity is not None:
        return ClaimVerdict(claim, UNCHECKABLE,
                             reason=ambiguity["reason"],
                             current_evidence=[ambiguity])
    if resolved is not None:
        file_rel = resolved
    rows = list(store.conn.execute(
        "SELECT file, line, exported FROM symbols "
        "WHERE name=? AND file=?", (claim.subject, file_rel)))
    if rows:
        exported = [r for r in rows if r["exported"]]
        if exported:
            return ClaimVerdict(
                claim, VERIFIED,
                current_evidence=[{"file": r["file"], "line": int(r["line"]),
                                    "exported": bool(r["exported"])}
                                  for r in exported])
        return ClaimVerdict(
            claim, REFUTED,
            reason=(f"symbol {claim.subject!r} present in {file_rel!r} "
                    "but not exported"),
            current_evidence=[{"file": r["file"], "line": int(r["line"]),
                                "exported": bool(r["exported"])}
                              for r in rows])
    # Enum-member fallback: scan enum_shape contracts in the file.
    try:
        enum_rows = list(store.conn.execute(
            "SELECT name, line, context FROM contracts "
            "WHERE kind='enum_shape' AND file=?", (file_rel,)))
    except Exception:
        enum_rows = []
    for er in enum_rows:
        ctx = er["context"] or ""
        # Context format: `<layer>:m1,m2,m3`.
        _, _, members_str = ctx.partition(":")
        members = {m for m in members_str.split(",") if m}
        if claim.subject in members:
            return ClaimVerdict(
                claim, VERIFIED,
                current_evidence=[{
                    "file":       file_rel,
                    "line":       int(er["line"] or 0),
                    "enum_name":  er["name"],
                    "as_member":  True,
                }])
    return ClaimVerdict(
        claim, REFUTED,
        reason=(f"symbol {claim.subject!r} not found in {file_rel!r} "
                "(and not a member of any enum_shape there)"),
        current_evidence=[])


def _detect_quality_warnings(store, kind: str, file_rel: str,
                              subject: str) -> List[Dict[str, Any]]:
    """Detect note-quality issues a VERIFIED claim can still have.

    - computed_access_in_file: same file has `process.env[variable]`
      style contracts (role='dynamic-access'). A literal claim at one
      site may miss runtime-determined env names.
    - schema_declared_elsewhere: same env name is also declared via a
      schema library (role='declare') in another file. Literal-read
      claim is correct but misses the schema DECLARATION that defines
      the canonical shape.
    """
    warnings: List[Dict[str, Any]] = []
    try:
        computed = list(store.conn.execute(
            "SELECT file, line FROM contracts "
            "WHERE kind=? AND file=? AND role='dynamic-access' "
            "LIMIT 5",
            (kind, file_rel)))
        if computed:
            warnings.append({
                "code": "computed_access_in_file",
                "severity": "warning",
                "message": (
                    f"{file_rel} contains {len(computed)} computed "
                    f"{kind}-access site(s). Your literal claim at "
                    f"one site may not cover every runtime {kind} "
                    "name read here."),
                "sites": [{"file": r["file"], "line": int(r["line"])}
                           for r in computed[:5]],
            })
    except Exception:
        pass
    try:
        declared = list(store.conn.execute(
            "SELECT file, line, context FROM contracts "
            "WHERE kind=? AND name=? AND role='declare' LIMIT 5",
            (kind, subject)))
        if declared:
            first_ctx = declared[0]["context"] or "schema"
            warnings.append({
                "code": "schema_declared_elsewhere",
                "severity": "info",
                "message": (
                    f"{kind} {subject!r} is also DECLARED in a schema "
                    f"({first_ctx}). The canonical shape lives in that "
                    "schema; your literal read claim captures one use site."),
                "declarations": [{"file": r["file"],
                                   "line": int(r["line"]),
                                   "context": r["context"]}
                                  for r in declared[:5]],
            })
    except Exception:
        pass
    return warnings


def _verify_contract_read_at(store, claim: Claim, kind: str) -> ClaimVerdict:
    """Shared verifier for env-read-at / flag-read-at / similar.

    VERIFIED if contracts table has a row with matching (kind, name, file, line, role='read').
    REFUTED when the same location has a DIFFERENT contract name (rename case),
    or when the name exists elsewhere but not at the claimed site.
    """
    file_rel, line = _parse_location(claim.object)
    if not file_rel or line is None:
        return ClaimVerdict(
            claim, UNCHECKABLE,
            reason=f"object must be 'file:line', got {claim.object!r}")

    resolved, ambiguity = _resolve_cited_file(store, file_rel)
    if ambiguity is not None:
        return ClaimVerdict(claim, UNCHECKABLE,
                             reason=ambiguity["reason"],
                             current_evidence=[ambiguity])
    if resolved is not None:
        file_rel = resolved

    # Pre-compute note-quality signals that apply regardless of verdict.
    # We look for COMPUTED-ACCESS contracts in the same file — they
    # indicate the literal claim is potentially incomplete (some
    # runtime env name the claim doesn't cover may also be read).
    quality = _detect_quality_warnings(store, kind, file_rel, claim.subject)

    # Exact match query — same kind + name + file + line.
    exact_rows = list(store.conn.execute(
        "SELECT name, file, line, role, kind FROM contracts "
        "WHERE kind=? AND name=? AND file=? AND line=?",
        (kind, claim.subject, file_rel, line)))
    if exact_rows:
        return ClaimVerdict(
            claim, VERIFIED,
            current_evidence=[{
                "file": r["file"], "line": int(r["line"]),
                "kind": r["kind"], "name": r["name"], "role": r["role"],
            } for r in exact_rows],
            quality_warnings=quality)

    # The site may now carry a DIFFERENT contract name (classic rename case
    # — `TSC_WATCHFILE` → `TSC_WATCH_FILE`). Report that as the refutation
    # evidence so the reader sees "the read is still there but it's a
    # different name now".
    site_rows = list(store.conn.execute(
        "SELECT name, file, line, role, kind FROM contracts "
        "WHERE kind=? AND file=? AND line=?",
        (kind, file_rel, line)))
    if site_rows:
        return ClaimVerdict(
            claim, REFUTED,
            reason=(f"{kind} read at {file_rel}:{line} is now "
                    f"{[r['name'] for r in site_rows]}, not "
                    f"{claim.subject!r}"),
            current_evidence=[{
                "file": r["file"], "line": int(r["line"]),
                "kind": r["kind"], "name": r["name"], "role": r["role"],
                "value": r["name"],
            } for r in site_rows],
            quality_warnings=quality)

    # Name may still exist elsewhere in the codebase. Surface that as the
    # refutation evidence so the reader can see the new call-site.
    elsewhere = list(store.conn.execute(
        "SELECT name, file, line, role, kind FROM contracts "
        "WHERE kind=? AND name=? LIMIT 20",
        (kind, claim.subject)))
    if elsewhere:
        return ClaimVerdict(
            claim, REFUTED,
            reason=(f"{kind} {claim.subject!r} exists but not at "
                    f"{file_rel}:{line}"),
            current_evidence=[{
                "file": r["file"], "line": int(r["line"]),
                "kind": r["kind"], "name": r["name"], "role": r["role"],
            } for r in elsewhere],
            quality_warnings=quality)

    # Name not found anywhere.
    return ClaimVerdict(
        claim, REFUTED,
        reason=f"no {kind} named {claim.subject!r} in the current index",
        current_evidence=[],
        quality_warnings=quality)


def _verify_env_read_at(store, claim: Claim) -> ClaimVerdict:
    return _verify_contract_read_at(store, claim, "env")


def _verify_flag_read_at(store, claim: Claim) -> ClaimVerdict:
    return _verify_contract_read_at(store, claim, "flag")


def _verify_reexported_via(store, claim: Claim) -> ClaimVerdict:
    """VERIFIED if <subject> (file) is re-exported via <object> (barrel file).
    Implementation: check the edges table for a reexport_star edge from
    object → subject."""
    barrel = claim.object
    leaf = claim.subject
    if not barrel or not leaf:
        return ClaimVerdict(claim, UNCHECKABLE,
                            reason="both subject (leaf) and object (barrel) required")
    for which, raw in (("barrel", barrel), ("leaf", leaf)):
        r, amb = _resolve_cited_file(store, raw)
        if amb is not None:
            return ClaimVerdict(claim, UNCHECKABLE,
                                 reason=f"{which}: {amb['reason']}",
                                 current_evidence=[amb])
        if r is not None:
            if which == "barrel":
                barrel = r
            else:
                leaf = r
    rows = list(store.conn.execute(
        "SELECT src, dst, type, evidence FROM edges "
        "WHERE src=? AND dst=? AND type='reexport_star'",
        (barrel, leaf)))
    if rows:
        return ClaimVerdict(
            claim, VERIFIED,
            current_evidence=[{
                "src": r["src"], "dst": r["dst"], "type": r["type"],
                "evidence": r["evidence"] or "",
            } for r in rows])
    # Barrel may exist but not re-export this leaf — look up edges from the
    # barrel to see what IS re-exported.
    siblings = list(store.conn.execute(
        "SELECT dst FROM edges WHERE src=? AND type='reexport_star' LIMIT 20",
        (barrel,)))
    if siblings:
        return ClaimVerdict(
            claim, REFUTED,
            reason=(f"{barrel!r} re-exports "
                    f"{[r['dst'] for r in siblings]}, not {leaf!r}"),
            current_evidence=[{"src": barrel, "dst": r["dst"],
                               "type": "reexport_star"}
                              for r in siblings])
    return ClaimVerdict(
        claim, REFUTED,
        reason=f"no reexport_star edge from {barrel!r} to {leaf!r}",
        current_evidence=[])


def _verify_reverse_dep(store, claim: Claim) -> ClaimVerdict:
    """VERIFIED if <object> (file) imports <subject> (file).
    Translates to: edges where src=object, dst=subject, type='imports'."""
    importer = claim.object
    target = claim.subject
    if not importer or not target:
        return ClaimVerdict(claim, UNCHECKABLE,
                            reason="both subject (importee) and object (importer) required")
    for which, raw in (("importer", importer), ("target", target)):
        r, amb = _resolve_cited_file(store, raw)
        if amb is not None:
            return ClaimVerdict(claim, UNCHECKABLE,
                                 reason=f"{which}: {amb['reason']}",
                                 current_evidence=[amb])
        if r is not None:
            if which == "importer":
                importer = r
            else:
                target = r
    rows = list(store.conn.execute(
        "SELECT src, dst, type, evidence FROM edges "
        "WHERE src=? AND dst=? AND type='imports'",
        (importer, target)))
    if rows:
        return ClaimVerdict(
            claim, VERIFIED,
            current_evidence=[{
                "src": r["src"], "dst": r["dst"], "type": r["type"],
                "evidence": r["evidence"] or "",
            } for r in rows])
    # Maybe reachable transitively via a barrel? Mark UNCHECKABLE rather than
    # REFUTED — the caller may want a stronger claim before asserting a miss.
    indirect = list(store.conn.execute(
        "SELECT src, dst, type FROM edges "
        "WHERE src=? AND type='reexport_star' LIMIT 5",
        (importer,)))
    if indirect:
        return ClaimVerdict(
            claim, UNCHECKABLE,
            reason=(f"no direct import edge {importer!r} → {target!r}; "
                    "barrel chain may apply — use `reexported-via` to "
                    "express a barrel relationship instead."),
            current_evidence=[])
    return ClaimVerdict(
        claim, REFUTED,
        reason=f"no import edge {importer!r} → {target!r}",
        current_evidence=[])


# Registry: predicate → verifier callable
PREDICATES: Dict[str, Callable[[Any, Claim], ClaimVerdict]] = {
    "defined-at":           _verify_defined_at,
    "exported-from":        _verify_exported_from,
    "env-read-at":          _verify_env_read_at,
    "flag-read-at":         _verify_flag_read_at,
    "reexported-via":       _verify_reexported_via,
    "reverse-dependency-of": _verify_reverse_dep,
}


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------

def verify_claim(store, claim: Claim) -> ClaimVerdict:
    """Dispatch to the registered verifier for claim.predicate.
    Unknown predicates → UNCHECKABLE with a clear reason."""
    verifier = PREDICATES.get(claim.predicate)
    if verifier is None:
        return ClaimVerdict(
            claim, UNCHECKABLE,
            reason=(f"unsupported predicate {claim.predicate!r}; "
                    f"supported: {sorted(PREDICATES)}"))
    try:
        return verifier(store, claim)
    except Exception as exc:   # pragma: no cover - defensive
        return ClaimVerdict(
            claim, UNCHECKABLE,
            reason=f"{type(exc).__name__}: {exc}")


def aggregate_status(verdicts: List[ClaimVerdict]) -> str:
    """Fold per-claim verdicts into a note-level staleness label.

    Policy:
      - no verdicts                                 → UNKNOWN
      - any REFUTED where truth_class == 'FACT'     → contradicted
      - any REFUTED                                 → strongly_stale
      - any MOVED                                   → weakly_stale
      - mix of VERIFIED + UNCHECKABLE               → weakly_stale
      - all UNCHECKABLE                             → unknown
      - all VERIFIED                                → fresh

    MOVED is deliberately aggregated as weakly_stale, never as
    contradicted — a symbol that just shifted lines (e.g. an import
    was added above) shouldn't trip the blocking BLOCKER signal.
    """
    if not verdicts:
        return UNKNOWN
    statuses = [v.status for v in verdicts]
    refuted_fact = any(
        v.status == REFUTED and v.claim.truth_class == "FACT"
        for v in verdicts)
    if refuted_fact:
        return CONTRADICTED
    if REFUTED in statuses:
        return STRONGLY_STALE
    if MOVED in statuses:
        return WEAKLY_STALE
    if all(s == VERIFIED for s in statuses):
        return FRESH
    if all(s == UNCHECKABLE for s in statuses):
        return UNKNOWN
    # Mix of VERIFIED and UNCHECKABLE.
    return WEAKLY_STALE


def verify_note(store, ann_row: Dict[str, Any]) -> Dict[str, Any]:
    """Verify every claim embedded in this annotation's evidence field.

    Returns:
      {
        "claims":                 [ClaimVerdict dicts],
        "overall_status":         staleness label or None if no claims,
        "verified_count":         int,
        "refuted_count":          int,
        "uncheckable_count":      int,
        "aggregate_confidence":   float or None,
      }

    If no claims are present, returns ``{"claims": [], "overall_status": None}``
    so the caller can fall back to the fingerprint path.
    """
    evidence = ann_row.get("evidence")
    claims = parse_claims(evidence)
    if not claims:
        return {"claims": [], "overall_status": None,
                "verified_count": 0, "refuted_count": 0,
                "uncheckable_count": 0, "aggregate_confidence": None}
    verdicts = [verify_claim(store, c) for c in claims]
    status = aggregate_status(verdicts)
    v_count = sum(1 for v in verdicts if v.status == VERIFIED)
    r_count = sum(1 for v in verdicts if v.status == REFUTED)
    u_count = sum(1 for v in verdicts if v.status == UNCHECKABLE)

    # Aggregate confidence: weighted average of per-claim confidence, where
    # REFUTED contributes 0 and UNCHECKABLE is neutral (0.5).
    total_weight = sum(v.claim.confidence for v in verdicts)
    if total_weight > 0:
        score = sum(
            v.claim.confidence * (1.0 if v.status == VERIFIED else
                                   0.5 if v.status == UNCHECKABLE else 0.0)
            for v in verdicts) / total_weight
        score = round(score, 4)
    else:
        score = None

    return {
        "claims":              [v.to_dict() for v in verdicts],
        "overall_status":      status,
        "verified_count":      v_count,
        "refuted_count":       r_count,
        "uncheckable_count":   u_count,
        "aggregate_confidence": score,
    }
