#!/usr/bin/env python3
"""Create an empty, resumable archival image capture package."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a package for reproducible archival viewer capture."
    )
    parser.add_argument("target", help="New empty package directory")
    parser.add_argument("--archive", required=True, help="Archive or repository name")
    parser.add_argument("--reference", required=True, help="Archive reference or document title")
    parser.add_argument("--catalog-url", default="", help="Catalog card URL")
    parser.add_argument(
        "--viewer-url",
        "--source-url",
        dest="viewer_url",
        required=True,
        help="Viewer URL (--source-url remains an alias)",
    )
    parser.add_argument("--document-id", default="", help="Stable document identifier")
    parser.add_argument("--positions", type=int, help="Expected viewer position count")
    parser.add_argument("--zoom", default="", help="Viewer zoom label, for example 100%")
    parser.add_argument("--pixel-ratio", type=float, help="Device pixel ratio during capture")
    parser.add_argument("--viewport-width", type=int, help="Capture viewport width in pixels")
    parser.add_argument("--viewport-height", type=int, help="Capture viewport height in pixels")
    parser.add_argument(
        "--tile-label",
        action="append",
        dest="tile_labels",
        help="Expected tile label; repeat for each tile after viewer calibration",
    )
    return parser.parse_args()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def one_line(value: str) -> str:
    return " ".join(value.splitlines()).strip()


def main() -> int:
    args = parse_args()
    if args.positions is not None and args.positions < 1:
        print("ERROR: --positions must be a positive integer", file=sys.stderr)
        return 2

    archive = one_line(args.archive)
    reference = one_line(args.reference)
    catalog_url = one_line(args.catalog_url)
    viewer_url = one_line(args.viewer_url)
    document_id = one_line(args.document_id)
    zoom = one_line(args.zoom)
    if not archive or not reference or not viewer_url:
        print("ERROR: archive, reference, and viewer URL must not be empty", file=sys.stderr)
        return 2
    if args.pixel_ratio is not None and args.pixel_ratio <= 0:
        print("ERROR: --pixel-ratio must be positive", file=sys.stderr)
        return 2
    viewport_values = (args.viewport_width, args.viewport_height)
    if any(value is not None for value in viewport_values) and not all(
        isinstance(value, int) and value > 0 for value in viewport_values
    ):
        print(
            "ERROR: --viewport-width and --viewport-height must be positive and used together",
            file=sys.stderr,
        )
        return 2
    expected_tiles = [one_line(item) for item in (args.tile_labels or [])]
    if any(not item for item in expected_tiles) or len(set(expected_tiles)) != len(expected_tiles):
        print("ERROR: tile labels must be non-empty and unique", file=sys.stderr)
        return 2

    target = Path(args.target).expanduser()
    if target.exists():
        if not target.is_dir():
            print(f"ERROR: target exists and is not a directory: {target}", file=sys.stderr)
            return 2
        if any(target.iterdir()):
            print(f"ERROR: target directory is not empty: {target}", file=sys.stderr)
            return 2
    else:
        target.mkdir(parents=True)

    for name in (
        "исходные-кадры",
        "собранные-изображения",
        "производные",
        "контроль",
    ):
        (target / name).mkdir()

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    state = {
        "schema_version": 1,
        "created_at": created_at,
        "archive": archive,
        "reference": reference,
        "catalog_url": catalog_url,
        "viewer_url": viewer_url,
        "document_id": document_id,
        "capture_settings": {
            "zoom": zoom or None,
            "device_pixel_ratio": args.pixel_ratio,
            "viewport": (
                {"width": args.viewport_width, "height": args.viewport_height}
                if args.viewport_width is not None
                else None
            ),
        },
        "expected_positions": args.positions,
        "expected_tiles": expected_tiles,
        "completed_positions": [],
        "exceptions": {},
    }
    pages = {"schema_version": 1, "positions": []}

    write_json(target / "capture-state.json", state)
    write_json(target / "page-sizes.json", pages)
    (target / "sha256.txt").write_text("", encoding="utf-8")
    (target / "README.md").write_text(
        "\n".join(
            [
                "# Пакет фиксации архивных изображений",
                "",
                "## Происхождение",
                "",
                f"- Архив или хранилище: {archive}",
                f"- Архивный шифр или заголовок: {reference}",
                f"- URL карточки: {catalog_url or '[не указан]'}",
                f"- URL просмотрщика: {viewer_url}",
                "- Идентификатор документа: "
                f"{document_id or '[не указан]'}",
                f"- Начало фиксации: {created_at}",
                f"- Масштаб просмотрщика: {zoom or '[не откалиброван]'}",
                "- Pixel ratio: "
                f"{args.pixel_ratio if args.pixel_ratio is not None else '[не откалиброван]'}",
                "- Окно фиксации: "
                f"{args.viewport_width or '[не откалибровано]'} × "
                f"{args.viewport_height or '[не откалибровано]'}",
                "",
                "## Тип копии и ограничения",
                "",
                "Указать, получены ли исходные файлы архива или кадры просмотрщика, "
                "какой использован масштаб, что перекрывает изображение и какие позиции исключены.",
                "",
                "## Охват",
                "",
                "Указать непрерывные диапазоны, пропуски и уровень содержательной проверки. "
                "Техническую фиксацию не выдавать за расшифровку.",
                "",
                "Cookies, токены, заголовки авторизации и данные пользовательской сессии "
                "в пакет не сохранять.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"Created capture package: {target.resolve()}")
    print(f"Expected positions: {args.positions if args.positions is not None else 'unknown'}")
    print(f"Expected tiles: {', '.join(expected_tiles) if expected_tiles else 'not calibrated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
