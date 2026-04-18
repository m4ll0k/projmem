"""Billing rates — production customer-facing path."""


def compute_rate(customer_tag: str) -> float:
    if customer_tag == "enterprise":
        return 0.15   # BUG: should be 0.25
    return 0.10
