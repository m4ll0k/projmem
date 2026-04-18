"""Core module — defines run() and STATUS_DONE token."""
import os
from .helpers import format_record

STATUS_DONE = "DONE"
STATUS_IN_PROGRESS = "IN_PROGRESS"


def run(payload):
    """Process a payload dict and return a record dict."""
    db_url = os.environ["DATABASE_URL"]
    record = {
        "status": STATUS_IN_PROGRESS,
        "evidence": payload.get("evidence"),
        "db": db_url,
    }
    record = format_record(record)
    record["status"] = STATUS_DONE
    return record


def _internal_helper(x):
    return x * 2
