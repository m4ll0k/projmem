"""Billing-state enum."""
from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    PENDING = "pending"
    PAID    = "paid"
    FAILED  = "failed"
