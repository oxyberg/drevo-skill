"""Shared state, locking, and validation for capture batches."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
JOB_STATES = {"pending", "running", "captured", "audited", "failed"}
TECHNICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class BatchError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BatchError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BatchError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise BatchError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def one_line(value: str) -> str:
    return " ".join(value.splitlines()).strip()


def positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def batch_lock(batch: Path, timeout: float = 10.0, stale_after: float = 120.0) -> Iterator[None]:
    """Serialize short state transitions using an atomic lock directory."""
    if not batch.is_dir():
        raise BatchError(f"batch directory does not exist: {batch}")
    lock = batch / ".batch-state.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > stale_after:
                    lock.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise BatchError("timed out waiting for the batch state lock")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except FileNotFoundError:
            pass


def state_path(batch: Path) -> Path:
    return batch / "batch-state.json"


def load_state(batch: Path) -> dict[str, Any]:
    path = state_path(batch)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BatchError(f"batch state does not exist: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError(f"cannot read batch-state.json: {exc}") from exc
    validate_state(value)
    return value


def validate_state(value: Any) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise BatchError("unsupported batch-state.json schema")
    for field in ("default_lease_seconds", "max_workers", "max_per_domain"):
        if not positive_int(value.get(field)):
            raise BatchError(f"{field} must be a positive integer")
    parse_time(value.get("created_at"), "created_at")
    parse_time(value.get("updated_at"), "updated_at")
    domain_limits = value.get("domain_limits")
    if not isinstance(domain_limits, dict) or not all(
        isinstance(host, str)
        and bool(host)
        and host == host.rstrip(".").lower()
        and not any(character.isspace() for character in host)
        and positive_int(limit)
        for host, limit in domain_limits.items()
    ):
        raise BatchError("domain_limits must map hostnames to positive integers")
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise BatchError("jobs must be a non-empty list")
    seen_ids: set[str] = set()
    seen_packages: set[str] = set()
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise BatchError(f"job {index} must be an object")
        job_id = job.get("id")
        package = job.get("package")
        domain = job.get("domain")
        state = job.get("state")
        if not isinstance(job_id, str) or not TECHNICAL_ID_RE.fullmatch(job_id):
            raise BatchError(f"job {index}: invalid technical id")
        if job_id in seen_ids:
            raise BatchError(f"duplicate job id: {job_id}")
        seen_ids.add(job_id)
        if not isinstance(package, str) or not package:
            raise BatchError(f"job {job_id}: package must be a relative path")
        package_path = Path(package)
        if package_path.is_absolute() or ".." in package_path.parts or package_path == Path("."):
            raise BatchError(f"job {job_id}: package path must stay below the batch directory")
        if package in seen_packages:
            raise BatchError(f"duplicate package path: {package}")
        seen_packages.add(package)
        if (
            not isinstance(domain, str)
            or not domain
            or domain != domain.rstrip(".").lower()
            or any(character.isspace() for character in domain)
        ):
            raise BatchError(f"job {job_id}: domain must be a non-empty string")
        if state not in JOB_STATES:
            raise BatchError(f"job {job_id}: invalid state {state!r}")
        if (
            not isinstance(job.get("attempt"), int)
            or isinstance(job["attempt"], bool)
            or job["attempt"] < 0
        ):
            raise BatchError(f"job {job_id}: attempt must be a non-negative integer")
        if not isinstance(job.get("priority"), int) or isinstance(job["priority"], bool):
            raise BatchError(f"job {job_id}: priority must be an integer")
        if state == "running":
            if not isinstance(job.get("worker_id"), str) or not TECHNICAL_ID_RE.fullmatch(
                job["worker_id"]
            ):
                raise BatchError(f"job {job_id}: running job must have worker_id")
            parse_time(job.get("claimed_at"), f"job {job_id}.claimed_at")
            parse_time(job.get("heartbeat_at"), f"job {job_id}.heartbeat_at")
            parse_time(job.get("lease_expires_at"), f"job {job_id}.lease_expires_at")
            if job.get("finished_at") is not None:
                raise BatchError(f"job {job_id}: running job must not be finished")
        elif job.get("lease_expires_at") is not None:
            raise BatchError(f"job {job_id}: non-running job must not have a lease")
        else:
            for field in ("worker_id", "claimed_at", "heartbeat_at"):
                if job.get(field) is not None:
                    raise BatchError(f"job {job_id}: non-running job must clear {field}")
        if state in {"captured", "audited", "failed"}:
            parse_time(job.get("finished_at"), f"job {job_id}.finished_at")
        elif job.get("finished_at") is not None:
            raise BatchError(f"job {job_id}: unfinished state must not have finished_at")
        last_error = job.get("last_error")
        if last_error is not None and not isinstance(last_error, str):
            raise BatchError(f"job {job_id}: last_error must be a string or null")


def resolve_package(batch: Path, relative_name: str) -> Path:
    candidate = Path(relative_name)
    if candidate.is_absolute():
        raise BatchError("package path must be relative to the batch directory")
    resolved_batch = batch.resolve()
    resolved = (batch / candidate).resolve()
    try:
        resolved.relative_to(resolved_batch)
    except ValueError as exc:
        raise BatchError(f"package path escapes the batch directory: {relative_name}") from exc
    if resolved == resolved_batch:
        raise BatchError("package path must not be the batch directory itself")
    return resolved


def find_job(state: dict[str, Any], job_id: str) -> dict[str, Any]:
    for job in state["jobs"]:
        if job["id"] == job_id:
            return job
    raise BatchError(f"unknown job id: {job_id}")


def domain_limit(state: dict[str, Any], domain: str) -> int:
    return state["domain_limits"].get(domain, state["max_per_domain"])


def clear_lease(job: dict[str, Any]) -> None:
    job["worker_id"] = None
    job["claimed_at"] = None
    job["heartbeat_at"] = None
    job["lease_expires_at"] = None


def expire_leases(state: dict[str, Any], now: datetime) -> list[str]:
    expired: list[str] = []
    for job in state["jobs"]:
        if job["state"] != "running":
            continue
        if (
            parse_time(job["lease_expires_at"], f"job {job['id']}.lease_expires_at")
            > now
        ):
            continue
        job["state"] = "pending"
        job["last_error"] = "lease expired"
        clear_lease(job)
        expired.append(job["id"])
    return expired


def touch_state(state: dict[str, Any], now: datetime) -> None:
    state["updated_at"] = format_time(now)
