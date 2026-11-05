import hashlib
import tempfile
import unittest
from pathlib import Path

from engine.task_engine import TaskEngine
from security.policy import PolicyError


class ElonCoreTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_task(self):
        task_id = self.engine.create_task("Test task", "Description")
        task = self.engine.get_task(task_id)

        self.assertEqual(task["title"], "Test task")
        self.assertEqual(task["status"], "pending")

    def test_patch_and_backup(self):
        target = self.root / "workspace" / "example.txt"
        target.write_text("original")

        task_id = self.engine.create_task("Modify file")
        old_hash = hashlib.sha256(b"original").hexdigest()

        result = self.engine.apply_patch(
            task_id,
            "example.txt",
            "updated",
            old_hash
        )

        self.assertTrue(result["success"])
        self.assertEqual(target.read_text(), "updated")
        self.assertTrue(Path(result["backup"]).exists())

    def test_wrong_hash_rejected(self):
        target = self.root / "workspace" / "example.txt"
        target.write_text("original")

        task_id = self.engine.create_task("Modify file")

        with self.assertRaises(PolicyError):
            self.engine.apply_patch(
                task_id, "example.txt", "updated", "wrong"
            )

        self.assertEqual(target.read_text(), "original")

    def test_path_traversal_blocked(self):
        target = self.root / "outside.txt"
        target.write_text("protected")

        task_id = self.engine.create_task("Security test")

        with self.assertRaises(PolicyError):
            self.engine.apply_patch(
                task_id,
                "../outside.txt",
                "changed",
                hashlib.sha256(b"protected").hexdigest()
            )

        self.assertEqual(target.read_text(), "protected")

    def test_repair_limit(self):
        target = self.root / "workspace" / "example.txt"
        target.write_text("version0")

        task_id = self.engine.create_task("Repair test")

        for i in range(5):
            old = target.read_bytes()
            digest = hashlib.sha256(old).hexdigest()

            self.engine.apply_patch(
                task_id, "example.txt",
                f"version{i+1}", digest, repair=True
            )

        old = target.read_bytes()

        with self.assertRaises(PolicyError):
            self.engine.apply_patch(
                task_id,
                "example.txt",
                "version6",
                hashlib.sha256(old).hexdigest(),
                repair=True
            )

    def test_audit_integrity(self):
        self.engine.create_task("Audit test")
        self.assertTrue(self.engine.audit_valid())

    def test_unregistered_test_blocked(self):
        with self.assertRaises(PolicyError):
            self.engine.run_registered_tests("unknown")


if __name__ == "__main__":
    unittest.main()
