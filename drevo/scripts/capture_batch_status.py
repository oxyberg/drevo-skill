#!/usr/bin/env python3
"""Report capture batch progress without changing leases or queue state."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from _batch_utils import BatchError, load_state, parse_time, utc_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show capture batch jobs and state counts.")
    parser.add_argument("batch", help="Capture batch directory")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch = Path(args.batch).expanduser().resolve()
    try:
        state = load_state(batch)
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    now = utc_now()
    jobs = []
    expired = []
    for job in state["jobs"]:
        lease_expired = False
        if job["state"] == "running":
            lease_expired = parse_time(
                job["lease_expires_at"], f"job {job['id']}.lease_expires_at"
            ) <= now
            if lease_expired:
                expired.append(job["id"])
        jobs.append(
            {
                "id": job["id"],
                "package": job["package"],
                "domain": job["domain"],
                "state": job["state"],
                "priority": job["priority"],
                "attempt": job["attempt"],
                "worker_id": job["worker_id"],
                "lease_expires_at": job["lease_expires_at"],
                "lease_expired": lease_expired,
                "last_error": job["last_error"],
            }
        )
    counts = dict(sorted(Counter(job["state"] for job in state["jobs"]).items()))
    result = {
        "counts": counts,
        "max_workers": state["max_workers"],
        "max_per_domain": state["max_per_domain"],
        "expired_running": expired,
        "jobs": jobs,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        rendered_counts = ", ".join(f"{name}={count}" for name, count in counts.items())
        print(f"BATCH {rendered_counts}")
        for job in jobs:
            worker = f" worker={job['worker_id']}" if job["worker_id"] else ""
            expired_label = " EXPIRED" if job["lease_expired"] else ""
            print(
                f"{job['state'].upper()} {job['id']} {job['package']} "
                f"domain={job['domain']}{worker}{expired_label}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
