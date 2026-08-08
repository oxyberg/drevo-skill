#!/usr/bin/env python3
"""Heartbeat or transition one capture batch job under an atomic lock."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from _batch_utils import (
    BatchError,
    TECHNICAL_ID_RE,
    atomic_write_json,
    batch_lock,
    clear_lease,
    expire_leases,
    find_job,
    format_time,
    load_state,
    one_line,
    state_path,
    touch_state,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Renew, finish, fail, release, or retry one capture job.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("batch", help="Capture batch directory")
    parser.add_argument("--job", required=True, help="Job ID")
    parser.add_argument("--worker-id", help="Current generic worker ID")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--heartbeat", action="store_true", help="Renew a running lease")
    action.add_argument("--captured", action="store_true", help="Mark capture complete")
    action.add_argument("--fail", metavar="REASON", help="Mark the job failed")
    action.add_argument("--release", action="store_true", help="Return the job to pending")
    action.add_argument("--retry", action="store_true", help="Return a failed job to pending")
    parser.add_argument("--lease-seconds", type=int, help="Heartbeat lease duration override")
    return parser.parse_args()


def require_owner(job: dict, worker_id: str) -> None:
    if job["state"] != "running":
        raise BatchError(f"job {job['id']} is not running")
    if job.get("worker_id") != worker_id:
        raise BatchError(f"job {job['id']} is owned by another worker")


def main() -> int:
    args = parse_args()
    job_id = one_line(args.job)
    worker_id = one_line(args.worker_id or "")
    if not job_id:
        print("ERROR: --job must not be empty", file=sys.stderr)
        return 2
    if not args.retry and not TECHNICAL_ID_RE.fullmatch(worker_id):
        print("ERROR: a valid --worker-id is required for this action", file=sys.stderr)
        return 2
    if args.retry and args.worker_id is not None:
        print("ERROR: --worker-id is not used with --retry", file=sys.stderr)
        return 2
    if args.lease_seconds is not None and args.lease_seconds < 1:
        print("ERROR: --lease-seconds must be positive", file=sys.stderr)
        return 2
    if args.lease_seconds is not None and not args.heartbeat:
        print("ERROR: --lease-seconds is only valid with --heartbeat", file=sys.stderr)
        return 2
    reason = one_line(args.fail or "")
    if args.fail is not None and not reason:
        print("ERROR: --fail reason must not be empty", file=sys.stderr)
        return 2
    if len(reason) > 500:
        print("ERROR: --fail reason must not exceed 500 characters", file=sys.stderr)
        return 2

    batch = Path(args.batch).expanduser().resolve()
    try:
        with batch_lock(batch):
            state = load_state(batch)
            now = utc_now()
            expire_leases(state, now)
            job = find_job(state, job_id)
            if args.retry:
                if job["state"] != "failed":
                    raise BatchError(f"job {job_id} is not failed")
                job["state"] = "pending"
                job["finished_at"] = None
                clear_lease(job)
                action = "RETRIED"
            else:
                require_owner(job, worker_id)
                if args.heartbeat:
                    lease_seconds = args.lease_seconds or state["default_lease_seconds"]
                    job["heartbeat_at"] = format_time(now)
                    job["lease_expires_at"] = format_time(
                        now + timedelta(seconds=lease_seconds)
                    )
                    action = "HEARTBEAT"
                elif args.captured:
                    job["state"] = "captured"
                    job["finished_at"] = format_time(now)
                    job["last_error"] = None
                    clear_lease(job)
                    action = "CAPTURED"
                elif args.fail is not None:
                    job["state"] = "failed"
                    job["finished_at"] = format_time(now)
                    job["last_error"] = reason
                    clear_lease(job)
                    action = "FAILED"
                else:
                    job["state"] = "pending"
                    job["finished_at"] = None
                    job["last_error"] = "released"
                    clear_lease(job)
                    action = "RELEASED"
            touch_state(state, now)
            atomic_write_json(state_path(batch), state)
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"{action} {job_id} state={job['state']}")
    if action == "HEARTBEAT":
        print(f"lease_expires_at={job['lease_expires_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
