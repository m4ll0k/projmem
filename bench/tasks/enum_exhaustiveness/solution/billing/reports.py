"""Summary reports — must be extended when a new state is added."""
from __future__ import annotations

from billing.status import Status


def summarise(counts: dict[Status, int]) -> str:
    parts = [
        f"pending={counts.get(Status.PENDING, 0)}",
        f"paid={counts.get(Status.PAID, 0)}",
        f"failed={counts.get(Status.FAILED, 0)}",
        f"refunded={counts.get(Status.REFUNDED, 0)}",
    ]
    return ", ".join(parts)
