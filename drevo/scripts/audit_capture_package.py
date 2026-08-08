#!/usr/bin/env python3
"""Audit completeness and basic integrity of an archival image capture package."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check capture metadata, position coverage, tile files, and duplicates."
    )
    parser.add_argument("package", help="Capture package directory")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="validate preparation metadata without requiring captured positions",
    )
    return parser.parse_args()


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing metadata file: {path.name}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {path.name}: {exc}")
    return None


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(24)
    except OSError:
        return None
    if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
        return struct.unpack(">II", header[16:24])
    return None


def positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def covers_canvas(rectangles: list[tuple[int, int, int, int]], width: int, height: int) -> bool:
    boundaries = sorted(
        {
            0,
            width,
            *(x for x, _, _, _ in rectangles),
            *(x + w for x, _, w, _ in rectangles),
        }
    )
    for left, right in zip(boundaries, boundaries[1:]):
        if left == right or left < 0 or right > width:
            continue
        intervals = sorted(
            (y, y + h)
            for x, y, tile_width, h in rectangles
            if x <= left and x + tile_width >= right
        )
        covered_until = 0
        for top, bottom in intervals:
            if top > covered_until:
                return False
            covered_until = max(covered_until, bottom)
            if covered_until >= height:
                break
        if covered_until < height:
            return False
    return True


def main() -> int:
    args = parse_args()
    package = Path(args.package).expanduser()
    if not package.is_dir():
        print(f"ERROR: package directory does not exist: {package}", file=sys.stderr)
        return 2

    errors: list[str] = []
    warnings: list[str] = []
    state = load_json(package / "capture-state.json", errors)
    pages = load_json(package / "page-sizes.json", errors)
    if not isinstance(state, dict) or not isinstance(pages, dict):
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if state.get("schema_version") != 1:
        errors.append("capture-state.json: unsupported schema_version")
    if pages.get("schema_version") != 1:
        errors.append("page-sizes.json: unsupported schema_version")

    for name in ("archive", "reference", "viewer_url"):
        value = state.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"capture-state.json: {name} must be a non-empty string")
    for name in ("README.md", "sha256.txt"):
        if not (package / name).is_file():
            errors.append(f"missing package file: {name}")
    for name in ("исходные-кадры", "собранные-изображения", "производные", "контроль"):
        if not (package / name).is_dir():
            errors.append(f"missing package directory: {name}")

    capture_settings = state.get("capture_settings")
    if not isinstance(capture_settings, dict):
        errors.append("capture-state.json: capture_settings must be an object")
    else:
        missing_settings: list[str] = []
        zoom = capture_settings.get("zoom")
        if not isinstance(zoom, str) or not zoom:
            missing_settings.append("zoom")
        pixel_ratio = capture_settings.get("device_pixel_ratio")
        if (
            not isinstance(pixel_ratio, (int, float))
            or isinstance(pixel_ratio, bool)
            or pixel_ratio <= 0
        ):
            missing_settings.append("device_pixel_ratio")
        viewport = capture_settings.get("viewport")
        if not isinstance(viewport, dict) or not all(
            positive_int(viewport.get(key)) for key in ("width", "height")
        ):
            missing_settings.append("viewport")
        if missing_settings:
            message = "capture settings not calibrated: " + ", ".join(missing_settings)
            if args.preflight:
                warnings.append(message)
            else:
                errors.append(message)

    expected_tiles = state.get("expected_tiles")
    if not isinstance(expected_tiles, list) or not all(
        isinstance(item, str) and item for item in expected_tiles
    ):
        errors.append("capture-state.json: expected_tiles must be a string list")
        expected_tiles = []
    elif not expected_tiles:
        message = "expected tile scheme is not calibrated"
        if args.preflight:
            warnings.append(message)
        else:
            errors.append(message)
    elif len(set(expected_tiles)) != len(expected_tiles):
        errors.append("capture-state.json: expected_tiles contains duplicates")

    records = pages.get("positions")
    if not isinstance(records, list):
        errors.append("page-sizes.json: positions must be a list")
        records = []

    positions: dict[int, dict[str, Any]] = {}
    hashes: dict[str, list[tuple[int, str]]] = defaultdict(list)
    valid_positions: set[int] = set()

    for index, record in enumerate(records, 1):
        context = f"page-sizes.json position entry {index}"
        if not isinstance(record, dict):
            errors.append(f"{context}: must be an object")
            continue
        position = record.get("position")
        if not positive_int(position):
            errors.append(f"{context}: position must be a positive integer")
            continue
        if position in positions:
            errors.append(f"{context}: duplicate position {position}")
            continue
        positions[position] = record

        canvas = record.get("canvas")
        if not isinstance(canvas, dict):
            errors.append(f"position {position}: canvas must be an object")
            continue
        canvas_width = canvas.get("width")
        canvas_height = canvas.get("height")
        if not positive_int(canvas_width) or not positive_int(canvas_height):
            errors.append(f"position {position}: canvas width and height must be positive integers")
            continue

        tiles = record.get("tiles")
        if not isinstance(tiles, list):
            errors.append(f"position {position}: tiles must be a list")
            continue

        labels: set[str] = set()
        rectangles: list[tuple[int, int, int, int]] = []
        position_valid = True
        for tile_index, tile in enumerate(tiles, 1):
            tile_context = f"position {position}, tile {tile_index}"
            if not isinstance(tile, dict):
                errors.append(f"{tile_context}: must be an object")
                position_valid = False
                continue
            label = tile.get("label")
            relative_name = tile.get("path")
            x, y = tile.get("x"), tile.get("y")
            width, height = tile.get("width"), tile.get("height")
            if not isinstance(label, str) or not label:
                errors.append(f"{tile_context}: label must be a non-empty string")
                position_valid = False
                continue
            if label in labels:
                errors.append(f"position {position}: duplicate tile label {label}")
                position_valid = False
            labels.add(label)
            if not isinstance(relative_name, str) or not relative_name:
                errors.append(f"{tile_context}: path must be a non-empty string")
                position_valid = False
                continue
            if not all(
                (
                    nonnegative_int(x),
                    nonnegative_int(y),
                    positive_int(width),
                    positive_int(height),
                )
            ):
                errors.append(f"{tile_context}: invalid coordinates or dimensions")
                position_valid = False
                continue
            if x + width > canvas_width or y + height > canvas_height:
                errors.append(f"{tile_context}: tile extends outside the canvas")
                position_valid = False
            else:
                rectangles.append((x, y, width, height))

            candidate = (package / relative_name).resolve()
            try:
                candidate.relative_to(package.resolve())
            except ValueError:
                errors.append(f"{tile_context}: path escapes package: {relative_name}")
                position_valid = False
                continue
            if not candidate.is_file():
                errors.append(f"{tile_context}: missing file: {relative_name}")
                position_valid = False
                continue
            if candidate.stat().st_size == 0:
                errors.append(f"{tile_context}: empty file: {relative_name}")
                position_valid = False
                continue

            actual_size = png_size(candidate)
            if candidate.suffix.lower() == ".png" and actual_size is None:
                errors.append(f"{tile_context}: invalid PNG header: {relative_name}")
                position_valid = False
            elif actual_size is not None and actual_size != (width, height):
                errors.append(
                    f"{tile_context}: metadata size {width}x{height} differs from PNG "
                    f"size {actual_size[0]}x{actual_size[1]}"
                )
                position_valid = False
            hashes[digest(candidate)].append((position, relative_name))

        missing_labels = sorted(set(expected_tiles) - labels)
        extra_labels = sorted(labels - set(expected_tiles))
        if missing_labels:
            errors.append(
                f"position {position}: missing expected tiles: {', '.join(missing_labels)}"
            )
            position_valid = False
        if extra_labels:
            warnings.append(
                f"position {position}: unexpected tile labels: {', '.join(extra_labels)}"
            )
        if rectangles and not covers_canvas(rectangles, canvas_width, canvas_height):
            errors.append(f"position {position}: tile coordinates do not cover the full canvas")
            position_valid = False
        if position_valid:
            valid_positions.add(position)

    expected_positions = state.get("expected_positions")
    if expected_positions is None:
        warnings.append("expected position count is unknown; continuity cannot be fully checked")
    elif not positive_int(expected_positions):
        errors.append("capture-state.json: expected_positions must be null or a positive integer")
    else:
        expected_set = set(range(1, expected_positions + 1))
        exceptions = state.get("exceptions", {})
        if not isinstance(exceptions, dict):
            errors.append("capture-state.json: exceptions must be an object")
            exceptions = {}
        exception_positions: set[int] = set()
        for key, reason in exceptions.items():
            try:
                value = int(key)
            except (TypeError, ValueError):
                errors.append(f"capture-state.json: invalid exception position {key!r}")
                continue
            if value not in expected_set or not isinstance(reason, str) or not reason.strip():
                errors.append(f"capture-state.json: invalid exception for position {key!r}")
                continue
            exception_positions.add(value)
        missing = sorted(expected_set - set(positions) - exception_positions)
        unexpected = sorted(set(positions) - expected_set)
        if missing:
            message = f"missing positions without documented exception: {missing}"
            if args.preflight:
                warnings.append(message)
            else:
                errors.append(message)
        if unexpected:
            errors.append(f"positions outside expected range: {unexpected}")

    completed = state.get("completed_positions")
    if not isinstance(completed, list) or not all(positive_int(item) for item in completed):
        errors.append("capture-state.json: completed_positions must be a positive integer list")
        completed_set: set[int] = set()
    else:
        completed_set = set(completed)
        if len(completed_set) != len(completed):
            errors.append("capture-state.json: completed_positions contains duplicates")
    invalid_completed = sorted(completed_set - valid_positions)
    unmarked_valid = sorted(valid_positions - completed_set)
    if invalid_completed:
        errors.append(f"positions marked complete but failing audit: {invalid_completed}")
    if unmarked_valid:
        warnings.append(f"valid positions not marked complete: {unmarked_valid}")

    for matches in hashes.values():
        distinct_positions = {position for position, _ in matches}
        if len(distinct_positions) > 1:
            rendered = ", ".join(f"{position}:{name}" for position, name in matches)
            warnings.append(f"exact duplicate image across positions: {rendered}")

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(
        f"Capture summary: {len(positions)} metadata position(s), "
        f"{len(valid_positions)} valid, "
        f"{sum(len(items) for items in hashes.values())} tile file(s)."
    )
    if errors:
        print(f"Capture audit failed with {len(errors)} error(s).", file=sys.stderr)
        return 1
    print("Capture preflight passed." if args.preflight else "Capture audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
