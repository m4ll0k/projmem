"""v2.1 skill scaffold — empty tables for path-scoped cognitive instructions.

Lands the storage shape from ``docs/v2-design.md`` Pillar 3.5 so the
v2.1 release ships as a feature flip, not a migration. The CLI verbs
(``projmem skill add | list | attach | detach | edit | disable |
test``) are NOT wired in v2.0; this migration only creates the empty
tables + indexes the eventual verbs will write to.

  * skill              — the reusable cognitive-instruction object
                         (name, prompt body, scope glob, trigger
                         enum, inject_as enum, tags, enabled flag).
  * skill_attachment   — explicit per-lifeline pins (the override
                         path; pattern-based attaches live entirely
                         in ``skill.scope_pattern``).

Both tables are empty until v2.1's authoring verbs ship; the
``editing`` response in v2.0 still has no ``skills[]`` field.
Adding the field is a v2.1 code change, gated by the presence of at
least one enabled row in ``skill``.
"""
from __future__ import annotations

import sqlite3


VERSION = 4
NAME = "skill-scaffold"


_UP_SQL = """
CREATE TABLE IF NOT EXISTS skill (
    id            TEXT PRIMARY KEY,        -- uuid
    name          TEXT UNIQUE NOT NULL,    -- "lateral-thinking"
    prompt        TEXT NOT NULL,           -- injection body
    scope_pattern TEXT NOT NULL,           -- glob "src/payment/**"
    trigger       TEXT NOT NULL DEFAULT 'on_edit',
                                            -- on_edit|on_read|on_create|always
    inject_as     TEXT NOT NULL DEFAULT 'prelude',
                                            -- reminder|prelude|system
    authored_by   TEXT,
    created_at    REAL NOT NULL,
    description   TEXT,
    tags          TEXT,                    -- JSON array
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- Two future-proofing nullables from the v2.1 wow-list:
    conflicts_with TEXT,                   -- JSON array of skill names
    composes_with  TEXT                    -- JSON array of skill names
);

CREATE INDEX IF NOT EXISTS idx_skill_enabled
    ON skill(enabled);
CREATE INDEX IF NOT EXISTS idx_skill_scope
    ON skill(scope_pattern);

CREATE TABLE IF NOT EXISTS skill_attachment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id      TEXT NOT NULL,
    lifeline_id   TEXT,                    -- nullable: null = pattern-based
    added_at      REAL NOT NULL,
    FOREIGN KEY(skill_id)    REFERENCES skill(id),
    FOREIGN KEY(lifeline_id) REFERENCES file_lifeline(id)
);

CREATE INDEX IF NOT EXISTS idx_skill_attach_skill
    ON skill_attachment(skill_id);
CREATE INDEX IF NOT EXISTS idx_skill_attach_lifeline
    ON skill_attachment(lifeline_id);
"""

_DOWN_SQL = """
DROP INDEX IF EXISTS idx_skill_attach_lifeline;
DROP INDEX IF EXISTS idx_skill_attach_skill;
DROP TABLE IF EXISTS skill_attachment;

DROP INDEX IF EXISTS idx_skill_scope;
DROP INDEX IF EXISTS idx_skill_enabled;
DROP TABLE IF EXISTS skill;
"""


def _exec_script(conn: sqlite3.Connection, sql: str) -> None:
    for stmt in sql.split(";"):
        s = "\n".join(line for line in stmt.splitlines()
                      if line.strip() and not line.strip().startswith("--"))
        s = s.strip()
        if s:
            conn.execute(s)


def up(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _UP_SQL)


def down(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _DOWN_SQL)
