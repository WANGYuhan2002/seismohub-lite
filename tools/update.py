#!/usr/bin/env python3
"""Refresh the SeismoHub Lite snapshot in one go, stopping before publishing.

    python3 tools/update.py --db <path-to>/seismo_hub.db --env-file <path-to>/.env [--env-file …]

Steps, in order. It stops at the first failure:
  1. tools/export_lite.py    read-only export of whitelisted fields
  2. tools/check_site.py     end-to-end check in headless Chrome
  3. tools/privacy_scan.py   must report RESULT: CLEAN
  4. shows `git status` and `git diff --stat` for review

It never commits or pushes. Publishing stays a manual, reviewed step:
look at the changes, then `git commit` and `git push` yourself.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent


def run(label: str, cmd: list[str]) -> bool:
    print(f"\n=== {label} ===", flush=True)
    return subprocess.run(cmd, cwd=REPO).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Export, check and scan SeismoHub Lite (never publishes)")
    ap.add_argument("--db", required=True, help="path to seismo_hub.db (opened read-only)")
    ap.add_argument("--env-file", action="append", default=[], help="env file whose values must not leak (repeatable)")
    ap.add_argument("--skip-browser-check", action="store_true", help="skip the headless-Chrome check")
    args = ap.parse_args()

    py = sys.executable
    scan = [py, str(TOOLS / "privacy_scan.py"), "--db", args.db]
    for env in args.env_file:
        scan += ["--env-file", env]
    steps = [("1/3 export", [py, str(TOOLS / "export_lite.py"), "--db", args.db])]
    if not args.skip_browser_check:
        steps.append(("2/3 page check", [py, str(TOOLS / "check_site.py")]))
    steps.append(("3/3 privacy scan", scan))

    for label, cmd in steps:
        if not run(label, cmd):
            print(f"\nSTOPPED at {label}. Fix the problem above; nothing was committed or pushed.")
            return 1

    print("\n=== changes to review ===", flush=True)
    subprocess.run(["git", "status", "--short"], cwd=REPO)
    subprocess.run(["git", "diff", "--stat"], cwd=REPO)
    print("\nAll checks passed. Review the changes above, then commit and push yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
