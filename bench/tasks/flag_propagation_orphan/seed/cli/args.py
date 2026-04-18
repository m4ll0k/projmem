"""Argument parser for the mini-CLI under test."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="in.txt")
    return p


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)
