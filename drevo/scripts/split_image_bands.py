#!/usr/bin/env python3
"""Split images into overlapping bands and record exact source coordinates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageOps, __version__ as PILLOW_VERSION
except ImportError:  # pragma: no cover
    print("ERROR: Pillow is required: python3 -m pip install Pillow", file=sys.stderr)
    raise SystemExit(2)

from _evidence_utils import (
    atomic_save_image,
    atomic_write_json,
    ensure_output_directory_separate,
    ensure_outputs_available,
    expand_image_inputs,
    sha256_file,
    source_identity,
    source_key,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create lossless overlapping reading bands.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="Image files or non-recursive directories")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--axis", choices=("horizontal", "vertical"), default="horizontal")
    parser.add_argument("--size", type=int, default=800, help="Band height or width in pixels")
    parser.add_argument("--overlap", type=int, default=200, help="Overlap in pixels")
    parser.add_argument("--max-bands", type=int, default=10000)
    parser.add_argument("--max-output-pixels", type=int, default=1_000_000_000)
    parser.add_argument(
        "--redact-source-names",
        action="store_true",
        help="Omit input basenames from output filenames and the manifest",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace only files planned by this run"
    )
    return parser.parse_args()


def starts_for(length: int, size: int, overlap: int) -> list[int]:
    if length <= size:
        return [0]
    step = size - overlap
    starts = list(range(0, length - size + 1, step))
    last = length - size
    if starts[-1] != last:
        starts.append(last)
    return starts


def main() -> int:
    args = parse_args()
    if args.size < 1 or args.overlap < 0 or args.overlap >= args.size:
        print("ERROR: require size > overlap >= 0", file=sys.stderr)
        return 2
    if args.max_bands < 1 or args.max_output_pixels < 1:
        print("ERROR: output safety limits must be positive", file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir).expanduser()
    try:
        ensure_output_directory_separate(args.inputs, output_dir)
        sources = expand_image_inputs(args.inputs)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    manifest_path = output_dir / "bands-manifest.json"
    plans: list[
        tuple[int, Path, str, tuple[int, int], list[tuple[tuple[int, int, int, int], Path]]]
    ] = []
    planned = [manifest_path]
    try:
        total_pixels = 0
        for index, source in enumerate(sources, start=1):
            digest = sha256_file(source)
            with Image.open(source) as opened:
                if getattr(opened, "n_frames", 1) != 1:
                    raise ValueError(f"multi-frame image is not supported: {source.name}")
                width, height = ImageOps.exif_transpose(opened).size
            length = height if args.axis == "horizontal" else width
            bands = []
            for start in starts_for(length, args.size, args.overlap):
                end = min(start + args.size, length)
                bounds = (
                    (0, start, width, end)
                    if args.axis == "horizontal"
                    else (start, 0, end, height)
                )
                coordinate = (
                    f"y{start:06d}-{end:06d}"
                    if args.axis == "horizontal"
                    else f"x{start:06d}-{end:06d}"
                )
                key = source_key(index, source, digest, args.redact_source_names)
                output = output_dir / f"{key}__{coordinate}.png"
                bands.append((bounds, output))
                planned.append(output)
                total_pixels += (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
            plans.append((index, source, digest, (width, height), bands))
        if len(planned) - 1 > args.max_bands:
            raise ValueError(f"planned band count exceeds --max-bands {args.max_bands}")
        if total_pixels > args.max_output_pixels:
            raise ValueError(
                f"planned output pixels exceed --max-output-pixels {args.max_output_pixels}"
            )
        ensure_outputs_available(planned, args.overwrite)
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    source_records: list[dict[str, object]] = []
    try:
        for index, source, digest, dimensions, bands in plans:
            with Image.open(source) as opened:
                if getattr(opened, "n_frames", 1) != 1:
                    raise ValueError(f"multi-frame image is not supported: {source.name}")
                image = ImageOps.exif_transpose(opened).copy()
            band_records = []
            for index, (bounds, output) in enumerate(bands, start=1):
                crop = image.crop(bounds)
                atomic_save_image(crop, output)
                band_records.append(
                    {
                        "index": index,
                        "file": output.name,
                        "bounds": {
                            "left": bounds[0],
                            "top": bounds[1],
                            "right": bounds[2],
                            "bottom": bounds[3],
                        },
                        "sha256": sha256_file(output),
                    }
                )
            source_records.append(
                {
                    **source_identity(index, source, digest, args.redact_source_names),
                    "source_width": dimensions[0],
                    "source_height": dimensions[1],
                    "bands": band_records,
                }
            )
    except Exception as exc:
        print(f"ERROR: band creation failed: {exc}", file=sys.stderr)
        return 1

    atomic_write_json(
        manifest_path,
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "tool": "split_image_bands.py",
            "pillow_version": PILLOW_VERSION,
            "parameters": {
                "axis": args.axis,
                "size": args.size,
                "overlap": args.overlap,
                "max_bands": args.max_bands,
                "max_output_pixels": args.max_output_pixels,
                "source_names_redacted": args.redact_source_names,
            },
            "sources": source_records,
            "classification": "reading derivatives; coordinates use the EXIF-oriented source image",
        },
    )
    print(f"Created {sum(len(record['bands']) for record in source_records)} band(s)")
    print(f"Manifest: {manifest_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
