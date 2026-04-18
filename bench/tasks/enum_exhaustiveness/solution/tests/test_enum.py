import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from billing.status import Status
from billing.workflow import advance
from billing.reports import summarise


def test_advance_pending_to_paid():
    assert advance(Status.PENDING) is Status.PAID


def test_summarise_baseline_states():
    s = summarise({Status.PENDING: 1, Status.PAID: 2, Status.FAILED: 3})
    assert "pending=1" in s and "paid=2" in s and "failed=3" in s


def test_advance_refunded_is_terminal():
    assert advance(Status.REFUNDED) is Status.REFUNDED


def test_summarise_reports_refunded():
    s = summarise({Status.PENDING: 0, Status.PAID: 0,
                   Status.FAILED: 0, Status.REFUNDED: 5})
    assert "refunded=5" in s
