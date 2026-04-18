"""Vendor rates — what WE pay third parties. Do NOT mix with billing."""


def compute_rate(vendor_tier: str) -> float:
    if vendor_tier == "gold":
        return 0.80
    return 0.60
