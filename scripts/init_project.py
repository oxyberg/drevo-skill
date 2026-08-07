#!/usr/bin/env python3
"""Create an empty Drevo genealogy project without initializing Git."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an evidence-based family tree project from the bundled template."
    )
    parser.add_argument("target", help="New project directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template = Path(__file__).resolve().parent.parent / "assets" / "project-template"
    target = Path(args.target).expanduser()

    if not template.is_dir():
        print(f"Template is missing: {template}", file=sys.stderr)
        return 2

    if target.exists():
        if not target.is_dir():
            print(f"Target exists and is not a directory: {target}", file=sys.stderr)
            return 2
        if any(target.iterdir()):
            print(f"Target directory is not empty: {target}", file=sys.stderr)
            return 2
    else:
        target.mkdir(parents=True)

    for source in template.iterdir():
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)

    print(f"Created Drevo project: {target.resolve()}")
    print("Git was not initialized.")
    print(f"Canonical GEDCOM: {(target / 'family-tree.ged').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

