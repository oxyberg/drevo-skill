#!/usr/bin/env python3
"""Atomically claim one eligible capture job with a renewable lease."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from _batch_utils import (
    BatchError,
    TECHNICAL_ID_RE,
    atomic_write_json,
    batch_lock,
    domain_limit,
    expire_leases,
    format_time,
    load_state,
    one_line,
    resolve_package,
    state_path,
    touch_state,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Claim one pending job without exceeding batch or domain concurrency.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("batch", help="Capture batch directory")
    parser.add_argument("--worker-id", required=True, help="Generic non-personal worker ID")
    parser.add_argument("--lease-seconds", type=int, help="Override the batch lease duration")
    parser.add_argument(
        "--domain",
        action="append",
        default=[],
        help="Restrict claims to this domain; repeat to allow several",
    )
    parser.add_argument("--json", action="store_true", help="Print one machine-readable record")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worker_id = one_line(args.worker_id)
    if not TECHNICAL_ID_RE.fullmatch(worker_id):
        print(
            "ERROR: worker ID must contain 1..64 ASCII letters, digits, "
            "dots, underscores, or hyphens",
            file=sys.stderr,
        )
        return 2
    if args.lease_seconds is not None and args.lease_seconds < 1:
        print("ERROR: --lease-seconds must be positive", file=sys.stderr)
        return 2
    normalized_domains = [one_line(item).rstrip(".").lower() for item in args.domain]
    allowed_domains = set(normalized_domains)
    if any(not item for item in normalized_domains) or len(allowed_domains) != len(args.domain):
        print("ERROR: --domain values must be non-empty and unique", file=sys.stderr)
        return 2

    batch = Path(args.batch).expanduser().resolve()
    try:
        with batch_lock(batch):
            state = load_state(batch)
            now = utc_now()
            expired = expire_leases(state, now)
            for job in state["jobs"]:
                package = resolve_package(batch, job["package"])
                if not package.is_dir():
                    raise BatchError(f"job {job['id']}: package directory does not exist")
            running = [job for job in state["jobs"] if job["state"] == "running"]
            domain_counts = Counter(job["domain"] for job in running)
            claimed = None
            if len(running) < state["max_workers"]:
                candidates = sorted(
                    (job for job in state["jobs"] if job["state"] == "pending"),
                    key=lambda job: (-job["priority"], job["id"]),
                )
                for job in candidates:
                    if allowed_domains and job["domain"] not in allowed_domains:
                        continue
                    if domain_counts[job["domain"]] >= domain_limit(state, job["domain"]):
                        continue
                    lease_seconds = args.lease_seconds or state["default_lease_seconds"]
                    job["state"] = "running"
                    job["attempt"] += 1
                    job["worker_id"] = worker_id
                    job["claimed_at"] = format_time(now)
                    job["heartbeat_at"] = format_time(now)
                    job["lease_expires_at"] = format_time(
                        now + timedelta(seconds=lease_seconds)
                    )
                    job["finished_at"] = None
                    job["last_error"] = None
                    claimed = job
                    break
            if expired or claimed is not None:
                touch_state(state, now)
                atomic_write_json(state_path(batch), state)
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if claimed is None:
        if args.json:
            print(json.dumps({"claimed": False}, ensure_ascii=False))
        else:
            print("NO_JOB")
        return 3
    result = {
        "claimed": True,
        "job_id": claimed["id"],
        "package": claimed["package"],
        "domain": claimed["domain"],
        "attempt": claimed["attempt"],
        "lease_expires_at": claimed["lease_expires_at"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            f"CLAIMED {claimed['id']} {claimed['package']} "
            f"lease_expires_at={claimed['lease_expires_at']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
