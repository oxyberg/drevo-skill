#!/usr/bin/env python3
"""Audit every package and queue state in a parallel capture batch."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from _batch_utils import (
    BatchError,
    atomic_write_json,
    batch_lock,
    domain_limit,
    find_job,
    format_time,
    load_state,
    parse_time,
    resolve_package,
    state_path,
    touch_state,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit queue integrity and all eligible capture packages.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("batch", help="Capture batch directory")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Audit preparation for every package without requiring captured files",
    )
    parser.add_argument(
        "--mark-audited",
        action="store_true",
        help="Move passing captured jobs to audited",
    )
    parser.add_argument("--workers", type=int, default=1, help="Concurrent package audits")
    return parser.parse_args()


def package_snapshot(package: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (
            path.relative_to(package).as_posix(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in package.rglob("*")
        if path.is_file()
    )


def run_command(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.rstrip()


def audit_package(
    scripts: Path, job: dict[str, Any], package: Path, preflight: bool
) -> tuple[str, bool, list[str]]:
    output: list[str] = []
    try:
        before = package_snapshot(package)
        audit_command = [sys.executable, str(scripts / "audit_capture_package.py")]
        if preflight:
            audit_command.append("--preflight")
        audit_command.append(str(package))
        audit_code, audit_output = run_command(audit_command)
        if audit_output:
            output.extend(audit_output.splitlines())
        passed = audit_code == 0
        if passed and not preflight:
            checksum_code, checksum_output = run_command(
                [
                    sys.executable,
                    str(scripts / "verify_checksums.py"),
                    "--strict",
                    str(package),
                ]
            )
            if checksum_output:
                output.extend(checksum_output.splitlines())
            passed = checksum_code == 0
        after = package_snapshot(package)
        if before != after:
            output.append("ERROR: package changed while it was being audited")
            passed = False
    except (OSError, subprocess.SubprocessError) as exc:
        output.append(f"ERROR: audit execution failed: {exc}")
        passed = False
    return job["id"], passed, output


def main() -> int:
    args = parse_args()
    if args.preflight and args.mark_audited:
        print("ERROR: --mark-audited cannot be used with --preflight", file=sys.stderr)
        return 2
    if args.workers < 1 or args.workers > 32:
        print("ERROR: --workers must be in 1..32", file=sys.stderr)
        return 2

    batch = Path(args.batch).expanduser().resolve()
    try:
        state = load_state(batch)
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    now = utc_now()
    errors: list[str] = []
    work: list[tuple[dict[str, Any], Path]] = []
    running = [job for job in state["jobs"] if job["state"] == "running"]
    if len(running) > state["max_workers"]:
        errors.append("running job count exceeds max_workers")
    for domain, count in Counter(job["domain"] for job in running).items():
        if count > domain_limit(state, domain):
            errors.append(f"running jobs for {domain} exceed its domain limit")
    for job in state["jobs"]:
        try:
            package = resolve_package(batch, job["package"])
        except BatchError as exc:
            errors.append(f"job {job['id']}: {exc}")
            continue
        if not package.is_dir():
            errors.append(f"job {job['id']}: package directory does not exist")
            continue
        if job["state"] == "running":
            expires = parse_time(
                job["lease_expires_at"], f"job {job['id']}.lease_expires_at"
            )
            if expires <= now:
                errors.append(f"job {job['id']}: running lease has expired")
        if args.preflight:
            work.append((job, package))
        elif job["state"] in {"captured", "audited"}:
            work.append((job, package))
        else:
            errors.append(f"job {job['id']}: batch is incomplete with state {job['state']}")

    scripts = Path(__file__).resolve().parent
    results: list[tuple[str, bool, list[str]]] = []
    if work:
        with ThreadPoolExecutor(max_workers=min(args.workers, len(work))) as executor:
            futures = [
                executor.submit(audit_package, scripts, job, package, args.preflight)
                for job, package in work
            ]
            results = [future.result() for future in futures]

    passed_ids: list[str] = []
    for job_id, passed, output in results:
        for line in output:
            print(f"[{job_id}] {line}")
        if passed:
            passed_ids.append(job_id)
        else:
            errors.append(f"job {job_id}: package audit failed")

    if args.mark_audited and passed_ids:
        try:
            with batch_lock(batch):
                fresh = load_state(batch)
                changed = False
                transition_time = utc_now()
                for job_id in passed_ids:
                    job = find_job(fresh, job_id)
                    if job["state"] == "captured":
                        job["state"] = "audited"
                        job["audited_at"] = format_time(transition_time)
                        changed = True
                    elif job["state"] != "audited":
                        raise BatchError(
                            f"job {job_id} changed state during audit: {job['state']}"
                        )
                if changed:
                    touch_state(fresh, transition_time)
                    atomic_write_json(state_path(batch), fresh)
        except BatchError as exc:
            errors.append(str(exc))

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(
        f"Batch summary: {len(state['jobs'])} job(s), "
        f"{len(passed_ids)} package audit(s) passed, {len(errors)} error(s)."
    )
    if errors:
        return 1
    print("Batch preflight passed." if args.preflight else "Batch audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
