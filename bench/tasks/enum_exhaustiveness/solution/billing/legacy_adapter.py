"""Legacy adapter — freezes the ORIGINAL 3-state enum for wire
compatibility with v1 webhook consumers. Do NOT extend this with
new states; v1 consumers would fail to parse them."""
from __future__ import annotations


def to_wire_v1(state_name: str) -> str:
    if state_name not in ("PENDING", "PAID", "FAILED"):
        raise ValueError(f"v1 wire cannot represent {state_name}")
    return state_name.lower()
