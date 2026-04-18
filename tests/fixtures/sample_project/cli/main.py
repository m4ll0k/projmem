"""Entrypoint: argparse CLI with --live-logs and --verbose flags."""
import argparse
from src.core import run


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--live-logs", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--new-flag", default=None)
    return p


def main():
    args = build_parser().parse_args()
    if args.live_logs:
        print("live logs on")
    # Dynamic call: getattr-style dispatch NOT resolved by static analysis.
    handler_name = "run"
    handler = globals().get(handler_name)
    return handler({"evidence": ["seed"]})


if __name__ == "__main__":
    main()
