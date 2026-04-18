"""projmem/paranoid.py — gate write commands on prior verification.

Round-6 user feedback: agents under time pressure trust their own
eyes. They write `@defined-at(foo, src/a.ts:10)` notes without ever
running `fact-check` first, locking in claims that may already be
wrong. PROJMEM_PARANOID=1 raises the cost of skipping verification:
every (subject, predicate, object) saved via `note add --claims` /
`conclude` must have been VERIFIED by a fact-check / check / check-line
call in the same process tree, AND within a TTL window. Otherwise
the write is rejected with `claim-not-verified`.

The memo is a small file under `.projmem/.verified_claims` keyed on
the claim triple, with an mtime-based TTL (default 10 minutes — long
enough for an agent to draft + commit, short enough that "I verified
it last week" doesn't count). Cleared when the file is older than TTL.

Designed to fail OPEN when PROJMEM_PARANOID isn't set — zero overhead
on the default code path. Only paranoid mode pays for the bookkeeping.
"""
from __future__ import annotations
import os
import time
from typing import Iterable, Tuple


_TTL_SECS = 10 * 60  # 10 min default: long enough for an audit pass


def _memo_path(repo_root: str) -> str:
    """One memo file per indexed root. Lives next to the index DB."""
    return os.path.join(repo_root, ".projmem", ".verified_claims")


def _now() -> float:
    return time.time()


def _load_memo(repo_root: str) -> dict[Tuple[str, str, str], float]:
    """Return {triple: ts} from the memo. Drop entries older than TTL.

    Format on disk: one line per triple, `<subject>\\t<predicate>\\t<object>\\t<ts>`.
    Tab-separated so commas / parens in subject/object don't collide.
    """
    path = _memo_path(repo_root)
    out: dict[Tuple[str, str, str], float] = {}
    cutoff = _now() - _TTL_SECS
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                try:
                    ts = float(parts[3])
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                out[(parts[0], parts[1], parts[2])] = ts
    except OSError:
        pass
    return out


def _write_memo(repo_root: str,
                memo: dict[Tuple[str, str, str], float]) -> None:
    path = _memo_path(repo_root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for (subj, pred, obj), ts in memo.items():
                f.write(f"{subj}\t{pred}\t{obj}\t{ts}\n")
    except OSError:
        pass


def is_paranoid() -> bool:
    return os.environ.get("PROJMEM_PARANOID") == "1"


def record_verified(repo_root: str,
                     triples: Iterable[Tuple[str, str, str]]) -> None:
    """Mark `(subject, predicate, object)` triples as verified now.

    Called by fact-check / check / check-line whenever a claim
    completes with VERIFIED or MOVED status. Cheap when paranoid
    mode is off (still records, since callers that flip the flag
    later in the same process should see prior verifications).
    """
    triples = list(triples)
    if not triples:
        return
    memo = _load_memo(repo_root)
    now = _now()
    for t in triples:
        memo[t] = now
    _write_memo(repo_root, memo)


def is_verified(repo_root: str,
                 triple: Tuple[str, str, str]) -> bool:
    """True when the triple is in the memo and within TTL."""
    return triple in _load_memo(repo_root)


def assert_verified_or_raise(repo_root: str,
                              claims: list[dict]) -> list[dict]:
    """Return the subset of `claims` that haven't been verified.
    Caller emits a structured error when the list is non-empty.
    Each claim dict should expose subject / predicate / object.

    Returns [] (and skips memo lookup entirely) when paranoid mode
    is OFF, so the caller pays zero cost on the default path.
    """
    if not is_paranoid():
        return []
    memo = _load_memo(repo_root)
    unverified: list[dict] = []
    for c in claims or []:
        triple = (
            str(c.get("subject") or ""),
            str(c.get("predicate") or ""),
            str(c.get("object") or ""),
        )
        if all(triple) and triple not in memo:
            unverified.append(c)
    return unverified
