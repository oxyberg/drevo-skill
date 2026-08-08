#!/usr/bin/env python3
"""Verify sha256.txt manifests below an evidence directory."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


ENTRY_RE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify all sha256.txt manifests recursively.")
    parser.add_argument("root", help="Evidence directory or a single sha256.txt file")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail when a package contains files not listed in its sha256.txt",
    )
    return parser.parse_args()


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    if root.is_file():
        manifests = [root]
    elif root.is_dir():
        manifests = sorted(root.rglob("sha256.txt"))
    else:
        print(f"ERROR: path does not exist: {root}", file=sys.stderr)
        return 2

    if not manifests:
        message = f"No sha256.txt manifests found below {root}."
        if args.strict:
            print(f"ERROR: {message}", file=sys.stderr)
            return 1
        print(message)
        return 0

    errors: list[str] = []
    checked = 0

    for manifest in manifests:
        base = manifest.parent.resolve()
        listed: set[Path] = set()
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{manifest}: cannot read manifest: {exc}")
            continue

        for number, line in enumerate(lines, 1):
            if not line or line.startswith("#"):
                continue
            match = ENTRY_RE.match(line)
            if not match:
                errors.append(f"{manifest}:{number}: invalid manifest entry")
                continue

            expected, relative_name = match.groups()
            candidate = (manifest.parent / relative_name).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                errors.append(f"{manifest}:{number}: path escapes package: {relative_name}")
                continue
            if candidate.name == "sha256.txt":
                errors.append(f"{manifest}:{number}: manifest must not list itself")
                continue
            relative_path = candidate.relative_to(base)
            if relative_path in listed:
                errors.append(f"{manifest}:{number}: duplicate entry: {relative_name}")
                continue
            listed.add(relative_path)
            if not candidate.is_file():
                errors.append(f"{manifest}:{number}: missing file: {relative_name}")
                continue

            actual = digest(candidate)
            checked += 1
            if actual.lower() != expected.lower():
                errors.append(
                    f"{manifest}:{number}: checksum mismatch for {relative_name}"
                )

        if args.strict:
            present: set[Path] = set()
            for path in manifest.parent.rglob("*"):
                if not path.is_file() or path.resolve() == manifest.resolve():
                    continue
                try:
                    present.add(path.resolve().relative_to(base))
                except ValueError:
                    errors.append(f"{manifest}: file escapes package: {path}")
            for relative_path in sorted(present - listed, key=lambda item: item.as_posix()):
                errors.append(f"{manifest}: unlisted file: {relative_path.as_posix()}")

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print(f"Checksum verification failed with {len(errors)} error(s).", file=sys.stderr)
        return 1

    print(f"Verified {checked} file(s) in {len(manifests)} manifest(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
