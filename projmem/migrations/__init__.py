"""Versioned schema migrations.

Pattern: each migration is a Python module under this package with the
filename shape ``mNNN_<short_name>.py`` where ``NNN`` is a zero-padded
integer. Modules define:

  VERSION : int     — strictly increasing across migrations.
  NAME    : str     — short human-readable label, written to ``meta``.
  up(conn)          — apply schema change. Idempotent if possible.
  down(conn)        — roll back the change. Used in tests + emergency.

The runner stamps the applied version into the ``meta`` table under the
key ``schema_version`` (single int = highest applied). Migrations only
run if their VERSION is greater than the current stamp.

The pre-v2 ad-hoc ALTER list in :class:`projmem.store.Store` is left
untouched — those add columns that pre-v2 databases may be missing and
are idempotent. They run BEFORE the versioned migrations so v1 schema
shape is fully present when versioned migrations execute.
"""
from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from typing import List, Tuple


META_KEY = "schema_version"


def _discover() -> List[Tuple[int, str, "object"]]:
    """Return a list of ``(version, name, module)`` for every migration.

    Sorted by version ascending. Missing version/name attributes raise
    so a malformed migration file is caught at import time rather than
    silently skipped.
    """
    out: List[Tuple[int, str, object]] = []
    pkg = __name__
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("m"):
            continue
        mod = importlib.import_module(f"{pkg}.{info.name}")
        try:
            version = int(getattr(mod, "VERSION"))
            name = str(getattr(mod, "NAME"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"migration {info.name} missing VERSION/NAME: {exc}") from exc
        if not hasattr(mod, "up") or not hasattr(mod, "down"):
            raise RuntimeError(
                f"migration {info.name} must define up(conn) and down(conn)")
        out.append((version, name, mod))
    out.sort(key=lambda row: row[0])
    versions = [v for v, _, _ in out]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return out


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied version, or 0 if the table is fresh."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (META_KEY, str(version)),
    )


def apply_pending(conn: sqlite3.Connection) -> List[int]:
    """Apply every migration with version > current_version.

    Returns the list of versions applied (empty if already up to date).
    Each migration runs inside a savepoint so partial failure rolls back
    cleanly. The runner commits the version stamp only after `up()` has
    returned without raising.
    """
    applied: List[int] = []
    cur = current_version(conn)
    for version, name, mod in _discover():
        if version <= cur:
            continue
        savepoint = f"mig_{version}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            mod.up(conn)
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        _set_version(conn, version)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        applied.append(version)
    conn.commit()
    return applied


def rollback_to(conn: sqlite3.Connection, target_version: int) -> List[int]:
    """Roll back applied migrations until ``current_version <= target_version``.

    Returns the list of versions rolled back (highest-first). Used by
    tests and by an emergency `projmem migrate --down` admin verb. Does
    nothing if already at or below the target.
    """
    rolled: List[int] = []
    pending = list(_discover())
    cur = current_version(conn)
    # Walk highest-first.
    for version, name, mod in reversed(pending):
        if version <= target_version:
            break
        if version > cur:
            continue
        savepoint = f"mig_down_{version}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            mod.down(conn)
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        # Stamp the new highest-applied version.
        applied_below = [v for v, _, _ in pending if v < version and v <= cur]
        _set_version(conn, max(applied_below) if applied_below else 0)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        rolled.append(version)
        cur = max(applied_below) if applied_below else 0
    conn.commit()
    return rolled


def list_migrations() -> List[Tuple[int, str]]:
    """Inspection helper for tests + admin output."""
    return [(v, n) for v, n, _ in _discover()]
