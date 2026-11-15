import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.project_scaffolder import ProjectScaffolder, ScaffoldingError


class ProjectScaffolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scaffolder = ProjectScaffolder(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def propose(self, name="sample_app", language="python"):
        return self.scaffolder.propose_template(
            name,
            language=language,
            description="A small sample application.",
            requirements="The application should expose a simple main entry point.",
        )

    def test_proposal_does_not_create_project_files(self):
        record = self.propose()

        self.assertEqual(record["status"], "pending")
        self.assertFalse(
            (self.root / "workspace" / "generated_projects" / "sample_app").exists()
        )
        self.assertEqual(len(record["files"]), 5)
        self.assertEqual(len(self.scaffolder.list_proposals()), 1)

    def test_apply_requires_explicit_approval(self):
        record = self.propose()

        with self.assertRaisesRegex(ScaffoldingError, "explicit approval"):
            self.scaffolder.apply(record["proposal_id"])

        self.assertFalse(
            (self.root / "workspace" / "generated_projects" / "sample_app").exists()
        )

    def test_approved_python_scaffold_is_created_and_idempotent(self):
        record = self.propose()
        self.scaffolder.approve(record["proposal_id"])
        applied = self.scaffolder.apply(record["proposal_id"])

        target = self.root / "workspace" / "generated_projects" / "sample_app"
        self.assertEqual(applied["status"], "applied")
        self.assertTrue((target / "main.py").is_file())
        self.assertTrue((target / "tests" / "test_smoke.py").is_file())
        self.assertTrue((target / "pyproject.toml").is_file())
        self.assertTrue((target / ".elon-scaffold.json").is_file())
        self.assertIn("A small sample application", (target / "README.md").read_text())

        repeated = self.scaffolder.apply(record["proposal_id"])
        self.assertEqual(repeated["status"], "applied")
        self.assertTrue(self.scaffolder.audit.verify())

    def test_verify_detects_modified_and_unexpected_generated_files(self):
        record = self.propose()
        self.scaffolder.approve(record["proposal_id"])
        self.scaffolder.apply(record["proposal_id"])

        target = self.root / "workspace" / "generated_projects" / "sample_app"
        (target / "main.py").write_text("print('changed')\n", encoding="utf-8")
        (target / "extra.txt").write_text("unexpected", encoding="utf-8")

        report = self.scaffolder.verify(record["proposal_id"])

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(report["read_only"])
        self.assertIn("main.py", report["modified"])
        self.assertIn("extra.txt", report["unexpected"])

    def test_approved_javascript_scaffold_is_created(self):
        record = self.propose("sample_web", "javascript")
        self.scaffolder.approve(record["proposal_id"])
        applied = self.scaffolder.apply(record["proposal_id"])

        target = self.root / "workspace" / "generated_projects" / "sample_web"
        self.assertEqual(applied["status"], "applied")
        self.assertTrue((target / "package.json").is_file())
        self.assertTrue((target / "src" / "index.mjs").is_file())
        self.assertTrue((target / "test" / "smoke.test.mjs").is_file())
        self.assertTrue(self.scaffolder.audit.verify())

    def test_invalid_project_name_and_language_are_rejected(self):
        for name in ("../escape", "UpperCase", ".hidden", "bad name"):
            with self.subTest(name=name):
                with self.assertRaises(ScaffoldingError):
                    self.propose(name)
        with self.assertRaises(ScaffoldingError):
            self.propose("valid_name", "ruby")

    def test_existing_destination_is_never_overwritten(self):
        target = self.root / "workspace" / "generated_projects" / "sample_app"
        target.mkdir(parents=True)
        sentinel = target / "user.txt"
        sentinel.write_text("keep me", encoding="utf-8")

        with self.assertRaisesRegex(ScaffoldingError, "already exists"):
            self.propose()

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")

    def test_apply_refuses_existing_unapproved_destination(self):
        record = self.propose()
        self.scaffolder.approve(record["proposal_id"])
        target = self.root / "workspace" / "generated_projects" / "sample_app"
        target.mkdir(parents=True)
        sentinel = target / "user.txt"
        sentinel.write_text("untouched", encoding="utf-8")

        with self.assertRaisesRegex(ScaffoldingError, "already exists"):
            self.scaffolder.apply(record["proposal_id"])

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")

    def test_interrupted_apply_can_resume_without_overwriting(self):
        record = self.propose()
        self.scaffolder.approve(record["proposal_id"])
        original_write = self.scaffolder._write_new_file
        calls = {"count": 0}

        def fail_second(target, relative, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated scaffold interruption")
            return original_write(target, relative, data)

        with patch.object(
            self.scaffolder,
            "_write_new_file",
            side_effect=fail_second,
        ):
            with self.assertRaises(OSError):
                self.scaffolder.apply(record["proposal_id"])

        saved = self.scaffolder._load(record["proposal_id"])
        self.assertEqual(saved["status"], "recovery_required")

        recovered = self.scaffolder.recover(
            record["proposal_id"],
            confirm_stopped=True,
        )
        target = self.root / "workspace" / "generated_projects" / "sample_app"
        self.assertEqual(recovered["status"], "applied")
        self.assertTrue((target / ".elon-scaffold.json").is_file())
        self.assertTrue((target / "main.py").is_file())
        self.assertTrue(self.scaffolder.audit.verify())

    def test_workspace_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "workspace").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(ScaffoldingError, "Workspace"):
            self.propose()

    def test_generated_root_symlink_is_rejected(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (workspace / "generated_projects").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(ScaffoldingError, "symlinks"):
            self.propose()

    def test_forged_approved_state_without_audit_event_is_rejected(self):
        record = self.propose()
        record["status"] = "approved"
        record["approved_at"] = self.scaffolder._now()
        self.scaffolder._write_record(
            self.scaffolder._proposal_path(record["proposal_id"]),
            record,
        )

        with self.assertRaisesRegex(ScaffoldingError, "audit evidence"):
            self.scaffolder.apply(record["proposal_id"])

        self.assertFalse(
            (self.root / "workspace" / "generated_projects" / "sample_app").exists()
        )

    def test_tampered_proposal_content_is_rejected_before_approval(self):
        record = self.propose()
        path = self.scaffolder._proposal_path(record["proposal_id"])
        saved = self.scaffolder._load(record["proposal_id"])
        saved["files"][0]["content"] += "tampered"
        self.scaffolder._write_record(path, saved)

        with self.assertRaisesRegex(ScaffoldingError, "hash or size"):
            self.scaffolder.approve(record["proposal_id"])

    def test_recovery_rejects_unexpected_files(self):
        record = self.propose()
        self.scaffolder.approve(record["proposal_id"])
        original_write = self.scaffolder._write_new_file
        calls = {"count": 0}

        def fail_second(target, relative, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated interruption")
            return original_write(target, relative, data)

        with patch.object(
            self.scaffolder,
            "_write_new_file",
            side_effect=fail_second,
        ):
            with self.assertRaises(OSError):
                self.scaffolder.apply(record["proposal_id"])

        target = self.root / "workspace" / "generated_projects" / "sample_app"
        (target / "unexpected.txt").write_text("not part of proposal")
        with self.assertRaisesRegex(ScaffoldingError, "Unexpected file"):
            self.scaffolder.recover(
                record["proposal_id"],
                confirm_stopped=True,
            )

    def test_recovery_requires_stopped_process_confirmation(self):
        record = self.propose()
        self.scaffolder.approve(record["proposal_id"])

        with patch.object(
            self.scaffolder,
            "_write_new_file",
            side_effect=OSError("simulated interruption"),
        ):
            with self.assertRaises(OSError):
                self.scaffolder.apply(record["proposal_id"])

        with self.assertRaisesRegex(ScaffoldingError, "confirmation"):
            self.scaffolder.recover(
                record["proposal_id"],
                confirm_stopped=False,
            )


if __name__ == "__main__":
    unittest.main()
