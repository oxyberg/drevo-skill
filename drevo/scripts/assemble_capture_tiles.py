#!/usr/bin/env python3
"""Assemble archival viewer tiles using recorded canvas coordinates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assemble lossless PNG images without modifying source tiles."
    )
    parser.add_argument("package", help="Capture package directory")
    parser.add_argument(
        "--position", type=int, action="append", help="Position to assemble; repeat as needed"
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing assemblies")
    return parser.parse_args()


def load_positions(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"{path.name}: unsupported schema")
    positions = value.get("positions")
    if not isinstance(positions, list) or not all(isinstance(item, dict) for item in positions):
        raise ValueError(f"{path.name}: positions must be a list of objects")
    return positions


def main() -> int:
    args = parse_args()
    package = Path(args.package).expanduser().resolve()
    if not package.is_dir():
        print(f"ERROR: package directory does not exist: {package}", file=sys.stderr)
        return 2

    try:
        from PIL import Image
    except ImportError:
        print(
            "ERROR: Pillow is required for assembly; install it in the local Python environment",
            file=sys.stderr,
        )
        return 2

    try:
        records = load_positions(package / "page-sizes.json")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    by_position: dict[int, dict[str, Any]] = {}
    for record in records:
        position = record.get("position")
        if not isinstance(position, int) or isinstance(position, bool) or position < 1:
            print("ERROR: every position must be a positive integer", file=sys.stderr)
            return 1
        if position in by_position:
            print(f"ERROR: duplicate position {position}", file=sys.stderr)
            return 1
        by_position[position] = record

    selected = sorted(set(args.position or by_position))
    missing = [position for position in selected if position not in by_position]
    if missing:
        print(f"ERROR: positions missing from metadata: {missing}", file=sys.stderr)
        return 1

    output_dir = package / "собранные-изображения"
    output_dir.mkdir(exist_ok=True)
    assembled = 0

    for position in selected:
        record = by_position[position]
        canvas = record.get("canvas")
        tiles = record.get("tiles")
        if not isinstance(canvas, dict) or not isinstance(tiles, list) or not tiles:
            print(f"ERROR: position {position} has invalid canvas or tiles", file=sys.stderr)
            return 1
        width, height = canvas.get("width"), canvas.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
            print(f"ERROR: position {position} has invalid canvas size", file=sys.stderr)
            return 1

        destination = output_dir / f"позиция-{position:04d}.png"
        if destination.exists() and not args.overwrite:
            print(f"ERROR: output exists; use --overwrite: {destination}", file=sys.stderr)
            return 1

        result = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        for tile in tiles:
            if not isinstance(tile, dict):
                print(f"ERROR: position {position} contains an invalid tile", file=sys.stderr)
                return 1
            relative_name = tile.get("path")
            x, y = tile.get("x"), tile.get("y")
            recorded_width, recorded_height = tile.get("width"), tile.get("height")
            if (
                not isinstance(relative_name, str)
                or not isinstance(x, int)
                or not isinstance(y, int)
            ):
                print(
                    f"ERROR: position {position} contains incomplete tile metadata",
                    file=sys.stderr,
                )
                return 1
            source = (package / relative_name).resolve()
            try:
                source.relative_to(package)
            except ValueError:
                print(f"ERROR: tile path escapes package: {relative_name}", file=sys.stderr)
                return 1
            if not source.is_file():
                print(f"ERROR: missing tile: {relative_name}", file=sys.stderr)
                return 1
            with Image.open(source) as opened:
                opened.load()
                if opened.size != (recorded_width, recorded_height):
                    print(
                        f"ERROR: position {position} tile size differs from metadata: "
                        f"{relative_name}",
                        file=sys.stderr,
                    )
                    return 1
                tile_image = opened.convert("RGBA")
            if x < 0 or y < 0 or x + tile_image.width > width or y + tile_image.height > height:
                print(
                    f"ERROR: position {position} tile exceeds canvas: {relative_name}",
                    file=sys.stderr,
                )
                return 1
            result.alpha_composite(tile_image, (x, y))

        result.save(destination, format="PNG", optimize=False)
        print(f"Assembled position {position}: {destination}")
        assembled += 1

    print(f"Assembled {assembled} position(s). Source tiles were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
