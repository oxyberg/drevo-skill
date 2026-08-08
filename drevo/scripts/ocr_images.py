#!/usr/bin/env python3
"""Run local Tesseract OCR for navigation and save text, TSV, and provenance."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _evidence_utils import (
    atomic_write_bytes,
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
        description="Create local OCR navigation indexes with Tesseract.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="Image files or non-recursive directories")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--language",
        default="rus",
        help="Tesseract language(s), for example rus or rus+eng",
    )
    parser.add_argument("--psm", type=int, default=6, help="Tesseract page segmentation mode")
    parser.add_argument("--oem", type=int, choices=(0, 1, 2, 3), help="Tesseract OCR engine mode")
    parser.add_argument("--user-words", help="Optional local Tesseract user-words file")
    parser.add_argument(
        "--timeout", type=float, default=300.0, help="Timeout per OCR invocation"
    )
    parser.add_argument(
        "--redact-source-names",
        action="store_true",
        help="Omit input basenames from output filenames and the manifest",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace only files planned by this run"
    )
    return parser.parse_args()


def tesseract_version(binary: str) -> str:
    result = subprocess.run(
        [binary, "--version"], capture_output=True, check=True, timeout=15
    )
    return result.stdout.decode("utf-8", errors="replace").splitlines()[0].strip()


def run_ocr(binary: str, source: Path, args: argparse.Namespace, output_kind: str) -> bytes:
    command = [
        binary,
        str(source),
        "stdout",
        "-l",
        args.language,
        "--psm",
        str(args.psm),
    ]
    if args.oem is not None:
        command.extend(["--oem", str(args.oem)])
    if args.user_words:
        command.extend(["--user-words", str(Path(args.user_words).expanduser())])
    if output_kind == "tsv":
        command.append("tsv")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Tesseract exited with {result.returncode}: {detail}")
    return result.stdout


def main() -> int:
    args = parse_args()
    if args.psm < 0 or args.psm > 13 or args.timeout <= 0:
        print("ERROR: --psm must be in 0..13 and --timeout must be positive", file=sys.stderr)
        return 2
    binary = shutil.which("tesseract")
    if not binary:
        print("ERROR: local Tesseract executable was not found", file=sys.stderr)
        return 2
    user_words = Path(args.user_words).expanduser() if args.user_words else None
    if user_words is not None and not user_words.is_file():
        print("ERROR: --user-words file does not exist", file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir).expanduser()
    try:
        ensure_output_directory_separate(args.inputs, output_dir)
        sources = expand_image_inputs(args.inputs)
        version = tesseract_version(binary)
    except (FileNotFoundError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    manifest_path = output_dir / "ocr-manifest.json"
    plans: list[tuple[int, Path, str, Path, Path]] = []
    planned = [manifest_path]
    for index, source in enumerate(sources, start=1):
        digest = sha256_file(source)
        key = source_key(index, source, digest, args.redact_source_names)
        text_path = output_dir / f"{key}.ocr.txt"
        tsv_path = output_dir / f"{key}.ocr.tsv"
        plans.append((index, source, digest, text_path, tsv_path))
        planned.extend((text_path, tsv_path))
    try:
        ensure_outputs_available(planned, args.overwrite)
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    records: list[dict[str, object]] = []
    try:
        for index, source, digest, text_path, tsv_path in plans:
            text_data = run_ocr(binary, source, args, "text")
            tsv_data = run_ocr(binary, source, args, "tsv")
            atomic_write_bytes(text_path, text_data)
            atomic_write_bytes(tsv_path, tsv_data)
            records.append(
                {
                    **source_identity(index, source, digest, args.redact_source_names),
                    "text_file": text_path.name,
                    "text_sha256": sha256_file(text_path),
                    "tsv_file": tsv_path.name,
                    "tsv_sha256": sha256_file(tsv_path),
                }
            )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: OCR failed: {exc}", file=sys.stderr)
        return 1

    parameters: dict[str, object] = {
        "engine": "tesseract",
        "engine_version": version,
        "language": args.language,
        "psm": args.psm,
        "oem": args.oem,
        "source_names_redacted": args.redact_source_names,
    }
    if user_words is not None:
        parameters["user_words_file"] = user_words.name
        parameters["user_words_sha256"] = sha256_file(user_words)
    atomic_write_json(
        manifest_path,
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "tool": "ocr_images.py",
            "parameters": parameters,
            "sources": records,
            "classification": (
                "machine-generated navigation index; verify every claim against the image"
            ),
        },
    )
    print(f"Created OCR indexes for {len(records)} image(s)")
    print(f"Manifest: {manifest_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
