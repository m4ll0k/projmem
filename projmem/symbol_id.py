"""SCIP-shaped deterministic symbol identifiers (M8).

Problem this solves: when `M3` emits an `extends` edge with `dst="Animal"`,
and two `Animal` classes exist in different files, the edge is ambiguous at
the store level. Same class of bug that `pack cdpCallOptional` had before
`file#symbol` disambiguation. SCIP solves this by making symbol identity
a structured string; we adopt a simplified version of that grammar.

Format (MVP):
    <file> "#" <name> <suffix>

Suffix vocabulary borrowed from SCIP (scip.proto:147-193):
    "."   term    — function, method, var, const, exported
    "#"   type    — class, interface, struct, enum, trait, type alias
    "/"   namespace — module, object, package-like
    "!"   macro

Future (deliberately deferred from MVP):
    - container qualification:  `file#Animal.sound().`
    - package/version scheme:    `npm @foo/bar 1.2 file#Name.`
    - local-counter for file-local names:  `local 42`

For V1 the symbol-ID is always `<file>#<name><suffix>`. This is enough to
disambiguate across files (different files → different IDs) and within a
file (name collisions are rare; when they happen, kind-suffix usually
distinguishes). Containers and package-scheme are additive and won't
break the V1 grammar.
"""
from __future__ import annotations
from typing import Optional


SUFFIX_BY_KIND = {
    # term-like (runtime value)
    "function": ".", "method": ".", "exported": ".",
    "var": ".", "const": ".", "getter": ".", "setter": ".",
    # type-like
    "class": "#", "interface": "#", "struct": "#",
    "enum": "#", "trait": "#", "type": "#", "type_alias": "#",
    "union": "#", "protocol": "#",
    # namespace-like
    "module": "/", "object": "/", "package": "/", "namespace": "/",
    # other
    "macro": "!",
}


def build(file: str, name: str, kind: str) -> str:
    """Deterministic symbol ID. Input is normalized (relpath uses `/`)."""
    if not file or not name:
        return f"{file or '?'}#{name or '?'}?"
    suffix = SUFFIX_BY_KIND.get(kind, ".")
    return f"{file}#{name}{suffix}"


def parse(sid: str) -> dict:
    """Reverse a symbol ID back into components. Returns a dict with
    `file`, `name`, `suffix`, `kind_hint`. `kind_hint` is the best-guess
    kind from the suffix (not authoritative — use the stored `kind`)."""
    if "#" not in sid:
        return {"raw": sid}
    file, rest = sid.split("#", 1)
    suffix = None
    for suf in ("#", ".", "/", "!"):
        if rest.endswith(suf):
            suffix = suf
            rest = rest[: -len(suf)]
            break
    kind_hint = {"#": "type", ".": "term", "/": "namespace",
                 "!": "macro"}.get(suffix or "")
    return {"file": file, "name": rest, "suffix": suffix,
            "kind_hint": kind_hint}


def is_symbol_id(s: str) -> bool:
    """Cheap detector — has a '#' separator and a known terminal suffix."""
    if "#" not in s:
        return False
    return any(s.endswith(suf) for suf in ("#", ".", "/", "!"))
