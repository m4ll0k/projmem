"""Skills — path-scoped cognitive instructions (v2.1 → promoted to v2.0).

A skill is a reusable prompt fragment that tells the agent *how to
think* when working in a particular zone of the codebase. Unlike
`guidance` (advice about what to watch out for) or `critical`
(load-bearing warnings), skills are mindsets injected as the prelude
the agent runs under for the duration of an `editing` lease.

Public surface:

    add_skill(store, *, name, prompt, scope_pattern, ...)
    list_skills(store)
    skills_for_path(store, path)           — pattern + explicit attaches
    attach_skill(store, name, path)        — explicit per-lifeline pin
    detach_skill(store, name, path)
    edit_skill(store, name, **fields)
    disable_skill(store, name, *, enabled=False)
    test_skill(store, name, *, path)       — preview the injection
    build_skill_prelude(rows)              — formatted block for the agent

Injection format depends on the skill's ``inject_as`` field — three
strength tiers (``reminder`` / ``prelude`` / ``system``) the brief
calls for so skills *feel* different from guidance.
"""
from __future__ import annotations

import fnmatch
import json
import pathlib
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _glob_match(pattern: str, path: str) -> bool:
    """Match ``path`` against ``pattern`` with gitignore-style `**` semantics.

    Standard glob libraries (fnmatch, pathlib.PurePath.match) require
    `**/x` to match AT LEAST one intermediate directory — so
    `projmem/**/*.py` won't match `projmem/store.py`. Users expect
    gitignore-style behavior where `**` matches zero-or-more segments,
    including the empty case. We expand each pattern containing `**`
    into the set of equivalent shallower patterns and try them all.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return False
    if pattern in ("**", "*", "**/*", "**/**"):
        return True

    candidates = {pattern}
    # `**/x` should also match `x` (zero segments case).
    if "**/" in pattern:
        candidates.add(pattern.replace("**/", ""))
    # `/**/` should collapse to `/` (zero segments between).
    if "/**/" in pattern:
        candidates.add(pattern.replace("/**/", "/"))
    # `**` alone (not preceded by `/`) — rewrite as `*`.
    candidates.add(pattern.replace("**", "*"))

    for cand in candidates:
        try:
            if pathlib.PurePath(path).match(cand):
                return True
        except ValueError:
            pass
        if fnmatch.fnmatch(path, cand):
            return True
    return False


VALID_TRIGGERS  = ("on_edit", "on_read", "on_create", "always")
VALID_INJECT_AS = ("reminder", "prelude", "system")


class SkillError(Exception):
    code: str = "skill-error"

    def envelope(self) -> Dict[str, Any]:
        return {"error": self.code, "message": str(self)}


class SkillNotFoundError(SkillError):
    code = "skill-not-found"


class SkillNameConflictError(SkillError):
    code = "skill-name-conflict"


class InvalidSkillFieldError(SkillError):
    code = "skill-field-invalid"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _validate_fields(
    *, name: str, prompt: str, scope_pattern: str,
    trigger: str, inject_as: str,
) -> None:
    if not (name or "").strip():
        raise InvalidSkillFieldError("name required (non-empty)")
    if len((prompt or "").strip()) < 8:
        raise InvalidSkillFieldError(
            "prompt too short (< 8 chars); skills are mindsets — describe "
            "the cognitive instruction in full sentences."
        )
    if not (scope_pattern or "").strip():
        raise InvalidSkillFieldError("scope_pattern required (use '**' for all)")
    if trigger not in VALID_TRIGGERS:
        raise InvalidSkillFieldError(
            f"trigger must be one of {list(VALID_TRIGGERS)}; got {trigger!r}"
        )
    if inject_as not in VALID_INJECT_AS:
        raise InvalidSkillFieldError(
            f"inject_as must be one of {list(VALID_INJECT_AS)}; got {inject_as!r}"
        )


def add_skill(
    store, *, name: str, prompt: str, scope_pattern: str,
    trigger: str = "on_edit", inject_as: str = "prelude",
    authored_by: Optional[str] = None,
    description: Optional[str] = None,
    tags: Sequence[str] = (),
) -> Dict[str, Any]:
    _validate_fields(name=name, prompt=prompt, scope_pattern=scope_pattern,
                     trigger=trigger, inject_as=inject_as)
    conn = store.conn
    if conn.execute("SELECT 1 FROM skill WHERE name=?", (name,)).fetchone():
        raise SkillNameConflictError(
            f"a skill named {name!r} already exists; use `skill edit` "
            "to modify or `skill disable` to soft-off."
        )
    skill_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO skill(id, name, prompt, scope_pattern, trigger, "
        "inject_as, authored_by, created_at, description, tags, enabled) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (skill_id, name, prompt, scope_pattern, trigger, inject_as,
         authored_by, time.time(), description,
         json.dumps(list(tags)) if tags else None),
    )
    conn.commit()
    return _row_to_dict(conn.execute(
        "SELECT * FROM skill WHERE id=?", (skill_id,)).fetchone())


def _row_to_dict(row) -> Dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    for k in ("tags", "conflicts_with", "composes_with"):
        v = d.get(k)
        if v:
            try:
                d[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    d["enabled"] = bool(d.get("enabled", 0))
    return d


def list_skills(store, *, include_disabled: bool = True) -> List[Dict[str, Any]]:
    where = "" if include_disabled else "WHERE enabled = 1"
    rows = store.conn.execute(
        f"SELECT * FROM skill {where} ORDER BY name ASC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _find_by_name(conn, name: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM skill WHERE name=?", (name,),
    ).fetchone()
    if row is None:
        raise SkillNotFoundError(f"no skill named {name!r}")
    return _row_to_dict(row)


def edit_skill(store, name: str, **fields) -> Dict[str, Any]:
    existing = _find_by_name(store.conn, name)
    allowed = {"prompt", "scope_pattern", "trigger", "inject_as",
               "description", "tags"}
    bad = set(fields) - allowed
    if bad:
        raise InvalidSkillFieldError(
            f"cannot edit fields {sorted(bad)}; allowed: {sorted(allowed)}"
        )
    merged = {**existing, **fields}
    _validate_fields(
        name=name, prompt=merged["prompt"],
        scope_pattern=merged["scope_pattern"],
        trigger=merged["trigger"], inject_as=merged["inject_as"],
    )
    sets, args = [], []
    for k, v in fields.items():
        if k == "tags":
            v = json.dumps(list(v or [])) if v is not None else None
        sets.append(f"{k}=?")
        args.append(v)
    args.append(name)
    store.conn.execute(
        f"UPDATE skill SET {', '.join(sets)} WHERE name=?", tuple(args),
    )
    store.conn.commit()
    return _find_by_name(store.conn, name)


def disable_skill(store, name: str, *, enabled: bool = False) -> Dict[str, Any]:
    existing = _find_by_name(store.conn, name)
    store.conn.execute(
        "UPDATE skill SET enabled=? WHERE name=?",
        (1 if enabled else 0, name),
    )
    store.conn.commit()
    existing["enabled"] = bool(enabled)
    return existing


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def _lifeline_for_path(conn, path: str) -> Optional[str]:
    row = conn.execute(
        "SELECT id FROM file_lifeline WHERE current_path=? "
        "AND tombstoned_at IS NULL LIMIT 1", (path,),
    ).fetchone()
    return row["id"] if row else None


def attach_skill(store, name: str, path: str) -> Dict[str, Any]:
    skill = _find_by_name(store.conn, name)
    lifeline_id = _lifeline_for_path(store.conn, path)
    if lifeline_id is None:
        raise SkillNotFoundError(
            f"no active lifeline at {path!r} to attach to"
        )
    store.conn.execute(
        "INSERT INTO skill_attachment(skill_id, lifeline_id, added_at) "
        "VALUES(?, ?, ?)",
        (skill["id"], lifeline_id, time.time()),
    )
    store.conn.commit()
    return {"skill": skill["name"], "path": path,
            "lifeline_id": lifeline_id}


def detach_skill(store, name: str, path: str) -> Dict[str, Any]:
    skill = _find_by_name(store.conn, name)
    lifeline_id = _lifeline_for_path(store.conn, path)
    if lifeline_id is None:
        return {"skill": name, "path": path, "removed": 0}
    cur = store.conn.execute(
        "DELETE FROM skill_attachment "
        "WHERE skill_id=? AND lifeline_id=?",
        (skill["id"], lifeline_id),
    )
    store.conn.commit()
    return {"skill": name, "path": path, "removed": cur.rowcount}


# ---------------------------------------------------------------------------
# Lookup — which skills apply to a path
# ---------------------------------------------------------------------------

def skills_for_path(
    store, path: str, *, trigger: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return every enabled skill whose scope_pattern matches ``path``
    or whose explicit attachment points at the path's lifeline.

    ``trigger`` filters by trigger kind (e.g. ``"on_edit"`` to surface
    only edit-time skills). When unset, returns every match — caller
    decides what to display.
    """
    out: Dict[str, Dict[str, Any]] = {}
    # Pattern-based: scan every enabled skill.
    for row in store.conn.execute(
        "SELECT * FROM skill WHERE enabled = 1 ORDER BY name ASC",
    ):
        d = _row_to_dict(row)
        if trigger is not None and d["trigger"] not in (trigger, "always"):
            continue
        if _glob_match(d["scope_pattern"], path):
            d["match_via"] = "pattern"
            out[d["name"]] = d
    # Explicit attachment via lifeline.
    lifeline_id = _lifeline_for_path(store.conn, path)
    if lifeline_id is not None:
        for row in store.conn.execute(
            "SELECT s.* FROM skill s "
            "JOIN skill_attachment sa ON sa.skill_id = s.id "
            "WHERE s.enabled = 1 AND sa.lifeline_id = ? "
            "ORDER BY s.name ASC",
            (lifeline_id,),
        ):
            d = _row_to_dict(row)
            if trigger is not None and d["trigger"] not in (trigger, "always"):
                continue
            existing = out.get(d["name"])
            if existing is not None:
                existing["match_via"] = "pattern+attached"
            else:
                d["match_via"] = "attached"
                out[d["name"]] = d
    return list(out.values())


def test_skill(store, name: str, *, path: str) -> Dict[str, Any]:
    """Preview whether a named skill would inject for the given path."""
    skill = _find_by_name(store.conn, name)
    matches_pattern = _glob_match(skill["scope_pattern"], path)
    lifeline_id = _lifeline_for_path(store.conn, path)
    attached = False
    if lifeline_id is not None:
        attached = bool(store.conn.execute(
            "SELECT 1 FROM skill_attachment "
            "WHERE skill_id=? AND lifeline_id=?",
            (skill["id"], lifeline_id),
        ).fetchone())
    would_inject = skill["enabled"] and (matches_pattern or attached)
    return {
        "skill":            skill["name"],
        "path":             path,
        "enabled":          skill["enabled"],
        "matches_pattern":  matches_pattern,
        "attached":         attached,
        "would_inject":     would_inject,
        "inject_format":    skill["inject_as"],
        "preview":          (build_skill_prelude([skill])
                              if would_inject else None),
    }


# ---------------------------------------------------------------------------
# Injection-block builder
# ---------------------------------------------------------------------------

_HEADER_BY_INJECT_AS = {
    "reminder": "💡 Skill active",
    "prelude":  "🧠 Cognitive mode",
    "system":   "⚙ Methodology override",
}


def build_skill_prelude(rows: Sequence[Dict[str, Any]]) -> str:
    """Format a list of skill rows as their per-strength block.

    Sorted ``system`` → ``prelude`` → ``reminder`` so the strongest
    instruction is the agent's first read.
    """
    if not rows:
        return ""
    order = {"system": 0, "prelude": 1, "reminder": 2}
    sorted_rows = sorted(
        rows, key=lambda r: order.get(r.get("inject_as", "prelude"), 1),
    )
    out = []
    for r in sorted_rows:
        header = _HEADER_BY_INJECT_AS.get(r.get("inject_as", "prelude"),
                                            "🧠 Skill active")
        out.append(f"{header}: {r.get('name')}")
        prompt = (r.get("prompt") or "").strip()
        out.append(prompt)
        if r.get("match_via"):
            out.append(f"(match: {r['match_via']})")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
