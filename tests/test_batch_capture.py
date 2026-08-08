from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "drevo" / "scripts"


def run_script(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def create_package(path: Path, viewer_url: str) -> None:
    path.mkdir(parents=True)
    for name in (
        "исходные-кадры",
        "собранные-изображения",
        "производные",
        "контроль",
    ):
        (path / name).mkdir()
    state = {
        "schema_version": 1,
        "archive": "Example repository",
        "reference": path.name,
        "catalog_url": "",
        "viewer_url": viewer_url,
        "document_id": "",
        "capture_settings": {
            "zoom": "100%",
            "device_pixel_ratio": 1,
            "viewport": {"width": 64, "height": 64},
        },
        "expected_positions": 1,
        "expected_tiles": ["full"],
        "completed_positions": [],
        "exceptions": {},
    }
    (path / "capture-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (path / "page-sizes.json").write_text(
        '{"schema_version": 1, "positions": []}\n', encoding="utf-8"
    )
    (path / "README.md").write_text("# Synthetic package\n", encoding="utf-8")
    (path / "sha256.txt").write_text("", encoding="utf-8")


class CaptureBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.batch = Path(self.temporary.name) / "batch"
        create_package(self.batch / "cases" / "case-001", "https://viewer-a.example/1")
        create_package(self.batch / "cases" / "case-002", "https://viewer-a.example/2")
        create_package(self.batch / "cases" / "case-003", "https://viewer-b.example/3")
        result = run_script(
            "init_capture_batch.py",
            str(self.batch),
            "--job",
            "case-001=cases/case-001",
            "--job",
            "case-002=cases/case-002",
            "--job",
            "case-003=cases/case-003",
            "--max-workers",
            "3",
            "--max-per-domain",
            "1",
            "--lease-seconds",
            "60",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.temporary.cleanup()

    def test_concurrent_claims_are_unique_and_respect_domain_limit(self):
        def claim(number: int) -> subprocess.CompletedProcess[str]:
            return run_script(
                "claim_capture_job.py",
                str(self.batch),
                "--worker-id",
                f"worker-{number:02d}",
                "--json",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(claim, range(1, 9)))
        claimed = [json.loads(result.stdout) for result in results if result.returncode == 0]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({item["job_id"] for item in claimed}), 2)
        self.assertEqual(
            {item["domain"] for item in claimed},
            {"viewer-a.example", "viewer-b.example"},
        )
        self.assertTrue(all(result.returncode in {0, 3} for result in results))

    def test_expired_lease_is_reclaimed(self):
        first = run_script(
            "claim_capture_job.py",
            str(self.batch),
            "--worker-id",
            "worker-01",
            "--domain",
            "viewer-a.example",
            "--json",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_job = json.loads(first.stdout)["job_id"]
        state_path = self.batch / "batch-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        job = next(item for item in state["jobs"] if item["id"] == first_job)
        job["lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).replace(microsecond=0).isoformat()
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        second = run_script(
            "claim_capture_job.py",
            str(self.batch),
            "--worker-id",
            "worker-02",
            "--domain",
            "viewer-a.example",
            "--json",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        reclaimed = json.loads(second.stdout)
        self.assertEqual(reclaimed["job_id"], first_job)
        self.assertEqual(reclaimed["attempt"], 2)

    def test_batch_preflight_audits_every_package(self):
        result = run_script(
            "audit_capture_batch.py",
            "--preflight",
            "--workers",
            "3",
            str(self.batch),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 package audit(s) passed", result.stdout)

    def test_status_is_machine_readable_and_does_not_claim(self):
        result = run_script("capture_batch_status.py", "--json", str(self.batch))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["counts"], {"pending": 3})
        self.assertEqual(status["expired_running"], [])
        state = json.loads((self.batch / "batch-state.json").read_text(encoding="utf-8"))
        self.assertTrue(all(job["attempt"] == 0 for job in state["jobs"]))

    def test_owner_heartbeat_failure_and_retry_transitions(self):
        claim = run_script(
            "claim_capture_job.py",
            str(self.batch),
            "--worker-id",
            "worker-01",
            "--domain",
            "viewer-a.example",
            "--json",
        )
        self.assertEqual(claim.returncode, 0, claim.stderr)
        job_id = json.loads(claim.stdout)["job_id"]
        rejected = run_script(
            "update_capture_job.py",
            str(self.batch),
            "--job",
            job_id,
            "--worker-id",
            "worker-02",
            "--heartbeat",
        )
        self.assertEqual(rejected.returncode, 1)
        heartbeat = run_script(
            "update_capture_job.py",
            str(self.batch),
            "--job",
            job_id,
            "--worker-id",
            "worker-01",
            "--heartbeat",
            "--lease-seconds",
            "120",
        )
        self.assertEqual(heartbeat.returncode, 0, heartbeat.stderr)
        failed = run_script(
            "update_capture_job.py",
            str(self.batch),
            "--job",
            job_id,
            "--worker-id",
            "worker-01",
            "--fail",
            "synthetic interruption",
        )
        self.assertEqual(failed.returncode, 0, failed.stderr)
        retry = run_script(
            "update_capture_job.py", str(self.batch), "--job", job_id, "--retry"
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_configuration_preserves_running_limits_and_applies_priority(self):
        for number in (1, 2):
            claim = run_script(
                "claim_capture_job.py",
                str(self.batch),
                "--worker-id",
                f"worker-{number:02d}",
                "--json",
            )
            self.assertEqual(claim.returncode, 0, claim.stderr)
        rejected = run_script(
            "configure_capture_batch.py",
            str(self.batch),
            "--max-workers",
            "1",
        )
        self.assertEqual(rejected.returncode, 1)
        state = json.loads((self.batch / "batch-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["max_workers"], 3)
        configured = run_script(
            "configure_capture_batch.py",
            str(self.batch),
            "--max-workers",
            "4",
            "--domain-limit",
            "viewer-a.example=2",
            "--lease-seconds",
            "120",
            "--priority",
            "case-002=10",
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        third = run_script(
            "claim_capture_job.py",
            str(self.batch),
            "--worker-id",
            "worker-03",
            "--json",
        )
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(json.loads(third.stdout)["job_id"], "case-002")


if __name__ == "__main__":
    unittest.main()
