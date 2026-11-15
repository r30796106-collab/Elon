import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main as elon_main
from engine.patch_approval import PatchApprovalManager
from engine.task_engine import TaskEngine


class ManualPatchProposalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.first = self.workspace / "app.py"
        self.second = self.workspace / "helpers.py"
        self.first.write_text("VALUE = 1\n", encoding="utf-8")
        self.second.write_text("OFFSET = 2\n", encoding="utf-8")
        self.engine = TaskEngine(self.root)
        self.task_id = self.engine.create_task(
            "Update calculator",
            "Update calculator values safely.",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _patch(self, path, content):
        original = (self.root / path).read_bytes()
        return {
            "path": path,
            "expected_sha256": hashlib.sha256(original).hexdigest(),
            "content": content,
        }

    def test_saves_manual_multi_file_proposal_without_ai_or_source_changes(self):
        proposal_path = self.workspace / "manual_patch.json"
        data = {
            "task_id": self.task_id,
            "summary": "Update two calculator modules",
            "test_name": "full",
            "patches": [
                self._patch("workspace/app.py", "VALUE = 10\n"),
                self._patch("workspace/helpers.py", "OFFSET = 20\n"),
            ],
        }
        proposal_path.write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        original_first = self.first.read_bytes()
        original_second = self.second.read_bytes()
        output = io.StringIO()

        with patch.object(elon_main, "ROOT", self.root), patch.object(
            sys,
            "argv",
            ["main.py", "patch-propose-json", "workspace/manual_patch.json"],
        ), redirect_stdout(output):
            elon_main.main()

        self.assertIn("MANUAL PATCH PROPOSAL SAVED", output.getvalue())
        self.assertIn("AI provider calls: DISABLED", output.getvalue())
        records = PatchApprovalManager(self.engine).list_proposals()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "pending")
        self.assertEqual(len(records[0]["patches"]), 2)
        self.assertEqual(self.first.read_bytes(), original_first)
        self.assertEqual(self.second.read_bytes(), original_second)

    def test_rejects_proposal_json_outside_workspace(self):
        output = io.StringIO()
        with patch.object(elon_main, "ROOT", self.root), patch.object(
            sys,
            "argv",
            ["main.py", "patch-propose-json", "../outside.json"],
        ), redirect_stdout(output):
            with self.assertRaises(SystemExit):
                elon_main.main()

        self.assertEqual(
            PatchApprovalManager(self.engine).list_proposals(),
            [],
        )


if __name__ == "__main__":
    unittest.main()
