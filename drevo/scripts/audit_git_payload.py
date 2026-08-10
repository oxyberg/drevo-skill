#!/usr/bin/env python3
"""Reject unexpectedly large staged payloads in a Drevo project."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "mode": "minimal",
    "max_staged_file_mib": 10,
    "max_staged_total_mib": 50,
    "max_staged_evidence_payload_files": 25,
}

PAYLOAD_SUFFIXES = {
    ".7z",
    ".avi",
    ".bmp",
    ".gz",
    ".heic",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".rar",
    ".tif",
    ".tiff",
    ".warc",
    ".wav",
    ".webp",
    ".zip",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit staged Git payload size using the Drevo storage policy."
    )
    parser.add_argument(
        "--repository",
        default=".",
        help="Git worktree root or a path inside it (default: current directory)",
    )
    parser.add_argument(
        "--all-tracked",
        action="store_true",
        help="audit every tracked file instead of only staged additions and changes",
    )
    return parser.parse_args()


def run_git(repository: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError("git executable was not found") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(message or "git command failed") from exc
    return result.stdout


def repository_root(path: Path) -> Path:
    output = run_git(path, "rev-parse", "--show-toplevel")
    return Path(output.decode("utf-8", errors="surrogateescape").strip())


def positive_number(policy: dict[str, Any], key: str) -> float:
    value = policy.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"storage policy field {key!r} must be a positive number")
    return float(value)


def positive_integer(policy: dict[str, Any], key: str) -> int:
    value = policy.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"storage policy field {key!r} must be a positive integer")
    return value


def load_policy(root: Path) -> dict[str, Any]:
    path = root / "research" / "evidence" / "storage-policy.json"
    if not path.is_file():
        print(
            f"WARNING: {path.relative_to(root)} is missing; safe defaults are used.",
            file=sys.stderr,
        )
        return dict(DEFAULT_POLICY)
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read storage policy: {exc}") from exc
    if not isinstance(policy, dict):
        raise ValueError("storage policy must be a JSON object")
    if policy.get("schema_version") != 1:
        raise ValueError("storage policy schema_version must be 1")
    if policy.get("mode") not in {"minimal", "full-repository"}:
        raise ValueError("storage policy mode must be minimal or full-repository")
    positive_number(policy, "max_staged_file_mib")
    positive_number(policy, "max_staged_total_mib")
    positive_integer(policy, "max_staged_evidence_payload_files")
    return policy


def tracked_paths(root: Path, all_tracked: bool) -> list[str]:
    if all_tracked:
        output = run_git(root, "ls-files", "-z")
    else:
        output = run_git(
            root,
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    ]


def index_blob_size(root: Path, path: str) -> int:
    output = run_git(root, "ls-files", "--stage", "-z", "--", path)
    for entry in output.split(b"\0"):
        if not entry or b"\t" not in entry:
            continue
        metadata, _ = entry.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) == 3 and fields[2] == b"0":
            return int(run_git(root, "cat-file", "-s", fields[1].decode("ascii")))
    raise ValueError(f"cannot resolve staged blob for {path}")


def is_evidence_payload(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("research/evidence/") and Path(normalized).suffix.lower() in PAYLOAD_SUFFIXES


def format_mib(size: int) -> str:
    return f"{size / (1024 * 1024):.2f} MiB"


def audit(root: Path, policy: dict[str, Any], all_tracked: bool) -> int:
    paths = tracked_paths(root, all_tracked)
    sizes = [(path, index_blob_size(root, path)) for path in paths]
    total = sum(size for _, size in sizes)
    evidence_payloads = [(path, size) for path, size in sizes if is_evidence_payload(path)]

    if policy["mode"] == "full-repository":
        print(
            "WARNING: full-repository mode is enabled; Drevo payload limits are not enforced."
        )
        print(
            f"Audited {len(sizes)} tracked blobs, {format_mib(total)} total, "
            f"{len(evidence_payloads)} evidence payload files."
        )
        return 0

    max_file = int(positive_number(policy, "max_staged_file_mib") * 1024 * 1024)
    max_total = int(positive_number(policy, "max_staged_total_mib") * 1024 * 1024)
    max_payloads = positive_integer(policy, "max_staged_evidence_payload_files")
    errors: list[str] = []

    for path, size in sizes:
        if size > max_file:
            errors.append(
                f"file exceeds {format_mib(max_file)}: {path} ({format_mib(size)})"
            )
    if total > max_total:
        errors.append(
            f"staged total exceeds {format_mib(max_total)}: {format_mib(total)}"
        )
    if len(evidence_payloads) > max_payloads:
        errors.append(
            "staged evidence payload count exceeds "
            f"{max_payloads}: {len(evidence_payloads)}"
        )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(
            "Keep full scans in cold storage. Switch to full-repository mode only "
            "after an explicit user request and a storage/privacy review.",
            file=sys.stderr,
        )
        return 1

    scope = "tracked" if all_tracked else "staged"
    print(
        f"Git payload audit passed: {len(sizes)} {scope} blobs, "
        f"{format_mib(total)} total, {len(evidence_payloads)} evidence payload files."
    )
    return 0


def main() -> int:
    args = parse_args()
    try:
        root = repository_root(Path(args.repository).expanduser())
        policy = load_policy(root)
        return audit(root, policy, args.all_tracked)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
