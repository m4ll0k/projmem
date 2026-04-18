"""State transitions. Any new enum member MUST get a branch here."""
from __future__ import annotations

from billing.status import Status


def advance(s: Status) -> Status:
    if s is Status.PENDING:
        return Status.PAID
    if s is Status.PAID:
        return Status.PAID  # terminal success
    if s is Status.FAILED:
        return Status.FAILED  # terminal failure
    raise ValueError(f"unknown state: {s!r}")
