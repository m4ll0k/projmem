"""projmem/ask.py — natural-language query dispatcher.

Agents don't know which of the ~30 projmem commands to call. They DO
know common question shapes ("who uses X", "what does X do", "what
changed since last session"). This module pattern-matches the question
to an underlying command and returns a synthesized answer.

Deliberately regex-based — no LLM call in the hot path. When a question
doesn't match any shape, we fall back to a generic `session <subject>`
or `pack <subject>` with an explicit note that the question couldn't be
classified.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple


# Extract a plausible "subject" — a file path or code identifier — from
# the question. Order matters: explicit paths > backticked > camel/snake
# case tokens. First match wins.
_PATH_SUBJ_RX = re.compile(
    r"\b([a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-./]+"
    r"\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|c|cc|cpp|cxx|h|hpp|java|"
    r"rb|kt|swift|php|scala|prisma|sql|json|md|yaml|yml|toml))\b")
_BACKTICK_SUBJ_RX = re.compile(r"`([A-Za-z_$][\w$]{1,80})`")
_QUOTED_SUBJ_RX = re.compile(r"[\"']([A-Za-z_$][\w$]{1,80})[\"']")
# Verb-anchored: grab the noun that's the OBJECT of the question's verb,
# regardless of case. Without this, lowercase Python/Java method names
# (`normalize`, `parse`) get skipped by the CamelCase fallback and the
# dispatcher latches onto a downstream qualifier (`CoyoteAdapter` in
# `who calls normalize in CoyoteAdapter`). Tomcat audit surfaced this.
_VERB_OBJECT_RX = re.compile(
    r"\b(?:uses|use|calls?|invoke[sd]?|imports?|"
    r"depend(?:s|ed)? on|consumers? of|callers? of|"
    r"reverse dep(?:s|endencies?)? (?:of|for)|"
    r"where(?:'s| is)|where (?:was|did)|"
    r"defined|exported(?: from)?|implemented|implements?|extends?|"
    r"to delete|safe to (?:delete|remove)|how does)\s+"
    r"([A-Za-z_][A-Za-z0-9_$]{1,80})\b", re.I)
_IDENT_AFTER_KEYWORD_RX = re.compile(
    r"\b(?:function|symbol|handler|gateway|the)\s+"
    r"([A-Z][A-Za-z0-9_]{2,60}|[a-z][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*)\b")
_BARE_IDENT_RX = re.compile(
    r"\b([A-Z][A-Za-z0-9_]{2,60}|[a-z][a-z0-9_]*[A-Z][A-Za-z0-9_]*)\b")

# F015 (round-7): lowercase identifiers (`normalize`, `parse`,
# `callapplication`) need their own pattern. CamelCase fallback can't
# see them. We accept any 4+ char lowercase token that isn't a
# common English stopword — short tokens (`if`, `is`, `or`) would
# false-match too aggressively, and the verb-anchored extractor
# upstream already catches the high-precision cases.
_BARE_LOWER_IDENT_RX = re.compile(
    r"\b([a-z][a-z0-9_]{3,60})\b")

# The canonical question shapes we dispatch. Patterns are tried in
# order; first match wins. Each entry: (compiled_regex, shape_label,
# handler_callable_name).
_SHAPES: List[Tuple[re.Pattern, str, str]] = [
    # Repo / project-level
    (re.compile(r"\b(what (?:is|does) (?:this|the) (?:repo|project|codebase))",
                re.I), "project_overview", "_do_overview"),
    (re.compile(r"\b(repo overview|project overview|orient me)", re.I),
     "project_overview", "_do_overview"),
    (re.compile(r"\b(what(?:'s| is|'ve we) (?:in )?memory|what do we know|"
                r"what(?:'s| is| have we) saved|prior (?:notes|conclusions)|"
                r"what did .{1,40} conclude)", re.I),
     "memory_recall", "_do_memory_recall"),
    # What changed / edit history
    (re.compile(r"\b(what('s| has| have we)? changed|what did we edit|"
                r"what was edited|what's new|latest edits?|"
                r"(?:since|last) session)", re.I),
     "changes", "_do_changes"),
    # Who uses / depends on X
    (re.compile(r"\b(who uses|who calls|who imports?|who depends on|"
                r"consumers? of|callers? of|reverse deps? (?:of|for))\b",
                re.I), "who_uses", "_do_reverse"),
    # Where is X (defined / read / set)
    (re.compile(r"\b(where (?:is|are) .+ defined|where'?s .+ defined|"
                r"where .+ (?:is )?declared)", re.I),
     "where_defined", "_do_symbol"),
    (re.compile(r"\b(where (?:is|are) .+ read|where (?:is|are) .+ set|"
                r"where (?:is|are) .+ used)", re.I),
     "where_used", "_do_flow_or_symbol"),
    # What does X do / what is X
    (re.compile(r"\b(what does .+ do|what is .+|explain .+|"
                r"how does .+ work|tell me about .+)",
                re.I), "explain", "_do_explain"),
    # Safety / delete
    (re.compile(r"\b(safe to (?:delete|remove)|can (?:i|we) (?:delete|"
                r"remove)|blast radius)", re.I),
     "blast_radius", "_do_blast_radius"),
    # Verify
    (re.compile(r"\b(verify .+|is .+ still (?:true|valid|correct)|"
                r"check (?:my )?(?:claim|note|belief) about .+)", re.I),
     "verify", "_do_verify"),
]


# Placeholder patterns an agent might paste verbatim from a template
# — "<name>", "<target>", "{file}", etc. Benchmark v3 Bug 5: codex
# ran `projmem ask "safe to delete <name>?"` literally; ask returned
# "Couldn't identify a subject" with no hint.
_PLACEHOLDER_RX = re.compile(
    r"[<{]\s*(?:name|target|symbol|file|subject|query|term)\s*[>}]",
    re.IGNORECASE)


def _contains_placeholder(q: str) -> bool:
    return bool(q) and bool(_PLACEHOLDER_RX.search(q))


def _extract_subject(q: str) -> Optional[str]:
    """Best-effort subject extraction. Returns a file path, an
    identifier, or None if the question has no clear subject."""
    if not q:
        return None
    for rx in (_PATH_SUBJ_RX, _BACKTICK_SUBJ_RX, _QUOTED_SUBJ_RX,
               _VERB_OBJECT_RX, _IDENT_AFTER_KEYWORD_RX):
        m = rx.search(q)
        if m:
            return m.group(1)
    # Last resort: the first CamelCase / snake-with-capital token in
    # the question that isn't a stopword.
    stopwords = {"Are", "Can", "Did", "Does", "Find", "Give", "How",
                 "If", "Is", "Show", "Tell", "This", "Was", "What",
                 "When", "Where", "Who", "Why", "Will", "Let"}
    for m in _BARE_IDENT_RX.finditer(q):
        if m.group(1) not in stopwords:
            return m.group(1)
    # F015 (round-7): pure-lowercase fallback. Allows `normalize`,
    # `callapplication`, `parsehandler` etc. to be picked up. A
    # short stopword list rules out common English question words —
    # not exhaustive (linguistic perfection isn't the bar) but
    # catches the cases that previously dropped to `subject: None`.
    _LOWER_STOPWORDS = {
        "about", "after", "again", "any", "anything", "around", "back",
        "before", "between", "both", "could", "doing", "down", "each",
        "every", "everything", "from", "have", "into", "just", "more",
        "much", "name", "next", "nothing", "okay", "only", "other",
        "over", "really", "should", "some", "something", "still",
        "such", "take", "than", "that", "them", "then", "there",
        "these", "they", "this", "those", "through", "under", "very",
        "want", "well", "were", "what", "when", "where", "which",
        "while", "with", "would", "your", "tell", "show", "find",
        "give", "make", "know", "think", "look", "going", "ever",
        "actually", "maybe", "okay", "exactly", "okay", "thing",
    }
    for m in _BARE_LOWER_IDENT_RX.finditer(q):
        cand = m.group(1)
        if cand.lower() in _LOWER_STOPWORDS:
            continue
        return cand
    return None


# ---------------------------------------------------------------------------
# Handlers — each returns a dict blending the underlying command's
# output with a one-sentence "summary" the agent can use without
# parsing the rest.
# ---------------------------------------------------------------------------


def _do_overview(cfg, store, subject, q):
    from . import packs as _packs
    loc = {"kind": "directory", "path": ".", "scope_prefix": "",
           "is_root": True}
    out = _packs.build_repo_overview(cfg, store, loc=loc)
    top = out.get("top_files_by_reverse_deps") or []
    langs = out.get("summary", {}).get("languages") or {}
    lang_desc = ", ".join(f"{n} {l}" for l, n in
                           list(sorted(langs.items(),
                                       key=lambda kv: -kv[1]))[:3])
    hot = ", ".join(f["file"] for f in top[:3])
    summary = (f"{out['summary']['files_indexed']} files "
                f"({lang_desc}). Hot files: {hot}.")
    return {"summary": summary, "commands_run": ["pack ."], "detail": out}


def _do_memory_recall(cfg, store, subject, q):
    from . import notes_summary as _ns
    out = _ns.build_summary(store, cfg.root)
    totals = out.get("totals") or {}
    n = totals.get("total_notes") or 0
    contradicted = totals.get("by_staleness", {}).get("contradicted") or 0
    summary = (f"{n} note(s) in memory; {contradicted} contradicted. "
                "See `recent_notes` for details.")
    return {"summary": summary, "commands_run": ["notes"], "detail": out}


def _do_changes(cfg, store, subject, q):
    from . import changes as _changes
    out = _changes.compute_changes(store, cfg.root)
    s = out.get("summary") or {}
    summary = (f"{s.get('changed_files', 0)} file(s) changed; "
                f"{s.get('drifted_on_disk', 0)} drifted on disk. "
                f"{out.get('hint', '')}")
    return {"summary": summary, "commands_run": ["changes"], "detail": out}


def _do_reverse(cfg, store, subject, q):
    if not subject:
        return {"summary": "Couldn't identify a subject in the question.",
                "commands_run": [], "detail": None, "error": "no_subject"}
    from . import graph as _graph
    # Subject might be a file or a symbol. Prefer file-path shape.
    if "/" in subject or subject.endswith((".ts", ".tsx", ".js", ".py",
                                              ".go", ".rs")):
        rd = _graph.reverse_deps(store, subject)
        summary = (f"{subject} has {len(rd)} reverse dep(s).")
        return {"summary": summary,
                "commands_run": [f"reverse {subject}"],
                "detail": {"subject": subject, "reverse_deps": rd[:50]}}
    # Symbol case — fall back to listing ref sites for the name.
    refs = list(store.conn.execute(
        "SELECT file, line, kind FROM refs WHERE name=? LIMIT 50",
        (subject,)))
    summary = f"{subject}: {len(refs)} ref site(s) in the index (name-level)."
    return {"summary": summary,
            "commands_run": [f"symbol {subject}"],
            "detail": {"subject": subject,
                        "refs": [dict(r) for r in refs]}}


def _do_symbol(cfg, store, subject, q):
    if not subject:
        # Benchmark: "where is x-opscanvas-signature set?" extracts no
        # valid symbol subject (kebab-case isn't identifier-shaped).
        # Fall back to literal token search so the question isn't a
        # dead end.
        return _do_fallback_literal_search(store, subject, q)
    defs = list(store.symbols_by_name(subject))
    if not defs:
        return _do_fallback_literal_search(store, subject, q)
    first = defs[0]
    summary = (f"{subject} is defined at {first['file']}:{first['line']} "
                f"({'+%d other site(s)' % (len(defs)-1) if len(defs) > 1 else 'unique'}).")
    return {"summary": summary,
            "commands_run": [f"symbol {subject}"],
            "detail": {"subject": subject,
                        "defs": [dict(d) for d in defs]}}


def _do_fallback_literal_search(store, subject, q):
    """When the subject doesn't resolve as a symbol, search:
      - contracts table (token / env / flag / schema_field)
      - note bodies (author, full-text)
      - any literal token in the original question
    Returns up to 5 hits with a hint pointing at the specific
    command that would have answered more directly.
    """
    # Extract every plausible literal token from the question itself
    # (kebab-case, snake_case, quoted strings). The `subject` is the
    # extracted symbol-shape thing; what we want HERE is the broader
    # set of tokens that appear in the question.
    tokens: List[str] = []
    if subject:
        tokens.append(subject)
    for m in re.finditer(r"[\"'`]([A-Za-z0-9_\-./]{3,60})[\"'`]", q or ""):
        tokens.append(m.group(1))
    for m in re.finditer(
            r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+){1,6})\b", q or ""):
        tokens.append(m.group(1))
    # Dedupe preserving order.
    seen: set = set()
    uniq_tokens = [t for t in tokens if not (t in seen or seen.add(t))]
    hits: List[Dict[str, Any]] = []
    for tok in uniq_tokens[:6]:
        # 1. Contracts (token / env / flag / schema_field).
        for r in store.conn.execute(
                "SELECT kind, name, file, line, role FROM contracts "
                "WHERE name=? LIMIT 5", (tok,)):
            hits.append({"source": "contracts", "token": tok,
                          "kind": r["kind"], "name": r["name"],
                          "file": r["file"], "line": r["line"],
                          "role": r["role"]})
        # 2. Notes whose body mentions the token.
        for r in store.conn.execute(
                "SELECT id, target, body FROM annotations "
                "WHERE body LIKE ? LIMIT 3", (f"%{tok}%",)):
            hits.append({"source": "notes", "token": tok,
                          "note_id": r["id"], "target": r["target"],
                          "body_preview": (r["body"] or "")[:120]})
    if not hits:
        summary = (f"No symbol {subject!r} and no literal matches for "
                    f"tokens in the question ({uniq_tokens[:3]})."
                    if subject
                    else "Couldn't extract a searchable subject.")
        return {"summary": summary,
                "commands_run": [f"symbol {subject}" if subject else ""],
                "detail": {"subject": subject, "tokens_tried": uniq_tokens,
                            "hits": []},
                "hints": [
                    "`projmem ask` is symbol-centric. For literal "
                    "strings use `projmem contracts --kind token <name>` "
                    "or `projmem flow <name>`. For prose search use "
                    "`projmem note search \"<term>\"` or `rg` directly.",
                ]}
    summary = (f"No symbol {subject!r}; found {len(hits)} literal "
                "match(es) in contracts/notes. See `detail.hits`.")
    return {"summary": summary,
            "commands_run": [f"contracts <token>", f"note search <token>"],
            "detail": {"subject": subject, "tokens_tried": uniq_tokens,
                        "hits": hits[:10]},
            "hints": [
                "`projmem ask` dispatched to a fallback because the "
                "subject wasn't a symbol def. For faster lookup on "
                "literal strings, use `projmem contracts --kind token "
                "<name>` directly."
            ]}


def _do_flow_or_symbol(cfg, store, subject, q):
    # Prefer flow when the subject is SHOUTY_SNAKE (typical env/flag
    # name); otherwise fall back to symbol.
    if subject and re.match(r"^[A-Z][A-Z0-9_]{2,64}$", subject):
        from . import flow as _flow
        out = _flow.trace_flow(store, cfg.root, subject)
        hints = out.get("hints") or []
        summary = (f"{subject}: {len(out.get('read_sites') or [])} read "
                    f"site(s).")
        if hints:
            summary += f" Hint: {hints[0]}"
        return {"summary": summary,
                "commands_run": [f"flow {subject}"],
                "detail": out}
    return _do_symbol(cfg, store, subject, q)


def _clean_commands_run(cmds: List[str]) -> List[str]:
    """Shim for backward compat — delegates to
    projmem.security.clean_command_list."""
    from . import security as _sec
    return _sec.clean_command_list(cmds)


def _do_explain(cfg, store, subject, q):
    if not subject:
        return _do_overview(cfg, store, subject, q)
    # Combine: symbol def + reverse dep count + notes on the subject.
    from . import graph as _graph
    defs = list(store.symbols_by_name(subject))
    def_site = None
    if defs:
        d = defs[0]
        def_site = f"{d['file']}:{d['line']}"
    rd_count = 0
    note_count = 0
    note_previews: List[str] = []
    if def_site:
        rd_count = len(_graph.reverse_deps(store, defs[0]["file"]))
    try:
        notes = store.list_annotations(target=subject)
        if not notes and def_site:
            notes = store.list_annotations(target=defs[0]["file"])
        note_count = len(notes)
        for n in notes[:2]:
            note_previews.append((n.get("body") or "")[:160])
    except Exception:
        pass
    if def_site:
        summary = (f"{subject} defined at {def_site}; "
                    f"{rd_count} reverse dep(s); {note_count} saved note(s).")
        out_hints: List[str] = []
    else:
        summary = f"No def for {subject!r}; {note_count} saved note(s)."
        # Benchmark v3 Bug 6: when the explain handler can't find a
        # def AND no notes exist on the subject, the question is
        # almost always asking for something projmem doesn't track
        # (runtime behavior, config values, timeouts). Admit it
        # explicitly instead of letting the weak summary stand alone.
        out_hints = [
            f"{subject!r} has no def in the index. If your question is "
            "about runtime behavior (timeouts, retries, side effects), "
            "projmem can't answer that — it tracks structure, not "
            "execution. Read the source file or run the code.",
            "If you're looking for a literal string / config value, "
            "try `projmem contracts --kind token <name>`.",
        ]
    return {"summary":       summary,
            "commands_run":  _clean_commands_run([
                f"symbol {subject}",
                f"reverse {defs[0]['file']}" if defs else "",
                "notes",
            ]),
            "detail": {"subject": subject, "def_site": def_site,
                        "reverse_dep_count": rd_count,
                        "note_previews": note_previews},
            "hints": out_hints}


def _do_blast_radius(cfg, store, subject, q):
    if not subject:
        return {"summary": "Couldn't identify a subject.",
                "commands_run": [], "detail": None, "error": "no_subject"}
    from . import graph as _graph
    if "/" in subject or subject.endswith((".ts", ".tsx", ".js", ".py",
                                              ".go", ".rs")):
        # File path that isn't indexed → can't assess, don't lie.
        file_indexed = bool(store.conn.execute(
            "SELECT 1 FROM files WHERE path=? LIMIT 1",
            (subject,)).fetchone())
        if not file_indexed:
            return {
                "summary": (f"Cannot assess {subject!r}: file not "
                             "in the index. Default verdict is "
                             "CANNOT_ASSESS — do NOT delete on this "
                             "answer alone."),
                "commands_run": [f"reverse {subject}"],
                "detail": {"subject":  subject,
                            "verdict": "CANNOT_ASSESS",
                            "reason":  "file-not-indexed",
                            "hint": ("Run `projmem index` to refresh, "
                                      "or pass an indexed path. "
                                      "`projmem files --glob '*'` lists "
                                      "every indexed file.")},
            }
        rd = _graph.reverse_deps(store, subject)
    else:
        defs = list(store.symbols_by_name(subject))
        if not defs:
            # F021 (round-7): the SAFER default is CANNOT_ASSESS, not
            # LIKELY_SAFE. A missing def could mean (a) deletable dead
            # code, (b) framework / dynamic-dispatch entry the indexer
            # can't see, (c) the agent typoed the name. We can't
            # discriminate. Returning "safe to remove" was a hazard.
            return {
                "summary": (f"Cannot assess {subject!r}: no def in "
                             "the index. Default verdict is "
                             "CANNOT_ASSESS — do NOT delete on this "
                             "answer alone (may be framework / "
                             "dynamic-dispatch / typo)."),
                "commands_run": [f"symbol {subject}"],
                "detail": {"subject":  subject,
                            "verdict": "CANNOT_ASSESS",
                            "reason":  "symbol-undefined",
                            "hint": ("Try `projmem search " + subject +
                                      "` for a fuzzy cross-bucket "
                                      "match; if zero hits anywhere "
                                      "the name is likely a typo, "
                                      "not a deletable symbol.")},
            }
        rd = _graph.reverse_deps(store, defs[0]["file"])
    verdict = ("UNSAFE"   if len(rd) >= 5 else
                "REVIEW"   if len(rd) >= 1 else
                "LIKELY_SAFE")
    summary = (f"{subject}: {len(rd)} reverse dep(s) → verdict {verdict}.")
    return {"summary": summary,
            "commands_run": [f"reverse {subject}"],
            "detail": {"subject": subject, "verdict": verdict,
                        "reverse_deps": rd[:20]}}


def _do_verify(cfg, store, subject, q):
    if not subject:
        return {"summary": "Couldn't identify a subject.",
                "commands_run": [], "detail": None, "error": "no_subject"}
    from . import integrity as _intg
    notes = store.list_annotations(target=subject)
    if not notes:
        defs = list(store.symbols_by_name(subject))
        if defs:
            notes = store.list_annotations(target=defs[0]["file"])
    if not notes:
        return {"summary": f"No notes on {subject!r} to verify.",
                "commands_run": ["note list"],
                "detail": {"subject": subject, "notes": []}}
    results: List[Dict[str, Any]] = []
    for n in notes[:5]:
        try:
            res = _intg.revalidate_annotation(store, cfg.root, n,
                                                persist=False)
            results.append({
                "id":          n.get("id"),
                "staleness":   res.now,
                "verified":    sum(1 for v in (res.claim_verdicts or [])
                                     if v.get("status") == "VERIFIED"),
                "refuted":     sum(1 for v in (res.claim_verdicts or [])
                                     if v.get("status") == "REFUTED"),
            })
        except Exception as e:
            results.append({"id": n.get("id"), "error": str(e)})
    total_refuted = sum(r.get("refuted", 0) for r in results)
    summary = (f"{len(results)} note(s) verified; {total_refuted} refuted "
                "claim(s).")
    return {"summary": summary,
            "commands_run": [f"note-verify {subject}"],
            "detail": {"subject": subject, "results": results}}


_HANDLERS = {
    "_do_overview":        _do_overview,
    "_do_memory_recall":   _do_memory_recall,
    "_do_changes":         _do_changes,
    "_do_reverse":         _do_reverse,
    "_do_symbol":          _do_symbol,
    "_do_flow_or_symbol":  _do_flow_or_symbol,
    "_do_explain":         _do_explain,
    "_do_blast_radius":    _do_blast_radius,
    "_do_verify":          _do_verify,
}


def ask(cfg, store, question: str) -> Dict[str, Any]:
    """Classify `question` to a shape, dispatch to the underlying
    command, return a synthesized answer.

    Output schema:
      {question, shape, subject, summary, commands_run, detail, hints?}

    When no pattern matches, shape="unclassified". Benchmark v3 Bug 6
    fix: instead of silently dumping a 6 KB repo_overview, we return
    a structured "I can't classify this" response with specific
    suggestions for what the caller might rephrase to.
    """
    q = (question or "").strip()

    # Benchmark v3 Bug 5: detect template placeholders like "<name>"
    # or "{target}". These happen when an agent pastes a prompt
    # verbatim. Short-circuit with a clear instruction.
    if _contains_placeholder(q):
        return {
            "question": question,
            "shape":    "placeholder_detected",
            "subject":  None,
            "summary":  ("Question contains a template placeholder (e.g. "
                          "<name> or {target}). Replace it with the real "
                          "symbol / file you want to ask about."),
            "commands_run": [],
            "detail": None,
            "hints": [
                "Example: projmem ask \"safe to delete signWebhookPayload?\"",
                "Example: projmem ask \"who uses src/server/db/prisma.ts?\"",
            ],
        }

    subject = _extract_subject(q)
    for rx, shape, handler_name in _SHAPES:
        if rx.search(q):
            handler = _HANDLERS[handler_name]
            out = handler(cfg, store, subject, q)
            out["question"] = question
            out["shape"] = shape
            out["subject"] = subject
            if "commands_run" in out:
                out["commands_run"] = _clean_commands_run(out["commands_run"])
            return out

    # Unclassified. Benchmark v3 Bug 6: stop returning a 6 KB overview
    # as a silent fallback. Explicitly admit the dispatcher couldn't
    # classify and offer actionable suggestions. Only call the overview
    # when the caller's question is literally empty / pure noise.
    if subject:
        # There's a plausible subject; a targeted explain is useful.
        out = _do_explain(cfg, store, subject, q)
        out["question"] = question
        out["shape"] = "unclassified_with_subject"
        out["subject"] = subject
        out.setdefault("hints", []).append(
            "Question pattern wasn't recognized, but a subject "
            f"({subject!r}) was extracted. The answer above reflects "
            "a symbol / explain pass. For more specific shapes try: "
            "'who uses X', 'where is X defined', 'what changed', "
            "'safe to delete X'.")
        if "commands_run" in out:
            out["commands_run"] = _clean_commands_run(out["commands_run"])
        return out
    # No subject extractable — genuinely can't help. Don't dump an
    # overview; return an explicit "can't classify" response.
    return {
        "question": question,
        "shape":    "unclassified",
        "subject":  None,
        "summary":  ("I couldn't classify that question or extract a "
                      "concrete subject from it. Projmem tracks code "
                      "structure (symbols, refs, contracts) — it "
                      "does not execute code or reason about runtime "
                      "behavior."),
        "commands_run": [],
        "detail": None,
        "hints": [
            "Recognized shapes include: 'who uses <path>', "
            "'where is <name> defined', 'what does <name> do', "
            "'what changed', 'safe to delete <name>', 'repo overview'.",
            "For project orientation run `projmem pack .` directly.",
            "For runtime-behavior questions (timeouts, retries, "
            "concurrency), read the source — projmem can't introspect "
            "execution.",
        ],
    }
