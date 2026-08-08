#!/usr/bin/env python3
"""Register independent capture packages in one resumable batch queue."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from _batch_utils import (
    BatchError,
    SCHEMA_VERSION,
    TECHNICAL_ID_RE,
    atomic_write_json,
    format_time,
    resolve_package,
    state_path,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a queue over existing independent capture packages.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("batch", help="Batch directory containing the packages")
    parser.add_argument(
        "--job",
        action="append",
        required=True,
        metavar="ID=PACKAGE",
        help="Stable job ID and package path relative to the batch; repeat for every case",
    )
    parser.add_argument("--lease-seconds", type=int, default=1800)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument(
        "--max-per-domain",
        type=int,
        default=1,
        help="Concurrent running jobs allowed for one archive domain",
    )
    parser.add_argument(
        "--domain-limit",
        action="append",
        default=[],
        metavar="HOST=COUNT",
        help="Override the concurrency limit for one domain",
    )
    parser.add_argument(
        "--priority",
        action="append",
        default=[],
        metavar="ID=NUMBER",
        help="Set a higher-first integer priority for one job",
    )
    return parser.parse_args()


def parse_assignment(value: str, label: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    key, item = key.strip(), item.strip()
    if not separator or not key or not item:
        raise BatchError(f"invalid {label}, expected KEY=VALUE: {value}")
    return key, item


def load_capture_state(package: Path) -> dict[str, Any]:
    path = package / "capture-state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise BatchError(f"unsupported capture-state.json schema: {path}")
    return value


def archive_domain(capture_state: dict[str, Any], job_id: str) -> str:
    viewer_url = capture_state.get("viewer_url")
    if not isinstance(viewer_url, str):
        raise BatchError(f"job {job_id}: viewer_url must be a string")
    try:
        hostname = urllib.parse.urlsplit(viewer_url).hostname
    except ValueError as exc:
        raise BatchError(f"job {job_id}: invalid viewer_url") from exc
    if not hostname:
        raise BatchError(f"job {job_id}: viewer_url must contain a host")
    return hostname.rstrip(".").lower()


def main() -> int:
    args = parse_args()
    for name, value in (
        ("--lease-seconds", args.lease_seconds),
        ("--max-workers", args.max_workers),
        ("--max-per-domain", args.max_per_domain),
    ):
        if value < 1:
            print(f"ERROR: {name} must be positive", file=sys.stderr)
            return 2

    batch = Path(args.batch).expanduser().resolve()
    batch.mkdir(parents=True, exist_ok=True)
    if state_path(batch).exists():
        print(f"ERROR: batch state already exists: {state_path(batch)}", file=sys.stderr)
        return 2

    try:
        domain_limits: dict[str, int] = {}
        for assignment in args.domain_limit:
            host, count_text = parse_assignment(assignment, "--domain-limit")
            host = host.rstrip(".").lower()
            try:
                count = int(count_text)
            except ValueError as exc:
                raise BatchError(f"invalid domain concurrency count: {count_text}") from exc
            if count < 1 or host in domain_limits:
                raise BatchError("domain limits must be positive and unique")
            domain_limits[host] = count

        priorities: dict[str, int] = {}
        for assignment in args.priority:
            job_id, priority_text = parse_assignment(assignment, "--priority")
            if job_id in priorities:
                raise BatchError(f"duplicate priority for job: {job_id}")
            try:
                priorities[job_id] = int(priority_text)
            except ValueError as exc:
                raise BatchError(f"invalid priority integer: {priority_text}") from exc

        jobs = []
        seen_ids: set[str] = set()
        seen_packages: set[str] = set()
        for assignment in args.job:
            job_id, relative_name = parse_assignment(assignment, "--job")
            if not TECHNICAL_ID_RE.fullmatch(job_id):
                raise BatchError(
                    "job ID must contain 1..64 ASCII letters, digits, dots, underscores, or hyphens"
                )
            if job_id in seen_ids:
                raise BatchError(f"duplicate job ID: {job_id}")
            package = resolve_package(batch, relative_name)
            if not package.is_dir():
                raise BatchError(f"job {job_id}: package directory does not exist: {package}")
            normalized_package = package.relative_to(batch).as_posix()
            if normalized_package in seen_packages:
                raise BatchError(f"duplicate package path: {normalized_package}")
            capture_state = load_capture_state(package)
            jobs.append(
                {
                    "id": job_id,
                    "package": normalized_package,
                    "domain": archive_domain(capture_state, job_id),
                    "state": "pending",
                    "priority": priorities.pop(job_id, 0),
                    "attempt": 0,
                    "worker_id": None,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "lease_expires_at": None,
                    "finished_at": None,
                    "last_error": None,
                }
            )
            seen_ids.add(job_id)
            seen_packages.add(normalized_package)
        if priorities:
            raise BatchError(f"priority references unknown job: {sorted(priorities)[0]}")
        unknown_domains = set(domain_limits) - {job["domain"] for job in jobs}
        if unknown_domains:
            raise BatchError(
                f"domain limit references unknown job domain: {sorted(unknown_domains)[0]}"
            )
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    now = utc_now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "created_at": format_time(now),
        "updated_at": format_time(now),
        "default_lease_seconds": args.lease_seconds,
        "max_workers": args.max_workers,
        "max_per_domain": args.max_per_domain,
        "domain_limits": domain_limits,
        "jobs": jobs,
    }
    atomic_write_json(state_path(batch), state)
    print(f"Created capture batch with {len(jobs)} job(s): {state_path(batch)}")
    for job in jobs:
        print(f"PENDING {job['id']} {job['package']} domain={job['domain']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
