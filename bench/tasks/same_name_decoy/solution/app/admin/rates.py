"""Admin rates — internal tool, different domain. Do NOT mix with billing."""


def compute_rate(quota_bucket: str) -> float:
    if quota_bucket == "priority":
        return 0.50
    return 0.30
