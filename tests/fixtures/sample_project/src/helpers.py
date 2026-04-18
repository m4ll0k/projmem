"""Helpers — format_record consumes schema fields status/evidence."""


def format_record(rec):
    rec.setdefault("status", "IN_PROGRESS")
    if not rec.get("evidence"):
        rec["evidence"] = []
    return rec


def unused_helper():
    return 1
