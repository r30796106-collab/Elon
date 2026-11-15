import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main as elon_main
from engine.project_scaffolder import ProjectScaffolder


class ProjectScaffoldCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "workspace").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, arguments):
        output = io.StringIO()
        with patch.object(elon_main, "ROOT", self.root), patch.object(
            sys,
            "argv",
            ["main.py", *arguments],
        ), redirect_stdout(output):
            elon_main.main()
        return output.getvalue()

    def test_python_scaffold_cli_requires_approval_before_creation(self):
        output = self.run_cli([
            "scaffold-propose",
            "cli_sample",
            "--language",
            "python",
            "--description",
            "CLI-generated sample project",
            "--requirements",
            "Provide a simple main entry point.",
        ])
        self.assertIn("PROJECT SCAFFOLD PROPOSAL SAVED", output)
        self.assertIn("AI provider calls: DISABLED", output)

        manager = ProjectScaffolder(self.root)
        record = manager.list_proposals()[0]
        target = self.root / "workspace" / "generated_projects" / "cli_sample"
        self.assertEqual(record["status"], "pending")
        self.assertFalse(target.exists())

        approved = self.run_cli([
            "scaffold-approve",
            record["proposal_id"],
        ])
        self.assertIn("SCAFFOLD APPROVED", approved)
        self.assertFalse(target.exists())

        applied = self.run_cli([
            "scaffold-apply",
            record["proposal_id"],
        ])
        self.assertIn("SCAFFOLD APPLIED", applied)
        self.assertTrue((target / "main.py").is_file())


if __name__ == "__main__":
    unittest.main()
