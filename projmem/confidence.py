"""Confidence aggregation utilities."""
from __future__ import annotations
from typing import Iterable, Dict

ORDER = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
INV = {v: k for k, v in ORDER.items()}


def min_conf(values: Iterable[str]) -> str:
    vals = [ORDER.get(v, 0) for v in values]
    if not vals:
        return "unknown"
    return INV[min(vals)]


def summarize(conf_counts: Dict[str, int]) -> Dict[str, object]:
    total = sum(conf_counts.values()) or 1
    return {
        "by_level": conf_counts,
        "total": total,
        "dominant": max(conf_counts, key=conf_counts.get) if conf_counts else "unknown",
    }
