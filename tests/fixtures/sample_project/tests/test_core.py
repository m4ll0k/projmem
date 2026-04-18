from src.core import run


def test_run_returns_status_done():
    import os; os.environ["DATABASE_URL"] = "sqlite://"
    r = run({"evidence": ["x"]})
    assert r["status"] == "DONE"
