#!/usr/bin/env python3
"""Write a deterministic sha256.txt for every file in one evidence package."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace a package sha256.txt with hashes of all regular files."
    )
    parser.add_argument("package", help="Single evidence package directory")
    return parser.parse_args()


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> int:
    args = parse_args()
    package = Path(args.package).expanduser().resolve()
    if not package.is_dir():
        print(f"ERROR: package directory does not exist: {package}", file=sys.stderr)
        return 2

    manifest = package / "sha256.txt"
    files: list[tuple[str, Path]] = []
    for path in package.rglob("*"):
        if not path.is_file() or path.resolve() == manifest:
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(package).as_posix()
        except ValueError:
            print(f"ERROR: file escapes package: {path}", file=sys.stderr)
            return 1
        files.append((relative, resolved))
    files.sort(key=lambda item: item[0])

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sha256.", suffix=".tmp", dir=package
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for relative, path in files:
                stream.write(f"{digest(path)}  ./{relative}\n")
        os.replace(temporary_name, manifest)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    print(f"Wrote {len(files)} checksum(s): {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
