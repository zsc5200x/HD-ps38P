#!/usr/bin/env python3
"""Run the project's cross-platform lint and test checks."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
CHECKS = (
    ("Python lint", "-m", "ruff", "check", "."),
    ("Unit tests", "-m", "unittest", "discover", "-s", "tests", "-v"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="include local Chromium regressions (requires an installed browser)",
    )
    args = parser.parse_args()
    environment = os.environ.copy()
    if args.browser:
        environment["PS38_TEST_BROWSER"] = "1"
        browser_dir = PROJECT_DIR / ".playwright-browsers"
        if browser_dir.is_dir():
            environment.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
    else:
        environment.pop("PS38_TEST_BROWSER", None)

    for title, *arguments in CHECKS:
        print(f"\n== {title} ==", flush=True)
        result = subprocess.run(
            [sys.executable, *arguments], cwd=PROJECT_DIR, env=environment, check=False
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
