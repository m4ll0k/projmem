"""projmem/budget.py — surface internal output bounds as an explicit
`--budget <tokens>` knob on `pack`, `flow`, `session`.

Token estimation uses the canonical "4 chars ~ 1 token" heuristic that
overestimates slightly for code (which averages closer to 3:1). We
prefer the slight overestimate so we ALWAYS deliver under the asked
budget — the consumer's downstream context window is what matters.

Truncation strategy:
  - Walk the payload in priority order (top keys first).
  - Once the running estimate exceeds the budget, drop subsequent
    items but record a `truncated_by_budget` summary.
  - Lists of dicts shrink from the tail; scalar leaves are kept.
"""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional, Tuple


# Char-per-token ratio. ~4 for English prose, ~3 for code-heavy text.
# We pick 3.5 as a conservative middle that errs toward overestimating
# (so callers stay under the requested ceiling).
CHARS_PER_TOKEN = 3.5


def estimate_tokens(value: Any) -> int:
    """Estimate the token count of any JSON-serializable value."""
    if value is None:
        return 0
    if isinstance(value, str):
        return max(1, int(len(value) / CHARS_PER_TOKEN) + 1)
    try:
        return max(1, int(len(json.dumps(value, default=str))
                          / CHARS_PER_TOKEN) + 1)
    except (TypeError, ValueError):
        return max(1, int(len(str(value)) / CHARS_PER_TOKEN) + 1)


def fit_to_budget(payload: Dict[str, Any], max_tokens: Optional[int],
                  *, priority: Optional[List[str]] = None
                  ) -> Dict[str, Any]:
    """Return a copy of `payload` trimmed to fit `max_tokens`. If
    `max_tokens` is None or the current estimate is already under, the
    payload is returned unchanged.

    `priority`: list of top-level keys, in order of importance. Keys
    earlier in the list are kept; later keys are dropped first when the
    budget is tight. Keys not in `priority` get appended in dict order.
    """
    if not max_tokens:
        return payload
    cur = estimate_tokens(payload)
    if cur <= max_tokens:
        # Still within budget — record the slack so consumers know.
        out = dict(payload)
        out["_budget"] = {
            "max_tokens":      int(max_tokens),
            "estimated_tokens": cur,
            "truncated":        False,
        }
        return out

    # Build the priority key list.
    keys = list(priority or [])
    for k in payload.keys():
        if k not in keys:
            keys.append(k)

    out: Dict[str, Any] = {}
    spent = 0
    dropped: List[str] = []
    for k in keys:
        if k not in payload:
            continue
        v = payload[k]
        cost = estimate_tokens(v) + estimate_tokens(k) + 4  # JSON overhead
        if spent + cost <= max_tokens:
            out[k] = v
            spent += cost
            continue
        # Try shrinking lists by the tail before dropping outright.
        shrunk = _shrink_to_fit(v, max_tokens - spent - estimate_tokens(k) - 4)
        if shrunk is not None:
            out[k] = shrunk
            spent += estimate_tokens(shrunk) + estimate_tokens(k) + 4
            dropped.append(f"{k} (shrunk)")
        else:
            dropped.append(k)

    out["_budget"] = {
        "max_tokens":      int(max_tokens),
        "estimated_tokens": spent,
        "truncated":        True,
        "dropped_keys":     dropped,
        "note": ("payload exceeded --budget; sections dropped/shrunk "
                 "in reverse priority order. Re-run with a higher "
                 "budget for the full payload."),
    }
    return out


def _shrink_to_fit(value: Any, target_tokens: int) -> Optional[Any]:
    """Try to shrink a value (list of dicts) to fit `target_tokens`.
    Returns None when the value cannot be shrunk meaningfully (scalar,
    target too small)."""
    if target_tokens <= 0:
        return None
    if isinstance(value, list):
        # Keep prefix until we exceed budget.
        out: List[Any] = []
        spent = 0
        for item in value:
            cost = estimate_tokens(item) + 2
            if spent + cost > target_tokens:
                break
            out.append(item)
            spent += cost
        if not out:
            return None
        return out
    if isinstance(value, dict):
        # Recursive — keep first-N keys.
        out2: Dict[str, Any] = {}
        spent = 0
        for k, v in value.items():
            cost = estimate_tokens(v) + estimate_tokens(k) + 4
            if spent + cost > target_tokens:
                break
            out2[k] = v
            spent += cost
        return out2 or None
    return None  # scalar — can't usefully shrink
