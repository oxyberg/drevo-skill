#!/usr/bin/env python3
"""Atomically update completed positions and exceptions in a capture package."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update capture-state.json atomically.")
    parser.add_argument("package", help="Capture package directory")
    parser.add_argument(
        "--complete", type=int, action="append", default=[], help="Mark a position complete"
    )
    parser.add_argument(
        "--exception",
        action="append",
        default=[],
        metavar="POSITION:REASON",
        help="Document a position that cannot be captured",
    )
    parser.add_argument(
        "--reopen", type=int, action="append", default=[], help="Remove completion or exception"
    )
    parser.add_argument("--zoom", help="Set the calibrated viewer zoom label")
    parser.add_argument("--pixel-ratio", type=float, help="Set the calibrated device pixel ratio")
    parser.add_argument("--viewport-width", type=int, help="Set capture viewport width")
    parser.add_argument("--viewport-height", type=int, help="Set capture viewport height")
    parser.add_argument(
        "--tile-label",
        action="append",
        dest="tile_labels",
        help="Replace the expected tile scheme; repeat for every label",
    )
    return parser.parse_args()


def positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def one_line(value: str) -> str:
    return " ".join(value.splitlines()).strip()


def main() -> int:
    args = parse_args()
    setting_actions = (
        args.zoom is not None,
        args.pixel_ratio is not None,
        args.viewport_width is not None,
        args.viewport_height is not None,
        args.tile_labels is not None,
    )
    if not args.complete and not args.exception and not args.reopen and not any(setting_actions):
        print("ERROR: specify a position or calibration update", file=sys.stderr)
        return 2

    package = Path(args.package).expanduser().resolve()
    state_path = package / "capture-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read capture-state.json: {exc}", file=sys.stderr)
        return 1
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        print("ERROR: unsupported capture-state.json schema", file=sys.stderr)
        return 1

    completed_value = state.get("completed_positions")
    exceptions_value = state.get("exceptions")
    if not isinstance(completed_value, list) or not all(
        positive_int(item) for item in completed_value
    ):
        print("ERROR: completed_positions must be a positive integer list", file=sys.stderr)
        return 1
    if not isinstance(exceptions_value, dict):
        print("ERROR: exceptions must be an object", file=sys.stderr)
        return 1

    expected = state.get("expected_positions")
    if expected is not None and not positive_int(expected):
        print("ERROR: expected_positions must be null or a positive integer", file=sys.stderr)
        return 1

    capture_settings = state.get("capture_settings")
    if not isinstance(capture_settings, dict):
        print("ERROR: capture_settings must be an object", file=sys.stderr)
        return 1
    if args.zoom is not None and not one_line(args.zoom):
        print("ERROR: --zoom must not be empty", file=sys.stderr)
        return 2
    if args.pixel_ratio is not None and args.pixel_ratio <= 0:
        print("ERROR: --pixel-ratio must be positive", file=sys.stderr)
        return 2
    viewport_values = (args.viewport_width, args.viewport_height)
    if any(value is not None for value in viewport_values) and not all(
        positive_int(value) for value in viewport_values
    ):
        print(
            "ERROR: --viewport-width and --viewport-height must be positive and used together",
            file=sys.stderr,
        )
        return 2
    if args.tile_labels is not None:
        tile_labels = [one_line(item) for item in args.tile_labels]
        if any(not item for item in tile_labels) or len(set(tile_labels)) != len(tile_labels):
            print("ERROR: tile labels must be non-empty and unique", file=sys.stderr)
            return 2
    else:
        tile_labels = None

    def validate_position(position: int) -> bool:
        if not positive_int(position):
            return False
        return expected is None or position <= expected

    completed = set(completed_value)
    exceptions: dict[str, str] = {}
    for key, reason in exceptions_value.items():
        try:
            position = int(key)
        except (TypeError, ValueError):
            position = 0
        if not validate_position(position) or not isinstance(reason, str) or not reason.strip():
            print(f"ERROR: invalid existing exception for position {key!r}", file=sys.stderr)
            return 1
        exceptions[str(position)] = reason

    for position in args.reopen:
        if not validate_position(position):
            print(f"ERROR: position outside expected range: {position}", file=sys.stderr)
            return 2
        completed.discard(position)
        exceptions.pop(str(position), None)

    for item in args.exception:
        position_text, separator, reason = item.partition(":")
        try:
            position = int(position_text)
        except ValueError:
            position = 0
        reason = " ".join(reason.splitlines()).strip()
        if not separator or not validate_position(position) or not reason:
            print(f"ERROR: invalid exception, expected POSITION:REASON: {item}", file=sys.stderr)
            return 2
        completed.discard(position)
        exceptions[str(position)] = reason

    for position in args.complete:
        if not validate_position(position):
            print(f"ERROR: position outside expected range: {position}", file=sys.stderr)
            return 2
        exceptions.pop(str(position), None)
        completed.add(position)

    if args.zoom is not None:
        capture_settings["zoom"] = one_line(args.zoom)
    if args.pixel_ratio is not None:
        capture_settings["device_pixel_ratio"] = args.pixel_ratio
    if args.viewport_width is not None:
        capture_settings["viewport"] = {
            "width": args.viewport_width,
            "height": args.viewport_height,
        }
    if tile_labels is not None:
        state["expected_tiles"] = tile_labels
    state["capture_settings"] = capture_settings

    state["completed_positions"] = sorted(completed)
    state["exceptions"] = dict(sorted(exceptions.items(), key=lambda item: int(item[0])))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".capture-state.", suffix=".tmp", dir=package
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, state_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    print(
        f"Updated capture state: {len(completed)} complete, "
        f"{len(exceptions)} documented exception(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
