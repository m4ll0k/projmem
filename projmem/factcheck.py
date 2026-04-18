"""projmem/factcheck.py — pre-output claim verification.

Tier-0 capability: extract claims from arbitrary text (a draft answer,
PR description, commit message, conversation turn) and verify each
against the current index BEFORE the text is shipped. Closes the gap
between agent confidence and agent correctness.

Extraction is deterministic by default — regex over known claim
shapes. A `use_llm` escape hatch exists for callers that want richer
extraction, but the default path runs with zero external dependencies
and sub-second latency.

The verifier reuses the existing `claims.PREDICATES` table, so a REFUTED
claim surfaced here is refuted under the same rules as a REFUTED claim
on a saved note — consistent semantics end-to-end.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple

from . import claims as _claims


# Detect *attempted* inline claims that the strict parser couldn't accept.
# Used to fill the `parse_errors` summary field — silent miss is the
# anti-pattern we want to kill: an agent who botched the syntax should
# see the failed parse, not a benign "extracted_count: 0".
#
# Matches a `@<name>(` opening; we then check downstream what shape it
# actually has. The strict regex (`_INLINE_CLAIM_RX`) requires a closing
# `)` AND a comma between subject and object. Anything that opens but
# doesn't satisfy both is reported here.
_INLINE_OPENING_RX = re.compile(
    r"@([a-z][a-z0-9_\-]{1,40})\(([^)\n]{0,500})(\)|$|\n)",
    re.IGNORECASE,
)


def _detect_parse_errors(text: str) -> List[Dict[str, Any]]:
    """Find @predicate( openings that look like attempted inline claims
    but don't satisfy the strict parser.

    Returned dict shape per error:
      {at: int, snippet: str, predicate: str, reason: str}
    """
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    for m in _INLINE_OPENING_RX.finditer(text):
        predicate = m.group(1)
        inner     = m.group(2)
        closer    = m.group(3)
        # Ok if the strict parser accepts: closing paren AND a comma.
        if closer == ")" and "," in inner:
            continue
        # Build a short snippet anchored on the opening for the report.
        start = m.start()
        snippet = text[start:start + 80].replace("\n", " ")
        if closer != ")":
            reason = ("opening @predicate( without a closing `)` on the "
                       "same line — claim was not extracted.")
        else:
            reason = ("predicate body has no comma — `@p(subject, object)` "
                       "needs both halves separated by a comma.")
        out.append({
            "at":        start,
            "snippet":   snippet,
            "predicate": predicate,
            "reason":    reason,
        })
    return out


# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------
# Each entry produces one or more `Claim` candidates. Extractors return
# a list so a single source span can yield multiple verifiable claims
# (e.g. "X is defined at Y and exported from Y" → two claims).


# 1. Inline @predicate(subject, object) — same as note-body parser.
#    Re-use that parser so fact-check and conclude share one grammar.
#
# Round-5-r3 F009: surface RAW candidates (pre-dedup) so the
# fact_check summary can report `duplicate_count > 0` when the
# same `@predicate(s, o)` appears twice in the input. The shared
# `parse_inline_claims` already dedupes upstream, so reaching for
# the regex directly here lets us count duplicates at the source.
def _extract_inline_predicates_raw(text: str) -> List[_claims.Claim]:
    if not text:
        return []
    out: List[_claims.Claim] = []
    for m in _claims._INLINE_CLAIM_RX.finditer(text):
        predicate = m.group(1).lower()
        inner = m.group(2)
        if "," not in inner:
            continue
        subj, obj = inner.split(",", 1)
        subj = subj.strip().strip("`'\"")
        obj  = obj.strip().strip("`'\"")
        if not subj or not obj:
            continue
        out.append(_claims.Claim(
            subject=subj, predicate=predicate, object=obj,
            truth_class="FACT", confidence=0.85,
        ))
    return out


def _extract_inline_predicates(text: str) -> List[_claims.Claim]:
    return _claims.parse_inline_claims(text, default_truth_class="FACT")


# 2. "`X` is defined at path/file.ts:42" / "`X` is declared at ..."
_NL_DEFINED_AT_RX = re.compile(
    r"`([A-Za-z_$][\w$]{1,80})`\s+(?:is|was)\s+"
    r"(?:defined|declared|located)\s+(?:at|in)\s+"
    r"([\w./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|h|"
    r"hpp|java|rb|kt|swift|php|scala)):(\d+)",
    re.IGNORECASE)


# 3. "`X` is exported from path/file.ts" / "`X` lives in path/file.ts"
_NL_EXPORTED_FROM_RX = re.compile(
    r"`([A-Za-z_$][\w$]{1,80})`\s+(?:is\s+)?"
    r"(?:exported\s+from|lives\s+in|defined\s+in)\s+"
    r"([\w./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|h|"
    r"hpp|java|rb|kt|swift|php|scala))\b(?!:\d)",
    re.IGNORECASE)


# 4. Bare "path/file.ts:N" mentions — weakest signal, treat as a
#    "file exists and line is in range" implicit claim. Emitted with
#    truth_class=INFERENCE so the verdict is more permissive.
_BARE_FILE_LINE_RX = re.compile(
    r"\b([\w./\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|h|"
    r"hpp|java|rb|kt|swift|php|scala)):(\d+)\b")


def _extract_nl_defined_at(text: str) -> List[_claims.Claim]:
    out: List[_claims.Claim] = []
    for m in _NL_DEFINED_AT_RX.finditer(text):
        out.append(_claims.Claim(
            subject=m.group(1), predicate="defined-at",
            object=f"{m.group(2)}:{m.group(3)}",
            truth_class="FACT", confidence=0.9,
        ))
    return out


def _extract_nl_exported_from(text: str) -> List[_claims.Claim]:
    out: List[_claims.Claim] = []
    for m in _NL_EXPORTED_FROM_RX.finditer(text):
        out.append(_claims.Claim(
            subject=m.group(1), predicate="exported-from",
            object=m.group(2),
            truth_class="FACT", confidence=0.85,
        ))
    return out


# ---------------------------------------------------------------------------
# Implicit-claim verifier: path:line sanity
# ---------------------------------------------------------------------------


def _verify_path_line_exists(store, path: str, line: int,
                              repo_root: Optional[str] = None
                              ) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return (status, current_evidence_dict). Implicit check: the cited
    path:line must exist in the repo. REFUTED if the file is missing OR
    the line exceeds the current line count.

    Benchmark v3 Bug 3 fix: when the bare filename doesn't match an
    indexed file by exact path, try a suffix match against the files
    table (e.g. `webhookDeliver.ts` → `src/worker/handlers/
    webhookDeliver.ts`). When a UNIQUE match is found, verify against
    that file. When MULTIPLE match, return UNCHECKABLE with the
    candidates so the author can disambiguate. Previously we silently
    REFUTED abbreviated paths, which looked identical to nonexistent
    files and destroyed signal.
    """
    import os as _os
    # Exact match in the files table first.
    row = store.conn.execute(
        "SELECT path FROM files WHERE path=? LIMIT 1", (path,)).fetchone()
    resolved_path: Optional[str] = path if row else None
    suffix_hit = False

    if resolved_path is None:
        # Suffix-match against indexed files. Only kick in when the
        # path contains no `/` OR the path is not rooted (relative-ish
        # but not a real repo path).
        candidates = list(store.conn.execute(
            "SELECT path FROM files WHERE path LIKE ? LIMIT 5",
            (f"%/{path}",)))
        if len(candidates) == 1:
            resolved_path = candidates[0]["path"]
            suffix_hit = True
        elif len(candidates) > 1:
            return "UNCHECKABLE", {
                "reason": "ambiguous path — citation matches multiple indexed files",
                "cited_path": path,
                "candidates": [c["path"] for c in candidates],
                "hint": ("use a repo-root-relative path, e.g. "
                          f"`{candidates[0]['path']}`"),
            }

    if resolved_path is None and repo_root is None:
        return "UNCHECKABLE", {"reason": "path not in index, no repo_root",
                                "cited_path": path}

    probe_path = resolved_path or path
    full = _os.path.join(repo_root or "", probe_path)
    if not _os.path.isfile(full):
        return "REFUTED", {"reason": "file missing", "path": probe_path,
                             "cited_path": path}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
    except OSError:
        return "UNCHECKABLE", {"reason": "read error",
                                "cited_path": path}
    if line > line_count:
        return "REFUTED", {
            "reason":              "line beyond EOF",
            "path":                probe_path,
            "cited_path":          path,
            "cited_line":          line,
            "current_line_count":  line_count,
        }
    ev: Dict[str, Any] = {"path": probe_path, "line": line,
                           "current_line_count": line_count}
    if suffix_hit:
        ev["resolved_from_suffix"] = True
        ev["cited_path"] = path
    return "VERIFIED", ev


def _extract_bare_file_lines(text: str) -> List[Tuple[str, int]]:
    """Bare `path:line` pairs that weren't already absorbed by a stronger
    NL pattern. Returns (path, line) tuples; caller deduplicates."""
    out: List[Tuple[str, int]] = []
    seen: set = set()
    for m in _BARE_FILE_LINE_RX.finditer(text):
        try:
            line = int(m.group(2))
        except ValueError:
            continue
        key = (m.group(1), line)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


def _record_paranoid_verifications(repo_root: Optional[str],
                                     entries: List[Dict[str, Any]]) -> None:
    """Round-6 paranoid mode: mark VERIFIED / MOVED claims as
    fact-checked-in-this-process so a later `note add --claims` can
    accept them under PROJMEM_PARANOID=1. Cheap no-op when no
    repo_root or no qualifying entries."""
    if not repo_root or not entries:
        return
    triples = [
        (str(e.get("subject") or ""),
         str(e.get("predicate") or ""),
         str(e.get("object") or ""))
        for e in entries
        if e.get("status") in ("VERIFIED", "MOVED")
    ]
    triples = [t for t in triples if all(t)]
    if not triples:
        return
    try:
        from . import paranoid as _paranoid
        _paranoid.record_verified(repo_root, triples)
    except Exception:
        pass  # bookkeeping must never break the verifier


def fact_check(store, text: str, *,
                repo_root: Optional[str] = None,
                max_claims: int = 50,
                include_bare_file_lines: bool = True,
                ) -> Dict[str, Any]:
    """Extract structured claims from `text` and verify each against the
    current index. Returns a structured report the caller can use to
    decide whether to ship the text as-is or revise it.

    Output shape:
      {
        extracted_count, verified, refuted, uncheckable, claims[],
        bare_file_line_checks[],
        verdict:  "all_verified" | "has_refuted" | "has_uncheckable" | "empty",
        hint
      }

    The top-level `verdict` is the single field a caller should gate on:
      * "all_verified" — every extractable claim holds; safe to ship
      * "has_refuted"  — at least one claim is false; revise first
      * "has_uncheckable" — ambiguous; caller decides
      * "empty" — no claims extracted (text makes no verifiable
                   statement). Caller should not conclude "safe"; the
                   tool just had nothing to check.
    """
    text = text or ""
    candidate_claims: List[_claims.Claim] = []
    # Round-5-r3 F009: feed RAW inline candidates so the dedup
    # counter sees duplicates that the upstream parser would have
    # silently collapsed.
    candidate_claims.extend(_extract_inline_predicates_raw(text))
    candidate_claims.extend(_extract_nl_defined_at(text))
    candidate_claims.extend(_extract_nl_exported_from(text))
    # Dedupe on (subject, predicate, object). Round-5 P3: surface the
    # duplicate count separately so a 3000-line draft that compresses
    # to 1 unique claim doesn't silently look like "I had 1 claim";
    # the caller can see the upstream input wasn't ignored.
    seen: set = set()
    dedup: List[_claims.Claim] = []
    duplicate_count = 0
    candidate_total = 0
    for c in candidate_claims:
        candidate_total += 1
        key = (c.subject, c.predicate, c.object)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        dedup.append(c)
        if len(dedup) >= max_claims:
            break

    # Surface attempted inline claims the strict parser rejected so a
    # botched `@defined-at(broken syntax` doesn't silently produce
    # `extracted_count: 0` (round-4 finding #2).
    parse_errors = _detect_parse_errors(text)

    results: List[Dict[str, Any]] = []
    v_count = r_count = u_count = m_count = 0
    for c in dedup:
        try:
            verdict = _claims.verify_claim(store, c)
            entry = verdict.to_dict()
        except Exception as e:
            entry = {**c.to_dict(), "status": "UNCHECKABLE",
                      "reason": f"verifier error: {e}"}
        status = entry.get("status") or "UNCHECKABLE"
        if status == "VERIFIED":
            v_count += 1
        elif status == "REFUTED":
            r_count += 1
        elif status == "MOVED":
            # MOVED is a soft warning, not a refutation. The cited file
            # still contains the symbol but at a different line. Counted
            # separately so a CI gate of `refuted == 0` doesn't lie about
            # the fact that line numbers drifted (round-4 finding #1).
            m_count += 1
        else:
            u_count += 1
        results.append(entry)

    # Bare path:line checks — implicit "this location still exists" claim.
    bare_checks: List[Dict[str, Any]] = []
    if include_bare_file_lines:
        # Absorb any (path, line) already covered by a strong NL claim.
        covered: set = {
            (c.object.split(":")[0], c.object.split(":")[-1])
            for c in dedup
            if c.predicate == "defined-at" and ":" in c.object
        }
        for path, line in _extract_bare_file_lines(text):
            if (path, str(line)) in covered:
                continue
            status, evidence = _verify_path_line_exists(
                store, path, line, repo_root=repo_root)
            bare_checks.append({
                "path": path, "line": line, "status": status,
                "evidence": evidence,
            })
            if status == "REFUTED":
                r_count += 1
            elif status == "VERIFIED":
                v_count += 1
            else:
                u_count += 1

    if not results and not bare_checks:
        if parse_errors:
            # Attempted to cite but botched syntax. This is a different
            # case from "nothing to check" — surface it as a distinct
            # verdict so a CI gate doesn't pass it as `empty`.
            verdict = "parse_errors"
            hint = (f"{len(parse_errors)} attempted inline claim(s) "
                    "were rejected by the parser. Fix the syntax — "
                    "`@predicate(subject, object)` requires both "
                    "halves comma-separated and a closing `)`.")
        else:
            verdict = "empty"
            hint = ("No verifiable claims extracted. This does NOT mean "
                    "the text is correct — fact-check only covers "
                    "claims that mention concrete code locations. If "
                    "your text makes factual statements, rewrite them "
                    "with backticked identifiers and file:line "
                    "citations so they can be checked.")
    elif r_count > 0:
        verdict = "has_refuted"
        hint = (f"{r_count} claim(s) REFUTED by the current code. "
                "Revise the text with the `current_evidence` values "
                "before shipping.")
    elif m_count > 0:
        # MOVED is a soft warning. Verdict tells the gate "look at the
        # moved_to lines"; nothing has to fail, but ignoring the signal
        # ships a stale citation.
        verdict = "has_moved"
        hint = (f"{v_count} claim(s) verified, {m_count} MOVED to a "
                "different line in the cited file. Update the line "
                "numbers from `moved_to` before shipping.")
    elif u_count > 0:
        verdict = "has_uncheckable"
        hint = (f"{v_count} claim(s) verified; {u_count} uncheckable. "
                "Nothing refuted; caller decides.")
    else:
        verdict = "all_verified"
        hint = (f"All {v_count} extractable claim(s) verified against "
                "the current code. Safe to ship on the claim front.")

    out: Dict[str, Any] = {
        "extracted_count":     len(dedup) + len(bare_checks),
        "candidate_count":     candidate_total,
        "duplicate_count":     duplicate_count,
        "verified":            v_count,
        "refuted":             r_count,
        "moved":               m_count,
        "uncheckable":         u_count,
        "claims":              results,
        "bare_file_line_checks": bare_checks,
        "parse_errors":        parse_errors,
        "verdict":             verdict,
        "hint":                hint,
    }
    # Round-6 paranoid: record VERIFIED / MOVED triples so PROJMEM_PARANOID
    # mode can later allow `note add --claims` for them. Always-on
    # bookkeeping; PROJMEM_PARANOID only flips the GATE on the write
    # side, not the recording side.
    _record_paranoid_verifications(repo_root, results)
    return out
