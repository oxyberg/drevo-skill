from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "drevo" / "scripts" / "audit_git_payload.py"
INIT_SCRIPT = REPOSITORY / "drevo" / "scripts" / "init_project.py"


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def run_audit(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repository", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


class GitPayloadAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy_path = (
            self.root / "research" / "evidence" / "storage-policy.json"
        )
        self.policy_path.parent.mkdir(parents=True)
        self.write_policy()
        self.assertEqual(git(self.root, "init", "-q").returncode, 0)
        self.assertEqual(
            git(self.root, "config", "user.email", "test@example.invalid").returncode,
            0,
        )
        self.assertEqual(
            git(self.root, "config", "user.name", "Drevo Test").returncode, 0
        )
        self.assertEqual(git(self.root, "add", ".").returncode, 0)
        self.assertEqual(git(self.root, "commit", "-qm", "initial").returncode, 0)

    def tearDown(self):
        self.temporary.cleanup()

    def write_policy(
        self,
        *,
        mode: str = "minimal",
        max_file: float = 10,
        max_total: float = 50,
        max_payloads: int = 25,
    ) -> None:
        policy = {
            "schema_version": 1,
            "mode": mode,
            "max_staged_file_mib": max_file,
            "max_staged_total_mib": max_total,
            "max_staged_evidence_payload_files": max_payloads,
        }
        self.policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def stage_payload(self, name: str, content: bytes = b"scan") -> None:
        path = self.root / "research" / "evidence" / "documents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.assertEqual(git(self.root, "add", str(path)).returncode, 0)

    def test_minimal_mode_rejects_oversized_staged_file(self):
        self.write_policy(max_file=0.0001)
        self.stage_payload("page.png", b"x" * 1024)
        result = run_audit(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("file exceeds", result.stderr)

    def test_minimal_mode_rejects_too_many_evidence_payloads(self):
        self.write_policy(max_payloads=1)
        self.stage_payload("page-1.jpg")
        self.stage_payload("page-2.jpg")
        result = run_audit(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("payload count exceeds", result.stderr)

    def test_full_repository_mode_allows_payload_after_explicit_policy_change(self):
        self.write_policy(mode="full-repository", max_file=0.0001)
        self.assertEqual(git(self.root, "add", str(self.policy_path)).returncode, 0)
        self.stage_payload("page.png", b"x" * 1024)
        result = run_audit(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("full-repository mode is enabled", result.stdout)


class ProjectTemplateStorageTests(unittest.TestCase):
    def test_new_project_has_minimal_agents_and_cold_storage_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            result = subprocess.run(
                [sys.executable, str(INIT_SCRIPT), str(project)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (project / "AGENTS.md").read_text(encoding="utf-8"),
                "# Контекст проекта\n\n"
                "- [Обязательные правила](PROJECT_RULES.md)\n"
                "- [Текущая стратегия](research/strategy.md)\n",
            )
            self.assertTrue(
                (project / "research/evidence/storage-policy.json").is_file()
            )
            self.assertTrue(
                (
                    project
                    / "research/evidence/documents/cold-storage-index.md"
                ).is_file()
            )
            self.assertEqual(git(project, "init", "-q").returncode, 0)
            ignored = git(
                project,
                "check-ignore",
                "research/evidence/documents/example/исходные-кадры/page.png",
                "research/evidence/documents/cold-storage.local",
            )
            self.assertEqual(ignored.returncode, 0, ignored.stderr)
            self.assertEqual(len(ignored.stdout.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
