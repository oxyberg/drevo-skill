#!/usr/bin/env python3
"""Create reproducible, non-generative image derivatives with a manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import (
        Image,
        ImageEnhance,
        ImageFilter,
        ImageMath,
        ImageOps,
        __version__ as PILLOW_VERSION,
    )
except ImportError:  # pragma: no cover - exercised by environments without Pillow
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


MODES = ("gray", "normalize", "red", "green", "blue")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create lossless reading derivatives without changing source images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="Image files or non-recursive directories")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--mode",
        action="append",
        choices=MODES,
        help="Derivative to create; repeat as needed (default: gray and normalize)",
    )
    parser.add_argument(
        "--binary-threshold",
        action="append",
        type=int,
        default=[],
        help="Create a black-and-white derivative at 0..255; repeat as needed",
    )
    parser.add_argument("--autocontrast-cutoff", type=float, default=0.5)
    parser.add_argument("--contrast", type=float, default=1.5)
    parser.add_argument("--unsharp-radius", type=float, default=1.0)
    parser.add_argument("--unsharp-percent", type=int, default=150)
    parser.add_argument("--unsharp-threshold", type=int, default=2)
    parser.add_argument("--background-radius", type=float, default=32.0)
    parser.add_argument(
        "--redact-source-names",
        action="store_true",
        help="Omit input basenames from output filenames and the manifest",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace only files planned by this run"
    )
    return parser.parse_args()


def enhance_gray(gray: Image.Image, args: argparse.Namespace) -> Image.Image:
    result = ImageOps.autocontrast(gray, cutoff=args.autocontrast_cutoff)
    result = ImageEnhance.Contrast(result).enhance(args.contrast)
    return result.filter(
        ImageFilter.UnsharpMask(
            radius=args.unsharp_radius,
            percent=args.unsharp_percent,
            threshold=args.unsharp_threshold,
        )
    )


def normalize_background(gray: Image.Image, args: argparse.Namespace) -> Image.Image:
    background = gray.filter(ImageFilter.GaussianBlur(args.background_radius))
    divided = ImageMath.lambda_eval(
        lambda values: values["image"] * 255 / (values["background"] + 1),
        image=gray,
        background=background,
    ).convert("L")
    return enhance_gray(divided, args)


def main() -> int:
    args = parse_args()
    modes = list(dict.fromkeys(args.mode or ["gray", "normalize"]))
    thresholds = list(dict.fromkeys(args.binary_threshold))
    if not 0 <= args.autocontrast_cutoff < 50:
        print("ERROR: --autocontrast-cutoff must be in [0, 50)", file=sys.stderr)
        return 2
    if args.contrast <= 0 or args.unsharp_radius < 0 or args.background_radius <= 0:
        print("ERROR: contrast and radii must be positive", file=sys.stderr)
        return 2
    if any(value < 0 or value > 255 for value in thresholds):
        print("ERROR: binary thresholds must be in 0..255", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir).expanduser()
    try:
        ensure_output_directory_separate(args.inputs, output_dir)
        sources = expand_image_inputs(args.inputs)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    manifest_path = output_dir / "derivatives-manifest.json"
    planned: list[Path] = [manifest_path]
    source_plans: list[tuple[int, Path, str, list[tuple[str, Path]]]] = []
    for index, source in enumerate(sources, start=1):
        digest = sha256_file(source)
        key = source_key(index, source, digest, args.redact_source_names)
        outputs = [(mode, output_dir / f"{key}__{mode}.png") for mode in modes]
        outputs.extend(
            (f"binary-{threshold}", output_dir / f"{key}__binary-{threshold}.png")
            for threshold in thresholds
        )
        source_plans.append((index, source, digest, outputs))
        planned.extend(path for _, path in outputs)
    try:
        ensure_outputs_available(planned, args.overwrite)
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    records: list[dict[str, object]] = []
    try:
        for index, source, digest, outputs in source_plans:
            with Image.open(source) as opened:
                if getattr(opened, "n_frames", 1) != 1:
                    raise ValueError(f"multi-frame image is not supported: {source.name}")
                rgb = ImageOps.exif_transpose(opened).convert("RGB")
            gray = rgb.convert("L")
            enhanced = enhance_gray(gray, args)
            generated: list[dict[str, object]] = []
            for kind, output in outputs:
                if kind == "gray":
                    derivative = enhanced
                elif kind == "normalize":
                    derivative = normalize_background(gray, args)
                elif kind in {"red", "green", "blue"}:
                    derivative = enhance_gray(rgb.getchannel(kind[0].upper()), args)
                else:
                    threshold = int(kind.split("-", 1)[1])
                    derivative = gray.point(
                        lambda value, t=threshold: 255 if value >= t else 0
                    )
                atomic_save_image(derivative, output)
                generated.append(
                    {
                        "kind": kind,
                        "file": output.name,
                        "sha256": sha256_file(output),
                        "width": derivative.width,
                        "height": derivative.height,
                    }
                )
            records.append(
                {
                    **source_identity(index, source, digest, args.redact_source_names),
                    "source_width": rgb.width,
                    "source_height": rgb.height,
                    "outputs": generated,
                }
            )
    except Exception as exc:
        print(f"ERROR: derivative creation failed: {exc}", file=sys.stderr)
        return 1

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "tool": "create_image_derivatives.py",
        "pillow_version": PILLOW_VERSION,
        "parameters": {
            "modes": modes,
            "binary_thresholds": thresholds,
            "autocontrast_cutoff": args.autocontrast_cutoff,
            "contrast": args.contrast,
            "unsharp_radius": args.unsharp_radius,
            "unsharp_percent": args.unsharp_percent,
            "unsharp_threshold": args.unsharp_threshold,
            "background_radius": args.background_radius,
            "source_names_redacted": args.redact_source_names,
        },
        "sources": records,
        "classification": "reading derivatives; not archival originals",
    }
    atomic_write_json(manifest_path, manifest)
    print(f"Created {sum(len(record['outputs']) for record in records)} derivative(s)")
    print(f"Manifest: {manifest_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
