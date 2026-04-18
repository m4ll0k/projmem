import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.billing.rates import compute_rate as billing_rate


def test_default_customer():
    assert billing_rate("standard") == 0.10


def test_enterprise_customer():
    assert billing_rate("enterprise") == 0.25
