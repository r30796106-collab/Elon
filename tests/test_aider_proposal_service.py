import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.aider_proposal_service import (
    AiderProposalError,
    AiderProposalService,
)
from engine.task_engine import TaskEngine
from engine.patch_approval import PatchApprovalError


class AiderProposalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.target = self.engine.workspace / "sample.py"
        self.target.write_text("value = 10\n", encoding="utf-8")
        self.task_id = self.engine.create_task("Aider adapter test")

        self.service = AiderProposalService(
            self.engine,
            aider_path="/fake/aider",
            environ={
                "PATH": "/usr/bin",
                "OPENROUTER_API_KEY": "test-key-not-real",
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_creates_pending_proposal_without_modifying_source(self, run):
        def fake_aider(command, **kwargs):
            candidate = Path(kwargs["cwd"]) / "sample.py"
            candidate.write_text("value = 20\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "edited", "")

        run.side_effect = fake_aider

        result = self.service.generate_proposal(
            self.task_id,
            ["sample.py"],
            "Change value to 20",
        )

        self.assertTrue(result["success"])
        proposal = result["proposal"]
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(len(proposal["patches"]), 1)
        self.assertEqual(
            proposal["patches"][0]["path"],
            "workspace/sample.py",
        )
        self.assertEqual(
            proposal["patches"][0]["expected_sha256"],
            hashlib.sha256(b"value = 10\n").hexdigest(),
        )
        self.assertEqual(self.target.read_text(), "value = 10\n")
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["shell"])

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_no_change_creates_no_proposal(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["aider"], 0, "no changes", ""
        )

        result = self.service.generate_proposal(
            self.task_id,
            ["sample.py"],
            "Review this file",
        )

        self.assertIsNone(result["proposal"])
        self.assertEqual(self.target.read_text(), "value = 10\n")

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_path_traversal_before_running_aider(self, run):
        with self.assertRaisesRegex(AiderProposalError, "Path traversal"):
            self.service.generate_proposal(
                self.task_id,
                ["../main.py"],
                "Modify a file",
            )
        run.assert_not_called()

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_missing_api_key(self, run):
        self.service.environ.pop("OPENROUTER_API_KEY")

        with self.assertRaisesRegex(AiderProposalError, "not configured"):
            self.service.generate_proposal(
                self.task_id,
                ["sample.py"],
                "Modify a file",
            )
        run.assert_not_called()

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_unexpected_file_created_by_aider(self, run):
        def fake_aider(command, **kwargs):
            (Path(kwargs["cwd"]) / "unexpected.py").write_text(
                "unsafe = True\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = fake_aider

        with self.assertRaisesRegex(AiderProposalError, "unexpected file"):
            self.service.generate_proposal(
                self.task_id,
                ["sample.py"],
                "Create another file",
            )

        self.assertEqual(self.target.read_text(), "value = 10\n")


    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_unknown_aider_metadata_file(self, run):
        def fake_aider(command, **kwargs):
            (Path(kwargs["cwd"]) / ".aider.evil").write_text("unexpected")
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = fake_aider
        with self.assertRaisesRegex(AiderProposalError, "unexpected file"):
            self.service.generate_proposal(self.task_id, ["sample.py"], "Test")
        self.assertEqual(self.target.read_text(), "value = 10\n")

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_symlink_created_by_aider(self, run):
        def fake_aider(command, **kwargs):
            link = Path(kwargs["cwd"]) / "evil-link"
            link.symlink_to(self.target)
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = fake_aider
        with self.assertRaisesRegex(AiderProposalError, "symbolic link"):
            self.service.generate_proposal(self.task_id, ["sample.py"], "Test")

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_handles_aider_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired("aider", 180)
        with self.assertRaisesRegex(AiderProposalError, "timed out"):
            self.service.generate_proposal(self.task_id, ["sample.py"], "Test")

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_nonzero_aider_exit(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["aider"], 2, "", "provider failure"
        )
        with self.assertRaisesRegex(AiderProposalError, "exit code 2"):
            self.service.generate_proposal(self.task_id, ["sample.py"], "Test")



    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_oversized_source_before_running_aider(self, run):
        self.target.write_bytes(b"x" * (self.service.MAX_SOURCE_BYTES + 1))
        with self.assertRaisesRegex(AiderProposalError, "exceeds 2 MB"):
            self.service.generate_proposal(self.task_id, ["sample.py"], "Test")
        run.assert_not_called()

    @patch("engine.aider_proposal_service.subprocess.run")
    def test_rejects_source_changed_during_aider_execution(self, run):
        def fake_aider(command, **kwargs):
            candidate = Path(kwargs["cwd"]) / "sample.py"
            candidate.write_text("value = 20\n", encoding="utf-8")
            self.target.write_text("value = 99\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = fake_aider
        with self.assertRaisesRegex(PatchApprovalError, "File changed since inspection"):
            self.service.generate_proposal(self.task_id, ["sample.py"], "Change value")

        self.assertEqual(self.target.read_text(), "value = 99\n")

if __name__ == "__main__":
    unittest.main()
