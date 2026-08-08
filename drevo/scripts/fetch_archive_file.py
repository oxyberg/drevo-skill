#!/usr/bin/env python3
"""Download an officially available archive file with provenance and SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from _evidence_utils import atomic_write_json, ensure_outputs_available, utc_now


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SYSTEM_GETADDRINFO = socket.getaddrinfo
PINNED_ADDRESSES: dict[tuple[str, int], list[tuple]] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one public official file without credentials, proxies, or private networks."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("url", help="Public HTTP(S) file URL")
    parser.add_argument("target", help="Destination file path")
    parser.add_argument("--metadata", help="Metadata JSON path")
    parser.add_argument("--expected-sha256", help="Fail if the downloaded hash differs")
    parser.add_argument("--max-bytes", type=int, help="Maximum accepted response size")
    parser.add_argument("--timeout", type=float, default=60.0, help="Network timeout in seconds")
    parser.add_argument(
        "--allow-query",
        action="store_true",
        help="Allow a non-secret query string; it remains visible to the process and server",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the target and metadata with rollback on ordinary write errors",
    )
    return parser.parse_args()


def redacted_url(value: str) -> tuple[str, bool]:
    parts = urllib.parse.urlsplit(value)
    return (
        urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
        bool(parts.query or parts.fragment),
    )


def _host_key(hostname: str, port: int) -> tuple[str, int]:
    return hostname.rstrip(".").lower(), port


def validate_url(value: str, allow_query: bool = False) -> None:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("URL must use HTTP or HTTPS and include a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials embedded in URLs are not allowed")
    if parts.query and not allow_query:
        raise ValueError("URL query requires explicit --allow-query acknowledgement")
    hostname = parts.hostname
    if hostname is None:
        raise ValueError("URL host is missing")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local and private hosts are not allowed")
    try:
        resolved = SYSTEM_GETADDRINFO(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"URL host cannot be resolved: {hostname}") from exc
    addresses = {
        ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in resolved
    }
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("URL host resolves to a non-public IP address")
    PINNED_ADDRESSES[_host_key(hostname, port)] = resolved


def pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Return only addresses validated before the connection, preventing DNS rebinding."""
    try:
        numeric_port = int(port)
    except (TypeError, ValueError):
        numeric_port = socket.getservbyname(str(port))
    records = PINNED_ADDRESSES.get(_host_key(str(host), numeric_port))
    if records is None:
        raise socket.gaierror("host was not approved by the public URL policy")
    filtered = [
        record
        for record in records
        if (family in (0, record[0]))
        and (type in (0, record[1]))
        and (proto in (0, record[2]))
    ]
    if not filtered:
        raise socket.gaierror("no approved address matches the requested socket parameters")
    return filtered


class PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects to local, private, link-local, or reserved networks."""

    def __init__(self, allow_query: bool):
        super().__init__()
        self.allow_query = allow_query

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_url(new_url, allow_query=self.allow_query)
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def _backup_existing(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".backup", dir=path.parent
    )
    os.close(descriptor)
    os.unlink(name)
    backup = Path(name)
    os.replace(path, backup)
    return backup


def _restore_backup(path: Path, backup: Optional[Path], existed_before: bool) -> None:
    if path.exists() and (backup is not None or not existed_before):
        path.unlink()
    if backup is not None and backup.exists():
        os.replace(backup, path)


def commit_file_and_metadata(
    temporary_name: str, target: Path, metadata: Path, record: dict[str, object]
) -> None:
    """Commit both outputs and roll back both after ordinary filesystem errors."""
    target_backup: Optional[Path] = None
    metadata_backup: Optional[Path] = None
    target_existed = target.exists()
    metadata_existed = metadata.exists()
    try:
        target_backup = _backup_existing(target)
        metadata_backup = _backup_existing(metadata)
        os.replace(temporary_name, target)
        atomic_write_json(metadata, record)
    except Exception:
        _restore_backup(target, target_backup, target_existed)
        _restore_backup(metadata, metadata_backup, metadata_existed)
        raise
    else:
        for backup in (target_backup, metadata_backup):
            if backup is not None and backup.exists():
                backup.unlink()


def main() -> int:
    args = parse_args()
    try:
        validate_url(args.url, allow_query=args.allow_query)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.expected_sha256 and not SHA256_RE.fullmatch(args.expected_sha256):
        print("ERROR: --expected-sha256 must contain 64 hexadecimal characters", file=sys.stderr)
        return 2
    if args.max_bytes is not None and args.max_bytes < 1:
        print("ERROR: --max-bytes must be positive", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("ERROR: --timeout must be positive", file=sys.stderr)
        return 2

    target = Path(args.target).expanduser()
    metadata = (
        Path(args.metadata).expanduser()
        if args.metadata
        else target.with_suffix(target.suffix + ".metadata.json")
    )
    if target.resolve() == metadata.resolve():
        print("ERROR: target and metadata paths must differ", file=sys.stderr)
        return 2
    try:
        ensure_outputs_available((target, metadata), args.overwrite)
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(
        args.url,
        headers={
            "User-Agent": "Drevo-Evidence-Fetcher/1.0",
            "Accept": "*/*",
        },
        method="GET",
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".download", dir=target.parent
    )
    downloaded = 0
    hasher = hashlib.sha256()
    response_metadata: dict[str, object] = {}
    try:
        with os.fdopen(descriptor, "wb") as output:
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    PublicOnlyRedirectHandler(args.allow_query),
                )
                previous_getaddrinfo = socket.getaddrinfo
                socket.getaddrinfo = pinned_getaddrinfo
                try:
                    response = opener.open(request, timeout=args.timeout)
                finally:
                    socket.getaddrinfo = previous_getaddrinfo
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"download failed: {exc}") from exc
            with response:
                declared_length = response.headers.get("Content-Length")
                if declared_length and args.max_bytes is not None:
                    try:
                        declared_value = int(declared_length)
                    except ValueError:
                        declared_value = None
                    if declared_value is not None and declared_value > args.max_bytes:
                        raise RuntimeError(
                            f"declared response size {declared_value} exceeds --max-bytes"
                        )
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if args.max_bytes is not None and downloaded > args.max_bytes:
                        raise RuntimeError("download exceeds --max-bytes")
                    output.write(chunk)
                    hasher.update(chunk)
                output.flush()
                os.fsync(output.fileno())
                validate_url(response.geturl(), allow_query=args.allow_query)
                final_url, final_redacted = redacted_url(response.geturl())
                response_metadata = {
                    "http_status": getattr(response, "status", None),
                    "final_url": final_url,
                    "final_url_query_redacted": final_redacted,
                    "content_type": response.headers.get("Content-Type"),
                    "declared_content_length": response.headers.get("Content-Length"),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }

        actual_hash = hasher.hexdigest()
        if args.expected_sha256 and actual_hash.lower() != args.expected_sha256.lower():
            raise RuntimeError(
                f"SHA-256 mismatch: downloaded {actual_hash}, expected {args.expected_sha256}"
            )
        source_url, source_redacted = redacted_url(args.url)
        record = {
            "schema_version": 1,
            "captured_at": utc_now(),
            "method": "public-http-download",
            "source_url": source_url,
            "source_url_query_redacted": source_redacted,
            **response_metadata,
            "file": target.name,
            "size_bytes": downloaded,
            "sha256": actual_hash,
            "limitations": (
                "No cookies, authorization headers, proxies, or session data were used. "
                "DNS addresses were pinned after public-address validation."
            ),
        }
        commit_file_and_metadata(temporary_name, target, metadata, record)
    except Exception as exc:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Downloaded {downloaded} byte(s): {target.resolve()}")
    print(f"SHA-256: {actual_hash}")
    print(f"Metadata: {metadata.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
