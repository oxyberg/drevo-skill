from __future__ import annotations

import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "drevo" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


utils = load_script("_evidence_utils")
fetch = load_script("fetch_archive_file")
bands = load_script("split_image_bands")


class EvidenceUtilsTests(unittest.TestCase):
    def test_source_keys_distinguish_identical_copies(self):
        digest = "a" * 64
        first = utils.source_key(1, Path("same.png"), digest, False)
        second = utils.source_key(2, Path("same.png"), digest, False)
        self.assertNotEqual(first, second)

    def test_redacted_identity_omits_basename(self):
        identity = utils.source_identity(1, Path("private-name.png"), "b" * 64, True)
        self.assertNotIn("source_file", identity)
        self.assertNotIn("private-name", json.dumps(identity))


class BandPlanningTests(unittest.TestCase):
    def test_starts_cover_full_axis_with_overlap(self):
        starts = bands.starts_for(length=1700, size=600, overlap=150)
        intervals = [(start, min(start + 600, 1700)) for start in starts]
        self.assertEqual(intervals[0][0], 0)
        self.assertEqual(intervals[-1][1], 1700)
        self.assertTrue(
            all(
                right_start <= left_end
                for (_, left_end), (right_start, _) in zip(intervals, intervals[1:])
            )
        )


class FetchPolicyTests(unittest.TestCase):
    def setUp(self):
        fetch.PINNED_ADDRESSES.clear()

    def test_private_literal_addresses_are_rejected(self):
        for url in (
            "http://127.0.0.1/file",
            "http://10.0.0.1/file",
            "http://169.254.1.1/file",
            "http://[::1]/file",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch.validate_url(url)

    def test_query_requires_acknowledgement_and_dns_is_pinned(self):
        records = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
        with mock.patch.object(fetch, "SYSTEM_GETADDRINFO", return_value=records):
            with self.assertRaises(ValueError):
                fetch.validate_url("https://archive.example/file?id=public")
            fetch.validate_url(
                "https://archive.example/file?id=public", allow_query=True
            )
        pinned = fetch.pinned_getaddrinfo(
            "archive.example", 443, type=socket.SOCK_STREAM
        )
        self.assertEqual(pinned[0][4][0], "93.184.216.34")

    def test_pair_commit_rolls_back_after_metadata_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "file.bin"
            metadata = root / "file.json"
            incoming = root / "incoming.tmp"
            target.write_bytes(b"old file")
            metadata.write_text("old metadata", encoding="utf-8")
            incoming.write_bytes(b"new file")
            with mock.patch.object(
                fetch, "atomic_write_json", side_effect=OSError("synthetic failure")
            ):
                with self.assertRaises(OSError):
                    fetch.commit_file_and_metadata(
                        str(incoming), target, metadata, {"new": True}
                    )
            self.assertEqual(target.read_bytes(), b"old file")
            self.assertEqual(metadata.read_text(encoding="utf-8"), "old metadata")
            self.assertEqual(list(root.glob("*.backup")), [])


if __name__ == "__main__":
    unittest.main()
