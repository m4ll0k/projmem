"""Canonical inventory of annotation `kind` values.

The ``annotations.kind`` column accepts any TEXT, but tooling (CLI option
choices, MCP server schema, UI dropdowns) needs a single source of truth
for the kinds the agent and human are expected to use.

Existing v1 kinds (``KINDS_V1``) are kept as-is — every legacy database
will read them back unchanged. v2 adds four guidance-shaped kinds
(``KINDS_V2_GUIDANCE``) that the new ``editing`` verb injects into the
agent's tool-result prelude; ``critical`` is the strongest of these and
carries the cosigner gate.

Adding a kind: extend the matching tuple here and add a regression test.
Removing a kind is not allowed without a migration that retags every
affected row — annotations are persistent across sessions and silent
removal would break recall.
"""
from __future__ import annotations

from typing import Tuple


KINDS_V1: Tuple[str, ...] = (
    "note",
    "refute",
    "verified-safe",
    "documented-footgun",
    "todo",
    "link",
    "risk",
)

KINDS_V2_GUIDANCE: Tuple[str, ...] = (
    "guidance",
    "constraint",
    "preference",
    "critical",
    # `exclude` — scope-out marker. When attached to a directory
    # (target ends in `/`) or `@project`, every file in that subtree
    # surfaces an "OUT OF SCOPE" warning on `projmem editing`. Use it
    # to keep agents from wasting tokens on Linux-only / Windows-only
    # / vendored / deprecated / generated subtrees.
    "exclude",
)

# Reserved for v2.1 skills — path-scoped cognitive instructions
# (docs/v2-design.md Pillar 3.5). The kind is in the enum so v2.1
# can land verbs without a schema migration; the storage scaffold
# is in projmem/migrations/m004_skill_scaffold.py.
KINDS_V21_SKILL: Tuple[str, ...] = (
    "skill",
)

KNOWN_KINDS: Tuple[str, ...] = (
    KINDS_V1 + KINDS_V2_GUIDANCE + KINDS_V21_SKILL
)

DEFAULT_KIND: str = "note"


def is_guidance_kind(kind: str) -> bool:
    """True for kinds whose body is injected as a tool-call prelude."""
    return kind in KINDS_V2_GUIDANCE


def is_critical_kind(kind: str) -> bool:
    """True only for the strongest kind; gated by the cosigner check."""
    return kind == "critical"


def is_skill_kind(kind: str) -> bool:
    """True for v2.1 skills (path-scoped cognitive instructions)."""
    return kind in KINDS_V21_SKILL
