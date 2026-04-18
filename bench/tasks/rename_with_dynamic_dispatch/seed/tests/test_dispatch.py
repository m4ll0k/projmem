import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.registry.dispatch import dispatch


def test_dispatch_compute_price():
    # Old name — must be updated after the rename.
    assert dispatch("compute_price", 4) == 5.0
