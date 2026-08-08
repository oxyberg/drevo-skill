#!/usr/bin/env python3
"""Build a deterministic contact sheet for navigation and record its inputs."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, __version__ as PILLOW_VERSION
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
        description="Create a labeled navigation contact sheet.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="Image files or non-recursive directories")
    parser.add_argument("--output", required=True, help="Output PNG path")
    parser.add_argument("--manifest", help="Manifest JSON path")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--thumb-width", type=int, default=360)
    parser.add_argument("--thumb-height", type=int, default=260)
    parser.add_argument("--padding", type=int, default=12)
    parser.add_argument("--label-height", type=int, default=28)
    parser.add_argument("--max-images", type=int, default=1000)
    parser.add_argument("--max-pixels", type=int, default=250_000_000)
    parser.add_argument("--no-labels", action="store_true")
    parser.add_argument(
        "--redact-source-names",
        action="store_true",
        help="Omit input basenames from labels and the manifest",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace only files planned by this run"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    numeric = (
        args.columns,
        args.thumb_width,
        args.thumb_height,
        args.max_images,
        args.max_pixels,
    )
    if any(value < 1 for value in numeric) or args.padding < 0 or args.label_height < 0:
        print("ERROR: dimensions must be positive and padding non-negative", file=sys.stderr)
        return 2
    output = Path(args.output).expanduser()
    if output.suffix.lower() != ".png":
        print("ERROR: --output must use the .png suffix", file=sys.stderr)
        return 2
    try:
        ensure_output_directory_separate(args.inputs, output.parent)
        sources = expand_image_inputs(args.inputs)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if len(sources) > args.max_images:
        print(
            f"ERROR: found {len(sources)} images, above --max-images {args.max_images}",
            file=sys.stderr,
        )
        return 2

    manifest = Path(args.manifest).expanduser() if args.manifest else output.with_suffix(".json")
    if output.resolve() == manifest.resolve():
        print("ERROR: output and manifest paths must differ", file=sys.stderr)
        return 2
    try:
        ensure_outputs_available((output, manifest), args.overwrite)
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    label_height = 0 if args.no_labels else args.label_height
    cell_width = args.thumb_width + 2 * args.padding
    cell_height = args.thumb_height + label_height + 2 * args.padding
    columns = min(args.columns, len(sources))
    rows = math.ceil(len(sources) / columns)
    total_pixels = columns * cell_width * rows * cell_height
    if total_pixels > args.max_pixels:
        print(
            f"ERROR: contact sheet would exceed --max-pixels {args.max_pixels}",
            file=sys.stderr,
        )
        return 2
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    records: list[dict[str, object]] = []
    try:
        for index, source in enumerate(sources):
            row, column = divmod(index, columns)
            cell_left, cell_top = column * cell_width, row * cell_height
            with Image.open(source) as opened:
                if getattr(opened, "n_frames", 1) != 1:
                    raise ValueError(f"multi-frame image is not supported: {source.name}")
                oriented = ImageOps.exif_transpose(opened).convert("RGB")
            thumbnail = ImageOps.contain(oriented, (args.thumb_width, args.thumb_height))
            image_left = cell_left + args.padding + (args.thumb_width - thumbnail.width) // 2
            image_top = cell_top + args.padding + (args.thumb_height - thumbnail.height) // 2
            sheet.paste(thumbnail, (image_left, image_top))
            draw.rectangle(
                (cell_left, cell_top, cell_left + cell_width - 1, cell_top + cell_height - 1),
                outline="#b0b0b0",
                width=1,
            )
            digest = sha256_file(source)
            source_number = index + 1
            ref = source_key(source_number, source, digest, args.redact_source_names)
            if not args.no_labels:
                label_name = ref if args.redact_source_names else source.name
                label = f"{source_number}: {label_name}"
                max_chars = max(8, args.thumb_width // 7)
                if len(label) > max_chars:
                    label = label[: max_chars - 1] + "…"
                draw.text(
                    (cell_left + args.padding, cell_top + args.padding + args.thumb_height + 6),
                    label,
                    fill="black",
                    font=font,
                )
            records.append(
                {
                    **source_identity(
                        source_number, source, digest, args.redact_source_names
                    ),
                    "cell": {
                        "left": cell_left,
                        "top": cell_top,
                        "right": cell_left + cell_width,
                        "bottom": cell_top + cell_height,
                    },
                }
            )
        atomic_save_image(sheet, output)
    except Exception as exc:
        print(f"ERROR: contact sheet creation failed: {exc}", file=sys.stderr)
        return 1

    atomic_write_json(
        manifest,
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "tool": "make_contact_sheet.py",
            "pillow_version": PILLOW_VERSION,
            "output_file": output.name,
            "output_sha256": sha256_file(output),
            "width": sheet.width,
            "height": sheet.height,
            "parameters": {
                "columns": columns,
                "thumb_width": args.thumb_width,
                "thumb_height": args.thumb_height,
                "padding": args.padding,
                "label_height": label_height,
                "max_pixels": args.max_pixels,
                "source_names_redacted": args.redact_source_names,
            },
            "images": records,
            "classification": "navigation derivative; not an archival original",
        },
    )
    print(f"Created contact sheet with {len(sources)} image(s): {output.resolve()}")
    print(f"Manifest: {manifest.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
