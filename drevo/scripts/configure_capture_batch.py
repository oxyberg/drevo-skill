#!/usr/bin/env python3
"""Atomically tune safe concurrency limits and priorities for a capture batch."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from _batch_utils import (
    BatchError,
    TECHNICAL_ID_RE,
    atomic_write_json,
    batch_lock,
    domain_limit,
    find_job,
    load_state,
    one_line,
    state_path,
    touch_state,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Change batch concurrency limits or job priorities without recreating it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("batch", help="Capture batch directory")
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--max-per-domain", type=int)
    parser.add_argument("--lease-seconds", type=int, help="Default for future claims and heartbeats")
    parser.add_argument(
        "--domain-limit",
        action="append",
        default=[],
        metavar="HOST=COUNT",
        help="Set or replace a per-domain concurrency limit",
    )
    parser.add_argument(
        "--remove-domain-limit",
        action="append",
        default=[],
        metavar="HOST",
        help="Return a domain to the batch default",
    )
    parser.add_argument(
        "--priority",
        action="append",
        default=[],
        metavar="ID=NUMBER",
        help="Set a higher-first integer job priority",
    )
    return parser.parse_args()


def assignment(value: str, label: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    key, item = one_line(key), one_line(item)
    if not separator or not key or not item:
        raise BatchError(f"invalid {label}, expected KEY=VALUE: {value}")
    return key, item


def normalized_host(value: str) -> str:
    host = one_line(value).rstrip(".").lower()
    if not host or any(character.isspace() for character in host):
        raise BatchError(f"invalid domain hostname: {value}")
    return host


def main() -> int:
    args = parse_args()
    if not any(
        (
            args.max_workers is not None,
            args.max_per_domain is not None,
            args.lease_seconds is not None,
            args.domain_limit,
            args.remove_domain_limit,
            args.priority,
        )
    ):
        print("ERROR: specify a limit or priority update", file=sys.stderr)
        return 2
    if args.max_workers is not None and args.max_workers < 1:
        print("ERROR: --max-workers must be positive", file=sys.stderr)
        return 2
    if args.max_per_domain is not None and args.max_per_domain < 1:
        print("ERROR: --max-per-domain must be positive", file=sys.stderr)
        return 2
    if args.lease_seconds is not None and args.lease_seconds < 1:
        print("ERROR: --lease-seconds must be positive", file=sys.stderr)
        return 2

    batch = Path(args.batch).expanduser().resolve()
    try:
        requested_limits: dict[str, int] = {}
        for value in args.domain_limit:
            host_text, count_text = assignment(value, "--domain-limit")
            host = normalized_host(host_text)
            try:
                count = int(count_text)
            except ValueError as exc:
                raise BatchError(f"invalid domain concurrency count: {count_text}") from exc
            if count < 1 or host in requested_limits:
                raise BatchError("domain limits must be positive and unique")
            requested_limits[host] = count
        removals = [normalized_host(value) for value in args.remove_domain_limit]
        if len(set(removals)) != len(removals):
            raise BatchError("domains to remove must be unique")
        overlap = set(requested_limits).intersection(removals)
        if overlap:
            raise BatchError(f"domain cannot be set and removed together: {sorted(overlap)[0]}")
        priorities: dict[str, int] = {}
        for value in args.priority:
            job_id, priority_text = assignment(value, "--priority")
            if not TECHNICAL_ID_RE.fullmatch(job_id) or job_id in priorities:
                raise BatchError(f"invalid or duplicate priority job ID: {job_id}")
            try:
                priorities[job_id] = int(priority_text)
            except ValueError as exc:
                raise BatchError(f"invalid priority integer: {priority_text}") from exc

        with batch_lock(batch):
            state = load_state(batch)
            known_domains = {job["domain"] for job in state["jobs"]}
            unknown_domains = (set(requested_limits) | set(removals)) - known_domains
            if unknown_domains:
                raise BatchError(
                    f"configuration references unknown job domain: {sorted(unknown_domains)[0]}"
                )
            if args.max_workers is not None:
                state["max_workers"] = args.max_workers
            if args.max_per_domain is not None:
                state["max_per_domain"] = args.max_per_domain
            if args.lease_seconds is not None:
                state["default_lease_seconds"] = args.lease_seconds
            state["domain_limits"].update(requested_limits)
            for host in removals:
                state["domain_limits"].pop(host, None)
            for job_id, priority in priorities.items():
                find_job(state, job_id)["priority"] = priority

            running = [job for job in state["jobs"] if job["state"] == "running"]
            if len(running) > state["max_workers"]:
                raise BatchError("new max_workers is below the current running job count")
            for domain, count in Counter(job["domain"] for job in running).items():
                if count > domain_limit(state, domain):
                    raise BatchError(
                        f"new limit for {domain} is below its current running job count"
                    )
            now = utc_now()
            touch_state(state, now)
            atomic_write_json(state_path(batch), state)
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"Configured batch: max_workers={state['max_workers']}, "
        f"max_per_domain={state['max_per_domain']}, "
        f"lease_seconds={state['default_lease_seconds']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
