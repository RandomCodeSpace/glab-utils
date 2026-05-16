#!/usr/bin/env python3
"""Run token-rotate quality gates using only standard-library test execution.

Coverage is measured with coverage.py when available. The production script does
not depend on coverage.py; CI installs it only for quality reporting.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run tests and enforce coverage threshold.")
    parser.add_argument("--min-coverage", type=float, default=95.0)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    source_file = root / "gitlab_project_token_rotator.py"

    try:
        import coverage  # type: ignore
    except ImportError:
        print("ERROR: coverage.py is required for the coverage quality gate.", file=sys.stderr)
        print("Install for development with: python3 -m pip install coverage", file=sys.stderr)
        return 2

    cov = coverage.Coverage(
        source=[str(root)],
        omit=[str(root / "test_*.py"), str(root / "quality_gate.py")],
        branch=True,
    )
    cov.start()
    suite = unittest.defaultTestLoader.discover(str(root), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    cov.stop()
    cov.save()

    percent = cov.report(include=[str(source_file)], show_missing=True)
    print(f"Coverage quality gate: required >= {args.min_coverage:.2f}%, actual = {percent:.2f}%")

    if not result.wasSuccessful():
        return 1
    if percent < args.min_coverage:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
