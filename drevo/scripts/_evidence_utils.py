"""Shared helpers for deterministic Drevo evidence tools."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def atomic_save_image(image: Any, path: Path, image_format: str = "PNG", **options: Any) -> None:
    """Save a Pillow image atomically without making Pillow a helper dependency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    try:
        image.save(temporary_name, format=image_format, **options)
        with open(temporary_name, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def ensure_outputs_available(paths: Iterable[Path], overwrite: bool) -> None:
    if overwrite:
        return
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("output already exists: " + ", ".join(existing))


def ensure_output_directory_separate(inputs: Iterable[str], output_dir: Path) -> None:
    """Prevent a directory input from ingesting outputs on a later run."""
    resolved_output = output_dir.resolve()
    for value in inputs:
        candidate = Path(value).expanduser()
        if candidate.is_dir() and candidate.resolve() == resolved_output:
            raise ValueError("output directory must differ from every input directory")


def source_key(index: int, path: Path, digest: str, redact_name: bool) -> str:
    prefix = f"source-{index:04d}" if redact_name else f"{index:04d}__{path.stem}"
    return f"{prefix}__{digest[:12]}"


def source_identity(index: int, path: Path, digest: str, redact_name: bool) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_index": index,
        "source_ref": source_key(index, path, digest, redact_name),
        "source_sha256": digest,
    }
    if redact_name:
        record["source_name_redacted"] = True
    else:
        record["source_file"] = path.name
    return record


def expand_image_inputs(values: Iterable[str]) -> list[Path]:
    images: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = Path(value).expanduser()
        candidates = (
            sorted(
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
            )
            if path.is_dir()
            else [path]
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            if not resolved.is_file():
                raise FileNotFoundError(f"input image does not exist: {candidate}")
            if resolved.suffix.lower() not in IMAGE_SUFFIXES:
                raise ValueError(f"unsupported image suffix: {candidate}")
            seen.add(resolved)
            images.append(resolved)
    if not images:
        raise ValueError("no input images found")
    return images
